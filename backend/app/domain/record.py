"""Build the sanitized business record for one revision."""

from __future__ import annotations

from typing import Any

from app.domain.fields import FIELDS, ambiguous_claim, injury_details_required


def build_record(session_id: str, revision: int, outcome: str, fields: dict[str, dict]) -> dict:
    rendered: dict[str, dict] = {}
    issues: list[dict] = []
    conflicts: list[dict] = []

    for key, spec in FIELDS.items():
        if key == "injuryDetails" and not injury_details_required(fields) and key not in fields:
            continue
        current = fields.get(key)
        if current is None:
            rendered[key] = {"value": None, "source": None, "status": "missing"}
            if spec.required or (key == "injuryDetails" and injury_details_required(fields)):
                issues.append(
                    {
                        "field": key,
                        "status": "missing",
                        "message": f"{spec.label.capitalize()} was not collected.",
                    }
                )
            continue

        status = current.get("status") or "missing"
        value: Any = current.get("value") if status in {"valid", "skipped"} else None
        if status == "conflict":
            value = None
            options = [
                {"value": item.get("value"), "source": item.get("source")}
                for item in current.get("candidates") or []
            ]
            conflicts.append({"field": key, "values": options})
            issues.append(
                {
                    "field": key,
                    "status": "conflict",
                    "message": current.get("message") or f"Two answers for {spec.label} need a decision.",
                }
            )
        elif status == "invalid":
            issues.append(
                {
                    "field": key,
                    "status": "invalid",
                    "message": current.get("message") or f"{spec.label.capitalize()} is not valid.",
                }
            )
        elif status == "missing" or (status != "skipped" and value in (None, "", [])):
            status = "missing"
            value = None
            if spec.required or key == "injuryDetails":
                issues.append(
                    {
                        "field": key,
                        "status": "missing",
                        "message": f"{spec.label.capitalize()} was not collected.",
                    }
                )

        rendered[key] = {
            "value": value,
            "source": current.get("source"),
            "status": status,
        }

    reason = ambiguous_claim(fields)
    if reason:
        issues.append({"field": "anyoneInjured", "status": "needs_review", "message": reason})

    return {
        "sessionId": session_id,
        "revision": revision,
        "outcome": outcome,
        "fields": rendered,
        "conflicts": conflicts,
        "issues": issues,
    }


def should_emit(revision: int, dirty: bool, outcome: str, latest_outcome: str | None) -> bool:
    """A later message files a new revision only when the claim changed, or the caller opts out."""
    if revision == 0 or dirty:
        return True
    return outcome == "optedOut" and latest_outcome != "optedOut"
