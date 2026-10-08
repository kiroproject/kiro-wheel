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
from bot.services.subscription_service_impl.trial_traffic_reset import (
    reset_trial_traffic_for_paid_activation,
)
from db.dal import subscription_dal, user_dal

from bot.plugins.extensions.contracts import ExtensionError

from . import daily
from .storage import COLOR_KEYS, PLUGIN_ID, PRIZE_KINDS, list_prizes, load_config

logger = logging.getLogger(__name__)
_random = secrets.SystemRandom()
GIB = 1024**3

# Prizes that need an active subscription (days / regular traffic: Core's rewards API; premium: its squad).
NEEDS_SUBSCRIPTION = {"days", "traffic", "premium"}

# Who can win a prize. none = no active subscription, trial = trial subscription (no tariff),
# paid = a subscription bound to a tariff (optionally only some tariffs).
SEGMENTS = ("none", "trial", "paid")
SEGMENT_LABELS = {"none": "без подписки", "trial": "пробная подписка", "paid": "платная подписка"}
ALL_SEGMENTS = list(SEGMENTS)
# Segments each kind may be offered to. The rest is impossible (nothing to extend / no premium squad).
ALLOWED_SEGMENTS = {
    "days": ("trial", "paid"),
    "traffic": ("trial", "paid"),
    "premium": ("paid",),
}


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


def legacy_audience(kind: str) -> dict[str, Any]:
    """Availability a prize had before audiences existed (used for old rows and old pending spins)."""
    return {"segments": list(ALLOWED_SEGMENTS.get(kind, ALL_SEGMENTS)), "tariff_keys": []}


def prize_audience(prize: dict[str, Any]) -> dict[str, Any]:
    """Effective audience of a prize (stored one, or the legacy default when it was never set)."""
    kind = str(prize.get("kind") or "")
    raw = (prize.get("params") or {}).get("audience")
    if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
        return legacy_audience(kind)
    segments = [s for s in SEGMENTS if s in raw["segments"]]
    keys = [str(k) for k in (raw.get("tariff_keys") or []) if str(k).strip()]
    return {"segments": segments, "tariff_keys": keys}


def clean_audience(raw: Any, kind: str, tariffs: Any = None) -> dict[str, Any]:
    """Validate an admin-provided audience. ``tariffs`` is Core's TariffsConfig (None: keys are not checked)."""
    if raw is None:
        return legacy_audience(kind)
    if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
        raise WheelError("invalid_audience")
    requested = [str(s) for s in raw["segments"]]
    if any(s not in SEGMENTS for s in requested):
        raise WheelError("invalid_audience")
    allowed = ALLOWED_SEGMENTS.get(kind, ALL_SEGMENTS)
    if any(s not in allowed for s in requested):
        # e.g. "no subscription" for days: there is nothing to extend
        raise WheelError("audience_segment_unsupported")
    segments = [s for s in SEGMENTS if s in requested]
    if not segments:
        raise WheelError("audience_empty")
    keys: list[str] = []
    if "paid" in segments:
        raw_keys = raw.get("tariff_keys") or []
        if not isinstance(raw_keys, list):
            raise WheelError("invalid_audience")
        for item in raw_keys:
            key = str(item or "").strip()
            if not key:
                continue
            if len(key) > 64:
                raise WheelError("invalid_audience_tariff")
            if tariffs is not None:
                tariff = tariffs.get(key)
                if tariff is None:
                    raise WheelError("invalid_audience_tariff")
                key = str(tariff.key)
            if key not in keys:
                keys.append(key)
    return {"segments": segments, "tariff_keys": keys}


def _convert_key(value: str, tariffs: Any) -> str:
    """Validate a tariff a trial may be converted to (must exist and be a period tariff)."""
    if len(value) > 64:
        raise WheelError("invalid_convert_tariff")
    if tariffs is None:
        return value
    tariff = tariffs.get(value)
    if tariff is None or str(getattr(tariff, "billing_model", "period")) != "period":
        raise WheelError("invalid_convert_tariff")
    return str(tariff.key)


def clean_prize(body: dict[str, Any], tariffs: Any = None) -> dict[str, Any]:
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
        convert = str(raw.get("convert_tariff") or "").strip()
        if convert:
            params["convert_tariff"] = _convert_key(convert, tariffs)
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
    if kind in {"discount", "gift_code"}:  # premium is credited at once now: no code to expire
        params["valid_days"] = _num(
            raw.get("valid_days", 30), lo=1, hi=365, integer=True, name="valid_days"
        )
    params["audience"] = clean_audience(body.get("audience"), kind, tariffs)
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


def clean_config(body: dict[str, Any], current: dict[str, Any], tariffs: Any = None) -> dict[str, Any]:
    data = dict(current)
    if "trial_convert_tariff" in body:
        key = str(body["trial_convert_tariff"] or "").strip()
        data["trial_convert_tariff"] = _convert_key(key, tariffs) if key else ""
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
        "debug_log",
    ):
        if key in body:
            data[key] = bool(body[key])
    if "banner_image_id" in body:
        banner = str(body["banner_image_id"] or "").strip()
        if banner and (not banner.isalnum() or len(banner) > 64):
            raise WheelError("invalid_image")
        data["banner_image_id"] = banner
    if "daily_enabled" in body:
        data["daily_enabled"] = bool(body["daily_enabled"])
    if "daily_rewards" in body:
        try:
            data["daily_rewards"] = daily.clean_rewards(body["daily_rewards"])
        except daily.DailyError as exc:
            raise WheelError(exc.code) from exc
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


async def account_status(session: AsyncSession, user_id: int) -> str | None:
    """None when the account may play, otherwise the reason code shown to the player and logged."""
    row = (await session.execute(text("select is_banned from users where user_id = :u"), {"u": user_id})).first()
    if row is None:
        return "profile_not_found"
    # is_banned is a nullable column: NULL means "not banned", exactly as in the Minishop core
    return "account_banned" if row[0] else None


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
    # With the daily login calendar on, the automatic free spin of the day is no longer given.
    # Automatic free spins per day; 0 means there is no next-day spin to count down to.
    auto_per_day = 0 if config.get("daily_enabled") else int(config["daily_free_spins"])
    daily_left = max(0, auto_per_day - daily_used)
    subscribed = await has_active_subscription(session, user_id)
    access = await account_status(session, user_id)
    reason = None
    if not config["enabled"]:
        reason = "disabled"
    elif access:
        reason = access
    elif config["require_active_subscription"] and not subscribed:
        reason = "subscription_required"
    elif daily_left + bonus <= 0:
        reason = "no_spins"
    return {
        "daily_left": daily_left,
        "bonus": bonus,
        "available": 0 if reason in {"disabled", "subscription_required", "account_banned", "profile_not_found"} else daily_left + bonus,
        "access": access,
        "next_free_at": (day_start + timedelta(days=1)).isoformat() if auto_per_day > 0 else None,
        "subscribed": subscribed,
        "can_spin": reason is None,
        "reason": reason,
    }


# ---------------------------------------------------------------- spinning


# ---------------------------------------------------------------- audiences


def tariffs_of(service: Any) -> Any:
    """Core TariffsConfig from a SubscriptionService or Settings object (None when unavailable)."""
    settings = getattr(service, "settings", service)
    try:
        return getattr(settings, "tariffs_config", None)
    except Exception:  # noqa: BLE001 - a broken tariffs.json must not break the wheel
        logger.warning("kiro-wheel: tariffs config unavailable", exc_info=True)
        return None


def _canonical_tariff_key(tariffs: Any, key: str) -> str:
    tariff = tariffs.get(key) if tariffs is not None and key else None
    return str(tariff.key) if tariff is not None else key


def tariff_title(tariff: Any) -> str:
    names = getattr(tariff, "names", None) or {}
    for lang in ("ru", "en"):
        if names.get(lang):
            return str(names[lang])
    for value in names.values():
        if value:
            return str(value)
    return str(getattr(tariff, "key", ""))


def tariff_catalog(tariffs: Any) -> list[dict[str, Any]]:
    """Tariffs for the admin UI: key, title, traffic, premium flag."""
    if tariffs is None:
        return []
    out = []
    for tariff in getattr(tariffs, "tariffs", []):
        out.append(
            {
                "key": str(tariff.key),
                "title": tariff_title(tariff),
                "enabled": bool(getattr(tariff, "enabled", True)),
                "billing_model": str(getattr(tariff, "billing_model", "period")),
                "monthly_gb": getattr(tariff, "monthly_gb", None),
                "premium": bool(getattr(tariff, "premium_squad_uuids", None)),
                "premium_monthly_gb": getattr(tariff, "premium_monthly_gb", None),
                "hwid_device_limit": getattr(tariff, "hwid_device_limit", None),
            }
        )
    return out


async def active_subscription(session: AsyncSession, user_id: int) -> dict[str, Any] | None:
    """Current subscription row (latest end date among active ones)."""
    row = (
        await session.execute(
            text(
                "select subscription_id, panel_user_uuid, provider, status_from_panel, tariff_key, end_date, "
                "traffic_limit_bytes "
                "from subscriptions where user_id = :u and is_active and end_date > now() "
                "order by end_date desc limit 1"
            ),
            {"u": user_id},
        )
    ).mappings().first()
    return dict(row) if row else None


def has_unlimited_traffic(sub: dict[str, Any] | None) -> bool:
    """Core keeps a 0 (or NULL) traffic limit for "no limit" (same rule as kiro-diagnostics).

    Adding GB to such a subscription would turn it into a capped one, so the wheel must not do it.
    """
    return sub is not None and "traffic_limit_bytes" in sub and int(sub["traffic_limit_bytes"] or 0) <= 0


def is_trial(sub: dict[str, Any] | None) -> bool:
    """Core subscription_is_trial() for a subscription that is not bound to a tariff yet."""
    if not sub or str(sub.get("tariff_key") or "").strip():
        return False
    provider = str(sub.get("provider") or "").strip().lower()
    status = str(sub.get("status_from_panel") or "").strip().upper()
    return provider == "trial" or status == "TRIAL"


async def user_segment(session: AsyncSession, user_id: int, tariffs: Any = None) -> dict[str, Any]:
    """none (no active subscription) / trial (trial without tariff) / paid (+ its tariff key)."""
    sub = await active_subscription(session, user_id)
    if sub is None:
        return {"segment": "none", "tariff_key": None, "sub": None}
    if is_trial(sub):
        return {"segment": "trial", "tariff_key": None, "sub": sub}
    key = str(sub.get("tariff_key") or "").strip()
    return {"segment": "paid", "tariff_key": _canonical_tariff_key(tariffs, key) if key else None, "sub": sub}


def convert_tariff_for(prize: dict[str, Any], config: dict[str, Any], tariffs: Any) -> Any:
    """Tariff a trial is converted to: the prize's own, else the plugin default, else Core's default."""
    if tariffs is None:
        return None
    key = (
        str((prize.get("params") or {}).get("convert_tariff") or "").strip()
        or str(config.get("trial_convert_tariff") or "").strip()
        or str(getattr(tariffs, "default_tariff", "") or "").strip()
    )
    tariff = tariffs.get(key) if key else None
    if tariff is None or str(getattr(tariff, "billing_model", "period")) != "period":
        return None
    return tariff


def ineligible_reason(
    prize: dict[str, Any], seg: dict[str, Any], config: dict[str, Any], service: Any = None
) -> str | None:
    """None when the user (segment) may receive the prize now, otherwise a reason code."""
    kind = prize["kind"]
    segment = seg["segment"]
    if kind in NEEDS_SUBSCRIPTION and segment == "none":
        return "subscription_required"
    tariffs = tariffs_of(service) if service is not None else None
    if kind == "premium":  # hard rule on top of the audience: only a tariff that has a premium squad
        tariff = tariffs.get(seg["tariff_key"]) if tariffs is not None and seg.get("tariff_key") else None
        if segment != "paid" or tariff is None or not getattr(tariff, "premium_squad_uuids", None):
            return "premium_unavailable"
    audience = prize_audience(prize)
    if segment not in audience["segments"]:
        return "prize_not_eligible"
    if segment == "paid" and audience["tariff_keys"]:
        allowed = {_canonical_tariff_key(tariffs, k) for k in audience["tariff_keys"]}
        if seg.get("tariff_key") not in allowed:
            return "prize_not_eligible"
    if kind == "days" and segment == "trial" and convert_tariff_for(prize, config, tariffs) is None:
        return "prize_not_eligible"
    if kind == "traffic" and segment == "trial":
        if int(getattr(getattr(service, "settings", None), "trial_traffic_limit_bytes", 0) or 0) <= 0:
            return "prize_not_eligible"  # unlimited trial: there is nothing to add
    if kind == "traffic" and has_unlimited_traffic(seg.get("sub")):
        return "prize_not_eligible"  # unlimited plan: GB must not touch the subscription at all
    return None


def _eligible(
    prizes: list[dict[str, Any]], seg: dict[str, Any], config: dict[str, Any], service: Any = None
) -> list[dict[str, Any]]:
    out = []
    for prize in prizes:
        if prize["weight"] <= 0:
            continue
        if prize["stock"] is not None and prize["stock"] <= 0:
            continue
        if ineligible_reason(prize, seg, config, service):
            continue
        out.append(prize)
    return out


async def decision_hint(
    session: AsyncSession, user_id: int, prize: dict[str, Any], config: dict[str, Any], service: Any
) -> str | None:
    """A short note shown with a pending prize when it behaves differently for this player."""
    kind = prize.get("kind")
    if kind not in {"days", "traffic"} or service is None:
        return None
    tariffs = tariffs_of(service)
    seg = await user_segment(session, user_id, tariffs)
    if seg["segment"] != "trial":
        return None
    if kind == "days":
        tariff = convert_tariff_for(prize, config, tariffs)
        if tariff is None:
            return None
        days = int((prize.get("params") or {}).get("days") or 0)
        return f"Пробный период будет заменён тарифом «{tariff_title(tariff)}» на {days} дн. с момента получения приза."
    return "ГБ добавятся к трафику пробного периода."


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


async def _convert_trial(
    session: AsyncSession,
    service: Any,
    user_id: int,
    seg: dict[str, Any],
    tariff: Any,
    days: int,
    key: str,
) -> dict[str, Any]:
    """Trial -> paid base tariff for exactly ``days`` days from now (the rest of the trial is dropped).

    Same result as an admin switching the tariff by hand (Core ``admin_assign``, which has a dedicated
    trial branch): trial squads are replaced by the tariff's, tariff limits / HWID / tag are applied and the
    subscription stops being a trial. The row stays, so Core still treats the trial as used.
    Core's grant(Reward) is deliberately NOT used: its traffic sync resets a tariff-less trial to unlimited.
    """
    sub = seg["sub"]
    subscription_id = int(sub["subscription_id"])
    ends_at = datetime.now(UTC) + timedelta(days=int(days))
    # The switch pushes the row's end_date to the panel, so set the new term first (same transaction).
    await subscription_dal.update_subscription(session, subscription_id, {"end_date": ends_at})
    switched = await service.switch_tariff_without_payment(
        session, user_id, str(tariff.key), "admin_assign", apply_tariff_hwid_limit=True
    )
    if not switched:
        raise ExtensionError("trial_convert_failed", 502)
    # Mark where the tariff came from (tariff_changes has no note column; it only says "admin_assign").
    await subscription_dal.update_subscription(
        session, subscription_id, {"tariff_binding_note": f"{PLUGIN_ID}:trial_convert:{key}"[:255]}
    )
    # Same step Core does on a paid activation over a trial: the paid counter starts from zero.
    try:
        reset = await reset_trial_traffic_for_paid_activation(service, str(sub["panel_user_uuid"]))
    except Exception:  # noqa: BLE001 - best effort, the tariff itself is already switched
        logger.warning("kiro-wheel: trial traffic reset failed", exc_info=True)
        reset = None
    if reset:
        await session.execute(
            text(
                "update subscriptions set traffic_used_bytes = :used, "
                "traffic_period_lifetime_start_bytes = :life, traffic_topup_accounting_state = null, "
                "period_start_at = :start where subscription_id = :id"
            ),
            {
                "used": reset.get("traffic_used_bytes"),
                "life": reset.get("traffic_period_lifetime_start_bytes"),
                "start": reset.get("period_start_at"),
                "id": subscription_id,
            },
        )
    title = tariff_title(tariff)
    return {
        "applied": True,
        "converted_trial": True,
        "source": PLUGIN_ID,
        "tariff_key": str(tariff.key),
        "tariff_title": title,
        "days": int(days),
        "ends_at": ends_at.isoformat(),
        "subscription_id": subscription_id,
        "text": f"Пробный период заменён тарифом «{title}» на {days} дн.",
    }


async def _grant_trial_traffic(
    session: AsyncSession, service: Any, sub: dict[str, Any], amount: int
) -> None:
    """Add regular traffic to a trial WITHOUT Core's sync_main_traffic_limit_to_panel().

    That sync treats a tariff-less trial as "baseline 0" and pushes trafficLimitBytes=0 (unlimited),
    hwidDeviceLimit=USER_HWID_DEVICE_LIMIT and tag=None. Here only trafficLimitBytes (+status) is sent,
    squads, HWID, tag and the term are untouched. The value is absolute (trial limit + top-ups + bonus),
    so a retry after a failure converges to the same result.
    """
    base = int(service.settings.trial_traffic_limit_bytes or 0)
    row = (
        await session.execute(
            text(
                "select regular_bonus_bytes, topup_balance_bytes from subscriptions "
                "where subscription_id = :id for update"
            ),
            {"id": sub["subscription_id"]},
        )
    ).first()
    bonus = int(row[0] or 0) + int(amount)
    limit = base + int(row[1] or 0) + bonus
    if limit > 2**63 - 1:
        raise ExtensionError("invalid_extension_reward", 400)
    panel_uuid = str(sub["panel_user_uuid"])
    updated = await service.panel_service.update_user_details_on_panel(
        panel_uuid, {"uuid": panel_uuid, "status": "ACTIVE", "trafficLimitBytes": limit}
    )
    if not updated or (isinstance(updated, dict) and updated.get("error")):
        raise ExtensionError("extension_panel_sync_failed", 502)
    await session.execute(
        text(
            "update subscriptions set regular_bonus_bytes = :b, traffic_limit_bytes = :l, "
            "is_throttled = false where subscription_id = :id"
        ),
        {"b": bonus, "l": limit, "id": sub["subscription_id"]},
    )


async def _fulfil(
    session: AsyncSession,
    prize: dict[str, Any],
    user_id: int,
    key: str,
    service: Any = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Give the prize to ``user_id``. ``service`` is Core's SubscriptionService.

    The audience is checked here against the *current* state of the recipient, so it also covers a prize
    that was won earlier, or gifted to someone else (the segment is always the recipient's).
    """
    kind = prize["kind"]
    p = prize["params"]
    await user_dal.lock_user_by_id(session, user_id)  # Core lock order: the user row first
    config = config if config is not None else await load_config(session)
    seg = await user_segment(session, user_id, tariffs_of(service) if service is not None else None)
    reason = ineligible_reason(prize, seg, config, service)
    if reason:
        raise WheelError(reason, 409)
    segment = seg["segment"]
    if kind == "days":
        if segment == "trial":
            tariff = convert_tariff_for(prize, config, tariffs_of(service))
            if tariff is None:  # guarded by ineligible_reason; kept as a hard stop
                raise WheelError("prize_not_eligible", 409)
            return await _convert_trial(session, service, user_id, seg, tariff, int(p["days"]), key)
        await grant(
            session,
            owner=PLUGIN_ID,
            user_id=user_id,
            idempotency_key=key,
            reward=Reward(kind="days", amount=int(p["days"])),
        )
        return {"applied": True, "text": f"К подписке добавлено {p['days']} дн."}
    if kind == "traffic":
        amount = int(float(p["gb"]) * GIB)
        if segment == "trial":
            await _grant_trial_traffic(session, service, seg["sub"], amount)
            return {"applied": True, "text": f"Добавлено {p['gb']} ГБ трафика к пробному периоду"}
        await grant(
            session,
            owner=PLUGIN_ID,
            user_id=user_id,
            idempotency_key=key,
            reward=Reward(kind="traffic", amount=amount),
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
        # Paid tariff with a premium squad (checked above): credited at once, exactly like the admin
        # "premium top-up" (premium_topup_balance_bytes + T-PremiumWL squad + a traffic_topups row).
        granted = await service.admin_grant_premium_topup(session, user_id, float(p["gb"]))
        if not granted:
            raise ExtensionError("extension_panel_sync_failed", 502)
        return {
            "applied": True,
            "granted_premium_bytes": int(granted["granted_bytes"]),
            "text": f"Добавлено {p['gb']} ГБ Premium-трафика (белые списки)",
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
    session: AsyncSession,
    user_id: int,
    config: dict[str, Any],
    service: Any,
    *,
    source: str,
    request_id: str,
    parent_id: str | None,
) -> dict[str, Any]:
    seg = await user_segment(session, user_id, tariffs_of(service) if service is not None else None)
    prizes = _eligible(await list_prizes(session, include_disabled=False), seg, config, service)
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


async def spin(
    session: AsyncSession, user_id: int, request_id: str, service: Any = None
) -> dict[str, Any]:
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

    access = await account_status(session, user_id)
    if access:
        raise WheelError(access, 403)
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
    row = await _roll(session, user_id, config, service, source=source, request_id=request_id, parent_id=None)
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


async def keep(session: AsyncSession, user_id: int, spin_id: str, service: Any = None) -> dict[str, Any]:
    await _lock_user(session, user_id)
    row = await _pending_for_update(session, user_id, spin_id)
    prize = _row_json(row["prize"])
    try:
        result = await _fulfil(session, prize, user_id, f"spin:{spin_id}", service)
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


async def reroll(session: AsyncSession, user_id: int, spin_id: str, service: Any = None) -> dict[str, Any]:
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
    new = await _roll(
        session, user_id, config, service, source="reroll", request_id=f"reroll-{spin_id}", parent_id=spin_id
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


async def redeem_gift(
    session: AsyncSession, user_id: int, code: str, service: Any = None
) -> dict[str, Any]:
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
    access = await account_status(session, user_id)
    if access:
        raise WheelError(access, 403)
    prize = _row_json(gift_row["prize"])
    try:
        result = await _fulfil(session, prize, user_id, f"gift:{code}", service)
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
