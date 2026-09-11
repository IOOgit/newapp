"""Тесты расчёта покрытия архива (compute_coverage) и суточной проверки."""
import datetime as dt

from app.drivers.base import ArchiveSegment
from app.models import ArchiveState
from app.services.archive import compute_coverage

DAY = dt.date(2026, 6, 10)
START = dt.datetime.combine(DAY, dt.time.min)
END = dt.datetime.combine(DAY, dt.time.max).replace(microsecond=0)


def _seg(h1, h2):
    return ArchiveSegment(START + dt.timedelta(hours=h1), START + dt.timedelta(hours=h2))


def test_full_day():
    segs = [_seg(0, 24)]
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.FULL
    assert recorded >= 23 * 60
    assert gaps == []


def test_no_recording():
    status, recorded, largest, gaps = compute_coverage([], START, END, 60)
    assert status == ArchiveState.NONE
    assert recorded == 0
    assert largest > 60


def test_partial_with_gap():
    # запись 0–2 и 5–24, дыра 2–5 (3 часа)
    segs = [_seg(0, 2), _seg(5, 24)]
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.PARTIAL
    assert largest >= 170  # ~180 минут
    assert len(gaps) == 1


def test_small_gap_still_full():
    # дыра всего 30 минут (< порога 60) → FULL
    segs = [_seg(0, 12), ArchiveSegment(START + dt.timedelta(hours=12, minutes=30), END)]
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.FULL


def test_overlapping_segments_merge():
    segs = [_seg(0, 10), _seg(8, 24)]  # перекрытие
    status, recorded, largest, gaps = compute_coverage(segs, START, END, 60)
    assert status == ArchiveState.FULL
    assert recorded <= 24 * 60  # не считаем перекрытие дважды


async def test_archive_overview_and_dashboard(db):
    import httpx
    from sqlalchemy import select  # noqa: F401

    from app.database import SessionLocal
    from app.main import app
    from app.models import ArchiveCoverage, ArchiveState, Channel, ChannelState, Device

    day = dt.date.today() - dt.timedelta(days=1)
    async with SessionLocal() as s:
        d = Device(name="NVR1", host="h", username="a", api_type="hikvision", capabilities={})
        s.add(d)
        await s.commit()
        did = d.id
        for cid in (1, 2, 3):
            s.add(Channel(device_id=did, channel_id=cid, status=ChannelState.ONLINE, enabled=True))
        await s.commit()
        s.add(ArchiveCoverage(device_id=did, channel_id=1, day=day,
                              status=ArchiveState.FULL, recorded_minutes=1440, gaps=[]))
        s.add(ArchiveCoverage(device_id=did, channel_id=2, day=day,
                              status=ArchiveState.PARTIAL, recorded_minutes=1000,
                              largest_gap_minutes=120, gaps=[["02:00", "04:00"]]))
        s.add(ArchiveCoverage(device_id=did, channel_id=3, day=day,
                              status=ArchiveState.NONE, recorded_minutes=0, gaps=[]))
        await s.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        ov = (await c.get("/api/archive/overview")).json()
        assert ov["day"] == day.isoformat()
        dev = next(x for x in ov["devices"] if x["device_id"] == did)
        assert dev["full"] == 1 and dev["partial"] == 1 and dev["none"] == 1
        assert dev["status"] == "red"          # есть канал без записи
        assert ov["totals"]["none"] >= 1
        # дашборд и TV рендерятся с архивом (KPI-панель «Покрытие архива»)
        assert "Покрытие архива" in (await c.get("/")).text
        assert (await c.get("/tv")).status_code == 200
