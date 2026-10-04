"""Стандартные MIB и расчёты на mock-ответах; реальное оборудование не требуется."""
import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.crypto import encrypt
from app.database import SessionLocal
from app.models import NetworkSwitch, SwitchEvent, utcnow
from app.services import switches
from app.services.snmp import standard as s
from app.services.snmp.client import Connection, SnmpClient, SnmpError, check_error
from app.services.snmp.counters import delta, rates, rebooted


def mib(*, poe=False, high=True, indices=(7, 101, 999)):
    values = {s.SYSTEM['sys_descr']: b'Acme managed switch', s.SYSTEM['sys_object_id']: '1.3.6.1.4.1.99999.1',
              s.SYSTEM['sys_name']: b'Camera switch', s.SYSTEM['uptime_ticks']: 100000, s.IF_NUMBER: len(indices)}
    for i in indices:
        base = {1:i, 2:f'Ethernet {i}'.encode(), 3:6, 5:100000000, 6:bytes.fromhex('001122334455'),
                7:1, 8:1, 9:200, 10:1000, 13:0, 14:0, 16:2000, 19:0, 20:0}
        x = {1:f'port-{i}'.encode(), 15:100, 17:1, 18:b'Camera entrance', 19:0}
        if high: x.update({6:1000, 10:2000})
        values.update({f'{s.IF_ENTRY}.{k}.{i}': v for k,v in base.items()})
        values.update({f'{s.IFX_ENTRY}.{k}.{i}': v for k,v in x.items()})
    if poe:
        values.update({'1.3.6.1.2.1.105.1.1.1.3.1.5':1, '1.3.6.1.2.1.105.1.1.1.6.1.5':3,
                       '1.3.6.1.2.1.105.1.1.1.11.1.5':0, '1.3.6.1.2.1.105.1.3.1.1.2.1':193,
                       '1.3.6.1.2.1.105.1.3.1.1.3.1':1, '1.3.6.1.2.1.105.1.3.1.1.4.1':82})
    return values


class Agent:
    def __init__(self, values=None, *, error=None, optional_error=None):
        self.values = values if values is not None else mib()
        self.error, self.optional_error = error, optional_error
        self.walks, self.gets = [], []
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def get(self, oids):
        self.gets.append(oids)
        if self.error: raise self.error
        return {oid:self.values[oid] for oid in oids if oid in self.values}
    async def walk(self, root, *, limit=20000, partial=False):
        self.walks.append(root)
        if self.error: raise self.error
        if self.optional_error and root == s.POE_PORTS: raise self.optional_error
        values = {oid:v for oid,v in self.values.items() if oid.startswith(root + '.')}
        return dict(list(values.items())[:limit]) if partial else values


async def test_unknown_vendor_standard_discovery_and_cached_poll():
    agent = Agent()
    first = await s.collect(agent)
    assert first['vendor'] is None
    assert first['capabilities']['interfaces'] == 'available'
    assert first['capabilities']['if_hc_counters'] == 'available'
    assert first['capabilities']['poe'] == 'unavailable'
    assert [p['if_index'] for p in first['interfaces']] == [7, 101, 999]
    assert first['interfaces'][1]['name'] == 'port-101'
    assert first['interfaces'][0]['mac_address'] == '00:11:22:33:44:55'
    agent.walks.clear()
    second = await s.collect(agent, cache=first['inventory'])
    assert not second['discovered']
    assert not agent.walks
    assert second['interfaces'] == first['interfaces']


async def test_reboot_refreshes_interface_inventory_and_optional_poe_failure_isolated():
    agent = Agent(optional_error=SnmpError('timeout', 'timeout'))
    result = await s.collect(agent, old_uptime=999999, elapsed=30)
    assert result['rebooted']
    assert result['capabilities']['poe'] == 'unknown'
    assert result['capabilities']['interfaces'] == 'available'


async def test_poe_standard_indices_are_not_ifindex_or_invented_watts():
    result = await s.collect(Agent(mib(poe=True)))
    assert result['capabilities']['poe'] == 'available'
    assert result['poe_supplies']['1']['used_w'] == 82
    assert result['poe_supplies']['1']['budget_w'] == 193
    assert result['poe_ports']['1.5']['power_w'] is None
    assert result['poe_ports']['1.5']['status'] == 3
    assert all('poe_index' not in port for port in result['interfaces'])


def test_physical_interfaces_exclude_vlan_and_loopback():
    values = mib(indices=(1,2,3))
    values[f'{s.IF_ENTRY}.3.2'] = 135
    values[f'{s.IFX_ENTRY}.17.2'] = 2
    values[f'{s.IF_ENTRY}.3.3'] = 24
    values[f'{s.IFX_ENTRY}.17.3'] = 2
    assert [p['if_index'] for p in s.parse_interfaces(values) if p['physical']] == [1]


async def test_fallback_counters_missing_optional_column_and_zeroes():
    agent = Agent(mib(high=False))
    result = await s.collect(agent)
    assert result['capabilities']['if_hc_counters'] == 'unavailable'
    assert result['interfaces'][0]['counters']['in_bits'] == 32
    del agent.values[f'{s.IF_ENTRY}.14.7']
    next_result = await s.collect(agent, cache=result['inventory'])
    assert next_result['interfaces'][0]['counters']['in_errors'] is None
    assert next_result['interfaces'][0]['counters']['out_errors'] == 0


def counters(value=1000, bits=64, discontinuity=0):
    return dict(in_bits=bits, out_bits=bits, **{'in':value, 'out':value,
      'in_errors':0,'out_errors':0,'in_discards':0,'out_discards':0,'discontinuity':discontinuity})


def test_delta_rates_rollover_reset_discontinuity_and_width():
    assert rates(counters(), counters(4000), 3, 100)['in_bps'] == 8000
    assert rates(counters(), counters(4000), 3, 100, reset=True)['in_bps'] is None
    assert rates(counters(), counters(4000, discontinuity=3), 3, 100)['in_bps'] is None
    assert rates(counters(), counters(4000, bits=32), 3, 100)['in_bps'] is None
    assert delta(100000, 200) is None
    assert delta(2**32 - 100, 200) == 300
    assert delta(2**64 - 100, 200, 64) == 300
    assert rates(counters(2**32 - 100, 32), counters(200, 32), 1, 100)['in_bps'] == 2400
    assert rates(counters(100, 32), counters(200, 32), 60, 1000)['in_bps'] is None
    assert rates(counters(), counters(2**60), 30, 100)['in_bps'] is None
    assert rates({}, counters(), 30, 100)['in_bps'] is None
    assert rates(counters(), counters(), 0, 100)['in_bps'] is None


def test_uptime_wrap_is_not_reboot():
    assert rebooted(999999, 100, 30)
    assert not rebooted(2**32 - 1000, 2000, 30)
    assert not rebooted(None, 100, 30)


@pytest.mark.parametrize('status,expected', [(6,'auth_error'), (16,'auth_error'), (2,'unavailable')])
def test_protocol_errors(status, expected):
    with pytest.raises(SnmpError) as error: check_error(None, status)
    assert error.value.status == expected


def test_timeout_is_not_false_auth_error_and_connection_repr_hides_secret():
    from pysnmp.proto import errind
    with pytest.raises(SnmpError) as error: check_error(errind.requestTimedOut, 0)
    assert error.value.status == 'timeout'
    assert 'SUPER-SECRET' not in repr(Connection('127.0.0.1', community='SUPER-SECRET'))


async def make_switch(session, **kwargs):
    switch = NetworkSwitch(name='Тест', host='192.0.2.1', snmp_enabled=True,
                           community_enc=encrypt('SUPER-SECRET'), ports=[], **kwargs)
    session.add(switch)
    await session.commit()
    return switch


async def test_poll_two_samples_state_transitions_and_event_dedup(db, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(switches, 'utcnow', lambda: now)
    agent = Agent(mib(poe=True))
    async with SessionLocal() as session:
        switch = await make_switch(session)
        async def poll():
            return await switches.poll_switch(session, switch, probe=AsyncMock(), snmp_factory=lambda _:agent)
        assert await poll()
        port = next(p for p in switch.ports if p.if_index == 7)
        assert port.in_bps is None
        assert switch.snmp_status == 'available'
        now += dt.timedelta(seconds=30)
        agent.values[s.SYSTEM['uptime_ticks']] += 3000
        agent.values[f'{s.IFX_ENTRY}.6.7'] += 30000
        agent.values[f'{s.IF_ENTRY}.8.101'] = 2
        agent.values[f'{s.IF_ENTRY}.14.7'] = 3
        agent.values[f'{s.POE_PORTS}.6.1.5'] = 4
        assert await poll()
        assert port.in_bps == 8000
        assert switches.summarize(switch, now)['ports_total'] == 3
        now += dt.timedelta(seconds=30)
        agent.values[s.SYSTEM['uptime_ticks']] += 3000
        agent.values[f'{s.IF_ENTRY}.14.7'] = 6
        assert await poll()
        kinds = list((await session.execute(select(SwitchEvent.kind))).scalars())
        assert kinds.count('port_down') == 1
        assert kinds.count('errors') == 1
        assert kinds.count('poe_failure') == 1
        assert port.poe_index is None
        assert switches.switch_health(switch, now)['state'] == 'yellow'
        now += dt.timedelta(seconds=30)
        agent.values[s.SYSTEM['uptime_ticks']] = 100
        agent.values[f'{s.IFX_ENTRY}.6.7'] = 200
        assert await poll()
        assert port.in_bps is None
        assert 'reboot' in list((await session.execute(select(SwitchEvent.kind))).scalars())


@pytest.mark.parametrize('error',[SnmpError('timeout','timeout'),SnmpError('auth_error','auth_error'),ValueError('SUPER-SECRET')])
async def test_failure_preserves_tcp_and_no_secret_in_log_or_error(db, caplog, error):
    async with SessionLocal() as session:
        switch = await make_switch(session)
        agent = Agent(error=error)
        assert await switches.poll_switch(session, switch, probe=AsyncMock(), snmp_factory=lambda _:agent)
        assert switch.reachable
        assert switch.snmp_status != 'available'
        assert 'SUPER-SECRET' not in caplog.text + (switch.snmp_error or '')


async def test_snmp_success_with_closed_web_port(db):
    async with SessionLocal() as session:
        switch = await make_switch(session)
        assert await switches.poll_switch(session, switch, probe=AsyncMock(side_effect=OSError()), snmp_factory=lambda _:Agent())
        assert switch.reachable and switch.snmp_status == 'available'


async def test_reassignment_clears_manual_mapping_and_stale_samples(db, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(switches, 'utcnow', lambda: now)
    async with SessionLocal() as session:
        switch = await make_switch(session)
        agent = Agent()
        await switches.poll_switch(session,switch,probe=AsyncMock(),snmp_factory=lambda _:agent)
        port = switch.ports[0]
        port.expected_up, port.poe_index = True, '1.5'
        await session.commit()
        now += dt.timedelta(seconds=30)
        agent.values[s.SYSTEM['uptime_ticks']] = 10
        agent.values[f'{s.IFX_ENTRY}.1.7'] = b'new-interface'
        await switches.poll_switch(session,switch,probe=AsyncMock(),snmp_factory=lambda _:agent)
        assert not port.expected_up and port.poe_index is None
        assert switches.telemetry_fresh(switch,now)
        assert not switches.telemetry_fresh(switch,now+dt.timedelta(hours=1))


async def test_poll_deadline_and_worker_isolation(db, monkeypatch):
    monkeypatch.setattr(switches.settings, 'switch_poll_timeout_seconds', .05)
    class Slow(Agent):
        async def get(self, oids): await asyncio.sleep(5)
    async with SessionLocal() as session:
        switch = await make_switch(session)
        assert await switches.poll_switch(session,switch,probe=AsyncMock(),snmp_factory=lambda _:Slow())
        assert switch.snmp_status == 'timeout'


async def test_sensors_units():
    data = {f'{s.SENSORS}.{k}.1':v for k,v in {1:8,2:9,3:1,4:423,5:1}.items()}
    assert s.parse_sensors(data)[0]['value'] == pytest.approx(42.3)


@pytest.mark.parametrize('pen,vendor',[(9,'Cisco'),(171,'D-Link'),(890,'Zyxel'),(2011,'Huawei'),
 (11863,'TP-Link'),(14988,'MikroTik'),(37496,'Dahua'),(39165,'Hikvision')])
def test_vendor_label_from_iana_enterprise_does_not_gate_monitoring(pen,vendor):
    assert s.identify({'sys_object_id':f'1.3.6.1.4.1.{pen}.1'}, {})['vendor']==vendor
    assert s.identify({'sys_object_id':'1.3.6.1.4.1.99999.1'}, {})['vendor'] is None
