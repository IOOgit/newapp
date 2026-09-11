"""Опрос устройств: статус каналов, HDD, время. Обновляет БД и поднимает алерты."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.drivers import build_client
from app.drivers.base import FeatureUnavailable, NVRAuthError, NVRConnectionError, NVRError
from app.models import (
    Channel,
    ChannelState,
    Device,
    Hdd,
    HddState,
    Severity,
    utcnow,
)
from app.services import alerts

log = logging.getLogger(__name__)

# Один общий semaphore на процесс — щадим VPN-каналы
_semaphore = asyncio.Semaphore(settings.max_concurrent_polls)

# Время последнего завершённого полного опроса (для watchdog — детект «опрос завис»)
last_poll_at: dt.datetime | None = None


def _mark_check(device: Device, name: str, status: str, detail: str | None = None) -> None:
    """Храним результат попытки отдельно от последних измеренных значений.

    JSON заменяется целиком: SQLAlchemy не замечает вложенные изменения dict.
    Эти отметки используются только панелью и не запускают уведомления.
    """
    checks = dict(device.monitoring_checks or {})
    checks[name] = {"status": status, "checked_at": utcnow().isoformat(), "detail": detail}
    device.monitoring_checks = checks


async def poll_all() -> None:
    """Опрашивает все включённые устройства параллельно (с ограничением)."""
    global last_poll_at
    async with SessionLocal() as session:
        devices = (
            await session.execute(select(Device).where(Device.enabled.is_(True)))
        ).scalars().all()
        ids = [d.id for d in devices]
    if ids:
        log.info("Опрос %d устройств", len(ids))
        await asyncio.gather(*(poll_device(did) for did in ids), return_exceptions=True)
    last_poll_at = utcnow()


async def poll_device(device_id: int) -> None:
    """Опрашивает одно устройство в собственной сессии БД."""
    async with SessionLocal() as session:
        device = (
            await session.execute(
                select(Device)
                .where(Device.id == device_id)
                .options(selectinload(Device.channels), selectinload(Device.hdds))
            )
        ).scalar_one_or_none()
        if device is None or not device.enabled:
            return
        try:
            await _poll_one(session, device)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка опроса устройства %s", device_id)
        await session.commit()


async def _poll_one(session: AsyncSession, device: Device) -> None:
    for name in ("channels", "hdd", "time", "health"):
        _mark_check(device, name, "error", "Опрос не завершён")
    client = build_client(device, semaphore=_semaphore)
    caps = device.capabilities or {}

    # ── Проба доступности ──────────────────────────────────────────────────
    try:
        await client.get_device_info()
    except NVRConnectionError as exc:
        await _handle_unreachable(session, device, str(exc))
        return
    except NVRAuthError as exc:
        # Разовый 401 (нестабильный канал/занятый NVR) не алармим — только при
        # стойкой ошибке авторизации N циклов подряд.
        device.auth_failures += 1
        device.last_error = f"auth: {exc}"
        if device.auth_failures >= settings.nvr_auth_error_threshold:
            await alerts.raise_alert(
                session, scope_key=f"device:{device.id}:auth",
                alert_type="nvr_auth_error", severity=Severity.CRITICAL,
                device_id=device.id,
                message=(
                    f"NVR «{device.name}» ({device.host}): ошибка авторизации "
                    f"({device.auth_failures} циклов подряд)"
                ),
            )
        else:
            log.info(
                "NVR %s ошибка авторизации (%d/%d): %s",
                device.id, device.auth_failures, settings.nvr_auth_error_threshold, exc,
            )
        return
    except NVRError as exc:
        log.warning("deviceInfo %s: %s", device.id, exc)

    # Устройство доступно
    await _handle_reachable(session, device)

    # ── Каналы ──────────────────────────────────────────────────────────────
    if caps.get("channels", True):
        try:
            statuses = await client.get_channel_statuses()
            await _update_channels(session, device, statuses)
            _mark_check(device, "channels", "ok")
        except NVRConnectionError as exc:
            _mark_check(device, "channels", "error", str(exc))
            await _handle_unreachable(session, device, str(exc))
            return
        except FeatureUnavailable as exc:
            _mark_check(device, "channels", "unavailable", str(exc))
        except NVRError as exc:
            _mark_check(device, "channels", "error", str(exc))
            log.warning("Каналы %s недоступны: %s", device.id, exc)
        except (ValueError, TypeError) as exc:
            _mark_check(device, "channels", "error", "Некорректный ответ каналов")
            log.warning("Каналы %s: некорректный ответ: %s", device.id, exc)
    else:
        _mark_check(device, "channels", "unavailable", "API каналов недоступен")

    # ── HDD ───────────────────────────────────────────────────────────────────
    if caps.get("hdd", True):
        try:
            hdds = await client.get_hdd_info()
            await _update_hdds(session, device, hdds)
            _mark_check(device, "hdd", "ok")
        except FeatureUnavailable as exc:
            _mark_check(device, "hdd", "unavailable", str(exc))
        except NVRError as exc:
            _mark_check(device, "hdd", "error", str(exc))
            log.debug("HDD %s: %s", device.id, exc)
    else:
        _mark_check(device, "hdd", "unavailable", "API хранилища недоступен")

    # ── Время устройства ───────────────────────────────────────────────────────
    if caps.get("time", True):
        try:
            device_time = await client.get_device_time()
            await _check_time_drift(session, device, device_time)
            _mark_check(device, "time", "ok")
        except FeatureUnavailable as exc:
            _mark_check(device, "time", "unavailable", str(exc))
        except NVRError as exc:
            _mark_check(device, "time", "error", str(exc))
            log.debug("Время %s: %s", device.id, exc)
    else:
        _mark_check(device, "time", "unavailable", "API времени недоступен")

    # ── Здоровье железа (температура/нагрузка) ─────────────────────────────────
    try:
        health = await client.get_health()
        await _check_health(session, device, health)
        available = any(value is not None for value in (health.cpu_percent, health.memory_percent, health.temperature_c))
        _mark_check(device, "health", "ok" if available else "unavailable")
    except FeatureUnavailable as exc:
        _mark_check(device, "health", "unavailable", str(exc))
    except NVRError as exc:
        _mark_check(device, "health", "error", str(exc))
        log.debug("Health %s: %s", device.id, exc)


async def _check_health(session: AsyncSession, device: Device, health) -> None:
    device.cpu_load = health.cpu_percent
    device.memory_usage = health.memory_percent
    device.temperature = health.temperature_c

    scope = f"device:{device.id}:overheat"
    if health.temperature_c is not None and health.temperature_c >= settings.temp_alert_celsius:
        await alerts.raise_alert(
            session, scope_key=scope, alert_type="overheat", severity=Severity.CRITICAL,
            device_id=device.id,
            message=f"«{device.name}»: перегрев NVR — {health.temperature_c}°C",
        )
    elif health.temperature_c is not None:
        await alerts.resolve_alert(
            session, scope_key=scope, device_id=device.id,
            message=f"«{device.name}»: температура в норме ({health.temperature_c}°C)",
        )

    cpu_scope = f"device:{device.id}:cpu"
    if health.cpu_percent is not None and health.cpu_percent >= settings.cpu_alert_percent:
        await alerts.raise_alert(
            session, scope_key=cpu_scope, alert_type="high_cpu", severity=Severity.WARNING,
            device_id=device.id,
            message=f"«{device.name}»: высокая загрузка CPU — {health.cpu_percent}%",
        )
    elif health.cpu_percent is not None:
        await alerts.resolve_alert(
            session, scope_key=cpu_scope, device_id=device.id,
            message=f"«{device.name}»: загрузка CPU в норме ({health.cpu_percent}%)",
            notify=False,
        )


# ── Доступность устройства ─────────────────────────────────────────────────────
async def _handle_unreachable(session: AsyncSession, device: Device, error: str) -> None:
    device.consecutive_failures += 1
    device.reachable = False
    device.last_error = error
    log.info(
        "NVR %s недоступен (%d/%d): %s",
        device.id, device.consecutive_failures, settings.nvr_unreachable_threshold, error,
    )
    if device.consecutive_failures >= settings.nvr_unreachable_threshold:
        await alerts.raise_alert(
            session, scope_key=f"device:{device.id}:unreachable",
            alert_type="nvr_unreachable", severity=Severity.CRITICAL,
            device_id=device.id,
            message=(
                f"NVR «{device.name}» ({device.host}:{device.http_port}) недоступен "
                f"({device.consecutive_failures} циклов подряд)"
            ),
            context={"error": error},
        )


async def _handle_reachable(session: AsyncSession, device: Device) -> None:
    was_down = device.consecutive_failures >= settings.nvr_unreachable_threshold
    was_auth_failing = device.auth_failures >= settings.nvr_auth_error_threshold
    device.consecutive_failures = 0
    device.auth_failures = 0
    device.reachable = True
    device.last_seen = utcnow()
    device.last_error = None
    if was_down:
        await alerts.resolve_alert(
            session, scope_key=f"device:{device.id}:unreachable",
            device_id=device.id,
            message=f"NVR «{device.name}» ({device.host}) снова доступен",
        )
    if was_auth_failing:
        await alerts.resolve_alert(
            session, scope_key=f"device:{device.id}:auth", device_id=device.id,
            message=f"NVR «{device.name}»: авторизация восстановлена", notify=False,
        )


# ── Каналы ─────────────────────────────────────────────────────────────────────
async def _update_channels(session: AsyncSession, device: Device, statuses) -> None:
    existing = {c.channel_id: c for c in device.channels}
    original_ids = set(existing.keys())   # каналы, известные ДО этого опроса
    had_channels = bool(original_ids)     # не первый ли это опрос
    seen_ids: set[int] = set()
    now = utcnow()
    threshold = dt.timedelta(minutes=settings.camera_offline_alert_minutes)

    for st in statuses:
        seen_ids.add(st.channel_id)
        ch = existing.get(st.channel_id)
        if ch is None:
            ch = Channel(
                device_id=device.id, channel_id=st.channel_id,
                name=st.name, kind=st.kind, status=ChannelState.UNKNOWN,
            )
            session.add(ch)
            existing[st.channel_id] = ch
            # Новая камера появилась в конфигурации NVR (не на первом опросе)
            if had_channels:
                await alerts.notify_once(
                    session, type_="camera_added", severity=Severity.INFO,
                    device_id=device.id, channel_id=st.channel_id,
                    message=f"«{device.name}»: добавлена камера, канал {st.channel_id} ({st.name or '—'})",
                )
        if st.name and ch.name != st.name:
            ch.name = st.name
        ch.kind = st.kind
        ch.last_seen = now

        new_state = st.state
        if ch.status != new_state:
            ch.status = new_state
            ch.last_status_change = now

        scope = f"device:{device.id}:channel:{st.channel_id}:down"
        if ch.enabled is False:  # именно False (None у свежесозданного = ещё не выключен)
            # Канал снят с мониторинга («заглушка») — гасим активный алерт и не тревожим
            await alerts.resolve_alert(
                session, scope_key=scope, device_id=device.id, channel_id=st.channel_id,
                message=f"«{device.name}» канал {st.channel_id}: снят с мониторинга",
                notify=False,
            )
            continue
        if new_state == ChannelState.ONLINE:
            await alerts.resolve_alert(
                session, scope_key=scope, device_id=device.id, channel_id=st.channel_id,
                message=f"«{device.name}» канал {st.channel_id} ({ch.name or '—'}): онлайн",
            )
        else:
            down_since = ch.last_status_change or now
            if now - down_since >= threshold:
                label = "offline" if new_state == ChannelState.OFFLINE else "нет видео"
                await alerts.raise_alert(
                    session, scope_key=scope, alert_type="camera_down",
                    severity=Severity.WARNING, device_id=device.id,
                    channel_id=st.channel_id,
                    message=(
                        f"«{device.name}» канал {st.channel_id} "
                        f"({ch.name or '—'}): {label} "
                        f"уже {int((now - down_since).total_seconds() // 60)} мин"
                    ),
                )

    # Камеры, пропавшие из конфигурации NVR (были, но в опросе их больше нет)
    for cid in original_ids - seen_ids:
        ch = existing.get(cid)
        if ch is None or ch.status == ChannelState.UNKNOWN:
            continue
        ch.status = ChannelState.UNKNOWN
        await alerts.notify_once(
            session, type_="camera_removed", severity=Severity.WARNING,
            device_id=device.id, channel_id=cid,
            message=f"«{device.name}»: камера убрана из конфигурации NVR, канал {cid} ({ch.name or '—'})",
        )


# ── HDD ──────────────────────────────────────────────────────────────────────
async def _update_hdds(session: AsyncSession, device: Device, hdds) -> None:
    existing = {h.hdd_id: h for h in device.hdds}
    # Драйвер возвращает только полный валидный инвентарь; при ошибке он бросает
    # исключение, поэтому пропажу нельзя спутать с неподдерживаемым API/таймаутом.
    seen_ids = {info.hdd_id for info in hdds}
    for info in hdds:
        h = existing.get(info.hdd_id)
        if h is None:
            h = Hdd(device_id=device.id, hdd_id=info.hdd_id)
            session.add(h)
            existing[info.hdd_id] = h
        h.name = info.name
        h.capacity_mb = info.capacity_mb
        h.free_mb = info.free_mb
        h.status = info.status
        h.raw_status = info.raw_status
        h.present = True

        fault_scope = f"device:{device.id}:hdd:{info.hdd_id}:fault"
        if info.status in (HddState.ERROR, HddState.NO_DISK):
            label = "ошибка диска" if info.status == HddState.ERROR else "диск отсутствует"
            await alerts.raise_alert(
                session, scope_key=fault_scope, alert_type="hdd_fault",
                severity=Severity.CRITICAL, device_id=device.id,
                message=f"«{device.name}» HDD {info.hdd_id}: {label}",
            )
        elif info.status == HddState.OK:
            await alerts.resolve_alert(
                session, scope_key=fault_scope, device_id=device.id,
                message=f"«{device.name}» HDD {info.hdd_id}: норма",
            )

        # Порог заполнения (опционально)
        full_scope = f"device:{device.id}:hdd:{info.hdd_id}:full"
        limit = settings.hdd_usage_alert_percent
        if limit and h.capacity_mb:
            usage = h.usage_percent
            if usage >= limit:
                await alerts.raise_alert(
                    session, scope_key=full_scope, alert_type="hdd_full",
                    severity=Severity.WARNING, device_id=device.id,
                    message=f"«{device.name}» HDD {info.hdd_id}: заполнен на {usage}%",
                )
            else:
                await alerts.resolve_alert(
                    session, scope_key=full_scope, device_id=device.id,
                    message=f"«{device.name}» HDD {info.hdd_id}: заполнение в норме ({usage}%)",
                    notify=False,
                )

    for hid in existing.keys() - seen_ids:
        h = existing[hid]
        h.present = False
        h.status = HddState.MISSING


# ── Время ──────────────────────────────────────────────────────────────────────
async def _check_time_drift(session: AsyncSession, device: Device, device_time: dt.datetime) -> None:
    # Корректно учитываем часовой пояс:
    #  - если NVR вернул время со смещением (напр. +03:00) — сравниваем абсолютные
    #    моменты в UTC (разница поясов сама по себе НЕ считается дрейфом);
    #  - если время «наивное» (без пояса) — сравниваем со «стенными» часами сервера
    #    (для этого задайте TZ контейнера под пояс регистраторов, см. .env).
    if device_time.tzinfo is not None:
        drift = int(abs((device_time - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    else:
        drift = int(abs((device_time - dt.datetime.now()).total_seconds()))
    device.time_drift_seconds = drift

    scope = f"device:{device.id}:timedrift"
    limit = settings.time_drift_alert_minutes * 60
    if drift > limit:
        await alerts.raise_alert(
            session, scope_key=scope, alert_type="time_drift",
            severity=Severity.WARNING, device_id=device.id,
            message=(
                f"«{device.name}»: часы NVR расходятся с сервером на "
                f"{drift // 60} мин (проверка архива может врать)"
            ),
            context={"drift_seconds": drift},
        )
    else:
        await alerts.resolve_alert(
            session, scope_key=scope, device_id=device.id,
            message=f"«{device.name}»: время синхронизировано", notify=False,
        )
