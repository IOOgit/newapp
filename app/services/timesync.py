"""NTP reference clock and persistent daily recorder synchronization settings."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import struct
import time
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from app.database import SessionLocal
from app.drivers.base import NVRError
from app.models import AppSetting, Device


class TimeSyncSettings(BaseModel):
    server: str = Field(default="", max_length=253)
    port: int = Field(default=123, ge=1, le=65535)
    enabled: bool = False
    time: str = Field(default="03:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "Asia/Vladivostok"

    @field_validator("server")
    @classmethod
    def valid_server(cls, value):
        value = value.strip()
        if value and (any(c.isspace() for c in value) or any(c in value for c in "/\\@?#")):
            raise ValueError("Укажите IP или имя сервера без протокола и пути")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, KeyError):
            raise ValueError("Неизвестный часовой пояс")
        return value

    @model_validator(mode="after")
    def require_server(self):
        if self.enabled and not self.server:
            raise ValueError("Для расписания укажите NTP-сервер")
        return self


async def load_settings() -> TimeSyncSettings:
    async with SessionLocal() as session:
        row = await session.get(AppSetting, "time_sync_settings")
        return TimeSyncSettings.model_validate_json(row.value) if row else TimeSyncSettings()


async def save_json(key, value):
    async with SessionLocal() as session:
        row = await session.get(AppSetting, key)
        encoded = json.dumps(value, ensure_ascii=False)
        if row:
            row.value = encoded
        else:
            session.add(AppSetting(key=key, value=encoded))
        await session.commit()


NTP_EPOCH = 2208988800


def _timestamp(data: bytes, pivot: float) -> float:
    seconds, fraction = struct.unpack("!II", data)
    # Unfold the 32-bit NTP era using the local clock (including dates after 2036).
    seconds += round((pivot + NTP_EPOCH - seconds) / 2**32) * 2**32
    return seconds - NTP_EPOCH + fraction / 2**32


def _query_ntp(server: str, port: int) -> float:
    packet = bytearray(48)
    packet[0] = 0x23  # NTPv4 client
    addresses = socket.getaddrinfo(server, port, type=socket.SOCK_DGRAM)
    last_error = None
    for family, kind, proto, _, address in addresses:
        try:
            with socket.socket(family, kind, proto) as sock:
                sock.settimeout(2)
                sock.connect(address)
                t1 = time.time()
                mono = time.monotonic()
                seconds = t1 + NTP_EPOCH
                packet[40:48] = struct.pack("!II", int(seconds) % 2**32, int((seconds % 1) * 2**32))
                sock.send(packet)
                reply = sock.recv(512)
            elapsed = time.monotonic() - mono
            if len(reply) < 48 or reply[0] & 7 != 4 or (reply[0] >> 3) & 7 not in (3, 4):
                raise ValueError("Некорректный ответ NTP")
            if reply[0] >> 6 == 3 or not 1 <= reply[1] <= 15:
                raise ValueError("NTP-сервер не синхронизирован или отклонил запрос")
            if reply[24:32] != packet[40:48] or reply[32:40] == bytes(8) or reply[40:48] == bytes(8):
                raise ValueError("NTP: ответ не соответствует запросу")
            received = _timestamp(reply[32:40], t1)
            sent = _timestamp(reply[40:48], t1)
            delay = elapsed - (sent - received)
            if sent < received or delay < -0.1 or delay > 5:
                raise ValueError("NTP: недопустимая задержка ответа")
            return sent + max(0, delay) / 2
        except (OSError, ValueError) as exc:
            last_error = exc
    raise ValueError(f"NTP недоступен: {last_error}")


async def reference_time(config: TimeSyncSettings | None = None) -> dt.datetime:
    config = config or await load_settings()
    if not config.server:
        return dt.datetime.now().astimezone()
    try:
        stamp = await asyncio.wait_for(asyncio.to_thread(_query_ntp, config.server, config.port), timeout=8)
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        raise NVRError(f"Не удалось получить время NTP: {exc}") from exc
    return dt.datetime.fromtimestamp(stamp, ZoneInfo(config.timezone))


_run_lock = asyncio.Lock()


async def scheduled_sync():
    from app import crud
    from app.drivers import build_client
    config = await load_settings()
    if not config.enabled or _run_lock.locked():
        return
    async with _run_lock:
        result = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "devices": [], "error": None}
        try:
            reference = await reference_time(config)
            started = time.monotonic()
            async with SessionLocal() as session:
                ids = list((await session.execute(select(Device.id).where(Device.enabled.is_(True)))).scalars())
            for device_id in ids:
                async with SessionLocal() as session:
                    device = await crud.get_device(session, device_id)
                    if device is None or not device.enabled:
                        continue
                    item = {"id": device.id, "name": device.name, "ok": False}
                    try:
                        await build_client(device).sync_time(reference + dt.timedelta(seconds=time.monotonic() - started))
                        item["ok"] = True
                    except Exception as exc:
                        item["error"] = str(exc)
                    item["at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    result["devices"].append(item)
                    await save_json("time_sync_last_run", result)
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            result["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            await save_json("time_sync_last_run", result)


async def sync_device(client):
    """Shared reference for manual, bulk and bot commands; no silent fallback."""
    await client.sync_time(await reference_time())
