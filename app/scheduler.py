"""Планировщик фоновых задач (APScheduler): опрос и суточная проверка архива."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.services import archive, backup, poller, quality, recording, switches, watchdog

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        poller.poll_all,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes),
        id="poll_all",
        name="Опрос устройств",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        archive.daily_archive_job,
        trigger=CronTrigger(
            hour=settings.archive_check_hour, minute=settings.archive_check_minute
        ),
        id="archive_check",
        name="Суточная проверка архива + глубина",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    if settings.recording_check_minutes > 0:
        scheduler.add_job(
            recording.check_recording_all,
            trigger=IntervalTrigger(minutes=settings.recording_check_minutes),
            id="recording_check", name="Свежесть записи NVR",
            max_instances=1, coalesce=True, replace_existing=True,
        )
    scheduler.add_job(
        switches.poll_all_switches,
        trigger=IntervalTrigger(seconds=settings.switch_poll_seconds),
        id="switch_poll", name="Доступность коммутаторов",
        max_instances=1, coalesce=True, replace_existing=True,
    )

    if settings.watchdog_url:
        scheduler.add_job(
            watchdog.ping,
            trigger=IntervalTrigger(minutes=settings.watchdog_interval_minutes),
            id="watchdog",
            name="Watchdog-пульс",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

    scheduler.add_job(
        backup.auto_backup,
        trigger=CronTrigger(hour=3, minute=30),
        id="auto_backup",
        name="Ежедневный авто-бэкап",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    if settings.onec_enabled:
        from app.services import onec_sync

        scheduler.add_job(
            onec_sync.scheduled_sync,
            trigger=IntervalTrigger(minutes=settings.onec_sync_minutes),
            id="onec_sync",
            name="Синхронизация 1С:Отель + калибровка часов",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

    scheduler.start()
    log.info(
        "Планировщик запущен: опрос каждые %d мин, архив в %02d:%02d",
        settings.poll_interval_minutes,
        settings.archive_check_hour,
        settings.archive_check_minute,
    )


def _add_checkin_job(hour: int, minute: int) -> None:
    from app.services import checkin_ingest

    scheduler.add_job(
        checkin_ingest.scheduled_ingestion,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="checkin_ingest",
        name="Ночной ingestion заселений",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )


async def configure_checkin_job() -> None:
    """Регистрирует ночной джоб заселений, читая время из БД (AppSetting)."""
    from app.database import SessionLocal
    from app.services import appsettings

    async with SessionLocal() as s:
        hour = await appsettings.get_int(s, "checkin_job_hour", 1)
        minute = await appsettings.get_int(s, "checkin_job_minute", 0)
    _add_checkin_job(hour, minute)
    log.info("Ночной ingestion заселений: %02d:%02d", hour, minute)


def reschedule_checkin_job(hour: int, minute: int) -> None:
    """Перепланирует ночной джоб без рестарта контейнера (вызывается из UI)."""
    if scheduler.running:
        _add_checkin_job(hour, minute)
        log.info("Ночной ingestion заселений перепланирован на %02d:%02d", hour, minute)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def configure_time_sync_job() -> None:
    from app.services.timesync import load_settings, scheduled_sync

    config = await load_settings()
    if scheduler.get_job("time_sync"):
        scheduler.remove_job("time_sync")
    if config.enabled:
        hour, minute = map(int, config.time.split(":"))
        scheduler.add_job(
            scheduled_sync,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=config.timezone),
            id="time_sync", name="Синхронизация NVR по NTP",
            max_instances=1, coalesce=True, misfire_grace_time=300,
            replace_existing=True,
        )
