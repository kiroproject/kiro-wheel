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
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from sqlalchemy import text

from bot.app.web.context import (
    get_bot,
    get_bot_username,
    get_session_factory,
    get_settings,
    get_subscription_service,
)
from bot.app.web.session import extract_authenticated_user_id
from bot.plugins.extensions.contracts import (
    DurableSubscription,
    ExtensionContributions,
    JobHandler,
    OperationContext,
    UserContext,
)
from bot.plugins.spec import WEB_SCOPE_WEBAPP, Plugin, PluginContext

from . import daily, diag, logic
from .presets import STARTER_PRIZES
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
    tariff_catalog,
    tariffs_of,
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


def _safe_user(request: web.Request) -> int | None:
    try:
        return int(extract_authenticated_user_id(request) or 0) or None
    except Exception:  # noqa: BLE001
        return None


async def _log(
    request: web.Request,
    level: str,
    event: str,
    user_id: int | None = None,
    status: int | None = None,
    **detail: Any,
) -> None:
    """Write one entry to the plugin journal (never raises)."""
    await diag.record(
        get_session_factory(request),
        level,
        event,
        user_id=user_id if user_id is not None else _safe_user(request),
        path=f"{request.method} {request.path}",
        status=status,
        detail=detail,
    )


def _guard(handler):
    async def wrapped(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except WheelError as exc:
            level = "error" if exc.status >= 500 else "warn" if exc.status in (401, 403) else "info"
            cause = repr(exc.__cause__)[:300] if exc.__cause__ is not None else None
            await _log(request, level, "request_failed", status=exc.status, error_code=exc.code, cause=cause)
            return _err(exc.code, exc.status)
        except web.HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("kiro-wheel: %s %s failed", request.method, request.path)
            await _log(
                request, "error", "internal_error", status=500,
                error=repr(exc)[:300], trace=traceback.format_exc()[-3000:],
            )
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
    banner = str(config.get("banner_image_id") or "")
    theme["banner"] = f"{USER}/img/{banner}" if banner else None
    theme["banner_fill"] = bool(config.get("banner_fill"))
    return theme


async def _pending_payload(
    session: Any, user_id: int, pending: dict[str, Any] | None, config: dict[str, Any], service: Any
) -> dict[str, Any] | None:
    if not pending:
        return None
    view = pending_view(pending, config)
    view["hint"] = await logic.decision_hint(session, user_id, view["prize"], config, service)
    return view


async def _state_payload(session: Any, user_id: int, service: Any = None) -> dict[str, Any]:
    config = await load_config(session)
    # The reel shows only what this player can actually win (prize audiences).
    seg = await logic.user_segment(session, user_id, tariffs_of(service) if service is not None else None)
    prizes = [
        public_prize(p)
        for p in await list_prizes(session, include_disabled=False)
        if logic.ineligible_reason(p, seg, config, service) is None
    ]
    state = await spin_state(session, user_id, config)
    pending = await get_pending(session, user_id)
    daily_state = await daily.get_state(session, user_id, config)
    if state.get("access"):
        daily_state = {**daily_state, "can_claim": False, "reason": state["access"]}
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
        "daily": daily_state,
        "pending": await _pending_payload(session, user_id, pending, config, service),
        "gifts": await my_gifts(session, user_id),
        "rules": {
            "daily_free_spins": 0 if config["daily_enabled"] else config["daily_free_spins"],
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
        payload = await _state_payload(session, user_id, get_subscription_service(request))
    for gift_row in payload["gifts"]:
        gift_row["link"] = _gift_link(request, gift_row["code"])
    state = payload["state"]
    daily_view = payload["daily"] or {}
    await _log(
        request, "debug", "state", user_id, 200,
        can_spin=state["can_spin"], reason=state["reason"], access=state.get("access"),
        available=state["available"], pending=bool(payload["pending"]),
        daily_reason=daily_view.get("reason"), daily_can_claim=daily_view.get("can_claim"),
    )
    return _ok(payload)


async def _fresh_state(request: web.Request, user_id: int) -> dict[str, Any]:
    async with get_session_factory(request)() as session:
        config = await load_config(session)
        return await spin_state(session, user_id, config)


async def user_spin(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    service = get_subscription_service(request)
    async with get_session_factory(request)() as session:
        outcome = await spin(session, user_id, str(body.get("request_id") or ""), service)
        outcome["hint"] = await logic.decision_hint(
            session, user_id, outcome["prize"], await load_config(session), service
        )
        await session.commit()
    prize = outcome.get("prize") or {}
    await _log(
        request, "info", "spin", user_id, 200,
        spin_id=outcome.get("spin_id"), source=outcome.get("source"), prize_id=prize.get("id"),
        kind=prize.get("kind"), repeated=bool(outcome.get("repeated")),
    )
    return _ok({"pending": outcome, "state": await _fresh_state(request, user_id)})


async def user_resolve(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    spin_id = str(body.get("spin_id") or "")
    action = str(body.get("action") or "")
    if not spin_id.isalnum() or len(spin_id) != 32:
        raise WheelError("spin_not_found", 404)
    service = get_subscription_service(request)
    async with get_session_factory(request)() as session:
        if action == "keep":
            outcome = await logic.keep(session, user_id, spin_id, service)
        elif action == "gift":
            outcome = await logic.gift(session, user_id, spin_id)
            outcome["link"] = _gift_link(request, outcome["code"])
        elif action == "reroll":
            new = await logic.reroll(session, user_id, spin_id, service)
            new["hint"] = await logic.decision_hint(
                session, user_id, new["prize"], await load_config(session), service
            )
            outcome = {"pending": new}
        else:
            raise WheelError("invalid_action")
        await session.commit()
    result = outcome.get("result") or {}
    await _log(
        request, "info", "resolve", user_id, 200,
        action=action, spin_id=spin_id, kind=(outcome.get("prize") or {}).get("kind"),
        applied=result.get("applied"), manual=bool(result.get("manual")),
    )
    if result.get("converted_trial"):
        await _log_conversion(request, user_id, outcome["prize"], result, spin_id=spin_id)
    if action == "keep" and result.get("manual"):
        await _notify_manual(request, user_id, outcome["prize"], spin_id)
    return _ok({**outcome, "state": await _fresh_state(request, user_id)})


async def _log_conversion(
    request: web.Request, user_id: int, prize: dict[str, Any], result: dict[str, Any], **ref: Any
) -> None:
    """Journal mark for a trial replaced by a paid base tariff because of a wheel prize."""
    await _log(
        request, "info", "trial_converted", user_id, 200,
        prize_id=prize.get("id"), title=prize.get("title"), tariff_key=result.get("tariff_key"),
        days=result.get("days"), ends_at=result.get("ends_at"), subscription_id=result.get("subscription_id"),
        **ref,
    )


async def user_gift_cancel(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    async with get_session_factory(request)() as session:
        pending = await logic.cancel_gift(session, user_id, str(body.get("code") or ""))
        await session.commit()
    await _log(request, "info", "gift_cancel", user_id, 200, code=str(body.get("code") or ""))
    return _ok({"pending": pending})


async def user_gift_redeem(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _json_body(request)
    async with get_session_factory(request)() as session:
        outcome = await logic.redeem_gift(
            session, user_id, str(body.get("code") or ""), get_subscription_service(request)
        )
        await session.commit()
    await _log(
        request, "info", "gift_redeem", user_id, 200,
        code=str(body.get("code") or ""), from_user=outcome.get("from_user"),
        kind=(outcome.get("prize") or {}).get("kind"), applied=outcome["result"].get("applied"),
    )
    if outcome["result"].get("converted_trial"):
        await _log_conversion(
            request, user_id, outcome["prize"], outcome["result"],
            gift_code=str(body.get("code") or ""), from_user=outcome.get("from_user"),
        )
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("kiro-wheel: giver notification skipped")
        await _log(request, "warn", "notify_giver_skipped", giver_id, error=repr(exc)[:200])


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


async def user_daily_claim(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    async with get_session_factory(request)() as session:
        config = await load_config(session)
        if not config["enabled"]:
            raise WheelError("disabled", 409)
        access = await logic.account_status(session, user_id)
        if access:
            raise WheelError(access, 403)
        try:
            result = await daily.claim(session, user_id, config, add_bonus_spins)
        except daily.DailyError as exc:
            raise WheelError(exc.code, exc.status) from exc
        await session.commit()
    fresh = await _fresh_state(request, user_id)
    await _log(
        request, "info", "daily_claim", user_id, 200,
        day=result.get("day"), granted=result.get("granted"), streak=(result.get("daily") or {}).get("streak"),
    )
    return _ok({**result, "state": fresh})


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
            except Exception as exc:  # noqa: BLE001
                logger.warning("kiro-wheel: admin notification failed")
                await _log(request, "warn", "notify_admin_failed", user_id, error=repr(exc)[:200], admin=admin_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kiro-wheel: manual prize notification skipped")
        await _log(request, "warn", "notify_manual_skipped", user_id, error=repr(exc)[:200])


# ---------------------------------------------------------------- admin


def _admin(request: web.Request) -> None:
    if not request.get("admin_authorized", False):
        raise WheelError("forbidden", 403)


def _recent_note(raw: Any) -> str | None:
    result = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    if not result.get("converted_trial"):
        return None
    return "триал заменён тарифом «{}» на {} дн.".format(result.get("tariff_title"), result.get("days"))


async def admin_overview(request: web.Request) -> web.Response:
    _admin(request)
    tariffs = tariff_catalog(tariffs_of(get_settings(request)))
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
                    "audience": logic.prize_audience(p),
                }
                for p in prizes
            ],
            "tariffs": tariffs,
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
                    "note": _recent_note(row["result"]),
                }
                for row in recent
            ],
        }
    )


async def admin_save_config(request: web.Request) -> web.Response:
    _admin(request)
    body = await _json_body(request)
    async with get_session_factory(request)() as session:
        before = await load_config(session)
        data = clean_config(body, before, tariffs_of(get_settings(request)))
        await save_config(session, data)
        await session.commit()
    diag.set_debug(bool(data.get("debug_log")))
    changed = sorted(key for key in data if data[key] != before.get(key))
    await _log(request, "info", "admin_config_saved", status=200, changed=changed, debug_log=data.get("debug_log"))
    return _ok({"config": data})


async def admin_create_prize(request: web.Request) -> web.Response:
    _admin(request)
    prize = clean_prize(await _json_body(request), tariffs_of(get_settings(request)))
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
    await _log(
        request, "info", "admin_prize_created", status=200,
        prize_id=prize_id, title=prize["title"], kind=prize["kind"],
    )
    return _ok({"id": prize_id})


async def admin_update_prize(request: web.Request) -> web.Response:
    _admin(request)
    prize_id = int(request.match_info["prize_id"])
    prize = clean_prize(await _json_body(request), tariffs_of(get_settings(request)))
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
    await _log(
        request, "info", "admin_prize_updated", status=200,
        prize_id=prize_id, title=prize["title"], kind=prize["kind"],
    )
    return _ok({"id": prize_id})


async def admin_tariffs(request: web.Request) -> web.Response:
    """Tariffs from Core's tariffs.json for the audience / trial-conversion pickers."""
    _admin(request)
    config = await _config_for_admin(request)
    return _ok({"tariffs": tariff_catalog(tariffs_of(get_settings(request))), "trial_convert_tariff": config["trial_convert_tariff"]})


async def _config_for_admin(request: web.Request) -> dict[str, Any]:
    async with get_session_factory(request)() as session:
        return await load_config(session)


async def admin_delete_prize(request: web.Request) -> web.Response:
    _admin(request)
    prize_id = int(request.match_info["prize_id"])
    async with get_session_factory(request)() as session:
        await session.execute(
            text("update ext_kiro_wheel_prizes set deleted_at = now(), enabled = false where id = :id"),
            {"id": prize_id},
        )
        await session.commit()
    await _log(request, "info", "admin_prize_deleted", status=200, prize_id=prize_id)
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


async def admin_add_starter_prizes(request: web.Request) -> web.Response:
    """Add the bundled starter set; prizes whose title already exists are left untouched."""
    _admin(request)
    added: list[str] = []
    skipped: list[str] = []
    async with get_session_factory(request)() as session:
        position = int(
            await session.scalar(
                text("select coalesce(max(position), -1) + 1 from ext_kiro_wheel_prizes where deleted_at is null")
            )
            or 0
        )
        for item in STARTER_PRIZES:
            exists = await session.scalar(
                text("select 1 from ext_kiro_wheel_prizes where title = :t and deleted_at is null limit 1"),
                {"t": item["title"]},
            )
            if exists:
                skipped.append(item["title"])
                continue
            raw = base64.b64decode(item["image"])
            image_id = hashlib.sha256(raw).hexdigest()[:40]
            await session.execute(
                text(
                    "insert into ext_kiro_wheel_images (id, content_type, body) values (:id, :t, :b) "
                    "on conflict (id) do nothing"
                ),
                {"id": image_id, "t": _image_type(raw) or "image/webp", "b": raw},
            )
            prize = clean_prize({**item, "image_id": image_id, "position": position})
            await session.execute(
                text(
                    "insert into ext_kiro_wheel_prizes (title, description, kind, params, weight, stock, "
                    "color, image_id, enabled, position) values (:title, :description, :kind, "
                    "cast(:params as jsonb), :weight, :stock, :color, :image_id, :enabled, :position)"
                ),
                {**prize, "params": json.dumps(prize["params"], ensure_ascii=False)},
            )
            position += 1
            added.append(item["title"])
        await session.commit()
    await _log(request, "info", "admin_starter_added", status=200, added=len(added), skipped=len(skipped))
    return _ok({"added": len(added), "skipped": len(skipped)})


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
    await _log(request, "info", "admin_spins_granted", status=200, user_id=int(user_id), spins=spins)
    return _ok({"user_id": user_id, "spins": spins})


async def admin_logs(request: web.Request) -> web.Response:
    _admin(request)
    level = request.query.get("level") or None
    try:
        limit = int(request.query.get("limit") or 100)
    except ValueError:
        limit = 100
    async with get_session_factory(request)() as session:
        config = await load_config(session)
        rows = await diag.fetch(session, limit=limit, min_level=level)
        counts = await diag.counts(session)
    return _ok(
        {
            "debug": bool(config.get("debug_log")),
            "counts": counts,
            "max_rows": diag.MAX_ROWS,
            "rows": [
                {
                    "id": r["id"],
                    "at": r["created_at"].isoformat(),
                    "level": r["level"],
                    "event": r["event"],
                    "user_id": r["user_id"],
                    "path": r["path"],
                    "status": r["status"],
                    "detail": r["detail"],
                }
                for r in rows
            ],
        }
    )


async def admin_logs_download(request: web.Request) -> web.Response:
    _admin(request)
    mask = request.query.get("mask", "1") != "0"
    try:
        limit = int(request.query.get("limit") or diag.MAX_ROWS)
    except ValueError:
        limit = diag.MAX_ROWS
    async with get_session_factory(request)() as session:
        config = await load_config(session)
        header = await diag.collect_header(session, KiroWheelPlugin.version, config)
        rows = await diag.fetch(session, limit=limit)
    report = diag.format_report(header, rows, mask=mask)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    await _log(request, "info", "admin_log_exported", status=200, rows=len(rows), masked=mask)
    return web.Response(
        text=report,
        content_type="text/plain",
        charset="utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="kiro-wheel-log-{stamp}.txt"',
            "Cache-Control": "no-store",
        },
    )


async def admin_logs_clear(request: web.Request) -> web.Response:
    _admin(request)
    async with get_session_factory(request)() as session:
        removed = await diag.clear(session)
        await session.commit()
    await _log(request, "info", "admin_log_cleared", status=200, removed=removed)
    return _ok({"removed": removed})


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
        await diag.record(
            op.runtime.require_session_factory(), "info", "payment_spins", user_id=user_id,
            detail={"payment_id": payment_id, "spins": spins, "added": bool(added)},
        )
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
    version = "1.8.0"
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
        router.add_post(f"{USER}/daily/claim", _guard(user_daily_claim))
        router.add_post(f"{USER}/resolve", _guard(user_resolve))
        router.add_post(f"{USER}/gift/cancel", _guard(user_gift_cancel))
        router.add_post(f"{USER}/gift/redeem", _guard(user_gift_redeem))
        router.add_get(USER + "/img/{image_id}", _guard(user_image))
        router.add_get(f"{ADMIN}/overview", _guard(admin_overview))
        router.add_put(f"{ADMIN}/config", _guard(admin_save_config))
        router.add_get(f"{ADMIN}/tariffs", _guard(admin_tariffs))
        router.add_post(f"{ADMIN}/prizes", _guard(admin_create_prize))
        router.add_put(ADMIN + "/prizes/{prize_id:\\d+}", _guard(admin_update_prize))
        router.add_delete(ADMIN + "/prizes/{prize_id:\\d+}", _guard(admin_delete_prize))
        router.add_post(f"{ADMIN}/images", _guard(admin_upload_image))
        router.add_post(f"{ADMIN}/presets/starter", _guard(admin_add_starter_prizes))
        router.add_post(f"{ADMIN}/spins", _guard(admin_grant_spins))
        router.add_get(f"{ADMIN}/logs", _guard(admin_logs))
        router.add_get(f"{ADMIN}/logs/download", _guard(admin_logs_download))
        router.add_post(f"{ADMIN}/logs/clear", _guard(admin_logs_clear))

    def locales_dir(self) -> Path | None:
        path = Path(__file__).resolve().parents[2] / "locales"
        return path if path.is_dir() else None


plugin = KiroWheelPlugin()
