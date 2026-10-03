"""Фоновый TCP/SNMP-мониторинг, история и оценка состояния коммутаторов."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from contextlib import contextmanager

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.crypto import decrypt
from app.database import SessionLocal
from app.models import NetworkSwitch, SwitchEvent, SwitchPort, SwitchTelemetry, utcnow
from app.services.snmp import standard
from app.services.snmp.client import Connection, SnmpClient, SnmpError
from app.services.snmp.counters import rates

log = logging.getLogger(__name__)
_polling: set[int] = set()
_pending: set[int] = set()
_tasks: set[asyncio.Task] = set()
_jobs: dict[str, dict] = {}
_limiter = None
_limiter_loop = None


def _utc(value):
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _age(value, now):
    return max(0, (_utc(now) - _utc(value)).total_seconds()) if value else None


def _clear_rates(port):
    port.in_bps = port.out_bps = None
    port.in_error_delta = port.out_error_delta = None
    port.in_discard_delta = port.out_discard_delta = None


def is_polling(switch_id):
    return switch_id in _polling or switch_id in _pending


class SwitchBusy(Exception):
    """Опрос или изменение свитча уже выполняется."""


@contextmanager
def claim_switch(switch_id):
    if is_polling(switch_id):
        raise SwitchBusy
    _polling.add(switch_id)
    try:
        yield
    finally:
        _polling.discard(switch_id)


def _semaphore():
    global _limiter, _limiter_loop
    loop = asyncio.get_running_loop()
    if _limiter_loop is not loop:
        _limiter, _limiter_loop = asyncio.Semaphore(settings.switch_max_concurrent_polls), loop
    return _limiter


def connection(switch):
    return Connection(host=switch.host, port=switch.snmp_port, version=switch.snmp_version,
                      community=decrypt(switch.community_enc), timeout=switch.timeout, retries=switch.retries)


async def tcp_probe(host, port, timeout):
    _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout)
    finally:
        writer.close()


def _event(session, switch, kind, message, if_index=None):
    session.add(SwitchEvent(switch_id=switch.id, kind=kind, message=message, if_index=if_index))


def _failure(switch, error):
    # Только коды и безопасные сообщения; исключения транспорта могут содержать секрет.
    if switch.snmp_status != error.status or switch.snmp_error != str(error):
        log.warning("SNMP id=%s: %s", switch.id, error.code)
    switch.snmp_status, switch.snmp_error = error.status, str(error)
    switch.snmp_error_at = utcnow()
    for port in switch.ports:
        _clear_rates(port)
        port.counters = {}  # после пропуска измерения нужен новый baseline


def _apply(session, switch, snapshot, now):
    elapsed = _age(switch.snmp_last_seen, now)
    reset = snapshot['rebooted']
    if reset:
        _event(session, switch, 'reboot', 'Обнаружен перезапуск SNMP-агента или устройства')
        log.info('Перезапуск SNMP id=%s', switch.id)
    old_ports = {p.if_index: p for p in switch.ports}
    seen = set()
    for item in snapshot['interfaces']:
        index = item['if_index']
        seen.add(index)
        port = old_ports.get(index)
        if port is None:
            port = SwitchPort(switch_id=switch.id, if_index=index)
            switch.ports.append(port)
        previous = dict(port.counters or {})
        identity_changed = bool(port.name and item['name'] and port.name != item['name'])
        # Переназначение ifIndex не должно привязывать старую камеру к чужому порту.
        if identity_changed:
            port.channel_ref_id, port.poe_index, port.expected_up = None, None, False
            _event(session, switch, 'mapping_changed', f'Изменилось имя интерфейса ifIndex {index}; проверьте привязку', index)
        if (not reset and not identity_changed and port.present and port.oper_status in (1, 2)
                and item['oper_status'] in (1, 2) and port.oper_status != item['oper_status']):
            up = item['oper_status'] == 1
            _event(session, switch, 'port_up' if up else 'port_down',
                   f"{item['name'] or item['description'] or 'ifIndex ' + str(index)}: {'линк поднят' if up else 'линк отключён'}", index)
        calculated = rates(previous, item['counters'], elapsed, item['speed_mbps'], reset=reset or identity_changed)
        growing = any((calculated[k] or 0) > 0 for k in ('in_error_delta', 'out_error_delta'))
        if growing and not previous.get('error_growth'):
            _event(session, switch, 'errors', f'Рост ошибок интерфейса ifIndex {index}', index)
        item['counters']['error_growth'] = growing
        for key, value in {**item, **calculated}.items():
            setattr(port, key, value)
        port.present, port.observed_at = True, now
    for index, port in old_ports.items():
        if index not in seen:
            if port.present:
                _event(session, switch, 'port_removed', f'Интерфейс ifIndex {index} исчез из таблицы агента', index)
            port.present = False
            _clear_rates(port)
            port.counters = {}
    for index, data in snapshot['poe_ports'].items():
        previous = (switch.poe_ports or {}).get(index, {})
        if data.get('status') in (4, 6) and previous.get('status') not in (4, 6):
            _event(session, switch, 'poe_failure', f'Ошибка PoE, индекс {index}')
    for key, value in snapshot['system'].items():
        setattr(switch, key, value)
    for key in ('vendor', 'detected_model', 'firmware', 'capabilities', 'inventory', 'poe_ports', 'poe_supplies'):
        setattr(switch, key, snapshot.get(key))
    switch.snmp_status, switch.snmp_error, switch.snmp_last_seen = 'available', None, now
    if snapshot['discovered']:
        switch.discovery_at = now
        log.info('Обновлено сопоставление интерфейсов SNMP id=%s, интерфейсов=%s', switch.id, len(seen))
        if snapshot['diagnostics']:
            log.info('Необязательные MIB id=%s: %s', switch.id, snapshot['diagnostics'])


async def poll_switch(session, switch, *, probe=None, snmp_factory=None):
    if switch.id in _polling or not switch.enabled:
        return False
    switch_id = switch.id
    _polling.add(switch_id)
    try:
        await session.refresh(switch)
        await session.refresh(switch, attribute_names=['ports'])
        if not switch.enabled:
            return False
        previous_reachable, had_attempt = switch.reachable, switch.last_attempt_at is not None
        now = utcnow()
        switch.last_attempt_at = now
        # TCP и SNMP независимы: отказ одного протокола не отменяет другой.
        try:
            async with asyncio.timeout(switch.timeout + 1):
                await (probe or tcp_probe)(switch.host, switch.management_port, switch.timeout)
            switch.reachable, switch.last_seen, switch.last_error = True, now, None
        except (TimeoutError, OSError):
            switch.reachable = False
            switch.last_error = f'Веб-интерфейс не отвечает по TCP-порту {switch.management_port}'
        if switch.snmp_enabled:
            try:
                async with asyncio.timeout(settings.switch_poll_timeout_seconds):
                    async with (snmp_factory or SnmpClient)(connection(switch)) as client:
                        snapshot = await standard.collect(client, cache=switch.inventory,
                            old_uptime=switch.uptime_ticks, elapsed=_age(switch.snmp_last_seen, now),
                            discover=switch.discovery_at is None or _age(switch.discovery_at, now) >= settings.switch_discovery_seconds)
                _apply(session, switch, snapshot, utcnow())
                switch.reachable, switch.last_seen = True, utcnow()
            except SnmpError as error:
                _failure(switch, error)
            except TimeoutError:
                _failure(switch, SnmpError('timeout', 'timeout'))
            except Exception:
                _failure(switch, SnmpError())
        else:
            switch.snmp_status = 'unknown'
            for port in switch.ports:
                _clear_rates(port)
        if had_attempt and previous_reachable != switch.reachable:
            _event(session, switch, 'online' if switch.reachable else 'offline',
                   'Коммутатор доступен' if switch.reachable else 'Коммутатор недоступен')
        latest = (await session.execute(select(SwitchTelemetry.created_at)
                  .where(SwitchTelemetry.switch_id == switch.id).order_by(SwitchTelemetry.created_at.desc()).limit(1))).scalar()
        if latest is None or _age(latest, now) >= settings.switch_telemetry_seconds:
            summary = summarize(switch, now)
            session.add(SwitchTelemetry(switch_id=switch.id, created_at=now, data={
                **summary, 'online': switch.reachable, 'snmp_status': switch.snmp_status,
                'ports': [{'if_index': p.if_index, 'up': p.oper_status == 1 if summary['fresh'] else None,
                           'in_bps': p.in_bps, 'out_bps': p.out_bps, 'errors': _sum([p.in_error_delta, p.out_error_delta]),
                           'poe_w': (switch.poe_ports or {}).get(p.poe_index, {}).get('power_w') if summary['fresh'] else None}
                          for p in switch.ports if p.present and p.physical]}))
        await session.commit()
        return switch.reachable
    except Exception:
        await session.rollback()
        log.error('Не удалось сохранить опрос коммутатора id=%s', switch_id)
        return False
    finally:
        _polling.discard(switch_id)


async def _run_one(switch_id):
    async with _semaphore(), SessionLocal() as session:
        _pending.discard(switch_id)
        switch = (await session.execute(select(NetworkSwitch).options(selectinload(NetworkSwitch.ports))
                                        .where(NetworkSwitch.id == switch_id))).scalar_one_or_none()
        if switch is not None:
            return await poll_switch(session, switch)
    return False


async def poll_all_switches():
    async with SessionLocal() as session:
        ids = iter((await session.execute(select(NetworkSwitch.id).where(NetworkSwitch.enabled.is_(True)))).scalars().all())
    async def worker():
        for switch_id in ids:
            if is_polling(switch_id):
                continue
            try:
                await _run_one(switch_id)
            except Exception:
                log.error('Фоновый опрос id=%s завершился ошибкой', switch_id)
    await asyncio.gather(*(worker() for _ in range(settings.switch_max_concurrent_polls)))
    async with SessionLocal() as session:
        await session.execute(delete(SwitchTelemetry).where(SwitchTelemetry.created_at < utcnow() - dt.timedelta(days=settings.switch_telemetry_days)))
        await session.execute(delete(SwitchEvent).where(SwitchEvent.created_at < utcnow() - dt.timedelta(days=settings.switch_event_days)))
        await session.commit()


def _start_job(run, owner_id):
    now = utcnow()
    for key, value in list(_jobs.items()):
        if _age(value['created_at'], now) > 600 and value['state'] != 'running':
            del _jobs[key]
    if len(_tasks) >= settings.switch_max_concurrent_polls * 4 or len(_jobs) >= 200:
        raise SwitchBusy
    key = uuid.uuid4().hex
    job = _jobs[key] = dict(state='running', result=None, created_at=now, owner_id=owner_id)
    async def execute():
        try:
            job['result'] = await run()
            job['state'] = 'done'
        except asyncio.CancelledError:
            job['state'] = 'cancelled'
            raise
        except Exception:
            job['result'], job['state'] = {'snmp_status': 'unavailable', 'error': 'Не удалось выполнить проверку'}, 'done'
    task = asyncio.create_task(execute())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return key


def queue_poll(switch_id, owner_id=None):
    if is_polling(switch_id):
        raise SwitchBusy
    async def run():
        try:
            return {'ok': await _run_one(switch_id)}
        finally:
            _pending.discard(switch_id)
    key = _start_job(run, owner_id)
    _pending.add(switch_id)
    return key


def queue_probe(config, owner_id=None):
    async def run():
        async with _semaphore():
            try:
                async with asyncio.timeout(settings.switch_poll_timeout_seconds):
                    async with SnmpClient(config) as client:
                        system = await standard.probe(client)
                return dict(snmp_status='available', **system, **standard.identify(system, {}))
            except SnmpError as error:
                return {'snmp_status': error.status, 'error': str(error)}
            except TimeoutError:
                return {'snmp_status': 'timeout', 'error': str(SnmpError('timeout', 'timeout'))}
    return _start_job(run, owner_id)


def get_job(key, owner_id):
    job = _jobs.get(key)
    if job is None or job['owner_id'] != owner_id:
        return None
    return {k: job[k] for k in ('state', 'result')}


async def stop_jobs():
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _pending.clear()


def _sum(values):
    values = list(values)
    return sum(values) if values and all(v is not None for v in values) else None


def telemetry_fresh(switch, now=None):
    age = _age(switch.snmp_last_seen, now or utcnow())
    return bool(switch.enabled and switch.snmp_enabled and switch.snmp_status == 'available'
                and age is not None and age <= max(120, settings.switch_poll_seconds * 3))


def summarize(switch, now=None):
    fresh = telemetry_fresh(switch, now)
    ports = [p for p in switch.ports if p.present and p.physical]
    supplies = list((switch.poe_supplies or {}).values()) if fresh else []
    return dict(fresh=fresh, ports_total=len(ports) if fresh and ports else None,
                ports_up=sum(p.oper_status == 1 for p in ports) if fresh and ports and all(p.oper_status is not None for p in ports) else None,
                in_bps=_sum(p.in_bps for p in ports) if fresh else None,
                out_bps=_sum(p.out_bps for p in ports) if fresh else None,
                errors=_sum(v for p in ports for v in (p.in_error_delta, p.out_error_delta)) if fresh else None,
                poe_active=sum(p.get('status') == 3 for p in switch.poe_ports.values()) if fresh and switch.poe_ports and all(p.get('status') is not None for p in switch.poe_ports.values()) else None,
                poe_used_w=_sum(p.get('used_w') for p in supplies), poe_budget_w=_sum(p.get('budget_w') for p in supplies))


def port_health(port, switch, now=None):
    if not port.present or not telemetry_fresh(switch, now):
        return dict(state='gray', label='Нет свежих данных', reasons=[], stale=True)
    poe = (switch.poe_ports or {}).get(port.poe_index, {})
    if poe.get('status') in (4, 6):
        return dict(state='red', label='Ошибка PoE', reasons=['Агент сообщил неисправность PoE'], stale=False)
    if any((n or 0) > 0 for n in (port.in_error_delta, port.out_error_delta, port.in_discard_delta, port.out_discard_delta)):
        return dict(state='yellow', label='Ошибки / потери', reasons=['Рост счётчиков за последний интервал'], stale=False)
    if port.oper_status == 1:
        return dict(state='green', label='Линк поднят', reasons=[], stale=False)
    if port.oper_status == 2:
        return dict(state='red' if port.expected_up else 'gray', label='Нет линка', reasons=[], stale=False)
    return dict(state='gray', label='Неизвестно', reasons=[], stale=False)


def switch_health(switch, now=None):
    now = now or utcnow()
    age = _age(switch.last_seen, now)
    stale = age is None or age > max(120, settings.switch_poll_seconds * 3)
    if not switch.enabled:
        return dict(state='gray', label='Проверка выключена', reasons=[], stale=stale)
    if switch.last_attempt_at is None:
        return dict(state='gray', label='Ещё не проверен', reasons=[], stale=True)
    if not switch.reachable:
        return dict(state='red', label='Офлайн', reasons=[switch.last_error or 'Нет ответа'], stale=stale)
    if stale:
        return dict(state='gray', label='Данные устарели', reasons=[], stale=True)
    if switch.snmp_enabled and not telemetry_fresh(switch, now):
        return dict(state='yellow', label='Онлайн', reasons=['SNMP: нет свежих данных'], stale=False)
    problems = [p for p in switch.ports if port_health(p, switch, now)['state'] in ('red', 'yellow')]
    reasons = [f'Портов с проблемами: {len(problems)}'] if problems else []
    if telemetry_fresh(switch, now) and (any(p.get('status') in (4, 6) for p in (switch.poe_ports or {}).values())
                                       or any(p.get('status') == 3 for p in (switch.poe_supplies or {}).values())):
        reasons.append('Агент сообщил ошибку PoE')
    return dict(state='yellow' if reasons else 'green', label='Онлайн', reasons=reasons, stale=False)
