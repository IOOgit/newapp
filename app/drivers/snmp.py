"""SNMP только для чтения: стандартные System, IF-MIB и POWER-ETHERNET-MIB.

DH-CS4226-24ET-240 использует этот пробуемый профиль; конкретный набор MIB
зависит от прошивки. Проприетарные OID и операции изменения здесь отсутствуют.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    UdpTransportTarget, Udp6TransportTarget, bulk_cmd, get_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

SYSTEM = "1.3.6.1.2.1.1"
IF_TABLE = "1.3.6.1.2.1.2.2.1"
IFX_TABLE = "1.3.6.1.2.1.31.1.1.1"
POE_PORT_TABLE = "1.3.6.1.2.1.105.1.1.1"
POE_SUPPLY_TABLE = "1.3.6.1.2.1.105.1.3.1.1"
_EXCEPTIONS = (NoSuchObject, NoSuchInstance, EndOfMibView)


class SNMPError(Exception):
    """Безопасная для показа ошибка без адресов, community и сырых ответов."""


@dataclass
class SwitchSnapshot:
    uptime_ticks: int
    sys_name: str | None = None
    sys_descr: str | None = None
    sys_object_id: str | None = None
    ports: dict[int, dict] = field(default_factory=dict)
    capabilities: dict[str, str] = field(default_factory=dict)
    poe_ports: dict[str, dict] = field(default_factory=dict)
    poe_supplies: dict[str, dict] = field(default_factory=dict)


def _number(value) -> int | None:
    if value is None or isinstance(value, _EXCEPTIONS):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _text(value) -> str | None:
    if value is None or isinstance(value, _EXCEPTIONS):
        return None
    if hasattr(value, "asOctets"):
        raw = value.asOctets()
        try:
            return raw.decode("utf-8")[:512]
        except UnicodeDecodeError:
            return raw.decode("latin-1")[:512]
    return str(value)[:512]


class SNMPClient:
    """Ограниченный обход таблиц; снаружи весь collect ограничен общим таймаутом."""

    def __init__(self, host: str, community: str, *, port: int = 161,
                 timeout: float = 2.0, retries: int = 1):
        self.host, self.community, self.port = host, community, port
        self.timeout, self.retries = timeout, retries
        self.engine = SnmpEngine()
        self.target = None

    async def _target(self):
        if self.target is None:
            cls = Udp6TransportTarget if ":" in self.host else UdpTransportTarget
            self.target = await cls.create(
                (self.host, self.port), timeout=self.timeout, retries=self.retries,
            )
        return self.target

    def _check(self, error, status) -> None:
        if error:
            raise SNMPError("SNMP не отвечает: проверьте сеть, доступ и community")
        if status:
            raise SNMPError("SNMP отклонил запрос: проверьте права чтения")

    async def get(self, *oids: str) -> dict[str, object]:
        result = await get_cmd(
            self.engine, CommunityData(self.community, mpModel=1), await self._target(),
            ContextData(), *(ObjectType(ObjectIdentity(oid)) for oid in oids),
            lookupMib=False,
        )
        error, status, _, bindings = result
        self._check(error, status)
        return {str(oid): value for oid, value in bindings if not isinstance(value, _EXCEPTIONS)}

    async def walk(self, root: str, *, max_values: int = 2048, max_calls: int = 110) -> dict:
        """GETBULK по одному поддереву с защитой от циклов и усечённых таблиц."""
        prefix = tuple(int(n) for n in root.split("."))
        cursor = prefix
        values = {}
        for _ in range(max_calls):
            error, status, _, bindings = await bulk_cmd(
                self.engine, CommunityData(self.community, mpModel=1), await self._target(),
                ContextData(), 0, 20, ObjectType(ObjectIdentity(cursor)), lookupMib=False,
            )
            self._check(error, status)
            if not bindings:
                raise SNMPError("SNMP вернул пустой ответ при обходе таблицы")
            for oid, value in bindings:
                name = tuple(int(n) for n in oid)
                if isinstance(value, _EXCEPTIONS) or name[:len(prefix)] != prefix:
                    return values
                if name <= cursor:
                    raise SNMPError("SNMP вернул повторяющийся OID")
                cursor = name
                values[name[len(prefix):]] = value
                if len(values) > max_values:
                    raise SNMPError("Таблица SNMP превышает лимит профиля")
        raise SNMPError("Обход таблицы SNMP не завершён в пределах лимита")

    async def collect(self) -> SwitchSnapshot:
        system = await self.get(*(f"{SYSTEM}.{i}.0" for i in (1, 2, 3, 5)))
        uptime = _number(system.get(f"{SYSTEM}.3.0"))
        if uptime is None or not 0 <= uptime < 2**32:
            raise SNMPError("SNMP не вернул корректный sysUpTime")
        snap = SwitchSnapshot(
            uptime_ticks=uptime,
            sys_name=_text(system.get(f"{SYSTEM}.5.0")),
            sys_descr=_text(system.get(f"{SYSTEM}.1.0")),
            sys_object_id=_text(system.get(f"{SYSTEM}.2.0")),
        )
        table = await self.walk(IF_TABLE)
        indexes = {key[1] for key in table if len(key) == 2 and key[0] == 1}
        snap.capabilities["interfaces"] = "supported" if indexes else "unsupported"
        for index in sorted(indexes):
            number = lambda column: _number(table.get((column, index)))
            snap.ports[index] = {
                "if_index": index, "name": _text(table.get((2, index))) or str(index),
                "description": _text(table.get((2, index))), "alias": None,
                "admin_status": number(7), "oper_status": number(8),
                "speed_mbps": number(5) / 1_000_000 if number(5) else None,
                "last_change_ticks": number(9),
                "counters": {"bits": 32, "in_octets": number(10), "out_octets": number(16),
                             "in_errors": number(14), "out_errors": number(20),
                             "in_discards": number(13), "out_discards": number(19),
                             "discontinuity": None},
            }
        optional = {}
        for key, root in (("ifx", IFX_TABLE), ("poe_ports", POE_PORT_TABLE),
                          ("poe_supplies", POE_SUPPLY_TABLE)):
            try:
                optional[key] = await self.walk(root)
                snap.capabilities[key] = "supported" if optional[key] else "unsupported"
            except SNMPError:
                optional[key] = {}
                snap.capabilities[key] = "error"
        for index, port in snap.ports.items():
            extended = optional["ifx"]
            port["name"] = _text(extended.get((1, index))) or port["name"]
            port["alias"] = _text(extended.get((18, index)))
            speed = _number(extended.get((15, index)))
            if speed:
                port["speed_mbps"] = speed
            port["counters"]["discontinuity"] = _number(extended.get((19, index)))
            incoming, outgoing = (_number(extended.get((col, index))) for col in (6, 10))
            if incoming is not None and outgoing is not None:
                port["counters"].update(bits=64, in_octets=incoming, out_octets=outgoing)
        for key, values in optional["poe_ports"].items():
            if len(key) != 3:
                continue
            column, group, port = key
            field_name = {3: "admin_enable", 6: "detection_status", 12: "power_denied",
                          13: "overload", 14: "short"}.get(column)
            if field_name:
                snap.poe_ports.setdefault(f"{group}.{port}", {})[field_name] = _number(values)
        for key, value in optional["poe_supplies"].items():
            if len(key) != 2:
                continue
            column, group = key
            field_name = {2: "nominal_watts", 3: "oper_status", 4: "consumption_watts"}.get(column)
            if field_name:
                snap.poe_supplies.setdefault(str(group), {})[field_name] = _number(value)
        return snap

    def close(self) -> None:
        self.engine.close_dispatcher()
