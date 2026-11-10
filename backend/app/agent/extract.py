"""Pull raw field snippets out of one user message. Validation is separate."""

from __future__ import annotations

import re
from datetime import date

from app.domain.fields import (
    FIELDS,
    INJURED_NO_RE,
    INJURED_YES_RE,
    STORY_RE,
    clean_text,
    is_no,
    is_yes,
    normalize_email,
    normalize_plate,
    normalize_policy,
    normalize_preference,
    parse_date_string,
)

_SPECIFIC_LOCATION = re.compile(
    r"\b(?:near|outside|around|at)\s+([A-Za-z0-9][^.,;\n]{1,80})",
    re.IGNORECASE,
)
_GENERAL_LOCATION = re.compile(
    r"\b(?:in|on|by)\s+([A-Za-z0-9][^.,;\n]{1,80})",
    re.IGNORECASE,
)
_FOLLOW_UP = re.compile(r"\b(call me|email me|follow up|contact me)\b", re.IGNORECASE)


def extract_candidates(
    message: str,
    expecting: str | None,
    today: date,
    channel: str,
) -> dict[str, str]:
    text = clean_text(message)
    if not text:
        return {}

    found: dict[str, str] = {}
    policy = normalize_policy(text)
    if policy:
        found["policyNumber"] = policy
    email = normalize_email(text)
    if email:
        found["contactEmail"] = email
    plate = normalize_plate(text)
    if plate:
        found["vehicleRegistration"] = plate
    if parse_date_string(text, today) is not None:
        found["incidentDate"] = text

    if expecting == "contactPreference" or _FOLLOW_UP.search(text):
        preference = normalize_preference(text)
        if preference:
            found["contactPreference"] = preference

    if INJURED_NO_RE.search(text) or (expecting == "anyoneInjured" and is_no(text)):
        found["anyoneInjured"] = text
    elif expecting == "anyoneInjured" and (is_yes(text) or INJURED_YES_RE.search(text)):
        found["anyoneInjured"] = text
    elif INJURED_YES_RE.search(text):
        found["anyoneInjured"] = text

    location = _location_snippet(text, today)
    if location:
        found["incidentLocation"] = location
    elif expecting == "incidentLocation" and "incidentDate" not in found:
        found["incidentLocation"] = text

    if expecting == "injuryDetails":
        found["injuryDetails"] = text

    if expecting == "incidentDescription":
        found["incidentDescription"] = text
    elif STORY_RE.search(text) and len(text.split()) >= 8:
        found["incidentDescription"] = text

    if expecting and expecting not in found:
        spec = FIELDS[expecting]
        words = len(text.split())
        structured_chat_field = spec.channel == "chat" and spec.kind in {"text", "choice"}
        if structured_chat_field and channel == "call" and words > 8:
            return found
        found[expecting] = text
    return found


def _location_snippet(text: str, today: date) -> str | None:
    for pattern in (_SPECIFIC_LOCATION, _GENERAL_LOCATION):
        match = pattern.search(text)
        if not match:
            continue
        snippet = clean_text(match.group(1))
        if snippet.lower() in {"chat", "the chat", "here", "there", "the call"}:
            continue
        if parse_date_string(snippet, today) is not None and len(snippet.split()) <= 5:
            continue
        if len(snippet) < 3:
            continue
        return snippet
    return None
