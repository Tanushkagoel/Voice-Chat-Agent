import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "claim.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + database.resolve().as_posix())
    monkeypatch.setenv("ALDER_TODAY", "2026-11-09")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RECORD_WEBHOOK_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("LIVEKIT_URL", "")
    monkeypatch.setenv("LIVEKIT_PUBLIC_URL", "")
    monkeypatch.setenv("LIVEKIT_API_KEY", "")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "")
    from app.config import reset_settings

    reset_settings()
    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _turn(client: AsyncClient, session_id: str, message: str, channel: str) -> dict:
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json={"message": message, "channel": channel, "clientId": "test"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _action(client: AsyncClient, session_id: str, action: str, channel: str = "chat") -> dict:
    response = await client.post(
        f"/api/sessions/{session_id}/actions",
        json={"action": action, "channel": channel, "clientId": "test"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_health(client: AsyncClient):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_call_and_chat_share_one_claim_and_file_one_revision(client: AsyncClient):
    created = (await client.post("/api/sessions")).json()
    session_id = created["sessionId"]
    assert "What happened" in created["turns"][0]["content"]

    spoken = await _turn(
        client,
        session_id,
        "I had an accident yesterday near my office. Nobody was injured.",
        "call",
    )
    fields = spoken["fields"]
    assert fields["incidentDescription"]["status"] == "valid"
    assert fields["incidentDescription"]["source"] == "call"
    assert fields["incidentDate"]["value"] == "2026-11-08"
    assert fields["incidentLocation"]["value"] == "my office"
    assert fields["anyoneInjured"]["value"] is False

    ended = await client.post(f"/api/sessions/{session_id}/call", json={"active": False})
    assert ended.status_code == 200
    assert ended.json()["callActive"] is False
    assert ended.json()["records"] == []
    assert ended.json()["fields"]["incidentDate"]["value"] == "2026-11-08"

    invalid = await _turn(client, session_id, "ABC123", "chat")
    assert invalid["fields"]["policyNumber"]["status"] == "invalid"

    policy = await _turn(client, session_id, "My policy is POL123456", "chat")
    assert policy["fields"]["policyNumber"]["value"] == "POL123456"
    assert policy["fields"]["policyNumber"]["source"] == "chat"

    plate = await _turn(client, session_id, "MH 12 AB 1234", "chat")
    assert plate["fields"]["vehicleRegistration"]["value"] == "MH12AB1234"

    email = await _turn(client, session_id, "ada@example.com", "chat")
    assert email["fields"]["contactEmail"]["value"] == "ada@example.com"
    assert email["input"]["type"] == "file"

    uploaded = await client.post(
        f"/api/sessions/{session_id}/uploads",
        files=[
            ("files", ("front.jpg", b"\xff\xd8\xff" + b"photo", "image/jpeg")),
            ("files", ("side.png", b"\x89PNG\r\n\x1a\n" + b"photo", "image/png")),
        ],
        data={"message": "Damage photos", "channel": "chat"},
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert len(body["fields"]["damagePhotos"]["value"]) == 2
    assert body["pendingAction"] == "confirm_submit"
    assert body["records"] == []

    # Ending the call must not file the claim. Starting it again keeps the photos.
    connected = await client.post(f"/api/sessions/{session_id}/call", json={"active": True})
    assert connected.status_code == 200
    assert connected.json()["records"] == []

    filed = await _action(client, session_id, "submit", "call")
    assert filed["outcome"] == "completed"
    assert filed["revision"] == 1
    record = filed["latestRecord"]
    assert record["outcome"] == "completed"
    assert record["fields"]["policyNumber"] == {
        "value": "POL123456",
        "source": "chat",
        "status": "valid",
    }
    assert record["fields"]["incidentDate"]["value"] == "2026-11-08"
    assert record["conflicts"] == []
    assert record["deliveryStatus"] == "delivered"

    inbox = (await client.get("/api/inbox")).json()
    assert len(inbox["deliveries"]) == 1

    again = await client.post("/api/admin/recover-deliveries")
    assert again.status_code == 200
    inbox_again = (await client.get("/api/inbox")).json()
    assert inbox_again["deliveries"] == inbox["deliveries"]
    history = (await client.get(f"/api/sessions/{session_id}/records")).json()
    assert len(history["records"]) == 1

    duplicate = await _action(client, session_id, "submit", "chat")
    assert duplicate["revision"] == 1
    assert len(duplicate["records"]) == 1


async def test_partial_then_return_creates_a_new_revision(client: AsyncClient):
    created = (await client.post("/api/sessions")).json()
    session_id = created["sessionId"]
    await _turn(client, session_id, "I had an accident yesterday near my office. Nobody was injured.", "call")
    partial = await _action(client, session_id, "finish_later")
    assert partial["outcome"] == "partial"
    assert partial["revision"] == 1
    assert "policyNumber" in partial["missing"]

    await _turn(client, session_id, "POL123456", "chat")
    await _turn(client, session_id, "MH12AB1234", "chat")
    await _turn(client, session_id, "ada@example.com", "chat")
    await client.post(
        f"/api/sessions/{session_id}/uploads",
        files=[("files", ("front.jpg", b"\xff\xd8\xffrest", "image/jpeg"))],
        data={"channel": "chat"},
    )
    revised = await _action(client, session_id, "submit")
    assert revised["outcome"] == "completed"
    assert revised["revision"] == 2
    assert [item["revision"] for item in revised["records"]] == [1, 2]
    assert revised["records"][0]["outcome"] == "partial"
    assert revised["records"][1]["outcome"] == "completed"


async def test_opt_out_and_conflict_review(client: AsyncClient):
    created = (await client.post("/api/sessions")).json()
    session_id = created["sessionId"]
    opted = await _turn(client, session_id, "I don't want to continue", "call")
    assert opted["outcome"] == "optedOut"
    assert opted["latestRecord"]["outcome"] == "optedOut"

    created = (await client.post("/api/sessions")).json()
    session_id = created["sessionId"]
    await _turn(client, session_id, "I had an accident yesterday near my office. Nobody was injured.", "call")
    conflict = await _turn(client, session_id, "The date was 2 November 2026", "chat")
    assert conflict["fields"]["incidentDate"]["status"] == "conflict"
    assert conflict["pendingAction"] == "resolve_conflict"

    review = await _action(client, session_id, "submit", "call")
    assert review["outcome"] == "needsReview"
    assert review["latestRecord"]["conflicts"][0]["field"] == "incidentDate"
    assert len(review["latestRecord"]["conflicts"][0]["values"]) == 2


async def test_correction_overwrites_without_a_conflict(client: AsyncClient):
    created = (await client.post("/api/sessions")).json()
    session_id = created["sessionId"]
    await _turn(client, session_id, "I had an accident yesterday near my office. Nobody was injured.", "call")
    corrected = await _turn(client, session_id, "Actually the date was 1 November 2026", "call")
    assert corrected["fields"]["incidentDate"]["status"] == "valid"
    assert corrected["fields"]["incidentDate"]["value"] == "2026-11-01"
    assert corrected["records"] == []


async def test_restart_keeps_fields_and_does_not_duplicate_the_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "restart.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + database.resolve().as_posix())
    monkeypatch.setenv("ALDER_TODAY", "2026-11-09")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RECORD_WEBHOOK_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    from app.config import reset_settings
    from app.main import create_app

    reset_settings()
    first = create_app()
    session_id = ""
    async with first.router.lifespan_context(first):
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://test") as http:
            created = (await http.post("/api/sessions")).json()
            session_id = created["sessionId"]
            await _turn(http, session_id, "I had an accident yesterday near my office. Nobody was injured.", "call")
            saved = await _action(http, session_id, "finish_later")
            assert saved["revision"] == 1

    reset_settings()
    second = create_app()
    async with second.router.lifespan_context(second):
        async with AsyncClient(transport=ASGITransport(app=second), base_url="http://test") as http:
            restored = (await http.get(f"/api/sessions/{session_id}")).json()
            assert restored["fields"]["incidentLocation"]["value"] == "my office"
            assert restored["revision"] == 1
            inbox = (await http.get("/api/inbox")).json()
            assert len(inbox["deliveries"]) == 1
            history = (await http.get(f"/api/sessions/{session_id}/records")).json()
            assert len(history["records"]) == 1


async def test_open_call_hears_a_chat_update(client: AsyncClient):
    created = (await client.post("/api/sessions")).json()
    session_id = created["sessionId"]
    connected = await client.post(f"/api/sessions/{session_id}/call", json={"active": True})
    assert connected.status_code == 200
    noted = await _turn(client, session_id, "POL123456", "chat")
    assert noted["voiceNotice"]
    assert "POL123456" in noted["voiceNotice"]
    assert any(turn["channel"] == "call" and "POL123456" in turn["content"] for turn in noted["turns"])
    assert noted["callActive"] is True
    assert noted["records"] == []


async def test_bad_photo_is_rejected(client: AsyncClient):
    created = (await client.post("/api/sessions")).json()
    session_id = created["sessionId"]
    response = await client.post(
        f"/api/sessions/{session_id}/uploads",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
        data={"channel": "chat"},
    )
    assert response.status_code == 422


def test_extraction_prompt_keeps_the_json_shape():
    from app.agent.llm import SYSTEM

    text = SYSTEM.format(today="2026-11-09")
    assert '"intent"' in text
    assert "2026-11-09" in text


async def test_setup_names_the_one_credential_and_voice_stays_off_without_it(client: AsyncClient):
    setup = await client.get("/api/setup")
    assert setup.status_code == 200
    body = setup.json()
    assert body["requiredFromYou"][0]["name"] == "GEMINI_API_KEY"
    assert body["requiredFromYou"][0]["set"] is False
    assert {item["name"] for item in body["included"]} == {"PostgreSQL", "Redis", "LiveKit"}
    created = (await client.post("/api/sessions")).json()
    token = await client.post(f"/api/sessions/{created['sessionId']}/livekit")
    assert token.status_code == 503
    assert "LiveKit" in token.json()["detail"]
