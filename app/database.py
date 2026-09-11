"""Асинхронный движок и сессии SQLAlchemy."""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


def _prepare_sqlite_path(url: str) -> None:
    """Создаёт каталог для файла SQLite, если его нет."""
    marker = ":///"
    if "sqlite" in url and marker in url:
        path = url.split(marker, 1)[1]
        if path and path != ":memory:":
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)


_prepare_sqlite_path(settings.database_url)

engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Зависимость FastAPI: выдаёт сессию БД."""
    async with SessionLocal() as session:
        yield session


# Лёгкие миграции: новые колонки, которые create_all НЕ добавит к уже
# существующим таблицам. (table, column, DDL-тип). Без Alembic для простоты.
_NEW_COLUMNS = [
    ("devices", "monitoring_checks", "JSON DEFAULT '{}'"),
    ("hdds", "raw_status", "VARCHAR(128)"),
    ("hdds", "present", "BOOLEAN DEFAULT TRUE"),
    ("channels", "recording_mode", "VARCHAR(16) DEFAULT 'continuous'"),
    ("channels", "recording_status", "VARCHAR(16) DEFAULT 'unknown'"),
    ("channels", "recording_checked_at", "TIMESTAMP"),
    ("channels", "recording_last_end", "TIMESTAMP"),
    ("channels", "recording_age_seconds", "INTEGER"),
    ("channels", "recording_error", "TEXT"),
    ("devices", "cpu_load", "FLOAT"),
    ("devices", "memory_usage", "FLOAT"),
    ("devices", "temperature", "FLOAT"),
    ("devices", "latitude", "FLOAT"),
    ("devices", "longitude", "FLOAT"),
    ("channels", "archive_depth_days", "INTEGER"),
    ("channels", "quality", "VARCHAR(16)"),
    ("channels", "quality_checked_at", "TIMESTAMP"),
    ("channels", "frame_sig", "TEXT"),
    ("channels", "frozen_count", "INTEGER DEFAULT 0"),
    ("swap_log", "user", "VARCHAR(64)"),
    ("disks", "location", "VARCHAR(16) DEFAULT 'shelf'"),
    ("disks", "last_audit_at", "TIMESTAMP"),
    ("buses", "collect_weekday", "INTEGER"),
    ("buses", "location", "VARCHAR(128)"),
    ("buses", "problem_note", "TEXT"),
    ("buses", "has_problem", "BOOLEAN DEFAULT FALSE"),
    ("buses", "swap_alert_enabled", "BOOLEAN DEFAULT FALSE"),
    ("devices", "auth_failures", "INTEGER DEFAULT 0"),
    ("disks", "batch_id", "INTEGER"),
    ("disks", "warranty_until", "DATE"),
    ("assets", "assigned_bus_id", "INTEGER"),
    ("checkin_clips", "download_bytes", "INTEGER"),
    ("checkin_clips", "download_ms", "INTEGER"),
    ("checkin_ingest_runs", "dl_bytes", "INTEGER DEFAULT 0"),
    ("checkin_ingest_runs", "dl_ms", "INTEGER DEFAULT 0"),
    ("checkin_recorders", "use_https", "BOOLEAN DEFAULT FALSE"),
    ("checkin_recorders", "model_info", "VARCHAR(255)"),
    ("checkin_clips", "width", "INTEGER"),
    ("checkin_clips", "height", "INTEGER"),
    ("checkin_recorders", "time_offset_sec", "INTEGER"),
    ("checkin_recorders", "time_offset_at", "TIMESTAMP"),
    ("users", "is_owner", "BOOLEAN DEFAULT FALSE"),
    ("users", "permissions", "TEXT DEFAULT '[]'"),
    ("users", "enabled", "BOOLEAN DEFAULT TRUE"),
    ("users", "display_name", "VARCHAR(128)"),
    ("users", "last_login_at", "TIMESTAMP"),
    # 1С: реальная схема Document_Accommodation + $expand=Room
    ("onec_connections", "field_date_fallback", "VARCHAR(128) DEFAULT 'Date'"),
    ("onec_connections", "expand_room", "VARCHAR(128) DEFAULT 'Room'"),
    ("onec_connections", "field_room_ref", "VARCHAR(128) DEFAULT 'Room_Key'"),
    ("onec_connections", "field_room_number", "VARCHAR(128) DEFAULT 'Description'"),
    ("onec_connections", "field_room_floor", "VARCHAR(128) DEFAULT 'Floor'"),
    ("onec_connections", "lookback_days", "INTEGER DEFAULT 5"),
    ("onec_connections", "field_query_date", "VARCHAR(128) DEFAULT 'Date'"),
    ("onec_connections", "service_url", "TEXT"),
]


def _lightweight_migrate(sync_conn) -> None:
    import logging

    from sqlalchemy import inspect, text

    log = logging.getLogger("nvrmon.migrate")
    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    for table, column, ddl in _NEW_COLUMNS:
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        if column not in existing:
            # Имена в кавычках — column может быть зарезервированным словом (напр. "user" в Postgres).
            # SAVEPOINT, чтобы сбой одной миграции не ронял весь старт и не «портил» транзакцию.
            try:
                with sync_conn.begin_nested():
                    sync_conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
            except Exception as exc:  # noqa: BLE001
                log.warning("Миграция %s.%s пропущена: %s", table, column, exc)


async def init_db() -> None:
    """Создаёт таблицы и добавляет недостающие колонки (для MVP — без Alembic)."""
    from app import models  # noqa: F401  (регистрация моделей)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_lightweight_migrate)
    await ensure_owner_and_migrate_roles()


async def ensure_owner_and_migrate_roles() -> None:
    """Разовая конвертация legacy-ролей в права + бутстрап владельца.

    1. У пользователей с пустыми permissions выводим права из legacy-role:
       admin → все ключи, bus → ["buses"] (чтобы никто не потерял доступ).
    2. Владелец: логин из OWNER_USERNAME (по умолчанию IOO). Если есть —
       делаем владельцем; если нет и задан OWNER_PASSWORD — создаём; иначе
       предупреждаем в лог.
    3. Если задан OWNER_PASSWORD и владелец уже есть — сбрасываем ему пароль
       (единственный путь аварийного восстановления вместо env-админа).
    """
    import logging

    from sqlalchemy import select

    from app.config import settings
    from app.models import User
    from app.permissions import ALL_KEYS
    from app.services import users as users_svc

    log = logging.getLogger("nvrmon.owner")
    async with SessionLocal() as session:
        rows = list((await session.execute(select(User))).scalars())
        for u in rows:
            if not u.permissions:
                u.permissions = list(ALL_KEYS) if u.role == "admin" else ["buses"]

        owner_name = (settings.owner_username or "IOO").strip()
        owner = next((u for u in rows if u.username == owner_name), None)
        if owner is not None:
            owner.is_owner = True
            owner.enabled = True
            if not owner.permissions:
                owner.permissions = list(ALL_KEYS)
            if settings.owner_password:
                owner.password_hash = users_svc.hash_password(settings.owner_password)
                log.warning("Пароль владельца «%s» сброшен из OWNER_PASSWORD — очистите переменную", owner_name)
        elif settings.owner_password:
            owner = User(
                username=owner_name,
                password_hash=users_svc.hash_password(settings.owner_password),
                is_owner=True, enabled=True, permissions=list(ALL_KEYS),
            )
            session.add(owner)
            log.warning("Создан владелец панели «%s» из OWNER_PASSWORD — очистите переменную", owner_name)
        else:
            log.warning("Владелец «%s» не найден. Задайте OWNER_PASSWORD для создания/сброса.", owner_name)
        await session.commit()
