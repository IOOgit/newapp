"""Валидируемые настройки свитчей; секрет существует только во входном запросе."""
from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SwitchFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("host", check_fields=False)
    @classmethod
    def host_only(cls, value):
        if value is None:
            return value
        value = value.strip()
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
        labels = value.rstrip(".").split(".")
        if len(value) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
            raise ValueError("Укажите IP или имя узла без URL, пути и номера порта")
        if all(part.isdigit() for part in labels):
            raise ValueError("Некорректный IP-адрес")
        return value

    @field_validator("name", "model", check_fields=False)
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Значение не должно быть пустым")
        return value.strip() if value is not None else value


class SwitchCreate(SwitchFields):
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=253)
    model: Literal["DH-CS4226-24ET-240"] = "DH-CS4226-24ET-240"
    group_id: int | None = Field(default=None, gt=0)
    snmp_port: int = Field(default=161, ge=1, le=65535)
    snmp_version: Literal["2c"] = "2c"
    community: SecretStr = Field(min_length=1, max_length=255)
    timeout: float = Field(default=2, ge=0.5, le=5, allow_inf_nan=False)
    retries: int = Field(default=1, ge=0, le=2)
    enabled: bool = True


class SwitchUpdate(SwitchFields):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    model: Literal["DH-CS4226-24ET-240"] | None = None
    group_id: int | None = Field(default=None, gt=0)
    snmp_port: int | None = Field(default=None, ge=1, le=65535)
    snmp_version: Literal["2c"] | None = None
    community: SecretStr | None = Field(default=None, max_length=255)
    timeout: float | None = Field(default=None, ge=0.5, le=5, allow_inf_nan=False)
    retries: int | None = Field(default=None, ge=0, le=2)
    enabled: bool | None = None


class SwitchPortUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_up: bool | None = None
    channel_ref_id: int | None = Field(default=None, gt=0)
    poe_index: str | None = Field(default=None, pattern=r"^[1-9][0-9]*\.[1-9][0-9]*$", max_length=30)
