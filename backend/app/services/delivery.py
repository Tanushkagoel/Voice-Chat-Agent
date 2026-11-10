"""Deliver each revision once. The record row is unique; the inbox key blocks a second copy."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import session_factory
from app.models import InboxRow, RecordRow

log = logging.getLogger(__name__)


def idempotency_key(session_id: str, revision: int) -> str:
    return f"{session_id}:{revision}"


async def deliver_record(record_id: str) -> None:
    factory = session_factory()
    async with factory() as db:
        claimed = await db.execute(
            update(RecordRow)
            .where(RecordRow.id == record_id, RecordRow.delivery_status == "pending")
            .values(delivery_status="sending")
        )
        if claimed.rowcount == 0:
            await db.rollback()
            return
        record = await db.get(RecordRow, record_id)
        if record is None:
            await db.rollback()
            return
        key = idempotency_key(record.session_id, record.revision)
        try:
            await _dispatch(db, key, record.id, record.payload_json)
        except Exception:
            log.exception("Record delivery failed for %s", key)
            record.delivery_status = "pending"
            await db.commit()
            return
        record.delivery_status = "delivered"
        record.delivered_at = datetime.now(timezone.utc)
        await db.commit()
        log.info("Delivered claim record %s", key)


async def recover_deliveries() -> int:
    """Restart path: resume anything not marked delivered, without inserting another revision."""
    factory = session_factory()
    async with factory() as db:
        await db.execute(
            update(RecordRow)
            .where(RecordRow.delivery_status == "sending")
            .values(delivery_status="pending")
        )
        rows = (
            await db.execute(select(RecordRow.id).where(RecordRow.delivery_status == "pending"))
        ).scalars().all()
        await db.commit()
    for record_id in rows:
        await deliver_record(record_id)
    return len(rows)


async def _dispatch(db, key: str, record_id: str, payload_json: str) -> None:
    settings = get_settings()
    if settings.record_webhook_url:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                settings.record_webhook_url,
                content=payload_json.encode(),
                headers={"Content-Type": "application/json", "Idempotency-Key": key},
            )
            response.raise_for_status()
        return
    await _accept_inbox(db, key, record_id, payload_json)


async def accept_inbox(key: str, record_id: str, payload_json: str) -> bool:
    factory = session_factory()
    async with factory() as db:
        created = await _accept_inbox(db, key, record_id, payload_json)
        await db.commit()
        return created


async def _accept_inbox(db, key: str, record_id: str, payload_json: str) -> bool:
    existing = await db.get(InboxRow, key)
    if existing is not None:
        return False
    db.add(
        InboxRow(
            idempotency_key=key,
            record_id=record_id,
            payload_json=payload_json,
            created_at=datetime.now(timezone.utc),
        )
    )
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        return False
    return True
