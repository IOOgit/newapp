"""Проверки связности нового мониторинга: API, миграция и ручные настройки."""
import datetime as dt

import httpx
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import SessionLocal, _lightweight_migrate
from app.main import app
from app.models import Channel, Device, Hdd, NetworkSwitch, SwitchPort, utcnow
from app.services.backup import export_data, import_data


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def seed_recording_problem():
    async with SessionLocal() as session:
        now = utcnow()
        d = Device(name="NVR", host="127.0.0.1", enabled=True, reachable=True,
                   last_seen=now, time_drift_seconds=0, capabilities={"archive": True},
                   monitoring_checks={kind: {"status": "ok", "checked_at": now.isoformat()}
                                      for kind in ("channels", "hdd", "time")})
        session.add(d)
        await session.flush()
        ch = Channel(device_id=d.id, channel_id=1, name="Вход", enabled=True,
                     status="online", recording_mode="continuous", recording_status="missing",
                     recording_checked_at=now)
        session.add(ch)
        session.add(Hdd(device_id=d.id, hdd_id="1", capacity_mb=1024, free_mb=0,
                        status="ok", raw_status="normal", present=True))
        await session.commit()
        return d.id, ch.id


async def test_online_nvr_with_stopped_recording_is_problem_in_shared_api(db):
    did, _ = await seed_recording_problem()
    async with client() as c:
        response = await c.get("/api/monitoring/health")
        assert response.status_code == 200
        item = next(d for d in response.json()["devices"] if d["id"] == did)
        assert item["reachable"] is True
        assert item["health"]["color"] == "red"
        assert any(i["kind"] == "recording" for i in item["health"]["issues"])
        summary = (await c.get("/api/summary")).json()
        assert summary["devices_with_problems"] == 1
        assert summary["devices_healthy"] == 0


async def test_recording_mode_validated_and_invalidates_old_verdict(db):
    did, _ = await seed_recording_problem()
    async with client() as c:
        path = f"/api/devices/{did}/channels/1/recording"
        assert (await c.put(path, json={"recording_mode": "invented"})).status_code == 422
        response = await c.put(path, json={"recording_mode": "event"})
        assert response.status_code == 200
        assert response.json()["recording_mode"] == "event"
        assert response.json()["recording_status"] == "unknown"
        assert response.json()["recording_checked_at"] is None
        assert (await c.put(f"/api/devices/{did}/channels/99/recording",
                            json={"recording_mode": "event"})).status_code == 404


async def test_recording_manual_overlap_returns_conflict(db, monkeypatch):
    did, _ = await seed_recording_problem()

    async def occupied(*args, **kwargs):
        return False

    monkeypatch.setattr("app.api.devices.recording.check_recording_device", occupied)
    async with client() as c:
        assert (await c.post(f"/api/devices/{did}/recording-check")).status_code == 409


async def test_backup_keeps_switch_mapping_and_channel_recording_mode(db):
    did, cid = await seed_recording_problem()
    from app.crypto import decrypt, encrypt

    async with SessionLocal() as session:
        channel = await session.get(Channel, cid)
        channel.recording_mode = "event"
        switch = NetworkSwitch(name="Этаж 1", host="192.0.2.1", community_enc=encrypt("test-secret"))
        session.add(switch)
        await session.flush()
        session.add(SwitchPort(switch_id=switch.id, if_index=7, expected_up=True,
                               channel_ref_id=cid, poe_index="1.3"))
        await session.commit()
        data = await export_data(session)
    async with SessionLocal() as session:
        await import_data(session, data)
    async with SessionLocal() as session:
        port = (await session.execute(select(SwitchPort))).scalar_one()
        assert port.if_index == 7 and port.poe_index == "1.3" and port.expected_up
        channel = await session.get(Channel, port.channel_ref_id)
        assert channel.device_id == did and channel.recording_mode == "event"
        switch = await session.get(NetworkSwitch, port.switch_id)
        assert decrypt(switch.community_enc) == "test-secret"


async def test_existing_database_migration_preserves_rows_and_adds_defaults():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            for table in ("devices", "channels", "hdds"):
                await conn.execute(text(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, name TEXT)'))
                await conn.execute(text(f'INSERT INTO "{table}" (id, name) VALUES (1, \'старое\')'))
            await conn.run_sync(_lightweight_migrate)
            await conn.run_sync(_lightweight_migrate)
            row = (await conn.execute(text("SELECT name, recording_mode, recording_status FROM channels"))).one()
            assert tuple(row) == ("старое", "continuous", "unknown")
            assert (await conn.execute(text("SELECT present FROM hdds"))).scalar_one() == 1
            columns = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("devices")})
            assert "monitoring_checks" in columns
    finally:
        await engine.dispose()
