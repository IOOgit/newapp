"""Модуль «Автобусы»: ручной офлайн-учёт дисковой ротации.

Никакой сети/пингов — все статусы вносятся руками. Атомарная операция «Замена
диска» одним диалогом + отдельные действия для нестандартных ситуаций.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.config import settings
from app.database import get_session
from app.models import (
    ASSET_KINDS,
    ASSET_STATUSES,
    COLLECTION_ISSUES,
    DISK_LOCATIONS,
    OBSERVATION_TAGS,
    WEEKDAYS,
    Asset,
    AssetBatch,
    AssetStatus,
    Bus,
    BusProblemLog,
    Disk,
    DiskLocation,
    DiskReview,
    DiskStatus,
    SwapLog,
    utcnow,
)
from app.services import appsettings

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402

_register_filters(templates)

router = APIRouter(tags=["buses"])


async def _thresholds(session: AsyncSession) -> tuple[int, int]:
    """Пороги (замена, просмотр) — из БД, иначе из .env."""
    swap = await appsettings.get_int(session, "bus_swap_alert_days", settings.bus_swap_alert_days)
    review = await appsettings.get_int(session, "bus_review_alert_days", settings.bus_review_alert_days)
    return swap, review


# ── Статус автобуса (для индикации) ─────────────────────────────────────────────
def _aware(d: dt.datetime | None) -> dt.datetime | None:
    """Приводим время к timezone-aware (БД может отдавать наивное)."""
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def bus_status(bus: Bus, disks: list[Disk], swap_days: int = 14) -> tuple[str, str]:
    """Возвращает (цвет, причина): red/orange/yellow/green."""
    if bus.installed_disk_id is None:
        return "red", "регистратор без диска"
    if bus.has_problem:
        return "red", "отмечена проблема"
    # Напоминание о замене диска — только если включено для этого автобуса.
    if bus.swap_alert_enabled:
        since = _aware(bus.installed_since)
        if since is not None and (utcnow() - since).days >= swap_days:
            days = (utcnow() - since).days
            return "orange", f"диск стоит {days} дн. — пора менять"
    # Резерв = закреплённый за автобусом готовый диск (кроме установленного).
    others = [d for d in disks if d.assigned_bus_id == bus.id and d.id != bus.installed_disk_id]
    if any(d.status == DiskStatus.READY for d in others):
        return "green", "всё ок"
    if not others:
        return "yellow", "нет закреплённого резерва"
    if any(d.status == DiskStatus.REMOVED_REVIEW for d in others):
        return "yellow", "резерв не готов (второй диск на просмотре)"
    return "yellow", "резерв не готов"


_RANK = {"red": 0, "orange": 1, "yellow": 2, "green": 3}


def _nat(s: str | None):
    """Натуральный ключ сортировки: '5' раньше '10', None в конец."""
    s = (s or "").strip()
    if not s:
        return (2, "")
    return (0, int(s)) if s.isdigit() else (1, s.lower())


async def _disks(session: AsyncSession) -> list[Disk]:
    return list((await session.execute(select(Disk))).scalars())


async def _bus(session: AsyncSession, bus_id: int) -> Bus:
    bus = (await session.execute(select(Bus).where(Bus.id == bus_id))).scalar_one_or_none()
    if bus is None:
        raise HTTPException(404, "Автобус не найден")
    return bus


async def _disk(session: AsyncSession, disk_id: int) -> Disk:
    disk = (await session.execute(select(Disk).where(Disk.id == disk_id))).scalar_one_or_none()
    if disk is None:
        raise HTTPException(404, "Диск не найден")
    return disk


# ── REST: автобусы ──────────────────────────────────────────────────────────────
@router.post("/api/buses/settings")
async def set_bus_settings(data: schemas.BusSettings, session: AsyncSession = Depends(get_session)):
    await appsettings.set_value(session, "bus_swap_alert_days", max(data.swap_days, 1))
    await appsettings.set_value(session, "bus_review_alert_days", max(data.review_days, 1))
    return {"ok": True}


@router.get("/api/buses")
async def list_buses(session: AsyncSession = Depends(get_session)):
    buses = list((await session.execute(select(Bus))).scalars())
    disks = await _disks(session)
    swap_days, _ = await _thresholds(session)
    by_id = {d.id: d for d in disks}
    out = []
    for b in buses:
        color, reason = bus_status(b, disks, swap_days)
        cur = by_id.get(b.installed_disk_id) if b.installed_disk_id else None
        out.append({
            "id": b.id, "bus_number": b.bus_number, "route": b.route,
            "dvr_model": b.dvr_model, "has_problem": b.has_problem,
            "swap_alert_enabled": b.swap_alert_enabled,
            "installed_disk": (f"{cur.label} ({cur.type})" if cur else None),
            "installed_since": b.installed_since.isoformat() if b.installed_since else None,
            "color": color, "reason": reason,
        })
    out.sort(key=lambda x: (_RANK[x["color"]], x["bus_number"]))
    return out


@router.post("/api/buses")
async def create_bus(data: schemas.BusCreate, session: AsyncSession = Depends(get_session)):
    bus = Bus(bus_number=data.bus_number, route=data.route, dvr_model=data.dvr_model)
    session.add(bus)
    await session.commit()
    return {"id": bus.id}


async def _open_problem_entry(session: AsyncSession, bus_id: int) -> BusProblemLog | None:
    """Текущая (незакрытая) запись истории проблем автобуса."""
    return (await session.execute(
        select(BusProblemLog)
        .where(BusProblemLog.bus_id == bus_id, BusProblemLog.closed_at.is_(None))
        .order_by(BusProblemLog.id.desc())
    )).scalars().first()


@router.put("/api/buses/{bus_id}")
async def update_bus(bus_id: int, data: schemas.BusUpdate, request: Request,
                     session: AsyncSession = Depends(get_session)):
    bus = await _bus(session, bus_id)
    payload = data.model_dump(exclude_unset=True)
    for k, v in payload.items():
        setattr(bus, k, v)
    # История проблем: пометка открывает запись, снятие — закрывает,
    # смена формулировки — закрывает старую и открывает новую.
    if "has_problem" in payload or "problem_note" in payload:
        user = request.session.get("user")
        entry = await _open_problem_entry(session, bus_id)
        if bus.has_problem:
            if entry is None:
                session.add(BusProblemLog(bus_id=bus_id, note=bus.problem_note, opened_by=user))
            elif (bus.problem_note or None) != (entry.note or None):
                entry.closed_at = utcnow()
                entry.closed_by = user
                session.add(BusProblemLog(bus_id=bus_id, note=bus.problem_note, opened_by=user))
        elif entry is not None:
            entry.closed_at = utcnow()
            entry.closed_by = user
    await session.commit()
    return {"ok": True}


@router.delete("/api/buses/{bus_id}")
async def delete_bus(bus_id: int, session: AsyncSession = Depends(get_session)):
    bus = await _bus(session, bus_id)
    # Освобождаем диски этого автобуса, чтобы не остались «установленными» без
    # автобуса (висячая ссылка). Стоявший диск возвращаем в резерв на полку.
    for d in (await session.execute(
            select(Disk).where(Disk.assigned_bus_id == bus.id))).scalars():
        d.assigned_bus_id = None
        if d.status == DiskStatus.INSTALLED or d.id == bus.installed_disk_id:
            d.status = DiskStatus.READY
            d.status_since = utcnow()
            d.location = DiskLocation.SHELF
    # История проблем удалённого автобуса больше не нужна
    await session.execute(delete(BusProblemLog).where(BusProblemLog.bus_id == bus.id))
    await session.delete(bus)
    await session.commit()
    return {"ok": True}


# ── REST: диски ──────────────────────────────────────────────────────────────────
@router.get("/api/disks")
async def list_disks(session: AsyncSession = Depends(get_session)):
    disks = await _disks(session)
    buses = {b.id: b.bus_number for b in (await session.execute(select(Bus))).scalars()}
    return [
        {"id": d.id, "label": d.label, "type": d.type, "capacity_gb": d.capacity_gb,
         "status": d.status, "assigned_bus_id": d.assigned_bus_id,
         "assigned_bus": buses.get(d.assigned_bus_id), "note": d.note,
         "status_since": d.status_since.isoformat() if d.status_since else None}
        for d in disks
    ]


@router.post("/api/disks")
async def create_disk(data: schemas.DiskCreate, session: AsyncSession = Depends(get_session)):
    disk = Disk(label=data.label, type=data.type, capacity_gb=data.capacity_gb,
                assigned_bus_id=data.assigned_bus_id, status=data.status, note=data.note)
    session.add(disk)
    await session.commit()
    return {"id": disk.id}


@router.put("/api/disks/{disk_id}")
async def update_disk(disk_id: int, data: schemas.DiskUpdate, session: AsyncSession = Depends(get_session)):
    disk = await _disk(session, disk_id)
    fields = data.model_dump(exclude_unset=True)
    # Место установленного диска руками не меняем — оно задаётся установкой/снятием.
    if ("location" in fields and disk.status == DiskStatus.INSTALLED
            and fields["location"] != DiskLocation.IN_BUS):
        raise HTTPException(400, "У установленного диска нельзя менять место — он в автобусе. Сначала снимите его.")
    if ("assigned_bus_id" in fields and disk.status == DiskStatus.INSTALLED
            and fields["assigned_bus_id"] != disk.assigned_bus_id):
        raise HTTPException(400, "Установленный диск закреплён за своим автобусом — сначала снимите его.")
    # «В автобусе» — только через установку (Замену), не руками.
    if ("location" in fields and fields["location"] == DiskLocation.IN_BUS
            and disk.status != DiskStatus.INSTALLED):
        raise HTTPException(400, "Место «в автобусе» ставится установкой диска через «Замену».")
    for k, v in fields.items():
        setattr(disk, k, v)
    # Синхронизируем статус с местом, чтобы они не расходились
    # (установленный/неисправный не трогаем — у них своя логика).
    if "location" in fields and disk.status not in (DiskStatus.INSTALLED, DiskStatus.FAULTY):
        if disk.location == DiskLocation.SHELF and disk.status in (
                DiskStatus.REMOVED_REVIEW, DiskStatus.REVIEWED):
            disk.status = DiskStatus.READY          # вернули на полку → резерв
            disk.status_since = utcnow()
        elif disk.location in (DiskLocation.REVIEWER, DiskLocation.TRANSIT) and (
                disk.status == DiskStatus.READY):
            disk.status = DiskStatus.REMOVED_REVIEW  # резерв отдали на (пере)просмотр
            disk.status_since = utcnow()
    await session.commit()
    return {"ok": True}


@router.delete("/api/disks/{disk_id}")
async def delete_disk(disk_id: int, session: AsyncSession = Depends(get_session)):
    """Удалить диск из реестра. Если он числится установленным в автобусе — сначала
    снимаем ссылку у автобуса (он останется без диска), чтобы не было висячей
    ссылки. История замен/наблюдений остаётся в журналах как есть."""
    disk = await _disk(session, disk_id)
    bus = (await session.execute(
        select(Bus).where(Bus.installed_disk_id == disk.id))).scalar_one_or_none()
    if bus is not None:
        bus.installed_disk_id = None
        bus.installed_since = None
    await session.delete(disk)
    await session.commit()
    return {"ok": True}



@router.post("/api/disks/{disk_id}/reviewed")
async def disk_reviewed(disk_id: int, session: AsyncSession = Depends(get_session)):
    """Отметить диск просмотренным. Диск остаётся У СМОТРЯЩЕГО (место не меняем) —
    на полку он возвращается отдельным действием «вернуть на полку»."""
    disk = await _disk(session, disk_id)
    if disk.status != DiskStatus.REMOVED_REVIEW:
        raise HTTPException(400, "Просмотренным можно пометить только диск 'на просмотре'")
    disk.status = DiskStatus.REVIEWED
    disk.status_since = utcnow()
    await session.commit()
    return {"ok": True}


@router.post("/api/disks/{disk_id}/to-shelf")
async def disk_to_shelf(disk_id: int, session: AsyncSession = Depends(get_session)):
    """Вернуть диск на полку → готов (резерв). Из 'на просмотре' или 'просмотрен'."""
    disk = await _disk(session, disk_id)
    if disk.status not in (DiskStatus.REMOVED_REVIEW, DiskStatus.REVIEWED):
        raise HTTPException(400, "Вернуть на полку можно диск, который на просмотре или просмотрен")
    disk.status = DiskStatus.READY
    disk.status_since = utcnow()
    disk.location = DiskLocation.SHELF
    await session.commit()
    return {"ok": True}


@router.post("/api/disks/{disk_id}/faulty")
async def disk_faulty(disk_id: int, data: schemas.FaultyRequest, session: AsyncSession = Depends(get_session)):
    disk = await _disk(session, disk_id)
    # если был установлен — автобус остаётся без диска, а диск уже не «в автобусе»
    if disk.assigned_bus_id:
        bus = (await session.execute(select(Bus).where(Bus.id == disk.assigned_bus_id))).scalar_one_or_none()
        if bus and bus.installed_disk_id == disk.id:
            bus.installed_disk_id = None
            bus.installed_since = None
    if disk.location == DiskLocation.IN_BUS:
        disk.location = DiskLocation.SHELF
    disk.status = DiskStatus.FAULTY
    disk.status_since = utcnow()
    if data.note:
        disk.note = data.note
    await session.commit()
    return {"ok": True}


# ── Ключевая операция: замена/снятие/установка ──────────────────────────────────
async def _perform_swap(session: AsyncSession, bus: Bus, new: Disk | None, note: str | None, who: str | None):
    now = utcnow()
    removed = None
    if bus.installed_disk_id:
        removed = await _disk(session, bus.installed_disk_id)
    if removed:
        removed.status = DiskStatus.REMOVED_REVIEW
        removed.status_since = now
        removed.location = DiskLocation.REVIEWER  # снят → у смотрящего
    if new:
        new.status = DiskStatus.INSTALLED
        new.status_since = now
        new.assigned_bus_id = bus.id
        new.location = DiskLocation.IN_BUS
        bus.installed_disk_id = new.id
        bus.installed_since = now
    else:
        bus.installed_disk_id = None
        bus.installed_since = None
    session.add(SwapLog(
        date=now, bus_id=bus.id,
        removed_disk_id=removed.id if removed else None,
        installed_disk_id=new.id if new else None,
        note=note, user=who,
    ))
    await session.commit()


@router.post("/api/buses/{bus_id}/swap")
async def swap_disk(bus_id: int, data: schemas.SwapRequest, request: Request,
                    session: AsyncSession = Depends(get_session)):
    """Атомарно: снять текущий диск (→ на просмотр) и/или установить выбранный."""
    bus = await _bus(session, bus_id)
    new = None
    if data.installed_disk_id:
        new = await _disk(session, data.installed_disk_id)
        if new.status in (DiskStatus.INSTALLED, DiskStatus.FAULTY, DiskStatus.WRITTEN_OFF):
            raise HTTPException(400, f"Нельзя установить диск со статусом «{new.status}»")
        if new.assigned_bus_id not in (None, bus.id) and not data.force:
            raise HTTPException(409, "Диск закреплён за другим автобусом — подтвердите установку")
    await _perform_swap(session, bus, new, data.note, request.session.get("user"))
    return {"ok": True}


@router.post("/api/buses/{bus_id}/swap-reserve")
async def swap_reserve(bus_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Замена «парой» в один тап: ставит готовый резерв этого автобуса."""
    bus = await _bus(session, bus_id)
    disks = await _disks(session)
    reserve = next(
        (d for d in disks if d.assigned_bus_id == bus.id and d.status == DiskStatus.READY), None
    )
    if reserve is None:
        raise HTTPException(400, "Нет готового резерва, закреплённого за этим автобусом")
    await _perform_swap(session, bus, reserve, "замена парой", request.session.get("user"))
    return {"ok": True, "installed": reserve.label}


# Префикс заметки для записей «диск не собрали» (без замены диска).
NOT_COLLECTED_PREFIX = "не собрали: "


@router.post("/api/buses/{bus_id}/not-collected")
async def bus_not_collected(bus_id: int, data: schemas.NotCollectedRequest, request: Request,
                            session: AsyncSession = Depends(get_session)):
    """Отметить в день сбора, что диск не забрали (с причиной). Замены диска нет —
    пишем запись в журнал, автобус считается обработанным на сегодня."""
    bus = await _bus(session, bus_id)
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(400, "Укажите причину")
    session.add(SwapLog(
        date=utcnow(), bus_id=bus.id, removed_disk_id=None, installed_disk_id=None,
        note=NOT_COLLECTED_PREFIX + reason, user=request.session.get("user"),
    ))
    await session.commit()
    return {"ok": True}


@router.post("/api/disks/{disk_id}/restore")
async def disk_restore(disk_id: int, session: AsyncSession = Depends(get_session)):
    """Вернуть неисправный диск в строй (faulty → ready, резерв)."""
    disk = await _disk(session, disk_id)
    if disk.status not in (DiskStatus.FAULTY, DiskStatus.WRITTEN_OFF):
        raise HTTPException(400, "Вернуть в строй можно только неисправный/списанный диск")
    disk.status = DiskStatus.READY
    disk.status_since = utcnow()
    disk.location = DiskLocation.SHELF
    await session.commit()
    return {"ok": True}


@router.post("/api/disks/{disk_id}/write-off")
async def disk_write_off(disk_id: int, data: schemas.WriteOffRequest,
                         session: AsyncSession = Depends(get_session)):
    """Списать диск (вывести из эксплуатации). Установленный — нельзя, сначала снять."""
    disk = await _disk(session, disk_id)
    if disk.status == DiskStatus.INSTALLED:
        raise HTTPException(400, "Нельзя списать установленный диск — сначала снимите/замените его")
    bus = (await session.execute(
        select(Bus).where(Bus.installed_disk_id == disk.id))).scalar_one_or_none()
    if bus is not None:
        bus.installed_disk_id = None
        bus.installed_since = None
    disk.status = DiskStatus.WRITTEN_OFF
    disk.status_since = utcnow()
    if data.reason:
        disk.note = f"списан: {data.reason}"
    await session.commit()
    return {"ok": True}


@router.post("/api/buses/{bus_id}/replace-faulty")
async def replace_faulty(bus_id: int, data: schemas.ReplaceFaultyRequest, request: Request,
                         session: AsyncSession = Depends(get_session)):
    """Замена по неисправности: снять текущий диск как НЕИСПРАВНЫЙ (не на просмотр)
    и сразу поставить замену (готовый резерв автобуса или указанный диск)."""
    bus = await _bus(session, bus_id)
    if not bus.installed_disk_id:
        raise HTTPException(400, "В автобусе нет установленного диска")
    old = await _disk(session, bus.installed_disk_id)
    if data.new_disk_id:
        new = await _disk(session, data.new_disk_id)
    else:
        disks = await _disks(session)
        new = next((d for d in disks if d.assigned_bus_id == bus.id and d.status == DiskStatus.READY), None)
        if new is None:
            raise HTTPException(400, "Нет готового резерва для этого автобуса — укажите диск явно")
    if new.status in (DiskStatus.INSTALLED, DiskStatus.FAULTY, DiskStatus.WRITTEN_OFF):
        raise HTTPException(400, f"Нельзя поставить диск со статусом «{new.status}»")

    now = utcnow()
    reason = (data.reason or "вышел из строя").strip()
    # старый диск → неисправен (а не на просмотр — он сломан)
    old.status = DiskStatus.FAULTY
    old.status_since = now
    if old.location == DiskLocation.IN_BUS:
        old.location = DiskLocation.SHELF
    old.note = f"неисправен: {reason}"
    # новый → установлен
    new.status = DiskStatus.INSTALLED
    new.status_since = now
    new.assigned_bus_id = bus.id
    new.location = DiskLocation.IN_BUS
    bus.installed_disk_id = new.id
    bus.installed_since = now
    session.add(SwapLog(
        date=now, bus_id=bus.id, removed_disk_id=old.id, installed_disk_id=new.id,
        note=f"замена по неисправности: {reason}", user=request.session.get("user"),
    ))
    await session.commit()
    return {"ok": True, "installed": new.label, "removed": old.label}


@router.post("/api/asset-batches")
async def create_asset_batch(data: schemas.BatchCreate, request: Request,
                             session: AsyncSession = Depends(get_session)):
    """Поступление партии. Для дисков сразу создаёт qty единиц на складе (ready)."""
    qty = max(int(data.qty or 0), 0)
    batch = AssetBatch(
        kind=data.kind, model=data.model.strip(), vendor=(data.vendor or None),
        supplier=(data.supplier or None), qty=qty, unit_cost=data.unit_cost,
        warranty_until=data.warranty_until, note=(data.note or None),
        user=request.session.get("user"),
    )
    session.add(batch)
    await session.flush()  # получить batch.id
    created = 0
    prefix = (data.label_prefix or f"П{batch.id}-").strip()
    if data.kind == "disk":
        for i in range(1, qty + 1):
            session.add(Disk(
                label=f"{prefix}{i}", type=(data.disk_type or DiskType.SSD),
                capacity_gb=data.capacity_gb, status=DiskStatus.READY,
                location=DiskLocation.SHELF, assigned_bus_id=data.assigned_bus_id,
                batch_id=batch.id, warranty_until=data.warranty_until,
                note=f"партия: {batch.model}",
            ))
            created += 1
    elif data.kind in ("nvr", "camera"):
        # если сразу указан автобус — ставим в работу, иначе на склад
        st = AssetStatus.DEPLOYED if data.assigned_bus_id else AssetStatus.IN_STOCK
        for i in range(1, qty + 1):
            session.add(Asset(
                kind=data.kind, label=f"{prefix}{i}", model=batch.model, vendor=batch.vendor,
                status=st, assigned_bus_id=data.assigned_bus_id,
                batch_id=batch.id, warranty_until=data.warranty_until,
                note=f"партия: {batch.model}",
            ))
            created += 1
    await session.commit()
    return {"id": batch.id, "created": created}


async def _asset(session: AsyncSession, asset_id: int) -> Asset:
    a = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Актив не найден")
    return a


@router.post("/api/assets")
async def create_asset(data: schemas.AssetCreate, session: AsyncSession = Depends(get_session)):
    a = Asset(
        kind=data.kind, label=data.label.strip(), model=(data.model or None),
        serial=(data.serial or None), vendor=(data.vendor or None),
        status=data.status or AssetStatus.IN_STOCK, assigned_bus_id=data.assigned_bus_id,
        warranty_until=data.warranty_until, note=(data.note or None),
    )
    session.add(a)
    await session.commit()
    return {"id": a.id}


@router.put("/api/assets/{asset_id}")
async def update_asset(asset_id: int, data: schemas.AssetUpdate, session: AsyncSession = Depends(get_session)):
    a = await _asset(session, asset_id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    await session.commit()
    return {"ok": True}


@router.delete("/api/assets/{asset_id}")
async def delete_asset(asset_id: int, session: AsyncSession = Depends(get_session)):
    a = await _asset(session, asset_id)
    await session.delete(a)
    await session.commit()
    return {"ok": True}


@router.post("/api/assets/{asset_id}/deploy")
async def deploy_asset(asset_id: int, data: schemas.AssetAction, session: AsyncSession = Depends(get_session)):
    """Установить актив в автобус (in_stock → deployed, закрепляем за автобусом)."""
    a = await _asset(session, asset_id)
    if not data.bus_id:
        raise HTTPException(400, "Укажите автобус для установки")
    await _bus(session, data.bus_id)  # проверка, что автобус существует
    a.status = AssetStatus.DEPLOYED
    a.status_since = utcnow()
    a.assigned_bus_id = data.bus_id
    await session.commit()
    return {"ok": True}


@router.post("/api/assets/{asset_id}/faulty")
async def asset_faulty(asset_id: int, data: schemas.AssetAction, session: AsyncSession = Depends(get_session)):
    a = await _asset(session, asset_id)
    a.status = AssetStatus.FAULTY
    a.status_since = utcnow()
    if data.reason:
        a.note = f"неисправен: {data.reason}"
    await session.commit()
    return {"ok": True}


@router.post("/api/assets/{asset_id}/restore")
async def asset_restore(asset_id: int, session: AsyncSession = Depends(get_session)):
    a = await _asset(session, asset_id)
    a.status = AssetStatus.IN_STOCK
    a.status_since = utcnow()
    a.assigned_bus_id = None
    await session.commit()
    return {"ok": True}


@router.post("/api/assets/{asset_id}/write-off")
async def asset_write_off(asset_id: int, data: schemas.AssetAction, session: AsyncSession = Depends(get_session)):
    a = await _asset(session, asset_id)
    a.status = AssetStatus.WRITTEN_OFF
    a.status_since = utcnow()
    if data.reason:
        a.note = f"списан: {data.reason}"
    await session.commit()
    return {"ok": True}


@router.post("/api/assets/{asset_id}/replace")
async def replace_asset(asset_id: int, data: schemas.AssetReplace, session: AsyncSession = Depends(get_session)):
    """Замена по неисправности: старый → неисправен, новый (со склада) → на его место."""
    old = await _asset(session, asset_id)
    new = await _asset(session, data.new_id)
    if new.kind != old.kind:
        raise HTTPException(400, "Заменять можно активом того же типа")
    if new.status in (AssetStatus.DEPLOYED, AssetStatus.WRITTEN_OFF):
        raise HTTPException(400, f"Нельзя поставить актив со статусом «{ASSET_STATUSES.get(new.status, new.status)}»")
    now = utcnow()
    reason = (data.reason or "вышел из строя").strip()
    bus_id = old.assigned_bus_id
    old.status = AssetStatus.FAULTY
    old.status_since = now
    old.assigned_bus_id = None
    old.note = f"неисправен: {reason}"
    new.status = AssetStatus.DEPLOYED
    new.status_since = now
    new.assigned_bus_id = bus_id           # встаёт на тот же автобус
    new.note = f"замена {old.label} ({reason})"
    await session.commit()
    return {"ok": True, "installed": new.label, "removed": old.label}


@router.get("/disks/{disk_id}/qr.png")
async def disk_qr(disk_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """QR-код со ссылкой на паспорт диска (для наклейки на сам диск)."""
    from fastapi.responses import Response

    import qrcode

    disk = (await session.execute(select(Disk).where(Disk.id == disk_id))).scalar_one_or_none()
    if disk is None:
        raise HTTPException(404, "Диск не найден")
    base = str(request.base_url).rstrip("/")
    img = qrcode.make(f"{base}/disks/{disk.id}/passport")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/disks/labels", response_class=HTMLResponse)
async def disk_labels_page(request: Request, session: AsyncSession = Depends(get_session)):
    """Печать QR-наклеек на диски (для ревизии сканированием)."""
    disks = [d for d in await _disks(session) if d.status != DiskStatus.WRITTEN_OFF]
    disks.sort(key=lambda d: d.label or "")
    return templates.TemplateResponse("disk_labels.html", {"request": request, "disks": disks})


@router.post("/api/disks/audit")
async def disks_audit(data: schemas.AuditRequest, session: AsyncSession = Depends(get_session)):
    """Ревизия: отметить подтверждённые (физически найденные) диски."""
    now = utcnow()
    ids = set(data.present_ids)
    for d in await _disks(session):
        if d.id in ids:
            d.last_audit_at = now
    await session.commit()
    return {"ok": True, "confirmed": len(ids)}


@router.post("/api/disks/{disk_id}/review")
async def disk_review(disk_id: int, data: schemas.ReviewRequest, request: Request,
                      session: AsyncSession = Depends(get_session)):
    """Записать наблюдение по диску (теги + заметка); опц. пометить готовым."""
    disk = await _disk(session, disk_id)
    session.add(DiskReview(
        disk_id=disk.id, bus_id=disk.assigned_bus_id,
        tags=data.tags or [], note=data.note, user=request.session.get("user"),
    ))
    # отражаем последнее наблюдение в заметке диска (для быстрого взгляда)
    summary = ", ".join(data.tags or [])
    if data.note:
        summary = (summary + " · " + data.note) if summary else data.note
    if summary:
        disk.note = summary
    if data.finish and disk.status == DiskStatus.REMOVED_REVIEW:
        # Просмотрен, но остаётся у смотрящего (на полку — отдельной кнопкой).
        disk.status = DiskStatus.REVIEWED
        disk.status_since = utcnow()
    await session.commit()
    return {"ok": True}


# ── Журнал замен ─────────────────────────────────────────────────────────────────
async def _swaplog(session: AsyncSession, bus_id: int | None = None, disk_id: int | None = None):
    q = select(SwapLog).order_by(SwapLog.date.desc())
    if bus_id is not None:
        q = q.where(SwapLog.bus_id == bus_id)
    rows = list((await session.execute(q)).scalars())
    if disk_id is not None:
        rows = [r for r in rows if disk_id in (r.removed_disk_id, r.installed_disk_id)]
    return rows


@router.get("/api/buses/swaplog.csv")
async def swaplog_csv(bus_id: int | None = None, disk_id: int | None = None,
                      session: AsyncSession = Depends(get_session)):
    rows = await _swaplog(session, bus_id, disk_id)
    buses = {b.id: b.bus_number for b in (await session.execute(select(Bus))).scalars()}
    disks = {d.id: d.label for d in await _disks(session)}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Автобус", "Снят диск", "Установлен диск", "Кто", "Заметка"])
    for r in rows:
        w.writerow([
            r.date.strftime("%Y-%m-%d %H:%M"), buses.get(r.bus_id, r.bus_id),
            disks.get(r.removed_disk_id, "") if r.removed_disk_id else "",
            disks.get(r.installed_disk_id, "") if r.installed_disk_id else "",
            r.user or "", r.note or "",
        ])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=swaplog.csv"})


async def _problem_rows(session: AsyncSession) -> list[dict]:
    """Автобусы с отмеченной проблемой (кнопка «проблема» на странице автобуса)."""
    buses = list((await session.execute(
        select(Bus).where(Bus.has_problem).order_by(Bus.route, Bus.bus_number)
    )).scalars())
    disks = {d.id: d for d in await _disks(session)}
    # Сколько раз проблема отмечалась за всю историю — для динамики на листе.
    counts: dict[int, int] = {}
    for row in (await session.execute(select(BusProblemLog.bus_id))).scalars():
        counts[row] = counts.get(row, 0) + 1
    return [{
        "bus": b, "disk": disks.get(b.installed_disk_id),
        # проблемы, отмеченные до появления истории, в журнал не попали — минимум 1
        "times": max(counts.get(b.id, 0), 1),
    } for b in buses]


async def _chronic_rows(session: AsyncSession, limit: int = 10) -> list[dict]:
    """Топ «хронических» автобусов: проблема отмечалась 2+ раза за всю историю."""
    stats: dict[int, dict] = {}
    for bus_id, opened_at, closed_at in (await session.execute(
        select(BusProblemLog.bus_id, BusProblemLog.opened_at, BusProblemLog.closed_at)
    )).all():
        s = stats.setdefault(bus_id, {"times": 0, "last": None, "open": False})
        s["times"] += 1
        if s["last"] is None or opened_at > s["last"]:
            s["last"] = opened_at
        if closed_at is None:
            s["open"] = True
    chronic_ids = [bid for bid, s in stats.items() if s["times"] >= 2]
    if not chronic_ids:
        return []
    buses = {b.id: b for b in (await session.execute(
        select(Bus).where(Bus.id.in_(chronic_ids)))).scalars()}
    rows = [{"bus": buses[bid], **stats[bid]} for bid in chronic_ids if bid in buses]
    rows.sort(key=lambda r: (-r["times"], r["bus"].bus_number))
    return rows[:limit]


@router.get("/buses/analytics", response_class=HTMLResponse)
async def bus_analytics_page(request: Request, session: AsyncSession = Depends(get_session)):
    """Аналитика проблем: по маршрутам, по месяцам, «хроника» — на базе bus_problem_log."""
    buses = {b.id: b for b in (await session.execute(select(Bus))).scalars()}
    entries = list((await session.execute(select(BusProblemLog))).scalars())
    now = utcnow()

    fixed_days = [max((_aware(e.closed_at) - _aware(e.opened_at)).days, 0)
                  for e in entries if e.closed_at]
    totals = {
        "total": len(entries),
        "last30": sum(1 for e in entries if (now - _aware(e.opened_at)).days <= 30),
        "open": sum(1 for e in entries if e.closed_at is None),
        "avg_fix": round(sum(fixed_days) / len(fixed_days), 1) if fixed_days else None,
    }

    # — по маршрутам: где болит чаще всего —
    by_route: dict[str, dict] = {}
    for e in entries:
        b = buses.get(e.bus_id)
        route = (b.route if b and b.route else "без маршрута")
        r = by_route.setdefault(route, {"route": route, "total": 0, "open": 0, "buses": set()})
        r["total"] += 1
        if e.closed_at is None:
            r["open"] += 1
        r["buses"].add(e.bus_id)
    routes = sorted(by_route.values(), key=lambda r: (-r["total"], _nat(r["route"])))
    max_route = max((r["total"] for r in routes), default=1)
    for r in routes:
        r["buses"] = len(r["buses"])
        r["pct"] = round(r["total"] / max_route * 100, 1)

    # — по месяцам: последние 12, динамика во времени —
    y, m = now.year, now.month
    keys = []
    for _ in range(12):
        keys.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    per_month = {k: 0 for k in keys}
    for e in entries:
        op = _aware(e.opened_at)
        k = (op.year, op.month)
        if k in per_month:
            per_month[k] += 1
    max_month = max(per_month.values(), default=0) or 1
    months = [{
        "label": f"{mm:02d}.{yy % 100:02d}", "count": per_month[(yy, mm)],
        "pct": round(per_month[(yy, mm)] / max_month * 100),
    } for yy, mm in keys]

    chronic = await _chronic_rows(session)
    return templates.TemplateResponse("bus_analytics.html", {
        "request": request, "totals": totals, "routes": routes,
        "months": months, "chronic": chronic,
    })


@router.get("/buses/problems", response_class=HTMLResponse)
async def bus_problems_page(request: Request, session: AsyncSession = Depends(get_session)):
    """Печатная сводка проблем: распечатать или сохранить в файл — интернет есть не везде."""
    rows = await _problem_rows(session)
    chronic = await _chronic_rows(session)
    return templates.TemplateResponse("bus_problems.html", {
        "request": request, "rows": rows, "chronic": chronic,
        "printed_at": dt.datetime.now(),
    })


@router.get("/api/buses/problems.csv")
async def bus_problems_csv(session: AsyncSession = Depends(get_session)):
    """Выгрузка отмеченных проблем в CSV (Excel-совместимый, ;-разделитель)."""
    rows = await _problem_rows(session)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Автобус", "Маршрут", "Модель DVR", "Где стоит", "Диск", "Диск с",
                "Проблема", "Раз отмечалась"])
    for r in rows:
        b, d = r["bus"], r["disk"]
        w.writerow([
            b.bus_number, b.route or "", b.dvr_model or "", b.location or "",
            d.label if d else "нет диска",
            b.installed_since.strftime("%Y-%m-%d") if b.installed_since else "",
            b.problem_note or "проблема (без описания)",
            r["times"],
        ])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=bus_problems.csv"})


# ── Страницы ─────────────────────────────────────────────────────────────────────
@router.get("/buses", response_class=HTMLResponse)
async def buses_page(request: Request, q: str = "", sort: str = "status",
                     session: AsyncSession = Depends(get_session)):
    """Список автобусов: номер, закреплённые диски, отметка проблемы.

    Упрощённая логика статуса (без дисковой ротации): проблема → red,
    нет закреплённых дисков → yellow, иначе green.
    """
    buses = list((await session.execute(select(Bus))).scalars())
    disks = await _disks(session)
    assigned_map: dict[int, list[Disk]] = {}
    for d in disks:
        if d.assigned_bus_id and d.status != DiskStatus.WRITTEN_OFF:
            assigned_map.setdefault(d.assigned_bus_id, []).append(d)
    # сколько раз проблема отмечалась за историю — «N-й раз» на карточке
    counts: dict[int, int] = {}
    for bid in (await session.execute(select(BusProblemLog.bus_id))).scalars():
        counts[bid] = counts.get(bid, 0) + 1
    rows = []
    for b in buses:
        if q and q.lower() not in (f"{b.bus_number} {b.route or ''}").lower():
            continue
        assigned = sorted(assigned_map.get(b.id, []), key=lambda d: _nat(d.label))
        if b.has_problem:
            color = "red"
        elif not assigned:
            color = "yellow"
        else:
            color = "green"
        rows.append({"bus": b, "color": color, "assigned": assigned,
                     "times": counts.get(b.id, 0)})
    # Сортировка: по статусу (проблемные сверху), по маршруту или по номеру
    if sort in ("route", "group"):
        rows.sort(key=lambda x: (_nat(x["bus"].route), _nat(x["bus"].bus_number)))
    elif sort == "number":
        rows.sort(key=lambda x: _nat(x["bus"].bus_number))
    else:
        rows.sort(key=lambda x: (_RANK[x["color"]], _nat(x["bus"].bus_number)))
    # Группировка по маршрутам (подзаголовки)
    grouped = None
    if sort == "group":
        grouped = []
        for r in rows:
            label = r["bus"].route or "Без маршрута"
            if not grouped or grouped[-1][0] != label:
                grouped.append((label, []))
            grouped[-1][1].append(r)
    summary = {
        "buses": len(buses),
        "problems": sum(1 for b in buses if b.has_problem),
        "no_assigned": sum(1 for b in buses if not assigned_map.get(b.id)),
        "disks_assigned": sum(len(v) for v in assigned_map.values()),
    }
    return templates.TemplateResponse("buses.html", {
        "request": request, "rows": rows, "grouped": grouped,
        "q": q, "sort": sort, "summary": summary,
    })


@router.get("/buses/swaplog", response_class=HTMLResponse)
async def swaplog_page(request: Request, bus_id: int | None = None, disk_id: int | None = None,
                       session: AsyncSession = Depends(get_session)):
    rows = await _swaplog(session, bus_id, disk_id)
    buses = {b.id: b for b in (await session.execute(select(Bus))).scalars()}
    disks = {d.id: d for d in await _disks(session)}
    filter_label = ""
    if disk_id and disk_id in disks:
        filter_label = f"диск {disks[disk_id].label}"
    elif bus_id and bus_id in buses:
        filter_label = f"автобус {buses[bus_id].bus_number}"
    return templates.TemplateResponse("swaplog.html", {
        "request": request, "rows": rows, "buses": buses, "disks": disks,
        "bus_id": bus_id, "disk_id": disk_id, "filter_label": filter_label,
    })


@router.get("/buses/collection", response_class=HTMLResponse)
async def collection_page(request: Request, today: int = 0, session: AsyncSession = Depends(get_session)):
    """День сбора дисков: быстрый проход по списку с заменой в один клик."""
    buses = list((await session.execute(select(Bus))).scalars())
    disks = await _disks(session)
    by_id = {d.id: d for d in disks}
    ready = [d for d in disks if d.status == DiskStatus.READY]
    swap_days, _ = await _thresholds(session)

    today_date = dt.datetime.now().date()
    today_wd = dt.datetime.now().weekday()
    swapped_today = set()
    issue_today: dict[int, str] = {}  # bus_id -> причина «не собрали» за сегодня
    for log in await _swaplog(session):
        d = log.date
        local = d.astimezone().replace(tzinfo=None) if d.tzinfo else d
        if local.date() == today_date:
            swapped_today.add(log.bus_id)
            if (log.removed_disk_id is None and log.installed_disk_id is None
                    and log.note and log.note.startswith(NOT_COLLECTED_PREFIX)):
                issue_today[log.bus_id] = log.note[len(NOT_COLLECTED_PREFIX):]

    rows = []
    for b in sorted(buses, key=lambda b: (_nat(b.route), _nat(b.bus_number))):
        if today and b.collect_weekday != today_wd:   # фильтр «сегодня к сбору»
            continue
        color, reason = bus_status(b, disks, swap_days)
        rows.append({
            "bus": b, "color": color, "reason": reason,
            "disk": by_id.get(b.installed_disk_id) if b.installed_disk_id else None,
            "done": b.id in swapped_today,
            "issue": issue_today.get(b.id),
            "ready": sorted(ready, key=lambda d: 0 if d.assigned_bus_id == b.id else 1),
        })
    planned_today = sum(1 for b in buses if b.collect_weekday == today_wd)
    return templates.TemplateResponse("collection.html", {
        "request": request, "rows": rows, "today_only": bool(today),
        "total": len(rows), "done": sum(1 for r in rows if r["done"]),
        "planned_today": planned_today, "weekday": WEEKDAYS[today_wd],
        "issues": COLLECTION_ISSUES,
    })


@router.get("/buses/plan", response_class=HTMLResponse)
async def plan_page(request: Request, session: AsyncSession = Depends(get_session)):
    buses = sorted(
        (await session.execute(select(Bus))).scalars(),
        key=lambda b: (_nat(b.route), _nat(b.bus_number)),
    )
    # Группируем по маршрутам — чтобы можно было назначить день всему маршруту разом.
    groups: list[dict] = []
    for b in buses:
        label = b.route or "Без маршрута"
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "buses": []})
        groups[-1]["buses"].append(b)
    return templates.TemplateResponse("plan.html", {
        "request": request, "groups": groups, "weekdays": WEEKDAYS,
    })


@router.get("/buses/review", response_class=HTMLResponse)
async def review_queue(request: Request, session: AsyncSession = Depends(get_session)):
    """Очередь на просмотр: диски в статусе 'на просмотре'."""
    disks = await _disks(session)
    buses = {b.id: b for b in (await session.execute(select(Bus))).scalars()}
    now = utcnow()
    waiting_items, held_items = [], []
    for d in disks:
        if d.status not in (DiskStatus.REMOVED_REVIEW, DiskStatus.REVIEWED):
            continue
        since = _aware(d.status_since)
        days = (now - since).days if since else 0
        row = {"disk": d, "bus": buses.get(d.assigned_bus_id), "waiting": days}
        (waiting_items if d.status == DiskStatus.REMOVED_REVIEW else held_items).append(row)
    waiting_items.sort(key=lambda x: -x["waiting"])  # дольше всех ждут — сверху
    held_items.sort(key=lambda x: -x["waiting"])
    return templates.TemplateResponse("review.html", {
        "request": request, "waiting_items": waiting_items, "held_items": held_items,
        "tags": OBSERVATION_TAGS,
    })


@router.post("/api/buses/stats/reset")
async def reset_stats(request: Request, session: AsyncSession = Depends(get_session)):
    """Обнулить статистику: очистить журнал замен и записи просмотров.
    Доступно владельцу или пользователю с полным доступом (право «monitoring»)."""
    if not (request.session.get("is_owner") or "monitoring" in request.session.get("caps", [])):
        raise HTTPException(403, "Обнуление статистики доступно администратору")
    swaps = len((await session.execute(select(SwapLog))).scalars().all())
    reviews = len((await session.execute(select(DiskReview))).scalars().all())
    await session.execute(delete(SwapLog))
    await session.execute(delete(DiskReview))
    await session.commit()
    return {"ok": True, "removed": {"swaps": swaps, "reviews": reviews}}


@router.get("/buses/stats", response_class=HTMLResponse)
async def bus_stats(request: Request, days: int = 30, session: AsyncSession = Depends(get_session)):
    since = utcnow() - dt.timedelta(days=days)
    now = utcnow()
    reviews = [
        r for r in (await session.execute(select(DiskReview))).scalars()
        if _aware(r.created_at) >= since
    ]
    # Только реальные замены (записи «не собрали» без движения диска не считаем).
    swaps = [
        s for s in await _swaplog(session)
        if _aware(s.date) >= since and (s.removed_disk_id or s.installed_disk_id)
    ]
    buses = {b.id: b for b in (await session.execute(select(Bus))).scalars()}
    disks = await _disks(session)
    swap_days, _ = await _thresholds(session)

    # Сводка по парку и дискам
    park = {
        "buses": len(buses),
        "no_disk": sum(1 for b in buses.values() if b.installed_disk_id is None),
        "overdue": sum(
            1 for b in buses.values()
            if b.swap_alert_enabled and _aware(b.installed_since)
            and (now - _aware(b.installed_since)).days >= swap_days
        ),
    }
    disk_stats = {
        "total": len(disks),
        "installed": sum(1 for d in disks if d.status == DiskStatus.INSTALLED),
        "ready": sum(1 for d in disks if d.status == DiskStatus.READY),
        "review": sum(1 for d in disks if d.status == DiskStatus.REMOVED_REVIEW),
        "reviewed": sum(1 for d in disks if d.status == DiskStatus.REVIEWED),
        "faulty": sum(1 for d in disks if d.status == DiskStatus.FAULTY),
    }

    tag_counts: dict[str, int] = {}
    bus_problems: dict[int, int] = {}
    for r in reviews:
        problem = False
        for t in (r.tags or []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
            if t != "ок":
                problem = True
        if problem and r.bus_id:
            bus_problems[r.bus_id] = bus_problems.get(r.bus_id, 0) + 1

    top_buses = sorted(
        ({"bus": buses.get(bid), "bid": bid, "count": n} for bid, n in bus_problems.items()),
        key=lambda x: -x["count"],
    )[:15]
    tag_rows = sorted(tag_counts.items(), key=lambda kv: -kv[1])

    # Кто делал замены (по сотрудникам)
    by_user: dict[str, int] = {}
    for s in swaps:
        by_user[s.user or "—"] = by_user.get(s.user or "—", 0) + 1
    user_rows = sorted(by_user.items(), key=lambda kv: -kv[1])

    return templates.TemplateResponse("stats.html", {
        "request": request, "days": days, "top_buses": top_buses, "tag_rows": tag_rows,
        "reviews_total": len(reviews), "swaps_total": len(swaps),
        "park": park, "disk_stats": disk_stats, "user_rows": user_rows,
    })


@router.get("/buses/{bus_id}", response_class=HTMLResponse)
async def bus_page(bus_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    bus = (await session.execute(select(Bus).where(Bus.id == bus_id))).scalar_one_or_none()
    if bus is None:
        return HTMLResponse("Автобус не найден", status_code=404)
    disks = await _disks(session)
    assigned = sorted((d for d in disks if d.assigned_bus_id == bus.id
                       and d.status != DiskStatus.WRITTEN_OFF), key=lambda d: _nat(d.label))
    # свободные диски — для закрепления прямо со страницы автобуса
    free_disks = sorted((d for d in disks if not d.assigned_bus_id
                         and d.status != DiskStatus.WRITTEN_OFF), key=lambda d: _nat(d.label))
    color = "red" if bus.has_problem else ("yellow" if not assigned else "green")
    problem_log = list((await session.execute(
        select(BusProblemLog).where(BusProblemLog.bus_id == bus_id)
        .order_by(BusProblemLog.id.desc()).limit(50)
    )).scalars())
    # Оборудование автобуса: закреплённые регистраторы/камеры (не списанные)
    equipment = [
        a for a in (await session.execute(
            select(Asset).where(Asset.assigned_bus_id == bus.id))).scalars()
        if a.status != AssetStatus.WRITTEN_OFF
    ]
    equipment.sort(key=lambda a: (a.kind, a.label))
    return templates.TemplateResponse("bus.html", {
        "request": request, "bus": bus, "color": color,
        "assigned": assigned, "free_disks": free_disks,
        "problem_log": problem_log, "equipment": equipment,
        "kinds": ASSET_KINDS, "asset_statuses": ASSET_STATUSES,
    })


@router.get("/api/buses/collection.csv")
async def collection_csv(session: AsyncSession = Depends(get_session)):
    """Ведомость на сбор: автобус, маршрут, текущий диск, готов ли резерв."""
    buses = list((await session.execute(select(Bus))).scalars())
    disks = await _disks(session)
    by_id = {d.id: d for d in disks}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Автобус", "Маршрут", "Диск в регистраторе", "Резерв готов", "Заметка по диску"])
    for b in sorted(buses, key=lambda b: (_nat(b.route), _nat(b.bus_number))):
        cur = by_id.get(b.installed_disk_id) if b.installed_disk_id else None
        reserve = any(d.assigned_bus_id == b.id and d.status == DiskStatus.READY for d in disks)
        w.writerow([b.bus_number, b.route or "", cur.label if cur else "НЕТ ДИСКА",
                    "да" if reserve else "нет", (cur.note if cur else "") or ""])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=collection.csv"})


@router.get("/disks", response_class=HTMLResponse)
async def disks_page(request: Request, status: str = "", q: str = "", sort: str = "status",
                     session: AsyncSession = Depends(get_session)):
    all_disks = await _disks(session)
    buses = {b.id: b.bus_number for b in (await session.execute(select(Bus))).scalars()}
    # дубли меток (без учёта регистра)
    seen: dict[str, int] = {}
    for d in all_disks:
        key = (d.label or "").strip().lower()
        seen[key] = seen.get(key, 0) + 1
    dup_labels = sorted({d.label for d in all_disks if seen.get((d.label or "").strip().lower(), 0) > 1})

    disks = all_disks
    if status:
        disks = [d for d in disks if d.status == status]
    if q:
        ql = q.lower()
        disks = [
            d for d in disks
            if ql in (d.label or "").lower() or ql in (d.note or "").lower()
            or ql in (buses.get(d.assigned_bus_id, "") or "").lower()
        ]
    disks = sorted(disks, key=lambda d: (d.status, d.label))
    sort_keys = {
        "status": lambda d: (d.status, (d.label or "").lower()),
        "label": lambda d: _nat(d.label),
        "type": lambda d: ((d.type or ""), (d.label or "").lower()),
        "capacity": lambda d: (-(d.capacity_gb or 0), (d.label or "").lower()),
        "bus": lambda d: (d.assigned_bus_id is None, _nat(buses.get(d.assigned_bus_id) or "")),
    }
    disks = sorted(disks, key=sort_keys.get(sort, sort_keys["status"]))
    bus_options = sorted(buses.items(), key=lambda kv: _nat(kv[1]))  # для закрепления
    # Подсказка автобуса по метке диска: первый «токен» метки == номер автобуса.
    # Только подсказка (одним кликом закрепить), не закрепляем насильно.
    num_to_bus = {(bn or "").strip().upper(): (bid, bn) for bid, bn in buses.items()}
    suggested: dict[int, tuple] = {}
    for d in disks:
        if d.assigned_bus_id is None and d.status != DiskStatus.INSTALLED and d.label:
            tok = d.label.strip().split()[0].split("(")[0].strip().upper()
            if tok and tok in num_to_bus:
                suggested[d.id] = num_to_bus[tok]
    return templates.TemplateResponse("disks.html", {
        "request": request, "disks": disks, "buses": buses, "status": status,
        "q": q, "sort": sort, "dup_labels": dup_labels, "locations": DISK_LOCATIONS,
        "bus_options": bus_options, "suggested": suggested,
    })


_LOW_STOCK = 2  # порог «низкого остатка» на складе по модели


@router.get("/assets", response_class=HTMLResponse)
async def assets_page(request: Request, session: AsyncSession = Depends(get_session)):
    """Склад/Активы: остаток по моделям, гарантия, поступления партиями."""
    disks = await _disks(session)
    batches = (await session.execute(
        select(AssetBatch).order_by(AssetBatch.received_at.desc()))).scalars().all()
    today = dt.date.today()

    # Свободный склад = готовые диски, НЕ закреплённые ни за каким автобусом.
    # Закреплённые «(Резерв)» за автобусами — это уже рабочий парк, не склад.
    def _is_free(d):
        return d.status == DiskStatus.READY and d.assigned_bus_id is None

    stock: dict[str, int] = {}
    for d in disks:
        if _is_free(d):
            key = f"{d.type} · {d.capacity_gb} ГБ" if d.capacity_gb else d.type
            stock[key] = stock.get(key, 0) + 1
    stock_rows = sorted(
        ({"model": k, "qty": v, "low": v < _LOW_STOCK} for k, v in stock.items()),
        key=lambda r: r["qty"],
    )

    # NVR/камеры — единицы учёта (Asset)
    assets = (await session.execute(select(Asset).order_by(Asset.kind, Asset.label))).scalars().all()
    # Остаток по NVR/камерам (на складе, по моделям)
    asset_stock: dict[str, int] = {}
    for a in assets:
        if a.status == AssetStatus.IN_STOCK:
            label = ASSET_KINDS.get(a.kind, a.kind)
            key = f"{label} · {a.model}" if a.model else label
            asset_stock[key] = asset_stock.get(key, 0) + 1
    asset_stock_rows = sorted(
        ({"model": k, "qty": v, "low": v < _LOW_STOCK} for k, v in asset_stock.items()),
        key=lambda r: r["qty"],
    )

    # Гарантия (диски + NVR/камеры): истекает ≤30 дней или истекла (не списанные)
    warranty = []
    for d in disks:
        if d.status != DiskStatus.WRITTEN_OFF and d.warranty_until:
            days = (d.warranty_until - today).days
            if days <= 30:
                warranty.append({"name": d.label, "sub": f"диск, {d.type}",
                                 "href": f"/disks/{d.id}/passport", "until": d.warranty_until, "days": days})
    warranty.sort(key=lambda x: x["days"])  # гарантия только для дисков

    summary = {
        "free": sum(1 for d in disks if _is_free(d)),
        "assigned": sum(1 for d in disks
                        if d.assigned_bus_id is not None and d.status != DiskStatus.WRITTEN_OFF),
        "faulty": sum(1 for d in disks if d.status == DiskStatus.FAULTY),
        "written_off": sum(1 for d in disks if d.status == DiskStatus.WRITTEN_OFF),
        "nvr": sum(1 for a in assets if a.kind == "nvr" and a.status != AssetStatus.WRITTEN_OFF),
        "camera": sum(1 for a in assets if a.kind == "camera" and a.status != AssetStatus.WRITTEN_OFF),
    }
    bus_list = list((await session.execute(select(Bus))).scalars())
    buses = {b.id: b.bus_number for b in bus_list}
    bus_options = sorted(((b.id, b.bus_number) for b in bus_list), key=lambda kv: _nat(kv[1]))
    return templates.TemplateResponse("assets.html", {
        "request": request, "batches": batches, "stock_rows": stock_rows,
        "asset_stock_rows": asset_stock_rows, "assets": assets, "buses": buses,
        "warranty": warranty, "summary": summary, "low_stock": _LOW_STOCK,
        "kinds": ASSET_KINDS, "asset_statuses": ASSET_STATUSES, "bus_options": bus_options,
    })


@router.get("/disks/audit", response_class=HTMLResponse)
async def disks_audit_page(request: Request, session: AsyncSession = Depends(get_session)):
    """Ревизия: пройтись по дискам и подтвердить наличие; пропавшие подсветятся."""
    disks = await _disks(session)
    buses = {b.id: b.bus_number for b in (await session.execute(select(Bus))).scalars()}
    now = utcnow()
    items = []
    for d in sorted(disks, key=lambda d: (d.status, d.label)):
        age = None
        if d.last_audit_at:
            la = d.last_audit_at.astimezone().replace(tzinfo=None) if d.last_audit_at.tzinfo else d.last_audit_at
            age = (dt.datetime.now() - la).days
        items.append({"disk": d, "bus": buses.get(d.assigned_bus_id), "audit_age": age})
    return templates.TemplateResponse("disks_audit.html", {
        "request": request, "items": items, "locations": DISK_LOCATIONS,
    })


@router.get("/disks/{disk_id}/passport", response_class=HTMLResponse)
async def disk_passport(disk_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Паспорт диска: вся жизнь — где стоял, кто снимал, что находили."""
    disk = (await session.execute(select(Disk).where(Disk.id == disk_id))).scalar_one_or_none()
    if disk is None:
        return HTMLResponse("Диск не найден", status_code=404)
    buses = {b.id: b for b in (await session.execute(select(Bus))).scalars()}
    swaps = await _swaplog(session, disk_id=disk_id)
    reviews = list((await session.execute(
        select(DiskReview).where(DiskReview.disk_id == disk_id).order_by(DiskReview.created_at.desc())
    )).scalars())
    install_count = sum(1 for s in swaps if s.installed_disk_id == disk_id)
    return templates.TemplateResponse("passport.html", {
        "request": request, "disk": disk, "buses": buses, "swaps": swaps,
        "reviews": reviews, "install_count": install_count,
        "locations": DISK_LOCATIONS, "bus": buses.get(disk.assigned_bus_id),
    })
