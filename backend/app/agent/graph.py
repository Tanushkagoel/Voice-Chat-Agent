"""LangGraph workflow shared by the call and the chat."""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.agent.extract import extract_candidates
from app.agent.llm import extract_with_gemini, phrase_reply
from app.agent.transition import choose_next, interpret_intent, merge_turn
from app.config import Settings
from app.domain.fields import clean_text, is_correction, is_finish_later, is_no, is_opt_out, is_yes

_PROTECTED = {"policyNumber", "contactEmail", "vehicleRegistration", "incidentDate", "anyoneInjured"}


class TurnState(TypedDict, total=False):
    message: str
    channel: str
    today: str
    expecting: str | None
    fields: dict
    pending_action: str | None
    pending: dict | None
    optional_offered: bool
    call_active: bool
    attachments: list
    attachment_error: str | None
    skip_field: str | None
    force_intent: str | None
    intent: str
    correction: bool
    candidates: dict
    prompt_tokens: int
    completion_tokens: int
    fields_out: dict
    changes: dict
    repeats: list
    warnings: list
    resolved: bool
    conflict_field: str | None
    reply: str
    input: dict
    set_pending: bool
    pending_action_out: str | None
    pending_out: dict | None
    finalize: str | None
    voice_notice: str | None
    optional_offered_out: bool
    focus_field: str | None
    revision: int
    dirty: bool
    latest_outcome: str | None


def build_graph(settings: Settings):
    async def interpret(state: TurnState) -> dict[str, Any]:
        message = state.get("message") or ""
        expecting = state.get("expecting")
        force = state.get("force_intent")
        intent = interpret_intent(message, state.get("pending_action"), expecting, force)
        candidates = extract_candidates(message, expecting, date.fromisoformat(state["today"]), state["channel"])
        prompt_tokens = 0
        completion_tokens = 0
        if force is None and settings.gemini_api_key:
            llm, prompt_tokens, completion_tokens = await extract_with_gemini(
                settings, message, date.fromisoformat(state["today"]), expecting
            )
            llm_fields = llm.get("fields") if isinstance(llm.get("fields"), dict) else {}
            for key, value in llm_fields.items():
                if value in (None, "", []) or key in candidates and key in _PROTECTED:
                    continue
                if key in candidates:
                    continue
                if key == "anyoneInjured" and isinstance(value, bool):
                    candidates[key] = "yes" if value else "no"
                else:
                    candidates[key] = clean_text(str(value))
            if intent == "provide" and not candidates and llm.get("intent") in {"opt_out", "finish_later"}:
                intent = str(llm["intent"])

        text = clean_text(message)
        if intent in {"confirm", "deny", "opt_out", "finish_later", "skip_optional"} and len(text.split()) <= 4:
            if force or is_yes(text) or is_no(text) or is_opt_out(text) or is_finish_later(text) or intent == "skip_optional":
                candidates = {}
        return {
            "intent": intent,
            "correction": is_correction(message),
            "candidates": candidates,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }

    async def validate_and_merge(state: TurnState) -> dict[str, Any]:
        merged = merge_turn(
            state.get("fields") or {},
            dict(state.get("candidates") or {}),
            message=state.get("message") or "",
            channel=state["channel"],
            today=date.fromisoformat(state["today"]),
            correction=bool(state.get("correction")),
            attachments=list(state.get("attachments") or []),
            skip_optional=state.get("intent") == "skip_optional" or state.get("skip_field") == "contactPreference",
            pending_action=state.get("pending_action"),
            pending=state.get("pending"),
        )
        return {
            "fields_out": merged["fields"],
            "changes": merged["changes"],
            "repeats": merged["repeats"],
            "warnings": merged["warnings"],
            "resolved": merged["resolved"],
            "conflict_field": merged["conflict_field"],
        }

    async def respond(state: TurnState) -> dict[str, Any]:
        decision = choose_next(
            fields=state.get("fields_out") or {},
            changes=state.get("changes") or {},
            repeats=list(state.get("repeats") or []),
            warnings=list(state.get("warnings") or []),
            resolved=bool(state.get("resolved")),
            conflict_field=state.get("conflict_field"),
            intent=state.get("intent") or "provide",
            channel=state["channel"],
            call_active=bool(state.get("call_active")),
            optional_offered=bool(state.get("optional_offered")),
            pending_action=state.get("pending_action"),
            attachment_error=state.get("attachment_error"),
            revision=int(state.get("revision") or 0),
            dirty=bool(state.get("dirty")),
            latest_outcome=state.get("latest_outcome"),
            fields_changed=bool(state.get("changes")),
        )
        reply = decision["reply"]
        voice_notice = decision["voice_notice"]
        prompt_tokens = int(state.get("prompt_tokens") or 0)
        completion_tokens = int(state.get("completion_tokens") or 0)
        phrased, phrase_prompt, phrase_completion = await phrase_reply(settings, reply)
        if phrased:
            reply = phrased
            if voice_notice:
                voice_notice = phrased
        return {
            "reply": reply,
            "input": decision["input"],
            "set_pending": decision["set_pending"],
            "pending_action_out": decision["pending_action"],
            "pending_out": decision["pending"],
            "finalize": decision["finalize"],
            "voice_notice": voice_notice,
            "optional_offered_out": decision["optional_offered"],
            "focus_field": decision["focus_field"],
            "prompt_tokens": prompt_tokens + phrase_prompt,
            "completion_tokens": completion_tokens + phrase_completion,
        }

    graph = StateGraph(TurnState)
    graph.add_node("interpret", interpret)
    graph.add_node("validate_and_merge", validate_and_merge)
    graph.add_node("respond", respond)
    graph.set_entry_point("interpret")
    graph.add_edge("interpret", "validate_and_merge")
    graph.add_edge("validate_and_merge", "respond")
    graph.add_edge("respond", END)
    return graph.compile()
