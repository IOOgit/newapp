import datetime as dt
import struct
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import timesync
from app.drivers.base import NVRError
from tests.conftest import make_driver


@pytest.mark.parametrize('values', [
    {'enabled': True}, {'server': 'http://pool.ntp.org'}, {'port': 0},
    {'time': '24:00'}, {'timezone': 'Not/AZone'},
])
def test_validation(values):
    with pytest.raises(ValueError):
        timesync.TimeSyncSettings(**values)


async def test_settings_api(db):
    from app.main import app
    from app.scheduler import scheduler
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        config = {'server': 'time.example', 'port': 123, 'enabled': True,
                  'time': '04:25', 'timezone': 'Asia/Vladivostok'}
        response = await client.put('/api/monitoring/time-sync', json=config)
        assert response.status_code == 200, response.text
        assert (await timesync.load_settings()).model_dump() == config
        trigger = scheduler.get_job('time_sync').trigger
        next_time = trigger.get_next_fire_time(None, dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc))
        assert next_time.hour == 4 and next_time.minute == 25
        assert next_time.utcoffset() == dt.timedelta(hours=10)
        assert (await client.get('/time-settings')).status_code == 200
        config['enabled'] = False
        assert (await client.put('/api/monitoring/time-sync', json=config)).status_code == 200
        assert scheduler.get_job('time_sync') is None
        config['time'] = '25:00'
        assert (await client.put('/api/monitoring/time-sync', json=config)).status_code == 422


async def test_no_fallback_on_ntp_error(monkeypatch):
    monkeypatch.setattr(timesync, 'load_settings', AsyncMock(return_value=timesync.TimeSyncSettings(server='broken')))
    def fail(*args):
        raise OSError('timeout')
    monkeypatch.setattr(timesync, '_query_ntp', fail)
    client = AsyncMock()
    with pytest.raises(NVRError):
        await timesync.sync_device(client)
    client.sync_time.assert_not_called()


@pytest.mark.parametrize('api', ['hikvision', 'dahua'])
async def test_explicit_time_reaches_driver(api, nvr):
    client = make_driver(api, nvr)
    target = dt.datetime(2026, 9, 15, 3, 25, tzinfo=dt.timezone(dt.timedelta(hours=10)))
    client._request = AsyncMock(wraps=client._request)
    await client.sync_time(target)
    sent = client._request.call_args.kwargs
    if api == 'hikvision':
        assert '<localTime>2026-09-15T03:25:00+10:00</localTime>' in sent['data']
        assert '<timeMode>manual</timeMode>' in sent['data']
    else:
        assert sent['params']['time'] == '2026-09-15 03:25:00'


@pytest.mark.parametrize('bad', [None, 'short', 'mode', 'unsynced', 'stratum', 'origin', 'zero'])
def test_ntp_packet_validation(monkeypatch, bad):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, value): pass
        def send(self, packet): self.packet = packet
        def recv(self, size):
            reply = bytearray(48)
            reply[0] = 0x24
            reply[1] = 2
            reply[24:32] = self.packet[40:48]
            reply[32:40] = self.packet[40:48]
            reply[40:48] = self.packet[40:48]
            if bad == 'short': return b'abc'
            if bad == 'mode': reply[0] = 0x23
            if bad == 'unsynced': reply[0] |= 0xc0
            if bad == 'stratum': reply[1] = 0
            if bad == 'origin': reply[24:32] = bytes(8)
            if bad == 'zero': reply[40:48] = bytes(8)
            return bytes(reply)
    monkeypatch.setattr(timesync.socket, 'getaddrinfo', lambda *a, **k: [(2, 2, 17, '', ('127.0.0.1', 123))])
    monkeypatch.setattr(timesync.socket, 'socket', lambda *a: Socket())
    if bad:
        with pytest.raises(ValueError): timesync._query_ntp('test', 123)
    else:
        assert abs(timesync._query_ntp('test', 123) - timesync.time.time()) < 1


async def test_scheduled_run_continues_and_persists(db, monkeypatch):
    from app import crud, schemas
    from app.database import SessionLocal
    from app.models import AppSetting
    import app.drivers
    import json
    async with SessionLocal() as session:
        for name, enabled in [('bad', True), ('good', True), ('disabled', False)]:
            await crud.create_device(session, schemas.DeviceCreate(name=name, host='localhost', username='x', password='x', api_type='hikvision', enabled=enabled))
    await timesync.save_json('time_sync_settings', timesync.TimeSyncSettings(server='ntp', enabled=True).model_dump())
    monkeypatch.setattr(timesync, 'reference_time', AsyncMock(return_value=dt.datetime.now(dt.timezone.utc)))
    good, bad = AsyncMock(), AsyncMock()
    bad.sync_time.side_effect = NVRError('offline')
    monkeypatch.setattr(app.drivers, 'build_client', lambda d: bad if d.name == 'bad' else good)
    await timesync.scheduled_sync()
    good.sync_time.assert_awaited_once()
    async with SessionLocal() as session:
        row = await session.get(AppSetting, 'time_sync_last_run')
        result = json.loads(row.value)
    assert [d['ok'] for d in result['devices']] == [False, True]
    assert result['finished_at']
    good.reset_mock()
    monkeypatch.setattr(timesync, 'reference_time', AsyncMock(side_effect=NVRError('NTP offline')))
    await timesync.scheduled_sync()
    good.sync_time.assert_not_called()


async def test_settings_require_monitoring_permission(db):
    import app.main as main
    from app.database import SessionLocal
    from app.services import users
    async with SessionLocal() as session:
        user = await users.create_user(session, 'limited', 'pass123', permissions=['buses'])
    main.TEST_SESSION_OVERRIDE = {'auth': True, 'user_id': user.id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        assert (await client.get('/api/monitoring/time-sync')).status_code == 403
        assert (await client.put('/api/monitoring/time-sync', json={})).status_code == 403
        assert (await client.post('/api/monitoring/time-sync/test', json={})).status_code == 403
        assert (await client.get('/time-settings')).status_code == 303
