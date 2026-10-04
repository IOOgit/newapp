"""Валидируемые настройки свитчей; секрет существует только во входном запросе."""
from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


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

    @field_validator("name", check_fields=False)
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Значение не должно быть пустым")
        return value.strip() if value is not None else value


class SwitchCreate(SwitchFields):
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=253)
    model: str = Field(default="", max_length=128)
    group_id: int | None = Field(default=None, gt=0)
    management_port: int = Field(default=80, ge=1, le=65535)
    timeout: float = Field(default=2, ge=0.5, le=5, allow_inf_nan=False)
    enabled: bool = True
    snmp_enabled: bool = False
    snmp_version: Literal["1", "2c"] = "2c"
    snmp_port: int = Field(default=161, ge=1, le=65535)
    retries: int = Field(default=1, ge=0, le=2)
    community: SecretStr | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def credentials_required(self):
        if self.snmp_enabled and (not self.community or not self.community.get_secret_value()):
            raise ValueError("Для включения SNMP укажите community")
        return self


class SwitchUpdate(SwitchFields):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    model: str | None = Field(default=None, max_length=128)
    group_id: int | None = Field(default=None, gt=0)
    management_port: int | None = Field(default=None, ge=1, le=65535)
    timeout: float | None = Field(default=None, ge=0.5, le=5, allow_inf_nan=False)
    enabled: bool | None = None
    snmp_enabled: bool | None = None
    snmp_version: Literal["1", "2c"] | None = None
    snmp_port: int | None = Field(default=None, ge=1, le=65535)
    retries: int | None = Field(default=None, ge=0, le=2)
    community: SecretStr | None = Field(default=None, max_length=255)


class SwitchProbe(SwitchFields):
    switch_id: int | None = Field(default=None, gt=0)
    host: str = Field(min_length=1, max_length=253)
    snmp_version: Literal["1", "2c"] = "2c"
    snmp_port: int = Field(default=161, ge=1, le=65535)
    timeout: float = Field(default=2, ge=0.5, le=5, allow_inf_nan=False)
    retries: int = Field(default=1, ge=0, le=2)
    community: SecretStr | None = Field(default=None, max_length=255)


class SwitchPortUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_up: bool | None = None
    channel_ref_id: int | None = Field(default=None, gt=0)
    poe_index: str | None = Field(default=None, pattern=r"^[1-9][0-9]*\.[1-9][0-9]*$", max_length=30)
