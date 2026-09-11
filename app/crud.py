"""CRUD-операции с БД."""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import schemas
from app.crypto import encrypt
from app.models import Channel, Device, Event, Group, SwitchPort


# ── Группы ────────────────────────────────────────────────────────────────────
async def list_groups(session: AsyncSession) -> list[Group]:
    return list((await session.execute(select(Group).order_by(Group.name))).scalars())


async def create_group(session: AsyncSession, data: schemas.GroupCreate) -> Group:
    group = Group(name=data.name, description=data.description)
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return group


# ── Устройства ──────────────────────────────────────────────────────────────────
async def list_devices(session: AsyncSession) -> list[Device]:
    return list(
        (
            await session.execute(
                select(Device)
                .options(selectinload(Device.channels), selectinload(Device.hdds))
                .order_by(Device.name)
            )
        ).scalars()
    )


async def get_device(session: AsyncSession, device_id: int) -> Device | None:
    return (
        await session.execute(
            select(Device)
            .where(Device.id == device_id)
            .options(selectinload(Device.channels), selectinload(Device.hdds))
        )
    ).scalar_one_or_none()


async def create_device(session: AsyncSession, data: schemas.DeviceCreate) -> Device:
    device = Device(
        name=data.name,
        host=data.host,
        http_port=data.http_port,
        use_https=data.use_https,
        username=data.username,
        password_enc=encrypt(data.password) if data.password else "",
        api_type=data.api_type,
        auth_scheme=data.auth_scheme,
        group_id=data.group_id,
        timeout=data.timeout,
        retries=data.retries,
        enabled=data.enabled,
        latitude=data.latitude,
        longitude=data.longitude,
    )
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return device


async def update_device(
    session: AsyncSession, device: Device, data: schemas.DeviceUpdate
) -> Device:
    payload = data.model_dump(exclude_unset=True)
    password = payload.pop("password", None)
    for key, value in payload.items():
        setattr(device, key, value)
    if password:  # меняем пароль только если передан непустой
        device.password_enc = encrypt(password)
    await session.commit()
    await session.refresh(device)
    return device


async def delete_device(session: AsyncSession, device: Device) -> None:
    # SQLite может работать без FK enforcement: снимаем ссылку и при ORM-каскаде.
    await session.execute(update(SwitchPort).where(
        SwitchPort.channel_ref_id.in_(select(Channel.id).where(Channel.device_id == device.id))
    ).values(channel_ref_id=None))
    await session.delete(device)
    await session.commit()


# ── События ───────────────────────────────────────────────────────────────────
async def list_events(
    session: AsyncSession,
    *,
    device_id: int | None = None,
    severity: str | None = None,
    limit: int = 200,
) -> list[Event]:
    query = select(Event).order_by(Event.created_at.desc()).limit(limit)
    if device_id is not None:
        query = query.where(Event.device_id == device_id)
    if severity is not None:
        query = query.where(Event.severity == severity)
    return list((await session.execute(query)).scalars())
