"""Тесты модуля «Автобусы»: замена дисков, валидации, страницы."""
import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Bus, Disk, DiskStatus, SwapLog


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _disk_status(disk_id: int) -> str:
    async with SessionLocal() as s:
        return (await s.execute(select(Disk).where(Disk.id == disk_id))).scalar_one().status


async def test_install_into_empty_and_swap(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "56", "route": "56"})).json()["id"]
        d1 = (await c.post("/api/disks", json={"label": "А-1", "assigned_bus_id": bus})).json()["id"]
        d2 = (await c.post("/api/disks", json={"label": "А-2", "assigned_bus_id": bus})).json()["id"]

        # установка в пустой регистратор
        r = await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d1, "note": "первая установка"})
        assert r.status_code == 200
        assert await _disk_status(d1) == DiskStatus.INSTALLED

        # замена d1 -> d2
        r = await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d2})
        assert r.status_code == 200
        assert await _disk_status(d1) == DiskStatus.REMOVED_REVIEW  # снятый → на просмотр
        assert await _disk_status(d2) == DiskStatus.INSTALLED

    async with SessionLocal() as s:
        b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
        assert b.installed_disk_id == d2
        logs = (await s.execute(select(SwapLog).where(SwapLog.bus_id == bus))).scalars().all()
        assert len(logs) == 2


async def test_remove_without_replacement(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "7"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Б-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        r = await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None, "note": "сняли"})
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.REMOVED_REVIEW
    async with SessionLocal() as s:
        b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
        assert b.installed_disk_id is None  # автобус без диска


async def test_validations(db):
    async with _client() as c:
        bus1 = (await c.post("/api/buses", json={"bus_number": "1"})).json()["id"]
        bus2 = (await c.post("/api/buses", json={"bus_number": "2"})).json()["id"]
        d_other = (await c.post("/api/disks", json={"label": "Ч-1", "assigned_bus_id": bus2})).json()["id"]
        # чужой диск без force → 409
        r = await c.post(f"/api/buses/{bus1}/swap", json={"installed_disk_id": d_other})
        assert r.status_code == 409
        # с force → ок
        r = await c.post(f"/api/buses/{bus1}/swap", json={"installed_disk_id": d_other, "force": True})
        assert r.status_code == 200
        # установить уже установленный → 400
        r = await c.post(f"/api/buses/{bus2}/swap", json={"installed_disk_id": d_other})
        assert r.status_code == 400


async def test_reviewed_and_faulty(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "9"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Р-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None})  # снят -> review
        r = await c.post(f"/api/disks/{d}/reviewed")
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.REVIEWED  # просмотрен, ещё у смотрящего
        # вернули на полку → готов (резерв)
        r = await c.post(f"/api/disks/{d}/to-shelf")
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.READY
        r = await c.post(f"/api/disks/{d}/faulty", json={"note": "битый"})
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.FAULTY


async def test_restore_faulty(db):
    async with _client() as c:
        d = (await c.post("/api/disks", json={"label": "Ф-1"})).json()["id"]
        await c.post(f"/api/disks/{d}/faulty", json={"note": "битый"})
        assert await _disk_status(d) == DiskStatus.FAULTY
        r = await c.post(f"/api/disks/{d}/restore")
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.READY
        # ready нельзя «вернуть в строй»
        assert (await c.post(f"/api/disks/{d}/restore")).status_code == 400


async def test_collection_page_and_csv(db):
    async with _client() as c:
        await c.post("/api/buses", json={"bus_number": "55", "route": "5"})
        assert (await c.get("/buses/collection")).status_code == 200
        r = await c.get("/api/buses/collection.csv")
        assert r.status_code == 200 and "text/csv" in r.headers["content-type"]


async def test_swap_reserve_pair(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "21"})).json()["id"]
        d1 = (await c.post("/api/disks", json={"label": "П-1", "assigned_bus_id": bus})).json()["id"]
        d2 = (await c.post("/api/disks", json={"label": "П-2", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d1})  # d1 стоит, d2 резерв
        r = await c.post(f"/api/buses/{bus}/swap-reserve")                       # пара в один тап
        assert r.status_code == 200 and r.json()["installed"] == "П-2"
        assert await _disk_status(d2) == DiskStatus.INSTALLED
        assert await _disk_status(d1) == DiskStatus.REMOVED_REVIEW
        # резерва больше нет → 400
        assert (await c.post(f"/api/buses/{bus}/swap-reserve")).status_code == 400


async def test_disk_review_and_stats(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "30", "route": "7"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Р-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None})  # снят → review
        # запись наблюдения с проблемой + пометить просмотренным
        r = await c.post(f"/api/disks/{d}/review", json={"tags": ["нет записи"], "note": "канал 3", "finish": True})
        assert r.status_code == 200
        assert await _disk_status(d) == DiskStatus.REVIEWED  # просмотрен, у смотрящего
        # статистика видит проблемный автобус
        stats = (await c.get("/buses/stats")).text
        assert "нет записи" in stats and "30" in stats


async def test_review_and_dup_pages(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "40"})).json()["id"]
        await c.post("/api/disks", json={"label": "ДУБ", "assigned_bus_id": bus})
        await c.post("/api/disks", json={"label": "дуб", "assigned_bus_id": bus})  # дубль (регистр)
        assert (await c.get("/buses/review")).status_code == 200
        page = (await c.get("/disks")).text
        assert "Дубли меток" in page
        assert (await c.get("/disks?q=ДУБ")).status_code == 200


async def test_location_auto_set(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "12"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Л-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
    async with SessionLocal() as s:
        disk = (await s.execute(select(Disk).where(Disk.id == d))).scalar_one()
        assert disk.location == "in_bus"  # установлен → в автобусе
    async with _client() as c:
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None})  # снят
    async with SessionLocal() as s:
        disk = (await s.execute(select(Disk).where(Disk.id == d))).scalar_one()
        assert disk.location == "reviewer"  # снят → у смотрящего


async def test_audit_and_passport(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "13"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "А-7", "assigned_bus_id": bus})).json()["id"]
        r = await c.post("/api/disks/audit", json={"present_ids": [d]})
        assert r.status_code == 200 and r.json()["confirmed"] == 1
        assert (await c.get("/disks/audit")).status_code == 200
        assert (await c.get(f"/disks/{d}/passport")).status_code == 200


async def test_plan_and_today(db):
    async with _client() as c:
        import datetime as dt
        bus = (await c.post("/api/buses", json={"bus_number": "14", "route": "5"})).json()["id"]
        wd = dt.datetime.now().weekday()
        await c.put(f"/api/buses/{bus}", json={"collect_weekday": wd})
        plan = (await c.get("/buses/plan")).text
        assert "Маршрут 5" in plan and "Назначить всему маршруту" in plan  # группировка + массовое назначение
        page = (await c.get("/buses/collection?today=1")).text
        assert "14" in page  # автобус с сегодняшним днём сбора попал в список


async def test_pages_render(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "100", "route": "5"})).json()["id"]
        disk = (await c.post("/api/disks", json={"label": "Д-9", "assigned_bus_id": bus})).json()["id"]
        for path in ["/buses", "/buses?sort=route", "/buses?sort=number",
                     f"/buses/{bus}", "/disks", "/buses/swaplog",
                     f"/buses/swaplog?disk_id={disk}", "/api/buses/swaplog.csv"]:
            r = await c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"


async def test_settings_and_grouping(db):
    async with _client() as c:
        await c.post("/api/buses", json={"bus_number": "А1", "route": "5"})
        await c.post("/api/buses", json={"bus_number": "А2", "route": "5"})
        await c.post("/api/buses", json={"bus_number": "Б1", "route": "10"})
        # пороги сохраняются через API (форма убрана из UI вместе с ротацией)
        r = await c.post("/api/buses/settings", json={"swap_days": 21, "review_days": 3})
        assert r.status_code == 200
        assert (await c.get("/buses")).status_code == 200
        # группировка по маршрутам
        grouped = (await c.get("/buses?sort=group")).text
        assert "Маршрут 5" in grouped and "Маршрут 10" in grouped


async def test_swap_alert_opt_in(db):
    """Напоминание «пора менять» выключено по умолчанию и включается по автобусу."""
    import datetime as dt

    async with _client() as c:
        await c.post("/api/buses/settings", json={"swap_days": 14, "review_days": 7})
        bus = (await c.post("/api/buses", json={"bus_number": "Т1"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "Т-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})

        # состарим установку так, чтобы порог был превышен
        async with SessionLocal() as s:
            b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
            b.installed_since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)
            await s.commit()

        # по умолчанию напоминание выключено — оранжевого статуса нет
        info = next(x for x in (await c.get("/api/buses")).json() if x["id"] == bus)
        assert info["swap_alert_enabled"] is False
        assert info["color"] != "orange"

        # включаем напоминание для этого автобуса
        r = await c.put(f"/api/buses/{bus}", json={"swap_alert_enabled": True})
        assert r.status_code == 200
        info = next(x for x in (await c.get("/api/buses")).json() if x["id"] == bus)
        assert info["swap_alert_enabled"] is True
        assert info["color"] == "orange" and "пора менять" in info["reason"]

        # выключаем — снова тихо
        await c.put(f"/api/buses/{bus}", json={"swap_alert_enabled": False})
        info = next(x for x in (await c.get("/api/buses")).json() if x["id"] == bus)
        assert info["color"] != "orange"


async def test_route_sort_order(db):
    async with _client() as c:
        await c.post("/api/buses", json={"bus_number": "А1", "route": "10"})
        await c.post("/api/buses", json={"bus_number": "А2", "route": "5"})
        r = await c.get("/buses?sort=route")
        body = r.text
        # маршрут 5 должен идти раньше маршрута 10 (натуральная сортировка)
        assert body.index("Маршрут 5") < body.index("Маршрут 10")


async def test_delete_disk(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "77", "route": "77"})).json()["id"]
        # обычный диск (резерв) удаляется
        d = (await c.post("/api/disks", json={"label": "У-1"})).json()["id"]
        assert (await c.delete(f"/api/disks/{d}")).status_code == 200
        async with SessionLocal() as s:
            assert (await s.execute(select(Disk).where(Disk.id == d))).scalar_one_or_none() is None
        # установленный диск тоже можно удалить — автобус освобождается от ссылки
        d2 = (await c.post("/api/disks", json={"label": "У-2", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d2})
        assert await _disk_status(d2) == DiskStatus.INSTALLED
        assert (await c.delete(f"/api/disks/{d2}")).status_code == 200
        async with SessionLocal() as s:
            assert (await s.execute(select(Disk).where(Disk.id == d2))).scalar_one_or_none() is None
            b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
            assert b.installed_disk_id is None  # автобус без висячей ссылки
        # несуществующий — 404
        assert (await c.delete("/api/disks/999999")).status_code == 404


async def test_delete_bus_frees_disks(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "88"})).json()["id"]
        inst = (await c.post("/api/disks", json={"label": "В-1", "assigned_bus_id": bus})).json()["id"]
        res = (await c.post("/api/disks", json={"label": "В-2", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": inst})
        assert await _disk_status(inst) == DiskStatus.INSTALLED
        # удаляем автобус — закреплённые за ним диски освобождаются
        assert (await c.delete(f"/api/buses/{bus}")).status_code == 200
    async with SessionLocal() as s:
        installed = (await s.execute(select(Disk).where(Disk.id == inst))).scalar_one()
        reserve = (await s.execute(select(Disk).where(Disk.id == res))).scalar_one()
        assert installed.status == DiskStatus.READY      # не остался "установлен"
        assert installed.location == "shelf"             # и не "в автобусе"
        assert installed.assigned_bus_id is None
        assert reserve.assigned_bus_id is None


async def test_installed_location_locked(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "99"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "З-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        # место установленного диска руками не сменить
        r = await c.put(f"/api/disks/{d}", json={"location": "shelf"})
        assert r.status_code == 400
    async with SessionLocal() as s:
        assert (await s.execute(select(Disk).where(Disk.id == d))).scalar_one().location == "in_bus"


async def test_disk_action_buttons_well_formed(db):
    """Регрессия: строковый аргумент в onclick (метка/заметка) не должен рвать
    HTML-атрибут. tojson не экранирует двойные кавычки, поэтому onclick обёрнут
    в одинарные кавычки."""
    async with _client() as c:
        await c.post("/api/disks", json={"label": 'A"B', "status": "faulty"})
        html = (await c.get("/disks")).text
    assert "onclick='delDisk(" in html and "onclick='editNote(" in html
    assert 'onclick="delDisk(' not in html and 'onclick="editNote(' not in html


async def test_reserve_assignment_and_reasons(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "200"})).json()["id"]
        other = (await c.post("/api/buses", json={"bus_number": "201"})).json()["id"]
        main = (await c.post("/api/disks", json={"label": "ОСН", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": main})

        async def reason():
            return next(b["reason"] for b in (await c.get("/api/buses")).json() if b["id"] == bus)

        # нет закреплённого резерва — честный текст (не "второй диск на просмотре")
        assert await reason() == "нет закреплённого резерва"

        # закрепляем готовый резерв за автобусом → всё ок
        res = (await c.post("/api/disks", json={"label": "РЕЗ", "status": "ready"})).json()["id"]
        assert (await c.put(f"/api/disks/{res}", json={"assigned_bus_id": bus})).status_code == 200
        assert await reason() == "всё ок"

        # установленный диск нельзя перезакрепить за другим автобусом
        assert (await c.put(f"/api/disks/{main}", json={"assigned_bus_id": other})).status_code == 400

        # после замены снятый диск реально на просмотре → текст про просмотр корректен
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": res})  # main->review, res главный
        assert "просмотре" in await reason()


async def test_location_status_sync(db):
    async with _client() as c:
        d = (await c.post("/api/disks", json={"label": "С-1", "status": "ready"})).json()["id"]
        # «у смотрящего» → статус «на просмотре»
        assert (await c.put(f"/api/disks/{d}", json={"location": "reviewer"})).status_code == 200
        assert await _disk_status(d) == DiskStatus.REMOVED_REVIEW
        # «на полке» → снова «готов»
        assert (await c.put(f"/api/disks/{d}", json={"location": "shelf"})).status_code == 200
        assert await _disk_status(d) == DiskStatus.READY
        # «в пути» → тоже «на просмотре» (диск в цепочке просмотра)
        assert (await c.put(f"/api/disks/{d}", json={"location": "transit"})).status_code == 200
        assert await _disk_status(d) == DiskStatus.REMOVED_REVIEW
        # «в автобусе» руками нельзя
        assert (await c.put(f"/api/disks/{d}", json={"location": "in_bus"})).status_code == 400


async def test_stats_reset(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "300"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "СТ", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        page = (await c.get("/buses/stats")).text
        assert "Кто делал замены" in page and "Сейчас по парку" in page  # новые секции
        r = await c.post("/api/buses/stats/reset")
        assert r.status_code == 200 and r.json()["removed"]["swaps"] >= 1
    async with SessionLocal() as s:
        assert (await s.execute(select(SwapLog))).scalars().all() == []


async def test_not_collected(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "400"})).json()["id"]
        r = await c.post(f"/api/buses/{bus}/not-collected", json={"reason": "автобус не найден"})
        assert r.status_code == 200
        # пустая причина → 400
        assert (await c.post(f"/api/buses/{bus}/not-collected", json={"reason": "  "})).status_code == 400
        # на странице сбора видна отметка и список причин
        page = (await c.get("/buses/collection")).text
        assert "не собрали: автобус не найден" in page
        assert "Не собрали" in page
    async with SessionLocal() as s:
        logs = (await s.execute(select(SwapLog).where(SwapLog.bus_id == bus))).scalars().all()
        assert len(logs) == 1
        assert logs[0].removed_disk_id is None and logs[0].installed_disk_id is None


async def test_review_to_shelf_flow(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "501"})).json()["id"]
        d = (await c.post("/api/disks", json={"label": "ПР-1", "assigned_bus_id": bus})).json()["id"]
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": d})
        await c.post(f"/api/buses/{bus}/swap", json={"installed_disk_id": None})  # снят → на просмотр
        assert await _disk_status(d) == DiskStatus.REMOVED_REVIEW
        # просмотрели → остаётся у смотрящего, место НЕ меняется
        assert (await c.post(f"/api/disks/{d}/reviewed")).status_code == 200
        assert await _disk_status(d) == DiskStatus.REVIEWED
        async with SessionLocal() as s:
            assert (await s.execute(select(Disk).where(Disk.id == d))).scalar_one().location == "reviewer"
        # очередь показывает раздел «у смотрящего»
        assert "у смотрящего" in (await c.get("/buses/review")).text
        # вернули на полку → готов + место «на полке»
        assert (await c.post(f"/api/disks/{d}/to-shelf")).status_code == 200
        async with SessionLocal() as s:
            disk = (await s.execute(select(Disk).where(Disk.id == d))).scalar_one()
            assert disk.status == DiskStatus.READY and disk.location == "shelf"


async def test_bus_location(db):
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "L1", "route": "9"})).json()["id"]
        r = await c.put(f"/api/buses/{bus}", json={"location": "Парковка №3, бокс 12"})
        assert r.status_code == 200
        page = (await c.get(f"/buses/{bus}")).text
        assert "Где стоит" in page and "Парковка №3, бокс 12" in page
    async with SessionLocal() as s:
        b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
        assert b.location == "Парковка №3, бокс 12"


async def test_bus_page_mixed_ready_disks(db):
    """Регрессия: страница автобуса не должна падать, когда среди готовых дисков
    есть и закреплённые (assigned_bus_id=int), и незакреплённые (None)."""
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "22T", "route": "14"})).json()["id"]
        await c.post("/api/disks", json={"label": "R1", "status": "ready", "assigned_bus_id": bus})
        await c.post("/api/disks", json={"label": "R2", "status": "ready"})  # assigned_bus_id=None
        r = await c.get(f"/buses/{bus}")
        assert r.status_code == 200
        assert "R1" in r.text and "R2" in r.text


async def test_disk_bus_suggestion(db):
    """Незакреплённому диску предлагается автобус по метке (одним кликом, не насильно)."""
    async with _client() as c:
        await c.post("/api/buses", json={"bus_number": "AB396"})
        await c.post("/api/disks", json={"label": "AB396 (Резерв)", "status": "ready"})  # не закреплён
        page = (await c.get("/disks")).text
        assert "↳ AB396?" in page and "assignSuggested" in page
        # метка без совпадения — подсказки нет
        await c.post("/api/disks", json={"label": "ZZZ-1", "status": "ready"})
        assert "↳ ZZZ" not in (await c.get("/disks")).text


async def test_bus_problems_report_and_csv(db):
    """Отмеченные проблемы попадают в печатную сводку и CSV-выгрузку."""
    async with _client() as c:
        b1 = (await c.post("/api/buses", json={"bus_number": "П-1", "route": "5"})).json()["id"]
        b2 = (await c.post("/api/buses", json={"bus_number": "П-2", "route": "5"})).json()["id"]
        r = await c.put(f"/api/buses/{b1}", json={"has_problem": True, "problem_note": "не пишет звук"})
        assert r.status_code == 200

        page = await c.get("/buses/problems")
        assert page.status_code == 200
        assert "П-1" in page.text and "не пишет звук" in page.text
        assert "П-2" not in page.text  # без проблемы — не в списке

        csv_r = await c.get("/api/buses/problems.csv")
        assert csv_r.status_code == 200
        assert "П-1" in csv_r.text and "не пишет звук" in csv_r.text
        assert "attachment" in csv_r.headers["content-disposition"]

        # проблему сняли — сводка пустеет
        await c.put(f"/api/buses/{b1}", json={"has_problem": False, "problem_note": None})
        page = await c.get("/buses/problems")
        assert "Проблем нет" in page.text


async def test_bus_problem_history(db):
    """Пометки проблем копятся в истории: открытие, смена формулировки, снятие."""
    from app.models import BusProblemLog

    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "И-1", "route": "9"})).json()["id"]
        # отметили проблему → открылась запись
        await c.put(f"/api/buses/{bus}", json={"has_problem": True, "problem_note": "шумит вентилятор"})
        # сменили формулировку → старая закрылась, открылась новая
        await c.put(f"/api/buses/{bus}", json={"has_problem": True, "problem_note": "не пишет канал 3"})
        # сняли проблему → запись закрылась
        await c.put(f"/api/buses/{bus}", json={"has_problem": False, "problem_note": None})

    async with SessionLocal() as s:
        log = (await s.execute(select(BusProblemLog).where(BusProblemLog.bus_id == bus)
                               .order_by(BusProblemLog.id))).scalars().all()
        assert len(log) == 2
        assert log[0].note == "шумит вентилятор" and log[0].closed_at is not None
        assert log[1].note == "не пишет канал 3" and log[1].closed_at is not None

    async with _client() as c:
        # история видна на странице автобуса
        page = await c.get(f"/buses/{bus}")
        assert "История проблем" in page.text
        assert "шумит вентилятор" in page.text and "не пишет канал 3" in page.text

        # повторная пометка: в сводке счётчик «Раз» учитывает всю историю
        await c.put(f"/api/buses/{bus}", json={"has_problem": True, "problem_note": "опять канал 3"})
        csv_r = await c.get("/api/buses/problems.csv")
        row = next(line for line in csv_r.text.splitlines() if "И-1" in line)
        assert row.rstrip().endswith(";3")


async def test_bus_problem_backfill(db):
    """Проблемы, отмеченные до появления истории, подхватываются бэкфиллом при старте."""
    from app.database import _backfill_bus_problem_log
    from app.models import BusProblemLog

    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "Б-1"})).json()["id"]
    # имитируем «старую» пометку — напрямую в БД, мимо API (история не писалась)
    async with SessionLocal() as s:
        b = (await s.execute(select(Bus).where(Bus.id == bus))).scalar_one()
        b.has_problem = True
        b.problem_note = "записи не полные"
        await s.commit()

    await _backfill_bus_problem_log()
    await _backfill_bus_problem_log()  # повторный запуск дублей не плодит

    async with SessionLocal() as s:
        log = (await s.execute(select(BusProblemLog).where(BusProblemLog.bus_id == bus))).scalars().all()
        assert len(log) == 1
        assert log[0].note == "записи не полные" and log[0].closed_at is None

    async with _client() as c:
        page = await c.get(f"/buses/{bus}")
        assert "записи не полные" in page.text and "История проблем" in page.text


async def test_bus_chronic_block(db):
    """Автобус с 2+ пометками попадает в блок «Чаще всего болеют»."""
    async with _client() as c:
        b1 = (await c.post("/api/buses", json={"bus_number": "Х-1", "route": "3"})).json()["id"]
        b2 = (await c.post("/api/buses", json={"bus_number": "Х-2", "route": "3"})).json()["id"]
        # Х-1: дважды отмечали и снимали; Х-2 — один раз
        for note in ("не пишет", "снова не пишет"):
            await c.put(f"/api/buses/{b1}", json={"has_problem": True, "problem_note": note})
            await c.put(f"/api/buses/{b1}", json={"has_problem": False, "problem_note": None})
        await c.put(f"/api/buses/{b2}", json={"has_problem": True, "problem_note": "разово"})

        page = (await c.get("/buses/problems")).text
        assert "Чаще всего болеют" in page and "Х-1" in page
        # Х-2 с одной пометкой в хронику не попадает (но есть в основном списке)
        chronic_part = page.split("Чаще всего болеют")[1]
        assert "Х-2" not in chronic_part


async def test_disk_qr_and_labels(db):
    """QR-наклейки дисков: png-код и страница печати; ревизия содержит сканер."""
    async with _client() as c:
        d = (await c.post("/api/disks", json={"label": "QR-1", "type": "SSD"})).json()["id"]

        qr = await c.get(f"/disks/{d}/qr.png")
        assert qr.status_code == 200 and qr.headers["content-type"] == "image/png"
        assert (await c.get("/disks/99999/qr.png")).status_code == 404

        page = (await c.get("/disks/labels")).text
        assert "QR-наклейки на диски" in page and "QR-1" in page

        audit = (await c.get("/disks/audit")).text
        assert "Сканировать" in audit and "Быстрый ввод" in audit


async def test_buses_simplified_cards_and_analytics(db):
    """Упрощённые карточки: закреплённые диски и проблема на карточке; аналитика."""
    async with _client() as c:
        bus = (await c.post("/api/buses", json={"bus_number": "УК-1", "route": "7"})).json()["id"]
        await c.post("/api/disks", json={"label": "УК-1 (SSD)", "type": "SSD", "assigned_bus_id": bus})
        await c.put(f"/api/buses/{bus}", json={"has_problem": True, "problem_note": "не пишет канал 2"})

        page = (await c.get("/buses")).text
        # диск-чип и текст проблемы прямо на карточке, кнопки отметки/снятия
        assert "УК-1 (SSD)" in page and "не пишет канал 2" in page
        assert "снять проблему" in page and "markProblem" in page

        # страница автобуса: закрепление свободного диска на месте
        free = (await c.post("/api/disks", json={"label": "СВ-1", "type": "SSD"})).json()["id"]
        bpage = (await c.get(f"/buses/{bus}")).text
        assert "Закрепить за автобусом" in bpage and "СВ-1" in bpage
        r = await c.put(f"/api/disks/{free}", json={"assigned_bus_id": bus})
        assert r.status_code == 200
        assert "СВ-1" in (await c.get("/buses")).text  # чип появился на карточке

        # аналитика: маршрут и месяц посчитаны
        a = (await c.get("/buses/analytics")).text
        assert "Аналитика проблем" in a and "марш. 7" in a and "По месяцам" in a
