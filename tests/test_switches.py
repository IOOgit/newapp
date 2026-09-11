"""DH-CS4226-24ET-240: только честная проверка TCP-доступности."""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import httpx

from app.database import SessionLocal
from app.models import NetworkSwitch

NOW = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)


def _switch(**changes):
    values = dict(
        name="Свитч камер", host="192.0.2.10", model="DH-CS4226-24ET-240",
        snmp_port=80, snmp_version="none", community_enc="", retries=0,
        enabled=True, reachable=True, last_attempt_at=NOW, last_seen=NOW,
        capabilities={"reachability": "tcp", "interfaces": "unavailable"}, ports=[],
    )
    values.update(changes)
    return NetworkSwitch(**values)


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_reachable_switch_is_never_green_without_port_telemetry():
    from app.services.switches import switch_health
    health = switch_health(_switch(), now=NOW)
    assert health["state"] == "yellow"
    assert "без телеметрии" in health["label"]
    assert "PoE" in health["reasons"][0]


def test_unreachable_and_stale_states_are_explicit():
    from app.services.switches import switch_health
    assert switch_health(_switch(reachable=False, last_error="Нет TCP"), now=NOW)["state"] == "red"
    stale = _switch(last_seen=NOW - dt.timedelta(hours=1))
    assert switch_health(stale, now=NOW)["state"] == "gray"


async def test_poll_checks_management_tcp_only(db, monkeypatch):
    from app.services import switches
    monkeypatch.setattr(switches, "utcnow", lambda: NOW)
    probe = AsyncMock()
    async with SessionLocal() as session:
        switch = _switch(reachable=False, last_attempt_at=None, last_seen=None)
        session.add(switch)
        await session.commit()
        await session.refresh(switch)
        assert await switches.poll_switch(session, switch, probe=probe) is True
        probe.assert_awaited_once_with("192.0.2.10", 80, switch.timeout)
        assert switch.capabilities["interfaces"] == "unavailable"
        assert switches.switch_health(switch, now=NOW)["state"] == "yellow"


async def test_failed_tcp_probe_marks_switch_unreachable(db):
    from app.services import switches
    probe = AsyncMock(side_effect=OSError("connection refused"))
    async with SessionLocal() as session:
        switch = _switch(last_attempt_at=None, last_seen=None)
        session.add(switch)
        await session.commit()
        await session.refresh(switch)
        assert await switches.poll_switch(session, switch, probe=probe) is False
        assert switch.reachable is False
        assert "TCP-порту 80" in switch.last_error


async def test_api_accepts_no_snmp_credentials_and_hides_legacy_fields(db):
    async with _client() as client:
        response = await client.post("/api/switches", json={
            "name": "Шкаф 1", "host": "192.0.2.20",
            "model": "DH-CS4226-24ET-240", "management_port": 8080,
        })
        assert response.status_code == 201
        body = response.json()
        assert body["management_port"] == 8080
        assert body["monitoring_method"] == "tcp"
        assert body["telemetry_available"] is False
        assert "community" not in body
        assert "snmp_port" not in body
        assert "snmp_version" not in body


async def test_api_rejects_snmp_fields_for_this_model(db):
    async with _client() as client:
        response = await client.post("/api/switches", json={
            "name": "Шкаф 1", "host": "192.0.2.20",
            "model": "DH-CS4226-24ET-240", "snmp_port": 161, "community": "public",
        })
        assert response.status_code == 422


async def test_switch_pages_state_model_limit(db):
    async with _client() as client:
        created = await client.post("/api/switches", json={
            "name": "Шкаф 1", "host": "192.0.2.20",
            "model": "DH-CS4226-24ET-240",
        })
        switch_id = created.json()["id"]
        listing = await client.get("/switches")
        card = await client.get(f"/switches/{switch_id}")
        assert "не поддерживает SNMP" in listing.text
        assert "SNMP отсутствует" in card.text
        assert "TCP-порт веб-интерфейса" in listing.text
