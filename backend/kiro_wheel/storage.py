"""Tables, defaults and small SQL helpers for the wheel. All tables are plugin-owned."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from db.migrator.engine import Migration

PLUGIN_ID = "kiro-wheel"

PRIZE_KINDS = (
    "nothing",  # "повезёт в следующий раз"
    "days",  # +N дней к активной подписке (rewards.days)
    "traffic",  # +N ГБ обычного трафика (rewards.traffic)
    "balance",  # +N ₽ на внутренний баланс (rewards.balance)
    "discount",  # персональный промокод со скидкой N% на следующую покупку
    "premium",  # персональный промокод на N ГБ Premium-трафика
    "gift_code",  # подарочный код на N дней, который можно передать другу
    "manual",  # приз выдаёт администратор вручную (мерч, подарок и т.п.)
)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "title": "Колесо удачи",
    "subtitle": "Крутите колесо и забирайте подарки",
    "daily_free_spins": 1,
    "require_active_subscription": True,
    "spins_per_payment": 1,
    "max_bonus_spins": 20,
    "day_offset_hours": 3,
    "notify_admins_on_manual": True,
    "show_feed": True,
    "allow_reroll": True,
    "allow_gift": True,
    "gift_ttl_days": 7,
    # appearance
    "bg_from": "#ff3b1d",
    "bg_to": "#f7821b",
    "text_color": "#ffffff",
    "btn_bg": "#ffffff",
    "btn_text": "#1b1b1f",
    "win_from": "#ff3b1d",
    "win_to": "#f7821b",
    "win_text": "#ffffff",
    "tile_enabled": False,
    "banner_image_id": "",
    "banner_fill": False,
    "tile_bg": "#ffffff",
    # daily login calendar: replaces the automatic free spin of the day when enabled
    "daily_enabled": False,
    "daily_rewards": [1, 1, 2, 2, 3, 3, 5],
}

COLOR_KEYS = ("bg_from", "bg_to", "text_color", "btn_bg", "btn_text", "win_from", "win_to", "win_text", "tile_bg")


def _upgrade_0001(connection: Connection) -> None:
    for statement in (
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_config (
            id INTEGER PRIMARY KEY,
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_images (
            id VARCHAR(64) PRIMARY KEY,
            content_type VARCHAR(32) NOT NULL,
            body BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_prizes (
            id SERIAL PRIMARY KEY,
            title VARCHAR(80) NOT NULL,
            description VARCHAR(300) NOT NULL DEFAULT '',
            kind VARCHAR(16) NOT NULL,
            params JSONB NOT NULL DEFAULT '{}'::jsonb,
            weight INTEGER NOT NULL DEFAULT 1,
            stock INTEGER NULL,
            color VARCHAR(16) NOT NULL DEFAULT '',
            image_id VARCHAR(64) NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_spins (
            id VARCHAR(32) PRIMARY KEY,
            user_id BIGINT NOT NULL,
            request_id VARCHAR(64) NOT NULL,
            source VARCHAR(16) NOT NULL,
            prize_id INTEGER NULL,
            prize JSONB NOT NULL,
            result JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_ext_kiro_wheel_spin_request UNIQUE (user_id, request_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_wheel_spins_user ON ext_kiro_wheel_spins (user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_wheel_spins_created ON ext_kiro_wheel_spins (created_at)",
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_credits (
            user_id BIGINT PRIMARY KEY,
            spins INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_grants (
            key VARCHAR(128) PRIMARY KEY,
            user_id BIGINT NOT NULL,
            spins INTEGER NOT NULL,
            reason VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ):
        connection.execute(text(statement))


def _upgrade_0002(connection: Connection) -> None:
    """Pending prizes (keep / gift / reroll) and friend gifts. Old spins stay 'claimed'."""
    for statement in (
        "ALTER TABLE ext_kiro_wheel_spins ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'claimed'",
        "ALTER TABLE ext_kiro_wheel_spins ADD COLUMN IF NOT EXISTS parent_id VARCHAR(32) NULL",
        "ALTER TABLE ext_kiro_wheel_spins ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ NULL",
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_wheel_spins_status ON ext_kiro_wheel_spins (user_id, status)",
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_gifts (
            code VARCHAR(16) PRIMARY KEY,
            spin_id VARCHAR(32) NOT NULL,
            from_user BIGINT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'open',
            claimed_by BIGINT NULL,
            result JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            claimed_at TIMESTAMPTZ NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_wheel_gifts_spin ON ext_kiro_wheel_gifts (spin_id)",
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_wheel_gifts_from ON ext_kiro_wheel_gifts (from_user, status)",
    ):
        connection.execute(text(statement))


def _upgrade_0003(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_wheel_daily (
            user_id BIGINT PRIMARY KEY,
            streak SMALLINT NOT NULL DEFAULT 0,
            last_day DATE,
            total_tickets INTEGER NOT NULL DEFAULT 0,
            claims INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


MIGRATIONS = [
    Migration(
        id=f"{PLUGIN_ID}.0001_initial",
        description="Wheel of fortune: config, prizes, images, spins, bonus spins",
        upgrade=_upgrade_0001,
    ),
    Migration(
        id=f"{PLUGIN_ID}.0002_pending_and_gifts",
        description="Wheel of fortune: pending prizes, rerolls and gifts to friends",
        upgrade=_upgrade_0002,
    ),
    Migration(
        id=f"{PLUGIN_ID}.0003_daily_rewards",
        description="Wheel of fortune: daily login rewards calendar",
        upgrade=_upgrade_0003,
    ),
]


async def load_config(session: AsyncSession) -> dict[str, Any]:
    raw = await session.scalar(text("select data from ext_kiro_wheel_config where id = 1"))
    data = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        data.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    elif isinstance(raw, str):
        data.update({k: v for k, v in json.loads(raw).items() if k in DEFAULT_CONFIG})
    return data


async def save_config(session: AsyncSession, data: dict[str, Any]) -> None:
    await session.execute(
        text(
            "insert into ext_kiro_wheel_config (id, data, updated_at) values (1, cast(:d as jsonb), now()) "
            "on conflict (id) do update set data = excluded.data, updated_at = now()"
        ),
        {"d": json.dumps(data, ensure_ascii=False)},
    )


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value if value is not None else {}


async def list_prizes(session: AsyncSession, *, include_disabled: bool) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "select id, title, description, kind, params, weight, stock, color, image_id, "
                "enabled, position from ext_kiro_wheel_prizes where deleted_at is null "
                + ("" if include_disabled else "and enabled ")
                + "order by position, id"
            )
        )
    ).mappings().all()
    return [{**dict(row), "params": _json(row["params"])} for row in rows]
