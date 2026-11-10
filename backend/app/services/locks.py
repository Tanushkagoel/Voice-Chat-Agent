"""Redis lock so a call turn and a chat turn cannot merge the same claim at once."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

from app.services.events import redis_client

_RELEASE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""


class SessionBusy(Exception):
    pass


@asynccontextmanager
async def session_lock(session_id: str):
    client = redis_client()
    if client is None:
        yield
        return
    key = f"lock:{session_id}"
    token = uuid.uuid4().hex
    acquired = False
    for _ in range(20):
        acquired = bool(await client.set(key, token, nx=True, px=8000))
        if acquired:
            break
        await asyncio.sleep(0.1)
    if not acquired:
        raise SessionBusy(session_id)
    try:
        yield
    finally:
        try:
            await client.eval(_RELEASE, 1, key, token)
        except Exception:
            pass
