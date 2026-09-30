"""Spin rules, server-side prize choice and prize fulfilment."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.plugins.extensions.rewards import Reward, grant
from bot.services.promo_code_service import PromoCodeService
from bot.services.promo_effects import PromoEffects

from bot.plugins.extensions.contracts import ExtensionError

from .storage import COLOR_KEYS, PLUGIN_ID, PRIZE_KINDS, list_prizes, load_config

logger = logging.getLogger(__name__)
_random = secrets.SystemRandom()
GIB = 1024**3

# Prizes that Core's rewards API only applies to an active subscription.
NEEDS_SUBSCRIPTION = {"days", "traffic"}


GIFT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hex_color(value: Any) -> str | None:
    color = str(value or "").strip()
    if len(color) == 7 and color.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in color[1:]):
        return color.lower()
    return None


def anonymize(name: str) -> str:
    """sirbiprod -> s***od, Иван -> И***"""
    name = (name or "").strip().lstrip("@")
    if not name:
        return "Игрок"
    if len(name) <= 4:
        return f"{name[0]}***"
    return f"{name[0]}***{name[-2:]}"


class WheelError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


# ---------------------------------------------------------------- validation


def _num(value: Any, *, lo: float, hi: float, integer: bool = False, name: str) -> float | int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise WheelError(f"invalid_{name}") from exc
    if not lo <= number <= hi:
        raise WheelError(f"invalid_{name}")
    return int(number) if integer or number.is_integer() else round(number, 3)


def clean_prize(body: dict[str, Any]) -> dict[str, Any]:
    kind = str(body.get("kind") or "")
    if kind not in PRIZE_KINDS:
        raise WheelError("invalid_kind")
    title = str(body.get("title") or "").strip()
    if not 1 <= len(title) <= 80:
        raise WheelError("invalid_title")
    description = str(body.get("description") or "").strip()[:300]
    raw = body.get("params") if isinstance(body.get("params"), dict) else {}
    params: dict[str, Any] = {}
    if kind == "days":
        params["days"] = _num(raw.get("days"), lo=1, hi=365, integer=True, name="days")
    elif kind in {"traffic", "premium"}:
        params["gb"] = _num(raw.get("gb"), lo=0.1, hi=10000, name="gb")
    elif kind == "balance":
        params["rub"] = _num(raw.get("rub"), lo=1, hi=100000, name="rub")
    elif kind == "discount":
        params["percent"] = _num(raw.get("percent"), lo=1, hi=99, integer=True, name="percent")
        scope = str(raw.get("applies_to") or "subscription")
        params["applies_to"] = scope if scope in {"subscription", "all"} else "subscription"
    elif kind == "gift_code":
        params["days"] = _num(raw.get("days"), lo=1, hi=365, integer=True, name="days")
    elif kind == "manual":
        params["note"] = str(raw.get("note") or "").strip()[:300]
    if kind in {"discount", "premium", "gift_code"}:
        params["valid_days"] = _num(
            raw.get("valid_days", 30), lo=1, hi=365, integer=True, name="valid_days"
        )
    badge = str(raw.get("badge") or "").strip()[:16]
    if badge:
        params["badge"] = badge
    tile = _hex_color(raw.get("tile_bg"))
    if tile:
        params["tile_bg"] = tile
    weight = _num(body.get("weight", 1), lo=0, hi=1_000_000, integer=True, name="weight")
    stock_raw = body.get("stock")
    stock = (
        None
        if stock_raw in (None, "", -1)
        else _num(stock_raw, lo=0, hi=1_000_000, integer=True, name="stock")
    )
    color = str(body.get("color") or "").strip()[:16]
    if color and not (color.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in color[1:])):
        color = ""
    image_id = body.get("image_id") or None
    if image_id is not None and (not isinstance(image_id, str) or not image_id.isalnum() or len(image_id) > 64):
        raise WheelError("invalid_image")
    return {
        "title": title,
        "description": description,
        "kind": kind,
        "params": params,
        "weight": weight,
        "stock": stock,
        "color": color,
        "image_id": image_id,
        "enabled": bool(body.get("enabled", True)),
        "position": _num(body.get("position", 0), lo=-100000, hi=100000, integer=True, name="position"),
    }


def clean_config(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    data = dict(current)
    if "enabled" in body:
        data["enabled"] = bool(body["enabled"])
    for key in ("title", "subtitle"):
        if key in body:
            data[key] = str(body[key] or "").strip()[: 80 if key == "title" else 200]
    for key, lo, hi in (
        ("daily_free_spins", 0, 20),
        ("spins_per_payment", 0, 50),
        ("max_bonus_spins", 0, 1000),
        ("day_offset_hours", -12, 14),
    ):
        if key in body:
            data[key] = _num(body[key], lo=lo, hi=hi, integer=True, name=key)
    for key in (
        "require_active_subscription",
        "notify_admins_on_manual",
        "show_feed",
        "allow_reroll",
        "allow_gift",
        "tile_enabled",
        "banner_fill",
    ):
        if key in body:
            data[key] = bool(body[key])
    if "banner_image_id" in body:
        banner = str(body["banner_image_id"] or "").strip()
        if banner and (not banner.isalnum() or len(banner) > 64):
            raise WheelError("invalid_image")
        data["banner_image_id"] = banner
    if "gift_ttl_days" in body:
        data["gift_ttl_days"] = _num(body["gift_ttl_days"], lo=1, hi=90, integer=True, name="gift_ttl_days")
    for key in COLOR_KEYS:
        if key in body:
            color = _hex_color(body[key])
            if color:
                data[key] = color
    return data


# ---------------------------------------------------------------- public view


def prize_label(prize: dict[str, Any]) -> str:
    p = prize.get("params") or {}
    kind = prize["kind"]
    if kind == "days":
        return f"+{p.get('days')} дн. подписки"
    if kind == "traffic":
        return f"+{p.get('gb')} ГБ трафика"
    if kind == "premium":
        return f"+{p.get('gb')} ГБ Premium-трафика"
    if kind == "balance":
        return f"+{p.get('rub')} ₽ на баланс"
    if kind == "discount":
        return f"Скидка {p.get('percent')}%"
    if kind == "gift_code":
        return f"Подарочный код на {p.get('days')} дн."
    if kind == "manual":
        return "Особый приз"
    return "Без выигрыша"


def prize_badge(prize: dict[str, Any]) -> str:
    """Short sticker text shown on the reel (admin may override it)."""
    p = prize.get("params") or {}
    if p.get("badge"):
        return str(p["badge"])
    kind = prize["kind"]
    if kind == "days":
        return f"+{p.get('days')} дн."
    if kind in {"traffic", "premium"}:
        return f"+{p.get('gb')} ГБ"
    if kind == "balance":
        return f"+{p.get('rub')} ₽"
    if kind == "discount":
        return f"−{p.get('percent')}%"
    if kind == "gift_code":
        return f"{p.get('days')} дн."
    if kind == "manual":
        return "Приз"
    return "Мимо"


def public_prize(prize: dict[str, Any]) -> dict[str, Any]:
    return {
        "tile": (prize.get("params") or {}).get("tile_bg"),
        "badge": prize_badge(prize),
        "id": prize["id"],
        "title": prize["title"],
        "description": prize["description"],
        "kind": prize["kind"],
        "label": prize_label(prize),
        "color": prize["color"],
        "image": f"/api/plugins/{PLUGIN_ID}/img/{prize['image_id']}" if prize.get("image_id") else None,
    }


def _day_start(now: datetime, offset_hours: int) -> datetime:
    local = now + timedelta(hours=offset_hours)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local - timedelta(hours=offset_hours)


async def has_active_subscription(session: AsyncSession, user_id: int) -> bool:
    return bool(
        await session.scalar(
            text(
                "select 1 from subscriptions where user_id = :u and is_active "
                "and end_date > now() limit 1"
            ),
            {"u": user_id},
        )
    )


async def spin_state(session: AsyncSession, user_id: int, config: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    day_start = _day_start(now, int(config["day_offset_hours"]))
    daily_used = int(
        await session.scalar(
            text(
                "select count(*) from ext_kiro_wheel_spins where user_id = :u and source = 'daily' "
                "and created_at >= :d"
            ),
            {"u": user_id, "d": day_start},
        )
        or 0
    )
    bonus = int(
        await session.scalar(
            text("select spins from ext_kiro_wheel_credits where user_id = :u"), {"u": user_id}
        )
        or 0
    )
    daily_left = max(0, int(config["daily_free_spins"]) - daily_used)
    subscribed = await has_active_subscription(session, user_id)
    reason = None
    if not config["enabled"]:
        reason = "disabled"
    elif config["require_active_subscription"] and not subscribed:
        reason = "subscription_required"
    elif daily_left + bonus <= 0:
        reason = "no_spins"
    return {
        "daily_left": daily_left,
        "bonus": bonus,
        "available": 0 if reason in {"disabled", "subscription_required"} else daily_left + bonus,
        "next_free_at": (day_start + timedelta(days=1)).isoformat(),
        "subscribed": subscribed,
        "can_spin": reason is None,
        "reason": reason,
    }


# ---------------------------------------------------------------- spinning


async def _eligible(session: AsyncSession, prizes: list[dict[str, Any]], subscribed: bool) -> list[dict[str, Any]]:
    out = []
    for prize in prizes:
        if prize["weight"] <= 0:
            continue
        if prize["stock"] is not None and prize["stock"] <= 0:
            continue
        if prize["kind"] in NEEDS_SUBSCRIPTION and not subscribed:
            continue
        out.append(prize)
    return out


def _choose(prizes: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(int(p["weight"]) for p in prizes)
    roll = _random.randrange(total)
    for prize in prizes:
        roll -= int(prize["weight"])
        if roll < 0:
            return prize
    return prizes[-1]


async def _issue_code(
    session: AsyncSession, user_id: int | None, effects: PromoEffects, valid_days: int
) -> str:
    code = await PromoCodeService.issue_code(
        session,
        effects=effects,
        code=None,
        max_activations=1,
        valid_until=datetime.now(UTC) + timedelta(days=int(valid_days)),
        origin=PLUGIN_ID,
        created_by_admin_id=None,
        user_id=user_id,
    )
    return str(code.code)


async def _fulfil(
    session: AsyncSession, prize: dict[str, Any], user_id: int, key: str
) -> dict[str, Any]:
    kind = prize["kind"]
    p = prize["params"]
    if kind == "days":
        await grant(
            session,
            owner=PLUGIN_ID,
            user_id=user_id,
            idempotency_key=key,
            reward=Reward(kind="days", amount=int(p["days"])),
        )
        return {"applied": True, "text": f"К подписке добавлено {p['days']} дн."}
    if kind == "traffic":
        await grant(
            session,
            owner=PLUGIN_ID,
            user_id=user_id,
            idempotency_key=key,
            reward=Reward(kind="traffic", amount=int(float(p["gb"]) * GIB)),
        )
        return {"applied": True, "text": f"Добавлено {p['gb']} ГБ трафика"}
    if kind == "balance":
        await grant(
            session,
            owner=PLUGIN_ID,
            user_id=user_id,
            idempotency_key=key,
            reward=Reward(kind="balance", amount=int(round(float(p["rub"]) * 100)), currency="RUB"),
        )
        return {"applied": True, "text": f"На баланс зачислено {p['rub']} ₽"}
    if kind == "discount":
        code = await _issue_code(
            session,
            user_id,
            PromoEffects(discount_percent=float(p["percent"]), applies_to=p.get("applies_to", "subscription")),
            p["valid_days"],
        )
        return {
            "applied": True,
            "code": code,
            "text": f"Промокод на скидку {p['percent']}%: введите его при оплате. "
            f"Действует {p['valid_days']} дн.",
        }
    if kind == "premium":
        code = await _issue_code(
            session, user_id, PromoEffects(premium_traffic_gb=float(p["gb"])), p["valid_days"]
        )
        return {
            "applied": True,
            "code": code,
            "text": f"Код на {p['gb']} ГБ Premium-трафика: активируйте его в настройках. "
            f"Действует {p['valid_days']} дн.",
        }
    if kind == "gift_code":
        code = await _issue_code(session, None, PromoEffects(bonus_days=int(p["days"])), p["valid_days"])
        return {
            "applied": True,
            "code": code,
            "text": f"Подарочный код на {p['days']} дн. Можно активировать самому или подарить другу. "
            f"Действует {p['valid_days']} дн.",
        }
    if kind == "manual":
        return {
            "applied": False,
            "manual": True,
            "text": p.get("note") or "Мы свяжемся с вами, чтобы вручить приз.",
        }
    return {"applied": False, "text": prize.get("description") or "В этот раз без выигрыша. Попробуйте ещё!"}


def _row_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return json.loads(value) if value else {}


async def _lock_user(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        text("select pg_advisory_xact_lock(hashtext('kiro-wheel'), cast(cast(:u as bigint) % 2147483647 as integer))"),
        {"u": user_id},
    )


def pending_view(row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    prize = _row_json(row["prize"])
    nothing = prize.get("kind") == "nothing"
    return {
        "spin_id": row["id"],
        "prize": prize,
        "can_reroll": bool(config["allow_reroll"]) and row["source"] != "reroll",
        "can_gift": bool(config["allow_gift"]) and not nothing,
        "nothing": nothing,
    }


async def get_pending(session: AsyncSession, user_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "select id, source, prize, created_at from ext_kiro_wheel_spins "
                "where user_id = :u and status = 'pending' order by created_at desc limit 1"
            ),
            {"u": user_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def _roll(
    session: AsyncSession, user_id: int, subscribed: bool, *, source: str, request_id: str, parent_id: str | None
) -> dict[str, Any]:
    prizes = await _eligible(session, await list_prizes(session, include_disabled=False), subscribed)
    if not prizes:
        raise WheelError("no_prizes", 409)
    chosen = _choose(prizes)
    if chosen["stock"] is not None:
        updated = await session.execute(
            text("update ext_kiro_wheel_prizes set stock = stock - 1 where id = :id and stock > 0"),
            {"id": chosen["id"]},
        )
        if updated.rowcount != 1:  # raced to zero: fall back to another prize
            prizes = [p for p in prizes if p["id"] != chosen["id"] and p["stock"] is None]
            if not prizes:
                raise WheelError("no_prizes", 409)
            chosen = _choose(prizes)
    spin_id = uuid.uuid4().hex
    snapshot = {**public_prize(chosen), "params": chosen["params"]}
    await session.execute(
        text(
            "insert into ext_kiro_wheel_spins (id, user_id, request_id, source, prize_id, prize, result, status, parent_id) "
            "values (:id, :u, :r, :s, :p, cast(:prize as jsonb), '{}'::jsonb, 'pending', :parent)"
        ),
        {
            "id": spin_id,
            "u": user_id,
            "r": request_id,
            "s": source,
            "p": chosen["id"],
            "prize": json.dumps(snapshot, ensure_ascii=False),
            "parent": parent_id,
        },
    )
    return {"id": spin_id, "source": source, "prize": snapshot}


async def spin(session: AsyncSession, user_id: int, request_id: str) -> dict[str, Any]:
    """Consume one spin and draw a prize that stays *pending* until kept, gifted or rerolled.

    Runs inside one transaction; the caller commits.
    """
    if not (8 <= len(request_id) <= 64) or not all(c.isalnum() or c in "-_" for c in request_id):
        raise WheelError("invalid_request_id")
    await _lock_user(session, user_id)
    config = await load_config(session)
    previous = (
        await session.execute(
            text("select id, source, prize from ext_kiro_wheel_spins where user_id = :u and request_id = :r"),
            {"u": user_id, "r": request_id},
        )
    ).mappings().first()
    if previous is not None:
        return {**pending_view(dict(previous), config), "repeated": True}

    banned = await session.scalar(text("select is_banned from users where user_id = :u"), {"u": user_id})
    if banned is None or banned:
        raise WheelError("access_denied", 403)
    if await get_pending(session, user_id):
        raise WheelError("pending_prize", 409)
    state = await spin_state(session, user_id, config)
    if not state["can_spin"]:
        raise WheelError(state["reason"] or "no_spins", 409)

    source = "daily" if state["daily_left"] > 0 else "bonus"
    if source == "bonus":
        updated = await session.execute(
            text(
                "update ext_kiro_wheel_credits set spins = spins - 1, updated_at = now() "
                "where user_id = :u and spins > 0"
            ),
            {"u": user_id},
        )
        if updated.rowcount != 1:
            raise WheelError("no_spins", 409)
    row = await _roll(session, user_id, state["subscribed"], source=source, request_id=request_id, parent_id=None)
    return {**pending_view(row, config), "repeated": False}


async def _pending_for_update(session: AsyncSession, user_id: int, spin_id: str) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                "select id, source, prize, prize_id, status from ext_kiro_wheel_spins "
                "where id = :id and user_id = :u for update"
            ),
            {"id": spin_id, "u": user_id},
        )
    ).mappings().first()
    if row is None:
        raise WheelError("spin_not_found", 404)
    if row["status"] != "pending":
        raise WheelError("already_resolved", 409)
    return dict(row)


async def keep(session: AsyncSession, user_id: int, spin_id: str) -> dict[str, Any]:
    await _lock_user(session, user_id)
    row = await _pending_for_update(session, user_id, spin_id)
    prize = _row_json(row["prize"])
    try:
        result = await _fulfil(session, prize, user_id, f"spin:{spin_id}")
    except ExtensionError as exc:
        raise WheelError("subscription_required" if "subscription" in str(exc) else "fulfil_failed", 409) from exc
    await session.execute(
        text(
            "update ext_kiro_wheel_spins set status = 'claimed', result = cast(:r as jsonb), resolved_at = now() "
            "where id = :id"
        ),
        {"id": spin_id, "r": json.dumps(result, ensure_ascii=False)},
    )
    return {"spin_id": spin_id, "prize": prize, "result": result}


async def reroll(session: AsyncSession, user_id: int, spin_id: str) -> dict[str, Any]:
    await _lock_user(session, user_id)
    config = await load_config(session)
    if not config["allow_reroll"]:
        raise WheelError("reroll_disabled", 409)
    row = await _pending_for_update(session, user_id, spin_id)
    if row["source"] == "reroll":
        raise WheelError("reroll_used", 409)
    await session.execute(
        text("update ext_kiro_wheel_spins set status = 'declined', resolved_at = now() where id = :id"),
        {"id": spin_id},
    )
    if row["prize_id"] is not None:  # the declined prize goes back to stock
        await session.execute(
            text("update ext_kiro_wheel_prizes set stock = stock + 1 where id = :id and stock is not null"),
            {"id": row["prize_id"]},
        )
    subscribed = await has_active_subscription(session, user_id)
    new = await _roll(
        session, user_id, subscribed, source="reroll", request_id=f"reroll-{spin_id}", parent_id=spin_id
    )
    return pending_view(new, config)


def _gift_code() -> str:
    return "".join(_random.choice(GIFT_ALPHABET) for _ in range(8))


async def gift(session: AsyncSession, user_id: int, spin_id: str) -> dict[str, Any]:
    await _lock_user(session, user_id)
    config = await load_config(session)
    if not config["allow_gift"]:
        raise WheelError("gift_disabled", 409)
    row = await _pending_for_update(session, user_id, spin_id)
    prize = _row_json(row["prize"])
    if prize.get("kind") == "nothing":
        raise WheelError("nothing_to_gift", 409)
    code = _gift_code()
    expires = datetime.now(UTC) + timedelta(days=int(config["gift_ttl_days"]))
    await session.execute(
        text(
            "insert into ext_kiro_wheel_gifts (code, spin_id, from_user, expires_at) values (:c, :s, :u, :e)"
        ),
        {"c": code, "s": spin_id, "u": user_id, "e": expires},
    )
    await session.execute(
        text("update ext_kiro_wheel_spins set status = 'gifted', resolved_at = now() where id = :id"),
        {"id": spin_id},
    )
    return {"code": code, "expires_at": expires.isoformat(), "prize": prize}


async def cancel_gift(session: AsyncSession, user_id: int, code: str) -> dict[str, Any]:
    await _lock_user(session, user_id)
    gift_row = (
        await session.execute(
            text(
                "select code, spin_id, status from ext_kiro_wheel_gifts where code = :c and from_user = :u for update"
            ),
            {"c": code.upper(), "u": user_id},
        )
    ).mappings().first()
    if gift_row is None:
        raise WheelError("gift_not_found", 404)
    if gift_row["status"] != "open":
        raise WheelError("gift_used", 409)
    if await get_pending(session, user_id):
        raise WheelError("pending_prize", 409)
    await session.execute(
        text("update ext_kiro_wheel_gifts set status = 'cancelled' where code = :c"), {"c": gift_row["code"]}
    )
    await session.execute(
        text("update ext_kiro_wheel_spins set status = 'pending', resolved_at = null where id = :id"),
        {"id": gift_row["spin_id"]},
    )
    config = await load_config(session)
    row = (
        await session.execute(
            text("select id, source, prize from ext_kiro_wheel_spins where id = :id"), {"id": gift_row["spin_id"]}
        )
    ).mappings().first()
    return pending_view(dict(row), config)


async def redeem_gift(session: AsyncSession, user_id: int, code: str) -> dict[str, Any]:
    code = "".join(ch for ch in code.upper() if ch.isalnum())
    if not 6 <= len(code) <= 16:
        raise WheelError("gift_not_found", 404)
    await _lock_user(session, user_id)
    gift_row = (
        await session.execute(
            text(
                "select g.code, g.spin_id, g.from_user, g.status, g.expires_at, s.prize "
                "from ext_kiro_wheel_gifts g join ext_kiro_wheel_spins s on s.id = g.spin_id "
                "where g.code = :c for update of g"
            ),
            {"c": code},
        )
    ).mappings().first()
    if gift_row is None:
        raise WheelError("gift_not_found", 404)
    if gift_row["status"] != "open":
        raise WheelError("gift_used", 409)
    if gift_row["expires_at"] <= datetime.now(UTC):
        raise WheelError("gift_expired", 409)
    if int(gift_row["from_user"]) == int(user_id):
        raise WheelError("gift_own", 409)
    banned = await session.scalar(text("select is_banned from users where user_id = :u"), {"u": user_id})
    if banned is None or banned:
        raise WheelError("access_denied", 403)
    prize = _row_json(gift_row["prize"])
    try:
        result = await _fulfil(session, prize, user_id, f"gift:{code}")
    except ExtensionError as exc:
        raise WheelError("subscription_required" if "subscription" in str(exc) else "fulfil_failed", 409) from exc
    await session.execute(
        text(
            "update ext_kiro_wheel_gifts set status = 'claimed', claimed_by = :u, claimed_at = now(), "
            "result = cast(:r as jsonb) where code = :c"
        ),
        {"u": user_id, "c": code, "r": json.dumps(result, ensure_ascii=False)},
    )
    await session.execute(
        text("update ext_kiro_wheel_spins set status = 'transferred' where id = :id"), {"id": gift_row["spin_id"]}
    )
    return {"prize": prize, "result": result, "from_user": int(gift_row["from_user"])}


async def my_gifts(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "select g.code, g.status, g.expires_at, g.created_at, s.prize from ext_kiro_wheel_gifts g "
                "join ext_kiro_wheel_spins s on s.id = g.spin_id where g.from_user = :u "
                "and (g.status = 'open' or g.claimed_at > now() - interval '7 days') "
                "order by g.created_at desc limit 10"
            ),
            {"u": user_id},
        )
    ).mappings().all()
    now = datetime.now(UTC)
    return [
        {
            "code": row["code"],
            "status": "expired" if row["status"] == "open" and row["expires_at"] <= now else row["status"],
            "expires_at": row["expires_at"].isoformat(),
            "prize": _row_json(row["prize"]).get("title"),
        }
        for row in rows
    ]


async def add_bonus_spins(
    session: AsyncSession, user_id: int, spins: int, *, key: str, reason: str, cap: int
) -> bool:
    """Idempotent by ``key``. Returns False when the grant was already applied."""
    inserted = await session.execute(
        text(
            "insert into ext_kiro_wheel_grants (key, user_id, spins, reason) values (:k, :u, :s, :r) "
            "on conflict (key) do nothing"
        ),
        {"k": key[:128], "u": user_id, "s": spins, "r": reason[:32]},
    )
    if inserted.rowcount != 1:
        return False
    await session.execute(
        text(
            "insert into ext_kiro_wheel_credits (user_id, spins) values (:u, least(cast(:s as integer), cast(:cap as integer))) "
            "on conflict (user_id) do update set spins = least(ext_kiro_wheel_credits.spins + cast(:s as integer), "
            "cast(:cap as integer)), "
            "updated_at = now()"
        ),
        {"u": user_id, "s": spins, "cap": max(cap, 0)},
    )
    return True
