"""Базовый интерфейс драйвера NVR и общие структуры данных.

Любой производитель (Hikvision/ISAPI, Dahua/CGI, в будущем — другие) реализует
абстрактный класс :class:`NVRClient`. Сервисы опроса работают только с этим
интерфейсом и ничего не знают о конкретном протоколе.
"""
from __future__ import annotations

import abc
import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


# ── Исключения ───────────────────────────────────────────────────────────────
class NVRError(Exception):
    """Базовая ошибка драйвера."""


class NVRConnectionError(NVRError):
    """Сетевая недоступность устройства (таймаут, отказ соединения)."""


class NVRAuthError(NVRError):
    """Ошибка аутентификации (неверный логин/пароль)."""


class FeatureUnavailable(NVRError):
    """Эндпоинт/возможность не поддерживается данной прошивкой."""


# ── Структуры данных, общие для всех драйверов ──────────────────────────────
@dataclass
class DeviceInfo:
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    device_type: str | None = None  # NVR / DVR / IPC ...
    channels_total: int | None = None


@dataclass
class ChannelStatus:
    channel_id: int
    name: str | None = None
    online: bool = False
    video_loss: bool = False
    kind: str = "ip"  # ip | analog

    @property
    def state(self) -> str:
        """Нормализованный статус: online / offline / no_video."""
        from app.models import ChannelState

        if not self.online:
            return ChannelState.OFFLINE
        if self.video_loss:
            return ChannelState.NO_VIDEO
        return ChannelState.ONLINE


@dataclass
class HddInfo:
    hdd_id: str
    name: str | None = None
    capacity_mb: int = 0
    free_mb: int = 0
    status: str = "unknown"  # ok | error | no_disk | unknown | unformatted | read_only
    raw_status: str | None = None


@dataclass
class ArchiveSegment:
    """Непрерывный отрезок записи в архиве."""

    start: dt.datetime
    end: dt.datetime

    @property
    def duration_minutes(self) -> float:
        return max((self.end - self.start).total_seconds() / 60.0, 0.0)


@dataclass
class HealthInfo:
    cpu_percent: float | None = None
    memory_percent: float | None = None
    temperature_c: float | None = None


@dataclass
class DeviceTime:
    device_time: dt.datetime
    server_time: dt.datetime

    @property
    def drift_seconds(self) -> int:
        return int(abs((self.device_time - self.server_time).total_seconds()))


@dataclass
class Capabilities:
    """Результат проверки возможностей (capability-check)."""

    channels: bool = False
    hdd: bool = False
    archive: bool = False
    time: bool = False

    def as_dict(self) -> dict:
        return {
            "channels": self.channels,
            "hdd": self.hdd,
            "archive": self.archive,
            "time": self.time,
        }


# ── Абстрактный драйвер ──────────────────────────────────────────────────────
class NVRClient(abc.ABC):
    """Общий интерфейс для всех NVR.

    Подклассы реализуют конкретные протоколы. HTTP-помощник :meth:`_request`
    инкапсулирует digest/basic-auth, таймауты, повторы и semaphore.
    """

    api_type: str = "unknown"

    def __init__(
        self,
        host: str,
        port: int = 80,
        username: str = "admin",
        password: str = "",
        *,
        use_https: bool = False,
        timeout: float = 15.0,
        retries: int = 2,
        auth_scheme: str = "digest",
        semaphore: asyncio.Semaphore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_https = use_https
        self.timeout = timeout
        self.retries = retries
        self.auth_scheme = auth_scheme
        self._semaphore = semaphore
        # Внешний клиент (например, для тестов через ASGITransport)
        self._external_client = client

    # ── HTTP ────────────────────────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_https else "http"
        return f"{scheme}://{self.host}:{self.port}"

    def _make_auth(self) -> httpx.Auth:
        if self.auth_scheme == "basic":
            return httpx.BasicAuth(self.username, self.password)
        return httpx.DigestAuth(self.username, self.password)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: str | bytes | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        """Единая точка HTTP-запросов: auth + таймаут + retry + semaphore."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_exc: Exception | None = None

        async def _do(http: httpx.AsyncClient) -> httpx.Response:
            nonlocal last_exc
            for attempt in range(self.retries + 1):
                try:
                    resp = await http.request(
                        method, url, content=data, params=params, headers=headers
                    )
                    if resp.status_code == 401:
                        raise NVRAuthError(f"401 Unauthorized: {url}")
                    return resp
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    raise NVRConnectionError(f"{type(exc).__name__}: {exc}") from exc
            raise NVRConnectionError(str(last_exc))  # pragma: no cover

        if self._external_client is not None:
            return await self._guarded(_do, self._external_client)

        async with httpx.AsyncClient(
            auth=self._make_auth(),
            timeout=self.timeout,
            verify=False,
            follow_redirects=True,
        ) as http:
            return await self._guarded(_do, http)

    async def _guarded(self, fn, http: httpx.AsyncClient) -> httpx.Response:
        if self._semaphore is not None:
            async with self._semaphore:
                return await fn(http)
        return await fn(http)

    # ── Контракт драйвера ─────────────────────────────────────────────────────
    @abc.abstractmethod
    async def get_device_info(self) -> DeviceInfo: ...

    @abc.abstractmethod
    async def get_channel_statuses(self) -> list[ChannelStatus]: ...

    @abc.abstractmethod
    async def get_hdd_info(self) -> list[HddInfo]: ...

    @abc.abstractmethod
    async def search_archive(
        self, channel_id: int, start: dt.datetime, end: dt.datetime
    ) -> list[ArchiveSegment]: ...

    @abc.abstractmethod
    async def get_device_time(self) -> dt.datetime: ...

    # ── Действия (необязательные; по умолчанию — «не поддерживается») ──────────
    async def get_snapshot(self, channel_id: int) -> bytes:
        """Возвращает текущий кадр канала (JPEG)."""
        raise FeatureUnavailable("снимок не поддерживается")

    async def sync_time(self) -> None:
        """Выставляет на устройстве текущее локальное время сервера."""
        raise FeatureUnavailable("синхронизация времени не поддерживается")

    async def reboot(self) -> None:
        """Перезагружает устройство."""
        raise FeatureUnavailable("перезагрузка не поддерживается")

    async def get_health(self) -> "HealthInfo":
        """Возвращает температуру/нагрузку устройства, если доступно."""
        raise FeatureUnavailable("телеметрия здоровья не поддерживается")

    # ── Модуль «Заселения»: треки, поиск активности, RTSP архива ──────────────
    async def list_tracks(self) -> list[dict]:
        """Список дорожек записи: [{'trackid':101,'channel':1,'type':'video','is_sub':False}].

        Нужен, чтобы в UI выбирать trackid субпотока из списка, а не угадывать.
        """
        raise FeatureUnavailable("список треков не поддерживается")

    async def search_activity(
        self, channel_id: int, start: dt.datetime, end: dt.datetime, *, mode: str = "all"
    ) -> list[ArchiveSegment]:
        """Сегменты с активностью. mode: 'human' | 'motion' | 'all'.

        Реализация по умолчанию не умеет фильтровать → возвращает всё окно целиком
        (субпоток лёгкий — это штатный фолбэк из ТЗ).
        """
        return [ArchiveSegment(start, end)]

    def rtsp_playback_url(
        self, trackid: int, start: dt.datetime, end: dt.datetime, *, rtsp_port: int = 554
    ) -> str:
        """URL для выкачивания отрезка архива по треку через RTSP."""
        raise FeatureUnavailable("RTSP playback не поддерживается")

    async def test_connection(self) -> DeviceInfo:
        """Базовый тест: получить инфо об устройстве (бросает при ошибке)."""
        return await self.get_device_info()

    async def probe_capabilities(self) -> Capabilities:
        """Проверяет каждый ключевой эндпоинт. Недоступные → False, без падения.

        Реализация по умолчанию пробует все методы; драйверы могут переопределить.
        """
        caps = Capabilities()

        async def _probe(coro) -> bool:
            try:
                await coro
                return True
            except FeatureUnavailable:
                return False
            except NVRError as exc:
                log.debug("probe: %s", exc)
                return False

        caps.channels = await _probe(self.get_channel_statuses())
        caps.hdd = await _probe(self.get_hdd_info())
        caps.time = await _probe(self.get_device_time())
        # Архив пробуем коротким окном на первом канале
        try:
            now = dt.datetime.now()
            await self.search_archive(1, now - dt.timedelta(minutes=5), now)
            caps.archive = True
        except (FeatureUnavailable, NVRError):
            caps.archive = False
        return caps
