"""Тесты рабочего списка проблем («разбор полётов») и массовых операций."""
import datetime as dt

import httpx

from app.database import SessionLocal
from app.main import app
from app.models import (
    ApiType,
    ArchiveCoverage,
    ArchiveState,
    Channel,
    ChannelState,
    Device,
    Quality,
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _seed() -> int:
    async with SessionLocal() as s:
        d = Device(name="Объект-WL", host="10.9.9.9", http_port=80,
                   api_type=ApiType.HIKVISION, enabled=True, reachable=True,
                   capabilities={"archive": True})
        s.add(d)
        await s.flush()
        s.add(Channel(device_id=d.id, channel_id=1, name="Вход", status=ChannelState.OFFLINE,
                      last_status_change=dt.datetime.now(dt.timezone.utc)))
        s.add(Channel(device_id=d.id, channel_id=2, name="Зал", status=ChannelState.ONLINE,
                      quality=Quality.DARK, quality_checked_at=dt.datetime.now(dt.timezone.utc)))
        day = dt.date.today() - dt.timedelta(days=1)
        s.add(ArchiveCoverage(device_id=d.id, channel_id=2, day=day, status=ArchiveState.NONE,
                              recorded_minutes=0, largest_gap_minutes=1439, gaps=[["00:00", "23:59"]]))
        await s.commit()
        return d.id


async def test_worklist_collects_issues(db):
    did = await _seed()
    async with _client() as c:
        r = await c.get("/worklist")
        assert r.status_code == 200

        data = (await c.get("/api/worklist")).json()
        kinds = {i["kind"] for i in data["issues"] if i["device_id"] == did}
        # Контроль качества убран из панели; старые quality-данные не создают проблему.
        assert {"channel", "archive"} <= kinds
        assert "quality" not in kinds
        assert data["summary"]["critical"] >= 2  # offline + нет архива
        assert all(i["ack"] is None for i in data["issues"])


async def test_worklist_ack_and_unack(db):
    did = await _seed()
    async with _client() as c:
        data = (await c.get("/api/worklist")).json()
        key = next(i["key"] for i in data["issues"] if i["device_id"] == did)

        r = await c.post("/api/worklist/ack", json={"key": key, "note": "звоню на объект"})
        assert r.status_code == 200
        data2 = (await c.get("/api/worklist")).json()
        item = next(i for i in data2["issues"] if i["key"] == key)
        assert item["ack"] and item["ack"]["note"] == "звоню на объект"
        assert data2["summary"]["taken"] == 1

        r = await c.post("/api/worklist/unack", json={"key": key})
        assert r.status_code == 200
        data3 = (await c.get("/api/worklist")).json()
        assert next(i for i in data3["issues"] if i["key"] == key)["ack"] is None


async def test_bulk_run_status_and_bad_action(db):
    await _seed()
    async with _client() as c:
        # неизвестное действие → 400
        assert (await c.post("/api/bulk/run?action=nonsense")).status_code == 400
        # статус доступен всегда
        st = (await c.get("/api/bulk/status")).json()
        assert "running" in st and "done" in st
