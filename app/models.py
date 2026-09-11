"""ORM-модели: устройства, каналы, HDD, события, архив, состояние алертов."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── Типы API / статусы (строковые константы, чтобы не зависеть от Enum в БД) ──
class ApiType:
    HIKVISION = "hikvision"
    DAHUA = "dahua"
    ONVIF = "onvif"
    AUTO = "auto"
    UNKNOWN = "unknown"


class ChannelState:
    ONLINE = "online"
    OFFLINE = "offline"
    NO_VIDEO = "no_video"
    UNKNOWN = "unknown"


class HddState:
    OK = "ok"
    ERROR = "error"
    NO_DISK = "no_disk"
    UNKNOWN = "unknown"
    UNFORMATTED = "unformatted"
    READ_ONLY = "read_only"
    MISSING = "missing"


class ArchiveState:
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Severity:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Group(Base):
    """Объект / клиент для группировки регистраторов."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="group")


class Device(Base):
    """Видеорегистратор (NVR)."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), default=None)
    address: Mapped[str | None] = mapped_column(String(512), default=None)  # адрес объекта/клиента

    host: Mapped[str] = mapped_column(String(255))
    http_port: Mapped[int] = mapped_column(Integer, default=80)
    use_https: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(255), default="admin")
    password_enc: Mapped[str] = mapped_column(Text, default="")

    api_type: Mapped[str] = mapped_column(String(32), default=ApiType.AUTO)
    auth_scheme: Mapped[str] = mapped_column(String(16), default="digest")  # digest|basic

    # Идентификация (заполняется при тесте/определении)
    model: Mapped[str | None] = mapped_column(String(255), default=None)
    firmware: Mapped[str | None] = mapped_column(String(255), default=None)
    serial: Mapped[str | None] = mapped_column(String(255), default=None)

    # Сетевые параметры
    timeout: Mapped[float] = mapped_column(Float, default=15.0)
    retries: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Возможности (результат capability-check): {"channels": true, "hdd": ..., "archive": ..., "time": ...}
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    monitoring_checks: Mapped[dict] = mapped_column(JSON, default=dict)

    # Рантайм-состояние
    reachable: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    auth_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    time_drift_seconds: Mapped[int | None] = mapped_column(Integer, default=None)

    # Здоровье железа (температура/нагрузка), если NVR отдаёт
    cpu_load: Mapped[float | None] = mapped_column(Float, default=None)
    memory_usage: Mapped[float | None] = mapped_column(Float, default=None)
    temperature: Mapped[float | None] = mapped_column(Float, default=None)

    # Координаты объекта (для карты)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    group: Mapped["Group | None"] = relationship(back_populates="devices")
    channels: Mapped[list["Channel"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    hdds: Mapped[list["Hdd"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("device_id", "channel_id", name="uq_device_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(Integer)  # номер канала на NVR
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    kind: Mapped[str] = mapped_column(String(16), default="ip")  # ip|analog
    status: Mapped[str] = mapped_column(String(16), default=ChannelState.UNKNOWN)
    last_status_change: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # False = не мониторить (заглушка)
    # Реальная глубина архива по каналу (дней), измеряется суточной задачей.
    archive_depth_days: Mapped[int | None] = mapped_column(Integer, default=None)

    # Свежесть архива: конец записи — местное время NVR, проверка — UTC.
    recording_mode: Mapped[str] = mapped_column(String(16), default="continuous")
    recording_status: Mapped[str] = mapped_column(String(16), default="unknown")
    recording_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    recording_last_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False), default=None)
    recording_age_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    recording_error: Mapped[str | None] = mapped_column(Text, default=None)

    # Контроль качества картинки (компьютерное зрение по снапшоту)
    quality: Mapped[str | None] = mapped_column(String(16), default=None)  # ok|dark|uniform|blurry|frozen|error
    quality_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    frame_sig: Mapped[str | None] = mapped_column(Text, default=None)  # подпись кадра (для детекта фриза)
    frozen_count: Mapped[int] = mapped_column(Integer, default=0)

    device: Mapped["Device"] = relationship(back_populates="channels")


class Quality:
    OK = "ok"
    DARK = "dark"
    UNIFORM = "uniform"
    BLURRY = "blurry"
    FROZEN = "frozen"
    ERROR = "error"


class Hdd(Base):
    __tablename__ = "hdds"
    __table_args__ = (UniqueConstraint("device_id", "hdd_id", name="uq_device_hdd"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    hdd_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    capacity_mb: Mapped[int] = mapped_column(Integer, default=0)
    free_mb: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=HddState.UNKNOWN)
    raw_status: Mapped[str | None] = mapped_column(String(128), default=None)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    device: Mapped["Device"] = relationship(back_populates="hdds")

    @property
    def used_mb(self) -> int:
        return max(self.capacity_mb - self.free_mb, 0)

    @property
    def usage_percent(self) -> float:
        return round(self.used_mb / self.capacity_mb * 100, 1) if self.capacity_mb else 0.0


class NetworkSwitch(Base):
    """Коммутатор камер: SNMP только для чтения, community хранится зашифрованным."""

    __tablename__ = "network_switches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    host: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(128), default="DH-CS4226-24ET-240")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), default=None)
    snmp_port: Mapped[int] = mapped_column(Integer, default=161)
    snmp_version: Mapped[str] = mapped_column(String(8), default="2c")
    community_enc: Mapped[str] = mapped_column(Text, default="")
    timeout: Mapped[float] = mapped_column(Float, default=2.0)
    retries: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reachable: Mapped[bool] = mapped_column(Boolean, default=False)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    sys_name: Mapped[str | None] = mapped_column(String(255), default=None)
    sys_descr: Mapped[str | None] = mapped_column(Text, default=None)
    sys_object_id: Mapped[str | None] = mapped_column(String(255), default=None)
    uptime_ticks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    poe_ports: Mapped[dict] = mapped_column(JSON, default=dict)
    poe_supplies: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    ports: Mapped[list["SwitchPort"]] = relationship(back_populates="switch", cascade="all, delete-orphan")


class SwitchPort(Base):
    """Индекс интерфейса и необязательная ручная привязка камеры/PoE."""

    __tablename__ = "switch_ports"
    __table_args__ = (
        UniqueConstraint("switch_id", "if_index", name="uq_switch_interface"),
        UniqueConstraint("channel_ref_id", name="uq_switch_camera"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    switch_id: Mapped[int] = mapped_column(ForeignKey("network_switches.id", ondelete="CASCADE"))
    if_index: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    alias: Mapped[str | None] = mapped_column(String(255), default=None)
    admin_status: Mapped[int | None] = mapped_column(Integer, default=None)
    oper_status: Mapped[int | None] = mapped_column(Integer, default=None)
    speed_mbps: Mapped[float | None] = mapped_column(Float, default=None)
    last_change_ticks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    in_bps: Mapped[float | None] = mapped_column(Float, default=None)
    out_bps: Mapped[float | None] = mapped_column(Float, default=None)
    in_error_delta: Mapped[int | None] = mapped_column(BigInteger, default=None)
    out_error_delta: Mapped[int | None] = mapped_column(BigInteger, default=None)
    in_discard_delta: Mapped[int | None] = mapped_column(BigInteger, default=None)
    out_discard_delta: Mapped[int | None] = mapped_column(BigInteger, default=None)
    expected_up: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_ref_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"), default=None)
    poe_index: Mapped[str | None] = mapped_column(String(64), default=None)
    switch: Mapped["NetworkSwitch"] = relationship(back_populates="ports")


class ArchiveCoverage(Base):
    """Покрытие архива по каналу за конкретный день."""

    __tablename__ = "archive_coverage"
    __table_args__ = (
        UniqueConstraint("device_id", "channel_id", "day", name="uq_archive_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=ArchiveState.UNKNOWN)
    recorded_minutes: Mapped[int] = mapped_column(Integer, default=0)
    largest_gap_minutes: Mapped[int] = mapped_column(Integer, default=0)
    gaps: Mapped[list] = mapped_column(JSON, default=list)  # [["HH:MM","HH:MM"], ...]
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Лог событий и история изменений статуса."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), default=None
    )
    channel_id: Mapped[int | None] = mapped_column(Integer, default=None)
    type: Mapped[str] = mapped_column(String(64))  # camera_offline, nvr_unreachable, hdd_error, ...
    severity: Mapped[str] = mapped_column(String(16), default=Severity.INFO)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Note(Base):
    """Журнал обслуживания: заметки по устройству/каналу."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int | None] = mapped_column(Integer, default=None)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanMarker(Base):
    """Точка камеры/устройства на плане объекта (координаты в процентах 0–100)."""

    __tablename__ = "plan_markers"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    channel_id: Mapped[int | None] = mapped_column(Integer, default=None)
    label: Mapped[str | None] = mapped_column(String(255), default=None)
    x: Mapped[float] = mapped_column(Float, default=50.0)
    y: Mapped[float] = mapped_column(Float, default=50.0)


class AuditLog(Base):
    """Журнал действий оператора в панели (кто, что, когда)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user: Mapped[str] = mapped_column(String(64), default="—")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255), default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertState(Base):
    """Активное состояние проблемы для анти-спама и уведомлений 'восстановлено'."""

    __tablename__ = "alert_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(255), unique=True)  # уникальный ключ проблемы
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), default=None
    )
    alert_type: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_notified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    context: Mapped[dict] = mapped_column(JSON, default=dict)


class IssueAck(Base):
    """«Взял в работу» для рабочего списка проблем (разбор полётов).

    Не связано с алертами: это ручная пометка дежурного по текущей проблеме,
    идентифицируемой стабильным ключом ``issue_key`` (напр. ``dev:5:ch:3:offline``).
    """

    __tablename__ = "issue_acks"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_key: Mapped[str] = mapped_column(String(255), unique=True)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    ack_by: Mapped[str | None] = mapped_column(String(64), default=None)
    ack_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ── Модуль «Автобусы» (ручной офлайн-учёт дисковой ротации) ──────────────────
class DiskStatus:
    INSTALLED = "installed"          # стоит в автобусе
    REMOVED_REVIEW = "removed_review"  # снят, ждёт просмотра (в очереди)
    REVIEWED = "reviewed"            # просмотрен, но ещё лежит у смотрящего
    READY = "ready"                  # вернули на полку — готов к установке (резерв)
    FAULTY = "faulty"                # неисправен (ещё не списан)
    WRITTEN_OFF = "written_off"      # списан (выведен из эксплуатации)


class DiskType:
    SSD = "SSD"
    HDD = "HDD"


class Bus(Base):
    """Автобусный видеорегистратор (только ручной учёт, без сети)."""

    __tablename__ = "buses"

    id: Mapped[int] = mapped_column(primary_key=True)
    bus_number: Mapped[str] = mapped_column(String(64))
    route: Mapped[str | None] = mapped_column(String(64), default=None)
    dvr_model: Mapped[str | None] = mapped_column(String(128), default=None)
    location: Mapped[str | None] = mapped_column(String(128), default=None)  # где стоит автобус
    installed_disk_id: Mapped[int | None] = mapped_column(Integer, default=None)
    installed_since: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    collect_weekday: Mapped[int | None] = mapped_column(Integer, default=None)  # день недели сбора 0=Пн
    problem_note: Mapped[str | None] = mapped_column(Text, default=None)
    has_problem: Mapped[bool] = mapped_column(Boolean, default=False)
    # Напоминание «пора менять диск» — по умолчанию выключено, включается вручную
    # для каждого автобуса (кнопкой на странице автобуса).
    swap_alert_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Disk(Base):
    """Съёмный диск автобусного регистратора."""

    __tablename__ = "disks"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(8), default=DiskType.SSD)
    capacity_gb: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(20), default=DiskStatus.READY)
    assigned_bus_id: Mapped[int | None] = mapped_column(Integer, default=None)
    status_since: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    location: Mapped[str] = mapped_column(String(16), default="shelf")  # где физически
    last_audit_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    batch_id: Mapped[int | None] = mapped_column(Integer, default=None)        # из какой партии (asset_batches.id)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date, default=None)  # гарантия до
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetBatch(Base):
    """Поступление партии активов (диски/NVR/камеры). Единый учёт закупок."""

    __tablename__ = "asset_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="disk")  # disk | nvr | camera
    model: Mapped[str] = mapped_column(String(128))               # модель (SSD 1ТБ, DS-7616…)
    vendor: Mapped[str | None] = mapped_column(String(128), default=None)
    supplier: Mapped[str | None] = mapped_column(String(128), default=None)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    unit_cost: Mapped[float | None] = mapped_column(Float, default=None)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    user: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


ASSET_KINDS = {"disk": "Диски", "nvr": "Регистраторы", "camera": "Камеры"}


class AssetStatus:
    IN_STOCK = "in_stock"        # на складе (свободен)
    DEPLOYED = "deployed"        # установлен/в работе
    FAULTY = "faulty"            # неисправен
    WRITTEN_OFF = "written_off"  # списан


ASSET_STATUSES = {
    AssetStatus.IN_STOCK: "на складе",
    AssetStatus.DEPLOYED: "установлен",
    AssetStatus.FAULTY: "неисправен",
    AssetStatus.WRITTEN_OFF: "списан",
}


class Asset(Base):
    """Единица учёта NVR/камеры (склад → установка → неисправность → списание).

    Диски учитываются отдельной моделью Disk (они завязаны на ротацию автобусов);
    здесь — регистраторы и камеры как физические единицы с серийником/гарантией.
    """

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="nvr")  # nvr | camera
    label: Mapped[str] = mapped_column(String(64))                # инв. имя/метка
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    serial: Mapped[str | None] = mapped_column(String(128), default=None)
    vendor: Mapped[str | None] = mapped_column(String(128), default=None)
    status: Mapped[str] = mapped_column(String(16), default=AssetStatus.IN_STOCK)
    status_since: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assigned_bus_id: Mapped[int | None] = mapped_column(Integer, default=None)  # закреплён за автобусом
    location: Mapped[str | None] = mapped_column(String(255), default=None)  # доп. примечание о месте (опц.)
    device_id: Mapped[int | None] = mapped_column(Integer, default=None)     # связь с мониторингом (NVR), опц.
    batch_id: Mapped[int | None] = mapped_column(Integer, default=None)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BusProblemLog(Base):
    """История отмеченных проблем по автобусам.

    Запись открывается при пометке «проблема» и закрывается при её снятии
    (или при смене формулировки — тогда открывается новая). Даёт динамику:
    сколько раз и как долго автобус был проблемным.
    """

    __tablename__ = "bus_problem_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    bus_id: Mapped[int] = mapped_column(Integer, index=True)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    opened_by: Mapped[str | None] = mapped_column(String(64), default=None)
    closed_by: Mapped[str | None] = mapped_column(String(64), default=None)


class SwapLog(Base):
    """Журнал замен дисков по автобусам."""

    __tablename__ = "swap_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    bus_id: Mapped[int] = mapped_column(Integer)
    removed_disk_id: Mapped[int | None] = mapped_column(Integer, default=None)
    installed_disk_id: Mapped[int | None] = mapped_column(Integer, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    user: Mapped[str | None] = mapped_column(String(64), default=None)  # кто выполнил


class AppSetting(Base):
    """Простое хранилище настроек (ключ/значение), правится из UI."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    """Учётная запись панели.

    Доступ — через ``is_owner`` (владелец, видит всё) + ``permissions`` (список
    ключей вкладок из ``app/permissions.py``). Поле ``role`` — legacy, в логике
    больше не используется (оставлено, чтобы не ломать SQLite).
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="bus")  # legacy, не используется
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)  # владелец: полный контроль
    permissions: Mapped[list] = mapped_column(JSON, default=list)   # список ключей вкладок
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)    # можно отключить вход без удаления
    display_name: Mapped[str | None] = mapped_column(String(128), default=None)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DiskReview(Base):
    """Запись просмотра диска: что нашли (теги + заметка), кто и когда."""

    __tablename__ = "disk_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    disk_id: Mapped[int] = mapped_column(Integer)
    bus_id: Mapped[int | None] = mapped_column(Integer, default=None)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    user: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# Стандартные теги наблюдений при просмотре диска
OBSERVATION_TAGS = [
    "нет записи", "дыра в записи", "камера замазана",
    "нет звука", "засвет/темно", "ок",
]

# Причины, почему диск не удалось собрать в день сбора
COLLECTION_ISSUES = [
    "автобус не найден",
    "стоит в другом месте",
    "диск не сняли",
]


class DiskLocation:
    IN_BUS = "in_bus"        # в автобусе (в регистраторе)
    REVIEWER = "reviewer"    # у смотрящего (на просмотре)
    SHELF = "shelf"          # на полке (резерв)
    TRANSIT = "transit"      # в пути


DISK_LOCATIONS = {
    DiskLocation.IN_BUS: "в автобусе",
    DiskLocation.REVIEWER: "у смотрящего",
    DiskLocation.SHELF: "на полке",
    DiskLocation.TRANSIT: "в пути",
}

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ── Модуль «Заселения» (ingestion субпотока + учёт заселений + сверка с 1С) ──
class RecorderModel:
    """Типы регистраторов модуля заселений."""
    E2 = "ds7616ni_e2"       # Hikvision DS-7616NI-E2/8P — аналитики нет, motion-поиск
    H332 = "dsh332_2q"       # HiWatch DS-H332/2Q(B) — есть аналитика «человек»


# analytics_capable по умолчанию из типа регистратора
RECORDER_ANALYTICS_DEFAULT = {
    RecorderModel.E2: False,
    RecorderModel.H332: True,
}
RECORDER_MODEL_NAMES = {
    RecorderModel.E2: "Hikvision DS-7616NI-E2/8P",
    RecorderModel.H332: "HiWatch DS-H332/2Q(B)",
}


class ChannelRole:
    ENTRANCE = "entrance"
    RECEPTION = "reception"
    # Этажи гостиницы (камеры коридоров)
    FLOOR1 = "floor1"
    FLOOR2 = "floor2"
    FLOOR3 = "floor3"
    FLOOR4 = "floor4"
    FLOOR5 = "floor5"
    FLOOR6 = "floor6"


CHANNEL_ROLE_NAMES = {
    ChannelRole.ENTRANCE: "вход",
    ChannelRole.RECEPTION: "ресепшн",
    ChannelRole.FLOOR1: "этаж 1",
    ChannelRole.FLOOR2: "этаж 2",
    ChannelRole.FLOOR3: "этаж 3",
    ChannelRole.FLOOR4: "этаж 4",
    ChannelRole.FLOOR5: "этаж 5",
    ChannelRole.FLOOR6: "этаж 6",
}


class ClipStatus:
    PENDING = "pending"
    OK = "ok"
    ERROR = "error"


class CheckinVerdict:
    CHECKIN = "checkin"
    NOT = "not"
    DISPUTED = "disputed"


class NotificationStatus:
    NEW = "new"
    SEEN = "seen"
    RESOLVED = "resolved"


class CheckinHotel(Base):
    """Гостиница."""

    __tablename__ = "checkin_hotels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    recorders: Mapped[list["CheckinRecorder"]] = relationship(
        back_populates="hotel", cascade="all, delete-orphan"
    )
    onec: Mapped["OneCConnection | None"] = relationship(
        back_populates="hotel", uselist=False, cascade="all, delete-orphan"
    )


class CheckinRecorder(Base):
    """Регистратор гостиницы (реквизиты подключения + ночное окно)."""

    __tablename__ = "checkin_recorders"

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("checkin_hotels.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255), default="")
    host: Mapped[str] = mapped_column(String(255))
    http_port: Mapped[int] = mapped_column(Integer, default=80)
    rtsp_port: Mapped[int] = mapped_column(Integer, default=554)
    use_https: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(255), default="admin")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    model_type: Mapped[str] = mapped_column(String(32), default=RecorderModel.E2)
    # Модель как строка с устройства (для справки в UI; model_type — поведение)
    model_info: Mapped[str | None] = mapped_column(String(255), default=None)
    analytics_capable: Mapped[bool] = mapped_column(Boolean, default=False)
    night_start: Mapped[str] = mapped_column(String(5), default="07:00")  # HH:MM
    night_end: Mapped[str] = mapped_column(String(5), default="24:00")    # HH:MM (24:00 = конец суток)
    # Калибровка часов: смещение часов регистратора относительно сервера (сек),
    # offset = время_регистратора − время_сервера. Для формулы позиции метки 1С.
    time_offset_sec: Mapped[int | None] = mapped_column(Integer, default=None)
    time_offset_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_test_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_test_msg: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    hotel: Mapped["CheckinHotel"] = relationship(back_populates="recorders")
    channels: Mapped[list["CheckinChannel"]] = relationship(
        back_populates="recorder", cascade="all, delete-orphan"
    )


class CheckinChannel(Base):
    """Канал интереса (вход/ресепшн) с track-id субпотока."""

    __tablename__ = "checkin_channels"
    __table_args__ = (UniqueConstraint("recorder_id", "channel_id", name="uq_checkin_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recorder_id: Mapped[int] = mapped_column(ForeignKey("checkin_recorders.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    role: Mapped[str] = mapped_column(String(16), default=ChannelRole.ENTRANCE)
    substream_trackid: Mapped[int] = mapped_column(Integer, default=0)  # напр. 102 = кан.1 субпоток
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    recorder: Mapped["CheckinRecorder"] = relationship(back_populates="channels")


class CheckinClip(Base):
    """Скачанный клип субпотока (метаданные + статус загрузки)."""

    __tablename__ = "checkin_clips"
    __table_args__ = (
        UniqueConstraint("recorder_id", "channel_id", "start_ts", "end_ts", name="uq_checkin_clip"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("checkin_hotels.id", ondelete="CASCADE"))
    recorder_id: Mapped[int] = mapped_column(ForeignKey("checkin_recorders.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16), default=ChannelRole.ENTRANCE)
    day: Mapped[dt.date] = mapped_column(Date)
    start_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    path: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # Метрики загрузки: сколько скачано с устройства (сырые байты по сети) и за сколько.
    download_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    download_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    # Разрешение готового клипа — видно, какой поток реально приехал
    # (352×288 = субпоток, 1280×720/1920×1080 = основной).
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(16), default=ClipStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckinLog(Base):
    """Вердикт оператора по клипу/заселению (Фаза 2)."""

    __tablename__ = "checkin_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int | None] = mapped_column(ForeignKey("checkin_clips.id", ondelete="SET NULL"), default=None)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("checkin_hotels.id", ondelete="CASCADE"))
    day: Mapped[dt.date] = mapped_column(Date)
    shift: Mapped[str | None] = mapped_column(String(32), default=None)
    room: Mapped[str | None] = mapped_column(String(32), default=None)
    event_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    verdict: Mapped[str] = mapped_column(String(16), default=CheckinVerdict.CHECKIN)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    operator: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckinReconciliation(Base):
    """Результат сверки смены: по камере (оператор) ↔ 1С (Фаза 3)."""

    __tablename__ = "checkin_reconciliation"

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("checkin_hotels.id", ondelete="CASCADE"))
    day: Mapped[dt.date] = mapped_column(Date)
    shift: Mapped[str | None] = mapped_column(String(32), default=None)
    camera_count: Mapped[int] = mapped_column(Integer, default=0)
    ones_count: Mapped[int] = mapped_column(Integer, default=0)
    delta: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckinNotification(Base):
    """Запись Центра уведомлений (создаётся движком сверки при расхождении)."""

    __tablename__ = "checkin_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), default="shift_discrepancy")
    hotel_id: Mapped[int | None] = mapped_column(ForeignKey("checkin_hotels.id", ondelete="SET NULL"), default=None)
    day: Mapped[dt.date | None] = mapped_column(Date, default=None)
    shift: Mapped[str | None] = mapped_column(String(32), default=None)
    title: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # {camera, ones, delta, clips_url}
    status: Mapped[str] = mapped_column(String(16), default=NotificationStatus.NEW)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IngestRunStatus:
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELED = "canceled"


class CheckinIngestRun(Base):
    """Состояние прогона ночного/ручного ingestion — для прогресс-бара в UI."""

    __tablename__ = "checkin_ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[dt.date | None] = mapped_column(Date, default=None)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|schedule
    status: Mapped[str] = mapped_column(String(16), default=IngestRunStatus.RUNNING)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    recorders_total: Mapped[int] = mapped_column(Integer, default=0)
    recorders_done: Mapped[int] = mapped_column(Integer, default=0)
    downloaded: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    current: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    detail: Mapped[list] = mapped_column(JSON, default=list)  # по регистраторам
    # Суммарно за прогон: сколько байт скачано и суммарное время загрузки (мс).
    dl_bytes: Mapped[int] = mapped_column(Integer, default=0)
    dl_ms: Mapped[int] = mapped_column(Integer, default=0)


# ── Интеграция 1С:Отель (per-hotel, см. CLAUDE.md §12) ──────────────────────
class OneCConnection(Base):
    """Подключение к 1С:Отель — одна конфигурация на гостиницу.

    Поддерживает два случая развёртывания:
    - раздельные базы 1С: у каждой ГС свой base_url + креды;
    - одна база на несколько ГС: URL/креды одинаковые, события разделяются
      реквизитом-разделителем (property_field/property_value).
    """

    __tablename__ = "onec_connections"
    __table_args__ = (UniqueConstraint("hotel_id", name="uq_onec_conn_hotel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("checkin_hotels.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Подключение (стандартный OData 1С)
    base_url: Mapped[str] = mapped_column(Text, default="")   # http://<host>/<base>/odata/standard.odata
    # Приоритетный путь: URL HTTP-сервиса 1С, отдающего свежие заселения за период
    # (обход ограничений OData на больших базах). Если задан — используется он.
    service_url: Mapped[str | None] = mapped_column(Text, default=None)  # .../hs/checkins/v1
    username: Mapped[str] = mapped_column(String(255), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet, как у CheckinRecorder
    onec_tz: Mapped[str] = mapped_column(String(64), default="Asia/Khabarovsk")

    # Объект и реквизиты 1С (реальная схема Document_Accommodation, всё настраиваемо)
    entity: Mapped[str] = mapped_column(String(255), default="Document_Accommodation")
    field_ref: Mapped[str] = mapped_column(String(128), default="Ref_Key")
    field_date: Mapped[str] = mapped_column(String(128), default="CheckInDate")  # из него берём время метки
    field_date_fallback: Mapped[str] = mapped_column(String(128), default="Date")  # запас, если CheckInDate пуст
    # По этому полю $filter и $orderby: CheckInDate в части конфигураций 1С
    # недоступен для отбора (ошибка WHERE), а служебная Date — всегда отбираема.
    field_query_date: Mapped[str] = mapped_column(String(128), default="Date")
    field_guest: Mapped[str] = mapped_column(String(128), default="GuestFullName")  # ФИО строкой
    field_arrival: Mapped[str | None] = mapped_column(String(128), default=None)
    filter_posted: Mapped[bool] = mapped_column(Boolean, default=True)

    # Комната — через разворот ссылки ($expand): номер и этаж приходят готовыми полями
    expand_room: Mapped[str] = mapped_column(String(128), default="Room")            # что разворачивать
    field_room_ref: Mapped[str] = mapped_column(String(128), default="Room_Key")     # ссылка на комнату
    field_room_number: Mapped[str] = mapped_column(String(128), default="Description")  # номер во вложенном Room
    field_room_floor: Mapped[str] = mapped_column(String(128), default="Floor")      # этаж во вложенном Room

    # Разделитель для случая «одна база 1С на несколько ГС»
    property_field: Mapped[str | None] = mapped_column(String(128), default=None)  # напр. Организация_Key
    property_value: Mapped[str | None] = mapped_column(String(128), default=None)  # GUID/значение этой ГС

    # Поведение
    lookback_days: Mapped[int] = mapped_column(Integer, default=5)  # скользящее окно перечитывания
    mask_guest: Mapped[bool] = mapped_column(Boolean, default=True)

    # legacy (не используются; оставлены с дефолтами, чтобы INSERT не падал на
    # старых БД, где эти колонки были NOT NULL — как User.role, см. §13)
    field_room: Mapped[str] = mapped_column(String(128), default="Номер")
    room_floor_rule: Mapped[str] = mapped_column(String(64), default="first_digit")
    backfill_days: Mapped[int] = mapped_column(Integer, default=3)

    # Статус последней синхронизации
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_msg: Mapped[str | None] = mapped_column(Text, default=None)
    last_doc_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)  # наивное локальное
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    hotel: Mapped["CheckinHotel"] = relationship(back_populates="onec")


class OneCCheckin(Base):
    """Событие заселения из 1С:Отель (для отметок на таймлайне и сверки).

    doc_time — НАИВНОЕ локальное время Хабаровска (§6.2, без конверсии в UTC).
    Принадлежность гостинице обязательна: события ГС A не видны в клипах ГС B.
    """

    __tablename__ = "onec_checkins"
    __table_args__ = (
        UniqueConstraint("hotel_id", "onec_ref", name="uq_onec_hotel_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("checkin_hotels.id", ondelete="CASCADE"))
    onec_ref: Mapped[str] = mapped_column(String(64))            # Ref_Key документа (GUID)
    doc_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))  # наивное локальное
    room: Mapped[str | None] = mapped_column(String(64), default=None)
    floor: Mapped[int | None] = mapped_column(Integer, default=None)  # по правилу room_floor_rule
    guest: Mapped[str | None] = mapped_column(String(255), default=None)
    arrival_planned: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    matched_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("checkin_logs.id", ondelete="SET NULL"), default=None
    )  # сопоставленный вердикт оператора (сверка)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)        # исходная запись 1С
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
