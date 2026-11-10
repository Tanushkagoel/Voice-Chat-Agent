from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import AttachmentRow, InboxRow, RecordRow, SessionRow, TurnRow
from app.services.delivery import accept_inbox, recover_deliveries
from app.services.events import publish, publish_voice_control, subscribe, unsubscribe
from app.services.locks import SessionBusy, session_lock
from app.services.turns import new_id, utcnow
from app.services.uploads import extension_for, safe_filename, sniff_image

router = APIRouter(prefix="/api")


class TurnIn(BaseModel):
    message: str = ""
    channel: str = Field(pattern="^(call|chat)$")
    clientId: str | None = None
    skipField: str | None = None


class CallIn(BaseModel):
    active: bool
    clientId: str | None = None


class ActionIn(BaseModel):
    action: str = Field(pattern="^(submit|finish_later|opt_out)$")
    channel: str = Field(default="chat", pattern="^(call|chat)$")
    clientId: str | None = None


def _service(request: Request):
    return request.app.state.turns


def _missing(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Unknown session {exc.args[0]}")


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    await db.execute(text("SELECT 1"))
    settings = get_settings()
    return {
        "ok": True,
        "postgres": settings.database_url.startswith("postgresql"),
        "redis": bool(settings.redis_url),
        "livekit": bool(settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret),
        "gemini": bool(settings.gemini_api_key),
    }


@router.get("/setup")
async def setup() -> dict:
    settings = get_settings()
    return {
        "requiredFromYou": [
            {
                "name": "GEMINI_API_KEY",
                "set": bool(settings.gemini_api_key),
                "why": "Gemini transcribes the call, speaks the reply, extracts messy answers, and phrases the next question. Create a key at https://aistudio.google.com/apikey",
            }
        ],
        "included": [
            {
                "name": "PostgreSQL",
                "set": settings.database_url.startswith("postgresql"),
                "why": "Docker Compose creates the database. No separate account.",
            },
            {
                "name": "Redis",
                "set": bool(settings.redis_url),
                "why": "Docker Compose runs Redis for call events and the session lock. No account.",
            },
            {
                "name": "LiveKit",
                "set": bool(settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret),
                "why": "Docker Compose runs LiveKit with the built-in dev key. A public HTTPS deployment needs either that host's UDP ports or a LiveKit Cloud URL, API key, and secret.",
            },
        ],
    }


@router.post("/sessions")
async def create_session(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    return await _service(request).create_session(db)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await _service(request).get_session(db, session_id)
    except KeyError as exc:
        raise _missing(exc) from exc


@router.post("/sessions/{session_id}/turns")
async def post_turn(
    session_id: str,
    body: TurnIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.skipField and body.skipField != "contactPreference":
        raise HTTPException(status_code=422, detail="Only the contact preference can be skipped.")
    if not body.message.strip() and not body.skipField:
        raise HTTPException(status_code=400, detail="Say something, or upload a photo in chat.")
    if len(body.message) > 4000:
        raise HTTPException(status_code=422, detail="That message is too long.")
    return await _run_turn(
        request,
        db,
        session_id,
        message=body.message,
        channel=body.channel,
        client_id=body.clientId,
        skip_field=body.skipField,
    )


@router.post("/sessions/{session_id}/uploads")
async def upload(
    session_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    message: str = Form(""),
    channel: str = Form("chat"),
    clientId: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if channel not in {"call", "chat"}:
        raise HTTPException(status_code=422, detail="Channel must be call or chat.")
    if len(files) > 6:
        raise HTTPException(status_code=422, detail="Upload up to 6 photos at a time.")
    settings = get_settings()
    saved: list[dict] = []
    errors: list[str] = []
    for upload_file in files:
        data = await upload_file.read()
        display = safe_filename(upload_file.filename)
        if len(data) > settings.max_upload_bytes:
            errors.append(f"{display} is larger than 8 MB.")
            continue
        kind = sniff_image(data)
        if kind is None:
            errors.append(f"{display} needs to be a JPEG, PNG, or WebP photo.")
            continue
        file_id = new_id("file")
        path = settings.upload_path / f"{file_id}{extension_for(kind)}"
        path.write_bytes(data)
        db.add(
            AttachmentRow(
                id=file_id,
                session_id=session_id,
                field_key="damagePhotos",
                filename=display,
                content_type=kind,
                path=str(path),
                size=len(data),
                created_at=utcnow(),
            )
        )
        saved.append({"id": file_id, "name": display, "contentType": kind})
    if not saved and not message.strip():
        detail = " ".join(errors) or "Upload a JPEG, PNG, or WebP photo."
        raise HTTPException(status_code=422, detail=detail)
    return await _run_turn(
        request,
        db,
        session_id,
        message=message,
        channel=channel,
        client_id=clientId,
        attachments=saved,
        attachment_error=" ".join(errors) or None,
    )


@router.post("/sessions/{session_id}/call")
async def set_call(
    session_id: str,
    body: CallIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        payload = await _service(request).set_call(db, session_id, body.active)
    except KeyError as exc:
        raise _missing(exc) from exc
    await db.commit()
    await publish(session_id, {"type": "state", "origin": body.clientId, "state": _public_event(payload)})
    if settings_ready_for_voice():
        await publish_voice_control(
            {
                "action": "start" if body.active else "stop",
                "sessionId": session_id,
                "opening": payload.get("speak") if body.active else None,
            }
        )
    return payload


@router.post("/sessions/{session_id}/livekit")
async def livekit_token(session_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    settings = get_settings()
    public_url = settings.livekit_public_url or settings.livekit_url
    if not (public_url and settings.livekit_api_key and settings.livekit_api_secret):
        raise HTTPException(
            status_code=503,
            detail="LiveKit is not configured. Start docker compose, which includes a LiveKit server and the built-in dev key.",
        )
    if not settings.gemini_api_key:
        raise HTTPException(
            status_code=503,
            detail="Set GEMINI_API_KEY. Gemini transcribes the call and speaks the reply. Chat still works without it.",
        )
    try:
        await _service(request).get_session(db, session_id)
    except KeyError as exc:
        raise _missing(exc) from exc
    from app.voice.tokens import caller_token

    return {"url": public_url, "token": caller_token(settings, session_id), "room": session_id}


@router.post("/sessions/{session_id}/actions")
async def act(
    session_id: str,
    body: ActionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    intent = {"submit": "confirm", "finish_later": "finish_later", "opt_out": "opt_out"}[body.action]
    return await _run_turn(
        request,
        db,
        session_id,
        message="",
        channel=body.channel,
        client_id=body.clientId,
        force_intent=intent,
    )


@router.get("/sessions/{session_id}/records")
async def records(session_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        items = await _service(request).list_records(db, session_id)
    except KeyError as exc:
        raise _missing(exc) from exc
    return {"records": items}


@router.get("/sessions/{session_id}/attachments/{file_id}")
async def attachment(session_id: str, file_id: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    row = await db.get(AttachmentRow, file_id)
    if row is None or row.session_id != session_id:
        raise HTTPException(status_code=404, detail="Unknown file")
    return FileResponse(row.path, media_type=row.content_type, filename=row.filename)


@router.get("/sessions/{session_id}/events")
async def events(session_id: str, request: Request) -> StreamingResponse:
    queue = subscribe(session_id)

    async def stream() -> AsyncIterator[str]:
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: update\ndata: {json.dumps(item, default=str)}\n\n"
        finally:
            unsubscribe(session_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.websocket("/sessions/{session_id}/ws")
async def session_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    queue = subscribe(session_id)
    sender = asyncio.create_task(_forward_events(websocket, queue))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        unsubscribe(session_id, queue)


async def _forward_events(websocket: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(item)
    except (WebSocketDisconnect, RuntimeError):
        return


@router.post("/hooks/records")
async def hook(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    raw = await request.body()
    created = await accept_inbox(idempotency_key, "external", raw.decode())
    return {"status": "accepted" if created else "duplicate"}


@router.get("/inbox")
async def inbox(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(InboxRow).order_by(InboxRow.created_at))).scalars()
    return {
        "deliveries": [
            {"idempotencyKey": row.idempotency_key, "recordId": row.record_id} for row in rows
        ]
    }


@router.post("/admin/recover-deliveries")
async def recover() -> dict:
    count = await recover_deliveries()
    return {"retried": count}


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        await db.execute(
            select(TurnRow.latency_ms, TurnRow.prompt_tokens, TurnRow.completion_tokens).where(
                TurnRow.role == "assistant", TurnRow.latency_ms.is_not(None)
            )
        )
    ).all()
    latencies = sorted(row[0] for row in rows if row[0] is not None)
    prompt_tokens = sum(row[1] or 0 for row in rows)
    completion_tokens = sum(row[2] or 0 for row in rows)
    settings = get_settings()
    estimated = (prompt_tokens / 1_000_000) * settings.gemini_input_usd_per_million + (
        completion_tokens / 1_000_000
    ) * settings.gemini_output_usd_per_million
    sessions = await db.scalar(select(func.count()).select_from(SessionRow))
    completed = await db.scalar(
        select(func.count()).select_from(RecordRow).where(RecordRow.outcome == "completed")
    )
    return {
        "assistantTurns": len(latencies),
        "p50ServerLatencyMs": _percentile(latencies, 50),
        "p95ServerLatencyMs": _percentile(latencies, 95),
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "estimatedUsd": round(estimated, 6),
        "sessions": sessions or 0,
        "completedRecords": completed or 0,
        "estimatedUsdPerCompletedRecord": round(estimated / completed, 6) if completed else None,
    }


async def _run_turn(request: Request, db: AsyncSession, session_id: str, **kwargs) -> dict:
    client_id = kwargs.pop("client_id", None)
    channel = kwargs.get("channel")
    try:
        async with session_lock(session_id):
            payload, record_id = await _service(request).handle_turn(db, session_id, **kwargs)
            reply = payload.get("reply")
            voice_notice = payload.get("voiceNotice")
            latency_ms = payload.get("latencyMs")
            await db.commit()
    except SessionBusy as exc:
        raise HTTPException(status_code=409, detail="The other channel is still saving this claim. Try again.") from exc
    except KeyError as exc:
        if exc.args and exc.args[0] == session_id:
            raise _missing(exc) from exc
        raise
    if record_id:
        from app.services.delivery import deliver_record

        try:
            await deliver_record(record_id)
        except Exception:
            payload["deliveryError"] = "The record is saved and will be sent again after a restart."
        payload = await _service(request).get_session(db, session_id)
    payload["reply"] = reply
    payload["voiceNotice"] = voice_notice
    payload["latencyMs"] = latency_ms
    speak = voice_notice
    if not speak and payload.get("callActive") and channel == "call":
        speak = reply
    event = {"type": "state", "origin": client_id, "state": _public_event(payload), "voiceNotice": voice_notice}
    if speak:
        event["speak"] = speak
    await publish(session_id, event)
    return payload


def settings_ready_for_voice() -> bool:
    settings = get_settings()
    return bool(settings.redis_url and settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret)


def _public_event(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key not in {"reply", "speak"}}


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    index = round((percentile / 100) * (len(values) - 1))
    return values[max(0, min(len(values) - 1, index))]
