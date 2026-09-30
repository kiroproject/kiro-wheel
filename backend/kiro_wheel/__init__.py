"""KIRO wheel of fortune: free spins, admin-configured prizes with images.

Customer API:  /api/plugins/kiro-wheel/*   (session user, via the Mini App host SDK)
Admin API:     /api/admin/kiro-wheel/*     (Core admin middleware resolves the role)
Spins cannot be bought: they are granted daily, per successful payment, or by an admin.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from sqlalchemy import text

from bot.app.web.context import get_bot, get_bot_username, get_session_factory, get_settings
from bot.app.web.session import extract_authenticated_user_id
from bot.plugins.extensions.contracts import (
    DurableSubscription,
    ExtensionContributions,
    JobHandler,
    OperationContext,
    UserContext,
)
from bot.plugins.spec import WEB_SCOPE_WEBAPP, Plugin, PluginContext

from . import logic
from .logic import (
    WheelError,
    add_bonus_spins,
    anonymize,
    clean_config,
    clean_prize,
    get_pending,
    my_gifts,
    pending_view,
    prize_label,
    public_prize,
    spin,
    spin_state,
)
from .storage import COLOR_KEYS, MIGRATIONS, PLUGIN_ID, list_prizes, load_config, save_config

logger = logging.getLogger(__name__)

USER = f"/api/plugins/{PLUGIN_ID}"
ADMIN = f"/api/admin/{PLUGIN_ID}"
IMAGE_TYPES = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF8": "image/gif",
}
MAX_IMAGE_BYTES = 1024 * 1024


def _ok(payload: dict[str, Any] | None = None, status: int = 200) -> web.Response:
    return web.json_response(
        {"ok": True, **(payload or {})},
        status=status,
        headers={"Cache-Control": "no-store"},
        dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str),
    )


def _err(code: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": code}, status=status)


def _image_type(body: bytes) -> str | None:
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    for magic, content_type in IMAGE_TYPES.items():
        if body.startswith(magic):
            return content_type
    return None


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise WheelError("invalid_json")
    if not isinstance(body, dict):
        raise WheelError("invalid_json")
    return body


def _guard(handler):
    async def wrapped(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except WheelError as exc:
            return _err(exc.code, exc.status)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("kiro-wheel: %s %s failed", request.method, request.path)
            return _err("internal_error", 500)

    return wrapped


# ---------------------------------------------------------------- customer


def _user_id(request: web.Request) -> int:
    user_id = extract_authenticated_user_id(request)
    if not user_id:
        raise WheelError("unauthorized", 401)
    return int(user_id)


STATUS_LABELS = {
    "pending": "ожидает решения",
    "claimed": "получен",
    "declined": "перекручен",
    "gifted": "подарен, ждёт друга",
    "transferred": "подарен другу",
}


GIFT_START_PREFIX = "wg_"


def _gift_link(request: web.Request, code: str) -> str:
    """t.me deep link that opens the Mini App; the wheel's home card routes it to the gift."""
    username = (get_bot_username(request) or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}?startapp={GIFT_START_PREFIX}{code}"
    settings = get_settings(request)
    base = str(getattr(settings, "SUBSCRIPTION_MINI_APP_URL", "") or "").rstrip("/")
    return f"{base}/extensions/{PLUGIN_ID}/wheel?wheel_gift={code}" if base else code


def _theme(config: dict[str, Any]) -> dict[str, Any]:
    theme = {key: config[key] for key in COLOR_KEYS}
    theme["tile_enabled"] = bool(config["tile_enabled"])
    return theme


async def _state_payload(session: Any, user_id: int) -> dict[str, Any]:
    config = await load_config(session)
    prizes = [public_prize(p) for p in await list_prizes(session, include_disabled=False)]
    state = await spin_state(session, user_id, config)
    pending = await get_pending(session, user_id)
    history = (
        await session.execute(
            text(
                "select prize, result, status, created_at from ext_kiro_wheel_spins where user_id = :u "
                "and status <> 'pending' order by created_at desc limit 10"
            ),
            {"u": user_id},
        )
    ).mappings().all()
    feed_rows = []
    if config["show_feed"] and config["enabled"]:
        feed_rows = (
            await session.execute(
                text(
                    "select s.prize, s.created_at, u.username, u.first_name from ext_kiro_wheel_spins s "
                    "join users u on u.user_id = s.user_id "
                    "where coalesce(s.prize->>'kind', '') <> 'nothing' and s.status in ('claimed', 'transferred') "
                    "order by s.created_at desc limit 8"
                )
            )
        ).mappings().all()
    feed = []
    for row in feed_rows:
        prize = logic._row_json(row["prize"])
        feed.append(
            {
                "name": anonymize(row["username"] or row["first_name"] or ""),
                "prize": prize.get("title"),
                "badge": prize.get("badge") or prize.get("label"),
                "at": row["created_at"].isoformat(),
            }
        )
    return {
        "theme": _theme(config),
        "feed": feed,
        "title": config["title"],
        "subtitle": config["subtitle"],
        "enabled": config["enabled"],
        "prizes": prizes if config["enabled"] else [],
        "state": state,
        "pending": pending_view(pending, config) if pending else None,
        "gifts": await my_gifts(session, user_id),
        "rules": {
            "daily_free_spins": config["daily_free_spins"],
            "spins_per_payment": config["spins_per_payment"],
            "require_active_subscription": config["require_active_subscription"],
            "allow_gift": config["allow_gift"],
            "allow_reroll": config["allow_reroll"],
            "gift_ttl_days": config["gift_ttl_days"],
        },
        "history": [
            {
                "prize": logic._row_json(row["prize"]),
                "result": logic._row_json(row["result"]),
                "status": row["status"],
                "status_label": STATUS_LABELS.get(row["status"], row["status"]),
                "at": row["created_at"].isoformat(),
            }
            for row in history
        ],
    }


async def user_state(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    async with get_session_factory(request)() as session:
        payload = await _state_payload(session, user_id)
    for gift_row in payload["gifts"]:
        gift_row["link"] = _gift_link(request, gift_row["code"])
    return _ok(payload)


async def _fresh_state(request: web.Request, user_id: int) -> dict[str, Any]:
    async with get_session_factory(request)() as session:
        config = await load_config(session)
        return await spin_state(session, user_id, config)


async def user_spin(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    async with get_session_factory(request)() as session:
        outcome = await spin(session, user_id, str(body.get("request_id") or ""))
        await session.commit()
    return _ok({"pending": outcome, "state": await _fresh_state(request, user_id)})


async def user_resolve(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    spin_id = str(body.get("spin_id") or "")
    action = str(body.get("action") or "")
    if not spin_id.isalnum() or len(spin_id) != 32:
        raise WheelError("spin_not_found", 404)
    async with get_session_factory(request)() as session:
        if action == "keep":
            outcome = await logic.keep(session, user_id, spin_id)
        elif action == "gift":
            outcome = await logic.gift(session, user_id, spin_id)
            outcome["link"] = _gift_link(request, outcome["code"])
        elif action == "reroll":
            outcome = {"pending": await logic.reroll(session, user_id, spin_id)}
        else:
            raise WheelError("invalid_action")
        await session.commit()
    if action == "keep" and outcome["result"].get("manual"):
        await _notify_manual(request, user_id, outcome["prize"], spin_id)
    return _ok({**outcome, "state": await _fresh_state(request, user_id)})


async def user_gift_cancel(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    async with get_session_factory(request)() as session:
        pending = await logic.cancel_gift(session, user_id, str(body.get("code") or ""))
        await session.commit()
    return _ok({"pending": pending})


async def user_gift_redeem(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    async with get_session_factory(request)() as session:
        outcome = await logic.redeem_gift(session, user_id, str(body.get("code") or ""))
        await session.commit()
    if outcome["result"].get("manual"):
        await _notify_manual(request, user_id, outcome["prize"], "gift")
    await _notify_giver(request, outcome["from_user"], outcome["prize"])
    return _ok({"prize": outcome["prize"], "result": outcome["result"]})


async def _notify_giver(request: web.Request, giver_id: int, prize: dict[str, Any]) -> None:
    try:
        async with get_session_factory(request)() as session:
            telegram_id = await session.scalar(
                text("select telegram_id from users where user_id = :u"), {"u": giver_id}
            )
        if telegram_id:
            await get_bot(request).send_message(
                int(telegram_id), f"🎁 Ваш друг забрал подарок из Колеса удачи: {prize.get('title')}"
            )
    except Exception:  # noqa: BLE001
        logger.warning("kiro-wheel: giver notification skipped")


def _spins_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "бонусное вращение"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "бонусных вращения"
    return "бонусных вращений"


async def _send_user_message(bot: Any, settings: Any, telegram_id: int, message: str) -> None:
    if bot is not None:
        await bot.send_message(telegram_id, message, disable_web_page_preview=True)
        return
    token = str(getattr(settings, "BOT_TOKEN", "") or "")
    if not token:
        raise RuntimeError("bot_unavailable")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        async with http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": telegram_id, "text": message, "disable_web_page_preview": True},
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"telegram_http_{response.status}")


async def _notify_spins(
    session: Any, bot: Any, settings: Any, user_id: int, spins: int, reason: str
) -> None:
    """Tell the player about new bonus spins; a delivery failure never undoes the grant."""
    try:
        telegram_id = await session.scalar(
            text("select telegram_id from users where user_id = :u"), {"u": user_id}
        )
        if not telegram_id:
            return
        head = "🎡 Спасибо за оплату!" if reason == "payment" else "🎁 Вам подарок!"
        message = (
            f"{head} Вам начислено {spins} {_spins_word(spins)} в Колесе удачи.\n"
            "Откройте приложение и крутите колесо: выигрыши ждут в разделе «Колесо удачи»."
        )
        await _send_user_message(bot, settings, int(telegram_id), message)
    except Exception:  # noqa: BLE001
        logger.warning("kiro-wheel: bonus spins notification skipped")


async def user_image(request: web.Request) -> web.Response:
    image_id = request.match_info["image_id"]
    if not image_id.isalnum() or len(image_id) > 64:
        raise web.HTTPNotFound()
    async with get_session_factory(request)() as session:
        row = (
            await session.execute(
                text("select content_type, body from ext_kiro_wheel_images where id = :id"),
                {"id": image_id},
            )
        ).first()
    if row is None:
        raise web.HTTPNotFound()
    return web.Response(
        body=bytes(row[1]),
        content_type=row[0],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _notify_manual(request: web.Request, user_id: int, prize: dict[str, Any], ref: str) -> None:
    try:
        async with get_session_factory(request)() as session:
            config = await load_config(session)
            user = (
                await session.execute(
                    text("select telegram_id, username, first_name from users where user_id = :u"),
                    {"u": user_id},
                )
            ).mappings().first()
        if not config["notify_admins_on_manual"] or user is None:
            return
        bot = get_bot(request)
        settings = get_settings(request)
        name = user["first_name"] or user["username"] or str(user_id)
        link = f'<a href="tg://user?id={int(user["telegram_id"])}">{name}</a>' if user["telegram_id"] else name
        message = (
            "🎡 <b>Колесо удачи: нужен ручной приз</b>\n\n"
            f"Приз: <b>{prize.get('title')}</b>\n"
            f"Пользователь: {link} (ID <code>{user_id}</code>)\n"
            f"<i>#wheel{ref[:8]}</i>"
        )
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(int(admin_id), message, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                logger.warning("kiro-wheel: admin notification failed")
    except Exception:  # noqa: BLE001
        logger.warning("kiro-wheel: manual prize notification skipped")


# ---------------------------------------------------------------- admin


def _admin(request: web.Request) -> None:
    if not request.get("admin_authorized", False):
        raise WheelError("forbidden", 403)


async def admin_overview(request: web.Request) -> web.Response:
    _admin(request)
    async with get_session_factory(request)() as session:
        config = await load_config(session)
        prizes = await list_prizes(session, include_disabled=True)
        stats = (
            await session.execute(
                text(
                    "select count(*) filter (where created_at >= now() - interval '1 day') as day, "
                    "count(*) filter (where created_at >= now() - interval '30 days') as month, "
                    "count(*) as total, count(distinct user_id) as players from ext_kiro_wheel_spins"
                )
            )
        ).mappings().first()
        per_prize = {
            row[0]: int(row[1])
            for row in (
                await session.execute(
                    text(
                        "select prize_id, count(*) from ext_kiro_wheel_spins "
                        "where created_at >= now() - interval '30 days' group by prize_id"
                    )
                )
            ).all()
        }
        recent = (
            await session.execute(
                text(
                    "select s.created_at, s.user_id, s.source, s.status, s.prize, s.result, u.username, u.first_name "
                    "from ext_kiro_wheel_spins s left join users u on u.user_id = s.user_id "
                    "order by s.created_at desc limit 30"
                )
            )
        ).mappings().all()
    total_weight = sum(p["weight"] for p in prizes if p["enabled"] and (p["stock"] is None or p["stock"] > 0))
    return _ok(
        {
            "config": config,
            "prizes": [
                {
                    **p,
                    "label": prize_label(p),
                    "image": f"{USER}/img/{p['image_id']}" if p["image_id"] else None,
                    "chance": (
                        round(100 * p["weight"] / total_weight, 2)
                        if total_weight and p["enabled"] and (p["stock"] is None or p["stock"] > 0)
                        else 0
                    ),
                    "won_30d": per_prize.get(p["id"], 0),
                }
                for p in prizes
            ],
            "stats": dict(stats) if stats else {},
            "recent": [
                {
                    "at": row["created_at"].isoformat(),
                    "user_id": row["user_id"],
                    "user": row["first_name"] or row["username"] or "",
                    "source": row["source"],
                    "status": STATUS_LABELS.get(row["status"], row["status"]),
                    "prize": (row["prize"] if isinstance(row["prize"], dict) else json.loads(row["prize"])).get("title"),
                    "code": (row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])).get("code"),
                }
                for row in recent
            ],
        }
    )


async def admin_save_config(request: web.Request) -> web.Response:
    _admin(request)
    body = await _json_body(request)
    async with get_session_factory(request)() as session:
        data = clean_config(body, await load_config(session))
        await save_config(session, data)
        await session.commit()
    return _ok({"config": data})


async def admin_create_prize(request: web.Request) -> web.Response:
    _admin(request)
    prize = clean_prize(await _json_body(request))
    async with get_session_factory(request)() as session:
        prize_id = await session.scalar(
            text(
                "insert into ext_kiro_wheel_prizes (title, description, kind, params, weight, stock, "
                "color, image_id, enabled, position) values (:title, :description, :kind, "
                "cast(:params as jsonb), :weight, :stock, :color, :image_id, :enabled, :position) returning id"
            ),
            {**prize, "params": json.dumps(prize["params"], ensure_ascii=False)},
        )
        await session.commit()
    return _ok({"id": prize_id})


async def admin_update_prize(request: web.Request) -> web.Response:
    _admin(request)
    prize_id = int(request.match_info["prize_id"])
    prize = clean_prize(await _json_body(request))
    async with get_session_factory(request)() as session:
        updated = await session.execute(
            text(
                "update ext_kiro_wheel_prizes set title = :title, description = :description, kind = :kind, "
                "params = cast(:params as jsonb), weight = :weight, stock = :stock, color = :color, "
                "image_id = :image_id, enabled = :enabled, position = :position, updated_at = now() "
                "where id = :id and deleted_at is null"
            ),
            {**prize, "params": json.dumps(prize["params"], ensure_ascii=False), "id": prize_id},
        )
        await session.commit()
    if updated.rowcount != 1:
        return _err("prize_not_found", 404)
    return _ok({"id": prize_id})


async def admin_delete_prize(request: web.Request) -> web.Response:
    _admin(request)
    prize_id = int(request.match_info["prize_id"])
    async with get_session_factory(request)() as session:
        await session.execute(
            text("update ext_kiro_wheel_prizes set deleted_at = now(), enabled = false where id = :id"),
            {"id": prize_id},
        )
        await session.commit()
    return _ok()


async def admin_upload_image(request: web.Request) -> web.Response:
    _admin(request)
    body = await _json_body(request)
    try:
        raw = base64.b64decode(str(body.get("data") or ""), validate=True)
    except ValueError:
        raise WheelError("invalid_image")
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise WheelError("image_too_large")
    content_type = _image_type(raw)
    if content_type is None:
        raise WheelError("unsupported_image")
    image_id = hashlib.sha256(raw).hexdigest()[:40]
    async with get_session_factory(request)() as session:
        await session.execute(
            text(
                "insert into ext_kiro_wheel_images (id, content_type, body) values (:id, :t, :b) "
                "on conflict (id) do nothing"
            ),
            {"id": image_id, "t": content_type, "b": raw},
        )
        await session.commit()
    return _ok({"image_id": image_id, "url": f"{USER}/img/{image_id}"})


async def admin_grant_spins(request: web.Request) -> web.Response:
    _admin(request)
    body = await _json_body(request)
    ref = str(body.get("user") or "").strip()
    spins = int(body.get("spins") or 0)
    if not 1 <= spins <= 100:
        raise WheelError("invalid_spins")
    async with get_session_factory(request)() as session:
        if ref.startswith("ms_"):
            user_id = await session.scalar(text("select user_id from users where minishop_id = :r"), {"r": ref})
        elif ref.lstrip("@") and not ref.lstrip("-").isdigit():
            user_id = await session.scalar(
                text("select user_id from users where lower(username) = lower(:r)"), {"r": ref.lstrip("@")}
            )
        else:
            user_id = await session.scalar(
                text("select user_id from users where user_id = :r or telegram_id = :r limit 1"),
                {"r": int(ref or 0)},
            )
        if user_id is None:
            raise WheelError("user_not_found", 404)
        config = await load_config(session)
        key = f"admin:{datetime.now(UTC).timestamp()}:{user_id}"
        await add_bonus_spins(
            session, int(user_id), spins, key=key, reason="admin", cap=int(config["max_bonus_spins"])
        )
        await session.commit()
        await _notify_spins(session, get_bot(request), get_settings(request), int(user_id), spins, "admin")
    return _ok({"user_id": user_id, "spins": spins})


# ---------------------------------------------------------------- payments -> bonus spins


async def _on_payment(op: OperationContext, payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    if str(event.get("funding_source") or "external") != "external" or float(event.get("amount") or 0) <= 0:
        return {"skipped": "not_external"}
    if str(event.get("sale_mode") or "").startswith("extension|"):
        return {"skipped": "extension_order"}
    user_id = int(event.get("user_id") or 0)
    payment_id = int(event.get("payment_db_id") or 0)
    if not user_id or not payment_id:
        return {"skipped": "invalid_event"}
    async with op.runtime.require_session_factory()() as session:
        config = await load_config(session)
        spins = int(config["spins_per_payment"])
        if not config["enabled"] or spins <= 0:
            return {"skipped": "disabled"}
        added = await add_bonus_spins(
            session,
            user_id,
            spins,
            key=f"payment:{payment_id}",
            reason="payment",
            cap=int(config["max_bonus_spins"]),
        )
        await session.commit()
        if added:
            await _notify_spins(
                session, getattr(op.runtime, "bot", None), op.runtime.settings, user_id, spins, "payment"
            )
    return {"added": spins if added else 0}


async def _view_policy(context: UserContext, view_id: str) -> bool:
    config = await load_config(context.session)
    return bool(config["enabled"])


class KiroWheelPlugin(Plugin):
    name = PLUGIN_ID
    version = "1.4.0"
    plugin_api_min_version = 1
    plugin_api_max_version = 1

    def migrations(self):
        return MIGRATIONS

    def extensions(self, ctx: PluginContext) -> ExtensionContributions:
        return ExtensionContributions(
            jobs=(JobHandler(id="payment-spins", run=_on_payment, timeout_seconds=30, max_attempts=10),),
            events=(DurableSubscription(event="payment.succeeded", job="payment-spins"),),
            view_policy=_view_policy,
            permissions=frozenset({"rewards.days", "rewards.traffic", "rewards.balance"}),
        )

    def setup_web(self, ctx: PluginContext, app: web.Application, *, scope: str) -> None:
        if scope != WEB_SCOPE_WEBAPP:
            return
        router = app.router
        router.add_get(f"{USER}/state", _guard(user_state))
        router.add_post(f"{USER}/spin", _guard(user_spin))
        router.add_post(f"{USER}/resolve", _guard(user_resolve))
        router.add_post(f"{USER}/gift/cancel", _guard(user_gift_cancel))
        router.add_post(f"{USER}/gift/redeem", _guard(user_gift_redeem))
        router.add_get(USER + "/img/{image_id}", _guard(user_image))
        router.add_get(f"{ADMIN}/overview", _guard(admin_overview))
        router.add_put(f"{ADMIN}/config", _guard(admin_save_config))
        router.add_post(f"{ADMIN}/prizes", _guard(admin_create_prize))
        router.add_put(ADMIN + "/prizes/{prize_id:\\d+}", _guard(admin_update_prize))
        router.add_delete(ADMIN + "/prizes/{prize_id:\\d+}", _guard(admin_delete_prize))
        router.add_post(f"{ADMIN}/images", _guard(admin_upload_image))
        router.add_post(f"{ADMIN}/spins", _guard(admin_grant_spins))

    def locales_dir(self) -> Path | None:
        path = Path(__file__).resolve().parents[2] / "locales"
        return path if path.is_dir() else None


plugin = KiroWheelPlugin()
