"""Time source and daily schedule settings for monitoring users."""
import json

from fastapi import APIRouter, Request

from app.database import SessionLocal
from app.models import AppSetting
from app.services import audit, timesync

router = APIRouter(prefix="/api/monitoring/time-sync", tags=["time-sync"])


@router.get("")
async def get_settings():
    from app.scheduler import scheduler
    config = await timesync.load_settings()
    async with SessionLocal() as session:
        row = await session.get(AppSetting, "time_sync_last_run")
        last_run = json.loads(row.value) if row else None
    job = scheduler.get_job("time_sync")
    next_run = getattr(job, "next_run_time", None)
    return {"settings": config.model_dump(), "last_run": last_run,
            "next_run": next_run.isoformat() if next_run else None}


@router.put("")
async def put_settings(data: timesync.TimeSyncSettings, request: Request):
    from app.scheduler import configure_time_sync_job
    await timesync.save_json("time_sync_settings", data.model_dump())
    await configure_time_sync_job()
    async with SessionLocal() as session:
        await audit.log_action(session, request, "time_sync_settings", detail=data.model_dump_json())
    return await get_settings()


@router.post("/test")
async def test_server(data: timesync.TimeSyncSettings):
    from fastapi import HTTPException
    from app.drivers.base import NVRError
    if not data.server:
        raise HTTPException(422, "Укажите NTP-сервер")
    try:
        value = await timesync.reference_time(data)
    except NVRError as exc:
        raise HTTPException(502, str(exc))
    return {"time": value.isoformat()}
