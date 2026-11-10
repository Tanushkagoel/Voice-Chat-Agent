"""Gemini extraction. Structured regex results override the model when both exist."""

from __future__ import annotations

import json
import re
from datetime import date

import httpx

from app.config import Settings

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM = """You extract one turn of a motor-insurance claim.
Return only JSON with this shape:
{{"intent":"provide|opt_out|finish_later|confirm|deny|other","correction":false,"fields":{{}}}}
fields may include incidentDescription, incidentDate (YYYY-MM-DD), incidentLocation,
anyoneInjured (boolean), injuryDetails, policyNumber, vehicleRegistration, contactEmail,
contactPreference (phone, email, or either).
Use null for anything the user did not clearly say. Do not invent values.
Today is {today}. Resolve yesterday, today, and last weekday against that date.
"""


async def extract_with_gemini(
    settings: Settings,
    message: str,
    today: date,
    expecting: str | None,
) -> tuple[dict, int, int]:
    if not settings.gemini_api_key:
        return {}, 0, 0
    prompt = {
        "today": today.isoformat(),
        "expecting": expecting,
        "message": message,
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM.format(today=today.isoformat())}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(url, params={"key": settings.gemini_api_key}, json=body)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return {}, 0, 0

    text = ""
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            text += part.get("text") or ""
    usage = payload.get("usageMetadata") or {}
    prompt_tokens = int(usage.get("promptTokenCount") or max(1, len(message) // 4))
    completion_tokens = int(usage.get("candidatesTokenCount") or max(1, len(text) // 4))
    parsed = _parse_json(text)
    if not isinstance(parsed, dict):
        return {}, prompt_tokens, completion_tokens
    return parsed, prompt_tokens, completion_tokens


_FACT = re.compile(
    r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}|POL[A-Z0-9]+|\b\d{4}-\d{2}-\d{2}\b|\b[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}\b",
    re.IGNORECASE,
)


async def phrase_reply(settings: Settings, reply: str) -> tuple[str | None, int, int]:
    """Gemini may rephrase the question. It may not change a fact the rules already chose."""
    if not settings.gemini_api_key or not reply.strip():
        return None, 0, 0
    body = {
        "system_instruction": {
            "parts": [
                {
                    "text": (
                        "Rewrite one claim-desk line so it sounds spoken and brief. "
                        "Keep every email, policy number, registration, and date exactly as written. "
                        "Do not add facts or a new question. Return only the rewritten line."
                    )
                }
            ]
        },
        "contents": [{"role": "user", "parts": [{"text": reply}]}],
        "generationConfig": {"temperature": 0.2},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(url, params={"key": settings.gemini_api_key}, json=body)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None, 0, 0
    text = ""
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            text += part.get("text") or ""
    usage = payload.get("usageMetadata") or {}
    prompt_tokens = int(usage.get("promptTokenCount") or 0)
    completion_tokens = int(usage.get("candidatesTokenCount") or 0)
    phrased = " ".join(text.strip().split())
    if not phrased or len(phrased) > len(reply) * 2 + 80:
        return None, prompt_tokens, completion_tokens
    for fact in _FACT.findall(reply):
        if fact.lower() not in phrased.lower():
            return None, prompt_tokens, completion_tokens
    return phrased, prompt_tokens, completion_tokens


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
