"""Pydantic-схемы для API."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ── Группы ────────────────────────────────────────────────────────────────────
class GroupCreate(BaseModel):
    name: str
    description: str | None = None


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None


# ── Устройства ──────────────────────────────────────────────────────────────────
class DeviceCreate(BaseModel):
    name: str
    host: str
    http_port: int = 80
    use_https: bool = False
    username: str = "admin"
    password: str = ""
    api_type: str = "auto"
    auth_scheme: str = "digest"
    group_id: int | None = None
    timeout: float = 15.0
    retries: int = 2
    enabled: bool = True
    latitude: float | None = None
    longitude: float | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    http_port: int | None = None
    use_https: bool | None = None
    username: str | None = None
    password: str | None = None  # пусто => не менять
    api_type: str | None = None
    auth_scheme: str | None = None
    group_id: int | None = None
    timeout: float | None = None
    retries: int | None = None
    enabled: bool | None = None
    latitude: float | None = None
    longitude: float | None = None


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    channel_id: int
    name: str | None = None
    kind: str
    status: str
    enabled: bool = True
    archive_depth_days: int | None = None
    quality: str | None = None
    quality_checked_at: dt.datetime | None = None
    last_status_change: dt.datetime | None = None
    last_seen: dt.datetime | None = None
    recording_mode: str = "continuous"
    recording_status: str = "unknown"
    recording_checked_at: dt.datetime | None = None
    recording_last_end: dt.datetime | None = None
    recording_age_seconds: int | None = None
    recording_error: str | None = None


class RecordingModeUpdate(BaseModel):
    recording_mode: Literal["continuous", "event", "disabled"]


class HddOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    hdd_id: str
    name: str | None = None
    capacity_mb: int
    free_mb: int
    status: str
    raw_status: str | None = None
    present: bool = True
    updated_at: dt.datetime | None = None


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    host: str
    http_port: int
    api_type: str
    group_id: int | None = None
    address: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    enabled: bool
    reachable: bool
    consecutive_failures: int
    capabilities: dict = Field(default_factory=dict)
    monitoring_checks: dict = Field(default_factory=dict)
    temperature: float | None = None
    cpu_load: float | None = None
    memory_usage: float | None = None
    last_seen: dt.datetime | None = None
    last_error: str | None = None
    time_drift_seconds: int | None = None
    latitude: float | None = None
    longitude: float | None = None


class DeviceDetail(DeviceOut):
    channels: list[ChannelOut] = Field(default_factory=list)
    hdds: list[HddOut] = Field(default_factory=list)


# ── Тест соединения / автоопределение ──────────────────────────────────────────
class TestConnectionRequest(BaseModel):
    host: str
    http_port: int = 80
    use_https: bool = False
    username: str = "admin"
    password: str = ""
    api_type: str = "auto"  # auto => автоопределение
    auth_scheme: str = "digest"
    timeout: float = 10.0


class TestConnectionResult(BaseModel):
    ok: bool
    api_type: str
    auth_scheme: str
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    capabilities: dict = Field(default_factory=dict)
    detail: str = ""


# ── События / архив ─────────────────────────────────────────────────────────────
class NoteCreate(BaseModel):
    text: str
    channel_id: int | None = None


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    channel_id: int | None = None
    text: str
    created_at: dt.datetime


class MarkerCreate(BaseModel):
    device_id: int
    channel_id: int | None = None
    label: str | None = None
    x: float = 50.0
    y: float = 50.0


class MarkerMove(BaseModel):
    x: float
    y: float


# ── Автобусы / диски ────────────────────────────────────────────────────────────
class IssueAckIn(BaseModel):
    key: str
    note: str | None = None


class BusCreate(BaseModel):
    bus_number: str
    route: str | None = None
    dvr_model: str | None = None


class BusUpdate(BaseModel):
    bus_number: str | None = None
    route: str | None = None
    dvr_model: str | None = None
    location: str | None = None
    problem_note: str | None = None
    has_problem: bool | None = None
    swap_alert_enabled: bool | None = None
    collect_weekday: int | None = None


class DiskCreate(BaseModel):
    label: str
    type: str = "SSD"
    capacity_gb: int | None = None
    assigned_bus_id: int | None = None
    status: str = "ready"
    note: str | None = None


class DiskUpdate(BaseModel):
    label: str | None = None
    type: str | None = None
    capacity_gb: int | None = None
    assigned_bus_id: int | None = None
    note: str | None = None
    location: str | None = None


class AuditRequest(BaseModel):
    present_ids: list[int] = []


class SwapRequest(BaseModel):
    installed_disk_id: int | None = None  # какой ставим (None = снять без замены)
    note: str | None = None
    force: bool = False                   # подтвердить установку чужого диска


class FaultyRequest(BaseModel):
    note: str | None = None


class NotCollectedRequest(BaseModel):
    reason: str


class BatchCreate(BaseModel):
    """Поступление партии. Для дисков сразу создаёт qty единиц на складе."""
    kind: str = "disk"
    model: str
    vendor: str | None = None
    supplier: str | None = None
    qty: int = 1
    unit_cost: float | None = None
    warranty_until: dt.date | None = None
    note: str | None = None
    # параметры дисков (kind=disk):
    disk_type: str = "SSD"
    capacity_gb: int | None = None
    label_prefix: str | None = None       # префикс меток; пусто → авто по дате
    assigned_bus_id: int | None = None     # сразу закрепить за автобусом (опц.)


class WriteOffRequest(BaseModel):
    reason: str | None = None


class ReplaceFaultyRequest(BaseModel):
    reason: str | None = None
    new_disk_id: int | None = None         # чем заменить; пусто → готовый резерв автобуса


# ── Активы (NVR/камеры) ───────────────────────────────────────────────────────
class AssetCreate(BaseModel):
    kind: str = "nvr"                      # nvr | camera
    label: str
    model: str | None = None
    serial: str | None = None
    vendor: str | None = None
    assigned_bus_id: int | None = None     # закрепить за автобусом
    status: str = "in_stock"
    warranty_until: dt.date | None = None
    note: str | None = None


class AssetUpdate(BaseModel):
    label: str | None = None
    model: str | None = None
    serial: str | None = None
    vendor: str | None = None
    assigned_bus_id: int | None = None
    warranty_until: dt.date | None = None
    note: str | None = None


class AssetAction(BaseModel):
    reason: str | None = None
    bus_id: int | None = None              # для установки (deploy) — в какой автобус


class AssetReplace(BaseModel):
    new_id: int                            # чем заменить (другой актив со склада)
    reason: str | None = None


class ReviewRequest(BaseModel):
    tags: list[str] = []
    note: str | None = None
    finish: bool = False  # пометить диск готовым (в резерв) после записи наблюдения


class BusSettings(BaseModel):
    swap_days: int = 14
    review_days: int = 7


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    permissions: list[str] = []
    is_owner: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = None
    permissions: list[str] | None = None
    enabled: bool | None = None
    is_owner: bool | None = None
    password: str | None = None   # непусто = сменить пароль


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int | None = None
    channel_id: int | None = None
    type: str
    severity: str
    message: str
    created_at: dt.datetime


class ArchiveCoverageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    channel_id: int
    day: dt.date
    status: str
    recorded_minutes: int
    largest_gap_minutes: int
    gaps: list = Field(default_factory=list)


# ── Модуль «Заселения» ────────────────────────────────────────────────────────
class CheckinHotelIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True


class CheckinRecorderIn(BaseModel):
    hotel_id: int
    name: str = ""
    host: str = Field(min_length=1, max_length=255)
    http_port: int = Field(default=80, ge=1, le=65535)
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    username: str = "admin"
    password: str = ""            # пусто при update = не менять
    model_type: str = "ds7616ni_e2"
    analytics_capable: bool | None = None   # None = авто по типу
    night_start: str = "07:00"
    night_end: str = "24:00"
    enabled: bool = True


class RecorderImportIn(BaseModel):
    hotel_id: int
    device_id: int
    name: str = ""
    model_type: str | None = None   # None = автоопределение по модели устройства
    night_start: str = "07:00"
    night_end: str = "24:00"


class OneCConnectionIn(BaseModel):
    """Сохранение подключения 1С:Отель для гостиницы (пароль пусто = не менять)."""
    hotel_id: int
    enabled: bool = False
    base_url: str = ""
    service_url: str | None = None
    username: str = ""
    password: str = ""
    onec_tz: str = "Asia/Khabarovsk"
    entity: str = "Document_Accommodation"
    field_ref: str = "Ref_Key"
    field_date: str = "CheckInDate"
    field_date_fallback: str = "Date"
    field_query_date: str = "Date"
    field_guest: str = "GuestFullName"
    filter_posted: bool = True
    expand_room: str = "Room"
    field_room_ref: str = "Room_Key"
    field_room_number: str = "Description"
    field_room_floor: str = "Floor"
    property_field: str | None = None
    property_value: str | None = None
    lookback_days: int = Field(default=5, ge=1, le=90)
    mask_guest: bool = True


class OneCSyncIn(BaseModel):
    hotel_id: int | None = None   # None = синхронизировать все включённые
    full: bool = False


class OneCTestIn(BaseModel):
    hotel_id: int


class CheckinChannelIn(BaseModel):
    recorder_id: int
    channel_id: int = Field(ge=0, le=100000)
    name: str | None = None
    role: str = "entrance"
    substream_trackid: int = 0
    enabled: bool = True


class CheckinScheduleIn(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class RecorderTestIn(BaseModel):
    """Тест подключения по id (пароль из БД) или по явным реквизитам (до сохранения)."""
    recorder_id: int | None = None
    host: str | None = None
    http_port: int = 80
    username: str = "admin"
    password: str = ""


class IngestRunIn(BaseModel):
    day: dt.date | None = None
    hotel_id: int | None = None


class NotificationStatusIn(BaseModel):
    status: str


class CheckinLogIn(BaseModel):
    """Вердикт оператора по заселению (Фаза 2)."""
    clip_id: int | None = None
    hotel_id: int | None = None      # если clip_id не задан
    day: dt.date | None = None
    shift: str | None = None
    room: str | None = None
    event_time: dt.datetime | None = None
    verdict: str = "checkin"
    note: str | None = None


class TestClipIn(BaseModel):
    minutes: int = Field(default=10, ge=1, le=60)
    hotel_id: int | None = None


class StorageIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
