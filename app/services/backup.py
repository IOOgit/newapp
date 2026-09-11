"""Резервное копирование/восстановление важных данных (портативный JSON).

Бэкапим конфигурацию и РУЧНЫЕ данные (их не восстановить опросом):
устройства, группы, автобусы, диски, журналы, заметки, метки плана, учётки,
настройки. Каналы включены для сохранения режима записи и привязок портов.
Коммутаторы и назначенные порты сохраняются с зашифрованными секретами.
HDD и события регенерируются опросом — их не включаем.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import logging
import os

from sqlalchemy import Date, DateTime, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting, Bus, Channel, Device, Disk, DiskReview, Group, NetworkSwitch,
    Note, PlanMarker, SwitchPort, SwapLog, User, utcnow,
)

log = logging.getLogger(__name__)

# Порядок ВСТАВКИ (родители раньше). Удаление — в обратном порядке.
_MODELS = [Group, Device, Channel, NetworkSwitch, SwitchPort,
           Bus, Disk, SwapLog, DiskReview, Note, PlanMarker, User, AppSetting]
BACKUP_DIR = "data/backups"
_KEEP = 30


def _enc(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


def _dec(col, v):
    if v is None:
        return None
    if isinstance(col.type, DateTime) and isinstance(v, str):
        return dt.datetime.fromisoformat(v)
    if isinstance(col.type, Date) and isinstance(v, str):
        return dt.date.fromisoformat(v)
    return v


async def export_data(session: AsyncSession) -> dict:
    out = {"_meta": {"created": utcnow().isoformat(), "version": 1}, "tables": {}}
    for m in _MODELS:
        rows = (await session.execute(select(m))).scalars().all()
        out["tables"][m.__tablename__] = [
            {c.name: _enc(getattr(r, c.name)) for c in m.__table__.columns} for r in rows
        ]
    return out


async def import_data(session: AsyncSession, data: dict) -> dict:
    """Полное восстановление: затирает перечисленные таблицы и заливает из бэкапа."""
    tables = data.get("tables", {})
    # удаляем в обратном порядке (дети раньше родителей)
    for m in reversed(_MODELS):
        await session.execute(delete(m))
    counts = {}
    for m in _MODELS:
        rows = tables.get(m.__tablename__, [])
        for row in rows:
            kwargs = {c.name: _dec(c, row.get(c.name)) for c in m.__table__.columns if c.name in row}
            session.add(m(**kwargs))
        counts[m.__tablename__] = len(rows)
    await session.commit()
    # для Postgres — поправить счётчики автоинкремента, иначе конфликт id
    if session.bind.dialect.name == "postgresql":
        for m in _MODELS:
            if "id" in m.__table__.columns:
                await session.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{m.__tablename__}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {m.__tablename__}), 1), true)"
                ))
        await session.commit()
    return counts


async def auto_backup() -> str | None:
    """Ежедневный авто-бэкап в файл; хранит последние _KEEP штук."""
    from app.database import SessionLocal

    os.makedirs(BACKUP_DIR, exist_ok=True)
    async with SessionLocal() as session:
        data = await export_data(session)
    fname = os.path.join(BACKUP_DIR, f"backup-{dt.datetime.now():%Y%m%d-%H%M}.json")
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")))
    for old in files[:-_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    log.info("Авто-бэкап: %s", fname)
    return fname


def list_backups() -> list[dict]:
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")), reverse=True)
    out = []
    for f in files:
        st = os.stat(f)
        out.append({"name": os.path.basename(f),
                    "size_kb": round(st.st_size / 1024, 1),
                    "time": dt.datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")})
    return out
