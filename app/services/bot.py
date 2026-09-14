"""Двусторонний Telegram-бот-пульт: команды и кнопки управления с телефона.

Работает на long-polling (getUpdates) — не нужен публичный URL/вебхук, годится
за NAT/VPN. Реагирует только на сообщения из настроенного TELEGRAM_CHAT_ID.

Команды: /status, /devices, /round (коллаж всех камер), /cam <имя>.
Кнопки под алертами устройства: перезагрузка / синхронизация времени.
"""
from __future__ import annotations

from app.services import timesync

import asyncio
import io
import logging

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.drivers import build_client
from app.drivers.base import NVRError
from app.models import Channel, ChannelState, Device
from app.services import poller, telegram

log = logging.getLogger(__name__)
_API = "https://api.telegram.org"

_HELP = (
    "🎛 <b>NVR Monitor — пульт</b>\n"
    "/status — сводка по парку\n"
    "/devices — список устройств\n"
    "/round — коллаж кадров со всех камер\n"
    "/cam &lt;имя&gt; — кадр камеры по имени\n"
)


# ── Низкоуровневые вызовы Telegram ─────────────────────────────────────────────
async def _api(method: str, http: httpx.AsyncClient, **payload):
    url = f"{_API}/bot{settings.telegram_bot_token}/{method}"
    r = await http.post(url, json=payload)
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def _allowed(chat_id) -> bool:
    return str(chat_id) == str(settings.telegram_chat_id)


# ── Сборка контента ────────────────────────────────────────────────────────────
async def build_status_text() -> str:
    async with SessionLocal() as session:
        devices = (await session.execute(select(Device))).scalars().all()
        total = len(devices)
        online = sum(1 for d in devices if d.reachable and d.enabled)
        problems = [d for d in devices if d.enabled and not d.reachable]
        down = (
            await session.execute(
                select(Channel).where(Channel.status != ChannelState.ONLINE, Channel.enabled.is_(True))
            )
        ).scalars().all()
    lines = [
        f"📊 <b>Парк:</b> всего {total}, онлайн {online}, недоступны {len(problems)}",
        f"📷 Каналов не в норме: {len(down)}",
    ]
    if problems:
        lines.append("\n<b>Недоступны:</b>")
        lines += [f"• {telegram.esc(d.name)} ({d.host})" for d in problems[:20]]
    return "\n".join(lines)


async def build_devices_text() -> str:
    async with SessionLocal() as session:
        devices = (await session.execute(select(Device).order_by(Device.name))).scalars().all()
    if not devices:
        return "Устройств нет."
    return "<b>Устройства:</b>\n" + "\n".join(
        f"#{d.id} {telegram.esc(d.name)} — {d.host} "
        f"{'🟢' if d.reachable else '🔴'}" for d in devices
    )


def make_collage(items: list[tuple[str, bytes]]) -> bytes | None:
    """Собирает кадры в одну картинку-сетку с подписями."""
    from PIL import Image, ImageDraw

    if not items:
        return None
    import math

    cell_w, cell_h = 320, 200
    cols = min(4, max(1, int(math.ceil(math.sqrt(len(items))))))
    rows = int(math.ceil(len(items) / cols))
    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (16, 20, 25))
    draw = ImageDraw.Draw(canvas)
    for i, (label, data) in enumerate(items):
        x = (i % cols) * cell_w
        y = (i // cols) * cell_h
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB").resize((cell_w, cell_h - 18))
            canvas.paste(img, (x, y))
        except Exception:  # noqa: BLE001
            draw.rectangle([x, y, x + cell_w, y + cell_h - 18], fill=(40, 0, 0))
        draw.text((x + 4, y + cell_h - 16), label[:40], fill=(255, 255, 255))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


async def _snapshot(device: Device, channel_id: int) -> bytes | None:
    try:
        return await build_client(device, semaphore=poller._semaphore).get_snapshot(channel_id)
    except NVRError:
        return None


async def collect_collage(limit: int = 24) -> bytes | None:
    async with SessionLocal() as session:
        devices = (
            await session.execute(
                select(Device).where(Device.enabled.is_(True))
            )
        ).scalars().all()
        targets = []
        for d in devices:
            chans = (
                await session.execute(
                    select(Channel).where(
                        Channel.device_id == d.id,
                        Channel.enabled.is_(True),
                        Channel.status == ChannelState.ONLINE,
                    )
                )
            ).scalars().all()
            for c in chans:
                targets.append((d, c.channel_id, c.name or f"к{c.channel_id}"))
    targets = targets[:limit]
    items: list[tuple[str, bytes]] = []
    for d, cid, name in targets:
        data = await _snapshot(d, cid)
        if data:
            items.append((f"{d.name}:{name}", data))
    return make_collage(items)


async def find_camera(query: str):
    """Ищет канал по имени (подстрока). Возвращает (device, channel_id, name)|None."""
    q = query.strip().lower()
    if not q:
        return None
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Channel, Device).join(Device, Channel.device_id == Device.id)
            )
        ).all()
    for ch, dev in rows:
        if ch.name and q in ch.name.lower():
            return dev, ch.channel_id, ch.name
    return None


# ── Обработка апдейтов ─────────────────────────────────────────────────────────
async def _handle_message(http: httpx.AsyncClient, msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    if not _allowed(chat_id):
        return
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lstrip("/").split("@")[0].lower()

    if cmd in ("start", "help"):
        await telegram.send_message(_HELP)
    elif cmd in ("status", "статус"):
        await telegram.send_message(await build_status_text())
    elif cmd in ("devices", "устройства"):
        await telegram.send_message(await build_devices_text())
    elif cmd in ("round", "обход"):
        await telegram.send_message("Собираю кадры…")
        collage = await collect_collage()
        if collage:
            await telegram.send_photo(collage, caption="🖼 Обход камер")
        else:
            await telegram.send_message("Нет доступных кадров.")
    elif cmd in ("cam", "камера"):
        found = await find_camera(arg)
        if not found:
            await telegram.send_message("Камера не найдена. Укажи часть имени: /cam фасад")
            return
        dev, cid, name = found
        data = await _snapshot(dev, cid)
        if data:
            await telegram.send_photo(data, caption=f"📷 {telegram.esc(dev.name)}: {telegram.esc(name)}")
        else:
            await telegram.send_message("Кадр недоступен.")
    else:
        await telegram.send_message(_HELP)


async def _handle_callback(http: httpx.AsyncClient, cb: dict) -> None:
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    data = cb.get("data", "")
    cb_id = cb.get("id")
    if not _allowed(chat_id):
        await _api("answerCallbackQuery", http, callback_query_id=cb_id, text="Нет доступа")
        return
    action, _, sid = data.partition(":")
    try:
        device_id = int(sid)
    except ValueError:
        return
    async with SessionLocal() as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one_or_none()
    if device is None:
        await _api("answerCallbackQuery", http, callback_query_id=cb_id, text="Устройство не найдено")
        return
    try:
        if action == "rb":
            await build_client(device).reboot()
            note = f"🔄 {device.name}: команда перезагрузки отправлена"
        elif action == "st":
            await timesync.sync_device(build_client(device))
            note = f"🕐 {device.name}: время синхронизировано"
        else:
            note = "Неизвестная команда"
    except NVRError as exc:
        note = f"Ошибка: {exc}"
    await _api("answerCallbackQuery", http, callback_query_id=cb_id, text=note[:200])
    await telegram.send_message(note)


async def run_bot() -> None:
    """Главный цикл long-polling. Тихо выходит, если бот не настроен."""
    if not (settings.telegram_bot_enabled and telegram.is_configured()):
        log.info("Telegram-бот выключен (нет токена/chat_id или TELEGRAM_BOT_ENABLED=false)")
        return
    log.info("Telegram-бот запущен (long-polling)")
    offset = 0
    warned_unreachable = False
    async with telegram.make_client(httpx.Timeout(40.0, connect=10.0)) as http:
        while True:
            try:
                resp = await _api("getUpdates", http, offset=offset, timeout=25)
                if not resp.get("ok", True):
                    log.error("Telegram вернул ошибку: %s", resp)
                    await asyncio.sleep(10)
                    continue
                warned_unreachable = False
                for upd in resp.get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        await _handle_message(http, upd["message"])
                    elif "callback_query" in upd:
                        await _handle_callback(http, upd["callback_query"])
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                # Долгий poll иногда таймаутит штатно — без шума.
                if not warned_unreachable:
                    log.warning(
                        "Бот: нет ответа от api.telegram.org (таймаут). "
                        "Проверьте доступ сервера к Telegram (firewall/блокировка)."
                    )
                    warned_unreachable = True
                await asyncio.sleep(3)
            except httpx.HTTPError as exc:
                log.warning("Бот: сетевая ошибка (%s): %s", type(exc).__name__, exc)
                await asyncio.sleep(5)
            except Exception as exc:  # noqa: BLE001
                log.warning("Ошибка бота (%s): %s", type(exc).__name__, exc)
                await asyncio.sleep(5)
