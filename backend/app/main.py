from __future__ import annotations

import logging
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
    yield
    await stop_redis()
    await dispose_engine()


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
