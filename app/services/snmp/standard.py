"""SNMPv2-MIB, IF-MIB, BRIDGE-MIB, ENTITY-MIB и POWER-ETHERNET-MIB.

Числовые OID из RFC 3418/2863/1493/4133/3433/3621; MIB-файлы на сервере не нужны.
"""
from __future__ import annotations

import asyncio
import re

from .client import SnmpError
from .counters import rebooted
from .vendors import adapter_for

SYSTEM = {"sys_descr": "1.3.6.1.2.1.1.1.0", "sys_object_id": "1.3.6.1.2.1.1.2.0",
          "uptime_ticks": "1.3.6.1.2.1.1.3.0", "sys_name": "1.3.6.1.2.1.1.5.0"}
IF_NUMBER = "1.3.6.1.2.1.2.1.0"
IF_ENTRY = "1.3.6.1.2.1.2.2.1"
IFX_ENTRY = "1.3.6.1.2.1.31.1.1.1"
BRIDGE_PORTS = "1.3.6.1.2.1.17.1.4.1.2"
ENTITY = "1.3.6.1.2.1.47.1.1.1.1"
SENSORS = "1.3.6.1.2.1.99.1.1.1"
POE_PORTS = "1.3.6.1.2.1.105.1.1.1"
POE_SUPPLIES = "1.3.6.1.2.1.105.1.3.1.1"
OPTIONAL = {"lldp": "1.0.8802.1.1.2.1.3.7.1", "vlans": "1.3.6.1.2.1.17.7.1.4.3.1",
            "mac_table": "1.3.6.1.2.1.17.4.3.1"}
# Имена из https://www.iana.org/assignments/enterprise-numbers.txt.
# Это только подписи производителя: ни один PEN не разрешает/запрещает мониторинг.
ENTERPRISES = {9: "Cisco", 171: "D-Link", 890: "Zyxel", 2011: "Huawei",
               11863: "TP-Link", 14988: "MikroTik", 37496: "Dahua", 39165: "Hikvision",
               65855: "TP-Link"}
IF_DYNAMIC = {5, 7, 8, 9, 10, 13, 14, 16, 19, 20}
IFX_DYNAMIC = {6, 10, 15, 19}


def text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").replace("\x00", "").strip()[:4096]
    return str(value)[:4096]


def integer(value):
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def rows(values, root):
    result = {}
    for oid, value in values.items():
        if not oid.startswith(root + "."):
            continue
        column, _, index = oid[len(root)+1:].partition(".")
        if column.isdigit() and index:
            result.setdefault(index, {})[int(column)] = value
    return result


def parse_interfaces(values):
    base, extended = rows(values, IF_ENTRY), rows(values, IFX_ENTRY)
    result = []
    for index in sorted(set(base) | set(extended), key=lambda s: tuple(map(int, s.split(".")))):
        if not index.isdigit():
            continue
        b, x = base.get(index, {}), extended.get(index, {})
        if_type, connector = integer(b.get(3)), integer(x.get(17))
        name, description = text(x.get(1)), text(b.get(2))
        physical = connector == 1 or (connector is None and if_type in (6, 7, 62, 69, 117)
                                     and not re.search(r"^(vlan|loopback|bridge|bond|lag)(?:\d|\W|$)", (name or description or ""), re.I))
        speed = integer(x.get(15))
        if not speed:
            raw_speed = integer(b.get(5))
            speed = raw_speed / 1e6 if raw_speed and raw_speed != 4294967295 else None
        mac = b.get(6)
        if isinstance(mac, bytes):
            mac = ":".join(f"{v:02x}" for v in mac) or None
        counters = {"discontinuity": integer(x.get(19))}
        for direction, standard_col, hc_col, err_col, disc_col in (
            ("in", 10, 6, 14, 13), ("out", 16, 10, 20, 19)
        ):
            high = integer(x.get(hc_col))
            counters[direction] = high if high is not None else integer(b.get(standard_col))
            counters[direction + "_bits"] = 64 if high is not None else 32
            counters[direction + "_errors"] = integer(b.get(err_col))
            counters[direction + "_discards"] = integer(b.get(disc_col))
        result.append(dict(if_index=int(index), name=name, description=description, alias=text(x.get(18)),
                           if_type=if_type, physical=physical, mac_address=text(mac), speed_mbps=speed,
                           admin_status=integer(b.get(7)), oper_status=integer(b.get(8)),
                           last_change_ticks=integer(b.get(9)), counters=counters))
    return result


def dynamic_oid(oid):
    for root, columns in ((IF_ENTRY, IF_DYNAMIC), (IFX_ENTRY, IFX_DYNAMIC)):
        if oid.startswith(root + "."):
            return int(oid[len(root)+1:].split(".")[0]) in columns
    return False


def parse_poe(port_data, supply_data):
    ports = {}
    for index, row in rows(port_data, POE_PORTS).items():
        ports[index] = dict(index=index, enabled={1: True, 2: False}.get(integer(row.get(3))),
                            status=integer(row.get(6)), description=text(row.get(9)),
                            power_class=integer(row.get(10)), power_w=None, allocated_w=None,
                            errors={str(c): integer(row.get(c)) for c in (11, 12, 13, 14)})
    supplies = {index: dict(budget_w=integer(row.get(2)), status=integer(row.get(3)),
                            used_w=integer(row.get(4))) for index, row in rows(supply_data, POE_SUPPLIES).items()}
    return ports, supplies


def identify(system, entity):
    chassis = next((r for r in rows(entity, ENTITY).values() if integer(r.get(5)) == 3), {})
    match = re.match(r"^1\.3\.6\.1\.4\.1\.(\d+)(?:\.|$)", system.get("sys_object_id") or "")
    vendor = ENTERPRISES.get(int(match[1])) if match else None
    vendor = vendor or text(chassis.get(12)) or None
    if not vendor:
        description = (system.get("sys_descr") or "").lower()
        for label in ("D-Link", "Dahua", "Hikvision", "TP-Link", "Zyxel", "MikroTik", "Huawei", "Cisco"):
            if label.lower() in description:
                vendor = label
                break
    return dict(vendor=vendor, detected_model=text(chassis.get(13)) or None,
                firmware=text(chassis.get(9)) or text(chassis.get(10)) or None)


def parse_sensors(values):
    result = []
    for index, row in rows(values, SENSORS).items():
        kind, scale, precision = integer(row.get(1)), integer(row.get(2)), integer(row.get(3))
        value, status = integer(row.get(4)), integer(row.get(5))
        # RFC 3433: scale 9 = units; precision задаёт число десятичных знаков.
        if None in (scale, precision, value) or status != 1 or kind not in (8, 9, 10):
            continue
        if 1 <= scale <= 17 and -8 <= precision <= 9:
            result.append(dict(index=index, kind=kind, value=value * 10.0 ** ((scale - 9) * 3 - precision)))
    return result


async def probe(client):
    values = await client.get(list(SYSTEM.values()))
    if not values:
        raise SnmpError("unavailable", "unsupported")
    return {name: integer(values.get(oid)) if name == "uptime_ticks" else text(values.get(oid))
            for name, oid in SYSTEM.items()}


async def collect(client, *, cache=None, old_uptime=None, elapsed=None, discover=False):
    cache = dict(cache or {})
    system = await probe(client)
    reset = rebooted(old_uptime, system["uptime_ticks"], elapsed)
    if_number = (await client.get([IF_NUMBER])).get(IF_NUMBER)
    discover = (discover or reset or "static" not in cache or if_number != cache.get("if_number")
                or system["sys_object_id"] != cache.get("sys_object_id"))
    caps = dict(cache.get("capabilities", {}))
    caps["snmp"] = "available"
    diagnostics = {}
    optional_deadline = None

    async def optional(root, *, limit=20000, partial=False):
        try:
            budget = min(6, optional_deadline - asyncio.get_running_loop().time()) if optional_deadline else 6
            if budget <= 0:
                diagnostics[root] = "timeout"
                return None
            async with asyncio.timeout(budget):
                return await client.walk(root, limit=limit, partial=partial)
        except (SnmpError, TimeoutError) as exc:
            diagnostics[root] = exc.code if isinstance(exc, SnmpError) else "timeout"
            return None

    if discover:
        values = await client.walk(IF_ENTRY)
        extended = await optional(IFX_ENTRY)
        values.update(extended or {})
        cache["static"] = {oid: text(v) if isinstance(v, bytes) else v
                           for oid, v in values.items() if not dynamic_oid(oid)}
        # MAC сохраняем как отображаемую строку, а не декодируем бинарный адрес.
        for oid, value in values.items():
            if oid.startswith(IF_ENTRY + ".6.") and isinstance(value, bytes):
                cache["static"][oid] = ":".join(f"{v:02x}" for v in value)
        cache["dynamic"] = [oid for oid in values if dynamic_oid(oid)]
        cache["if_number"], cache["sys_object_id"] = if_number, system["sys_object_id"]
        optional_deadline = asyncio.get_running_loop().time() + 10
        bridge = await optional(BRIDGE_PORTS)
        cache["bridge_ports"] = {oid[len(BRIDGE_PORTS)+1:]: integer(v) for oid, v in (bridge or {}).items()}
        entity = await optional(ENTITY)
        cache["identity"] = identify(system, entity or {})
        caps["power_supply"] = "available" if any(integer(r.get(5)) == 6 for r in rows(entity or {}, ENTITY).values()) else "unavailable"
        caps["fan"] = "available" if any(integer(r.get(5)) == 7 for r in rows(entity or {}, ENTITY).values()) else "unavailable"
        for capability, root in OPTIONAL.items():
            found = await optional(root, limit=1, partial=True)
            caps[capability] = "unknown" if found is None else "available" if found else "unavailable"
    else:
        values = {**cache["static"], **await client.get(cache.get("dynamic", []))}
    interfaces = parse_interfaces(values)
    caps["interfaces"] = "available" if interfaces else "unavailable"
    caps["if_hc_counters"] = "available" if any(p["counters"][d + "_bits"] == 64 for p in interfaces for d in ("in", "out")) else "unavailable"
    poe_ports, poe_supplies = {}, {}
    if discover or caps.get("poe") != "unavailable":
        port_data, supply_data = await optional(POE_PORTS), await optional(POE_SUPPLIES)
        poe_ports, poe_supplies = parse_poe(port_data or {}, supply_data or {})
        caps["poe"] = "available" if poe_ports or poe_supplies else "unknown" if port_data is None or supply_data is None else "unavailable"
    sensors = []
    if discover or caps.get("sensors") != "unavailable":
        sensor_data = await optional(SENSORS)
        sensors = parse_sensors(sensor_data or {})
        caps["sensors"] = "unknown" if sensor_data is None else "available" if sensor_data else "unavailable"
        caps["temperature"] = "available" if any(s["kind"] == 8 for s in sensors) else "unavailable"
        if any(s["kind"] == 10 for s in sensors):
            caps["fan"] = "available"
    cache["capabilities"], cache["sensors"] = caps, sensors
    snapshot = dict(system=system, interfaces=interfaces, capabilities=caps, inventory=cache,
                    poe_ports=poe_ports, poe_supplies=poe_supplies, discovered=discover,
                    rebooted=reset, diagnostics=diagnostics, **cache.get("identity", {}))
    adapter = adapter_for(system["sys_object_id"])
    if adapter:
        try:
            async with asyncio.timeout(5):
                await adapter.enrich(client, snapshot)
        except Exception:
            # Дополнение не отменяет уже полученные стандартные данные.
            diagnostics["vendor"] = "unavailable"
    return snapshot
