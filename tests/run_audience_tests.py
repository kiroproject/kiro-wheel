"""Prize audiences, trial conversion and trial-safe prizes on a throwaway PostgreSQL (pgserver).

Core (bot.*, db.*) is replaced by small fakes, the plugin's own logic.py / storage.py run unchanged and the
real migration SQL (0001, 0002, 0005) creates the plugin tables. Nothing here touches a live database.

Run:  python tests/run_audience_tests.py     (needs: pgserver sqlalchemy[asyncio] asyncpg)
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pgserver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND = Path(__file__).resolve().parents[1] / "backend"
GIB = 1024**3
PASSED = 0
COUNTER = [0]

# ------------------------------------------------------------------ fakes of Core


class ExtensionError(ValueError):
    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class Reward:
    def __init__(self, kind: str, amount: int, currency: str = "RUB") -> None:
        self.kind, self.amount, self.currency = kind, amount, currency


STATE: dict[str, Any] = {}


def reset_state() -> None:
    STATE.clear()
    STATE.update(
        grants=[], grant_keys=set(), switches=[], panel=[], premium=[], resets=[],
        fail_switch=False, fail_panel=0, fail_premium=False, fail_reset=False,
    )


async def fake_grant(session, *, owner, user_id, idempotency_key, reward):
    if idempotency_key in STATE["grant_keys"]:
        return None
    sub = (
        await session.execute(
            text("select subscription_id from subscriptions where user_id=:u and is_active and end_date > now()"),
            {"u": user_id},
        )
    ).first()
    if sub is None:
        raise ExtensionError("extension_reward_requires_subscription")
    STATE["grant_keys"].add(idempotency_key)
    STATE["grants"].append((user_id, reward.kind, reward.amount))
    if reward.kind == "days":
        await session.execute(
            text("update subscriptions set end_date = end_date + make_interval(days => :d) where subscription_id=:i"),
            {"d": reward.amount, "i": sub[0]},
        )
    elif reward.kind == "traffic":
        await session.execute(
            text("update subscriptions set regular_bonus_bytes = regular_bonus_bytes + :b where subscription_id=:i"),
            {"b": reward.amount, "i": sub[0]},
        )
    return SimpleNamespace()


class FakeUserDal:
    @staticmethod
    async def lock_user_by_id(session, user_id):
        await session.execute(text("select 1 from users where user_id=:u for update"), {"u": user_id})


class FakeSubscriptionDal:
    COLUMNS = {"end_date", "tariff_binding_note"}

    @staticmethod
    async def update_subscription(session, subscription_id, data, **_):
        assert set(data) <= FakeSubscriptionDal.COLUMNS, data
        sets = ", ".join(f"{k} = :{k}" for k in data)
        await session.execute(text(f"update subscriptions set {sets} where subscription_id = :id"), {**data, "id": subscription_id})


async def fake_reset(service, panel_user_uuid):
    STATE["resets"].append(panel_user_uuid)
    if STATE["fail_reset"]:
        raise RuntimeError("panel reset down")
    return {
        "traffic_used_bytes": 0,
        "traffic_period_lifetime_start_bytes": 777,
        "traffic_topup_accounting_state": None,
        "period_start_at": datetime.now(UTC),
    }


class Migration:
    def __init__(self, id, description, upgrade):
        self.id, self.description, self.upgrade = id, description, upgrade


class PromoCodeService:
    @staticmethod
    async def issue_code(*_, **__):
        return SimpleNamespace(code="PROMO123")


class PromoEffects:
    def __init__(self, **kw):
        self.kw = kw


def install_stubs() -> None:
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    for name in ("bot", "bot.plugins", "bot.plugins.extensions", "bot.services", "bot.services.subscription_service_impl", "db", "db.migrator"):
        mod(name)
    mod("bot.plugins.extensions.rewards", Reward=Reward, grant=fake_grant)
    mod("bot.plugins.extensions.contracts", ExtensionError=ExtensionError)
    mod("bot.services.promo_code_service", PromoCodeService=PromoCodeService)
    mod("bot.services.promo_effects", PromoEffects=PromoEffects)
    mod("bot.services.subscription_service_impl.trial_traffic_reset", reset_trial_traffic_for_paid_activation=fake_reset)
    dal = mod("db.dal", user_dal=FakeUserDal, subscription_dal=FakeSubscriptionDal)
    sys.modules["db"].dal = dal
    mod("db.migrator.engine", Migration=Migration)
    package = types.ModuleType("kiro_wheel")  # the real __init__ needs aiohttp and Core's web layer
    package.__path__ = [str(BACKEND / "kiro_wheel")]
    sys.modules["kiro_wheel"] = package


install_stubs()
logic = importlib.import_module("kiro_wheel.logic")
storage = importlib.import_module("kiro_wheel.storage")
WheelError = logic.WheelError


def T(key, name, *, premium=False, billing="period", gb=500, enabled=True, legacy=()):
    return SimpleNamespace(
        key=key, names={"ru": name, "en": name}, billing_model=billing, monthly_gb=gb, enabled=enabled,
        premium_squad_uuids=["sq-prem"] if premium else [], premium_monthly_gb=50 if premium else None,
        hwid_device_limit=5, legacy_keys=list(legacy),
    )


class FakeTariffs:
    default_tariff = "start"

    def __init__(self):
        self.tariffs = [
            T("start", "Старт", legacy=("basic",)),
            T("startplus", "Старт+"),
            T("family", "Семейный"),
            T("familyplus", "Семейный+", premium=True),
            T("unlimited", "Безлимит", gb=1000, premium=True),
            T("pack", "Пакет", billing="traffic"),
        ]

    def get(self, key):
        return next((t for t in self.tariffs if t.key == key or key in t.legacy_keys), None)


class FakePanel:
    async def update_user_details_on_panel(self, uuid, payload):
        STATE["panel"].append((uuid, dict(payload)))
        if STATE["fail_panel"] > 0:
            STATE["fail_panel"] -= 1
            return None
        return {"uuid": uuid}


class FakeService:
    def __init__(self, factory):
        self.factory = factory
        self.settings = SimpleNamespace(tariffs_config=FakeTariffs(), trial_traffic_limit_bytes=3 * GIB)
        self.panel_service = FakePanel()

    async def switch_tariff_without_payment(self, session, user_id, key, mode, payment_id=None, apply_tariff_hwid_limit=False):
        STATE["switches"].append((user_id, key, mode, apply_tariff_hwid_limit))
        if STATE["fail_switch"]:
            return None
        row = (
            await session.execute(
                text("select subscription_id, panel_user_uuid, end_date from subscriptions where user_id=:u and is_active and end_date > now()"),
                {"u": user_id},
            )
        ).first()
        await session.execute(
            text("update subscriptions set tariff_key=:k, provider='admin', status_from_panel='ACTIVE', "
                 "traffic_limit_bytes=536870912000, hwid_device_limit=5 where subscription_id=:i"),
            {"k": key, "i": row[0]},
        )
        STATE["panel"].append((row[1], {"expireAt": row[2], "tariff": key, "squads": "T-Base"}))
        return {"subscription_id": row[0], "tariff_key": key}

    async def admin_grant_premium_topup(self, session, user_id, gb):
        STATE["premium"].append((user_id, gb))
        if STATE["fail_premium"]:
            return None
        row = (
            await session.execute(
                text("select subscription_id, tariff_key from subscriptions where user_id=:u and is_active and end_date > now()"),
                {"u": user_id},
            )
        ).first()
        tariff = self.settings.tariffs_config.get(row[1]) if row and row[1] else None
        if not tariff or not tariff.premium_squad_uuids:
            return None
        add = int(gb * GIB)
        await session.execute(
            text("update subscriptions set premium_topup_balance_bytes = premium_topup_balance_bytes + :b where subscription_id=:i"),
            {"b": add, "i": row[0]},
        )
        return {"subscription_id": row[0], "granted_bytes": add}


# ------------------------------------------------------------------ harness


def ok(cond: bool, label: str) -> None:
    global PASSED
    if not cond:
        raise AssertionError(label)
    PASSED += 1
    print(f"  ok  {label}")


async def raises(coro, code: str, label: str) -> None:
    try:
        await coro
    except WheelError as exc:
        ok(exc.code == code, f"{label} -> {code}" + ("" if exc.code == code else f" (got {exc.code})"))
        return
    raise AssertionError(f"{label}: no error, expected {code}")


def raises_sync(fn, code: str, label: str) -> None:
    try:
        fn()
    except WheelError as exc:
        ok(exc.code == code, f"{label} -> {code}" + ("" if exc.code == code else f" (got {exc.code})"))
        return
    raise AssertionError(f"{label}: no error, expected {code}")


DDL = [
    "CREATE TABLE users (user_id BIGINT PRIMARY KEY, is_banned BOOLEAN)",
    """CREATE TABLE subscriptions (
        subscription_id SERIAL PRIMARY KEY, user_id BIGINT, panel_user_uuid TEXT, provider TEXT, status_from_panel TEXT,
        tariff_key TEXT, end_date TIMESTAMPTZ NOT NULL, is_active BOOLEAN DEFAULT TRUE,
        regular_bonus_bytes BIGINT DEFAULT 0, topup_balance_bytes BIGINT DEFAULT 0, traffic_limit_bytes BIGINT,
        is_throttled BOOLEAN DEFAULT TRUE, traffic_used_bytes BIGINT DEFAULT 0, traffic_period_lifetime_start_bytes BIGINT,
        traffic_topup_accounting_state TEXT, period_start_at TIMESTAMPTZ, premium_topup_balance_bytes BIGINT DEFAULT 0,
        tariff_binding_note TEXT, hwid_device_limit INT)""",
]


async def main() -> None:
    reset_state()
    server = pgserver.get_server(tempfile.mkdtemp())
    engine = create_async_engine(server.get_uri().replace("postgresql://", "postgresql+asyncpg://"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        for stmt in DDL:
            await conn.exec_driver_sql(stmt)
        await conn.run_sync(storage._upgrade_0001)
        await conn.run_sync(storage._upgrade_0002)

    service = FakeService(factory)
    tariffs = service.settings.tariffs_config

    async def q(sql, **params):
        async with factory() as s:
            return (await s.execute(text(sql), params)).mappings().all()

    async def exec_(sql, **params):
        async with factory() as s:
            await s.execute(text(sql), params)
            await s.commit()

    async def add_user(uid):
        await exec_("insert into users (user_id, is_banned) values (:u, null)", u=uid)

    async def add_sub(uid, *, trial=False, tariff=None, days=20, active=True, used=5 * GIB, limit=3 * GIB):
        await add_user(uid) if not await q("select 1 from users where user_id=:u", u=uid) else None
        await exec_(
            "insert into subscriptions (user_id, panel_user_uuid, provider, status_from_panel, tariff_key, end_date, is_active, "
            "traffic_used_bytes, traffic_limit_bytes) values (:u, :pu, :prov, :st, :tk, now() + make_interval(days => :d), :a, :used, :lim)",
            u=uid, pu=f"panel-{uid}", prov="trial" if trial else "yookassa", st="TRIAL" if trial else "ACTIVE",
            tk=tariff, d=days, a=active, used=used, lim=limit,
        )

    async def sub(uid):
        return dict((await q("select * from subscriptions where user_id=:u order by subscription_id desc limit 1", u=uid))[0])

    async def add_prize(**body):
        prize = logic.clean_prize({"title": body.pop("title", "p"), "weight": 1, **body}, tariffs)
        async with factory() as s:
            pid = await s.scalar(
                text("insert into ext_kiro_wheel_prizes (title, description, kind, params, weight, stock, color, image_id, enabled, position) "
                     "values (:title, :description, :kind, cast(:params as jsonb), :weight, :stock, :color, :image_id, :enabled, :position) returning id"),
                {**prize, "params": json.dumps(prize["params"])},
            )
            await s.commit()
        return next(p for p in await prizes_all() if p["id"] == pid)

    async def prizes_all():
        async with factory() as s:
            return await storage.list_prizes(s, include_disabled=True)

    async def pending_for(uid, prize):
        COUNTER[0] += 1
        spin_id = f"{uid:08d}{COUNTER[0]:024d}"
        snap = {**logic.public_prize(prize), "params": prize["params"]}
        await exec_(
            "insert into ext_kiro_wheel_spins (id, user_id, request_id, source, prize_id, prize, status) "
            "values (:i, :u, :r, 'bonus', :p, cast(:prize as jsonb), 'pending')",
            i=spin_id, u=uid, r=f"req-{spin_id}", p=prize["id"], prize=json.dumps(snap),
        )
        return spin_id

    async def keep(uid, spin_id, commit=True):
        async with factory() as s:
            out = await logic.keep(s, uid, spin_id, service)
            if commit:
                await s.commit()
            return out

    async def status_of(spin_id):
        return (await q("select status, result from ext_kiro_wheel_spins where id=:i", i=spin_id))[0]

    # ------------------------------------------------------------- migration
    print("migration 0005")
    legacy_kinds = ["nothing", "days", "traffic", "premium", "balance", "discount", "gift_code", "manual"]
    async with factory() as s:
        for kind in legacy_kinds:
            await s.execute(
                text("insert into ext_kiro_wheel_prizes (title, kind, params) values (:t, :k, cast(:p as jsonb))"),
                {"t": f"legacy-{kind}", "k": kind, "p": json.dumps({"days": 3, "badge": "x"})},
            )
        await s.execute(
            text("insert into ext_kiro_wheel_prizes (title, kind, params) values ('custom', 'days', cast(:p as jsonb))"),
            {"p": json.dumps({"days": 1, "audience": {"segments": ["paid"], "tariff_keys": ["family"]}})},
        )
        await s.commit()
    async with engine.begin() as conn:
        await conn.run_sync(storage._upgrade_0005)
    rows = {r["title"]: r for r in await prizes_all()}
    seg = lambda k: rows[f"legacy-{k}"]["params"]["audience"]["segments"]
    ok(seg("days") == ["trial", "paid"] and seg("traffic") == ["trial", "paid"], "days/traffic keep trial+paid availability")
    ok(seg("premium") == ["paid"], "premium keeps paid only")
    ok(all(seg(k) == ["none", "trial", "paid"] for k in ("nothing", "balance", "discount", "gift_code", "manual")), "nothing/balance/discount/gift_code/manual stay for every segment")
    ok(rows["legacy-days"]["params"]["badge"] == "x" and rows["legacy-days"]["params"]["days"] == 3, "other params survive the migration")
    ok(rows["custom"]["params"]["audience"] == {"segments": ["paid"], "tariff_keys": ["family"]}, "an audience that is already set is not overwritten")
    async with engine.begin() as conn:
        await conn.run_sync(storage._upgrade_0005)
    ok({r["title"]: r["params"] for r in await prizes_all()} == {r["title"]: r["params"] for r in rows.values()}, "migration is repeatable (no change on the second run)")
    ok(storage.MIGRATIONS[-1].id == "kiro-wheel.0005_prize_audience" and len({m.id for m in storage.MIGRATIONS}) == len(storage.MIGRATIONS), "migration is registered after 0004 with a unique id")
    ok(logic.prize_audience({"kind": "premium", "params": {}}) == {"segments": ["paid"], "tariff_keys": []}, "a prize without a stored audience falls back to the legacy default")
    ok(storage.DEFAULT_CONFIG["trial_convert_tariff"] == "start", "config default trial_convert_tariff = start")
    await exec_("delete from ext_kiro_wheel_prizes")

    # ------------------------------------------------------------- validation
    print("validation")
    a = logic.clean_audience({"segments": ["paid", "trial"], "tariff_keys": ["basic", "family", "family"]}, "traffic", tariffs)
    ok(a == {"segments": ["trial", "paid"], "tariff_keys": ["start", "family"]}, "segments are ordered, tariff keys canonicalised (legacy alias) and de-duplicated")
    ok(logic.clean_audience({"segments": ["trial"], "tariff_keys": ["family"]}, "days", tariffs)["tariff_keys"] == [], "tariff list is dropped when paid is not selected")
    raises_sync(lambda: logic.clean_audience({"segments": ["none", "paid"]}, "days", tariffs), "audience_segment_unsupported", "days for 'no subscription'")
    raises_sync(lambda: logic.clean_audience({"segments": ["trial"]}, "premium", tariffs), "audience_segment_unsupported", "premium for trial")
    raises_sync(lambda: logic.clean_audience({"segments": []}, "nothing", tariffs), "audience_empty", "empty audience")
    raises_sync(lambda: logic.clean_audience({"segments": ["vip"]}, "nothing", tariffs), "invalid_audience", "unknown segment")
    raises_sync(lambda: logic.clean_audience({"segments": ["paid"], "tariff_keys": ["nope"]}, "traffic", tariffs), "invalid_audience_tariff", "unknown tariff in the audience")
    ok(logic.clean_prize({"kind": "discount", "title": "d", "params": {"percent": 10}}, tariffs)["params"]["audience"]["segments"] == ["none", "trial", "paid"], "a new prize without an audience gets the legacy default")
    ok(logic.clean_prize({"kind": "days", "title": "d", "params": {"days": 2, "convert_tariff": "basic"}}, tariffs)["params"]["convert_tariff"] == "start", "convert tariff is canonicalised")
    raises_sync(lambda: logic.clean_prize({"kind": "days", "title": "d", "params": {"days": 2, "convert_tariff": "pack"}}, tariffs), "invalid_convert_tariff", "traffic-billed tariff as conversion target")
    raises_sync(lambda: logic.clean_prize({"kind": "days", "title": "d", "params": {"days": 2, "convert_tariff": "zzz"}}, tariffs), "invalid_convert_tariff", "unknown conversion tariff")
    ok("convert_tariff" not in logic.clean_prize({"kind": "traffic", "title": "d", "params": {"gb": 1, "convert_tariff": "family"}}, tariffs)["params"], "convert tariff is kept only for days prizes")
    ok("valid_days" not in logic.clean_prize({"kind": "premium", "title": "d", "params": {"gb": 1}}, tariffs)["params"], "premium no longer carries a code validity")
    cfg = logic.clean_config({"trial_convert_tariff": "family"}, dict(storage.DEFAULT_CONFIG), tariffs)
    ok(cfg["trial_convert_tariff"] == "family", "config default conversion tariff is saved")
    raises_sync(lambda: logic.clean_config({"trial_convert_tariff": "zzz"}, dict(storage.DEFAULT_CONFIG), tariffs), "invalid_convert_tariff", "unknown default conversion tariff")
    ok(logic.clean_config({"trial_convert_tariff": ""}, dict(storage.DEFAULT_CONFIG), tariffs)["trial_convert_tariff"] == "", "empty default = Core's default tariff")
    cat = logic.tariff_catalog(tariffs)
    ok([t["key"] for t in cat] == ["start", "startplus", "family", "familyplus", "unlimited", "pack"] and cat[0]["title"] == "Старт" and cat[3]["premium"] and not cat[0]["premium"] and cat[0]["monthly_gb"] == 500, "tariff catalog: key, title, monthly_gb, premium flag")

    # ------------------------------------------------------------- segments
    print("segments")
    await add_user(1)  # no subscription
    await add_sub(2, trial=True)
    await add_sub(3, tariff="start")
    await add_sub(4, tariff="familyplus")
    await add_sub(5, tariff="start", days=-1)  # expired
    await add_sub(6, tariff="basic")  # legacy alias
    await add_sub(7, trial=True)
    await add_sub(8, tariff="start")
    await add_sub(9, trial=True)
    await add_sub(10, tariff="familyplus")
    await add_sub(11, trial=True)
    await add_user(12)
    await add_sub(30, tariff="unlimited", limit=0)  # Core keeps 0 = no traffic limit
    async with factory() as s:
        seg_of = lambda uid: logic.user_segment(s, uid, tariffs)
        ok(logic.has_unlimited_traffic((await seg_of(30))["sub"]) and not logic.has_unlimited_traffic((await seg_of(3))["sub"]), "unlimited plan (limit 0) is detected; a capped plan is not")
        ok((await seg_of(1))["segment"] == "none", "no subscription -> none")
        ok((await seg_of(2))["segment"] == "trial" and (await seg_of(2))["tariff_key"] is None, "trial without tariff -> trial")
        ok(((await seg_of(3))["segment"], (await seg_of(3))["tariff_key"]) == ("paid", "start"), "tariff subscription -> paid + key")
        ok((await seg_of(5))["segment"] == "none", "expired subscription -> none")
        ok((await seg_of(6))["tariff_key"] == "start", "legacy tariff key is canonicalised")
    await exec_("insert into subscriptions (user_id, panel_user_uuid, provider, status_from_panel, tariff_key, end_date) values (3, 'x', 'trial', 'TRIAL', 'start', now() + interval '1 day')")
    await exec_("delete from subscriptions where user_id=3 and provider='trial'")

    # ------------------------------------------------------------- eligibility matrix
    print("eligibility")
    p_days = await add_prize(kind="days", params={"days": 3})
    p_days_paid = await add_prize(kind="days", params={"days": 30}, audience={"segments": ["paid"]})
    p_prem = await add_prize(kind="premium", params={"gb": 5})
    p_trf = await add_prize(kind="traffic", params={"gb": 2})
    p_disc = await add_prize(kind="discount", params={"percent": 10}, audience={"segments": ["trial", "paid"]})
    p_none = await add_prize(kind="nothing", params={})
    p_fam = await add_prize(kind="traffic", params={"gb": 5}, audience={"segments": ["paid"], "tariff_keys": ["family", "familyplus"]})
    cfgd = dict(storage.DEFAULT_CONFIG)
    seg_none = {"segment": "none", "tariff_key": None, "sub": None}
    seg_trial = {"segment": "trial", "tariff_key": None, "sub": {}}
    seg_start = {"segment": "paid", "tariff_key": "start", "sub": {}}
    seg_fplus = {"segment": "paid", "tariff_key": "familyplus", "sub": {}}
    why = lambda p, sg: logic.ineligible_reason(p, sg, cfgd, service)
    ok(why(p_days, seg_none) == "subscription_required", "days: no subscription is refused")
    ok(why(p_days, seg_trial) is None and why(p_days, seg_start) is None, "days (trial+paid): both are allowed")
    ok(why(p_days_paid, seg_trial) == "prize_not_eligible" and why(p_days_paid, seg_start) is None, "days (paid only): trial is refused")
    ok(why(p_prem, seg_trial) == "premium_unavailable" and why(p_prem, seg_none) == "subscription_required", "premium: trial and none are refused")
    ok(why(p_prem, seg_start) == "premium_unavailable" and why(p_prem, seg_fplus) is None, "premium: needs a tariff with a premium squad")
    ok(why(p_disc, seg_none) == "prize_not_eligible" and why(p_disc, seg_trial) is None, "discount configured for trial+paid: none is refused")
    ok(why(p_none, seg_none) is None, "nothing: everybody")
    ok(why(p_fam, seg_start) == "prize_not_eligible" and why(p_fam, seg_fplus) is None, "tariff list: only the listed tariffs")
    ok(why(p_trf, seg_trial) is None, "traffic on trial is allowed when the trial has a limit")
    service.settings.trial_traffic_limit_bytes = 0
    ok(why(p_trf, seg_trial) == "prize_not_eligible", "traffic on an unlimited trial is not offered")
    service.settings.trial_traffic_limit_bytes = 3 * GIB
    seg_unlim = {"segment": "paid", "tariff_key": "unlimited", "sub": {"traffic_limit_bytes": 0}}
    seg_capped = {"segment": "paid", "tariff_key": "start", "sub": {"traffic_limit_bytes": 3 * GIB}}
    ok(why(p_trf, seg_unlim) == "prize_not_eligible" and why(p_trf, seg_capped) is None, "traffic: an unlimited plan is never topped up, a capped plan is")
    ok(why(p_days, seg_unlim) is None and why(p_none, seg_unlim) is None, "days and other prizes stay available on an unlimited plan")
    ok(logic._eligible([p_trf], seg_unlim, cfgd, service) == [] and logic._eligible([p_trf], seg_capped, cfgd, service) == [p_trf], "the wheel does not roll traffic for an unlimited plan")
    ok(logic.ineligible_reason(p_prem, seg_fplus, cfgd, None) == "premium_unavailable", "without Core service premium cannot be verified, so it is not offered")
    legacy_snapshot = {"kind": "premium", "params": {"gb": 1}}
    ok(why(legacy_snapshot, seg_trial) == "premium_unavailable", "an old pending snapshot (no audience) follows the legacy rule")
    names = lambda sg: sorted(p["id"] for p in logic._eligible([p_days, p_days_paid, p_prem, p_trf, p_disc, p_none, p_fam], sg, cfgd, service))
    ok(names(seg_trial) == sorted([p_days["id"], p_trf["id"], p_disc["id"], p_none["id"]]), "wheel for a trial player: only trial-available prizes")
    ok(names(seg_none) == [p_none["id"]], "wheel for a player without subscription: only 'nothing'")
    ok(names(seg_fplus) == sorted(p["id"] for p in (p_days, p_days_paid, p_prem, p_trf, p_disc, p_none, p_fam)), "wheel for a premium-tariff player: everything")
    ok(p_prem["id"] not in names(seg_start) and p_fam["id"] not in names(seg_start), "wheel for 'start': no premium, no family-only prize")
    cfgd2 = {**cfgd, "trial_convert_tariff": "zzz"}
    ok(logic.ineligible_reason(p_days, seg_trial, cfgd2, service) == "prize_not_eligible", "days for trial is hidden while no valid conversion tariff exists")
    hint = None
    async with factory() as s:
        hint = await logic.decision_hint(s, 2, {"kind": "days", "params": {"days": 3}}, cfgd, service)
        ok(hint and "Старт" in hint and "3" in hint, "player on trial sees that the trial will be replaced by the tariff")
        ok(await logic.decision_hint(s, 3, {"kind": "days", "params": {"days": 3}}, cfgd, service) is None, "no hint for a paid player")
    await exec_("delete from ext_kiro_wheel_prizes")

    # ------------------------------------------------------------- spin() uses the audience
    print("spin")
    cfg_row = {**storage.DEFAULT_CONFIG, "enabled": True, "require_active_subscription": False, "daily_free_spins": 5}
    async with factory() as s:
        await storage.save_config(s, cfg_row)
        await s.commit()
    await add_prize(kind="premium", params={"gb": 5})
    await add_prize(kind="days", params={"days": 30}, audience={"segments": ["paid"]})
    async with factory() as s:
        await raises(logic.spin(s, 2, "req-trial-0001", service), "no_prizes", "trial player, only paid-only prizes exist")
    only = await add_prize(kind="days", title="trial days", params={"days": 2}, audience={"segments": ["trial"]})
    async with factory() as s:
        out = await logic.spin(s, 2, "req-trial-0002", service)
        await s.commit()
    ok(out["prize"]["id"] == only["id"], "trial player can only draw the prize meant for trial")
    async with factory() as s:
        await raises(logic.spin(s, 1, "req-none-00001", service), "no_prizes", "player without subscription")
    await exec_("delete from ext_kiro_wheel_spins")
    await exec_("delete from ext_kiro_wheel_prizes")

    # ------------------------------------------------------------- days on trial -> paid base tariff
    print("days prize on a trial")
    before = await sub(2)
    p = await add_prize(kind="days", params={"days": 3})
    sid = await pending_for(2, p)
    out = await keep(2, sid)
    after = await sub(2)
    r = out["result"]
    ok(STATE["switches"] == [(2, "start", "admin_assign", True)], "Core switch_tariff_without_payment(start, admin_assign, apply_tariff_hwid_limit=True)")
    ok(STATE["grants"] == [], "Core grant(Reward) is NOT used for a trial")
    delta = (after["end_date"] - datetime.now(UTC)).total_seconds()
    ok(2.9 * 86400 < delta < 3.01 * 86400, f"term is exactly N days from now (end in {delta / 86400:.3f} d), the 20 trial days are not added")
    ok(before["end_date"] > after["end_date"], "remaining trial days are dropped")
    ok(abs((STATE["panel"][-1][1]["expireAt"] - after["end_date"]).total_seconds()) < 1, "the switch sent the new term to the panel")
    ok(after["tariff_key"] == "start" and after["provider"] == "admin" and after["status_from_panel"] == "ACTIVE", "subscription is now a paid base tariff, not a trial")
    ok(not logic.is_trial(after), "Core would no longer see it as a trial")
    ok(STATE["resets"] == ["panel-2"] and after["traffic_used_bytes"] == 0 and after["traffic_period_lifetime_start_bytes"] == 777, "traffic counter is reset")
    ok(after["tariff_binding_note"].startswith("kiro-wheel:trial_convert:spin:"), "subscription row is marked as given by the wheel")
    ok(r["converted_trial"] and r["tariff_key"] == "start" and r["days"] == 3 and r["source"] == "kiro-wheel", "result carries the conversion mark")
    ok("Старт" in r["text"] and "3 дн." in r["text"], f"player text names the tariff: {r['text']}")
    st = await status_of(sid)
    res = st["result"] if isinstance(st["result"], dict) else json.loads(st["result"])
    ok(st["status"] == "claimed" and res["converted_trial"] and res["subscription_id"] == after["subscription_id"], "spin row stores prize, user and conversion (audit)")
    n_switch = len(STATE["switches"])
    await raises(keep(2, sid), "already_resolved", "second keep of the same spin")
    ok(len(STATE["switches"]) == n_switch, "a repeated keep does not switch again")
    async with factory() as s:
        ok((await logic.user_segment(s, 2, tariffs))["segment"] == "paid", "after the conversion the player is in the paid segment")

    # per-prize tariff and config default
    reset_state()
    p2 = await add_prize(kind="days", params={"days": 5, "convert_tariff": "family"})
    sid = await pending_for(7, p2)
    out = await keep(7, sid)
    ok(STATE["switches"] == [(7, "family", "admin_assign", True)] and out["result"]["tariff_title"] == "Семейный", "prize override tariff wins")
    reset_state()
    async with factory() as s:
        await storage.save_config(s, {**cfg_row, "trial_convert_tariff": "startplus"})
        await s.commit()
    p3 = await add_prize(kind="days", params={"days": 4})
    sid = await pending_for(9, p3)
    await keep(9, sid)
    ok(STATE["switches"] == [(9, "startplus", "admin_assign", True)], "plugin default tariff (from config, not hard-coded) is used")
    async with factory() as s:
        await storage.save_config(s, {**cfg_row, "trial_convert_tariff": ""})
        await s.commit()
    reset_state()
    await add_sub(13, trial=True)
    sid = await pending_for(13, p3)
    await keep(13, sid)
    ok(STATE["switches"] == [(13, "start", "admin_assign", True)], "empty default falls back to Core's default tariff")
    async with factory() as s:
        await storage.save_config(s, cfg_row)
        await s.commit()

    # failures roll back
    reset_state()
    await add_sub(14, trial=True)
    sid = await pending_for(14, p3)
    before = await sub(14)
    STATE["fail_switch"] = True
    async with factory() as s:
        try:
            await logic.keep(s, 14, sid, service)
            ok(False, "failed switch is reported")
        except WheelError as exc:
            ok(exc.code == "fulfil_failed", "Core refused the switch -> fulfil_failed")
        await s.rollback()
    after = await sub(14)
    ok(after["end_date"] == before["end_date"] and after["tariff_key"] is None and after["provider"] == "trial", "failed conversion leaves the trial untouched (end_date rolled back)")
    ok((await status_of(sid))["status"] == "pending", "the prize stays pending after a failure")
    STATE["fail_switch"] = False
    await keep(14, sid)
    ok((await status_of(sid))["status"] == "claimed" and (await sub(14))["tariff_key"] == "start", "retry after the failure succeeds exactly once")
    reset_state()
    STATE["fail_reset"] = True
    await add_sub(15, trial=True)
    sid = await pending_for(15, p3)
    await keep(15, sid)
    ok((await sub(15))["tariff_key"] == "start", "a failed traffic reset does not undo the conversion (best effort, as in Core)")

    # ------------------------------------------------------------- days on paid
    print("days prize on a paid subscription")
    reset_state()
    pd = await add_prize(kind="days", params={"days": 7})
    before = await sub(3)
    sid = await pending_for(3, pd)
    out = await keep(3, sid)
    after = await sub(3)
    ok(STATE["grants"] == [(3, "days", 7)] and STATE["switches"] == [], "paid: Core grant days, no tariff switch")
    ok(abs((after["end_date"] - before["end_date"]).total_seconds() - 7 * 86400) < 2 and after["tariff_key"] == "start", "paid: term extended by N days, tariff unchanged")
    ok("converted_trial" not in out["result"], "paid: no conversion mark")
    sid = await pending_for(1, pd)
    await raises(keep(1, sid), "subscription_required", "days for a player without subscription")
    ok((await status_of(sid))["status"] == "pending" and STATE["grants"] == [(3, "days", 7)], "nothing is granted and the prize stays pending")
    sid = await pending_for(5, pd)
    await raises(keep(5, sid), "subscription_required", "days when the subscription has expired meanwhile")

    # ------------------------------------------------------------- premium
    print("premium prize")
    reset_state()
    pp = await add_prize(kind="premium", params={"gb": 5})
    sid = await pending_for(4, pp)
    out = await keep(4, sid)
    after = await sub(4)
    ok(STATE["premium"] == [(4, 5.0)] and STATE["grants"] == [], "premium on a premium tariff -> admin_grant_premium_topup")
    ok(after["premium_topup_balance_bytes"] == 5 * GIB and out["result"]["granted_premium_bytes"] == 5 * GIB and out["result"]["applied"], "premium traffic is credited at once")
    sid = await pending_for(10, pp)
    STATE["fail_premium"] = True
    async with factory() as s:
        try:
            await logic.keep(s, 10, sid, service)
            ok(False, "premium failure reported")
        except WheelError as exc:
            ok(exc.code == "fulfil_failed", "admin_grant_premium_topup returned None -> fulfil_failed")
        await s.rollback()
    ok((await status_of(sid))["status"] == "pending" and (await sub(10))["premium_topup_balance_bytes"] == 0, "premium prize stays pending and nothing is credited")
    STATE["fail_premium"] = False
    await keep(10, sid)
    ok((await status_of(sid))["status"] == "claimed" and (await sub(10))["premium_topup_balance_bytes"] == 5 * GIB, "premium retry succeeds once")
    n = len(STATE["premium"])
    sid = await pending_for(11, pp)
    await raises(keep(11, sid), "premium_unavailable", "premium for a trial player")
    sid = await pending_for(8, pp)
    await raises(keep(8, sid), "premium_unavailable", "premium on a tariff without premium squad")
    sid = await pending_for(1, pp)
    await raises(keep(1, sid), "subscription_required", "premium for a player without subscription")
    ok(len(STATE["premium"]) == n and (await sub(11))["tariff_key"] is None, "refused premium touches nothing (no code, no squad)")

    # ------------------------------------------------------------- traffic
    print("traffic prize")
    reset_state()
    await add_sub(20, trial=True, limit=3 * GIB)
    pt = await add_prize(kind="traffic", params={"gb": 2})
    sid = await pending_for(20, pt)
    out = await keep(20, sid)
    after = await sub(20)
    uuid, payload = STATE["panel"][-1]
    ok(STATE["grants"] == [] and STATE["switches"] == [], "trial traffic: no Core grant, no tariff switch")
    ok(payload == {"uuid": "panel-20", "status": "ACTIVE", "trafficLimitBytes": 5 * GIB}, "PATCH has only trafficLimitBytes (+uuid, status): no hwid, squads, tag, term")
    ok(after["regular_bonus_bytes"] == 2 * GIB and after["traffic_limit_bytes"] == 5 * GIB, "limit = trial limit + bonus, stored locally")
    ok(after["provider"] == "trial" and after["tariff_key"] is None and after["end_date"] == (await sub(20))["end_date"], "the trial stays a trial (no conversion)")
    ok(out["result"]["applied"] and "пробному" in out["result"]["text"], "trial traffic text")
    pt2 = await add_prize(kind="traffic", params={"gb": 1})
    sid2 = await pending_for(20, pt2)
    await keep(20, sid2)
    ok(STATE["panel"][-1][1]["trafficLimitBytes"] == 6 * GIB and (await sub(20))["regular_bonus_bytes"] == 3 * GIB, "the second trial prize adds on top (absolute limit 3+2+1)")
    await raises(keep(20, sid2), "already_resolved", "second keep of the same spin")
    ok(len(STATE["panel"]) == 2, "a repeated keep does not call the panel again")
    # panel failure rolls back, retry converges
    await add_sub(21, trial=True, limit=3 * GIB)
    sid = await pending_for(21, pt)
    STATE["fail_panel"] = 1
    async with factory() as s:
        try:
            await logic.keep(s, 21, sid, service)
            ok(False, "panel failure reported")
        except WheelError as exc:
            ok(exc.code == "fulfil_failed", "panel rejected the update -> fulfil_failed")
        await s.rollback()
    ok((await sub(21))["regular_bonus_bytes"] == 0 and (await status_of(sid))["status"] == "pending", "panel failure: nothing is stored locally")
    await keep(21, sid)
    ok((await sub(21))["regular_bonus_bytes"] == 2 * GIB and STATE["panel"][-1][1]["trafficLimitBytes"] == 5 * GIB, "retry after the failure gives the same absolute limit (idempotent)")
    # unlimited trial
    service.settings.trial_traffic_limit_bytes = 0
    await add_sub(22, trial=True)
    sid = await pending_for(22, pt)
    await raises(keep(22, sid), "prize_not_eligible", "traffic for an unlimited trial")
    service.settings.trial_traffic_limit_bytes = 3 * GIB
    # paid traffic still uses Core
    reset_state()
    sid = await pending_for(3, pt)
    await keep(3, sid)
    ok(STATE["grants"] == [(3, "traffic", 2 * GIB)] and STATE["panel"] == [], "paid: Core grant traffic as before")

    # ------------------------------------------------------------- audience re-check at keep
    print("re-check at keep")
    reset_state()
    await add_sub(30, tariff="start")
    pf = await add_prize(kind="traffic", params={"gb": 1}, audience={"segments": ["paid"], "tariff_keys": ["family"]})
    sid = await pending_for(30, pf)
    await raises(keep(30, sid), "prize_not_eligible", "tariff outside the audience at keep time")
    await exec_("update subscriptions set tariff_key='family' where user_id=30")
    await keep(30, sid)
    ok(STATE["grants"] == [(30, "traffic", GIB)], "after the tariff changed into the audience the same prize is granted")
    await add_sub(31, tariff="start")
    pdo = await add_prize(kind="days", params={"days": 2}, audience={"segments": ["trial"]})
    sid = await pending_for(31, pdo)
    await raises(keep(31, sid), "prize_not_eligible", "trial-only prize after the player became paid")

    # ------------------------------------------------------------- gifts
    print("gifts")
    reset_state()

    async def make_gift(giver, prize, code):
        sid = await pending_for(giver, prize)
        await exec_("update ext_kiro_wheel_spins set status='gifted' where id=:i", i=sid)
        await exec_("insert into ext_kiro_wheel_gifts (code, spin_id, from_user, expires_at) values (:c, :s, :u, now() + interval '3 days')", c=code, s=sid, u=giver)
        return sid

    async def redeem(user, code, commit=True):
        async with factory() as s:
            out = await logic.redeem_gift(s, user, code, service)
            if commit:
                await s.commit()
            return out

    async def gift_status(code):
        return (await q("select status from ext_kiro_wheel_gifts where code=:c", c=code))[0]["status"]

    sid = await make_gift(4, pp, "GIFTPREM1")
    await raises(redeem(11, "GIFTPREM1"), "premium_unavailable", "premium gift to a trial player")
    await raises(redeem(1, "GIFTPREM1"), "subscription_required", "premium gift to a player without subscription")
    ok(await gift_status("GIFTPREM1") == "open" and STATE["premium"] == [], "refused gift is not spent (stays open, nothing granted)")
    out = await redeem(10, "GIFTPREM1")
    ok(await gift_status("GIFTPREM1") == "claimed" and out["result"]["applied"] and STATE["premium"][-1] == (10, 5.0), "the same gift works for a player on a premium tariff")
    await make_gift(4, pd, "GIFTDAYS1")
    await raises(redeem(1, "GIFTDAYS1"), "subscription_required", "days gift to a player without subscription")
    ok(await gift_status("GIFTDAYS1") == "open", "days gift stays open")
    await add_sub(32, trial=True)
    reset_state()
    out = await redeem(32, "GIFTDAYS1")
    s32 = await sub(32)
    ok(out["result"]["converted_trial"] and STATE["switches"] == [(32, "start", "admin_assign", True)] and s32["tariff_key"] == "start", "days gift to a trial player converts the trial")
    ok(2.9 * 86400 < (s32["end_date"] - datetime.now(UTC)).total_seconds() < 7.01 * 86400, "gift: term is the prize's N days from now")
    ok(await gift_status("GIFTDAYS1") == "claimed", "gift marked claimed after the conversion")
    await raises(redeem(32, "GIFTDAYS1"), "gift_used", "the gift cannot be used twice")
    await make_gift(3, pt, "GIFTTRF01")
    await add_sub(33, trial=True)
    reset_state()
    await redeem(33, "GIFTTRF01")
    ok(STATE["panel"][-1][1] == {"uuid": "panel-33", "status": "ACTIVE", "trafficLimitBytes": 5 * GIB} and STATE["grants"] == [], "traffic gift to a trial player is trial-safe")
    await make_gift(3, p_disc, "GIFTDISC1")
    await raises(redeem(1, "GIFTDISC1"), "prize_not_eligible", "audience of the prize is checked for the recipient (discount: trial+paid)")
    await make_gift(3, pd, "GIFTOWN01")
    await raises(redeem(3, "GIFTOWN01"), "gift_own", "own gift")

    await engine.dispose()
    server.cleanup()
    print(f"\n{PASSED} checks passed")


asyncio.run(main())
