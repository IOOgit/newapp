"""Карточки свитчей и проверка доступности без ложной SNMP-телеметрии."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.models import Channel, Device, Group, NetworkSwitch, SwitchPort
from app.services import audit
from app.services import switches as service
from app.switch_schemas import SwitchCreate, SwitchPortUpdate, SwitchUpdate
from app.templatefilters import register


class PrivateValidationRoute(APIRoute):
    """Стандартный 422, но без копии входного JSON с community."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                switch_id = request.path_params.get("switch_id")
                if switch_id is not None and request.method in ("PUT", "PATCH", "DELETE"):
                    try:
                        claim_id = int(switch_id)
                    except ValueError:
                        return await handler(request)
                    with service.claim_switch(claim_id):
                        return await handler(request)
                return await handler(request)
            except service.SwitchBusy:
                return JSONResponse(status_code=409, content={"detail": "Свитч уже опрашивается или изменяется"})
            except RequestValidationError as exc:
                return JSONResponse(status_code=422, content={"detail": [
                    {key: item[key] for key in ("loc", "msg", "type") if key in item}
                    for item in exc.errors()
                ]})
        return safe_handler


router = APIRouter(tags=["switches"], route_class=PrivateValidationRoute)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
register(templates)


def switch_payload(switch: NetworkSwitch) -> dict:
    """Только явно перечисленные публичные поля, никогда ORM/__dict__."""
    fields = ("id", "name", "host", "model", "group_id",
              "timeout", "enabled", "reachable", "last_attempt_at", "last_seen",
              "last_error", "sys_name", "sys_descr", "sys_object_id", "uptime_ticks",
              "capabilities", "poe_ports", "poe_supplies")
    result = {field: getattr(switch, field) for field in fields}
    result["management_port"] = switch.snmp_port  # имя столбца БД оставлено для совместимости
    result["monitoring_method"] = "tcp"
    result["telemetry_available"] = False
    result["health"] = service.switch_health(switch)
    result["polling"] = service.is_polling(switch.id)
    port_fields = ("id", "if_index", "name", "description", "alias", "admin_status",
                   "oper_status", "speed_mbps", "last_change_ticks", "observed_at", "present",
                   "counters", "in_bps", "out_bps", "in_error_delta", "out_error_delta",
                   "in_discard_delta", "out_discard_delta", "expected_up", "channel_ref_id", "poe_index")
    result["ports"] = [{**{field: getattr(port, field) for field in port_fields},
                        "health": service.port_health(port, switch)}
                       for port in sorted(switch.ports, key=lambda p: p.if_index)]
    return result


async def _get(session: AsyncSession, switch_id: int) -> NetworkSwitch:
    switch = (await session.execute(select(NetworkSwitch).options(
        selectinload(NetworkSwitch.ports)).where(NetworkSwitch.id == switch_id)
    )).scalar_one_or_none()
    if switch is None:
        raise HTTPException(404, "Свитч не найден")
    return switch


def _idle(switch_id: int) -> None:
    if service.is_polling(switch_id):
        raise HTTPException(409, "Свитч уже опрашивается; дождитесь завершения")


async def _group(session: AsyncSession, group_id: int | None) -> None:
    if group_id is not None and await session.get(Group, group_id) is None:
        raise HTTPException(422, "Объект не найден")


async def _list(session: AsyncSession, group_id=None):
    query = select(NetworkSwitch).options(selectinload(NetworkSwitch.ports)).order_by(NetworkSwitch.name)
    if group_id is not None:
        query = query.where(NetworkSwitch.group_id == group_id)
    return [switch_payload(switch) for switch in (await session.execute(query)).scalars()]


@router.get("/switches", response_class=HTMLResponse)
async def switches_page(request: Request, group_id: int | None = None,
                        session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse("switches.html", {
        "request": request, "switches": await _list(session, group_id),
        "groups": list((await session.execute(select(Group).order_by(Group.name))).scalars()),
        "group_id": group_id,
    })


@router.get("/switches/{switch_id}", response_class=HTMLResponse)
async def switch_page(switch_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    channels = (await session.execute(select(Channel, Device).join(Device, Channel.device_id == Device.id)
                                     .order_by(Device.name, Channel.channel_id))).all()
    return templates.TemplateResponse("switch.html", {
        "request": request, "switch": switch_payload(await _get(session, switch_id)),
        "groups": list((await session.execute(select(Group).order_by(Group.name))).scalars()),
        "channels": [{"id": channel.id, "device_id": device.id, "device_name": device.name,
                      "channel_index": channel.channel_id, "name": channel.name}
                     for channel, device in channels],
    })


@router.get("/api/switches")
async def list_switches(group_id: int | None = None, session: AsyncSession = Depends(get_session)):
    return await _list(session, group_id)


@router.get("/api/switches/{switch_id}")
async def get_switch(switch_id: int, session: AsyncSession = Depends(get_session)):
    return switch_payload(await _get(session, switch_id))


@router.post("/api/switches", status_code=201)
async def create_switch(data: SwitchCreate, request: Request, session: AsyncSession = Depends(get_session)):
    await _group(session, data.group_id)
    values = data.model_dump(exclude={"management_port"})
    switch = NetworkSwitch(**values, snmp_port=data.management_port,
                           snmp_version="none", community_enc="", retries=0, ports=[])
    session.add(switch)
    await session.commit()
    await audit.log_action(session, request, "create_switch", target=switch.name)
    return switch_payload(await _get(session, switch.id))


@router.put("/api/switches/{switch_id}")
async def update_switch(switch_id: int, data: SwitchUpdate, request: Request,
                        session: AsyncSession = Depends(get_session)):
    switch = await _get(session, switch_id)
    changes = data.model_dump(exclude_unset=True)
    if "management_port" in changes:
        changes["snmp_port"] = changes.pop("management_port")
    if "group_id" in changes:
        await _group(session, data.group_id)
        if data.group_id is not None:
            mapped = [p.channel_ref_id for p in switch.ports if p.channel_ref_id is not None]
            if mapped:
                groups = (await session.execute(select(Device.group_id).join(Channel, Channel.device_id == Device.id)
                                                .where(Channel.id.in_(mapped)))).scalars().all()
                if any(group != data.group_id for group in groups):
                    raise HTTPException(422, "Назначенные камеры относятся к другому объекту")
    # null допустим только для снятия группы; остальные обязательные настройки не обнуляем.
    for key, value in changes.items():
        if value is None and key != "group_id":
            raise HTTPException(422, "Обязательные настройки не могут быть null")
        setattr(switch, key, value)
    if any(key in changes for key in ("host", "snmp_port")):
        switch.reachable = False
        switch.last_error = "Настройки изменены; требуется новый опрос"
        for port in switch.ports:
            service._clear_rates(port)
    await session.commit()
    await audit.log_action(session, request, "update_switch", target=switch.name)
    return switch_payload(switch)


@router.delete("/api/switches/{switch_id}")
async def delete_switch(switch_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    switch = await _get(session, switch_id)
    name = switch.name
    await session.delete(switch)
    await session.commit()
    await audit.log_action(session, request, "delete_switch", target=name)
    return {"ok": True}


@router.post("/api/switches/{switch_id}/poll")
async def poll_switch_now(switch_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    _idle(switch_id)
    switch = await _get(session, switch_id)
    if not switch.enabled:
        raise HTTPException(409, "Опрос свитча выключен")
    ok = await service.poll_switch(session, switch)
    await audit.log_action(session, request, "poll_switch", target=switch.name,
                           detail="успешно" if ok else "нет подтверждённых данных")
    return {"ok": ok, "switch": switch_payload(switch)}


@router.patch("/api/switches/{switch_id}/ports/{port_id}")
async def update_port(switch_id: int, port_id: int, data: SwitchPortUpdate, request: Request,
                      session: AsyncSession = Depends(get_session)):
    switch = await _get(session, switch_id)
    port = next((p for p in switch.ports if p.id == port_id), None)
    if port is None:
        raise HTTPException(404, "Порт не найден")
    changes = data.model_dump(exclude_unset=True)
    if data.channel_ref_id is not None:
        row = (await session.execute(select(Channel, Device).join(Device, Channel.device_id == Device.id)
                                     .where(Channel.id == data.channel_ref_id))).first()
        if row is None:
            raise HTTPException(422, "Камера не найдена")
        if switch.group_id is not None and row[1].group_id != switch.group_id:
            raise HTTPException(422, "Камера принадлежит другому объекту")
        other = (await session.execute(select(SwitchPort.id).where(
            SwitchPort.channel_ref_id == data.channel_ref_id, SwitchPort.id != port.id
        ))).scalar_one_or_none()
        if other is not None:
            raise HTTPException(422, "Камера уже назначена другому порту")
        changes["expected_up"] = True
    if data.poe_index is not None:
        if data.poe_index not in (switch.poe_ports or {}):
            raise HTTPException(422, "Телеметрия PoE для этой модели недоступна")
        if any(p.id != port.id and p.poe_index == data.poe_index for p in switch.ports):
            raise HTTPException(422, "PoE-порт уже назначен другому интерфейсу")
    if "expected_up" in changes and changes["expected_up"] is None:
        raise HTTPException(422, "Признак ожидаемого порта не может быть null")
    for key, value in changes.items():
        setattr(port, key, value)
    port.counters = {**(port.counters or {}), "mapping_name": port.name}
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Камера уже назначена другому порту; обновите страницу") from None
    await audit.log_action(session, request, "map_switch_port", target=f"{switch.name}, ifIndex {port.if_index}")
    return switch_payload(switch)
