"""Daily rewards calendar tests on a throwaway PostgreSQL (pgserver).

Run:  python tests/run_daily_tests.py     (needs: pgserver sqlalchemy[asyncio] asyncpg)
"""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pgserver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

spec = importlib.util.spec_from_file_location("daily", Path(__file__).resolve().parents[1] / "backend" / "kiro_wheel" / "daily.py")
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)

DDL = """
CREATE TABLE subscriptions (subscription_id SERIAL PRIMARY KEY, user_id BIGINT, end_date TIMESTAMPTZ NOT NULL, is_active BOOLEAN DEFAULT TRUE);
CREATE TABLE ext_kiro_wheel_daily (user_id BIGINT PRIMARY KEY, streak SMALLINT NOT NULL DEFAULT 0, last_day DATE,
  total_tickets INTEGER NOT NULL DEFAULT 0, claims INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE credits (user_id BIGINT PRIMARY KEY, spins INTEGER NOT NULL DEFAULT 0);
CREATE TABLE grants (key TEXT PRIMARY KEY, user_id BIGINT, spins INT);
"""
PASSED = 0


def ok(cond: bool, label: str) -> None:
    global PASSED
    if not cond:
        raise AssertionError(label)
    PASSED += 1
    print(f"  ok  {label}")


async def fake_grant(session, user_id, spins, *, key, reason, cap):
    """Same contract as add_bonus_spins: idempotent by key, capped."""
    done = await session.execute(text("insert into grants (key, user_id, spins) values (:k, :u, :s) on conflict (key) do nothing"), {"k": key, "u": user_id, "s": spins})
    if done.rowcount != 1:
        return False
    await session.execute(
        text("insert into credits (user_id, spins) values (:u, least(cast(:s as integer), cast(:c as integer))) "
             "on conflict (user_id) do update set spins = least(credits.spins + cast(:s as integer), cast(:c as integer))"),
        {"u": user_id, "s": spins, "c": cap},
    )
    return True


async def main() -> None:
    server = pgserver.get_server(tempfile.mkdtemp())
    engine = create_async_engine(server.get_uri().replace("postgresql://", "postgresql+asyncpg://"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                await conn.exec_driver_sql(stmt)

    cfg = {"daily_enabled": True, "daily_rewards": [1, 1, 2, 3, 4, 5, 10], "day_offset_hours": 3, "max_bonus_spins": 100, "require_active_subscription": False}
    base = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)  # 12:00 MSK

    async def balance(uid):
        async with factory() as s:
            return int(await s.scalar(text("select coalesce((select spins from credits where user_id = :u), 0)"), {"u": uid}))

    async def do_claim(uid, now, config=cfg):
        async with factory() as s:
            res = await daily.claim(s, uid, config, fake_grant, now=now)
            await s.commit()
            return res

    async def state(uid, now, config=cfg):
        async with factory() as s:
            return await daily.get_state(s, uid, config, now=now)

    print("rules")
    ok(daily.clean_rewards([1, 2, 3, 4, 5, 6, 7]) == [1, 2, 3, 4, 5, 6, 7], "seven rewards are accepted")
    for bad in ([1, 2], [1] * 8, [-1] + [1] * 6, [101] + [1] * 6, ["x"] + [1] * 6, None):
        try:
            daily.clean_rewards(bad)
            ok(False, f"rejected {bad!r}")
        except daily.DailyError:
            pass
    ok(True, "malformed reward lists are rejected")

    print("calendar")
    s0 = await state(1, base)
    ok(s0["can_claim"] and s0["next_day"] == 1 and s0["today_tickets"] == 1, "new player starts on day 1")
    ok([d["status"] for d in s0["days"]] == ["current"] + ["locked"] * 6, "day 1 is current, the rest are locked")
    r = await do_claim(1, base)
    ok(r["granted"] == 1 and r["day"] == 1 and await balance(1) == 1, "day 1 pays 1 ticket")
    s1 = await state(1, base)
    ok(not s1["can_claim"] and s1["reason"] == "claimed_today" and s1["days"][0]["status"] == "claimed", "claimed today: cannot claim again")
    ok(s1["next_at"] == "2026-10-01T21:00:00+00:00", "next reward at the next local midnight (00:00 MSK)")
    try:
        await do_claim(1, base + timedelta(hours=2))
        ok(False, "second claim refused")
    except daily.DailyError as exc:
        ok(exc.code == "claimed_today" and await balance(1) == 1, "a second claim on the same day is refused and pays nothing")
    nxt = base + timedelta(days=1)
    s2 = await state(1, nxt)
    ok(s2["can_claim"] and s2["next_day"] == 2 and s2["streak"] == 1 and s2["days"][0]["status"] == "claimed", "the next day continues the streak")
    for day in range(2, 8):
        r = await do_claim(1, base + timedelta(days=day - 1))
        ok(r["day"] == day and r["granted"] == cfg["daily_rewards"][day - 1], f"day {day} pays {cfg['daily_rewards'][day - 1]}")
    ok(await balance(1) == sum(cfg["daily_rewards"]), "a full week pays the sum of all rewards")
    s7 = await state(1, base + timedelta(days=6))
    ok(all(d["status"] == "claimed" for d in s7["days"]), "after day 7 all days are marked claimed")
    s8 = await state(1, base + timedelta(days=7))
    ok(s8["next_day"] == 1 and s8["streak"] == 0 and all(d["status"] != "claimed" for d in s8["days"]), "the cycle restarts after day 7")
    r = await do_claim(1, base + timedelta(days=7))
    ok(r["day"] == 1, "the new cycle begins at day 1")

    print("missed days")
    await do_claim(2, base)
    await do_claim(2, base + timedelta(days=1))
    s = await state(2, base + timedelta(days=3))  # skipped one day
    ok(s["next_day"] == 1 and s["streak"] == 0 and s["broken"], "skipping a day resets the streak to day 1")
    r = await do_claim(2, base + timedelta(days=3))
    ok(r["day"] == 1 and r["granted"] == 1, "reward after a break is day 1 again")

    print("day boundary")
    late = datetime(2026, 10, 1, 20, 59, tzinfo=UTC)   # 23:59 MSK
    early = datetime(2026, 10, 1, 21, 1, tzinfo=UTC)   # 00:01 MSK next day
    await do_claim(3, late)
    s = await state(3, early)
    ok(s["can_claim"] and s["next_day"] == 2, "two minutes later, across local midnight, the next day is available")

    print("access")
    off = {**cfg, "daily_enabled": False}
    s = await state(4, base, off)
    ok(not s["can_claim"] and s["reason"] == "disabled" and not s["enabled"], "disabled calendar offers nothing")
    try:
        await do_claim(4, base, off)
        ok(False, "claim refused when disabled")
    except daily.DailyError as exc:
        ok(exc.code == "disabled", "claim is refused when the calendar is off")
    need_sub = {**cfg, "require_active_subscription": True}
    s = await state(5, base, need_sub)
    ok(not s["can_claim"] and s["reason"] == "subscription_required", "subscription is required when the wheel demands it")
    async with factory() as sess:
        await sess.execute(text("insert into subscriptions (user_id, end_date) values (5, now() + interval '5 days')"))
        await sess.commit()
    ok((await state(5, base, need_sub))["can_claim"], "an active subscription unlocks the reward")
    await do_claim(5, base, need_sub)
    ok(await balance(5) == 1, "paid out for a subscriber")

    print("concurrency and cap")
    results = await asyncio.gather(*(do_claim(6, base) for _ in range(5)), return_exceptions=True)
    wins = [r for r in results if isinstance(r, dict)]
    ok(len(wins) == 1 and await balance(6) == 1, "five simultaneous taps pay exactly once")
    capped = {**cfg, "max_bonus_spins": 3, "daily_rewards": [50] * 7}
    await do_claim(7, base, capped)
    ok(await balance(7) == 3, "the bonus-spin cap is respected")
    zero = {**cfg, "daily_rewards": [0, 1, 1, 1, 1, 1, 1]}
    r = await do_claim(8, base, zero)
    ok(r["granted"] == 0 and (await state(8, base, zero))["days"][0]["status"] == "claimed", "a zero day still counts for the streak")

    await engine.dispose()
    server.cleanup()
    print(f"\n{PASSED} checks passed")


asyncio.run(main())
