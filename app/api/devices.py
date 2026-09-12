"""REST API: устройства, группы, тест соединения, автоопределение, опрос."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, schemas
from app.database import get_session
from app.drivers import build_client, detect_api_type
from app.drivers.base import NVRError
from app.models import ApiType, Channel, Device, Note
from app.services import archive, audit, bulkops, poller, quality, recording

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["devices"])


# ── Группы ────────────────────────────────────────────────────────────────────
@router.get("/groups", response_model=list[schemas.GroupOut])
async def get_groups(session: AsyncSession = Depends(get_session)):
    return await crud.list_groups(session)


@router.post("/groups", response_model=schemas.GroupOut)
async def post_group(data: schemas.GroupCreate, session: AsyncSession = Depends(get_session)):
    return await crud.create_group(session, data)


# ── Устройства ──────────────────────────────────────────────────────────────────
@router.get("/devices", response_model=list[schemas.DeviceOut])
async def get_devices(session: AsyncSession = Depends(get_session)):
    return await crud.list_devices(session)


@router.get("/devices/{device_id}", response_model=schemas.DeviceDetail)
async def get_device(device_id: int, session: AsyncSession = Depends(get_session)):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    return device


@router.post("/devices", response_model=schemas.DeviceDetail)
async def post_device(data: schemas.DeviceCreate, session: AsyncSession = Depends(get_session)):
    # Автоопределение типа при необходимости + capability-check
    if data.api_type in (ApiType.AUTO, "", None):
        result = await detect_api_type(
            data.host, data.http_port, data.username, data.password,
            use_https=data.use_https, timeout=data.timeout,
        )
        data.api_type = result.api_type
        data.auth_scheme = result.auth_scheme

    device = await crud.create_device(session, data)
    await _enrich_device(session, device)
    await session.refresh(device)
    return await crud.get_device(session, device.id)


@router.put("/devices/{device_id}", response_model=schemas.DeviceDetail)
async def put_device(
    device_id: int, data: schemas.DeviceUpdate, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await crud.update_device(session, device, data)
    return await crud.get_device(session, device_id)


@router.delete("/devices/{device_id}")
async def remove_device(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    name = device.name
    await crud.delete_device(session, device)
    await audit.log_action(session, request, "delete_device", target=name)
    return {"ok": True}


# ── Тест соединения / автоопределение ──────────────────────────────────────────
@router.post("/devices/test", response_model=schemas.TestConnectionResult)
async def test_connection(data: schemas.TestConnectionRequest):
    api_type = data.api_type
    auth_scheme = data.auth_scheme
    detail = ""

    if api_type in (ApiType.AUTO, "", None):
        det = await detect_api_type(
            data.host, data.http_port, data.username, data.password,
            use_https=data.use_https, timeout=data.timeout,
        )
        api_type, auth_scheme, detail = det.api_type, det.auth_scheme, det.detail
        if api_type == ApiType.UNKNOWN:
            return schemas.TestConnectionResult(
                ok=False, api_type=api_type, auth_scheme=auth_scheme, detail=detail
            )

    # Временное устройство (без сохранения) для теста + probe
    tmp = Device(
        name="test", host=data.host, http_port=data.http_port, use_https=data.use_https,
        username=data.username, api_type=api_type, auth_scheme=auth_scheme,
        timeout=data.timeout, retries=1,
    )
    client = build_client(tmp, password=data.password)
    try:
        info = await client.test_connection()
        caps = await client.probe_capabilities()
        return schemas.TestConnectionResult(
            ok=True, api_type=api_type, auth_scheme=auth_scheme,
            model=info.model, firmware=info.firmware, serial=info.serial,
            capabilities=caps.as_dict(), detail=detail or "соединение успешно",
        )
    except NVRError as exc:
        return schemas.TestConnectionResult(
            ok=False, api_type=api_type, auth_scheme=auth_scheme, detail=str(exc)
        )


# ── Ручной запуск опроса / проверки архива ──────────────────────────────────────
@router.post("/devices/{device_id}/recording-check")
async def recording_check_now(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session),
):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    if not device.enabled:
        raise HTTPException(409, "Мониторинг устройства отключён")
    if not await recording.check_recording_device(session, device):
        raise HTTPException(409, "Проверка записи этого NVR уже выполняется")
    await session.commit()
    await audit.log_action(session, request, "recording_check", target=device.name)
    return {"ok": True}


@router.put("/devices/{device_id}/channels/{channel_id}/recording", response_model=schemas.ChannelOut)
async def set_recording_mode(
    device_id: int, channel_id: int, data: schemas.RecordingModeUpdate, request: Request,
    session: AsyncSession = Depends(get_session),
):
    channel = (await session.execute(select(Channel).where(
        Channel.device_id == device_id, Channel.channel_id == channel_id,
    ))).scalar_one_or_none()
    if channel is None:
        raise HTTPException(404, "Канал не найден")
    channel.recording_mode = data.recording_mode
    channel.recording_status = "disabled" if data.recording_mode == "disabled" else "unknown"
    channel.recording_checked_at = None
    channel.recording_age_seconds = None
    channel.recording_error = None
    await session.commit()
    await audit.log_action(session, request, "recording_mode",
                           target=f"устройство {device_id} канал {channel_id}",
                           detail=data.recording_mode)
    return channel


@router.post("/devices/{device_id}/poll")
async def poll_now(device_id: int, session: AsyncSession = Depends(get_session)):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await poller.poll_device(device_id)
    return {"ok": True}


@router.get("/devices/{device_id}/channels/{channel_id}/snapshot")
async def channel_snapshot(
    device_id: int, channel_id: int, session: AsyncSession = Depends(get_session)
):
    """Текущий кадр канала (JPEG)."""
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    client = build_client(device)
    try:
        data = await client.get_snapshot(channel_id)
    except NVRError as exc:
        raise HTTPException(502, f"Снимок недоступен: {exc}")
    return Response(content=data, media_type="image/jpeg")


@router.get("/devices/{device_id}/qr.png")
async def device_qr(device_id: int, request: Request):
    """QR-код со ссылкой на мобильную карточку устройства (для наклейки)."""
    import io

    import qrcode

    base = str(request.base_url).rstrip("/")
    url = f"{base}/m/{device_id}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/devices/{device_id}/raw")
async def raw_request(
    device_id: int, path: str, session: AsyncSession = Depends(get_session)
):
    """Диагностика: выполняет GET к произвольному эндпоинту NVR и отдаёт сырой ответ.

    Использует сохранённые учётные данные устройства. Только для отладки.
    """
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    if not path.startswith("/"):
        raise HTTPException(400, "path должен начинаться с /")
    client = build_client(device)
    try:
        resp = await client._request("GET", path)
    except NVRError as exc:
        raise HTTPException(502, f"Ошибка запроса: {exc}")
    try:
        text = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.content.decode("cp1251", errors="replace")
    return Response(content=text, media_type="text/plain; charset=utf-8")


@router.post("/devices/{device_id}/sync-time")
async def sync_time(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    client = build_client(device)
    try:
        await client.sync_time()
    except NVRError as exc:
        raise HTTPException(502, f"Не удалось синхронизировать время: {exc}")
    await audit.log_action(session, request, "sync_time", target=device.name)
    # сразу пересчитаем дрейф
    await poller.poll_device(device_id)
    return {"ok": True}


@router.post("/devices/{device_id}/reboot")
async def reboot_device(
    device_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    client = build_client(device)
    try:
        await client.reboot()
    except NVRError as exc:
        raise HTTPException(502, f"Не удалось перезагрузить: {exc}")
    await audit.log_action(session, request, "reboot", target=device.name)
    return {"ok": True}


@router.post("/devices/{device_id}/archive-depth")
async def archive_depth_now(device_id: int, session: AsyncSession = Depends(get_session)):
    """Измерить реальную глубину архива (сколько дней хранится)."""
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await archive.measure_device_depth(device_id)
    return {"ok": True}


@router.post("/devices/{device_id}/quality-check")
async def quality_check_now(device_id: int, session: AsyncSession = Depends(get_session)):
    raise HTTPException(410, "Проверка качества картинки отключена")


@router.post("/devices/{device_id}/channels/{channel_id}/toggle")
async def toggle_channel(
    device_id: int, channel_id: int, request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Включить/выключить мониторинг канала (заглушка)."""
    ch = (
        await session.execute(
            select(Channel).where(
                Channel.device_id == device_id, Channel.channel_id == channel_id
            )
        )
    ).scalar_one_or_none()
    if ch is None:
        raise HTTPException(404, "Канал не найден")
    ch.enabled = not ch.enabled
    await session.commit()
    await audit.log_action(
        session, request, "toggle_channel",
        target=f"устройство {device_id} канал {channel_id}",
        detail="включён" if ch.enabled else "заглушён",
    )
    return {"ok": True, "enabled": ch.enabled}


# ── Журнал обслуживания (заметки) ──────────────────────────────────────────────
@router.get("/devices/{device_id}/notes", response_model=list[schemas.NoteOut])
async def list_notes(device_id: int, session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Note).where(Note.device_id == device_id).order_by(Note.created_at.desc())
        )
    ).scalars().all()
    return rows


@router.post("/devices/{device_id}/notes", response_model=schemas.NoteOut)
async def add_note(
    device_id: int, data: schemas.NoteCreate, session: AsyncSession = Depends(get_session)
):
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    note = Note(device_id=device_id, channel_id=data.channel_id, text=data.text)
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return note


@router.delete("/notes/{note_id}")
async def delete_note(note_id: int, session: AsyncSession = Depends(get_session)):
    note = (
        await session.execute(select(Note).where(Note.id == note_id))
    ).scalar_one_or_none()
    if note is None:
        raise HTTPException(404, "Заметка не найдена")
    await session.delete(note)
    await session.commit()
    return {"ok": True}


# ── Массовые операции (по группе или по всем) ──────────────────────────────────
async def _bulk_targets(session: AsyncSession, group_id: int | None) -> list[int]:
    query = select(Device.id).where(Device.enabled.is_(True))
    if group_id is not None:
        query = query.where(Device.group_id == group_id)
    return list((await session.execute(query)).scalars())


@router.post("/bulk/poll")
async def bulk_poll(
    request: Request, group_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    ids = await _bulk_targets(session, group_id)
    for did in ids:
        await poller.poll_device(did)
    await audit.log_action(session, request, "bulk_poll", detail=f"{len(ids)} устройств")
    return {"ok": True, "count": len(ids)}


@router.post("/bulk/sync-time")
async def bulk_sync_time(
    request: Request, group_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    ids = await _bulk_targets(session, group_id)
    done = 0
    for did in ids:
        device = await crud.get_device(session, did)
        try:
            await build_client(device).sync_time()
            done += 1
        except NVRError:
            pass
    await audit.log_action(session, request, "bulk_sync_time", detail=f"{done}/{len(ids)}")
    return {"ok": True, "synced": done, "total": len(ids)}


@router.post("/bulk/run")
async def bulk_run(
    action: str, request: Request, group_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Запуск массовой операции в фоне (poll/sync_time/archive_check/quality_check/depth)."""
    try:
        started = bulkops.start(action, group_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not started:
        raise HTTPException(409, "Массовая операция уже выполняется")
    await audit.log_action(session, request, "bulk_run", detail=action)
    return {"started": True}


@router.get("/bulk/status")
async def bulk_status():
    """Прогресс текущей/последней массовой операции."""
    return bulkops.status()


@router.get("/audit")
async def audit_log(limit: int = 200, session: AsyncSession = Depends(get_session)):
    from app.models import AuditLog

    rows = (
        await session.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [
        {"user": r.user, "action": r.action, "target": r.target,
         "detail": r.detail, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.post("/devices/{device_id}/recheck", response_model=schemas.DeviceDetail)
async def recheck(device_id: int, session: AsyncSession = Depends(get_session)):
    """Заново тянет модель/прошивку и перепроверяет возможности устройства."""
    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    await _enrich_device(session, device)
    return await crud.get_device(session, device_id)


@router.post("/devices/{device_id}/archive-check")
async def archive_check_now(
    device_id: int, day: str | None = None, session: AsyncSession = Depends(get_session)
):
    import datetime as dt

    device = await crud.get_device(session, device_id)
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    target = (
        dt.date.fromisoformat(day) if day else dt.date.today() - dt.timedelta(days=1)
    )
    await archive.check_device_archive(device_id, target)
    return {"ok": True, "day": target.isoformat()}


# ── Вспомогательное: обогащение устройства инфо + capabilities ──────────────────
async def _enrich_device(session: AsyncSession, device: Device) -> None:
    """После создания: тянем модель/прошивку и проверяем возможности."""
    if device.api_type == ApiType.UNKNOWN:
        return
    client = build_client(device)
    try:
        info = await client.test_connection()
        device.model = info.model
        device.firmware = info.firmware
        device.serial = info.serial
    except NVRError as exc:
        device.last_error = str(exc)
        log.warning("Не удалось получить инфо устройства %s: %s", device.id, exc)
    try:
        caps = await client.probe_capabilities()
        device.capabilities = caps.as_dict()
    except NVRError as exc:
        log.warning("Capability-check %s не выполнен: %s", device.id, exc)
    await session.commit()
