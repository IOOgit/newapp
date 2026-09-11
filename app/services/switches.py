"""Честный контроль доступности коммутаторов без неподтверждённой телеметрии."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import NetworkSwitch, SwitchPort, utcnow

log = logging.getLogger(__name__)
_polling: set[int] = set()


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _age(value: dt.datetime | None, now: dt.datetime) -> float | None:
    return max(0, (_utc(now) - _utc(value)).total_seconds()) if value else None


def _clear_rates(port: SwitchPort) -> None:
    port.in_bps = port.out_bps = None
    port.in_error_delta = port.out_error_delta = None
    port.in_discard_delta = port.out_discard_delta = None


def is_polling(switch_id: int) -> bool:
    return switch_id in _polling


class SwitchBusy(Exception):
    """Опрос или изменение свитча уже выполняется."""


@contextmanager
def claim_switch(switch_id: int):
    if switch_id in _polling:
        raise SwitchBusy
    _polling.add(switch_id)
    try:
        yield
    finally:
        _polling.discard(switch_id)


async def tcp_probe(host: str, port: int, timeout: float) -> None:
    """Проверяет соединение, не выдавая доступность за телеметрию портов."""
    _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    writer.close()
    await writer.wait_closed()


async def poll_switch(session, switch: NetworkSwitch, *, probe=None) -> bool:
    if switch.id in _polling or not switch.enabled:
        return False
    _polling.add(switch.id)
    try:
        await session.refresh(switch)
        await session.refresh(switch, attribute_names=["ports"])
        if not switch.enabled:
            return False
        switch.last_attempt_at = utcnow()
        await session.commit()
        await (probe or tcp_probe)(switch.host, switch.snmp_port, switch.timeout)
        switch.reachable = True
        switch.last_seen = utcnow()
        switch.last_error = None
        switch.capabilities = {
            "reachability": "tcp",
            "interfaces": "unavailable",
            "poe_ports": "unavailable",
            "reason": "У модели нет SNMP; проверяется только TCP-доступность веб-интерфейса",
        }
        switch.sys_name = switch.sys_descr = switch.sys_object_id = None
        switch.uptime_ticks = None
        switch.poe_ports = {}
        switch.poe_supplies = {}
        for port in switch.ports:
            port.present = False
            _clear_rates(port)
        await session.commit()
        return True
    except (TimeoutError, asyncio.TimeoutError, OSError):
        switch.reachable = False
        switch.last_error = f"Веб-интерфейс не отвечает по TCP-порту {switch.snmp_port}"
        for port in switch.ports:
            port.present = False
            _clear_rates(port)
        await session.commit()
        return False
    except Exception:
        switch.reachable = False
        switch.last_error = "Не удалось выполнить проверку доступности"
        await session.commit()
        log.exception("Ошибка проверки доступности свитча id=%s", switch.id)
        return False
    finally:
        _polling.discard(switch.id)


async def poll_all_switches() -> None:
    async with SessionLocal() as session:
        ids = list((await session.execute(
            select(NetworkSwitch.id).where(NetworkSwitch.enabled.is_(True))
        )).scalars())
    semaphore = asyncio.Semaphore(max(1, settings.switch_max_concurrent_polls))

    async def run(switch_id: int):
        async with semaphore, SessionLocal() as session:
            switch = (await session.execute(select(NetworkSwitch).options(
                selectinload(NetworkSwitch.ports)).where(NetworkSwitch.id == switch_id)
            )).scalar_one_or_none()
            if switch is not None:
                await poll_switch(session, switch)
    await asyncio.gather(*(run(switch_id) for switch_id in ids))


def port_health(port: SwitchPort, switch: NetworkSwitch, now=None) -> dict:
    return dict(state="gray", label="Нет телеметрии портов", reasons=[
        "DH-CS4226-24ET-240 не предоставляет SNMP; состояние порта не проверяется"
    ], stale=True)


def switch_health(switch: NetworkSwitch, now=None) -> dict:
    now = now or utcnow()
    age = _age(switch.last_seen, now)
    stale = age is None or age > max(120, settings.switch_poll_seconds * 3)
    if not switch.enabled:
        return dict(state="gray", label="Проверка выключена", reasons=[], stale=stale)
    if switch.last_attempt_at is None:
        return dict(state="gray", label="Ещё не проверен", reasons=[
            "Будет проверена только доступность веб-интерфейса"
        ], stale=True)
    if not switch.reachable:
        return dict(state="red", label="Недоступен", reasons=[
            switch.last_error or "Нет TCP-соединения с веб-интерфейсом"
        ], stale=stale)
    if stale:
        return dict(state="gray", label="Данные устарели", reasons=[
            "Нет свежей успешной проверки доступности"
        ], stale=True)
    return dict(state="yellow", label="Доступен · без телеметрии", reasons=[
        "Порты, PoE, трафик и ошибки не контролируются: у модели нет SNMP"
    ], stale=False)
