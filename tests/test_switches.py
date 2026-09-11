"""SNMP-свитчи: достоверность состояния, безопасные счётчики и настройки."""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import NetworkSwitch, SwitchPort


NOW = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)


def _healthy_switch(**changes) -> NetworkSwitch:
    values = dict(
        name="Свитч камер", host="192.0.2.10", community_enc="", enabled=True,
        reachable=True, last_attempt_at=NOW, last_seen=NOW, uptime_ticks=100_000,
        capabilities={"interfaces": "supported"}, poe_ports={}, poe_supplies={}, ports=[],
    )
    values.update(changes)
    return NetworkSwitch(**values)


def _port(**changes) -> SwitchPort:
    values = dict(
        if_index=101, name="Ethernet1/0/1", admin_status=1, oper_status=1,
        speed_mbps=100, observed_at=NOW, present=True, expected_up=True,
        channel_ref_id=None, poe_index=None,
        counters={"bits": 32, "in_octets": 1_000, "out_octets": 2_000,
                  "in_errors": 2, "out_errors": 3,
                  "in_discards": 1, "out_discards": 0, "discontinuity": 0},
    )
    values.update(changes)
    return SwitchPort(**values)


def _client(**kwargs) -> httpx.AsyncClient:
    from app.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", **kwargs,
    )


@pytest.fixture
def snmp_client(monkeypatch):
    from app.drivers.snmp import SNMPClient

    client = SNMPClient("192.0.2.10", "driver-secret")
    monkeypatch.setattr(client, "_target", AsyncMock(return_value=object()))
    yield client
    client.close()


async def test_snmp_missing_oid_is_unsupported_not_a_value(snmp_client, monkeypatch):
    from pysnmp.proto.rfc1905 import NoSuchObject
    from app.drivers import snmp

    oid = f"{snmp.SYSTEM}.5.0"
    monkeypatch.setattr(snmp, "get_cmd", AsyncMock(return_value=(
        None, 0, 0, [(oid, NoSuchObject())],
    )))
    assert await snmp_client.get(oid) == {}


async def test_snmp_protocol_errors_do_not_reveal_credentials(snmp_client, monkeypatch):
    from app.drivers import snmp

    monkeypatch.setattr(snmp, "get_cmd", AsyncMock(return_value=(
        "request failed for community=driver-secret", 0, 0, [],
    )))
    with pytest.raises(snmp.SNMPError) as caught:
        await snmp_client.get(f"{snmp.SYSTEM}.3.0")
    assert "driver-secret" not in str(caught.value)


async def test_snmp_walk_stops_at_table_boundary(snmp_client, monkeypatch):
    from app.drivers import snmp

    root = tuple(map(int, snmp.IF_TABLE.split(".")))
    call = AsyncMock(return_value=(None, 0, 0, [
        (root + (1, 7), 7), (root + (2, 7), "Камера входа"),
        (tuple(map(int, snmp.IFX_TABLE.split("."))) + (1, 7), "outside"),
    ]))
    monkeypatch.setattr(snmp, "bulk_cmd", call)
    assert await snmp_client.walk(snmp.IF_TABLE) == {
        (1, 7): 7, (2, 7): "Камера входа",
    }
    assert call.await_count == 1


async def test_snmp_walk_repeated_oid_is_rejected(snmp_client, monkeypatch):
    from app.drivers import snmp

    root = tuple(map(int, snmp.IF_TABLE.split(".")))
    monkeypatch.setattr(snmp, "bulk_cmd", AsyncMock(return_value=(None, 0, 0, [
        (root + (1, 7), 7), (root + (1, 7), 7),
    ])))
    with pytest.raises(snmp.SNMPError, match="повторяющийся"):
        await snmp_client.walk(snmp.IF_TABLE)


async def test_snmp_walk_has_finite_call_budget(snmp_client, monkeypatch):
    from app.drivers import snmp

    root = tuple(map(int, snmp.IF_TABLE.split(".")))
    call = AsyncMock(side_effect=[
        (None, 0, 0, [(root + (1, 1), 1)]),
        (None, 0, 0, [(root + (1, 2), 2)]),
    ])
    monkeypatch.setattr(snmp, "bulk_cmd", call)
    with pytest.raises(snmp.SNMPError, match="лимита"):
        await snmp_client.walk(snmp.IF_TABLE, max_calls=2)
    assert call.await_count == 2


async def test_snmp_walk_has_finite_row_budget(snmp_client, monkeypatch):
    from app.drivers import snmp

    root = tuple(map(int, snmp.IF_TABLE.split(".")))
    monkeypatch.setattr(snmp, "bulk_cmd", AsyncMock(return_value=(None, 0, 0, [
        (root + (1, 1), 1), (root + (1, 2), 2),
    ])))
    with pytest.raises(snmp.SNMPError, match="лимит"):
        await snmp_client.walk(snmp.IF_TABLE, max_values=1)


@pytest.mark.parametrize("uptime", [None, "не число", -1, 1 << 32])
async def test_snmp_rejects_invalid_uptime(snmp_client, monkeypatch, uptime):
    from app.drivers import snmp

    monkeypatch.setattr(snmp_client, "get", AsyncMock(return_value={
        f"{snmp.SYSTEM}.3.0": uptime,
    }))
    with pytest.raises(snmp.SNMPError, match="sysUpTime"):
        await snmp_client.collect()


async def test_snmp_optional_mibs_report_unavailable_or_failed(snmp_client, monkeypatch):
    from app.drivers import snmp

    monkeypatch.setattr(snmp_client, "get", AsyncMock(return_value={
        f"{snmp.SYSTEM}.3.0": 12_000,
        f"{snmp.SYSTEM}.5.0": "Свитч первого этажа",
    }))

    async def walk(root):
        if root == snmp.IF_TABLE:
            return {(1, 101): 101, (2, 101): "Ethernet1/0/1", (7, 101): 1, (8, 101): 1}
        if root == snmp.POE_SUPPLY_TABLE:
            raise snmp.SNMPError("SNMP не отвечает")
        return {}

    monkeypatch.setattr(snmp_client, "walk", walk)
    result = await snmp_client.collect()
    assert result.sys_name == "Свитч первого этажа"
    assert result.capabilities["interfaces"] == "supported"
    assert result.capabilities["ifx"] == "unsupported"
    assert result.capabilities["poe_ports"] == "unsupported"
    assert result.capabilities["poe_supplies"] == "error"
    assert result.ports[101]["name"] == "Ethernet1/0/1"
    assert result.poe_ports == {}


async def test_snmp_uses_same_counter_width_for_both_directions(snmp_client, monkeypatch):
    from app.drivers import snmp

    monkeypatch.setattr(snmp_client, "get", AsyncMock(return_value={
        f"{snmp.SYSTEM}.3.0": 12_000,
    }))
    tables = {
        snmp.IF_TABLE: {(1, 101): 101, (10, 101): 1_000, (16, 101): 2_000},
        snmp.IFX_TABLE: {(6, 101): 1 << 35},
    }
    monkeypatch.setattr(snmp_client, "walk", AsyncMock(side_effect=lambda root: tables.get(root, {})))
    result = await snmp_client.collect()
    assert result.ports[101]["counters"]["bits"] == 32
    assert result.ports[101]["counters"]["in_octets"] == 1_000
    assert result.ports[101]["counters"]["out_octets"] == 2_000


@pytest.mark.parametrize("previous,current,bits,elapsed,speed,expected", [
    (1_000, 2_500, 32, 30, 100_000_000, 1_500),
    ((1 << 32) - 500, 500, 32, 30, 100_000_000, 1_000),
    (None, 500, 32, 30, 100_000_000, None),
    (500, None, 32, 30, 100_000_000, None),
    (1_000, 2_500, 32, 0, 100_000_000, None),
    (1_000, 2_500, 32, -1, 100_000_000, None),
])
def test_counter_delta_valid_wrap_and_missing_baseline(
    previous, current, bits, elapsed, speed, expected,
):
    from app.services.switches import counter_delta

    assert counter_delta(previous, current, bits, elapsed, speed_bps=speed) == expected


@pytest.mark.parametrize("previous,current,bits,elapsed,speed", [
    (1_000, 500, 32, 30, 100_000_000),  # обнуление не похоже на физически возможный оборот
    (1_000, 2_000, 32, 400, 100_000_000),  # возможны несколько оборотов
    (1_000, 2_000, 32, 30, None),  # без скорости неоднозначность Counter32 не разрешить
    (1_000, 500, 64, 30, None),
    (-1, 2_000, 32, 30, 100_000_000),
    (1_000, 1 << 32, 32, 30, 100_000_000),
])
def test_counter_delta_rejects_reset_and_ambiguous_intervals(previous, current, bits, elapsed, speed):
    from app.services.switches import counter_delta

    assert counter_delta(previous, current, bits, elapsed, speed_bps=speed) is None


@pytest.mark.parametrize("port_changes,expected_state", [
    ({"oper_status": 2, "expected_up": False}, "gray"),
    ({"oper_status": 2}, "red"),
    ({"admin_status": 2}, "red"),
    ({"oper_status": None}, "yellow"),
    ({"present": False}, "red"),
    ({"expected_up": False, "channel_ref_id": 777, "oper_status": 2}, "red"),
    ({"observed_at": None}, "gray"),
    ({"observed_at": NOW - dt.timedelta(hours=1)}, "gray"),
    ({"in_error_delta": 2}, "yellow"),
])
def test_port_health_uses_expectations_and_freshness(port_changes, expected_state):
    from app.services.switches import port_health

    assert port_health(_port(**port_changes), _healthy_switch(), now=NOW)["state"] == expected_state


def test_free_down_port_does_not_make_assigned_healthy_switch_red():
    from app.services.switches import switch_health

    switch = _healthy_switch(ports=[_port(), _port(if_index=102, expected_up=False, oper_status=2)])
    assert switch_health(switch, now=NOW)["state"] == "green"


@pytest.mark.parametrize("changes", [
    {"ports": []},
    {"capabilities": {"interfaces": "unsupported"}},
    {"capabilities": {"interfaces": "supported", "poe_ports": "error"}},
    {"last_seen": NOW - dt.timedelta(hours=1)},
    {"last_attempt_at": None},
    {"reachable": False},
    {"enabled": False},
])
def test_switch_is_not_green_without_verified_current_expected_ports(changes):
    from app.services.switches import switch_health

    values = {"ports": [_port()], **changes}
    assert switch_health(_healthy_switch(**values), now=NOW)["state"] != "green"


@pytest.mark.parametrize("poe,state", [
    ({"1.7": {"detection_status": 3}}, "green"),
    ({"1.7": {"detection_status": 4}}, "red"),
    ({"1.7": {"detection_status": 2}}, "red"),
    ({}, "yellow"),
])
def test_assigned_poe_port_requires_confirmed_delivery(poe, state):
    from app.services.switches import port_health

    assert port_health(_port(poe_index="1.7"), _healthy_switch(poe_ports=poe), now=NOW)["state"] == state


@pytest.mark.parametrize("change", ["normal", "reboot", "discontinuity", "counter_width", "renamed", "after_failure", "uptime_wrap"])
async def test_poll_rates_require_continuous_counters(db, monkeypatch, change):
    from app.drivers.snmp import SwitchSnapshot
    from app.services import switches

    monkeypatch.setattr(switches, "utcnow", lambda: NOW)
    old_uptime = (1 << 32) - 1_000 if change == "uptime_wrap" else 100_000
    uptime = 2_000 if change == "uptime_wrap" else (100 if change == "reboot" else 103_000)
    current = {**_port().counters, "in_octets": 4_000, "out_octets": 8_000, "in_errors": 4}
    if change == "discontinuity":
        current["discontinuity"] = 102_000
    if change == "counter_width":
        current["bits"] = 64
    snapshot = SwitchSnapshot(uptime_ticks=uptime, capabilities={"interfaces": "supported"}, ports={
        101: {"name": "renamed" if change == "renamed" else "Ethernet1/0/1",
              "admin_status": 1, "oper_status": 1, "speed_mbps": 100, "counters": current},
    })
    client = type("SnapshotClient", (), {"collect": AsyncMock(return_value=snapshot)})()
    async with SessionLocal() as session:
        switch = _healthy_switch(
            last_seen=NOW - dt.timedelta(seconds=30), uptime_ticks=old_uptime,
            reachable=change != "after_failure", ports=[_port()],
        )
        session.add(switch)
        await session.commit()
        assert await switches.poll_switch(session, switch, client=client) is True
        port = switch.ports[0]
        if change in ("normal", "uptime_wrap"):
            assert port.in_bps == pytest.approx(800)
            assert port.out_bps == pytest.approx(1_600)
            assert port.in_error_delta == 2
        else:
            assert port.in_bps is None and port.out_bps is None
            assert port.in_error_delta is None


async def test_poll_marks_disappeared_assigned_interface_missing(db, monkeypatch):
    from app.drivers.snmp import SwitchSnapshot
    from app.services import switches

    monkeypatch.setattr(switches, "utcnow", lambda: NOW)
    client = type("SnapshotClient", (), {"collect": AsyncMock(return_value=SwitchSnapshot(
        uptime_ticks=103_000, capabilities={"interfaces": "supported"}, ports={},
    ))})()
    async with SessionLocal() as session:
        switch = _healthy_switch(last_seen=NOW - dt.timedelta(seconds=30), ports=[_port()])
        session.add(switch)
        await session.commit()
        assert await switches.poll_switch(session, switch, client=client) is True
        assert switch.ports[0].present is False
        assert switch.ports[0].expected_up is True
        assert switches.switch_health(switch, now=NOW)["state"] == "red"


@pytest.mark.parametrize("error", [OSError("community=secret-from-transport"), ValueError("secret-from-transport"), TimeoutError()])
async def test_failed_poll_clears_rates_and_cannot_leave_green(db, monkeypatch, error):
    from app.services import switches

    monkeypatch.setattr(switches, "utcnow", lambda: NOW)
    client = type("FailingClient", (), {"collect": AsyncMock(side_effect=error)})()
    previous_seen = NOW - dt.timedelta(seconds=30)
    async with SessionLocal() as session:
        switch = _healthy_switch(last_seen=previous_seen, ports=[_port(in_bps=10_000, in_error_delta=2)])
        session.add(switch)
        await session.commit()
        assert await switches.poll_switch(session, switch, client=client) is False
        assert switch.reachable is False
        assert switch.last_seen.replace(tzinfo=dt.timezone.utc) == previous_seen
        assert switch.ports[0].in_bps is None
        assert switch.ports[0].in_error_delta is None
        assert switches.switch_health(switch, now=NOW)["state"] == "red"
        assert switches.port_health(switch.ports[0], switch, now=NOW)["state"] == "gray"
        assert "secret-from-transport" not in switch.last_error
        assert not switches.is_polling(switch.id)


async def test_switch_community_encrypted_and_never_returned(db):
    from app.crypto import decrypt

    secret = "test-only-camera-switch-community"
    async with _client() as c:
        created = await c.post("/api/switches", json={
            "name": "Свитч камер", "host": "192.0.2.10", "community": secret,
        })
        assert created.status_code == 201
        switch_id = created.json()["id"]
        async with SessionLocal() as session:
            switch = await session.get(NetworkSwitch, switch_id)
            encrypted = switch.community_enc
            assert encrypted.startswith("enc:")
            assert decrypt(encrypted) == secret

        for response in (
            created, await c.get("/api/switches"),
            await c.get(f"/api/switches/{switch_id}"),
        ):
            assert response.status_code in (200, 201)
            assert secret not in response.text
            assert encrypted not in response.text
            assert '"community_enc"' not in response.text


async def test_blank_community_edit_preserves_saved_secret(db):
    from app.crypto import decrypt

    async with _client() as c:
        created = await c.post("/api/switches", json={
            "name": "Свитч", "host": "192.0.2.10", "community": "saved-secret",
        })
        assert created.status_code == 201
        switch_id = created.json()["id"]
        edited = await c.put(f"/api/switches/{switch_id}", json={
            "name": "Свитч этажа 2", "community": "",
        })
        assert edited.status_code == 200

    async with SessionLocal() as session:
        switch = await session.get(NetworkSwitch, switch_id)
        assert switch.name == "Свитч этажа 2"
        assert decrypt(switch.community_enc) == "saved-secret"


async def test_switch_requires_existing_group(db):
    async with _client() as c:
        result = await c.post("/api/switches", json={
            "name": "Свитч", "host": "192.0.2.10", "community": "test-secret",
            "group_id": 987654,
        })
        assert result.status_code in (400, 404, 422)
    async with SessionLocal() as session:
        assert list((await session.execute(select(NetworkSwitch))).scalars()) == []


@pytest.mark.parametrize("bad_field", ["port", "community", "extra", "host"])
async def test_switch_validation_does_not_echo_community(db, bad_field):
    secret = "secret-must-not-appear-in-validation-response"
    payload = {"name": "Свитч", "host": "192.0.2.10", "community": secret}
    if bad_field == "port":
        payload["snmp_port"] = 0
    elif bad_field == "community":
        payload["community"] = secret * 10
    elif bad_field == "host":
        payload["host"] = f"http://{secret}@example.com"
    else:
        payload["extra"] = {"community": secret}
    async with _client() as c:
        result = await c.post("/api/switches", json=payload)
        assert result.status_code == 422
        assert secret not in result.text


async def test_switch_api_requires_login_and_monitoring_permission(db, monkeypatch):
    import app.main as main_mod
    from app.services import users

    async with SessionLocal() as session:
        await users.create_user(session, "only-buses", "pass123", permissions=["buses"])
        await users.create_user(session, "nvr-operator", "pass123", permissions=["monitoring"])
    monkeypatch.setattr(main_mod, "TEST_SESSION_OVERRIDE", None)

    async with _client(follow_redirects=False) as c:
        assert (await c.get("/api/switches")).status_code == 401
        assert (await c.post("/login", data={
            "username": "only-buses", "password": "pass123",
        })).status_code == 303
        assert (await c.get("/api/switches")).status_code == 403
        assert (await c.post("/api/switches", json={})).status_code == 403

    async with _client(follow_redirects=False) as c:
        assert (await c.post("/login", data={
            "username": "nvr-operator", "password": "pass123",
        })).status_code == 303
        assert (await c.get("/api/switches")).status_code == 200


async def test_port_mapping_validates_object_camera_and_poe_identity(db):
    from app.models import Channel, Device, Group

    async with SessionLocal() as session:
        group = Group(name="Объект А")
        other_group = Group(name="Объект Б")
        session.add_all([group, other_group])
        await session.flush()
        device = Device(name="NVR А", host="192.0.2.11", group_id=group.id)
        other_device = Device(name="NVR Б", host="192.0.2.12", group_id=other_group.id)
        session.add_all([device, other_device])
        await session.flush()
        camera = Channel(device_id=device.id, channel_id=7)
        other_camera = Channel(device_id=other_device.id, channel_id=7)
        session.add_all([camera, other_camera])
        switch = _healthy_switch(group_id=group.id, ports=[_port(), _port(if_index=102)],
                                 poe_ports={"1.7": {"detection_status": 3}})
        other_switch = _healthy_switch(name="Другой свитч", group_id=other_group.id, ports=[_port()])
        session.add_all([switch, other_switch])
        await session.commit()
        switch_id = switch.id
        first_port, second_port = (port.id for port in switch.ports)
        foreign_port = other_switch.ports[0].id
        camera_id, other_camera_id = camera.id, other_camera.id
        other_group_id = other_group.id

    first_path = f"/api/switches/{switch_id}/ports/{first_port}"
    second_path = f"/api/switches/{switch_id}/ports/{second_port}"
    async with _client() as c:
        assert (await c.patch(first_path, json={"channel_ref_id": 987654})).status_code == 422
        assert (await c.patch(first_path, json={"channel_ref_id": other_camera_id})).status_code == 422
        assert (await c.patch(f"/api/switches/{switch_id}/ports/{foreign_port}", json={"expected_up": True})).status_code == 404
        assert (await c.patch(first_path, json={"poe_index": "1.99"})).status_code == 422
        assert (await c.patch(first_path, json={"poe_index": "7"})).status_code == 422

        result = await c.patch(first_path, json={"channel_ref_id": camera_id, "poe_index": "1.7"})
        assert result.status_code == 200
        mapped = next(port for port in result.json()["ports"] if port["id"] == first_port)
        assert mapped["channel_ref_id"] == camera_id
        assert mapped["expected_up"] is True
        assert mapped["poe_index"] == "1.7"
        assert mapped["if_index"] == 101  # ifIndex и PoE-индекс остаются независимыми
        assert (await c.patch(second_path, json={"channel_ref_id": camera_id})).status_code == 422
        assert (await c.patch(second_path, json={"poe_index": "1.7"})).status_code == 422
        assert (await c.put(f"/api/switches/{switch_id}", json={"group_id": other_group_id})).status_code == 422

        cleared = await c.patch(first_path, json={"channel_ref_id": None, "poe_index": None, "expected_up": False})
        assert cleared.status_code == 200
        unmapped = next(port for port in cleared.json()["ports"] if port["id"] == first_port)
        assert unmapped["channel_ref_id"] is None
        assert unmapped["poe_index"] is None
        assert unmapped["expected_up"] is False


async def test_changed_snmp_target_invalidates_old_green_status(db, monkeypatch):
    from app.services import switches

    monkeypatch.setattr(switches, "utcnow", lambda: NOW)
    async with SessionLocal() as session:
        switch = _healthy_switch(ports=[_port(in_bps=5_000)])
        session.add(switch)
        await session.commit()
        switch_id = switch.id
    async with _client() as c:
        assert (await c.get(f"/api/switches/{switch_id}")).json()["health"]["state"] == "green"
        result = await c.put(f"/api/switches/{switch_id}", json={"host": "192.0.2.99"})
        assert result.status_code == 200
        assert result.json()["reachable"] is False
        assert result.json()["health"]["state"] != "green"
        assert result.json()["ports"][0]["in_bps"] is None


async def test_renumbered_interface_requires_mapping_confirmation(db, monkeypatch):
    from sqlalchemy.orm import selectinload
    from app.drivers.snmp import SwitchSnapshot
    from app.services import switches

    monkeypatch.setattr(switches, "utcnow", lambda: NOW)
    async with SessionLocal() as session:
        switch = _healthy_switch(ports=[_port()])
        session.add(switch)
        await session.commit()
        switch_id, port_id = switch.id, switch.ports[0].id
    path = f"/api/switches/{switch_id}/ports/{port_id}"
    async with _client() as c:
        assert (await c.patch(path, json={"expected_up": True})).status_code == 200

    client = type("SnapshotClient", (), {"collect": AsyncMock(return_value=SwitchSnapshot(
        uptime_ticks=103_000, capabilities={"interfaces": "supported"}, ports={
            101: {"name": "Ethernet1/0/9", "admin_status": 1, "oper_status": 1,
                  "speed_mbps": 100, "counters": _port().counters},
        },
    ))})()
    async with SessionLocal() as session:
        switch = (await session.execute(select(NetworkSwitch).options(
            selectinload(NetworkSwitch.ports)).where(NetworkSwitch.id == switch_id)
        )).scalar_one()
        # Повторный успешный опрос не должен самостоятельно подтвердить новую привязку.
        for _ in range(2):
            assert await switches.poll_switch(session, switch, client=client) is True
            health = switches.port_health(switch.ports[0], switch, now=NOW)
            assert health["state"] == "yellow"
            assert any("назначение" in reason for reason in health["reasons"])

    async with _client() as c:
        confirmed = await c.patch(path, json={"expected_up": True})
        assert confirmed.status_code == 200
        assert confirmed.json()["health"]["state"] == "green"


@pytest.mark.parametrize("interfaces,expected_state", [("supported", "red"), ("unsupported", "yellow")])
def test_missing_expected_port_uses_latest_switch_table_not_old_port_timestamp(interfaces, expected_state):
    from app.services.switches import port_health, switch_health

    port = _port(present=False, observed_at=NOW - dt.timedelta(days=1))
    switch = _healthy_switch(capabilities={"interfaces": interfaces}, ports=[port])
    assert port_health(port, switch, now=NOW)["state"] == expected_state
    assert switch_health(switch, now=NOW)["state"] == expected_state


async def test_edit_claim_blocks_poll_before_database_read(db, monkeypatch):
    from app.api import switches as api
    from app.services import switches

    entered, release = asyncio.Event(), asyncio.Event()
    original_get = api._get

    async def paused_get(session, switch_id):
        entered.set()
        await release.wait()
        return await original_get(session, switch_id)

    monkeypatch.setattr(api, "_get", paused_get)
    client = SimpleNamespace(collect=AsyncMock())
    async with SessionLocal() as session:
        switch = _healthy_switch(ports=[_port()])
        session.add(switch)
        await session.commit()
        switch_id = switch.id
        async with _client() as c:
            edit = asyncio.create_task(c.put(f"/api/switches/{switch_id}", json={"host": "192.0.2.99"}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert switches.is_polling(switch_id)
                assert await switches.poll_switch(session, switch, client=client) is False
                client.collect.assert_not_awaited()
            finally:
                release.set()
                result = await asyncio.wait_for(edit, timeout=5)
            assert result.status_code == 200
            assert not switches.is_polling(switch_id)
            # Освобождение блокировки разрешает следующую операцию над тем же свитчом.
            assert (await c.delete(f"/api/switches/{switch_id}")).status_code == 200
            assert not switches.is_polling(switch_id)


async def test_poll_claim_blocks_edit_delete_and_mapping_until_collect_finishes(db, monkeypatch):
    from app.drivers.snmp import SwitchSnapshot
    from app.services import switches

    monkeypatch.setattr(switches, "utcnow", lambda: NOW)
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_collect():
        entered.set()
        await release.wait()
        return SwitchSnapshot(uptime_ticks=103_000, capabilities={"interfaces": "supported"})

    async with SessionLocal() as session:
        switch = _healthy_switch(ports=[_port()])
        session.add(switch)
        await session.commit()
        switch_id, port_id = switch.id, switch.ports[0].id
        poll = asyncio.create_task(switches.poll_switch(
            session, switch, client=SimpleNamespace(collect=paused_collect),
        ))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            async with _client() as c:
                assert (await c.put(f"/api/switches/{switch_id}", json={"host": "192.0.2.99"})).status_code == 409
                assert (await c.delete(f"/api/switches/{switch_id}")).status_code == 409
                assert (await c.patch(f"/api/switches/{switch_id}/ports/{port_id}", json={"expected_up": False})).status_code == 409
            async with SessionLocal() as check_session:
                saved = await check_session.get(NetworkSwitch, switch_id)
                saved_port = await check_session.get(SwitchPort, port_id)
                assert saved.host == "192.0.2.10"
                assert saved_port.expected_up is True
        finally:
            release.set()
            result = await asyncio.wait_for(poll, timeout=5)
        assert result is True
        assert not switches.is_polling(switch_id)

    async with _client() as c:
        edited = await c.put(f"/api/switches/{switch_id}", json={"host": "192.0.2.99"})
        assert edited.status_code == 200
        assert edited.json()["host"] == "192.0.2.99"


async def test_poll_refreshes_target_changed_after_orm_was_loaded(db, monkeypatch):
    from app.crypto import encrypt
    from app.drivers.snmp import SwitchSnapshot
    from app.services import switches

    client = SimpleNamespace(collect=AsyncMock(return_value=SwitchSnapshot(uptime_ticks=103_000)), close=Mock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(switches, "SNMPClient", factory)
    async with SessionLocal() as old_session:
        switch = _healthy_switch(community_enc=encrypt("old-community"), ports=[_port()])
        old_session.add(switch)
        await old_session.commit()
        switch_id = switch.id

        async with SessionLocal() as writer:
            changed = await writer.get(NetworkSwitch, switch_id)
            changed.host = "192.0.2.99"
            changed.community_enc = encrypt("current-community")
            changed.snmp_port = 1161
            await writer.commit()
        assert switch.host == "192.0.2.10"  # Загруженный ORM всё ещё хранит старую настройку.

        assert await switches.poll_switch(old_session, switch) is True
        factory.assert_called_once_with(
            "192.0.2.99", "current-community", port=1161, timeout=2.0, retries=1,
        )
        client.collect.assert_awaited_once()
        client.close.assert_called_once()
        assert not switches.is_polling(switch_id)


def test_snmp_decodes_cyrillic_octet_string():
    from pysnmp.proto.rfc1902 import OctetString
    from app.drivers.snmp import _text

    assert _text(OctetString("Камера1".encode("utf-8"))) == "Камера1"


async def test_database_prevents_same_camera_on_ports_of_different_switches(db):
    from sqlalchemy.exc import IntegrityError
    from app.models import Channel, Device

    async with SessionLocal() as session:
        device = Device(name="NVR", host="192.0.2.20")
        session.add(device)
        await session.flush()
        camera = Channel(device_id=device.id, channel_id=7)
        session.add(camera)
        await session.flush()
        switch = _healthy_switch(ports=[_port(channel_ref_id=camera.id)])
        session.add(switch)
        await session.commit()
        camera_id = camera.id

        session.add(_healthy_switch(name="Другой свитч", ports=[_port(channel_ref_id=camera_id)]))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        assigned = list((await session.execute(select(SwitchPort).where(
            SwitchPort.channel_ref_id == camera_id,
        ))).scalars())
        assert len(assigned) == 1


async def test_mapping_integrity_race_returns_safe_conflict_and_releases_claim(db, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from app.database import get_session
    from app.main import app
    from app.services import switches

    async with SessionLocal() as session:
        switch = _healthy_switch(ports=[_port()])
        session.add(switch)
        await session.commit()
        switch_id, port_id = switch.id, switch.ports[0].id

        monkeypatch.setattr(session, "commit", AsyncMock(side_effect=IntegrityError(
            "private-sql-statement", {"community": "private-community"},
            RuntimeError("private-driver-message"),
        )))
        rollback = AsyncMock(wraps=session.rollback)
        monkeypatch.setattr(session, "rollback", rollback)

        async def override_session():
            yield session

        monkeypatch.setitem(app.dependency_overrides, get_session, override_session)
        async with _client() as c:
            response = await c.patch(f"/api/switches/{switch_id}/ports/{port_id}", json={"expected_up": False})
        assert response.status_code == 409
        assert "private-" not in response.text
        rollback.assert_awaited_once()
        assert not switches.is_polling(switch_id)


async def test_invalid_switch_path_parameter_remains_validation_error(db):
    async with _client() as c:
        response = await c.put("/api/switches/abc", json={"name": "Свитч"})
        assert response.status_code == 422
        assert any(error["loc"] == ["path", "switch_id"] for error in response.json()["detail"])
