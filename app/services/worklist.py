"""Рабочий список проблем («разбор полётов»).

Собирает в один список всё не-ок по всем объектам из текущего состояния:
недоступные регистраторы, offline-каналы, проблемы качества картинки, дрейф
времени, дыры/отсутствие архива за последний проверенный день, ошибки дисков.

Это НЕ уведомления — это рабочая очередь дежурного. Каждая проблема имеет
стабильный ключ ``issue_key``, по которому хранится пометка «взял в работу»
(таблица ``IssueAck``), переживающая перезагрузки страницы.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    ArchiveState,
    ChannelState,
    Device,
    HddState,
    IssueAck,
    NetworkSwitch,
)

_QUALITY_LABEL = {
    "dark": "тёмный / чёрный кадр",
    "uniform": "залеплен / однотонный",
    "blurry": "расфокус",
    "frozen": "завис",
    "error": "ошибка анализа кадра",
}

# ранги для сортировки
_SEV_RANK = {"critical": 0, "warning": 1}


async def collect_issues(session: AsyncSession) -> list[dict]:
    """Все текущие проблемы по объектам, отсортированные по важности."""
    devices = (
        await session.execute(
            select(Device)
            .where(Device.enabled.is_(True))
            .options(selectinload(Device.channels), selectinload(Device.hdds))
        )
    ).scalars().all()

    # покрытие архива за последний проверенный день
    from app.models import ArchiveCoverage

    cov_rows = (await session.execute(select(ArchiveCoverage))).scalars().all()
    latest_day = max((r.day for r in cov_rows), default=None)
    cov = {(r.device_id, r.channel_id): r for r in cov_rows if r.day == latest_day}

    acks = {a.issue_key: a for a in (await session.execute(select(IssueAck))).scalars()}
    drift_limit = settings.time_drift_alert_minutes * 60

    issues: list[dict] = []

    def add(key, device, severity, kind, title, detail, channel_id=None, since=None):
        a = acks.get(key)
        issues.append({
            "key": key,
            "device_id": device.id,
            "device_name": device.name,
            "channel_id": channel_id,
            "severity": severity,
            "kind": kind,
            "title": title,
            "detail": detail,
            "since": since.isoformat() if since else None,
            "ack": (
                {"note": a.note, "by": a.ack_by, "at": a.ack_at.isoformat()} if a else None
            ),
        })

    for d in devices:
        if not d.reachable:
            # недоступен — каналы в неизвестном состоянии, не сыпем по ним шум
            add(f"dev:{d.id}:unreachable", d, "critical", "unreachable",
                "Регистратор недоступен", d.last_error or "нет связи", since=d.last_seen)
        else:
            for c in d.channels:
                if c.enabled is False:
                    continue
                if c.status == ChannelState.OFFLINE:
                    add(f"dev:{d.id}:ch:{c.channel_id}:offline", d, "critical", "channel",
                        f"Канал {c.channel_id} offline", c.name or "—",
                        channel_id=c.channel_id, since=c.last_status_change)
                elif c.status == ChannelState.NO_VIDEO:
                    add(f"dev:{d.id}:ch:{c.channel_id}:no_video", d, "warning", "channel",
                        f"Канал {c.channel_id}: нет видео", c.name or "—",
                        channel_id=c.channel_id, since=c.last_status_change)

                if c.quality in _QUALITY_LABEL:
                    add(f"dev:{d.id}:ch:{c.channel_id}:quality", d, "warning", "quality",
                        f"Канал {c.channel_id}: {_QUALITY_LABEL[c.quality]}", c.name or "—",
                        channel_id=c.channel_id, since=c.quality_checked_at)

                r = cov.get((d.id, c.channel_id))
                if r and c.recording_mode == "continuous" and r.status == ArchiveState.NONE:
                    add(f"dev:{d.id}:ch:{c.channel_id}:archive", d, "critical", "archive",
                        f"Канал {c.channel_id}: нет архива за {latest_day.strftime('%d.%m')}",
                        "запись не ведётся", channel_id=c.channel_id)
                elif r and c.recording_mode == "continuous" and r.status == ArchiveState.PARTIAL:
                    add(f"dev:{d.id}:ch:{c.channel_id}:archive", d, "warning", "archive",
                        f"Канал {c.channel_id}: дыры в архиве за {latest_day.strftime('%d.%m')}",
                        f"макс. дыра {r.largest_gap_minutes} мин", channel_id=c.channel_id)

            if d.time_drift_seconds is not None and abs(d.time_drift_seconds) >= drift_limit:
                add(f"dev:{d.id}:drift", d, "warning", "drift",
                    "Дрейф времени регистратора", f"⏱ {d.time_drift_seconds // 60} мин")

        for h in d.hdds:
            if h.status in (HddState.ERROR, HddState.NO_DISK, HddState.UNFORMATTED,
                            HddState.READ_ONLY, HddState.MISSING):
                label = {HddState.ERROR: "ошибка диска", HddState.NO_DISK: "нет диска",
                         HddState.UNFORMATTED: "не инициализирован", HddState.READ_ONLY: "только чтение",
                         HddState.MISSING: "диск пропал"}[h.status]
                add(f"dev:{d.id}:hdd:{h.hdd_id}", d, "critical", "hdd",
                    f"Диск {h.hdd_id}: {label}", h.name or "—")

    # Новые причины здоровья дополняют прежние стабильные ключи квитирования.
    from app.services.health import get_health_map
    from app.services.switches import switch_health

    health = await get_health_map(session, devices)
    seen = {(i["device_id"], i["kind"], i["channel_id"]) for i in issues}
    for device in devices:
        for i in health[device.id]["issues"]:
            kind = {"channels": "channel", "connection": "unreachable", "time": "drift"}.get(i["kind"], i["kind"])
            identity = (device.id, kind, i.get("channel_id"))
            if identity in seen:
                continue
            seen.add(identity)
            add(f"dev:{device.id}:health:{i['kind']}:{i.get('channel_id') or 'device'}",
                device, i["severity"], kind, i["title"], i.get("detail") or "",
                channel_id=i.get("channel_id"))

    switches = (await session.execute(select(NetworkSwitch).where(NetworkSwitch.enabled.is_(True))
                .options(selectinload(NetworkSwitch.ports)))).scalars().all()
    for switch in switches:
        state = switch_health(switch)
        if state["state"] == "green":
            continue
        key = f"switch:{switch.id}:health"
        a = acks.get(key)
        issues.append({
            "key": key, "device_id": None, "device_name": switch.name,
            "target_url": f"/switches/{switch.id}", "channel_id": None,
            "severity": "critical" if state["state"] == "red" else "warning",
            "kind": "switch", "title": state["label"],
            "detail": "; ".join(state["reasons"]), "since": None,
            "ack": {"note": a.note, "by": a.ack_by, "at": a.ack_at.isoformat()} if a else None,
        })

    # взятые в работу — вниз; внутри — критичные выше, затем по объекту
    issues.sort(key=lambda x: (
        x["ack"] is not None,
        _SEV_RANK.get(x["severity"], 2),
        x["device_name"],
        x["channel_id"] if x["channel_id"] is not None else -1,
    ))
    return issues


def summarize(issues: list[dict]) -> dict:
    return {
        "total": len(issues),
        "critical": sum(1 for i in issues if i["severity"] == "critical"),
        "warning": sum(1 for i in issues if i["severity"] == "warning"),
        "taken": sum(1 for i in issues if i["ack"]),
        "open": sum(1 for i in issues if not i["ack"]),
    }
