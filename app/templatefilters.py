"""Jinja-фильтры панели.

Время в БД хранится в UTC (`utcnow()`), а показывать его надо в местном часовом
поясе — иначе события «отстают» на разницу с UTC. Фильтр `localtime` переводит
UTC→локаль по настройке `settings.timezone`.
"""
from __future__ import annotations

import datetime as dt

from app.config import settings


def _tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.timezone or "UTC")
    except Exception:  # noqa: BLE001  (нет zoneinfo/tzdata или кривое имя)
        return dt.timezone.utc


def localtime(value, fmt: str = "%d.%m %H:%M") -> str:
    """Дату/время → строка в местном поясе. None → «—». Дата без времени — как есть."""
    if value is None:
        return "—"
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)  # в БД — UTC
        value = value.astimezone(_tz())
    return value.strftime(fmt)


def days_since(value) -> int | None:
    """Сколько полных дней прошло с момента value (UTC). None → None.

    Нужен индикатору «срок диска» на карточках автобусов: возраст установки
    считается на отрисовке, бэкенд не трогаем.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return max((dt.datetime.now(dt.timezone.utc) - value).days, 0)


def register(templates) -> None:
    """Подключить фильтры и глобальные переменные к окружению Jinja."""
    from app import __version__

    templates.env.filters["localtime"] = localtime
    templates.env.filters["days_since"] = days_since
    # Версия приложения доступна во всех шаблонах как {{ app_version }}.
    templates.env.globals["app_version"] = __version__
