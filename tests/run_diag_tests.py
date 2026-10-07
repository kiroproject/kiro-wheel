"""Plugin journal and account-status tests on a throwaway PostgreSQL (pgserver).

Run:  python tests/run_diag_tests.py     (needs: pgserver sqlalchemy[asyncio] asyncpg)
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pgserver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND = Path(__file__).resolve().parents[1] / "backend" / "kiro_wheel"
spec = importlib.util.spec_from_file_location("diag", BACKEND / "diag.py")
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

# the real migration SQL for the journal table, copied from storage._upgrade_0004
storage_src = (BACKEND / "storage.py").read_text(encoding="utf-8")
LOG_DDL = storage_src.split("CREATE TABLE IF NOT EXISTS ext_kiro_wheel_log (")[1].split('"""')[0]
LOG_DDL = "CREATE TABLE IF NOT EXISTS ext_kiro_wheel_log (" + LOG_DDL

DDL = [
    LOG_DDL,
    "CREATE TABLE ext_kiro_wheel_spins (id TEXT PRIMARY KEY, user_id BIGINT, status TEXT DEFAULT 'claimed', created_at TIMESTAMPTZ DEFAULT NOW())",
    "CREATE TABLE ext_kiro_wheel_prizes (id SERIAL PRIMARY KEY, kind TEXT, enabled BOOLEAN DEFAULT TRUE, deleted_at TIMESTAMPTZ)",
    "CREATE TABLE ext_kiro_wheel_gifts (code TEXT PRIMARY KEY, status TEXT DEFAULT 'open')",
    "CREATE TABLE ext_kiro_wheel_daily (user_id BIGINT PRIMARY KEY)",
    "CREATE TABLE users (user_id BIGINT PRIMARY KEY, is_banned BOOLEAN DEFAULT FALSE)",
]
PASSED = 0


def ok(cond: bool, label: str) -> None:
    global PASSED
    if not cond:
        raise AssertionError(label)
    PASSED += 1
    print(f"  ok  {label}")


def load_function(name: str):
    """Pull one async function out of logic.py without importing the Minishop core it depends on."""
    tree = ast.parse((BACKEND / "logic.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    namespace: dict[str, Any] = {"text": text, "AsyncSession": AsyncSession, "Any": Any}
    exec(compile(ast.fix_missing_locations(module), "logic_fn", "exec"), namespace)  # noqa: S102
    return namespace[name]


async def main() -> None:
    server = pgserver.get_server(tempfile.mkdtemp())
    engine = create_async_engine(server.get_uri().replace("postgresql://", "postgresql+asyncpg://"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        for stmt in DDL:
            await conn.exec_driver_sql(stmt)

    async def rows(**kw):
        async with factory() as s:
            return await diag.fetch(s, **kw)

    print("writing")
    await diag.record(factory, "info", "spin", user_id=11, path="POST /spin", status=200, detail={"prize_id": 3})
    got = await rows()
    ok(len(got) == 1 and got[0]["event"] == "spin" and got[0]["user_id"] == 11, "an entry is stored with its user and path")
    ok(got[0]["detail"] == {"prize_id": 3} and got[0]["status"] == 200, "detail and status round-trip")

    diag.set_debug(False)
    await diag.record(factory, "debug", "state", user_id=11)
    ok(len(await rows()) == 1, "debug entries are dropped while the detailed journal is off")
    diag.set_debug(True)
    await diag.record(factory, "debug", "state", user_id=11)
    ok(len(await rows()) == 2, "debug entries are kept while the detailed journal is on")
    diag.set_debug(False)

    await diag.record(factory, "weird", "x")
    ok((await rows())[0]["level"] == "info", "an unknown level is stored as info")

    big = {"trace": "x" * 5000, "nested": {"a": list(range(100))}}
    await diag.record(factory, "error", "internal_error", detail=big)
    last = (await rows())[0]
    ok(last["event"] == "internal_error" and len(json.dumps(last["detail"])) <= diag.MAX_DETAIL_CHARS + 100, "oversized detail is clipped")

    class Broken:
        def __call__(self):
            raise RuntimeError("db down")

    await diag.record(Broken(), "error", "never_raises")
    ok(True, "a failing database never raises out of the journal")

    print("reading")
    await diag.record(factory, "warn", "request_failed", user_id=12, status=403, detail={"code": "account_banned"})
    only_warn = await rows(min_level="warn")
    ok({r["level"] for r in only_warn} <= {"warn", "error"} and len(only_warn) == 2, "level filter returns warnings and errors only")
    ok([r["id"] for r in await rows()] == sorted([r["id"] for r in await rows()], reverse=True), "newest entry comes first")
    ok(len(await rows(limit=2)) == 2, "limit is honoured")
    async with factory() as s:
        c = await diag.counts(s)
    ok(c.get("warn") == 1 and c.get("error") == 1 and c.get("info") == 2 and c.get("debug") == 1, "counts are grouped by level")

    print("retention")
    diag.MAX_ROWS = 10
    for i in range(30):
        await diag.record(factory, "info", "bulk", user_id=i)
    async with factory() as s:
        await diag.trim(s)
        await s.commit()
    ok(len(await rows(limit=100)) <= 11, "trim keeps only the newest MAX_ROWS entries")
    ok((await rows())[0]["user_id"] == 29, "the newest entries survive the trim")
    async with factory() as s:
        await s.execute(text("update ext_kiro_wheel_log set created_at = now() - interval '40 days' where id = (select min(id) from ext_kiro_wheel_log)"))
        await s.commit()
        before = len(await diag.fetch(s, limit=100))
        await diag.trim(s)
        await s.commit()
        after = len(await diag.fetch(s, limit=100))
    ok(after == before - 1, "entries older than KEEP_DAYS are removed")
    diag.MAX_ROWS = 5000

    print("report")
    sample = [
        {"id": 3, "created_at": datetime(2026, 10, 6, 12, 0, 5, tzinfo=UTC), "level": "warn", "event": "request_failed",
         "user_id": 777, "path": "POST /spin", "status": 403, "detail": {"error_code": "account_banned", "user_id": 777}},
        {"id": 2, "created_at": datetime(2026, 10, 6, 11, 59, 0, tzinfo=UTC), "level": "info", "event": "gift_redeem",
         "user_id": 888, "path": "POST /gift/redeem", "status": 200,
         "detail": {"code": "ABCD1234", "from_user": 777, "nested": {"telegram_id": 555}}},
        {"id": 1, "created_at": datetime(2026, 10, 6, 11, 58, 0, tzinfo=UTC), "level": "info", "event": "spin",
         "user_id": 777, "path": "POST /spin", "status": 200, "detail": {}},
    ]
    header = {"plugin": "kiro-wheel 1.7.1", "config": {"enabled": True}}
    masked = diag.format_report(header, sample, mask=True)
    ok("777" not in masked and "888" not in masked and "555" not in masked, "masked report contains no user ids")
    ok("ABCD1234" not in masked and "ABC…" in masked, "masked report hides gift codes")
    lines = [ln for ln in masked.splitlines() if " user=u" in ln]
    ok(len(lines) == 3 and lines[0].split("user=")[1].split()[0] == lines[2].split("user=")[1].split()[0], "one person keeps one label inside a report")
    ok(lines[0].startswith("2026-10-06 11:58:00Z INFO") and lines[-1].startswith("2026-10-06 12:00:05Z WARN"), "entries are listed oldest first with UTC time")
    ok("account_banned" in masked, "error codes stay readable in a masked report")
    ok("kiro-wheel 1.7.1" in masked and "privacy: user ids and gift codes are masked" in masked, "header and privacy note are present")
    again = diag.format_report(header, sample, mask=True)
    ok(masked.split("user=")[1].split()[0] != again.split("user=")[1].split()[0], "labels differ between two reports")
    raw = diag.format_report(header, sample, mask=False)
    ok("user=777" in raw and "ABCD1234" in raw and "RAW user ids" in raw, "unmasked report keeps the raw values and says so")

    print("header")
    async with factory() as s:
        await s.execute(text("insert into ext_kiro_wheel_spins (id, user_id) values ('a', 1), ('b', 2), ('c', 1)"))
        await s.execute(text("insert into ext_kiro_wheel_spins (id, user_id, status) values ('d', 3, 'pending')"))
        await s.execute(text("insert into ext_kiro_wheel_prizes (kind, enabled) values ('days', true), ('days', false), ('traffic', true)"))
        await s.execute(text("insert into ext_kiro_wheel_gifts (code) values ('G1')"))
        await s.execute(text("insert into ext_kiro_wheel_daily (user_id) values (1), (2)"))
        await s.commit()
        h = await diag.collect_header(s, "1.7.1", {"enabled": True, "banner_image_id": "abc", "secret_like": "x", "daily_rewards": [1, 2]})
    ok(h["spins_total"] == 4 and h["players"] == 3 and h["pending_prizes"] == 1, "header counts spins, players and pending prizes")
    ok(h["open_gifts"] == 1 and h["daily_players"] == 2, "header counts open gifts and calendar players")
    ok(h["prizes"] == {"days": "1/2 active", "traffic": "1/1 active"}, "header summarises prizes by kind")
    ok("secret_like" not in h["config"] and h["banner_set"] is True and h["config"]["daily_rewards"] == [1, 2], "only safe settings reach the report")

    print("clear")
    async with factory() as s:
        removed = await diag.clear(s)
        await s.commit()
    ok(removed > 0 and not await rows(), "clearing empties the journal")

    print("account status")
    account_status = load_function("account_status")
    async with factory() as s:
        await s.execute(text("insert into users (user_id, is_banned) values (1, false), (2, true), (4, NULL)"))
        await s.commit()
        ok(await account_status(s, 1) is None, "a normal account may play")
        ok(await account_status(s, 2) == "account_banned", "a banned account gets its own reason")
        ok(await account_status(s, 3) == "profile_not_found", "a missing profile gets its own reason")
        ok(await account_status(s, 4) is None, "a NULL is_banned means not banned, not a missing profile")

    await engine.dispose()
    server.cleanup()
    print(f"\n{PASSED} checks passed")


asyncio.run(main())
