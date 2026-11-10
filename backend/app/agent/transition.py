"""Merge one turn into claim state and choose the next thing to say."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.domain.fields import (
    FIELDS,
    ack_for,
    ambiguous_claim,
    clean_text,
    display_value,
    field_entry,
    input_for,
    is_correction,
    is_finish_later,
    is_no,
    is_opt_out,
    is_yes,
    missing_keys,
    next_question_key,
    question_for,
    validate_normalized,
    values_equal,
)
from app.domain.record import should_emit

_FIRST = re.compile(r"\b(first|former|original|earlier|1)\b", re.IGNORECASE)
_SECOND = re.compile(r"\b(second|latter|new one|latest|2)\b", re.IGNORECASE)
_SKIP = re.compile(r"^(skip|no thanks|that's all|thats all)$", re.IGNORECASE)


def interpret_intent(message: str, pending_action: str | None, expecting: str | None, force_intent: str | None) -> str:
    if force_intent:
        return force_intent
    text = clean_text(message)
    if not text:
        return "provide"
    if is_opt_out(text):
        return "opt_out"
    if is_finish_later(text):
        return "finish_later"
    if expecting == "contactPreference" and _SKIP.fullmatch(text):
        return "skip_optional"
    if pending_action in {"confirm_submit", "confirm_review"} and is_yes(text):
        return "confirm"
    if pending_action in {"confirm_submit", "confirm_review"} and is_no(text):
        return "deny"
    return "provide"


def merge_turn(
    fields: dict[str, dict],
    candidates: dict[str, str],
    *,
    message: str,
    channel: str,
    today: date,
    correction: bool,
    attachments: list[dict],
    skip_optional: bool,
    pending_action: str | None,
    pending: dict | None,
) -> dict[str, Any]:
    updated = {key: dict(value) for key, value in fields.items()}
    changes: dict[str, dict] = {}
    repeats: list[str] = []
    warnings: list[str] = []
    resolved = False

    if (
        pending_action == "resolve_conflict"
        and pending
        and not correction
    ):
        chosen = _match_option(message, pending.get("options") or [])
        if chosen is not None:
            key = pending["field"]
            updated[key] = field_entry(
                value=chosen["value"],
                raw=str(chosen["label"]),
                source=chosen.get("source"),
                status="valid",
            )
            changes[key] = updated[key]
            candidates = {name: raw for name, raw in candidates.items() if name != key}
            resolved = True

    for key, raw in candidates.items():
        if key not in FIELDS or key == "damagePhotos":
            continue
        value, error, raw_store = validate_normalized(key, raw, today)
        current = updated.get(key)
        if error or value is None:
            if current and current.get("status") == "valid":
                if error:
                    warnings.append(error)
                continue
            changes[key] = field_entry(
                value=None,
                raw=raw_store,
                source=channel,
                status="invalid",
                message=error,
            )
            updated[key] = changes[key]
            continue

        if current and current.get("status") == "valid" and values_equal(current.get("value"), value):
            repeats.append(key)
            continue
        if current and current.get("status") == "valid" and not values_equal(current.get("value"), value):
            if correction:
                changes[key] = field_entry(value=value, raw=raw_store, source=channel, status="valid")
                updated[key] = changes[key]
                continue
            option_old = {
                "value": current.get("value"),
                "source": current.get("source"),
                "label": display_value(key, current.get("value")),
            }
            option_new = {"value": value, "source": channel, "label": display_value(key, value)}
            changes[key] = field_entry(
                value=None,
                raw=raw_store,
                source=channel,
                status="conflict",
                message=f"Conflict on {FIELDS[key].label}.",
                candidates=[option_old, option_new],
            )
            updated[key] = changes[key]
            continue
        changes[key] = field_entry(value=value, raw=raw_store, source=channel, status="valid")
        updated[key] = changes[key]

    if attachments:
        existing = []
        current_photos = updated.get("damagePhotos")
        if current_photos and current_photos.get("status") == "valid":
            existing = list(current_photos.get("value") or [])
        seen = {item["id"] for item in existing}
        merged = existing + [item for item in attachments if item["id"] not in seen]
        changes["damagePhotos"] = field_entry(
            value=merged,
            raw=", ".join(item["name"] for item in merged),
            source="chat",
            status="valid",
        )
        updated["damagePhotos"] = changes["damagePhotos"]

    if skip_optional:
        changes["contactPreference"] = field_entry(
            value=None, raw=None, source=channel, status="skipped"
        )
        updated["contactPreference"] = changes["contactPreference"]

    conflict_field = next((key for key, item in changes.items() if item.get("status") == "conflict"), None)
    if conflict_field is None:
        conflict_field = next((key for key, item in updated.items() if item.get("status") == "conflict"), None)

    return {
        "fields": updated,
        "changes": changes,
        "repeats": repeats,
        "resolved": resolved,
        "conflict_field": conflict_field,
        "warnings": warnings,
    }


def _match_option(message: str, options: list[dict]) -> dict | None:
    if not options:
        return None
    if _FIRST.search(message) and not _SECOND.search(message):
        return options[0]
    if _SECOND.search(message) and not _FIRST.search(message):
        return options[1] if len(options) > 1 else options[0]
    lowered = message.lower()
    for option in options:
        label = str(option.get("label") or "").lower()
        raw_value = str(option.get("value")).lower()
        if label and label in lowered:
            return option
        if raw_value and raw_value in lowered:
            return option
    return None


def choose_next(
    *,
    fields: dict[str, dict],
    changes: dict[str, dict],
    repeats: list[str],
    warnings: list[str],
    resolved: bool,
    conflict_field: str | None,
    intent: str,
    channel: str,
    call_active: bool,
    optional_offered: bool,
    pending_action: str | None,
    attachment_error: str | None,
    revision: int,
    dirty: bool,
    latest_outcome: str | None,
    fields_changed: bool,
) -> dict[str, Any]:
    optional_out = optional_offered
    set_pending = False
    pending_action_out = pending_action
    pending_out = None
    finalize = None
    review_reason = ambiguous_claim(fields)

    def emit(outcome: str) -> str | None:
        # dirty for this decision includes edits from the current turn
        is_dirty = dirty or fields_changed
        if should_emit(revision, is_dirty, outcome, latest_outcome):
            return outcome
        return None

    if intent == "opt_out":
        finalize = emit("optedOut")
        reply = (
            "I’ve stopped this claim and recorded that you opted out."
            if finalize
            else "This claim is already recorded as opted out."
        )
        return _pack(reply, None, None, True, None, finalize, channel, call_active, optional_out, attachment_error)

    if intent == "deny" and pending_action in {"confirm_submit", "confirm_review"}:
        reply = "Okay. We won’t file yet. Tell me what you want to change."
        return _pack(reply, None, None, True, None, None, channel, call_active, optional_out, attachment_error)

    if intent == "finish_later":
        finalize = emit("partial")
        if finalize:
            reply = _missing_sentence(fields, "I’ve saved this as a partial claim.")
            reply += " When you come back with the rest, I’ll file a new revision rather than overwrite this one."
        else:
            reply = "I already saved this partial claim. Add the missing details when you’re ready."
        focus = missing_keys(fields)[0] if missing_keys(fields) else None
        return _pack(reply, None, None, True, focus, finalize, channel, call_active, optional_out, attachment_error)

    invalid = [key for key, item in changes.items() if item.get("status") == "invalid"]

    if intent == "confirm":
        if any(item.get("status") == "conflict" for item in fields.values()):
            finalize = emit("needsReview")
            reply = (
                "I’ve sent this for a person to review because an answer is still conflicting."
                if finalize
                else "A reviewer already has this version. Change an answer if you need another revision."
            )
            return _pack(reply, None, None, True, None, finalize, channel, call_active, optional_out, attachment_error)
        if missing_keys(fields) or invalid:
            # Confirmation can't invent the missing answers.
            focus = invalid[0] if invalid else missing_keys(fields)[0]
            reply = " ".join(
                part for part in [_ack(changes, repeats, resolved), " ".join(warnings), _ask_key(focus, channel)] if part
            )
            return _pack(reply, None, None, True, focus, None, channel, call_active, optional_out, attachment_error)
        if review_reason:
            finalize = emit("needsReview")
            reply = (
                f"{review_reason} I’ve sent it for review."
                if finalize
                else f"{review_reason} A reviewer already has this version."
            )
            return _pack(reply, None, None, True, None, finalize, channel, call_active, optional_out, attachment_error)
        finalize = emit("completed")
        if finalize:
            reply = "I’ve filed the claim. The structured record is below."
        else:
            reply = (
                f"Revision {revision} is already filed"
                + (f" as {latest_outcome}." if latest_outcome else ".")
                + " Tell me what to change if you need a new revision."
            )
        return _pack(reply, None, None, True, None, finalize, channel, call_active, optional_out, attachment_error)

    if conflict_field:
        spec = FIELDS[conflict_field]
        current = fields[conflict_field]
        options = current.get("candidates") or []
        if len(options) >= 2:
            reply = (
                f"I have two different answers for {spec.label}: "
                f"{options[0]['label']} from the {options[0]['source']}, and "
                f"{options[1]['label']} from the {options[1]['source']}. Which should I keep?"
            )
        else:
            reply = f"I have two answers for {spec.label}. Which should I keep?"
        return _pack(
            reply,
            "resolve_conflict",
            {"field": conflict_field, "options": options},
            True,
            conflict_field,
            None,
            channel,
            call_active,
            optional_out,
            attachment_error,
        )

    # Ordinary collection.
    ack = " ".join(part for part in [_ack(changes, repeats, resolved), " ".join(warnings)] if part)
    if invalid:
        messages = [fields[key].get("message") for key in invalid if fields.get(key, {}).get("message")]
        reply = " ".join(part for part in [ack, " ".join(messages), _ask_key(invalid[0], channel)] if part)
        return _pack(reply, None, None, True, invalid[0], None, channel, call_active, optional_out, attachment_error)

    missing = missing_keys(fields)
    if not missing:
        preference = fields.get("contactPreference")
        preference_done = preference is not None and preference.get("status") in {"valid", "skipped"}
        if not preference_done:
            optional_out = True
            reply = (ack + " " if ack else "") + question_for("contactPreference", channel)
            reply += " Say submit when you want me to file the claim."
            return _pack(
                reply.strip(),
                "confirm_submit",
                None,
                True,
                "contactPreference",
                None,
                channel,
                call_active,
                optional_out,
                attachment_error,
            )
        if review_reason:
            reply = (ack + " " if ack else "") + review_reason + " I can send this to a reviewer, or you can correct it."
            return _pack(
                reply.strip(),
                "confirm_review",
                None,
                True,
                None,
                None,
                channel,
                call_active,
                optional_out,
                attachment_error,
            )
        reply = (ack + " " if ack else "") + "I have the required details. Submit the claim when you’re ready."
        return _pack(
            reply.strip(),
            "confirm_submit",
            None,
            True,
            None,
            None,
            channel,
            call_active,
            optional_out,
            attachment_error,
        )

    next_key = missing[0]
    question = question_for(next_key, channel)
    reply = f"{ack} {question}".strip() if ack else question
    return _pack(reply, None, None, True, next_key, None, channel, call_active, optional_out, attachment_error)


def _ack(changes: dict[str, dict], repeats: list[str], resolved: bool) -> str:
    parts: list[str] = []
    for key, item in changes.items():
        if item.get("status") == "valid":
            parts.append(ack_for(key, item.get("value")))
        elif item.get("status") == "skipped":
            parts.append("We’ll skip the contact preference.")
    for key in repeats:
        parts.append(f"That’s the same {FIELDS[key].label} I already have.")
    if resolved and not parts:
        parts.append("I’ll keep that answer.")
    return " ".join(parts)


def _ask(fields: dict[str, dict], channel: str, optional_offered: bool) -> str:
    key = next_question_key(fields, optional_offered)
    if key is None:
        return "What should I change?"
    return question_for(key, channel)


def _ask_key(key: str, channel: str) -> str:
    return question_for(key, channel)


def _missing_sentence(fields: dict[str, dict], lead: str) -> str:
    missing = missing_keys(fields)
    if not missing:
        return lead
    labels = ", ".join(FIELDS[key].label for key in missing)
    return f"{lead} Still needed: {labels}."


def _pack(
    reply: str,
    pending_action: str | None,
    pending: dict | None,
    set_pending: bool,
    focus_field: str | None,
    finalize: str | None,
    channel: str,
    call_active: bool,
    optional_offered: bool,
    attachment_error: str | None,
) -> dict[str, Any]:
    reply = " ".join(reply.split())
    if attachment_error:
        reply = f"{attachment_error} {reply}".strip()
    if pending_action == "resolve_conflict" and pending:
        input_spec = {
            "type": "choice",
            "field": pending.get("field"),
            "choices": [
                {"value": str(item.get("value")), "label": f"{item.get('label')} ({item.get('source')})"}
                for item in pending.get("options") or []
            ],
        }
    elif focus_field in FIELDS:
        input_spec = input_for(focus_field, None)
    else:
        input_spec = input_for(None, pending_action)
    voice = reply if channel == "chat" and call_active else None
    return {
        "reply": reply,
        "input": input_spec,
        "set_pending": set_pending,
        "pending_action": pending_action,
        "pending": pending,
        "finalize": finalize,
        "voice_notice": voice,
        "optional_offered": optional_offered,
        "focus_field": focus_field,
    }

