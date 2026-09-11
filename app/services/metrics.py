"""Метрики в формате Prometheus (для Grafana)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import list_devices
from app.models import AlertState, ChannelState, HddState

# Какие типы алертов считаем критичными (для разбивки)
_CRITICAL_ALERTS = {
    "nvr_unreachable", "nvr_auth_error", "hdd_fault", "overheat", "archive_missing",
}


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


async def render_metrics(session: AsyncSession) -> str:
    from app.services import poller  # избегаем циклического импорта

    devices = await list_devices(session)
    from app.services.health import get_health_map
    health_map = await get_health_map(session, devices)
    alerts = (
        await session.execute(select(AlertState).where(AlertState.active.is_(True)))
    ).scalars().all()

    samples: dict[str, list[str]] = {}

    def add(name: str, value, labels: str = ""):
        samples.setdefault(name, []).append(
            f"{name}{{{labels}}} {value}" if labels else f"{name} {value}"
        )

    # ── Агрегаты ──────────────────────────────────────────────────────────────
    total = len(devices)
    online = sum(1 for d in devices if d.reachable and d.enabled)
    unreachable = sum(1 for d in devices if not d.reachable and d.enabled)
    ch_total = ch_online = ch_offline = ch_novideo = ch_problem = 0
    hdd_total = hdd_ok = hdd_error = hdd_nodisk = 0
    quality_counts: dict[str, int] = {}
    problem_devices = sum(1 for d in devices if d.enabled and health_map[d.id]["color"] in ("red", "yellow"))

    for d in devices:
        lbl = f'device="{_esc(d.name)}",host="{_esc(d.host)}"'
        c_total = len(d.channels)
        c_online = sum(1 for c in d.channels if c.status == ChannelState.ONLINE)
        c_off = sum(1 for c in d.channels if c.enabled is not False and c.status == ChannelState.OFFLINE)
        c_nv = sum(1 for c in d.channels if c.enabled is not False and c.status == ChannelState.NO_VIDEO)
        c_problem = c_off + c_nv
        ch_total += c_total; ch_online += c_online
        ch_offline += c_off; ch_novideo += c_nv; ch_problem += c_problem

        add("nvrmon_device_reachable", 1 if d.reachable else 0, lbl)
        if d.latitude is not None and d.longitude is not None:
            add("nvrmon_device_geo", 1 if d.reachable else 0,
                f'{lbl},lat="{d.latitude}",lon="{d.longitude}"')
        add("nvrmon_device_consecutive_failures", d.consecutive_failures, lbl)
        add("nvrmon_device_channels_total", c_total, lbl)
        add("nvrmon_device_channels_online", c_online, lbl)
        add("nvrmon_device_channels_problem", c_problem, lbl)
        if d.temperature is not None:
            add("nvrmon_device_temperature_celsius", d.temperature, lbl)
        if d.cpu_load is not None:
            add("nvrmon_device_cpu_percent", d.cpu_load, lbl)
        if d.memory_usage is not None:
            add("nvrmon_device_memory_percent", d.memory_usage, lbl)
        if d.time_drift_seconds is not None:
            add("nvrmon_device_time_drift_seconds", d.time_drift_seconds, lbl)

        # Каналы: статус и глубина архива
        for c in d.channels:
            clbl = f'{lbl},channel="{c.channel_id}",name="{_esc(c.name or "")}"'
            add("nvrmon_channel_up", 1 if c.status == ChannelState.ONLINE else 0, clbl)
            if c.archive_depth_days is not None:
                add("nvrmon_channel_archive_depth_days", c.archive_depth_days, clbl)
            if c.quality:
                quality_counts[c.quality] = quality_counts.get(c.quality, 0) + 1

        # Диски
        for h in d.hdds:
            hdd_total += 1
            if h.status == HddState.OK:
                hdd_ok += 1
            elif h.status in (HddState.ERROR, HddState.UNFORMATTED, HddState.READ_ONLY, HddState.MISSING):
                hdd_error += 1
            elif h.status == HddState.NO_DISK:
                hdd_nodisk += 1
            hlbl = f'{lbl},hdd="{_esc(h.hdd_id)}",status="{h.status}"'
            add("nvrmon_hdd_usage_percent", h.usage_percent, hlbl)
            add("nvrmon_hdd_free_gb", round(h.free_mb / 1024, 1), hlbl)
            add("nvrmon_hdd_capacity_gb", round(h.capacity_mb / 1024, 1), hlbl)

    # ── Глобальные ────────────────────────────────────────────────────────────
    add("nvrmon_up", 1)
    if poller.last_poll_at is not None:
        age = int((dt.datetime.now(dt.timezone.utc) - poller.last_poll_at).total_seconds())
        add("nvrmon_last_poll_age_seconds", age)
    add("nvrmon_devices_total", total)
    add("nvrmon_devices_online", online)
    add("nvrmon_devices_unreachable", unreachable)
    add("nvrmon_devices_with_problems", problem_devices)
    add("nvrmon_channels_total", ch_total)
    add("nvrmon_channels_online", ch_online)
    add("nvrmon_channels_offline", ch_offline)
    add("nvrmon_channels_no_video", ch_novideo)
    add("nvrmon_channels_problem", ch_problem)
    add("nvrmon_hdd_total", hdd_total)
    add("nvrmon_hdd_ok", hdd_ok)
    add("nvrmon_hdd_error", hdd_error)
    add("nvrmon_hdd_no_disk", hdd_nodisk)
    add("nvrmon_active_alerts", len(alerts))
    add("nvrmon_active_alerts_critical", sum(1 for a in alerts if a.alert_type in _CRITICAL_ALERTS))
    add("nvrmon_active_alerts_warning", sum(1 for a in alerts if a.alert_type not in _CRITICAL_ALERTS))
    for verdict in ("ok", "dark", "uniform", "blurry", "frozen"):
        add("nvrmon_channels_quality", quality_counts.get(verdict, 0), f'verdict="{verdict}"')

    _HELP = {
        "nvrmon_up": "Монитор жив (1)",
        "nvrmon_last_poll_age_seconds": "Секунд с последнего опроса",
        "nvrmon_devices_total": "Всего устройств",
        "nvrmon_devices_online": "Доступных устройств",
        "nvrmon_devices_unreachable": "Недоступных устройств",
        "nvrmon_devices_with_problems": "Устройств с проблемами",
        "nvrmon_channels_total": "Всего каналов",
        "nvrmon_channels_online": "Каналов онлайн",
        "nvrmon_channels_offline": "Каналов offline",
        "nvrmon_channels_no_video": "Каналов без видео",
        "nvrmon_channels_problem": "Каналов с проблемой",
        "nvrmon_channels_quality": "Каналов по качеству картинки",
        "nvrmon_hdd_total": "Всего дисков",
        "nvrmon_hdd_ok": "Дисков OK",
        "nvrmon_hdd_error": "Дисков с ошибкой",
        "nvrmon_hdd_no_disk": "Отсутствующих дисков",
        "nvrmon_active_alerts": "Активных алертов",
        "nvrmon_active_alerts_critical": "Критических алертов",
        "nvrmon_active_alerts_warning": "Предупреждений",
        "nvrmon_device_reachable": "Доступность устройства (1/0)",
        "nvrmon_device_geo": "Объект на карте (1=онлайн)",
        "nvrmon_device_consecutive_failures": "Неудачных опросов подряд",
        "nvrmon_device_channels_total": "Каналов на устройстве",
        "nvrmon_device_channels_online": "Каналов онлайн на устройстве",
        "nvrmon_device_channels_problem": "Каналов с проблемой на устройстве",
        "nvrmon_device_temperature_celsius": "Температура NVR, °C",
        "nvrmon_device_cpu_percent": "Загрузка CPU NVR, %",
        "nvrmon_device_memory_percent": "Память NVR, %",
        "nvrmon_device_time_drift_seconds": "Дрейф времени NVR, сек",
        "nvrmon_channel_up": "Канал онлайн (1/0)",
        "nvrmon_channel_archive_depth_days": "Глубина архива канала, дней",
        "nvrmon_hdd_usage_percent": "Заполнение диска, %",
        "nvrmon_hdd_free_gb": "Свободно на диске, ГБ",
        "nvrmon_hdd_capacity_gb": "Объём диска, ГБ",
    }

    out: list[str] = []
    for name in _HELP:
        if name not in samples:
            continue
        out.append(f"# HELP {name} {_HELP[name]}")
        out.append(f"# TYPE {name} gauge")
        out.extend(samples[name])
    return "\n".join(out) + "\n"
