"""Веб-дашборд (server-side рендеринг через Jinja2)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.api.monitoring import archive_calendar, archive_overview, summary
from app.database import get_session
from app.services.health import evaluate_device, get_health_map
from app.models import ArchiveCoverage, AuditLog, ChannelState, Group, PlanMarker

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
from app.templatefilters import register as _register_filters  # noqa: E402

_register_filters(templates)

router = APIRouter(tags=["dashboard"])


def _device_health(device) -> str:
    """Совместимость шаблонов: единая оценка состояния NVR."""
    return evaluate_device(device)["color"]


templates.env.globals["device_health"] = _device_health


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    devices = await crud.list_devices(session)
    groups = {g.id: g for g in (await session.execute(select(Group))).scalars()}
    stats = await summary(session)
    health_by_id = await get_health_map(session, devices)
    events = await crud.list_events(session, limit=20)
    archive = await archive_overview(session)
    arch_by_id = {a["device_id"]: a for a in archive["devices"]}

    # события за 24 ч по часам — для спарклайна на дашборде
    since24 = dt.datetime.utcnow() - dt.timedelta(hours=24)
    recent = await crud.list_events(session, limit=2000)
    buckets = [0] * 24
    for e in recent:
        t = e.created_at.replace(tzinfo=None)
        if t >= since24 and not e.type.endswith("_resolved"):
            h = int((t - since24).total_seconds() // 3600)
            if 0 <= h < 24:
                buckets[h] += 1

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "devices": devices,
            "groups": groups,
            "stats": stats,
            "health_by_id": health_by_id,
            "events": events,
            "archive": archive,
            "arch_by_id": arch_by_id,
            "events_24h": buckets,
            "events_24h_total": sum(buckets),
        },
    )


_STATUS_COLOR = {"online": "green", "offline": "red", "no_video": "yellow", "unknown": "gray"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(session: AsyncSession = Depends(get_session)):
    from app.services.metrics import render_metrics

    return PlainTextResponse(await render_metrics(session), media_type="text/plain; version=0.0.4")


@router.get("/tv", response_class=HTMLResponse)
async def tv_mode(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse("tv.html", {"request": request})


@router.get("/slideshow", response_class=HTMLResponse)
async def slideshow(request: Request, session: AsyncSession = Depends(get_session)):
    devices = await crud.list_devices(session)
    cams = []
    for d in devices:
        if not d.enabled:
            continue
        for c in sorted(d.channels, key=lambda c: c.channel_id):
            if c.enabled is False:
                continue
            cams.append({
                "device_id": d.id, "channel_id": c.channel_id,
                "name": c.name or f"канал {c.channel_id}",
                "device": d.name, "status": c.status,
            })
    return templates.TemplateResponse("slideshow.html", {"request": request, "cams": cams})


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, session: AsyncSession = Depends(get_session)):
    days = 30
    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    events = await crud.list_events(session, limit=5000)
    period = [
        e for e in events
        if e.created_at.replace(tzinfo=None) >= since and not e.type.endswith("_resolved")
    ]
    # инциденты по дням
    counts: dict[str, int] = {}
    for e in period:
        key = e.created_at.replace(tzinfo=None).date().isoformat()
        counts[key] = counts.get(key, 0) + 1
    day_seq = [(since.date() + dt.timedelta(days=i)) for i in range(days + 1)]
    by_day = [(d.strftime("%d.%m"), counts.get(d.isoformat(), 0)) for d in day_seq]
    max_day = max([c for _, c in by_day] + [1])

    # топ проблемных камер
    devices = await crud.list_devices(session)
    dname = {d.id: d.name for d in devices}
    cname = {(d.id, c.channel_id): c.name for d in devices for c in d.channels}
    cam: dict[tuple, int] = {}
    for e in period:
        if e.device_id and e.channel_id is not None and e.type in (
            "camera_down", "bad_image", "camera_removed"
        ):
            k = (e.device_id, e.channel_id)
            cam[k] = cam.get(k, 0) + 1
    top = sorted(cam.items(), key=lambda kv: -kv[1])[:15]
    top_rows = [
        {"device": dname.get(did, did), "channel": cid,
         "name": cname.get((did, cid), ""), "count": n}
        for (did, cid), n in top
    ]

    # KPI: потеряно часов записи за период (из суточного покрытия архива)
    cov = (
        await session.execute(
            select(ArchiveCoverage).where(ArchiveCoverage.day >= since.date())
        )
    ).scalars().all()
    lost: dict[tuple, int] = {}
    for r in cov:
        lost_min = max(1440 - r.recorded_minutes, 0)
        k = (r.device_id, r.channel_id)
        lost[k] = lost.get(k, 0) + lost_min
    lost_rows = sorted(
        [{"device": dname.get(d, d), "channel": c, "name": cname.get((d, c), ""),
          "hours": round(m / 60, 1)} for (d, c), m in lost.items() if m > 0],
        key=lambda r: -r["hours"],
    )[:20]

    # Heatmap аптайма: камера × день, цвет по числу инцидентов
    hm_days = [(since.date() + dt.timedelta(days=i)) for i in range(days + 1)]
    hm_day_iso = [d.isoformat() for d in hm_days]
    hm: dict[tuple, dict[str, int]] = {}
    for e in period:
        if e.device_id and e.channel_id is not None and e.type in (
            "camera_down", "bad_image", "camera_removed"
        ):
            k = (e.device_id, e.channel_id)
            day = e.created_at.replace(tzinfo=None).date().isoformat()
            hm.setdefault(k, {})[day] = hm.get(k, {}).get(day, 0) + 1
    heatmap = [
        {"device": dname.get(d, d), "channel": c, "name": cname.get((d, c), ""),
         "days": [hm[(d, c)].get(di, 0) for di in hm_day_iso]}
        for (d, c) in sorted(hm.keys())
    ]

    return templates.TemplateResponse(
        "history.html",
        {"request": request, "by_day": by_day, "max_day": max_day,
         "top_rows": top_rows, "events": period[:60], "days": days,
         "lost_rows": lost_rows, "heatmap": heatmap, "hm_days": hm_days},
    )


@router.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request, session: AsyncSession = Depends(get_session)):
    import glob

    devices = await crud.list_devices(session)
    markers = (await session.execute(select(PlanMarker))).scalars().all()
    # карты статусов каналов и здоровья устройств
    ch_status = {
        (d.id, c.channel_id): c.status for d in devices for c in d.channels
    }
    health_map = await get_health_map(session, devices)
    health = {d.id: health_map[d.id]["color"] for d in devices}
    marker_rows = []
    for m in markers:
        if m.channel_id is not None:
            color = _STATUS_COLOR.get(ch_status.get((m.device_id, m.channel_id), "unknown"), "gray")
        else:
            color = health.get(m.device_id, "gray")
        marker_rows.append({
            "id": m.id, "device_id": m.device_id, "channel_id": m.channel_id,
            "label": m.label or "", "x": m.x, "y": m.y, "color": color,
        })
    has_image = bool(glob.glob("data/plan.*"))
    devices_json = [
        {"id": d.id, "name": d.name,
         "channels": [{"channel_id": c.channel_id, "name": c.name or ""}
                      for c in sorted(d.channels, key=lambda c: c.channel_id)]}
        for d in devices
    ]
    return templates.TemplateResponse(
        "plan.html",
        {"request": request, "devices_json": devices_json,
         "markers": marker_rows, "has_image": has_image},
    )


@router.get("/firmware", response_class=HTMLResponse)
async def firmware_page(request: Request, session: AsyncSession = Depends(get_session)):
    devices = await crud.list_devices(session)
    models: dict[str, dict] = {}
    for d in devices:
        model = d.model or "неизвестно"
        m = models.setdefault(model, {"devices": [], "firmwares": set()})
        m["devices"].append(d)
        m["firmwares"].add(d.firmware or "—")
    rows = [
        {"model": model, "devices": data["devices"],
         "mixed": len(data["firmwares"]) > 1}
        for model, data in sorted(models.items())
    ]
    return templates.TemplateResponse(
        "firmware.html", {"request": request, "rows": rows}
    )


@router.get("/calc", response_class=HTMLResponse)
async def storage_calc(request: Request):
    return templates.TemplateResponse("calc.html", {"request": request})


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(300)
        )
    ).scalars().all()
    return templates.TemplateResponse("audit.html", {"request": request, "rows": rows})


@router.get("/labels", response_class=HTMLResponse)
async def labels_page(request: Request, session: AsyncSession = Depends(get_session)):
    devices = await crud.list_devices(session)
    return templates.TemplateResponse("labels.html", {"request": request, "devices": devices})


@router.get("/m/{device_id}", response_class=HTMLResponse)
async def mobile_device(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        return HTMLResponse("Устройство не найдено", status_code=404)
    events = await crud.list_events(session, device_id=device_id, limit=15)
    channels = sorted(device.channels, key=lambda c: c.channel_id)
    return templates.TemplateResponse(
        "mobile.html",
        {"request": request, "device": device, "channels": channels, "events": events},
    )


@router.get("/devices/{device_id}/wall", response_class=HTMLResponse)
async def camera_wall(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        return HTMLResponse("Устройство не найдено", status_code=404)
    channels = sorted(
        [c for c in device.channels if c.enabled], key=lambda c: c.channel_id
    )
    return templates.TemplateResponse(
        "wall.html", {"request": request, "device": device, "channels": channels}
    )


@router.get("/devices/{device_id}/report", response_class=HTMLResponse)
async def device_report(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        return HTMLResponse("Устройство не найдено", status_code=404)
    since = dt.datetime.utcnow() - dt.timedelta(days=30)
    events = await crud.list_events(session, device_id=device_id, limit=1000)
    period_events = [e for e in events if e.created_at.replace(tzinfo=None) >= since]
    # Сводка инцидентов по типам за период
    incidents: dict[str, int] = {}
    for e in period_events:
        if not e.type.endswith("_resolved"):
            incidents[e.type] = incidents.get(e.type, 0) + 1
    return templates.TemplateResponse(
        "report.html",
        {
            "request": request,
            "device": device,
            "incidents": incidents,
            "period_from": since.date(),
            "period_to": dt.date.today(),
            "now": dt.datetime.now(),
            "channels": sorted(device.channels, key=lambda c: c.channel_id),
        },
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
async def device_page(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        return HTMLResponse("Устройство не найдено", status_code=404)
    events = await crud.list_events(session, device_id=device_id, limit=50)
    calendar = await archive_calendar(device_id, days=14, session=session)
    health = (await get_health_map(session, [device]))[device.id]
    return templates.TemplateResponse(
        "device.html",
        {
            "request": request,
            "device": device,
            "events": events,
            "calendar": calendar,
            "health": health,
            "now": dt.datetime.utcnow(),
        },
    )


@router.get("/time-settings", response_class=HTMLResponse)
async def time_settings(request: Request):
    return templates.TemplateResponse(request, "time_settings.html", {})
