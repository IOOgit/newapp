"""Реестр возможностей (вкладок) — единый источник правды по доступу.

Добавление новой вкладки в будущем = одна запись в ``CAPABILITIES`` + один гейт
``can(key)`` в шаблоне. Права выдаёт владелец конкретному пользователю; владелец
(``is_owner``) видит всё безусловно, его ``permissions`` игнорируются.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    key: str            # ключ права (хранится в User.permissions)
    label: str          # русское название вкладки (для UI управления)
    prefixes: tuple     # URL-префиксы, которые открывает это право


# Выдаваемые владельцем возможности (вкладки)
CAPABILITIES = [
    Capability(
        "monitoring",
        "Мониторинг (дашборд, устройства, TV, история, план, прошивки, хранилище)",
        ("/time-settings", "/tv", "/slideshow", "/history", "/plan", "/firmware", "/calc", "/labels", "/m/",
         "/devices/", "/api/groups", "/api/devices", "/api/summary", "/api/events",
         "/api/alerts", "/api/archive", "/api/plan", "/plan/image", "/api/bulk",
         "/api/notes", "/api/metrics", "/api/monitoring", "/switches", "/api/switches"),
    ),
    Capability("worklist", "Доска проблем", ("/worklist", "/api/worklist")),
    Capability("audit", "Аудит действий", ("/audit", "/api/audit")),
    Capability("checkin", "Заселения (просмотр, клипы, вердикты)",
               ("/checkin", "/clips")),  # /api/checkin/* — кроме onec, см. path_allowed
    Capability("onec", "1С:Отель (интеграция, синхронизация, сверка)",
               ("/api/checkin/onec",)),  # + видимость карточки 1С в настройках заселений
    Capability("buses", "Автобусы (учёт дисков)",
               ("/buses", "/disks", "/assets", "/api/buses", "/api/disks", "/api/assets",
                "/api/asset-batches")),
    Capability("backup", "Бэкап (экспорт/восстановление)", ("/backup", "/api/backup")),
]

ALL_KEYS = [c.key for c in CAPABILITIES]
CAP_BY_KEY = {c.key: c for c in CAPABILITIES}

# Доступно ЛЮБОМУ вошедшему (оболочка приложения, самообслуживание)
BASE_PREFIXES = ("/logout", "/static", "/sw.js", "/offline", "/manifest.webmanifest", "/healthz")

# Порядок стартовой страницы: (ключ права, путь)
_START_ORDER = [
    ("monitoring", "/"), ("checkin", "/checkin"), ("buses", "/buses"),
    ("worklist", "/worklist"), ("audit", "/audit"), ("backup", "/backup"),
]


def path_allowed(path: str, caps: list[str], is_owner: bool) -> bool:
    """Разрешён ли путь при данных правах. Владелец — всё."""
    if is_owner:
        return True
    if path == "/" and "monitoring" in caps:
        return True   # дашборд
    if path.startswith(BASE_PREFIXES):
        return True
    # 1С — более специфичный префикс, проверяем ПЕРЕД общим checkin
    if path.startswith("/api/checkin/onec"):
        return "onec" in caps
    # общий модуль заселений (кроме onec-эндпоинтов)
    if path.startswith(("/checkin", "/clips", "/api/checkin")):
        return "checkin" in caps
    for key in caps:
        cap = CAP_BY_KEY.get(key)
        if cap and path.startswith(cap.prefixes):
            return True
    return False


def start_page(is_owner: bool, caps: list[str]) -> str | None:
    """Стартовая страница пользователя. None — прав нет вообще (заглушка)."""
    if is_owner or "monitoring" in caps:
        return "/"
    for key, url in _START_ORDER:
        if key in caps:
            return url
    return None


def normalize_permissions(keys) -> list[str]:
    """Отфильтровать ключи прав по реестру, сохранив порядок реестра, без дублей."""
    given = set(keys or [])
    return [k for k in ALL_KEYS if k in given]


def has_cap(request, key: str) -> bool:
    """Есть ли у текущей сессии право key (владелец — всегда да)."""
    return bool(request.session.get("is_owner")) or key in request.session.get("caps", [])


def require_cap(key: str):
    """FastAPI-зависимость для точечной защиты чувствительного эндпоинта."""
    from fastapi import HTTPException, Request

    def _dep(request: Request):
        if not has_cap(request, key):
            raise HTTPException(403, "Недостаточно прав")
        return True

    return _dep
