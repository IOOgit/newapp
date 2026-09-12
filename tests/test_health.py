"""Зелёный NVR требует свежих подтверждений, а не только доступного HTTP."""
import datetime as dt

import pytest

from app.config import settings
from app.models import ArchiveCoverage, Channel, Device, Hdd
from app.services.health import evaluate_device

NOW = dt.datetime(2026, 9, 11, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def healthy(monkeypatch):
    monkeypatch.setattr(settings, "quality_check_minutes", 0)
    device = Device(id=7, name="NVR", host="test", enabled=True, reachable=True,
                    last_seen=NOW, last_error=None, time_drift_seconds=0,
                    monitoring_checks={key: {"status": "ok", "checked_at": NOW.isoformat()}
                                       for key in ("channels", "hdd", "time")})
    device.channels = [Channel(channel_id=1, enabled=True, status="online", last_seen=NOW,
                               recording_mode="continuous", recording_status="ok", recording_checked_at=NOW)]
    device.hdds = [Hdd(hdd_id="1", status="ok", capacity_mb=1024, free_mb=0, present=True)]
    rows = [ArchiveCoverage(device_id=7, channel_id=1, day=NOW.date() - dt.timedelta(days=1),
                            status="full", checked_at=NOW)]
    return device, rows


def test_green_requires_complete_evidence_and_full_disk_is_normal(healthy):
    device, rows = healthy
    result = evaluate_device(device, now=NOW, archive_rows=rows)
    assert result["color"] == "green"
    assert not result["issues"]


@pytest.mark.parametrize("check", ["channels", "hdd", "time"])
@pytest.mark.parametrize("status", ["error", "unavailable", "unknown"])
def test_required_unknown_checks_never_green(healthy, check, status):
    device, rows = healthy
    device.monitoring_checks[check]["status"] = status
    result = evaluate_device(device, now=NOW, archive_rows=rows)
    assert result["color"] == "yellow"
    assert any(issue["kind"] == check for issue in result["issues"])


def test_stale_values_and_naive_sqlite_dates(healthy):
    device, rows = healthy
    device.last_seen = NOW.replace(tzinfo=None)
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "green"
    device.monitoring_checks["hdd"]["checked_at"] = (NOW - dt.timedelta(hours=1)).isoformat()
    result = evaluate_device(device, now=NOW, archive_rows=rows)
    assert result["color"] == "yellow"
    assert result["checks"]["hdd"]["status"] == "stale"


@pytest.mark.parametrize("state", ["unformatted", "read_only", "missing", "error", "no_disk"])
def test_hdd_readiness_fault_is_red(healthy, state):
    device, rows = healthy
    device.hdds[0].status = state
    result = evaluate_device(device, now=NOW, archive_rows=rows)
    assert result["color"] == "red"
    assert result["checks"]["hdd"]["status"] == "critical"


def test_successful_empty_disk_inventory_is_red(healthy):
    device, rows = healthy
    device.hdds = []
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "red"


@pytest.mark.parametrize("status", ["unknown", "error", "unavailable", "time_error", "disabled"])
def test_unproven_continuous_recording_never_green(healthy, status):
    device, rows = healthy
    device.channels[0].recording_status = status
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "yellow"


def test_missing_recording_and_archive_are_critical(healthy):
    device, rows = healthy
    device.channels[0].recording_status = "missing"
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "red"
    device.channels[0].recording_status = "ok"
    rows[0].status = "none"
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "red"


def test_last_confirmation_ages_before_next_poll(healthy):
    device, rows = healthy
    device.channels[0].recording_age_seconds = 14 * 60
    device.channels[0].recording_checked_at = NOW - dt.timedelta(minutes=2)
    result = evaluate_device(device, now=NOW, archive_rows=rows)
    assert result["color"] == "yellow"
    assert result["recording_stale_channels"] == [1]
    assert result["checks"]["recording"]["status"] == "stale"


def test_no_archive_evidence_does_not_mean_full(healthy):
    device, _ = healthy
    result = evaluate_device(device, now=NOW, archive_rows=[])
    assert result["color"] == "yellow"
    assert result["checks"]["archive"]["status"] == "no_data"


def test_disabled_channel_ignored_and_event_mode_respected(healthy):
    device, rows = healthy
    device.channels.append(Channel(channel_id=2, enabled=False, status="offline", recording_status="missing"))
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "green"
    device.channels[0].recording_mode = "event"
    device.channels[0].recording_status = "event"
    assert evaluate_device(device, now=NOW, archive_rows=[])["color"] == "green"


def test_disabled_and_never_polled_are_gray(healthy):
    device, rows = healthy
    device.last_seen = None
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "gray"
    device.enabled = False
    device.hdds[0].status = "error"
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "gray"


def test_optional_health_unsupported_is_neutral_but_overheat_is_not(healthy):
    device, rows = healthy
    device.monitoring_checks["health"] = {"status": "unavailable", "checked_at": NOW.isoformat()}
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "green"
    device.monitoring_checks["health"]["status"] = "ok"
    device.temperature = 90
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "red"


def test_quality_no_longer_affects_health_even_with_old_settings(healthy, monkeypatch):
    device, rows = healthy
    device.channels[0].quality = "frozen"
    device.channels[0].quality_checked_at = NOW
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "green"
    monkeypatch.setattr(settings, "quality_check_minutes", 5)
    health = evaluate_device(device, now=NOW, archive_rows=rows)
    assert health["color"] == "green"
    assert "quality" not in health["checks"]


def test_unreachable_is_critical_even_with_old_healthy_values(healthy):
    device, rows = healthy
    device.reachable = False
    assert evaluate_device(device, now=NOW, archive_rows=rows)["color"] == "red"
