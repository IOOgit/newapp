"""Точки расширения. Адаптеры дополняют стандартный снимок, не подменяют poller.

Регистрировать адаптер допустимо только с официальной MIB и тестовым walk.
Неизвестный sysObjectID всегда обслуживается стандартными MIB.
"""
from typing import Protocol

from ..client import Client


class VendorAdapter(Protocol):
    async def enrich(self, client: Client, snapshot: dict) -> None: ...


ADAPTERS: dict[str, VendorAdapter] = {}


def adapter_for(sys_object_id):
    for prefix in sorted(ADAPTERS, key=len, reverse=True):
        if sys_object_id == prefix or (sys_object_id or "").startswith(prefix + "."):
            return ADAPTERS[prefix]
    return None
