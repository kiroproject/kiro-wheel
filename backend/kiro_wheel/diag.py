"""Plugin journal: persistent event log that an admin can read in the panel and download for support.

Events live in ext_kiro_wheel_log (bounded: newest MAX_ROWS rows, at most KEEP_DAYS days). Writing never
raises and uses its own short session, so an entry survives even when the request transaction is rolled back.
The downloadable report masks user ids and gift codes by default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import random
import secrets
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("kiro_wheel.journal")

MAX_ROWS = 5000
KEEP_DAYS = 30
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_PY_LEVEL = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
MAX_DETAIL_CHARS = 4000
MAX_STRING = 600

# detail keys that identify a person or carry a redeemable value; masked in reports
USER_KEYS = {"user", "user_id", "from_user", "to_user", "claimed_by", "telegram_id", "giver"}
SECRET_KEYS = {"code", "link", "gift_code"}

# config keys that are safe to put into a report
SAFE_CONFIG_KEYS = (
    "enabled", "daily_enabled", "daily_rewards", "daily_free_spins", "spins_per_payment", "max_bonus_spins",
    "day_offset_hours", "require_active_subscription", "allow_gift", "allow_reroll", "gift_ttl_days",
    "notify_admins_on_manual", "show_feed", "banner_fill", "debug_log",
)

DEBUG = False


def set_debug(value: bool) -> None:
    global DEBUG
    DEBUG = bool(value)


def _clip(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
    if isinstance(value, dict) and depth < 4:
        return {str(k)[:48]: _clip(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)) and depth < 4:
        return [_clip(v, depth + 1) for v in list(value)[:40]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip(str(value), depth + 1)


def pack_detail(detail: dict[str, Any] | None) -> str:
    clipped = _clip(detail or {})
    raw = json.dumps(clipped, ensure_ascii=False, default=str)
    if len(raw) > MAX_DETAIL_CHARS:
        raw = json.dumps({"truncated": raw[:MAX_DETAIL_CHARS]}, ensure_ascii=False)
    return raw


async def record(
    factory: Any,
    level: str,
    event: str,
    *,
    user_id: int | None = None,
    path: str | None = None,
    status: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Store one journal entry. Debug entries are kept only while the 'detailed journal' option is on."""
    level = level if level in LEVELS else "info"
    if level == "debug" and not DEBUG:
        return
    try:
        logger.log(
            _PY_LEVEL[level],
            "kiro-wheel %s uid=%s %s %s %s",
            event,
            user_id,
            path or "",
            status or "",
            pack_detail(detail) if detail else "",
        )
        async with factory() as session:
            await session.execute(
                text(
                    "insert into ext_kiro_wheel_log (level, event, user_id, path, status, detail) "
                    "values (:level, :event, :user_id, :path, :status, cast(:detail as jsonb))"
                ),
                {
                    "level": level,
                    "event": event[:48],
                    "user_id": user_id,
                    "path": (path or None) and path[:160],
                    "status": status,
                    "detail": pack_detail(detail),
                },
            )
            if random.random() < 0.02:
                await trim(session)
            await session.commit()
    except Exception:  # noqa: BLE001 - the journal must never break a request
        logger.warning("kiro-wheel: journal write failed", exc_info=True)


async def trim(session: Any) -> None:
    await session.execute(
        text(
            "delete from ext_kiro_wheel_log where created_at < now() - make_interval(days => :d) "
            "or id <= (select max(id) from ext_kiro_wheel_log) - :n"
        ),
        {"d": KEEP_DAYS, "n": MAX_ROWS},
    )


async def clear(session: Any) -> int:
    done = await session.execute(text("delete from ext_kiro_wheel_log"))
    return int(done.rowcount or 0)


async def fetch(session: Any, *, limit: int = 200, min_level: str | None = None) -> list[dict[str, Any]]:
    floor = LEVELS.get(min_level or "", 0)
    levels = [name for name, rank in LEVELS.items() if rank >= floor]
    rows = (
        await session.execute(
            text(
                "select id, created_at, level, event, user_id, path, status, detail from ext_kiro_wheel_log "
                "where level = any(cast(:levels as text[])) order by id desc limit :n"
            ),
            {"levels": levels, "n": max(1, min(int(limit), MAX_ROWS))},
        )
    ).mappings().all()
    out = []
    for row in rows:
        detail = row["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                detail = {"raw": detail}
        out.append({**dict(row), "detail": detail or {}})
    return out


async def counts(session: Any) -> dict[str, int]:
    rows = (await session.execute(text("select level, count(*) from ext_kiro_wheel_log group by level"))).all()
    return {str(level): int(total) for level, total in rows}


async def collect_header(session: Any, version: str, config: dict[str, Any]) -> dict[str, Any]:
    """Facts about the install that help to read a log: version, runtime, safe settings, row counts."""

    async def scalar(sql: str) -> Any:
        try:
            return await session.scalar(text(sql))
        except Exception:  # noqa: BLE001
            await session.rollback()
            return None

    prizes = (
        await session.execute(
            text(
                "select kind, count(*) as total, count(*) filter (where enabled) as active "
                "from ext_kiro_wheel_prizes where deleted_at is null group by kind order by kind"
            )
        )
    ).mappings().all()
    return {
        "plugin": f"kiro-wheel {version}",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": {key: config.get(key) for key in SAFE_CONFIG_KEYS},
        "banner_set": bool(config.get("banner_image_id")),
        "prizes": {row["kind"]: f"{row['active']}/{row['total']} active" for row in prizes},
        "spins_total": await scalar("select count(*) from ext_kiro_wheel_spins"),
        "spins_24h": await scalar(
            "select count(*) from ext_kiro_wheel_spins where created_at >= now() - interval '1 day'"
        ),
        "players": await scalar("select count(distinct user_id) from ext_kiro_wheel_spins"),
        "pending_prizes": await scalar("select count(*) from ext_kiro_wheel_spins where status = 'pending'"),
        "open_gifts": await scalar("select count(*) from ext_kiro_wheel_gifts where status = 'open'"),
        "daily_players": await scalar("select count(*) from ext_kiro_wheel_daily"),
        "log_rows": await counts(session),
    }


class Masker:
    """Stable per-report pseudonyms: the same user gets the same label inside one file, never across files."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._salt = secrets.token_hex(8)

    def user(self, value: Any) -> str:
        if value is None or value == "":
            return "-"
        if not self.enabled:
            return str(value)
        digest = hashlib.sha256(f"{self._salt}:{value}".encode()).hexdigest()
        return f"u{digest[:8]}"

    def secret(self, value: Any) -> str:
        text_value = str(value)
        if not self.enabled:
            return text_value
        return text_value[:3] + "…" if len(text_value) > 3 else "…"

    def detail(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: self.detail(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.detail(v, key) for v in value]
        if key in USER_KEYS and not isinstance(value, (dict, list)):
            return self.user(value)
        if key in SECRET_KEYS and isinstance(value, str):
            return self.secret(value)
        return value


def format_report(header: dict[str, Any], rows: list[dict[str, Any]], *, mask: bool = True) -> str:
    """Plain-text report, oldest entry first, ready to be attached to a support message."""
    masker = Masker(mask)
    lines = [
        "=== KIRO Wheel journal ===",
        f"privacy: {'user ids and gift codes are masked' if mask else 'RAW user ids and gift codes (do not share publicly)'}",
    ]
    for key, value in header.items():
        rendered = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else value
        lines.append(f"{key}: {rendered}")
    lines.append(f"entries: {len(rows)} (oldest first)")
    lines.append("=" * 26)
    for row in reversed(rows):
        stamp = row["created_at"]
        stamp = stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if hasattr(stamp, "astimezone") else str(stamp)
        where = " ".join(part for part in (row.get("path") or "", str(row.get("status") or "")) if part)
        detail = masker.detail(row.get("detail") or {})
        tail = f" {json.dumps(detail, ensure_ascii=False, default=str)}" if detail else ""
        lines.append(
            f"{stamp}Z {str(row['level']).upper():5} {row['event']} user={masker.user(row.get('user_id'))}"
            f"{' ' + where if where else ''}{tail}"
        )
    return "\n".join(lines) + "\n"
