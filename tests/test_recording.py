"""Свежесть записи: настоящий индекс, ошибки, локальное время и восстановление."""
import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.drivers.base import ArchiveSegment, FeatureUnavailable, NVRAuthError, NVRConnectionError
from app.models import AlertState, Channel, ChannelState, Device, Event
from app.services import recording
from tests.conftest import make_driver


NOW = dt.datetime(2026, 9, 11, 12, 0)


@pytest.fixture
async def db():
    """Сервису достаточно БД: веб-роуты и пользовательская сессия не нужны."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


def _driver(segments=None, *, now=NOW):
    return SimpleNamespace(
        get_device_time=AsyncMock(return_value=now),
        search_archive=AsyncMock(return_value=segments if segments is not None else []),
    )


def _segment(age_minutes=2):
    return ArchiveSegment(NOW - dt.timedelta(minutes=40), NOW - dt.timedelta(minutes=age_minutes))


async def _device(session, *, modes=("continuous",), enabled=True):
    device = Device(name="Стационарный NVR", host="testserver", api_type="hikvision", enabled=enabled)
    session.add(device)
    await session.flush()
    channels = [Channel(
        device_id=device.id, channel_id=i + 1, enabled=True,
        status=ChannelState.ONLINE, recording_mode=mode,
    ) for i, mode in enumerate(modes)]
    session.add_all(channels)
    await session.commit()
    return device, channels


@pytest.mark.parametrize("age, status", [(2, "ok"), (15, "ok"), (16, "missing")])
async def test_fresh_index_and_stopped_recording(db, age, status):
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        client = _driver([_segment(age)])
        assert await recording.check_recording_device(session, device, client=client)
        await session.commit()
        await session.refresh(channel)
        assert channel.recording_status == status
        assert channel.recording_last_end == NOW - dt.timedelta(minutes=age)
        assert channel.recording_age_seconds == age * 60
        assert channel.recording_checked_at is not None
        assert channel.recording_error is None
        assert not device.monitoring_checks
        client.search_archive.assert_awaited_once_with(1, NOW - dt.timedelta(minutes=45), NOW)


async def test_empty_success_clears_previous_confirmation_and_recovers(db):
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        client = _driver([_segment()])
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == "ok"
        client.search_archive.return_value = []
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == "missing"
        assert channel.recording_last_end is None
        assert channel.recording_age_seconds is None
        client.search_archive.return_value = [_segment(1)]
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == "ok"
        assert channel.recording_last_end == NOW - dt.timedelta(minutes=1)
        assert channel.recording_error is None
        assert await session.scalar(select(func.count()).select_from(AlertState)) == 0
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.parametrize("error, status", [
    (NVRAuthError("401"), "error"),
    (NVRConnectionError("Таймаут"), "error"),
    (FeatureUnavailable("Поиск не поддерживается"), "unavailable"),
    (ValueError("Повреждённый ответ"), "error"),
])
async def test_failed_search_preserves_history_without_healthy_status(db, error, status):
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        previous = NOW - dt.timedelta(minutes=2)
        channel.recording_last_end = previous
        channel.recording_status = "ok"
        client = _driver()
        client.search_archive.side_effect = error
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == status
        assert channel.recording_last_end == previous
        assert channel.recording_age_seconds is None
        assert channel.recording_error
        client.search_archive.side_effect = None
        client.search_archive.return_value = [_segment(1)]
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == "ok"
        assert channel.recording_error is None


@pytest.mark.parametrize("clock_result, clock_error, status", [
    (None, None, "time_error"),
    (NOW, FeatureUnavailable("Нет часов NVR"), "time_error"),
    (NOW, NVRConnectionError("Нет связи"), "time_error"),
    (NOW, NVRAuthError("401"), "error"),
])
async def test_clock_failure_never_uses_server_clock(db, clock_result, clock_error, status):
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        client = _driver([_segment()], now=clock_result)
        client.get_device_time.side_effect = clock_error
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == status
        assert channel.recording_error
        client.search_archive.assert_not_awaited()


@pytest.mark.parametrize("segments", [
    [ArchiveSegment(NOW - dt.timedelta(minutes=2), NOW + dt.timedelta(minutes=1))],
    [ArchiveSegment(NOW + dt.timedelta(minutes=1), NOW + dt.timedelta(minutes=2))],
    [ArchiveSegment(NOW, NOW)],
    [ArchiveSegment(NOW, NOW - dt.timedelta(minutes=1))],
    [_segment(), ArchiveSegment(NOW, NOW + dt.timedelta(minutes=1))],
])
async def test_invalid_or_future_segments_cannot_make_channel_green(db, segments):
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        await recording.check_recording_device(session, device, client=_driver(segments))
        assert channel.recording_status == "error"
        assert channel.recording_last_end is None
        assert channel.recording_age_seconds is None


async def test_event_recording_and_disabled_channels(db):
    async with SessionLocal() as session:
        device, channels = await _device(session, modes=("continuous", "event", "disabled", "continuous"))
        channels[3].enabled = False
        client = _driver()
        await recording.check_recording_device(session, device, client=client)
        assert [channel.recording_status for channel in channels] == ["missing", "event", "disabled", "disabled"]
        assert [call.args[0] for call in client.search_archive.await_args_list] == [1, 2]
        client.search_archive.return_value = [_segment()]
        await recording.check_recording_device(session, device, client=client)
        assert channels[1].recording_status == "ok"
        client.search_archive.return_value = [_segment(30)]
        await recording.check_recording_device(session, device, client=client)
        assert channels[1].recording_status == "event"


async def test_disabled_device_has_no_requests(db):
    async with SessionLocal() as session:
        device, (channel,) = await _device(session, enabled=False)
        client = _driver()
        assert not await recording.check_recording_device(session, device, client=client)
        client.get_device_time.assert_not_awaited()
        client.search_archive.assert_not_awaited()
        assert channel.recording_status == "unknown"


async def test_timezone_and_midnight_use_nvr_wall_clock(db):
    tz = dt.timezone(dt.timedelta(hours=10))
    now = dt.datetime(2026, 9, 12, 0, 3, tzinfo=tz)
    end = dt.datetime(2026, 9, 11, 23, 58, tzinfo=tz)
    client = _driver([ArchiveSegment(end - dt.timedelta(minutes=10), end)], now=now)
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == "ok"
        assert channel.recording_last_end == end.replace(tzinfo=None)
        assert channel.recording_age_seconds == 300
        args = client.search_archive.await_args.args
        assert args[1] == dt.datetime(2026, 9, 11, 23, 18)
        assert args[2] == dt.datetime(2026, 9, 12, 0, 3)
        assert channel.recording_checked_at.tzinfo == dt.timezone.utc


async def test_fresh_archive_does_not_change_offline_channel_or_other_checks(db):
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        device.monitoring_checks = {"hdd": {"status": "error", "detail": "Диск не готов"}}
        channel.status = ChannelState.OFFLINE
        await recording.check_recording_device(session, device, client=_driver([_segment()]))
        assert channel.recording_status == "ok"
        assert channel.status == ChannelState.OFFLINE
        assert device.monitoring_checks["hdd"]["status"] == "error"


async def test_device_timeout_revokes_previous_green(db, monkeypatch):
    monkeypatch.setattr(recording, "_DEVICE_CHECK_TIMEOUT_SECONDS", 0.01)
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        previous = NOW - dt.timedelta(minutes=2)
        channel.recording_last_end = previous
        channel.recording_status = "ok"
        client = _driver()
        async def blocked_search(*args):
            await asyncio.Event().wait()

        client.search_archive.side_effect = blocked_search
        await recording.check_recording_device(session, device, client=client)
        assert channel.recording_status == "error"
        assert "Истекло время" in channel.recording_error
        assert channel.recording_last_end == previous
        assert device.id not in recording._active_device_ids


async def test_manual_and_scheduled_overlap_is_skipped(db):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_search(*args):
        entered.set()
        await release.wait()
        return [_segment()]

    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        client = _driver()
        client.search_archive.side_effect = blocked_search
        task = asyncio.create_task(recording.check_recording_device(session, device, client=client))
        await entered.wait()
        try:
            assert not await recording.check_recording_device(session, device, client=client)
        finally:
            release.set()
            await task
        assert channel.recording_status == "ok"
        client.search_archive.assert_awaited_once()


@pytest.mark.parametrize("api_type", ["hikvision", "dahua"])
async def test_real_driver_against_mock_nvr(db, nvr, api_type):
    client = make_driver(api_type, nvr)
    try:
        async with SessionLocal() as session:
            device, (channel,) = await _device(session)
            await recording.check_recording_device(session, device, client=client)
            assert channel.recording_status == "ok"
            assert channel.recording_age_seconds == 0
            nvr.get_channel(1).archive = "none"
            await recording.check_recording_device(session, device, client=client)
            assert channel.recording_status == "missing"
            assert not client._external_client.is_closed
    finally:
        await client._external_client.aclose()


async def test_scheduled_check_persists_results_and_retries_stale_capability(db, monkeypatch):
    client = _driver([_segment()])
    monkeypatch.setattr(recording, "build_client", lambda *args, **kwargs: client)
    async with SessionLocal() as session:
        device, (channel,) = await _device(session)
        device.capabilities = {"archive": False}
        await session.commit()
        channel_id = channel.id
        disabled, _ = await _device(session, enabled=False)
    await recording.check_recording_all()
    async with SessionLocal() as session:
        channel = await session.get(Channel, channel_id)
        assert channel.recording_status == "ok"
        disabled = await session.get(Device, disabled.id)
        assert not disabled.monitoring_checks
    client.search_archive.assert_awaited_once()
