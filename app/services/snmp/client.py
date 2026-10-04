"""Асинхронный транспорт SNMP v1/v2c. Секреты и сырые ошибки наружу не выходят."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pysnmp.hlapi.v3arch import asyncio as hlapi
from pysnmp.proto import errind, rfc1902, rfc1905


MESSAGES = {
    "timeout": "Нет ответа SNMP: проверьте адрес, UDP-порт, community и ACL",
    "auth_error": "Агент SNMP отклонил доступ",
    "network": "Сеть или адрес SNMP недоступны",
    "unsupported": "Агент не предоставил запрошенные OID",
    "protocol": "Некорректный ответ SNMP",
    "limit": "Превышен безопасный предел SNMP-таблицы",
    "credentials": "Community не задана или не удалось её расшифровать",
}


class SnmpError(Exception):
    def __init__(self, status="unavailable", code="protocol"):
        self.status, self.code = status, code
        super().__init__(MESSAGES.get(code, MESSAGES["protocol"]))


@dataclass(frozen=True)
class Connection:
    host: str
    port: int = 161
    version: str = "2c"
    community: str = field(default="", repr=False)
    timeout: float = 2
    retries: int = 1


class Client(Protocol):
    """Граница транспорта: будущий v3 использует тот же poller и модели данных."""
    async def get(self, oids: list[str]) -> dict: ...
    async def walk(self, root: str, *, limit: int = 20000, partial: bool = False) -> dict: ...


def _value(value):
    if isinstance(value, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance, rfc1905.EndOfMibView)):
        return None
    if isinstance(value, rfc1902.ObjectIdentifier):
        return str(value)
    if isinstance(value, rfc1902.OctetString):
        return value.asOctets()
    try:
        return int(value)
    except (ValueError, TypeError):
        return str(value)


def check_error(indication, status):
    if indication:
        if indication == errind.requestTimedOut:
            # v1/v2c часто молча отбрасывают неверную community: это timeout,
            # а не достоверно установленная ошибка авторизации.
            raise SnmpError("timeout", "timeout")
        if indication in (errind.authenticationFailure, errind.unknownCommunityName):
            raise SnmpError("auth_error", "auth_error")
        raise SnmpError("unavailable", "network")
    if status:
        code = int(status)
        if code in (6, 16):
            raise SnmpError("auth_error", "auth_error")
        raise SnmpError("unavailable", "unsupported" if code == 2 else "protocol")


class SnmpClient:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.engine = None

    async def __aenter__(self):
        c = self.connection
        if c.version not in ("1", "2c"):
            raise SnmpError("unavailable", "unsupported")
        if not c.community:
            raise SnmpError("auth_error", "credentials")
        self.engine = hlapi.SnmpEngine()
        self.auth = hlapi.CommunityData(c.community, mpModel=0 if c.version == "1" else 1)
        self.context = hlapi.ContextData()
        target = hlapi.Udp6TransportTarget if ":" in c.host else hlapi.UdpTransportTarget
        try:
            self.target = await target.create((c.host, c.port), timeout=c.timeout, retries=c.retries)
        except BaseException:
            self.engine.close_dispatcher()
            raise
        return self

    async def __aexit__(self, *_):
        if self.engine:
            self.engine.close_dispatcher()

    async def _request(self, command, *args):
        try:
            return await command(self.engine, self.auth, self.target, self.context, *args, lookupMib=False)
        except SnmpError:
            raise
        except Exception:
            raise SnmpError("unavailable", "network") from None

    async def _get_chunk(self, oids):
        if not oids:
            return {}
        reply = await self._request(hlapi.get_cmd, *((rfc1902.ObjectName(oid), rfc1902.Null("")) for oid in oids))
        indication, status, index, bindings = reply
        if not indication and int(status) == 1 and len(oids) > 1:
            middle = len(oids) // 2
            return {**await self._get_chunk(oids[:middle]), **await self._get_chunk(oids[middle:])}
        if not indication and int(status) == 2 and 0 < int(index) <= len(oids):
            # SNMPv1 noSuchName аннулирует весь GET: повторяем оставшиеся OID.
            remaining = oids[:int(index)-1] + oids[int(index):]
            return await self._get_chunk(remaining)
        check_error(indication, status)
        return {str(oid): val for oid, value in bindings if (val := _value(value)) is not None}

    async def get(self, oids):
        result = {}
        for start in range(0, len(oids), 24):
            result.update(await self._get_chunk(oids[start:start+24]))
        return result

    async def walk(self, root, *, limit=20000, partial=False):
        result, cursor = {}, root
        repetitions = 25 if self.connection.version == "2c" else 1
        for _ in range(limit + 1):
            binding = (rfc1902.ObjectName(cursor), rfc1902.Null(""))
            if repetitions > 1:
                reply = await self._request(hlapi.bulk_cmd, 0, repetitions, binding)
            else:
                reply = await self._request(hlapi.next_cmd, binding)
            indication, status, _index, bindings = reply
            if not indication and int(status) in (1, 5) and repetitions > 1:
                repetitions = 1  # урезанные агенты без корректного GETBULK
                continue
            if not indication and int(status) == 2:
                return result
            check_error(indication, status)
            if not bindings:
                return result
            for oid, value in bindings:
                name, value = str(oid), _value(value)
                if value is None or not name.startswith(root + "."):
                    return result
                if tuple(map(int, name.split("."))) <= tuple(map(int, cursor.split("."))):
                    raise SnmpError("unavailable", "protocol")
                result[name], cursor = value, name
                if len(result) >= limit:
                    if partial:
                        return result
                    raise SnmpError("unavailable", "limit")
        raise SnmpError("unavailable", "limit")
