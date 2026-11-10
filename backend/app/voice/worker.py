"""Joins a LiveKit room when Redis says a call started."""

from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as redis

from app.config import get_settings
from app.voice.room import run_room

log = logging.getLogger(__name__)


async def serve() -> None:
    settings = get_settings()
    if not settings.redis_url or not settings.livekit_url:
        raise SystemExit("The voice worker needs REDIS_URL and LIVEKIT_URL.")
    logging.basicConfig(level=logging.INFO)
    client = redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    await pubsub.subscribe("voice:control")
    log.info("LiveKit worker listening for calls")
    rooms: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not message or message.get("type") != "message":
                await asyncio.sleep(0.05)
                continue
            raw = message["data"]
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            session_id = str(data.get("sessionId") or "")
            action = data.get("action")
            if not session_id:
                continue
            if action == "start" and session_id not in rooms:
                stop = asyncio.Event()
                task = asyncio.create_task(run_room(session_id, data.get("opening"), stop))
                rooms[session_id] = (task, stop)
                log.info("Joined call %s", session_id)
            elif action == "stop" and session_id in rooms:
                task, stop = rooms.pop(session_id)
                stop.set()
                task.cancel()
                log.info("Left call %s", session_id)
    finally:
        await pubsub.aclose()
        await client.aclose()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
