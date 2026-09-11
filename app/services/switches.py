"""Опрос свитчей и оценка ожидаемых портов без Telegram и управляющих команд."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.crypto import decrypt
from app.database import SessionLocal
from app.drivers.snmp import SNMPClient, SNMPError, SwitchSnapshot
from app.models import NetworkSwitch, SwitchPort, utcnow

log = logging.getLogger(__name__)
_polling: set[int] = set()


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _age(value: dt.datetime | None, now: dt.datetime) -> float | None:
    return max(0, (_utc(now) - _utc(value)).total_seconds()) if value else None


def counter_delta(previous: int | None, current: int | None, bits: int,
                  elapsed: float, speed_bps: float | None = None) -> int | None:
    """Один оборот счётчика допустим только при известной физической границе."""
    if previous is None or current is None or elapsed <= 0 or bits not in (32, 64):
        return None
    modulus = 2**bits
    if not 0 <= previous < modulus or not 0 <= current < modulus:
        return None
    # При долгом интервале Counter32 мог обернуться неоднократно, даже если вырос.
    if bits == 32 and (not speed_bps or speed_bps * elapsed / 8 >= modulus):
        return None
    delta = (current - previous) % modulus
    if current < previous and not speed_bps:
        return None
    if speed_bps and delta * 8 > speed_bps * elapsed * 1.05:
        return None
    return delta


def _counter_increment(old, new) -> int | None:
    # Для ошибок/потерь неизвестна физическая граница: при уменьшении пропускаем.
    return new - old if isinstance(old, int) and isinstance(new, int) and new >= old else None


def _continuity(previous: int | None, current: int, elapsed: float | None) -> bool:
    if previous is None or elapsed is None or elapsed <= 0:
        return False
    forward = ((current - previous) % 2**32) / 100
    return abs(forward - elapsed) <= max(5.0, elapsed * 0.2)


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
    """Общая блокировка опроса и CRUD до любых ожиданий/чтений БД."""
    if switch_id in _polling:
        raise SwitchBusy
    _polling.add(switch_id)
    try:
        yield
    finally:
        _polling.discard(switch_id)


async def poll_switch(session: AsyncSession, switch: NetworkSwitch, *, client=None) -> bool:
    """Один атомарный снимок; старые показания сохраняются с явной ошибкой опроса."""
    if switch.id in _polling or not switch.enabled:
        return False
    _polling.add(switch.id)
    own_client = client is None
    try:
        # ORM мог быть загружен до захвата блокировки, а настройки уже изменены.
        await session.refresh(switch)
        await session.refresh(switch, attribute_names=["ports"])
        if not switch.enabled:
            return False
        previous_seen, previous_uptime = switch.last_seen, switch.uptime_ticks
        switch.last_attempt_at = utcnow()
        await session.commit()
        if client is None:
            community = decrypt(switch.community_enc)
            if not community:
                raise SNMPError("Не удалось прочитать сохранённый community")
            client = SNMPClient(switch.host, community, port=switch.snmp_port,
                                timeout=switch.timeout, retries=switch.retries)
        async with asyncio.timeout(settings.switch_poll_timeout_seconds):
            snapshot: SwitchSnapshot = await client.collect()
        observed_at = utcnow()
        elapsed = _age(previous_seen, observed_at)
        continuous = switch.reachable and _continuity(previous_uptime, snapshot.uptime_ticks, elapsed)
        old_ports = {p.if_index: p for p in switch.ports}
        for port in old_ports.values():
            port.present = False
            _clear_rates(port)
        for index, data in snapshot.ports.items():
            port = old_ports.get(index)
            if port is None:
                port = SwitchPort(switch_id=switch.id, if_index=index)
                switch.ports.append(port)
            previous = dict(port.counters or {})
            current = dict(data.get("counters") or {})
            # Назначение камеры подтверждается именем интерфейса, не одним ifIndex.
            if previous.get("mapping_name"):
                current["mapping_name"] = previous["mapping_name"]
            same_interface = port.name == data.get("name")
            same_counters = previous.get("bits") == current.get("bits")
            no_reset = previous.get("discontinuity") == current.get("discontinuity")
            speed = data.get("speed_mbps")
            if continuous and same_interface and same_counters and no_reset and elapsed:
                for direction in ("in", "out"):
                    delta = counter_delta(previous.get(f"{direction}_octets"),
                                          current.get(f"{direction}_octets"),
                                          current.get("bits", 32), elapsed,
                                          speed * 1_000_000 if speed else None)
                    setattr(port, f"{direction}_bps", delta * 8 / elapsed if delta is not None else None)
                    for name, attr in (("errors", "error"), ("discards", "discard")):
                        setattr(port, f"{direction}_{attr}_delta", _counter_increment(
                            previous.get(f"{direction}_{name}"), current.get(f"{direction}_{name}")))
            for key in ("name", "description", "alias", "admin_status", "oper_status",
                        "speed_mbps", "last_change_ticks"):
                setattr(port, key, data.get(key))
            port.counters = current
            port.present, port.observed_at = True, observed_at
        switch.sys_name, switch.sys_descr = snapshot.sys_name, snapshot.sys_descr
        switch.sys_object_id, switch.uptime_ticks = snapshot.sys_object_id, snapshot.uptime_ticks
        switch.capabilities = snapshot.capabilities
        switch.poe_ports, switch.poe_supplies = snapshot.poe_ports, snapshot.poe_supplies
        switch.reachable, switch.last_seen, switch.last_error = True, observed_at, None
        await session.commit()
        return True
    except (SNMPError, TimeoutError, OSError, ValueError) as exc:
        switch.reachable = False
        # Не сохраняем текст стороннего исключения: он может содержать credentials.
        switch.last_error = str(exc) if isinstance(exc, SNMPError) else (
            "Превышено время опроса SNMP" if isinstance(exc, TimeoutError)
            else "Ошибка соединения или ответа SNMP")
        for port in switch.ports:
            _clear_rates(port)
        await session.commit()
        return False
    except Exception:
        switch.reachable = False
        switch.last_error = "Не удалось обработать ответ SNMP"
        for port in switch.ports:
            _clear_rates(port)
        await session.commit()
        log.warning("Не удалось обработать опрос свитча id=%s", switch.id)
        return False
    finally:
        _polling.discard(switch.id)
        if own_client and client is not None:
            client.close()


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
    now = now or utcnow()
    stale = (_age(port.observed_at, now) or 0) > max(120, settings.switch_poll_seconds * 3)
    expected = port.expected_up or port.channel_ref_id is not None
    if not switch.enabled:
        return dict(state="gray", label="Опрос выключен", reasons=[], stale=stale)
    switch_age = _age(switch.last_seen, now)
    switch_stale = switch_age is None or switch_age > max(120, settings.switch_poll_seconds * 3)
    if not switch.reachable or switch_stale or (port.present and (not port.observed_at or stale)):
        return dict(state="gray", label="Нет свежих данных", reasons=[], stale=True)
    reasons, state = [], "yellow"
    if expected and not port.present:
        if (switch.capabilities or {}).get("interfaces") == "supported":
            reasons.append("Ожидаемый интерфейс исчез из таблицы SNMP")
            state = "red"
        else:
            reasons.append("Таблица интерфейсов недоступна; порт не проверен")
    elif expected and port.admin_status == 2:
        reasons.append("Ожидаемый порт административно выключен")
        state = "red"
    elif expected and port.oper_status in (2, 6, 7):
        reasons.append("Нет линка ожидаемого порта")
        state = "red"
    elif expected and (port.admin_status != 1 or port.oper_status != 1):
        reasons.append("Статус ожидаемого порта неизвестен или идёт проверка")
    mapping_name = (port.counters or {}).get("mapping_name")
    if expected and mapping_name and mapping_name != port.name:
        reasons.append("Имя интерфейса изменилось; проверьте назначение камеры")
    poe = (switch.poe_ports or {}).get(port.poe_index) if port.poe_index else None
    if expected and port.poe_index:
        if not poe or poe.get("detection_status") is None:
            reasons.append("Нет подтверждения питания назначенного PoE-порта")
        elif poe.get("detection_status") == 5:
            reasons.append("Назначенный PoE-порт проходит проверку")
        elif poe.get("detection_status") != 3:
            reasons.append("Назначенный PoE-порт не подаёт питание")
            state = "red"
    if reasons:
        return dict(state=state, label="Проблема порта" if state == "red" else "Требует проверки", reasons=reasons, stale=False)
    if any((getattr(port, field) or 0) > 0 for field in (
        "in_error_delta", "out_error_delta", "in_discard_delta", "out_discard_delta"
    )):
        return dict(state="yellow", label="Растут ошибки / потери", reasons=["Счётчики выросли с прошлого опроса"], stale=False)
    if port.oper_status == 1:
        return dict(state="green", label="Линк есть", reasons=[], stale=False)
    return dict(state="gray", label="Свободный / не контролируется", reasons=[], stale=False)


def switch_health(switch: NetworkSwitch, now=None) -> dict:
    now = now or utcnow()
    age = _age(switch.last_seen, now)
    stale = age is None or age > max(120, settings.switch_poll_seconds * 3)
    if not switch.enabled:
        return dict(state="gray", label="Опрос выключен", reasons=[], stale=stale)
    if switch.last_attempt_at is None:
        return dict(state="gray", label="Ещё не проверен", reasons=[], stale=True)
    if not switch.reachable:
        return dict(state="red", label="SNMP недоступен", reasons=[switch.last_error or "Нет ответа SNMP"], stale=stale)
    if stale:
        return dict(state="gray", label="Данные устарели", reasons=["Нет свежего успешного опроса"], stale=True)
    reasons = []
    state = "yellow"
    if (switch.capabilities or {}).get("interfaces") != "supported":
        reasons.append("Состояния портов недоступны по IF-MIB")
    expected_ports = [p for p in switch.ports if p.expected_up or p.channel_ref_id is not None]
    if not expected_ports:
        reasons.append("Ожидаемые порты не назначены")
    for port in expected_ports:
        health = port_health(port, switch, now)
        if health["state"] in ("red", "yellow", "gray"):
            reasons.extend(f"{port.name or port.if_index}: {reason}" for reason in
                           (health["reasons"] or [health["label"]]))
            if health["state"] == "red":
                state = "red"
    for group, supply in (switch.poe_supplies or {}).items():
        if supply.get("oper_status") in (2, 3):
            reasons.append(f"PoE-блок {group}: питание выключено или неисправно")
            state = "red"
    if any(value == "error" for value in (switch.capabilities or {}).values()):
        reasons.append("Часть дополнительных таблиц SNMP не удалось прочитать")
    return dict(state=state if reasons else "green", label="Требует внимания" if reasons else "Ожидаемые порты в норме",
                reasons=reasons, stale=False)
