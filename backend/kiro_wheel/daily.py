"""Daily rewards calendar: a 7-day streak that pays bonus spins ("tickets").

Rules: one reward per local day; coming back on the next day continues the streak, skipping a day
starts over from day 1; after day 7 the cycle begins again. A "day" follows the wheel's
`day_offset_hours` setting, the same way the free daily spin does.

The module only needs SQLAlchemy: the grant itself is injected, so the logic is testable without Core.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DAYS = 7
DEFAULT_REWARDS = [1, 1, 2, 2, 3, 3, 5]
MAX_REWARD = 100

Grant = Callable[..., Awaitable[bool]]


class DailyError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def clean_rewards(raw: Any) -> list[int]:
    """Seven non-negative whole numbers; anything else is a configuration error."""
    if not isinstance(raw, list) or len(raw) != DAYS:
        raise DailyError("invalid_daily_rewards")
    out = []
    for value in raw:
        try:
            number = int(float(value))
        except (TypeError, ValueError) as exc:
            raise DailyError("invalid_daily_rewards") from exc
        if not 0 <= number <= MAX_REWARD:
            raise DailyError("invalid_daily_rewards")
        out.append(number)
    return out


def rewards_of(config: dict[str, Any]) -> list[int]:
    try:
        return clean_rewards(config.get("daily_rewards"))
    except DailyError:
        return list(DEFAULT_REWARDS)


def local_day(now: datetime, offset_hours: int) -> date:
    return (now + timedelta(hours=offset_hours)).date()


def next_day_start(now: datetime, offset_hours: int) -> datetime:
    local = now + timedelta(hours=offset_hours)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return (start - timedelta(hours=offset_hours)).replace(tzinfo=UTC)


def compute(config: dict[str, Any], row: dict[str, Any] | None, now: datetime, *, subscribed: bool = True) -> dict[str, Any]:
    """The calendar as the player sees it. `row` is the stored streak (None = never claimed)."""
    offset = int(config["day_offset_hours"])
    today = local_day(now, offset)
    rewards = rewards_of(config)
    streak = int(row["streak"]) if row else 0
    last = row["last_day"] if row else None
    claimed_today = last == today
    continuing = last == today - timedelta(days=1)
    if claimed_today:
        claimed = streak
        next_idx = streak % DAYS + 1
    elif continuing and streak < DAYS:
        claimed = streak
        next_idx = streak + 1
    else:
        claimed = 0
        next_idx = 1
    broken = bool(row) and not claimed_today and not continuing and streak > 0
    reason = None
    if not config.get("daily_enabled"):
        reason = "disabled"
    elif config.get("require_active_subscription") and not subscribed:
        reason = "subscription_required"
    elif claimed_today:
        reason = "claimed_today"
    can_claim = reason is None
    days = []
    for i in range(1, DAYS + 1):
        status = "claimed" if i <= claimed else "current" if (i == next_idx and can_claim) else "locked"
        days.append({"day": i, "tickets": rewards[i - 1], "status": status})
    return {
        "enabled": bool(config.get("daily_enabled")),
        "can_claim": can_claim,
        "reason": reason,
        "streak": claimed,
        "next_day": next_idx,
        "today_tickets": rewards[next_idx - 1] if can_claim else 0,
        "days": days,
        "broken": broken,
        "next_at": next_day_start(now, offset).isoformat(),
    }


async def _subscribed(session: AsyncSession, user_id: int) -> bool:
    return bool(
        await session.scalar(
            text("select 1 from subscriptions where user_id = :u and is_active and end_date > now() limit 1"), {"u": user_id}
        )
    )


async def _row(session: AsyncSession, user_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    suffix = " for update" if lock else ""
    r = (
        await session.execute(
            text(f"select streak, last_day from ext_kiro_wheel_daily where user_id = :u{suffix}"), {"u": user_id}
        )
    ).mappings().first()
    return dict(r) if r else None


async def get_state(session: AsyncSession, user_id: int, config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if not config.get("daily_enabled"):
        return compute(config, None, now)
    return compute(config, await _row(session, user_id), now, subscribed=await _subscribed(session, user_id))


async def claim(
    session: AsyncSession, user_id: int, config: dict[str, Any], grant: Grant, *, now: datetime | None = None
) -> dict[str, Any]:
    """Pay today's reward once. Concurrent taps are serialised and the grant itself is idempotent per day."""
    now = now or datetime.now(UTC)
    await session.execute(
        text("select pg_advisory_xact_lock(hashtext('kiro-wheel-daily'), cast(cast(:u as bigint) % 2147483647 as integer))"),
        {"u": user_id},
    )
    state = compute(config, await _row(session, user_id, lock=True), now, subscribed=await _subscribed(session, user_id))
    if not state["can_claim"]:
        raise DailyError(state["reason"] or "not_available", 409)
    idx, tickets = state["next_day"], state["today_tickets"]
    today = local_day(now, int(config["day_offset_hours"]))
    if tickets > 0:
        await grant(session, user_id, tickets, key=f"daily:{user_id}:{today.isoformat()}", reason="daily", cap=int(config["max_bonus_spins"]))
    await session.execute(
        text(
            "insert into ext_kiro_wheel_daily (user_id, streak, last_day, total_tickets, claims, updated_at) "
            "values (:u, :s, :d, :t, 1, now()) on conflict (user_id) do update set streak = :s, last_day = :d, "
            "total_tickets = ext_kiro_wheel_daily.total_tickets + :t, claims = ext_kiro_wheel_daily.claims + 1, updated_at = now()"
        ),
        {"u": user_id, "s": idx, "d": today, "t": tickets},
    )
    after = compute(config, {"streak": idx, "last_day": today}, now, subscribed=True)
    return {"granted": tickets, "day": idx, "daily": after}
