"""Claim fields, validation, and normalization for a motor insurance claim."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

Channel = Literal["call", "chat"]
FieldStatus = Literal["valid", "invalid", "missing", "conflict", "skipped"]

POLICY_RE = re.compile(r"\bPOL[-\s]?[A-Z0-9]{6,12}\b", re.IGNORECASE)
POLICY_EXACT = re.compile(r"^POL[A-Z0-9]{6,12}$")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PLATE_GROUPED = re.compile(
    r"\b([A-Z]{2})\s*([0-9]{1,2})\s*([A-Z]{1,3})\s*([0-9]{4})\b",
    re.IGNORECASE,
)
BH_PLATE = re.compile(r"\b([0-9]{2})\s*BH\s*([0-9]{4})\s*([A-Z]{1,2})\b", re.IGNORECASE)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NUMERIC_DATE = re.compile(r"\b(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})\b")

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

CORRECTION_RE = re.compile(
    r"\b(actually|i meant|correction|sorry,? the|rather|instead|let me correct|change that)\b",
    re.IGNORECASE,
)
OPT_OUT_RE = re.compile(
    r"\b(don'?t want to continue|do not want to continue|opt out|cancel (the|this) claim|stop (the|this) claim)\b",
    re.IGNORECASE,
)
FINISH_LATER_RE = re.compile(
    r"\b(finish later|come back later|save (it|this) for later|continue later|i'?ll be back|pause (the|this) claim)\b",
    re.IGNORECASE,
)
YES_RE = re.compile(
    r"^(yes|yeah|yep|yup|submit|go ahead|please submit|confirm|looks good|file it|do it|that'?s right|that is right)\b",
    re.IGNORECASE,
)
NO_RE = re.compile(r"^(no|nope|wait|not yet|hold on|don'?t|do not)\b", re.IGNORECASE)
INJURED_NO_RE = re.compile(
    r"\b(nobody|no one|no injuries|no injury|not injured|wasn'?t injured|not hurt|wasn'?t hurt|no one was hurt|nobody was hurt)\b",
    re.IGNORECASE,
)
INJURED_YES_RE = re.compile(
    r"\b(injured|injury|injuries|hurt|hospital|bleeding|whiplash)\b",
    re.IGNORECASE,
)
STORY_RE = re.compile(
    r"\b(accident|crash|hit|collided|collision|bumped|damaged|happened|skidded|rear[- ]ended|scratched)\b",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"\b(?:near|outside|at|in|on|by|around)\s+([A-Za-z0-9][^.,;\n]{1,80})",
    re.IGNORECASE,
)
PREFERENCE_RE = re.compile(r"\b(phone|email|either)\b", re.IGNORECASE)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    channel: Channel
    required: bool
    kind: Literal["text", "date", "boolean", "choice", "files"]
    placeholder: str = ""


FIELDS: dict[str, FieldSpec] = {
    "incidentDescription": FieldSpec(
        "incidentDescription",
        "what happened",
        "call",
        True,
        "text",
        "A short account of the accident",
    ),
    "incidentDate": FieldSpec("incidentDate", "the date", "call", True, "date", "For example, yesterday"),
    "incidentLocation": FieldSpec(
        "incidentLocation", "the location", "call", True, "text", "Where did it happen?"
    ),
    "anyoneInjured": FieldSpec(
        "anyoneInjured", "whether anyone was injured", "call", True, "boolean", "Yes or no"
    ),
    "injuryDetails": FieldSpec(
        "injuryDetails", "the injury details", "call", False, "text", "Who was hurt, and how seriously?"
    ),
    "policyNumber": FieldSpec(
        "policyNumber", "the policy number", "chat", True, "text", "POL123456"
    ),
    "vehicleRegistration": FieldSpec(
        "vehicleRegistration", "the vehicle registration", "chat", True, "text", "MH12AB1234"
    ),
    "contactEmail": FieldSpec(
        "contactEmail", "the email address", "chat", True, "text", "you@example.com"
    ),
    "damagePhotos": FieldSpec("damagePhotos", "the damage photos", "chat", True, "files"),
    "contactPreference": FieldSpec(
        "contactPreference", "a contact preference", "chat", False, "choice", "Phone, email, or either"
    ),
}

ORDER = list(FIELDS)
CHOICES = {
    "contactPreference": [
        {"value": "phone", "label": "Phone"},
        {"value": "email", "label": "Email"},
        {"value": "either", "label": "Either"},
    ]
}

# Spoken and typed collection targets required by the brief.
CALL_REQUIRED = ["incidentDescription", "incidentDate", "incidentLocation", "anyoneInjured"]
CHAT_REQUIRED = ["policyNumber", "vehicleRegistration", "contactEmail", "damagePhotos"]


def clean_text(value: str, limit: int = 2000) -> str:
    text = CONTROL_CHARS.sub("", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def display_date(iso_value: str) -> str:
    parsed = date.fromisoformat(iso_value)
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def display_value(key: str, value: Any) -> str:
    if value is None:
        return "missing"
    if key == "incidentDate" and isinstance(value, str):
        try:
            return display_date(value)
        except ValueError:
            return value
    if key == "anyoneInjured":
        return "yes" if value else "no"
    if key == "damagePhotos" and isinstance(value, list):
        count = len(value)
        return f"{count} photo" if count == 1 else f"{count} photos"
    if key == "contactPreference":
        return str(value)
    return str(value)


def parse_date_string(text: str, today: date) -> date | None:
    lowered = text.lower()
    if re.search(r"\btomorrow\b", lowered):
        return today + timedelta(days=1)
    if re.search(r"\byesterday\b", lowered):
        return today - timedelta(days=1)
    if re.search(r"\btoday\b", lowered):
        return today

    weekday = re.search(
        r"\b(?:last|this past)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lowered,
    )
    if weekday:
        target = WEEKDAYS[weekday.group(1)]
        delta = (today.weekday() - target) % 7
        delta = 7 if delta == 0 else delta
        return today - timedelta(days=delta)

    named = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)(?:\s+(\d{4}))?\b",
        lowered,
    )
    if not named:
        named = re.search(
            r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
            lowered,
        )
        if named:
            month = MONTHS[named.group(1)]
            day = int(named.group(2))
            year = int(named.group(3)) if named.group(3) else today.year
            return _safe_date(year, month, day)
    else:
        day = int(named.group(1))
        month = MONTHS[named.group(2)]
        year = int(named.group(3)) if named.group(3) else today.year
        return _safe_date(year, month, day)

    iso = ISO_DATE.search(text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    numeric = NUMERIC_DATE.search(text)
    if numeric:
        day = int(numeric.group(1))
        month = int(numeric.group(2))
        year = int(numeric.group(3))
        if year < 100:
            year += 2000
        return _safe_date(year, month, day)
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def validate_date(value: date, today: date) -> tuple[str | None, str | None]:
    if value > today:
        return None, "The incident date can’t be in the future."
    if value < today - timedelta(days=366):
        return None, "Claims can be opened for incidents in the last year. Please check the date."
    return value.isoformat(), None


def normalize_policy(text: str) -> str | None:
    match = POLICY_RE.search(text)
    if not match:
        compact = re.sub(r"[^A-Z0-9]", "", text.upper())
        if POLICY_EXACT.fullmatch(compact):
            return compact
        return None
    compact = re.sub(r"[^A-Z0-9]", "", match.group(0).upper())
    if POLICY_EXACT.fullmatch(compact):
        return compact
    return None


def normalize_email(text: str) -> str | None:
    match = EMAIL_RE.search(text)
    if not match:
        return None
    return match.group(0).lower()


def normalize_plate(text: str) -> str | None:
    bh = BH_PLATE.search(text.upper())
    if bh:
        return f"{bh.group(1)}BH{bh.group(2)}{bh.group(3)}"
    plate = PLATE_GROUPED.search(text.upper())
    if plate:
        return f"{plate.group(1)}{plate.group(2)}{plate.group(3)}{plate.group(4)}"
    return None


def normalize_preference(text: str) -> str | None:
    match = PREFERENCE_RE.search(text)
    if not match:
        return None
    return match.group(1).lower()


def is_correction(text: str) -> bool:
    return bool(CORRECTION_RE.search(text))


def is_opt_out(text: str) -> bool:
    return bool(OPT_OUT_RE.search(text))


def is_finish_later(text: str) -> bool:
    return bool(FINISH_LATER_RE.search(text))


def is_yes(text: str) -> bool:
    return bool(YES_RE.search(text.strip()))


def is_no(text: str) -> bool:
    return bool(NO_RE.search(text.strip()))


def ambiguous_claim(fields: dict[str, dict]) -> str | None:
    description = str((fields.get("incidentDescription") or {}).get("value") or "")
    injured = (fields.get("anyoneInjured") or {}).get("value")
    lowered = description.lower()
    if any(word in lowered for word in ("died", "dead", "fatal", "killed", "passed away")):
        return "The description mentions a death, so a reviewer needs to see this claim."
    says_injury = bool(re.search(r"\b(injured|injury|hurt|bleeding|hospital)\b", lowered))
    says_none = bool(INJURED_NO_RE.search(lowered))
    if injured is False and says_injury and not says_none:
        return "The description mentions an injury, but the injury answer was no."
    if injured is True and says_none and not says_injury:
        return "The description says nobody was hurt, but the injury answer was yes."
    return None


def injury_details_required(fields: dict[str, dict]) -> bool:
    return (fields.get("anyoneInjured") or {}).get("value") is True


def is_satisfied(key: str, fields: dict[str, dict]) -> bool:
    spec = FIELDS[key]
    current = fields.get(key)
    if key == "injuryDetails" and not injury_details_required(fields):
        return True
    if not spec.required and key != "injuryDetails":
        if current is None:
            return False
        return current.get("status") in {"valid", "skipped"}
    if current is None:
        return False
    return current.get("status") == "valid" and current.get("value") not in (None, "", [])


def required_keys(fields: dict[str, dict]) -> list[str]:
    keys = [key for key, spec in FIELDS.items() if spec.required]
    if injury_details_required(fields):
        keys.append("injuryDetails")
    return keys


def missing_keys(fields: dict[str, dict]) -> list[str]:
    return [key for key in required_keys(fields) if not is_satisfied(key, fields)]


def next_question_key(fields: dict[str, dict], optional_offered: bool) -> str | None:
    missing = missing_keys(fields)
    if missing:
        return missing[0]
    preference = fields.get("contactPreference")
    if not optional_offered or preference is None:
        if preference is None or preference.get("status") not in {"valid", "skipped"}:
            return "contactPreference"
    return None


def input_for(field_key: str | None, pending_action: str | None) -> dict:
    if pending_action == "resolve_conflict":
        return {"type": "text", "field": field_key, "placeholder": "Say which value to keep"}
    if pending_action in {"confirm_submit", "confirm_review"}:
        return {
            "type": "choice",
            "field": "_confirm",
            "choices": [
                {"value": "submit", "label": "Submit claim"},
                {"value": "not_yet", "label": "Not yet"},
            ],
        }
    if field_key is None:
        return {"type": "text", "field": None, "placeholder": "Message Alder"}
    spec = FIELDS[field_key]
    if spec.kind == "choice":
        return {
            "type": "choice",
            "field": field_key,
            "choices": CHOICES[field_key],
            "skippable": True,
        }
    if spec.kind == "files":
        return {
            "type": "file",
            "field": field_key,
            "accept": "image/jpeg,image/png,image/webp",
            "multiple": True,
        }
    return {"type": "text", "field": field_key, "placeholder": spec.placeholder}


def question_for(field_key: str, channel: Channel) -> str:
    spec = FIELDS[field_key]
    if field_key == "incidentDescription":
        return "What happened to the vehicle?"
    if field_key == "incidentDate":
        return "When did this happen?"
    if field_key == "incidentLocation":
        return "Where did it happen?"
    if field_key == "anyoneInjured":
        return "Was anyone injured?"
    if field_key == "injuryDetails":
        return "Who was injured, and how seriously?"
    if field_key == "policyNumber":
        prompt = "Please enter the policy number. It looks like POL followed by 6 to 12 letters or digits."
        return _channel_hint(prompt, spec, channel)
    if field_key == "vehicleRegistration":
        return _channel_hint("Please enter the vehicle registration.", spec, channel)
    if field_key == "contactEmail":
        return _channel_hint("Please enter the best email for this claim.", spec, channel)
    if field_key == "damagePhotos":
        return _channel_hint("Please upload photos of the damage.", spec, channel)
    if field_key == "contactPreference":
        return "If you want, choose phone, email, or either for a follow-up. You can skip this and submit."
    return f"Please share {spec.label}."


def _channel_hint(prompt: str, spec: FieldSpec, channel: Channel) -> str:
    if spec.channel == "chat" and channel == "call":
        return f"{prompt} The chat panel is the surest place for that."
    if spec.channel == "call" and channel == "chat":
        return f"{prompt} You can answer here or say it on the call."
    return prompt


def ack_for(key: str, value: Any) -> str:
    shown = display_value(key, value)
    if key == "incidentDescription":
        return "I’ve noted what happened."
    if key == "incidentDate":
        return f"I’ve noted the date as {shown}."
    if key == "incidentLocation":
        return f"I’ve noted the location as {shown}."
    if key == "anyoneInjured":
        if value:
            return "I’ve noted that someone was injured."
        return "I’ve noted that nobody was injured."
    if key == "injuryDetails":
        return "I’ve noted the injury details."
    if key == "policyNumber":
        return f"I’ve noted policy number {shown}."
    if key == "vehicleRegistration":
        return f"I’ve noted registration {shown}."
    if key == "contactEmail":
        return f"I’ve noted the email {shown}."
    if key == "damagePhotos":
        return f"I’ve received the damage photos ({shown})."
    if key == "contactPreference":
        return f"I’ve noted that we should follow up by {shown}."
    return f"I’ve noted {FIELDS[key].label}."


def validate_normalized(key: str, raw: str, today: date) -> tuple[Any, str | None, str | None]:
    """Return value, error, raw-to-store. value is None when invalid."""
    text = clean_text(raw)
    if key == "incidentDescription":
        if len(text) < 8:
            return None, "Tell me a little more about what happened. A short sentence is enough.", text
        return text, None, text
    if key == "incidentLocation":
        if len(text) < 3:
            return None, "I need a place, even a short one such as a road or a landmark.", text
        return text, None, text
    if key == "incidentDate":
        parsed = parse_date_string(text, today)
        if parsed is None:
            return None, "I couldn’t read that date. Try 8 November 2026, or say yesterday.", text
        iso, error = validate_date(parsed, today)
        return iso, error, text
    if key == "anyoneInjured":
        if INJURED_NO_RE.search(text) or is_no(text):
            return False, None, text
        if INJURED_YES_RE.search(text) or is_yes(text):
            return True, None, text
        return None, "Please say yes or no: was anyone injured?", text
    if key == "injuryDetails":
        if len(text) < 3:
            return None, "Please add a short note about who was hurt.", text
        return text, None, text
    if key == "policyNumber":
        policy = normalize_policy(text)
        if policy is None:
            return None, "Policy numbers look like POL123456: POL plus 6 to 12 letters or digits.", text
        return policy, None, text
    if key == "vehicleRegistration":
        plate = normalize_plate(text)
        if plate is None:
            return None, "I couldn’t read that registration. An example is MH12AB1234.", text
        return plate, None, text
    if key == "contactEmail":
        email = normalize_email(text)
        if email is None:
            return None, "That email doesn’t look valid. Please try one like you@example.com.", text
        return email, None, text
    if key == "contactPreference":
        preference = normalize_preference(text)
        if preference is None:
            return None, "Choose phone, email, or either.", text
        return preference, None, text
    return text, None, text


def field_entry(
    *,
    value: Any,
    raw: str | None,
    source: str | None,
    status: FieldStatus,
    message: str | None = None,
    candidates: list[dict] | None = None,
) -> dict:
    return {
        "value": value,
        "raw": raw,
        "source": source,
        "status": status,
        "message": message,
        "candidates": candidates or [],
    }


def values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.strip().lower() == right.strip().lower()
    return left == right


def photo_entry(items: list[dict]) -> dict:
    return field_entry(
        value=items,
        raw=", ".join(item["name"] for item in items),
        source="chat",
        status="valid",
    )


def parse_stored_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
