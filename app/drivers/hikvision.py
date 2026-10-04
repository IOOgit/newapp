"""Драйвер Hikvision / HiWatch — ISAPI (HTTP + XML, digest auth)."""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx

# Дескрипторы метаданных ISAPI для поиска по типу активности (best-effort;
# при неподдержке прошивкой сервис откатывается на motion → всё окно целиком).
_MOTION_DESC = "//recordType.meta.std-cgi.com/motionDetection"
_HUMAN_DESC = "//recordType.meta.std-cgi.com/humanDetection"

from app.drivers.base import (
    ArchiveSegment,
    ChannelStatus,
    DeviceInfo,
    FeatureUnavailable,
    HddInfo,
    HealthInfo,
    NVRClient,
    NVRError,
)
from app.models import ApiType, HddState

log = logging.getLogger(__name__)


def _strip_ns(xml_text: str) -> ET.Element:
    """Парсит XML, убирая namespace из тегов (ISAPI любит namespaces)."""
    # удаляем xmlns=... чтобы теги были без префиксов
    cleaned = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml_text, count=0)
    cleaned = re.sub(r"<(/?)\w+:", r"<\1", cleaned)
    return ET.fromstring(cleaned)


def _body(resp) -> str:
    """Декодирует тело ответа в текст.

    ISAPI заявляет UTF-8, но многие прошивки Hikvision отдают имена камер в
    Windows-1251 (кириллица). Поэтому: пробуем строгий UTF-8, при ошибке —
    откатываемся на cp1251.
    """
    raw = resp.content
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    found = el.find(f".//{tag}")
    return found.text.strip() if found is not None and found.text else None


def _parse_hik_time(value: str) -> dt.datetime:
    """Парсит время ISAPI (ISO 8601, иногда с 'Z' для локального времени)."""
    v = value.strip()
    try:
        if v.endswith("Z"):
            return dt.datetime.fromisoformat(v[:-1])
        return dt.datetime.fromisoformat(v).replace(tzinfo=None)
    except ValueError:
        return dt.datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")


def _isapi_error(resp) -> str:
    """Достаёт причину отказа из тела ISAPI (statusString/subStatusCode)."""
    try:
        root = _strip_ns(_body(resp))
        parts = [_text(root, "statusString"), _text(root, "subStatusCode")]
        detail = ", ".join(p for p in parts if p)
        return detail or f"HTTP {resp.status_code}"
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"


class HikvisionClient(NVRClient):
    api_type = ApiType.HIKVISION

    # ── Идентификация ─────────────────────────────────────────────────────────
    async def get_device_info(self) -> DeviceInfo:
        resp = await self._request("GET", "/ISAPI/System/deviceInfo")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"deviceInfo: HTTP {resp.status_code}")
        root = _strip_ns(_body(resp))
        return DeviceInfo(
            model=_text(root, "model"),
            firmware=_text(root, "firmwareVersion"),
            serial=_text(root, "serialNumber"),
            device_type=_text(root, "deviceType"),
        )

    # ── Каналы ────────────────────────────────────────────────────────────────
    async def get_channel_statuses(self) -> list[ChannelStatus]:
        result: dict[int, ChannelStatus] = {}

        # 1) IP-камеры через InputProxy
        try:
            resp = await self._request(
                "GET", "/ISAPI/ContentMgmt/InputProxy/channels/status"
            )
            if resp.status_code == 200:
                root = _strip_ns(_body(resp))
                for item in root.findall(".//InputProxyChannelStatus"):
                    cid = _text(item, "id")
                    if cid is None:
                        continue
                    online = (_text(item, "online") or "false").lower() == "true"
                    name = _text(item, "name") or _text(item, "ipAddress")
                    result[int(cid)] = ChannelStatus(
                        channel_id=int(cid), name=name, online=online,
                        video_loss=not online, kind="ip",
                    )
        except FeatureUnavailable:
            pass

        # 2) Аналоговые/гибридные входы (DS-H332/2Q и т.п.) — videoloss
        try:
            resp = await self._request("GET", "/ISAPI/System/Video/inputs/channels")
            if resp.status_code == 200:
                root = _strip_ns(_body(resp))
                for ch in root.findall(".//VideoInputChannel"):
                    cid = _text(ch, "id")
                    if cid is None:
                        continue
                    cid_i = int(cid)
                    if cid_i in result:
                        continue  # уже учтён как IP
                    enabled = (_text(ch, "videoInputEnabled") or "true").lower() == "true"
                    if not enabled:
                        continue  # вход административно отключён — не мониторим
                    # Нет сигнала: пустое разрешение или явный маркер "NO VIDEO"
                    res_desc = (_text(ch, "resDesc") or "").strip().upper()
                    video_loss = (not res_desc) or "NO VIDEO" in res_desc
                    result[cid_i] = ChannelStatus(
                        channel_id=cid_i,
                        name=_text(ch, "name"),
                        online=True,           # аналоговый вход физически есть
                        video_loss=video_loss,
                        kind="analog",
                    )
        except FeatureUnavailable:
            pass

        if not result:
            raise FeatureUnavailable("статус каналов недоступен")
        return [result[k] for k in sorted(result)]

    # ── HDD ───────────────────────────────────────────────────────────────────
    async def get_hdd_info(self) -> list[HddInfo]:
        resp = await self._request("GET", "/ISAPI/ContentMgmt/Storage/hdd")
        if resp.status_code in (404, 405, 501):
            raise FeatureUnavailable(f"hdd: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise NVRError(f"hdd: HTTP {resp.status_code}")
        try:
            root = _strip_ns(_body(resp))
        except ET.ParseError as exc:
            raise NVRError("HDD: некорректный XML, состав дисков не подтверждён") from exc
        if root.tag != "hddList" and root.find(".//hddList") is None:
            raise NVRError("HDD: в ответе отсутствует полный список hddList")
        hdds: list[HddInfo] = []
        seen: set[str] = set()
        for hdd in root.findall(".//hdd"):
            hid = _text(hdd, "id")
            if not hid or hid in seen:
                raise NVRError("HDD: отсутствующий или повторный ID диска")
            seen.add(hid)
            raw_status = _text(hdd, "status") or ""
            state = raw_status.lower().replace("-", "").replace("_", "").replace(" ", "")
            prop = (_text(hdd, "property") or _text(hdd, "hddProperty") or "")
            access = prop.lower().replace("-", "").replace("_", "").replace(" ", "")
            try:
                cap = int(_text(hdd, "capacity"))  # МБ
                free = int(_text(hdd, "freeSpace") or 0)
                if cap < 0 or free < 0 or free > cap:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise NVRError(f"HDD {hid}: некорректный объём, состав дисков не подтверждён") from exc
            if state in ("error", "failed", "smartfailed", "abnormal"):
                status = HddState.ERROR
            elif state in ("unformatted", "uninitialized", "notformatted"):
                status = HddState.UNFORMATTED
            elif state in ("readonly", "ro") or access in ("readonly", "ro") or (_text(hdd, "readOnly") or "").lower() == "true":
                status = HddState.READ_ONLY
            elif state in ("nodisk", "notexist", "absent") or (state in ("", "idle") and cap == 0):
                status = HddState.NO_DISK
            elif state in ("ok", "normal") and cap > 0:
                status = HddState.OK
            else:
                status = HddState.UNKNOWN
            hdds.append(
                HddInfo(hdd_id=hid, name=_text(hdd, "hddName"),
                        capacity_mb=cap, free_mb=free, status=status,
                        raw_status=f"{raw_status}; property={prop}" if prop else raw_status or None)
            )
        # Валидный пустой hddList — подтверждённое отсутствие дисков, а не
        # неподдерживаемая возможность. Только такой ответ позволяет пометить пропажу.
        return hdds

    # ── Архив ─────────────────────────────────────────────────────────────────
    async def search_archive(
        self, channel_id: int, start: dt.datetime, end: dt.datetime
    ) -> list[ArchiveSegment]:
        track_id = channel_id * 100 + 1  # 1 → 101 (основной поток)
        # Часть прошивок Hikvision требует searchID строго в формате UUID,
        # иначе отвечают 400/невалидным XML. Используем настоящий UUID.
        search_id = str(uuid.uuid4())
        segments: list[ArchiveSegment] = []
        position = 0
        page = 100
        for _ in range(50):  # ограничение пагинации
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                "<CMSearchDescription version=\"1.0\" xmlns=\"http://www.hikvision.com/ver20/XMLSchema\">"
                f"<searchID>{search_id}</searchID>"
                f"<trackList><trackID>{track_id}</trackID></trackList>"
                "<timeSpanList><timeSpan>"
                f"<startTime>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</startTime>"
                f"<endTime>{end.strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>"
                "</timeSpan></timeSpanList>"
                f"<maxResults>{page}</maxResults>"
                f"<searchResultPosition>{position}</searchResultPosition>"
                "</CMSearchDescription>"
            )
            resp = await self._request(
                "POST", "/ISAPI/ContentMgmt/search", data=body,
                headers={"Content-Type": "application/xml"},
            )
            if resp.status_code in (404, 405, 501):
                raise FeatureUnavailable("поиск архива не поддерживается")
            if resp.status_code != 200:
                raise NVRError(f"search: HTTP {resp.status_code}")
            try:
                root = _strip_ns(_body(resp))
            except ET.ParseError as exc:
                raise NVRError("Архив: некорректный XML результата поиска") from exc
            status_str = (_text(root, "responseStatusStrg") or "").upper().strip()
            if root.tag != "CMSearchResult" or status_str not in ("OK", "MORE", "NO MATCHES"):
                raise NVRError("Архив: поиск не подтвердил успешный результат")
            if (_text(root, "responseStatus") or "true").lower() == "false" and status_str != "NO MATCHES":
                raise NVRError("Архив: NVR отклонил запрос поиска")
            matches = root.findall(".//searchMatchItem")
            reported = _text(root, "numOfMatches")
            if reported is not None:
                try:
                    count = int(reported)
                    if count < len(matches) or (status_str != "MORE" and count != len(matches)):
                        raise ValueError
                except ValueError as exc:
                    raise NVRError("Архив: число записей не соответствует результату поиска") from exc
            if (status_str == "MORE" and not matches) or (status_str == "NO MATCHES" and matches):
                raise NVRError("Архив: противоречивый результат поиска")
            for m in matches:
                ts = m.find(".//timeSpan")
                st = _text(ts, "startTime")
                en = _text(ts, "endTime")
                try:
                    if not st or not en:
                        raise ValueError
                    segment = ArchiveSegment(_parse_hik_time(st), _parse_hik_time(en))
                    if segment.end <= segment.start:
                        raise ValueError
                except ValueError as exc:
                    raise NVRError("Архив: некорректный временной интервал записи") from exc
                segments.append(segment)
            if status_str in ("OK", "NO MATCHES"):
                break
            position += len(matches)
        else:
            raise NVRError("Архив: превышен предел страниц; результат неполный")
        return segments

    # ── Время устройства ───────────────────────────────────────────────────────
    async def get_device_time(self) -> dt.datetime:
        resp = await self._request("GET", "/ISAPI/System/time")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"time: HTTP {resp.status_code}")
        root = _strip_ns(_body(resp))
        local = _text(root, "localTime")
        if not local:
            raise FeatureUnavailable("нет localTime")
        return _parse_hik_time(local)

    # ── Действия ───────────────────────────────────────────────────────────────
    async def get_snapshot(self, channel_id: int) -> bytes:
        # Основной поток канала: /Streaming/channels/<ch*100+1>/picture
        resp = await self._request(
            "GET", f"/ISAPI/Streaming/channels/{channel_id * 100 + 1}/picture"
        )
        if resp.status_code != 200 or not resp.content:
            raise FeatureUnavailable(f"snapshot: HTTP {resp.status_code}")
        return resp.content

    async def sync_time(self, target: dt.datetime | None = None) -> None:
        target = target or dt.datetime.now().astimezone()
        cur = await self._request("GET", "/ISAPI/System/time")
        if cur.status_code != 200:
            raise FeatureUnavailable(f"time GET: HTTP {cur.status_code}")
        now_iso = target.replace(microsecond=0).isoformat()
        body = re.sub(r"<localTime>.*?</localTime>", f"<localTime>{now_iso}</localTime>", _body(cur))
        body = re.sub(r"<timeMode>.*?</timeMode>", "<timeMode>manual</timeMode>", body)
        resp = await self._request(
            "PUT", "/ISAPI/System/time", data=body,
            headers={"Content-Type": "application/xml"},
        )
        if resp.status_code not in (200, 201):
            raise FeatureUnavailable(f"установка времени отклонена ({_isapi_error(resp)})")

    async def reboot(self) -> None:
        resp = await self._request("PUT", "/ISAPI/System/reboot")
        if resp.status_code not in (200, 201):
            raise FeatureUnavailable(f"перезагрузка отклонена ({_isapi_error(resp)})")

    async def get_health(self) -> HealthInfo:
        resp = await self._request("GET", "/ISAPI/System/status")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"status: HTTP {resp.status_code}")
        root = _strip_ns(_body(resp))
        # CPU: среднее по всем ядрам
        cpus = [int(e.text) for e in root.findall(".//cpuUtilization") if e is not None and e.text]
        cpu = round(sum(cpus) / len(cpus), 1) if cpus else None
        # Память: usage / (usage + available)
        usage = root.find(".//memoryUsage")
        avail = root.find(".//memoryAvailable")
        mem = None
        try:
            if usage is not None and avail is not None and usage.text and avail.text:
                u, a = float(usage.text), float(avail.text)
                if u + a > 0:
                    mem = round(u / (u + a) * 100, 1)
        except ValueError:
            mem = None
        temp_el = root.find(".//temperature")
        temp = None
        if temp_el is not None and temp_el.text:
            try:
                temp = float(temp_el.text)
            except ValueError:
                temp = None
        return HealthInfo(cpu_percent=cpu, memory_percent=mem, temperature_c=temp)

    # ── Модуль «Заселения» ────────────────────────────────────────────────────
    async def list_tracks(self) -> list[dict]:
        """Дорожки записи устройства (для выбора trackid субпотока в UI)."""
        resp = await self._request("GET", "/ISAPI/ContentMgmt/record/tracks")
        if resp.status_code != 200:
            raise FeatureUnavailable(f"tracks: HTTP {resp.status_code}")
        root = _strip_ns(_body(resp))
        tracks: list[dict] = []
        for tr in root.findall(".//Track"):
            tid = _text(tr, "id") or _text(tr, "trackID")
            if tid is None or not tid.isdigit():
                continue
            tid_i = int(tid)
            ttype = (_text(tr, "TrackType") or _text(tr, "trackType") or "").lower()
            if ttype and ttype != "video":
                continue  # интересует только видео-дорожка
            ch = _text(tr, "Channel") or _text(tr, "channel")
            tracks.append({
                "trackid": tid_i,
                "channel": int(ch) if ch and ch.isdigit() else tid_i // 100,
                "type": ttype or "video",
                "is_sub": (tid_i % 100) == 2,
            })
        if not tracks:
            raise FeatureUnavailable("список треков пуст")
        return sorted(tracks, key=lambda t: t["trackid"])

    async def _search_segments(
        self, track_id: int, start: dt.datetime, end: dt.datetime, descriptor: str | None = None
    ) -> list[ArchiveSegment]:
        """Поиск отрезков записи по треку; при descriptor — только с этой активностью."""
        search_id = str(uuid.uuid4())
        meta = (
            f"<metadataList><metadataDescriptor>{descriptor}</metadataDescriptor></metadataList>"
            if descriptor else ""
        )
        segments: list[ArchiveSegment] = []
        position, page = 0, 100
        for _ in range(50):
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                "<CMSearchDescription version=\"1.0\" xmlns=\"http://www.hikvision.com/ver20/XMLSchema\">"
                f"<searchID>{search_id}</searchID>"
                f"<trackList><trackID>{track_id}</trackID></trackList>"
                "<timeSpanList><timeSpan>"
                f"<startTime>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</startTime>"
                f"<endTime>{end.strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>"
                "</timeSpan></timeSpanList>"
                f"<maxResults>{page}</maxResults>"
                f"<searchResultPosition>{position}</searchResultPosition>"
                f"{meta}</CMSearchDescription>"
            )
            resp = await self._request(
                "POST", "/ISAPI/ContentMgmt/search", data=body,
                headers={"Content-Type": "application/xml"},
            )
            if resp.status_code in (400, 404):
                # прошивка не понимает дескриптор/поиск — сигналим фолбэк
                raise FeatureUnavailable(f"search ({descriptor or 'plain'}): HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise FeatureUnavailable(f"search: HTTP {resp.status_code}")
            root = _strip_ns(_body(resp))
            matches = root.findall(".//searchMatchItem")
            for m in matches:
                ts = m.find(".//timeSpan")
                if ts is None:
                    continue
                st, en = _text(ts, "startTime"), _text(ts, "endTime")
                if st and en:
                    segments.append(ArchiveSegment(_parse_hik_time(st), _parse_hik_time(en)))
            status_str = (_text(root, "responseStatusStrg") or "").upper()
            if len(matches) < page or status_str == "OK":
                break
            position += page
        return segments

    async def search_playback(
        self, channel_id: int, start: dt.datetime, end: dt.datetime, *, substream: bool = True
    ) -> list[dict]:
        """Ищет записанные сегменты трека и возвращает их ГОТОВЫЙ playbackURI с
        устройства (там верный trackid и формат времени самого регистратора).

        Возвращает [{'start': dt, 'end': dt, 'uri': 'rtsp://ip/Streaming/tracks/..'}].
        """
        track_id = channel_id * 100 + (2 if substream else 1)
        search_id = str(uuid.uuid4())
        out: list[dict] = []
        position, page = 0, 200
        for _ in range(50):
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                "<CMSearchDescription version=\"1.0\" xmlns=\"http://www.hikvision.com/ver20/XMLSchema\">"
                f"<searchID>{search_id}</searchID>"
                f"<trackList><trackID>{track_id}</trackID></trackList>"
                "<timeSpanList><timeSpan>"
                f"<startTime>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</startTime>"
                f"<endTime>{end.strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>"
                "</timeSpan></timeSpanList>"
                f"<maxResults>{page}</maxResults>"
                f"<searchResultPosition>{position}</searchResultPosition>"
                "</CMSearchDescription>"
            )
            resp = await self._request(
                "POST", "/ISAPI/ContentMgmt/search", data=body,
                headers={"Content-Type": "application/xml"},
            )
            if resp.status_code in (400, 404):
                raise FeatureUnavailable(f"search: HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise FeatureUnavailable(f"search: HTTP {resp.status_code}")
            root = _strip_ns(_body(resp))
            matches = root.findall(".//searchMatchItem")
            for m in matches:
                ts = m.find(".//timeSpan")
                st = _text(ts, "startTime") if ts is not None else None
                en = _text(ts, "endTime") if ts is not None else None
                uri = _text(m, "playbackURI")
                if st and en:
                    out.append({
                        "start": _parse_hik_time(st), "end": _parse_hik_time(en),
                        "uri": (uri or "").strip(),
                    })
            status_str = (_text(root, "responseStatusStrg") or "").upper()
            if len(matches) < page or status_str == "OK":
                break
            position += page
        return out

    async def download_segment(
        self, playback_uri: str, out_path: str, max_bytes: int | None = None,
        progress=None,
    ) -> tuple[bool, int, str]:
        """Скачивает отрезок архива по HTTP через /ISAPI/ContentMgmt/download.

        Обход RTSP (порт 554): использует playbackURI устройства и HTTP-порт 80
        (там же, где работает ISAPI). max_bytes — остановиться после N байт
        (для пробы). progress — async-колбэк (скачано_байт, ожидается_всего|0),
        вызывается не чаще ~раз в секунду для живого процента/скорости.
        Возвращает (успех, байт, ошибка)."""
        import time

        esc = playback_uri.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<downloadRequest version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            f"<playbackURI>{esc}</playbackURI></downloadRequest>"
        )
        url = f"{self.base_url}/ISAPI/ContentMgmt/download"
        try:
            async with httpx.AsyncClient(
                auth=self._make_auth(), timeout=self.timeout, verify=False, follow_redirects=True
            ) as http:
                async with http.stream(
                    "POST", url, content=body, headers={"Content-Type": "application/xml"}
                ) as resp:
                    if resp.status_code != 200:
                        text = (await resp.aread()).decode("utf-8", "replace")[:200]
                        return False, 0, f"HTTP {resp.status_code}: {text}"
                    expected = int(resp.headers.get("content-length") or 0)
                    total = 0
                    last_cb = time.monotonic()
                    with open(out_path, "wb") as fh:
                        async for chunk in resp.aiter_bytes(65536):
                            fh.write(chunk)
                            total += len(chunk)
                            if progress is not None:
                                now = time.monotonic()
                                if now - last_cb >= 1.0:
                                    await progress(total, expected)
                                    last_cb = now
                            if max_bytes and total >= max_bytes:
                                break  # достаточно для пробы
            if progress is not None:
                await progress(total, 0)
            return (total > 0), total, "" if total > 0 else "пустой ответ"
        except Exception as exc:  # noqa: BLE001
            return False, 0, f"{type(exc).__name__}: {exc}"

    def authed_rtsp(self, uri: str) -> str:
        """Вставляет логин/пароль в rtsp://host/... → rtsp://user:pass@host/..."""
        if not uri.startswith("rtsp://"):
            return uri
        rest = uri[len("rtsp://"):]
        if "@" in rest.split("/", 1)[0]:
            return uri  # креды уже есть
        return f"rtsp://{quote(self.username, safe='')}:{quote(self.password, safe='')}@{rest}"

    async def search_activity(
        self, channel_id: int, start: dt.datetime, end: dt.datetime, *, mode: str = "all"
    ) -> list[ArchiveSegment]:
        """Отрезки с активностью на канале за окно. mode: human|motion|all.

        Поиск идёт по основному треку (аналитика/детекция привязаны к нему);
        качать найденные интервалы будем с лёгкого субпотока.
        """
        if mode == "all":
            return [ArchiveSegment(start, end)]
        descriptor = _HUMAN_DESC if mode == "human" else _MOTION_DESC
        return await self._search_segments(channel_id * 100 + 1, start, end, descriptor)

    def rtsp_playback_url(
        self, trackid: int, start: dt.datetime, end: dt.datetime, *, rtsp_port: int = 554
    ) -> str:
        user = quote(self.username, safe="")
        pwd = quote(self.password, safe="")
        st = start.strftime("%Y%m%dT%H%M%SZ")
        en = end.strftime("%Y%m%dT%H%M%SZ")
        return (
            f"rtsp://{user}:{pwd}@{self.host}:{rtsp_port}"
            f"/Streaming/tracks/{trackid}?starttime={st}&endtime={en}"
        )
