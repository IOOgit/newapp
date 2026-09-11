"""Контракт драйверов: ошибка ответа не равна пустому диску/архиву."""
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import settings
from app.drivers.base import FeatureUnavailable, HddInfo, NVRError
from app.drivers.dahua import DahuaClient
from app.drivers.hikvision import HikvisionClient
from app.models import Device, Hdd
from app.services import poller


def driver(cls, handler):
    return cls(host="test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("cls", [HikvisionClient, DahuaClient])
@pytest.mark.parametrize("raw,expected", [("normal", "ok"), ("unformatted", "unformatted"),
                                         ("readOnly", "read_only"), ("newVendorStatus", "unknown")])
async def test_storage_unknown_and_not_ready_are_not_ok(cls, raw, expected):
    if cls is HikvisionClient:
        body = f"<hddList><hdd><id>1</id><capacity>1024</capacity><freeSpace>0</freeSpace><status>{raw}</status></hdd></hddList>"
    else:
        body = f"list[0].Detail[0].TotalBytes=1073741824\nlist[0].Detail[0].UsedBytes=1073741824\nlist[0].Detail[0].State={raw}"
    client = driver(cls, lambda r: httpx.Response(200, text=body))
    result = await client.get_hdd_info()
    assert result[0].status == expected
    assert result[0].raw_status == raw


@pytest.mark.parametrize("cls,body", [
    (HikvisionClient, "<hddList><hdd><id>1</id><capacity>1024</capacity><status>normal</status><property>readOnly</property></hdd></hddList>"),
    (DahuaClient, "list[0].Detail[0].TotalBytes=1073741824\nlist[0].Detail[0].State=Running\nlist[0].Detail[0].Type=ReadOnly"),
])
async def test_read_only_property_overrides_healthy_status(cls, body):
    result = await driver(cls, lambda r: httpx.Response(200, text=body)).get_hdd_info()
    assert result[0].status == "read_only"


@pytest.mark.parametrize("cls,body", [(HikvisionClient, "<hddList/>"), (DahuaClient, "list=0")])
async def test_explicit_empty_inventory_is_success(cls, body):
    assert await driver(cls, lambda r: httpx.Response(200, text=body)).get_hdd_info() == []


@pytest.mark.parametrize("cls,body", [(HikvisionClient, "<html/>"), (HikvisionClient, "<hddList>"),
                                    (HikvisionClient, "<hddList><hdd><capacity>12</capacity></hdd></hddList>"),
                                    (DahuaClient, ""), (DahuaClient, "list[0].Detail[0].State=Normal")])
async def test_bad_inventory_is_not_missing_disks(cls, body):
    with pytest.raises(NVRError):
        await driver(cls, lambda r: httpx.Response(200, text=body)).get_hdd_info()


@pytest.mark.parametrize("cls", [HikvisionClient, DahuaClient])
async def test_unsupported_storage_is_distinct_from_empty(cls):
    with pytest.raises(FeatureUnavailable):
        await driver(cls, lambda r: httpx.Response(404)).get_hdd_info()


async def test_missing_disk_reappears_and_readiness_never_notifies(monkeypatch):
    monkeypatch.setattr(settings, "hdd_usage_alert_percent", 0)
    raise_alert, resolve_alert = AsyncMock(), AsyncMock()
    monkeypatch.setattr(poller.alerts, "raise_alert", raise_alert)
    monkeypatch.setattr(poller.alerts, "resolve_alert", resolve_alert)
    device = Device(id=1, name="NVR", host="test")
    device.hdds = [Hdd(hdd_id="1", capacity_mb=1024, status="ok", present=True)]
    session = MagicMock()
    await poller._update_hdds(session, device, [])
    assert device.hdds[0].status == "missing"
    assert device.hdds[0].present is False
    await poller._update_hdds(session, device, [HddInfo("1", capacity_mb=1024, status="unformatted")])
    assert device.hdds[0].status == "unformatted"
    assert device.hdds[0].present is True
    raise_alert.assert_not_awaited()
    resolve_alert.assert_not_awaited()
    await poller._update_hdds(session, device, [HddInfo("1", capacity_mb=1024, status="ok")])
    assert device.hdds[0].status == "ok"
    resolve_alert.assert_awaited_once()  # Существующее восстановление не изменено.


@pytest.mark.parametrize("failure", [FeatureUnavailable("unsupported"), NVRError("timeout")])
async def test_failed_hdd_poll_preserves_last_inventory_with_error(monkeypatch, failure):
    client = MagicMock()
    client.get_device_info = AsyncMock()
    client.get_hdd_info = AsyncMock(side_effect=failure)
    client.get_health = AsyncMock(side_effect=FeatureUnavailable())
    monkeypatch.setattr(poller, "build_client", lambda *a, **kw: client)
    device = Device(id=1, name="NVR", host="test", monitoring_checks={},
                    capabilities={"channels": False, "time": False}, consecutive_failures=0, auth_failures=0)
    device.hdds = [Hdd(hdd_id="1", capacity_mb=1024, status="ok", present=True)]
    await poller._poll_one(MagicMock(), device)
    assert device.hdds[0].status == "ok"
    assert device.hdds[0].present is True
    assert device.monitoring_checks["hdd"]["status"] == ("unavailable" if isinstance(failure, FeatureUnavailable) else "error")


async def test_dahua_archive_failed_page_and_failed_find_are_errors():
    start, end = dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)
    for failed_action in ("findFile", "findNextFile"):
        def respond(request):
            action = request.url.params.get("action")
            if action == failed_action:
                return httpx.Response(500, text="Error")
            return httpx.Response(200, text="result=1" if action == "factory.create" else "OK")
        with pytest.raises(NVRError):
            await driver(DahuaClient, respond).search_archive(1, start, end)


@pytest.mark.parametrize("body", ["<html/>", "<CMSearchResult><responseStatusStrg>MORE</responseStatusStrg></CMSearchResult>",
                                    "<CMSearchResult><responseStatusStrg>OK</responseStatusStrg><searchMatchItem/></CMSearchResult>"])
async def test_hik_bad_archive_response_is_not_empty(body):
    with pytest.raises(NVRError):
        await driver(HikvisionClient, lambda r: httpx.Response(200, text=body)).search_archive(
            1, dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2))
