"""Load a session, run the shared graph, and persist the result."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import build_graph
from app.clock import today
from app.config import Settings
from app.domain.fields import input_for, missing_keys, next_question_key
from app.domain.record import build_record
from app.models import FieldRow, RecordRow, SessionRow, TurnRow

log = logging.getLogger(__name__)

GREETING = (
    "This is Alder claims. I’ll take what happened on the call, then the policy number, "
    "registration, email, and damage photos in chat. What happened to the vehicle?"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TurnService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.graph = build_graph(settings)

    async def create_session(self, db: AsyncSession) -> dict:
        session = SessionRow(
            id=new_id("session"),
            created_at=utcnow(),
            updated_at=utcnow(),
            call_active=False,
            optional_offered=False,
            dirty=False,
            latest_revision=0,
        )
        db.add(session)
        db.add(
            TurnRow(
                session_id=session.id,
                channel="both",
                role="assistant",
                content=GREETING,
                created_at=utcnow(),
            )
        )
        await db.flush()
        return await self._payload(db, session, {}, input_for("incidentDescription", None))

    async def get_session(self, db: AsyncSession, session_id: str) -> dict:
        session = await self._lock(db, session_id)
        fields = await self._load_fields(db, session.id)
        return await self._payload(db, session, fields, self._input_for_state(session, fields))

    async def set_call(self, db: AsyncSession, session_id: str, active: bool) -> dict:
        session = await self._lock(db, session_id)
        session.call_active = active
        session.updated_at = utcnow()
        fields = await self._load_fields(db, session.id)
        if active:
            content = "Call connected. " + self._prompt(session, fields, "call")
        else:
            content = "Call ended. Nothing was lost. You can continue in chat, or start the call again."
        db.add(
            TurnRow(
                session_id=session.id,
                channel="call",
                role="assistant",
                content=content,
                created_at=utcnow(),
            )
        )
        await db.flush()
        payload = await self._payload(db, session, fields, self._input_for_state(session, fields))
        payload["speak"] = content
        return payload

    async def handle_turn(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        message: str,
        channel: str,
        attachments: list[dict] | None = None,
        attachment_error: str | None = None,
        skip_field: str | None = None,
        force_intent: str | None = None,
    ) -> tuple[dict, str | None]:
        started = time.perf_counter()
        session = await self._lock(db, session_id)
        fields = await self._load_fields(db, session.id)
        pending = json.loads(session.pending_json) if session.pending_json else None
        expecting = None
        if session.pending_action == "resolve_conflict" and pending:
            expecting = pending.get("field")
        else:
            expecting = next_question_key(fields, session.optional_offered)

        result = await self.graph.ainvoke(
            {
                "message": message,
                "channel": channel,
                "today": today().isoformat(),
                "expecting": expecting,
                "fields": fields,
                "pending_action": session.pending_action,
                "pending": pending,
                "optional_offered": session.optional_offered,
                "call_active": session.call_active,
                "attachments": attachments or [],
                "attachment_error": attachment_error,
                "skip_field": skip_field,
                "force_intent": force_intent,
                "revision": session.latest_revision,
                "dirty": session.dirty,
                "latest_outcome": session.latest_outcome,
            }
        )
        fields_out = result.get("fields_out") or fields
        await self._save_fields(db, session.id, fields_out)
        if result.get("set_pending"):
            session.pending_action = result.get("pending_action_out")
            pending_out = result.get("pending_out")
            session.pending_json = json.dumps(pending_out) if pending_out else None
        if result.get("optional_offered_out"):
            session.optional_offered = True

        user_content = message.strip()
        if attachments:
            names = ", ".join(item["name"] for item in attachments)
            upload_line = f"Uploaded {len(attachments)} photo." if len(attachments) == 1 else f"Uploaded {len(attachments)} photos: {names}."
            user_content = f"{user_content} {upload_line}".strip() if user_content else upload_line
        if skip_field and not user_content:
            user_content = "Skip the contact preference."
        if force_intent == "confirm" and not user_content:
            user_content = "Submit the claim."
        if force_intent == "finish_later" and not user_content:
            user_content = "I’ll finish later."
        if force_intent == "opt_out" and not user_content:
            user_content = "I don’t want to continue."

        latency_ms = int((time.perf_counter() - started) * 1000)
        db.add(
            TurnRow(
                session_id=session.id,
                channel=channel,
                role="user",
                content=user_content or "(empty)",
                created_at=utcnow(),
            )
        )
        db.add(
            TurnRow(
                session_id=session.id,
                channel=channel,
                role="assistant",
                content=result.get("reply") or "",
                latency_ms=latency_ms,
                prompt_tokens=result.get("prompt_tokens") or 0,
                completion_tokens=result.get("completion_tokens") or 0,
                created_at=utcnow(),
            )
        )
        voice_notice = result.get("voice_notice")
        if voice_notice and channel == "chat" and session.call_active:
            db.add(
                TurnRow(
                    session_id=session.id,
                    channel="call",
                    role="assistant",
                    content=voice_notice,
                    created_at=utcnow(),
                )
            )

        record_id = None
        if result.get("finalize"):
            revision = session.latest_revision + 1
            payload = build_record(session.id, revision, result["finalize"], fields_out)
            record = RecordRow(
                id=new_id("record"),
                session_id=session.id,
                revision=revision,
                outcome=result["finalize"],
                payload_json=json.dumps(payload),
                delivery_status="pending",
                created_at=utcnow(),
            )
            db.add(record)
            session.latest_revision = revision
            session.latest_outcome = result["finalize"]
            session.dirty = False
            await db.flush()
            record_id = record.id
        elif result.get("changes"):
            session.dirty = True

        session.updated_at = utcnow()
        await db.flush()
        payload = await self._payload(
            db,
            session,
            fields_out,
            result.get("input") or self._input_for_state(session, fields_out),
        )
        payload["reply"] = result.get("reply")
        payload["voiceNotice"] = voice_notice
        payload["latencyMs"] = latency_ms
        return payload, record_id

    async def list_records(self, db: AsyncSession, session_id: str) -> list[dict]:
        await self._lock(db, session_id)
        rows = (
            await db.execute(
                select(RecordRow)
                .where(RecordRow.session_id == session_id)
                .order_by(RecordRow.revision)
            )
        ).scalars()
        return [json.loads(row.payload_json) for row in rows]

    def _prompt(self, session: SessionRow, fields: dict, channel: str) -> str:
        from app.domain.fields import question_for

        if session.pending_action == "confirm_submit":
            return "I have the required details. Submit the claim when you’re ready."
        if session.pending_action == "confirm_review":
            return "Some of this needs a reviewer. You can submit it for review, or correct it."
        key = next_question_key(fields, session.optional_offered)
        if key is None:
            return "Tell me what you want to change."
        return question_for(key, channel)  # type: ignore[arg-type]

    def _input_for_state(self, session: SessionRow, fields: dict) -> dict:
        if session.pending_action == "resolve_conflict" and session.pending_json:
            pending = json.loads(session.pending_json)
            return {
                "type": "choice",
                "field": pending.get("field"),
                "choices": [
                    {"value": str(item.get("value")), "label": f"{item.get('label')} ({item.get('source')})"}
                    for item in pending.get("options") or []
                ],
            }
        if session.pending_action in {"confirm_submit", "confirm_review"}:
            preference = fields.get("contactPreference")
            if preference is None or preference.get("status") not in {"valid", "skipped"}:
                return input_for("contactPreference", None)
            return input_for(None, session.pending_action)
        key = next_question_key(fields, session.optional_offered)
        return input_for(key, None)

    async def _lock(self, db: AsyncSession, session_id: str) -> SessionRow:
        stmt = select(SessionRow).where(SessionRow.id == session_id)
        bind = db.bind
        if bind is not None and bind.dialect.name != "sqlite":
            stmt = stmt.with_for_update()
        session = (await db.execute(stmt)).scalar_one_or_none()
        if session is None:
            raise KeyError(session_id)
        return session

    async def _load_fields(self, db: AsyncSession, session_id: str) -> dict[str, dict]:
        rows = (
            await db.execute(select(FieldRow).where(FieldRow.session_id == session_id))
        ).scalars()
        loaded: dict[str, dict] = {}
        for row in rows:
            loaded[row.field_key] = {
                "value": None if row.value_json is None else json.loads(row.value_json),
                "raw": row.raw_value,
                "source": row.source,
                "status": row.status,
                "message": row.message,
                "candidates": json.loads(row.candidates_json) if row.candidates_json else [],
            }
        return loaded

    async def _save_fields(self, db: AsyncSession, session_id: str, fields: dict[str, dict]) -> None:
        existing_rows = (
            await db.execute(select(FieldRow).where(FieldRow.session_id == session_id))
        ).scalars()
        by_key = {row.field_key: row for row in existing_rows}
        for key, entry in fields.items():
            payload = {
                "raw_value": entry.get("raw"),
                "value_json": None if entry.get("value") is None else json.dumps(entry.get("value")),
                "source": entry.get("source"),
                "status": entry.get("status") or "missing",
                "message": entry.get("message"),
                "candidates_json": json.dumps(entry.get("candidates") or []),
                "updated_at": utcnow(),
            }
            row = by_key.get(key)
            if row is None:
                db.add(FieldRow(session_id=session_id, field_key=key, **payload))
            else:
                for name, value in payload.items():
                    setattr(row, name, value)

    async def _payload(self, db: AsyncSession, session: SessionRow, fields: dict, input_spec: dict) -> dict:
        turns = (
            await db.execute(
                select(TurnRow).where(TurnRow.session_id == session.id).order_by(TurnRow.id)
            )
        ).scalars()
        records = (
            await db.execute(
                select(RecordRow).where(RecordRow.session_id == session.id).order_by(RecordRow.revision)
            )
        ).scalars()
        rendered_records = []
        for row in records:
            item = json.loads(row.payload_json)
            item["deliveryStatus"] = row.delivery_status
            rendered_records.append(item)
        conflicts = []
        public_fields = {}
        for key, entry in fields.items():
            public_fields[key] = {
                "value": entry.get("value") if entry.get("status") in {"valid", "skipped"} else None,
                "source": entry.get("source"),
                "status": entry.get("status"),
                "message": entry.get("message"),
            }
            if entry.get("status") == "conflict":
                conflicts.append({"field": key, "options": entry.get("candidates") or []})
        return {
            "sessionId": session.id,
            "callActive": session.call_active,
            "outcome": session.latest_outcome,
            "revision": session.latest_revision,
            "pendingAction": session.pending_action,
            "fields": public_fields,
            "missing": missing_keys(fields),
            "conflicts": conflicts,
            "input": input_spec,
            "turns": [
                {
                    "id": turn.id,
                    "channel": turn.channel,
                    "role": turn.role,
                    "content": turn.content,
                    "createdAt": turn.created_at.isoformat() if turn.created_at else None,
                }
                for turn in turns
            ],
            "records": rendered_records,
            "latestRecord": rendered_records[-1] if rendered_records else None,
        }
