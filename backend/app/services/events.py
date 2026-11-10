"""In-process event fanout, with Redis when several API workers are running."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)
INSTANCE_ID = uuid.uuid4().hex

_queues: dict[str, set[asyncio.Queue]] = {}
_redis = None
_listener: asyncio.Task | None = None


def subscribe(session_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _queues.setdefault(session_id, set()).add(queue)
    return queue


def unsubscribe(session_id: str, queue: asyncio.Queue) -> None:
    peers = _queues.get(session_id)
    if not peers:
        return
    peers.discard(queue)
    if not peers:
        _queues.pop(session_id, None)


def _fanout(session_id: str, payload: dict) -> None:
    for queue in list(_queues.get(session_id, set())):
        queue.put_nowait(payload)


def redis_client():
    return _redis


async def publish_voice_control(event: dict[str, Any]) -> None:
    """Tell the LiveKit worker to join or leave a call. Redis is the signal."""
    if _redis is None:
        log.warning("Redis is not connected, so the LiveKit worker will not join this call")
        return
    try:
        await _redis.publish("voice:control", json.dumps(event))
    except Exception:
        log.exception("Could not publish voice control")


async def publish(session_id: str, event: dict[str, Any]) -> None:
    payload = {**event, "_origin": INSTANCE_ID}
    _fanout(session_id, payload)
    if _redis is None:
        return
    try:
        await _redis.publish(f"session:{session_id}", json.dumps(payload, default=str))
    except Exception:
        log.exception("Redis publish failed for %s", session_id)


async def start_redis(url: str | None) -> None:
    global _redis, _listener
    if not url:
        return
    try:
        import redis.asyncio as redis

        client = redis.from_url(url)
        await client.ping()
    except Exception:
        log.warning("Redis is not reachable; live updates stay in this process")
        return
    _redis = client
    _listener = asyncio.create_task(_listen())


async def stop_redis() -> None:
    global _redis, _listener
    if _listener is not None:
        _listener.cancel()
        _listener = None
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def _listen() -> None:
    assert _redis is not None
    pubsub = _redis.pubsub()
    await pubsub.psubscribe("session:*")
    try:
        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                continue
            raw = message["data"]
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            if data.get("_origin") == INSTANCE_ID:
                continue
            channel = message["channel"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            session_id = channel.split(":", 1)[1]
            _fanout(session_id, data)
    except asyncio.CancelledError:
        await pubsub.aclose()
        raise
