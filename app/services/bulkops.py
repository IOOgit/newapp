"""Массовые операции по объектам с прогрессом.

Одна операция за раз, состояние — в памяти процесса (эфемерное: если сервер
перезапустят посреди прогона, прогресс просто исчезнет). Прогон идёт
последовательно по устройствам, чтобы прогресс был понятным «сделано N из M».
"""
from __future__ import annotations

from app.services import timesync

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from app import crud
from app.database import SessionLocal
from app.drivers import build_client
from app.models import Device, utcnow
from app.services import archive, poller, quality

log = logging.getLogger(__name__)

# action → человекочитаемое название
ACTIONS: dict[str, str] = {
    "poll": "Опрос",
    "sync_time": "Синхронизация времени",
    "archive_check": "Проверка архива (вчера)",
    "depth": "Глубина архива",
}

_state: dict = {
    "running": False,
    "action": None,
    "label": None,
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "current": None,
    "errors": [],
    "finished_at": None,
}


def status() -> dict:
    """Копия текущего состояния прогресса (для опроса из UI)."""
    return dict(_state)


async def _targets(group_id: int | None) -> list[tuple[int, str]]:
    async with SessionLocal() as session:
        query = select(Device.id, Device.name).where(Device.enabled.is_(True))
        if group_id is not None:
            query = query.where(Device.group_id == group_id)
        return [(r[0], r[1]) for r in (await session.execute(query)).all()]


async def _do(action: str, device_id: int) -> None:
    if action == "poll":
        await poller.poll_device(device_id)
    elif action == "sync_time":
        async with SessionLocal() as session:
            device = await crud.get_device(session, device_id)
            await timesync.sync_device(build_client(device))
        await poller.poll_device(device_id)  # сразу пересчитать дрейф
    elif action == "archive_check":
        target = dt.date.today() - dt.timedelta(days=1)
        await archive.check_device_archive(device_id, target)
    elif action == "depth":
        await archive.measure_device_depth(device_id)
    else:  # pragma: no cover — отсекается в start()
        raise ValueError(f"неизвестное действие: {action}")


async def _run(action: str, group_id: int | None) -> None:
    try:
        targets = await _targets(group_id)
        _state.update(total=len(targets), done=0, ok=0, failed=0, errors=[], current=None)
        for did, name in targets:
            _state["current"] = name
            try:
                await _do(action, did)
                _state["ok"] += 1
            except Exception as exc:  # noqa: BLE001 — любой сбой одного объекта не рушит прогон
                _state["failed"] += 1
                _state["errors"].append(f"{name}: {exc}")
                log.warning("Массовая операция %s для %s: %s", action, did, exc)
            _state["done"] += 1
    finally:
        _state["running"] = False
        _state["current"] = None
        _state["finished_at"] = utcnow().isoformat()


def start(action: str, group_id: int | None = None) -> bool:
    """Запускает массовую операцию в фоне. False — если уже что-то выполняется."""
    if action not in ACTIONS:
        raise ValueError(f"неизвестное действие: {action}")
    if _state["running"]:
        return False
    _state.update(
        running=True, action=action, label=ACTIONS[action],
        total=0, done=0, ok=0, failed=0, current=None, errors=[], finished_at=None,
    )
    asyncio.create_task(_run(action, group_id))
    return True
