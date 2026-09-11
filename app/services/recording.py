"""Проверка свежести индекса архива без загрузки видео и уведомлений.

Свежий сегмент подтверждает наличие записи в индексе NVR, но не её
воспроизводимость. Время сегментов сравнивается с настенными часами NVR:
переводить его в UTC нельзя. Служебные метки проверок хранятся в UTC.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal
from app.drivers import build_client
from app.drivers.base import ArchiveSegment, FeatureUnavailable, NVRAuthError, NVRClient
from app.models import Channel, Device, utcnow
from app.services.poller import _semaphore

log = logging.getLogger(__name__)

# Ограничиваем весь обход NVR, в том числе постраничный поиск и повторы драйвера.
_DEVICE_CHECK_TIMEOUT_SECONDS = 120.0
_recording_slots = asyncio.Semaphore(max(1, settings.max_concurrent_polls))
_active_device_ids: set[int] = set()


def _wall_clock(value: dt.datetime) -> dt.datetime:
    """Отбрасывает пояс без конверсии, согласно контракту драйверов."""
    if not isinstance(value, dt.datetime):
        raise ValueError("NVR вернул некорректное время")
    return value.replace(tzinfo=None)


def _latest_end(
    segments: list[ArchiveSegment], start: dt.datetime, now: dt.datetime,
) -> dt.datetime | None:
    """Возвращает конец последней реально начавшейся записи.

    NVR нередко отдаёт конец текущего открытого/планового фрагмента позже своих
    текущих часов. Такой конец ограничиваем текущим временем. Полностью будущие
    фрагменты игнорируем: они не подтверждают запись, но не ломают весь канал.
    """
    latest = None
    for segment in segments:
        seg_start = _wall_clock(segment.start)
        seg_end = _wall_clock(segment.end)
        if seg_end <= seg_start:
            raise ValueError("NVR вернул пустой или обратный интервал записи")
        if seg_start > now:
            continue
        if seg_end > now:
            seg_end = now
        if seg_end <= start:
            continue
        if latest is None or seg_end > latest:
            latest = seg_end
    return latest


def _failed(channel: Channel, status: str, detail: str, checked_at: dt.datetime) -> None:
    """Сохраняет последнюю успешную находку, но снимает подтверждение свежести."""
    channel.recording_status = status
    channel.recording_checked_at = checked_at
    channel.recording_age_seconds = None
    channel.recording_error = detail[:500]


async def _check_channels(device: Device, channels: list[Channel], client: NVRClient) -> None:
    try:
        now = _wall_clock(await client.get_device_time())
    except NVRAuthError as exc:
        for channel in channels:
            _failed(channel, "error", f"Ошибка авторизации NVR: {exc}", utcnow())
        return
    except Exception as exc:  # noqa: BLE001 — драйвер может вернуть невалидную дату
        for channel in channels:
            _failed(channel, "time_error", f"Не удалось получить время NVR: {exc}", utcnow())
        return

    # Окно не должно быть короче допустимого возраста записи.
    max_age = max(1, settings.recording_max_age_minutes) * 60
    window = max(settings.recording_window_minutes, settings.recording_max_age_minutes, 1)
    start = now - dt.timedelta(minutes=window)
    for index, channel in enumerate(channels):
        try:
            segments = await client.search_archive(channel.channel_id, start, now)
            latest = _latest_end(segments, start, now)
        except FeatureUnavailable as exc:
            _failed(channel, "unavailable", str(exc), utcnow())
            continue
        except NVRAuthError as exc:
            # Пароль общий для NVR: остальные каналы не нагружаем теми же 401.
            for pending in channels[index:]:
                _failed(pending, "error", f"Ошибка авторизации NVR: {exc}", utcnow())
            return
        except Exception as exc:  # noqa: BLE001 — повреждённый ответ не означает пустой архив
            log.warning("Проверка свежести записи %s/%s: %s", device.id, channel.channel_id, exc)
            _failed(channel, "error", f"Ошибка поиска архива: {exc}", utcnow())
            continue

        channel.recording_checked_at = utcnow()
        channel.recording_last_end = latest
        channel.recording_age_seconds = int((now - latest).total_seconds()) if latest else None
        channel.recording_error = None
        if latest is not None and channel.recording_age_seconds <= max_age:
            channel.recording_status = "ok"
        elif channel.recording_mode == "event":
            channel.recording_status = "event"
        else:
            channel.recording_status = "missing"


async def check_recording_device(
    session: AsyncSession, device: Device, *, client: NVRClient | None = None,
) -> bool:
    """Проверяет один NVR; транзакцией управляет вызывающий код.

    ``client`` — необязательный готовый драйвер для моков. Внешним HTTP-клиентом
    владеет вызывающий код; фабричный драйвер сам закрывает свои HTTP-соединения.
    Повторный запуск по уже проверяемому NVR пропускается, возвращая ``False``.
    """
    if not device.enabled or device.id in _active_device_ids:
        return False
    _active_device_ids.add(device.id)
    try:
        async with _recording_slots:
            channels = list((await session.scalars(
                select(Channel).where(Channel.device_id == device.id).order_by(Channel.channel_id)
            )).all())
            active = []
            for channel in channels:
                if not channel.enabled or channel.recording_mode == "disabled":
                    channel.recording_status = "disabled"
                    channel.recording_checked_at = utcnow()
                    channel.recording_last_end = None
                    channel.recording_age_seconds = None
                    channel.recording_error = None
                else:
                    active.append(channel)

            if active:
                try:
                    # Результат probe_capabilities мог быть временной сетевой ошибкой.
                    # Каждая проверка пробует возможность заново для восстановления.
                    driver = client if client is not None else build_client(device, semaphore=_semaphore)
                    async with asyncio.timeout(_DEVICE_CHECK_TIMEOUT_SECONDS):
                        await _check_channels(device, active, driver)
                except TimeoutError:
                    # Не оставляем смесь свежего зелёного и старых непроверенных каналов.
                    for channel in active:
                        _failed(channel, "error", "Истекло время проверки записи на NVR", utcnow())
                except Exception as exc:  # noqa: BLE001 — например, неизвестный тип API
                    for channel in active:
                        _failed(channel, "error", f"Не удалось запустить проверку записи: {exc}", utcnow())
            return True
    finally:
        _active_device_ids.discard(device.id)


async def check_device_recording(device_id: int) -> None:
    """Обёртка для ручного запуска: отдельная сессия и сохранение результата."""
    async with SessionLocal() as session:
        device = await session.get(Device, device_id)
        if device is None:
            return
        if await check_recording_device(session, device):
            await session.commit()


async def check_recording_all() -> None:
    """Проверяет включённые NVR ограниченным числом параллельных задач."""
    async with SessionLocal() as session:
        ids = list((await session.scalars(
            select(Device.id).where(Device.enabled.is_(True))
        )).all())

    async def worker() -> None:
        while ids:
            device_id = ids.pop()
            try:
                await check_device_recording(device_id)
            except Exception:  # noqa: BLE001 — один NVR не останавливает проверку остальных
                log.exception("Не удалось сохранить проверку записи NVR %s", device_id)

    await asyncio.gather(*(worker() for _ in range(min(len(ids), max(1, settings.max_concurrent_polls)))))
