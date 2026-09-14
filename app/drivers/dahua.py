"""Драйвер Dahua / RVI (Dahua-based) — HTTP CGI API (digest или basic auth).

Особенности, заложенные по ТЗ:
* статус камер через LogicDeviceManager (JSON), fallback на VideoLoss-индексы;
* поиск архива через фабрику mediaFileFind (create → findFile → findNextFile → destroy);
* поиск использует ЛОКАЛЬНОЕ время устройства (часы NVR могут плыть);
* часть CGI на старых прошивках отсутствует → FeatureUnavailable вместо падения.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.parse

from app.drivers.base import (
    ArchiveSegment,
    ChannelStatus,
    DeviceInfo,
    FeatureUnavailable,
    HddInfo,
    NVRClient,
    NVRError,
)
from app.models import ApiType, HddState

log = logging.getLogger(__name__)

_DAHUA_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_kv(text: str) -> dict[str, str]:
    """Парсит ответ Dahua вида key=value (по строке на пару)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip()
    return out


def _parse_dahua_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value.strip(), _DAHUA_TIME_FMT)


def _body(resp) -> str:
    """Декодирует тело как UTF-8 (имена камер бывают на кириллице)."""
    return resp.content.decode("utf-8", "replace")


class DahuaClient(NVRClient):
    api_type = ApiType.DAHUA

    # ── Идентификация ─────────────────────────────────────────────────────────
    async def get_device_info(self) -> DeviceInfo:
        resp = await self._request(
            "GET", "/cgi-bin/magicBox.cgi", params={"action": "getSystemInfo"}
        )
        if resp.status_code != 200:
            raise FeatureUnavailable(f"getSystemInfo: HTTP {resp.status_code}")
        kv = _parse_kv(resp.text)
        firmware = None
        try:
            r2 = await self._request(
                "GET", "/cgi-bin/magicBox.cgi", params={"action": "getSoftwareVersion"}
            )
            if r2.status_code == 200:
                firmware = _parse_kv(r2.text).get("version")
        except FeatureUnavailable:
            pass
        return DeviceInfo(
            model=kv.get("deviceType") or kv.get("DeviceType"),
            firmware=firmware or kv.get("version"),
            serial=kv.get("serialNumber") or kv.get("sn"),
            device_type=kv.get("deviceType"),
        )

    # ── Каналы ────────────────────────────────────────────────────────────────
    async def get_channel_statuses(self) -> list[ChannelStatus]:
        # Основной путь: LogicDeviceManager (JSON)
        try:
            resp = await self._request(
                "POST",
                "/cgi-bin/api/LogicDeviceManager/getCameraState",
                data=json.dumps({"uniqueChannels": [-1]}),
                headers={"Content-Type": "application/json"},
            )
            body = _body(resp)
            if resp.status_code == 200 and body.strip().startswith("{"):
                data = json.loads(body)
                states = data.get("states", [])
                result: list[ChannelStatus] = []
                for st in states:
                    ch = int(st.get("channel", 0))
                    conn = str(st.get("connectionState", "")).lower()
                    video = str(st.get("videoInputState", "normal")).lower()
                    online = conn == "connected"
                    video_loss = (not online) or video in ("lossvideo", "novideo", "loss")
                    result.append(
                        ChannelStatus(
                            channel_id=ch + 1,  # Dahua 0-based → 1-based для UI
                            name=st.get("name"),
                            online=online,
                            video_loss=video_loss,
                            kind="ip",
                        )
                    )
                if result:
                    return result
        except FeatureUnavailable:
            pass

        # Fallback: VideoLoss-индексы (старые прошивки)
        resp = await self._request(
            "GET",
            "/cgi-bin/eventManager.cgi",
            params={"action": "getEventIndexes", "code": "VideoLoss"},
        )
        if resp.status_code != 200:
            raise FeatureUnavailable("статус каналов недоступен")
        kv = _parse_kv(resp.text)
        # channels[0]=0, channels[1]=3 ... — каналы С потерей видео
        loss_channels = {int(v) for k, v in kv.items() if k.startswith("channels[")}
        total = int(kv.get("channels", 0)) if kv.get("channels", "").isdigit() else 16
        result = []
        for ch in range(total):
            result.append(
                ChannelStatus(
                    channel_id=ch + 1,
                    online=True,
                    video_loss=ch in loss_channels,
                    kind="ip",
                )
            )
        return result

    # ── HDD ───────────────────────────────────────────────────────────────────
    async def get_hdd_info(self) -> list[HddInfo]:
        resp = await self._request(
            "GET", "/cgi-bin/storageDevice.cgi", params={"action": "getDeviceAllInfo"}
        )
        if resp.status_code in (404, 405, 501):
            raise FeatureUnavailable(f"storageDevice: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise NVRError(f"storageDevice: HTTP {resp.status_code}")
        kv = _parse_kv(_body(resp))
        # Формат: list[0].Detail[0].TotalBytes=..., .UsedBytes=..., .State=...
        # Собираем по индексам list[i].Detail[j]
        disks: dict[str, dict[str, str]] = {}
        for key, val in kv.items():
            if re.fullmatch(r"list\[\d+\]\.Detail\[\d+\]\.\w+", key):
                prefix, _, field = key.rpartition(".")
                disks.setdefault(prefix, {})[field] = val
        hdds: list[HddInfo] = []
        for prefix, fields in sorted(disks.items()):
            try:
                total_b = int(fields["TotalBytes"])
                used_b = int(fields.get("UsedBytes", 0) or 0)
                if total_b < 0 or used_b < 0 or used_b > total_b:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise NVRError(f"HDD {prefix}: неполный или некорректный объём") from exc
            raw_state = fields.get("State", "")
            state = raw_state.lower().replace("-", "").replace("_", "").replace(" ", "")
            prop = fields.get("Type") or fields.get("Property") or ""
            access = prop.lower().replace("-", "").replace("_", "").replace(" ", "")
            name = fields.get("Name") or fields.get("Path") or prefix
            if state in ("error", "abnormal", "broken", "failed") or fields.get("IsError", "").lower() in ("true", "1"):
                status = HddState.ERROR
            elif state in ("unformatted", "uninitialized", "notformatted"):
                status = HddState.UNFORMATTED
            elif state in ("readonly", "ro") or access in ("readonly", "ro") or fields.get("ReadOnly", "").lower() in ("true", "1"):
                status = HddState.READ_ONLY
            elif state in ("nodisk", "notexist", "absent") or (total_b == 0 and state in ("", "idle")):
                status = HddState.NO_DISK
            elif state in ("ok", "normal", "running") and total_b > 0:
                status = HddState.OK
            else:
                status = HddState.UNKNOWN
            hdds.append(
                HddInfo(
                    hdd_id=prefix,
                    name=name,
                    capacity_mb=total_b // (1024 * 1024),
                    free_mb=max(total_b - used_b, 0) // (1024 * 1024),
                    status=status,
                    raw_status=f"{raw_state}; property={prop}" if prop else raw_state or None,
                )
            )
        if not hdds:
            # Пустое тело/неизвестный ответ не доказывает отсутствие дисков.
            # Принимаем только явный пустой список от прошивки.
            if not any(kv.get(key) in ("0", "[]") for key in ("list", "list.count", "list.length")):
                raise NVRError("HDD: полный список дисков не подтверждён")
        return hdds

    # ── Архив (фабрика mediaFileFind) ──────────────────────────────────────────
    async def search_archive(
        self, channel_id: int, start: dt.datetime, end: dt.datetime
    ) -> list[ArchiveSegment]:
        dahua_channel = channel_id - 1  # обратно в 0-based
        # a) factory.create
        resp = await self._request(
            "GET", "/cgi-bin/mediaFileFind.cgi", params={"action": "factory.create"}
        )
        if resp.status_code in (404, 405, 501):
            raise FeatureUnavailable("mediaFileFind не поддерживается")
        if resp.status_code != 200:
            raise NVRError(f"factory.create: HTTP {resp.status_code}")
        obj = _parse_kv(resp.text).get("result")
        if not obj or not obj.isdigit():
            raise NVRError("mediaFileFind: нет корректного object id")

        segments: list[ArchiveSegment] = []
        try:
            # b) findFile с условием
            find_params = {
                "action": "findFile",
                "object": obj,
                "condition.Channel": dahua_channel,
                "condition.StartTime": start.strftime(_DAHUA_TIME_FMT),
                "condition.EndTime": end.strftime(_DAHUA_TIME_FMT),
                "condition.Types[0]": "dav",
            }
            r = await self._request(
                "GET", "/cgi-bin/mediaFileFind.cgi", params=find_params
            )
            if r.status_code in (404, 405, 501):
                raise FeatureUnavailable("findFile не поддерживается")
            if r.status_code != 200:
                raise NVRError(f"findFile: HTTP {r.status_code}")
            if _parse_kv(r.text).get("found") == "0":
                return []  # Явный успешный пустой результат.
            if r.text.strip().lower() != "ok":
                raise NVRError("findFile: запрос не подтверждён устройством")

            # c) findNextFile (постранично)
            for _ in range(200):
                rn = await self._request(
                    "GET",
                    "/cgi-bin/mediaFileFind.cgi",
                    params={"action": "findNextFile", "object": obj, "count": 100},
                )
                if rn.status_code != 200:
                    raise NVRError(f"findNextFile: HTTP {rn.status_code}; результат неполный")
                kv = _parse_kv(rn.text)
                try:
                    found = int(kv["found"])
                    if not 0 <= found <= 100:
                        raise ValueError
                except (KeyError, ValueError) as exc:
                    raise NVRError("findNextFile: не подтверждено число найденных записей") from exc
                items: dict[int, dict[str, str]] = {}
                for key, val in kv.items():
                    if key.startswith("items["):
                        match = re.fullmatch(r"items\[(\d+)\]\.(\w+)", key)
                        if not match:
                            raise NVRError("findNextFile: некорректная запись результата")
                        items.setdefault(int(match[1]), {})[match[2]] = val
                if len(items) != found:
                    raise NVRError("findNextFile: неполная страница результатов")
                for _, fields in sorted(items.items()):
                    st = fields.get("StartTime")
                    en = fields.get("EndTime")
                    try:
                        if not st or not en:
                            raise ValueError
                        segment = ArchiveSegment(_parse_dahua_time(st), _parse_dahua_time(en))
                        if segment.end <= segment.start:
                            raise ValueError
                    except ValueError as exc:
                        raise NVRError("findNextFile: некорректный временной интервал") from exc
                    segments.append(segment)
                if found < 100:
                    break
            else:
                raise NVRError("Архив: превышен предел страниц; результат неполный")
        finally:
            # d) destroy
            try:
                await self._request(
                    "GET",
                    "/cgi-bin/mediaFileFind.cgi",
                    params={"action": "destroy", "object": obj},
                )
            except NVRError as exc:
                log.debug("mediaFileFind destroy: %s", exc)
        return segments

    # ── Время устройства ───────────────────────────────────────────────────────
    async def get_device_time(self) -> dt.datetime:
        resp = await self._request(
            "GET", "/cgi-bin/global.cgi", params={"action": "getCurrentTime"}
        )
        if resp.status_code != 200:
            raise FeatureUnavailable(f"getCurrentTime: HTTP {resp.status_code}")
        kv = _parse_kv(resp.text)
        raw = kv.get("result") or resp.text.strip()
        raw = urllib.parse.unquote(raw)
        return _parse_dahua_time(raw)

    # ── Действия ───────────────────────────────────────────────────────────────
    async def get_snapshot(self, channel_id: int) -> bytes:
        resp = await self._request(
            "GET", "/cgi-bin/snapshot.cgi", params={"channel": channel_id}
        )
        if resp.status_code != 200 or not resp.content:
            raise FeatureUnavailable(f"snapshot: HTTP {resp.status_code}")
        return resp.content

    async def sync_time(self, target: dt.datetime | None = None) -> None:
        target = target or dt.datetime.now().astimezone()
        now = target.strftime(_DAHUA_TIME_FMT)
        resp = await self._request(
            "GET", "/cgi-bin/global.cgi",
            params={"action": "setCurrentTime", "time": now},
        )
        if resp.status_code != 200 or "ok" not in resp.text.lower():
            raise FeatureUnavailable(f"setCurrentTime: HTTP {resp.status_code}")

    async def reboot(self) -> None:
        resp = await self._request(
            "GET", "/cgi-bin/magicBox.cgi", params={"action": "reboot"}
        )
        if resp.status_code != 200:
            raise FeatureUnavailable(f"reboot: HTTP {resp.status_code}")
