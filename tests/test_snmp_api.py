"""Настройки, фоновая проверка, безопасность API и миграция существующих данных."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.crypto import decrypt
from app.database import SessionLocal, _lightweight_migrate
from app.models import NetworkSwitch, SwitchPort, SwitchTelemetry, SwitchEvent, utcnow
from app.services import switches
from tests.test_snmp import Agent


def client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')


async def test_secret_encrypted_not_returned_or_rendered_and_blank_preserves(db):
    async with client() as c:
        created = await c.post('/api/switches',json={'name':'DES','model':'DES-1210-28P', 'host':'192.0.2.9',
                            'snmp_enabled':True,'community':'HIDDEN-secret','management_port':8080})
        assert created.status_code == 201
        result = created.json()
        sid = result['id']
        assert 'HIDDEN-secret' not in created.text and 'community_enc' not in created.text
        assert result['snmp_port'] == 161 and result['management_port'] == 8080
        response = await c.put(f'/api/switches/{sid}',json={'name':'Новое имя'})
        assert response.status_code == 200
        listing = await c.get('/api/switches')
        card = await c.get(f'/switches/{sid}')
        assert card.status_code == 200
        assert 'HIDDEN-secret' not in listing.text + card.text
        assert 'enc:' not in card.text
        async with SessionLocal() as session:
            switch = await session.get(NetworkSwitch,sid)
            assert switch.community_enc.startswith('enc:')
            assert decrypt(switch.community_enc) == 'HIDDEN-secret'
            switch.snmp_status, switch.snmp_last_seen = 'available', utcnow()
            await session.commit()
        unchanged = await c.put(f'/api/switches/{sid}',json={'name':'Только переименование',
                       'host':'192.0.2.9','snmp_enabled':True,'snmp_port':161,'snmp_version':'2c','management_port':8080})
        assert unchanged.json()['snmp_status'] == 'available'
        assert unchanged.json()['snmp_last_seen'] is not None
        bad = await c.put(f'/api/switches/{sid}',json={'community':'HIDDEN-secret','snmp_port':0})
        assert bad.status_code == 422 and 'HIDDEN-secret' not in bad.text
        assert (await c.put(f'/api/switches/{sid}',json={'community':''})).status_code == 422


async def test_community_required_and_model_is_free_text(db):
    async with client() as c:
        assert (await c.post('/api/switches',json={'name':'x','host':'192.0.2.1','snmp_enabled':True})).status_code == 422
        assert (await c.post('/api/switches',json={'name':'x','host':'http://localhost','community':'hidden'})).status_code == 422
        assert (await c.post('/api/switches',json={'name':'x','host':'192.0.2.1','snmp_version':'3'})).status_code == 422
        response = await c.post('/api/switches',json={'name':'x','host':'192.0.2.1','model':'DS-H332/2Q(B)'})
        assert response.status_code == 201 and response.json()['model'] == 'DS-H332/2Q(B)'


async def test_probe_returns_202_before_network_completes_and_is_private(db, monkeypatch):
    release = asyncio.Event()
    class WaitingAgent(Agent):
        async def get(self, oids):
            await release.wait()
            return await super().get(oids)
    monkeypatch.setattr(switches,'SnmpClient',lambda _:WaitingAgent())
    try:
        async with client() as c:
            response = await asyncio.wait_for(c.post('/api/switches/probe',json={'host':'192.0.2.1','community':'hidden'}),1)
            assert response.status_code == 202
            key = response.json()['job_id']
            assert switches.get_job(key, -1) is None
            assert (await c.get('/api/switches/jobs/'+key)).json()['state'] == 'running'
            release.set()
            for _ in range(20):
                response = await c.get('/api/switches/jobs/'+key)
                if response.json()['state'] == 'done': break
                await asyncio.sleep(.01)
            assert response.json()['result']['snmp_status'] == 'available'
            assert 'hidden' not in response.text
    finally:
        release.set()
        await switches.stop_jobs()


async def test_get_pages_never_poll_and_secret_omitted_with_telemetry(db, monkeypatch):
    monkeypatch.setattr(switches,'SnmpClient',lambda _:pytest.fail('GET не должен открывать сеть'))
    async with SessionLocal() as session:
        sw = NetworkSwitch(name='D-Link',host='192.0.2.1',snmp_enabled=True,reachable=True,
                 snmp_status='available',snmp_last_seen=utcnow(),last_seen=utcnow(),last_attempt_at=utcnow(),
                 capabilities={'interfaces':'available','poe':'unavailable'},ports=[])
        session.add(sw)
        await session.flush()
        sw.ports.append(SwitchPort(if_index=101,name='<script>alert(1)</script>',alias='Вход',physical=True,
                                   oper_status=1,admin_status=1,in_bps=8000,out_bps=16000,present=True))
        session.add(SwitchTelemetry(switch_id=sw.id,data={'in_bps':8000,'out_bps':16000,'ports':[]}))
        await session.commit()
        sid=sw.id
    async with client() as c:
        for path in ('/', '/switches',f'/switches/{sid}',f'/api/switches/{sid}',f'/api/switches/{sid}/history'):
            r=await c.get(path)
            assert r.status_code == 200
            if path==f'/switches/{sid}':
                assert '&lt;script&gt;alert(1)&lt;/script&gt;' in r.text
                assert '<script>alert(1)</script>' not in r.text
                assert 'data-sw-tab="poe"' not in r.text
                assert '8.0 Kbit/s' in r.text


async def test_legacy_migration_preserves_tcp_snmp_secrets_and_is_idempotent():
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE network_switches (id INTEGER PRIMARY KEY,model TEXT,snmp_port INTEGER,snmp_version TEXT,community_enc TEXT)"))
            await conn.execute(text("INSERT INTO network_switches VALUES (1,'DH-CS4226-24ET-240',8443,'none',''),(2,'DES-1210-28P',1161,'2c','enc:preserved')"))
            await conn.run_sync(_lightweight_migrate)
            values=(await conn.execute(text('SELECT management_port,snmp_port,snmp_version,snmp_enabled,community_enc FROM network_switches ORDER BY id'))).all()
            assert tuple(values[0])==(8443,161,'2c',0,'')
            assert tuple(values[1])==(80,1161,'2c',1,'enc:preserved')
            await conn.execute(text("UPDATE network_switches SET snmp_enabled=TRUE,community_enc='enc:new',snmp_port=2161 WHERE id=1"))
            await conn.run_sync(_lightweight_migrate)
            row=(await conn.execute(text('SELECT snmp_enabled,community_enc,snmp_port FROM network_switches WHERE id=1'))).one()
            assert tuple(row)==(1,'enc:new',2161)
    finally:
        await engine.dispose()


async def test_background_pool_bounded_and_failure_does_not_stop_others(db, monkeypatch):
    async with SessionLocal() as session:
        session.add_all(NetworkSwitch(name=str(i),host='192.0.2.1') for i in range(9))
        await session.commit()
    seen, running, maximum = [], 0, 0
    async def fake(session, switch):
        nonlocal running, maximum
        seen.append(switch.id)
        running+=1; maximum=max(maximum,running)
        try:
            await asyncio.sleep(.01)
            if switch.id==1: raise RuntimeError('simulated')
        finally: running-=1
    monkeypatch.setattr(switches,'poll_switch',fake)
    monkeypatch.setattr(switches.settings,'switch_max_concurrent_polls',2)
    await switches.poll_all_switches()
    assert len(seen)==9 and maximum<=2


async def test_history_retention_and_deleted_switch_cleans_children(db):
    import datetime as dt
    async with SessionLocal() as session:
        sw=NetworkSwitch(name='off',host='192.0.2.1',enabled=False)
        session.add(sw); await session.flush()
        session.add_all([SwitchTelemetry(switch_id=sw.id,data={},created_at=utcnow()-dt.timedelta(days=40)),
                         SwitchEvent(switch_id=sw.id,kind='online',message='old',created_at=utcnow()-dt.timedelta(days=40))])
        await session.commit(); sid=sw.id
    await switches.poll_all_switches()
    async with SessionLocal() as session:
        assert not (await session.execute(select(SwitchTelemetry))).all()
        assert not (await session.execute(select(SwitchEvent))).all()
        session.add(SwitchEvent(switch_id=sid,kind='online',message='new')); await session.commit()
    async with client() as c: assert (await c.delete(f'/api/switches/{sid}')).status_code==200
    async with SessionLocal() as session: assert not (await session.execute(select(SwitchEvent))).all()
