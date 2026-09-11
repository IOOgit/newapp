"""Единая оценка NVR для панели: доступность не равна сохранности записи.

Сервис только читает измерения. Он не создаёт события и не вызывает Telegram.
Служебные даты сравниваются в UTC; даты архива остаются локальными датами NVR.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import ArchiveCoverage, ArchiveState, ChannelState, Device, HddState, Quality, utcnow


def _utc(value) -> dt.datetime | None:
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def _fresh(value, now: dt.datetime, seconds: float) -> bool:
    value = _utc(value)
    return value is not None and -60 <= (now - value).total_seconds() <= seconds


def _iso(value) -> str | None:
    value = _utc(value)
    return value.isoformat() if value else None


def _archive_target(now: dt.datetime) -> dt.date:
    # Суточная задача использует локальный пояс процесса, как и search_archive.
    # Даём час на завершение обхода объектов после запланированного запуска.
    local = now.astimezone()
    due = local.replace(hour=settings.archive_check_hour, minute=settings.archive_check_minute,
                        second=0, microsecond=0) + dt.timedelta(hours=1)
    return local.date() - dt.timedelta(days=1 if local >= due else 2)


def evaluate_device(device: Device, *, now=None, archive_rows=None) -> dict:
    """Возвращает цвет, причины и свежесть проверок для загруженного устройства.

    Требуются заранее загруженные device.channels/hdds. archive_rows — результаты
    суточного архива; отсутствие результата явно остаётся неполной проверкой.
    """
    now = _utc(now) or utcnow()
    ttl = max(120, settings.poll_interval_minutes * 60 * 3)
    issues: list[dict] = []
    checks = {name: dict(value) for name, value in (device.monitoring_checks or {}).items()
              if isinstance(value, dict)}

    def issue(kind, title, detail=None, *, critical=False, channel_id=None):
        issues.append({"kind": kind, "severity": "critical" if critical else "warning",
                       "title": title, "detail": detail or title, "channel_id": channel_id})

    if device.enabled is False:
        return {"color": "gray", "label": "Мониторинг выключен", "issues": [], "checks": checks}

    last_seen = getattr(device, "last_seen", None)
    checks["connection"] = {"status": "ok", "checked_at": _iso(last_seen), "detail": None}
    if device.reachable is False:
        checks["connection"]["status"] = "error"
        issue("connection", "NVR недоступен", device.last_error, critical=True)
    elif getattr(device, "last_error", None):
        checks["connection"]["status"] = "error"
        issue("connection", "Ошибка опроса NVR", device.last_error)
    elif not _fresh(last_seen, now, ttl):
        checks["connection"]["status"] = "stale" if last_seen else "unknown"
        issue("connection", "Данные связи устарели" if last_seen else "NVR ещё не проверен")

    names = {"channels": "Камеры", "hdd": "Диски", "time": "Время", "health": "Телеметрия"}
    for key, title in names.items():
        check = checks.setdefault(key, {"status": "unknown", "checked_at": None, "detail": None})
        status = check.get("status", "unknown")
        if key == "health" and status in ("unknown", "unavailable"):
            continue  # Дополнительные датчики есть не на всех прошивках.
        if status == "ok" and not _fresh(check.get("checked_at"), now, ttl):
            check["status"] = "stale"
            issue(key, f"{title}: данные устарели")
        elif status != "ok":
            reason = {"error": "ошибка проверки", "unavailable": "API недоступен",
                      "stale": "данные устарели"}.get(status, "ещё не проверено")
            issue(key, f"{title}: {reason}", check.get("detail"))

    channels = [ch for ch in device.channels if ch.enabled is not False]
    if not channels:
        issue("channels", "Нет каналов под наблюдением")
        checks["channels"]["status"] = "unknown"
    for ch in channels:
        if ch.status in (ChannelState.OFFLINE, ChannelState.NO_VIDEO):
            label = "нет связи с камерой" if ch.status == ChannelState.OFFLINE else "нет видео"
            issue("channels", f"Канал {ch.channel_id}: {label}", channel_id=ch.channel_id)
        elif ch.status != ChannelState.ONLINE:
            issue("channels", f"Канал {ch.channel_id}: состояние неизвестно", channel_id=ch.channel_id)

    disks = list(device.hdds)
    if not disks and checks["hdd"].get("status") == "ok":
        issue("hdd", "В NVR не обнаружены диски", critical=True)
    hdd_labels = {HddState.ERROR: "ошибка диска", HddState.NO_DISK: "диск отсутствует",
                  HddState.UNFORMATTED: "диск не инициализирован", HddState.READ_ONLY: "диск только для чтения",
                  HddState.MISSING: "пропал из состава хранилища"}
    for hdd in disks:
        state = HddState.MISSING if getattr(hdd, "present", True) is False else hdd.status
        if state in hdd_labels:
            issue("hdd", f"HDD {hdd.hdd_id}: {hdd_labels[state]}",
                  getattr(hdd, "raw_status", None), critical=True)
        elif state != HddState.OK or not hdd.capacity_mb:
            issue("hdd", f"HDD {hdd.hdd_id}: готовность не подтверждена", getattr(hdd, "raw_status", None))
    # Заполненность не является неисправностью для циклической записи.

    drift = getattr(device, "time_drift_seconds", None)
    if drift is None and checks["time"].get("status") == "ok":
        issue("time", "Расхождение часов не измерено")
    elif drift is not None and drift > settings.time_drift_alert_minutes * 60:
        issue("time", f"Часы NVR расходятся на {drift // 60} мин")
    if checks["health"].get("status") not in ("unknown", "unavailable"):
        temp = getattr(device, "temperature", None)
        cpu = getattr(device, "cpu_load", None)
        if temp is not None and temp >= settings.temp_alert_celsius:
            issue("health", f"Перегрев NVR: {temp:g} °C", critical=True)
        if cpu is not None and cpu >= settings.cpu_alert_percent:
            issue("health", f"Высокая загрузка CPU: {cpu:g}%")

    rec_ttl = max(120, getattr(settings, "recording_check_minutes", 5) * 60 * 3)
    recording_channels = [ch for ch in channels if getattr(ch, "recording_mode", "continuous") != "disabled"]
    rec_dates = [_utc(getattr(ch, "recording_checked_at", None)) for ch in recording_channels]
    recording_stale_channels = []
    checks["recording"] = {"status": "ok" if recording_channels else "disabled",
                           "checked_at": _iso(min(rec_dates)) if rec_dates and all(rec_dates) else None,
                           "detail": None}
    for ch in recording_channels:
        status = getattr(ch, "recording_status", None) or "unknown"
        mode = getattr(ch, "recording_mode", "continuous")
        if status == "missing" and mode == "continuous":
            issue("recording", f"Канал {ch.channel_id}: свежая запись не найдена",
                  getattr(ch, "recording_error", None), critical=True, channel_id=ch.channel_id)
        elif status not in ("ok", "event"):
            label = {"error": "ошибка проверки записи", "unavailable": "поиск записи недоступен",
                     "time_error": "запись не проверена из-за времени NVR"}.get(status, "запись ещё не подтверждена")
            issue("recording", f"Канал {ch.channel_id}: {label}",
                  getattr(ch, "recording_error", None), channel_id=ch.channel_id)
        elif status == "event":
            if mode != "event":
                issue("recording", f"Канал {ch.channel_id}: непрерывная запись не подтверждена", channel_id=ch.channel_id)
            else:
                checks["recording"]["status"] = "event"
        if status == "ok" and mode == "continuous":
            age = getattr(ch, "recording_age_seconds", None)
            checked = _utc(getattr(ch, "recording_checked_at", None))
            if age is not None and checked and age + max(0, (now - checked).total_seconds()) > settings.recording_max_age_minutes * 60:
                issue("recording", f"Канал {ch.channel_id}: подтверждённая запись устарела", channel_id=ch.channel_id)
                recording_stale_channels.append(ch.channel_id)
        if not _fresh(getattr(ch, "recording_checked_at", None), now, rec_ttl):
            issue("recording", f"Канал {ch.channel_id}: нет свежей проверки записи", channel_id=ch.channel_id)
            recording_stale_channels.append(ch.channel_id)
    if recording_channels and not settings.recording_check_minutes:
        issue("recording", "Периодическая проверка свежей записи выключена")
    if recording_stale_channels:
        checks["recording"]["status"] = "stale"

    continuous = [ch for ch in channels if getattr(ch, "recording_mode", "continuous") == "continuous"]
    target = _archive_target(now)
    archive_by_channel = {}
    for row in archive_rows or []:
        if getattr(row, "device_id", device.id) != device.id:
            continue
        current = archive_by_channel.get(row.channel_id)
        if current is None or row.day > current.day:
            archive_by_channel[row.channel_id] = row
    checks["archive"] = {"status": "ok" if continuous else "disabled", "checked_at": None,
                         "detail": f"Суточная проверка: не ранее {target.isoformat()}"}
    archive_dates = []
    for ch in continuous:
        row = archive_by_channel.get(ch.channel_id)
        if row is None or row.day < target or not _fresh(row.checked_at, now, 48 * 3600):
            issue("archive", f"Канал {ch.channel_id}: нет актуальной суточной проверки архива", channel_id=ch.channel_id)
            checks["archive"]["status"] = "no_data"
            continue
        archive_dates.append(_utc(row.checked_at))
        if row.status == ArchiveState.NONE:
            issue("archive", f"Канал {ch.channel_id}: нет архива за {row.day}", critical=True, channel_id=ch.channel_id)
        elif row.status == ArchiveState.PARTIAL:
            issue("archive", f"Канал {ch.channel_id}: разрыв архива за {row.day}",
                  f"Наибольший разрыв: {row.largest_gap_minutes} мин", channel_id=ch.channel_id)
        elif row.status != ArchiveState.FULL:
            issue("archive", f"Канал {ch.channel_id}: архив не подтверждён", channel_id=ch.channel_id)
    if archive_dates:
        checks["archive"]["checked_at"] = _iso(min(archive_dates))

    checks["quality"] = {"status": "disabled" if not settings.quality_check_minutes else "unavailable",
                         "checked_at": None, "detail": None}
    if settings.quality_check_minutes:
        quality_dates = []
        for ch in channels:
            checked = getattr(ch, "quality_checked_at", None)
            if not checked:
                continue  # Снимки — необязательная возможность драйвера.
            quality_dates.append(_utc(checked))
            checks["quality"]["status"] = "ok"
            if not _fresh(checked, now, max(120, settings.quality_check_minutes * 60 * 3)):
                issue("quality", f"Канал {ch.channel_id}: проверка картинки устарела", channel_id=ch.channel_id)
            elif ch.quality and ch.quality != Quality.OK:
                labels = {Quality.DARK: "тёмный кадр", Quality.UNIFORM: "однотонный кадр",
                          Quality.BLURRY: "расфокус", Quality.FROZEN: "зависший кадр", Quality.ERROR: "ошибка снимка"}
                issue("quality", f"Канал {ch.channel_id}: {labels.get(ch.quality, ch.quality)}", channel_id=ch.channel_id)
        if quality_dates:
            checks["quality"]["checked_at"] = _iso(min(quality_dates))

    # Индикатор отражает и результат, и свежесть. Сырой результат попытки
    # остаётся в Device.monitoring_checks для диагностики.
    for key, check in checks.items():
        related = [item for item in issues if item["kind"] == key]
        if any(item["severity"] == "critical" for item in related):
            check["status"] = "critical"
        elif related and check.get("status") in ("ok", "event", "disabled"):
            check["status"] = "warning"
        if related:
            check["detail"] = "; ".join(item["title"] for item in related)
    critical = next((item for item in issues if item["severity"] == "critical"), None)
    color = "red" if critical else "yellow" if issues else "green"
    label = critical["title"] if critical else "Проверка неполная / есть замечания" if issues else "Запись и хранилище в норме"
    if not issues and not recording_channels:
        label = "Показатели в норме; контроль записи выключен"
    elif not issues and any(getattr(ch, "recording_mode", "continuous") == "event" for ch in recording_channels):
        label = "Проверенные показатели в норме"
    if not last_seen and not critical:
        color, label = "gray", "Ожидает первого полного опроса"
    return {"color": color, "label": label, "issues": issues, "checks": checks,
            "recording_stale_channels": sorted(set(recording_stale_channels))}


async def get_health_map(session: AsyncSession, devices=None) -> dict[int, dict]:
    """Загружает суточный архив одной выборкой и одинаково оценивает все NVR."""
    if devices is None:
        devices = (await session.execute(select(Device).options(
            selectinload(Device.channels), selectinload(Device.hdds)
        ))).scalars().all()
    devices = list(devices)
    if not devices:
        return {}
    now = utcnow()
    rows = (await session.execute(select(ArchiveCoverage).where(
        ArchiveCoverage.device_id.in_([device.id for device in devices]),
        ArchiveCoverage.day >= _archive_target(now),
        ArchiveCoverage.day < now.astimezone().date(),
    ))).scalars().all()
    by_device: dict[int, list] = {}
    for row in rows:
        by_device.setdefault(row.device_id, []).append(row)
    return {device.id: evaluate_device(device, now=now, archive_rows=by_device.get(device.id, []))
            for device in devices}
