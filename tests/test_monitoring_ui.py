"""Состояние NVR и время записи должны честно отображаться в интерфейсе."""
import datetime as dt

import httpx

from app.database import SessionLocal
from app.main import app
from app.models import Channel, Device, Hdd, utcnow


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _device(*, disk_status="ok", present=True):
    now = utcnow()
    async with SessionLocal() as session:
        device = Device(
            name="Стационарный NVR", host="127.0.0.1", username="admin",
            reachable=True, last_seen=now, time_drift_seconds=0,
            monitoring_checks={key: {"status": "ok", "checked_at": now.isoformat(), "detail": None}
                               for key in ("channels", "hdd", "time")},
        )
        session.add(device)
        await session.flush()
        session.add_all([
            Channel(device_id=device.id, channel_id=1, name="Вход <script>alert(1)</script>",
                    status="online", enabled=True, recording_status="ok",
                    recording_checked_at=now, recording_last_end=dt.datetime(2026, 9, 11, 12, 34, 56),
                    recording_age_seconds=90),
            Channel(device_id=device.id, channel_id=2, name="Резерв", status="offline", enabled=False),
            Hdd(device_id=device.id, hdd_id="1", capacity_mb=1024000, free_mb=0,
                status=disk_status, raw_status="unformatted" if disk_status == "unformatted" else "normal",
                present=present),
        ])
        await session.commit()
        return device.id


async def test_nvr_page_shows_storage_readiness_and_recording_wall_time(db):
    device_id = await _device(disk_status="unformatted")
    async with _client() as client:
        response = await client.get(f"/devices/{device_id}")
    assert response.status_code == 200
    body = response.text
    assert "не инициализирован" in body
    assert "unformatted" in body
    assert "Проверить свежую запись" in body
    assert "До 11.09 12:34:56" in body  # Не сдвигаем локальное время архива на UTC+10.
    assert "1.5 мин. назад" in body
    assert 'value="event"' in body and 'value="disabled"' in body
    assert "Вход &lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>alert(1)</script>" not in body


async def test_dashboard_counts_monitored_channels_and_neutral_disk_occupancy(db):
    await _device()
    async with _client() as client:
        response = await client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "1 под контролем" in body
    assert "<td>1/1</td>" in body
    assert "аптайм парка" not in body
    disks = body.split("💽 Заполнение HDD", 1)[1]
    assert "100%" in disks
    assert 'class="bar err"' not in disks and 'class="bar warn"' not in disks


async def test_missing_disk_keeps_identity_but_hides_old_free_space(db):
    device_id = await _device(present=False)
    async with _client() as client:
        response = await client.get(f"/devices/{device_id}")
    assert response.status_code == 200
    disks = response.text.split("<h2>Диски (HDD)</h2>", 1)[1].split("<h2>Календарь", 1)[0]
    assert "диск пропал" in disks
    assert "последний известный объём" in disks
    assert "100%" not in disks


async def test_expired_recording_check_does_not_keep_green_channel_badge(db):
    from sqlalchemy import select

    device_id = await _device()
    async with SessionLocal() as session:
        channel = (await session.execute(select(Channel).where(
            Channel.device_id == device_id, Channel.channel_id == 1,
        ))).scalar_one()
        channel.recording_checked_at = utcnow() - dt.timedelta(hours=1)
        await session.commit()
    async with _client() as client:
        response = await client.get(f"/devices/{device_id}")
    assert response.status_code == 200
    channels = response.text.split("<h2>Каналы</h2>", 1)[1].split("<h2>Диски", 1)[0]
    assert '<span class="badge warn">данные устарели</span>' in channels
    assert '<span class="badge ok">запись есть</span>' not in channels
