"""REST API: сводка, события, календарь архива."""
from __future__ import annotations

import datetime as dt

import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import crud, schemas
from app.database import get_session
from app.models import ArchiveCoverage, Channel, ChannelState, Device

router = APIRouter(prefix="/api", tags=["monitoring"])


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_session)):
    from app.services.health import get_health_map

    devices = await crud.list_devices(session)
    health = await get_health_map(session, devices)
    enabled = [d for d in devices if d.enabled]
    return {
        "devices_total": len(devices),
        "devices_enabled": len(enabled),
        "devices_online": sum(1 for d in enabled if d.reachable),
        "devices_unreachable": sum(1 for d in enabled if not d.reachable),
        "devices_healthy": sum(1 for d in enabled if health[d.id]["color"] == "green"),
        "devices_with_problems": sum(1 for d in enabled if health[d.id]["color"] in ("yellow", "red")),
        "devices_unknown": sum(1 for d in enabled if health[d.id]["color"] == "gray"),
        "channels_down": sum(1 for d in enabled for c in d.channels
                             if c.enabled and c.status != ChannelState.ONLINE),
    }


@router.get("/monitoring/health")
async def monitoring_health(session: AsyncSession = Depends(get_session)):
    """Общая оценка для дашборда и TV; время ответа не заменяет время опроса NVR."""
    from app.services.health import get_health_map
    from app.models import utcnow

    devices = await crud.list_devices(session)
    health = await get_health_map(session, devices)
    return {
        "updated_at": utcnow().isoformat(),
        "devices": [
            {"id": d.id, "name": d.name, "enabled": d.enabled, "reachable": d.reachable,
             "channels_total": sum(1 for c in d.channels if c.enabled),
             "channels_online": sum(1 for c in d.channels if c.enabled and c.status == ChannelState.ONLINE),
             "health": health[d.id]}
            for d in devices
        ],
    }


@router.get("/events", response_model=list[schemas.EventOut])
async def events(
    device_id: int | None = None,
    severity: str | None = None,
    limit: int = Query(200, le=1000),
    session: AsyncSession = Depends(get_session),
):
    return await crud.list_events(
        session, device_id=device_id, severity=severity, limit=limit
    )


@router.get("/events/export.csv")
async def export_events_csv(
    device_id: int | None = None,
    limit: int = Query(5000, le=20000),
    session: AsyncSession = Depends(get_session),
):
    """Экспорт журнала событий в CSV (для актов и истории)."""
    events = await crud.list_events(session, device_id=device_id, limit=limit)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата/время", "Устройство", "Канал", "Тип", "Важность", "Сообщение"])
    for e in events:
        writer.writerow([
            e.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            e.device_id or "",
            e.channel_id if e.channel_id is not None else "",
            e.type, e.severity, e.message,
        ])
    buf.seek(0)
    # BOM, чтобы Excel корректно открыл кириллицу
    data = "﻿" + buf.getvalue()
    return StreamingResponse(
        iter([data]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=events.csv"},
    )


@router.get("/alerts/recent")
async def recent_alerts(after_id: int = 0, session: AsyncSession = Depends(get_session)):
    """Свежие алерты (проблемы + 'восстановлено') с id больше after_id.

    Для живых браузерных оповещений: клиент опрашивает раз в несколько секунд.
    """
    from app.models import Event

    rows = (
        await session.execute(
            select(Event)
            .where(
                Event.id > after_id,
                or_(Event.severity != "info", Event.type.like("%_resolved")),
            )
            .order_by(Event.id.desc())
            .limit(40)
        )
    ).scalars().all()
    rows = list(reversed(rows))  # по возрастанию id
    return [
        {"id": e.id, "type": e.type, "severity": e.severity, "message": e.message,
         "device_id": e.device_id, "created_at": e.created_at.isoformat()}
        for e in rows
    ]


@router.get("/devices/{device_id}/archive")
async def archive_calendar(
    device_id: int,
    days: int = Query(14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """Матрица 'канал × день' с покрытием архива за последние N дней."""
    today = dt.date.today()
    start_day = today - dt.timedelta(days=days)
    rows = (
        await session.execute(
            select(ArchiveCoverage).where(
                ArchiveCoverage.device_id == device_id,
                ArchiveCoverage.day >= start_day,
            )
        )
    ).scalars().all()

    day_list = [(start_day + dt.timedelta(days=i)).isoformat() for i in range(days + 1)]
    matrix: dict[int, dict[str, dict]] = {}
    for r in rows:
        matrix.setdefault(r.channel_id, {})[r.day.isoformat()] = {
            "status": r.status,
            "recorded_minutes": r.recorded_minutes,
            "largest_gap_minutes": r.largest_gap_minutes,
            "gaps": r.gaps,
        }
    return {"days": day_list, "channels": matrix}


async def archive_overview(session: AsyncSession, day: dt.date | None = None) -> dict:
    """Сводка покрытия архива за один день по всем устройствам (для дашборда/TV).

    Берёт самый свежий проверенный день (по умолчанию) и считает по каждому
    устройству, сколько каналов с записью / частично / без записи / без данных.
    """
    rows = (await session.execute(select(ArchiveCoverage))).scalars().all()
    if day is None:
        day = max((r.day for r in rows), default=None)
    cov = {(r.device_id, r.channel_id): r.status for r in rows if day and r.day == day}

    devices = (
        await session.execute(select(Device).options(selectinload(Device.channels)))
    ).scalars().all()

    out_devices = []
    totals = {"full": 0, "partial": 0, "none": 0, "no_data": 0}
    for d in devices:
        if not d.enabled:
            continue
        chans = [c for c in d.channels if c.enabled is not False]
        cnt = {"full": 0, "partial": 0, "none": 0, "no_data": 0}
        for c in chans:
            st = cov.get((d.id, c.channel_id))
            cnt[st if st in ("full", "partial", "none") else "no_data"] += 1
        for k in totals:
            totals[k] += cnt[k]
        covered = cnt["full"] + cnt["partial"] + cnt["none"]
        if not chans or covered == 0:
            status = "gray"
        elif cnt["none"]:
            status = "red"
        elif cnt["partial"] or cnt["no_data"]:
            status = "yellow"
        else:
            status = "green"
        out_devices.append({
            "device_id": d.id, "name": d.name, "total": len(chans),
            "full": cnt["full"], "partial": cnt["partial"],
            "none": cnt["none"], "no_data": cnt["no_data"], "status": status,
        })
    problem = sum(1 for x in out_devices if x["status"] in ("red", "yellow"))
    return {
        "day": day.isoformat() if day else None,
        "devices": out_devices,
        "totals": {**totals, "devices": len(out_devices), "problem_devices": problem},
    }


@router.get("/archive/overview")
async def archive_overview_api(session: AsyncSession = Depends(get_session)):
    return await archive_overview(session)
