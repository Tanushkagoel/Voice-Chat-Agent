from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import get_settings
from app.db import dispose_engine, get_engine, init_engine
from app.models import Base
from app.services.delivery import recover_deliveries
from app.services.events import start_redis, stop_redis
from app.services.turns import TurnService

log = logging.getLogger(__name__)


def ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw = database_url[len(prefix) :]
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    ensure_sqlite_parent(settings.database_url)
    init_engine(settings.database_url)
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app.state.turns = TurnService(settings)
    await start_redis(settings.redis_url)
    try:
        await recover_deliveries()
    except Exception:
        log.exception("Delivery recovery failed")
    voice_task = _start_voice_worker(settings)
    try:
        yield
    finally:
        if voice_task is not None:
            voice_task.cancel()
            try:
                await voice_task
            except asyncio.CancelledError:
                pass
        await stop_redis()
        await dispose_engine()


def _start_voice_worker(settings) -> asyncio.Task | None:
    """Run the call listener in this process so a free web instance needs no paid worker."""
    if os.environ.get("RUN_VOICE_WORKER") != "1":
        return None
    if not settings.redis_url or not settings.livekit_url:
        log.warning("Voice worker not started: REDIS_URL and LIVEKIT_URL are required")
        return None
    port = os.environ.get("PORT")
    if port:
        settings.api_base_url = f"http://127.0.0.1:{port}"
    from app.voice.worker import serve

    log.info("Starting the voice worker inside the web process")
    return asyncio.create_task(serve())


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Alder Claims", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    static_dir = Path(settings.static_dir) if settings.static_dir else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if static_dir.exists():
        app.mount("/", _SPAStaticFiles(directory=static_dir, html=True), name="web")
    return app


class _SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException:
            return await super().get_response("index.html", scope)


app = create_app()
