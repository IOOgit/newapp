"""Точка входа FastAPI: БД, планировщик, роуты, дашборд, (опц.) mock-сервер."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import (
    auth, backup_api, buses, checkin, dashboard, devices, monitoring, plan, users_api,
    worklist, switches, timesync,
)
from app.config import settings
from app.database import init_db
from app.scheduler import configure_checkin_job, shutdown_scheduler, start_scheduler
from app.services import bot

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nvrmon")

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from app.services import checkin_ingest
    await checkin_ingest.abort_orphan_runs()  # чистим зомби-прогоны после рестарта
    await checkin_ingest.load_clips_dir()      # папка клипов из настроек
    start_scheduler()
    await configure_checkin_job()
    from app.scheduler import configure_time_sync_job
    await configure_time_sync_job()
    bot_task = asyncio.create_task(bot.run_bot())
    log.info("NVR Monitor запущен (mock_mode=%s)", settings.mock_mode)
    try:
        yield
    finally:
        shutdown_scheduler()
        bot_task.cancel()


app = FastAPI(title="NVR Monitor", version="0.1.0", lifespan=lifespan)

# Открытые без авторизации пути (статика, страница входа, проверки, mock)
_PUBLIC_PREFIXES = ("/static", "/login", "/logout", "/healthz", "/metrics", "/mock", "/docs", "/openapi.json", "/sw.js", "/offline", "/manifest.webmanifest")


# Тестовый seam: conftest подставляет сюда сессию владельца, чтобы существующие
# эндпоинт-тесты шли авторизованными. В проде ВСЕГДА None — боевой вход не трогается.
TEST_SESSION_OVERRIDE: dict | None = None


@app.middleware("http")
async def require_login(request: Request, call_next):
    from app.database import SessionLocal
    from app.permissions import path_allowed, start_page
    from app.services import users as users_svc

    if TEST_SESSION_OVERRIDE is not None:
        request.session.update(TEST_SESSION_OVERRIDE)

    path = request.url.path
    is_api = path.startswith("/api")
    if path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    if not request.session.get("auth"):
        if is_api:
            return JSONResponse({"detail": "Требуется вход"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    # Актуализируем права из БД каждый запрос: изменения владельца применяются
    # мгновенно, отключённый пользователь тут же вылетает.
    user_id = request.session.get("user_id")
    async with SessionLocal() as s:
        user = await users_svc.get_user(s, user_id) if user_id else None
    if user is None or not user.enabled:
        request.session.clear()
        if is_api:
            return JSONResponse({"detail": "Сессия недействительна"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    request.session["is_owner"] = bool(user.is_owner)
    request.session["caps"] = list(user.permissions or [])
    request.session["user"] = user.username

    if path == "/no-access":
        return await call_next(request)
    if not path_allowed(path, request.session["caps"], request.session["is_owner"]):
        if is_api:
            return JSONResponse({"detail": "Недостаточно прав"}, status_code=403)
        return RedirectResponse(start_page(user.is_owner, user.permissions or []) or "/no-access", status_code=303)
    return await call_next(request)


# SessionMiddleware добавляется ПОСЛЕ — значит он внешний и обрабатывает запрос
# первым, наполняя request.session до проверки require_login.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key or secrets.token_hex(32),
    session_cookie="nvrmon_session",
    max_age=60 * 60 * 24 * 7,
)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(monitoring.router)
app.include_router(timesync.router)
app.include_router(switches.router)
app.include_router(worklist.router)
app.include_router(plan.router)
app.include_router(buses.router)
app.include_router(checkin.router)
app.include_router(users_api.router)
app.include_router(backup_api.router)
app.include_router(dashboard.router)

# Статика
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Клипы модуля «Заселения» отдаёт динамический роут checkin.serve_clip
# (папка настраивается из UI без рестарта; Range поддерживается FileResponse).

# Встроенный mock-сервер NVR (только для разработки)
if settings.mock_mode:
    from mock.server import mock_app

    app.mount("/mock", mock_app)
    log.warning("MOCK_MODE включён: эмулятор NVR доступен на /mock")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/sw.js")
async def service_worker():
    from fastapi.responses import FileResponse

    return FileResponse(
        str(static_dir / "sw.js"), media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest")
async def web_manifest():
    from fastapi.responses import FileResponse

    return FileResponse(
        str(static_dir / "manifest.webmanifest"), media_type="application/manifest+json"
    )


@app.get("/offline")
async def offline_page():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Нет сети</title><link rel='stylesheet' href='/static/style.css'></head>"
        "<body style='display:flex;align-items:center;justify-content:center;height:100vh;text-align:center'>"
        "<div><h1>📴 Нет связи</h1><p class='muted'>Приложению нужен интернет/сеть до сервера.<br>"
        "Проверь подключение и потяни страницу вниз для обновления.</p></div></body></html>"
    )
