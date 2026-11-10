"""Gemini hears the caller and speaks the claim agent's reply. One API key covers both."""

from __future__ import annotations

import audioop
import base64
import logging

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


async def transcribe_wav(settings: Settings, wav: bytes) -> tuple[str, int, int]:
    if not settings.gemini_api_key or not wav:
        return "", 0, 0
    body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Transcribe the caller's speech in this audio. "
                            "Return only the words they said. If there is no speech, return an empty string."
                        )
                    },
                    {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0},
    }
    text, prompt_tokens, completion_tokens = await _generate(settings, settings.gemini_model, body)
    cleaned = text.strip().strip('"')
    if cleaned.lower() in {"", "empty", "silence", "[silence]", "(silence)"}:
        return "", prompt_tokens, completion_tokens
    return cleaned, prompt_tokens, completion_tokens


async def synthesize(settings: Settings, text: str) -> tuple[bytes, int] | None:
    """Return 16-bit mono PCM and its sample rate."""
    if not settings.gemini_api_key or not text.strip():
        return None
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": settings.gemini_tts_voice}}
            },
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_tts_model}:generateContent"
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, params={"key": settings.gemini_api_key}, json=body)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        log.exception("Gemini TTS failed")
        return None
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if not inline:
                continue
            raw = base64.b64decode(inline.get("data") or "")
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
            return _pcm_from_audio(raw, mime)
    log.warning("Gemini TTS returned no audio")
    return None


def resample(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate or not pcm:
        return pcm
    converted, _state = audioop.ratecv(pcm, 2, 1, source_rate, target_rate, None)
    return converted


async def _generate(settings: Settings, model: str, body: dict) -> tuple[str, int, int]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, params={"key": settings.gemini_api_key}, json=body)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        log.exception("Gemini request failed")
        return "", 0, 0
    text = ""
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            text += part.get("text") or ""
    usage = payload.get("usageMetadata") or {}
    return text, int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0)


def _pcm_from_audio(raw: bytes, mime: str) -> tuple[bytes, int]:
    rate = 24000
    for piece in mime.split(";"):
        if "rate=" in piece:
            try:
                rate = int(piece.split("rate=")[-1])
            except ValueError:
                rate = 24000
    if raw.startswith(b"RIFF") and len(raw) > 44:
        return raw[44:], rate
    return raw, rate
