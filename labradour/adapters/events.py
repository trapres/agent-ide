"""Validate adapter facts without guessing identity, causality, or coverage."""
import json
import re

KINDS = frozenset({"session.started", "session.ended", "turn.started", "turn.completed",
                   "tool.requested", "tool.started", "tool.completed", "tool.failed",
                   "tool.denied", "tool.interrupted", "approval.requested",
                   "actor.started", "actor.ended", "provider.unknown"})
PROVIDERS = frozenset({"generic", "fixture", "claude", "codex"})
IDENTITY_FIELDS = ("provider_session_id", "turn_id", "actor_id", "parent_actor_id", "call_id")


def identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("invalid " + name)
    return value


def validate(event):
    if not isinstance(event, dict) or type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        raise ValueError("adapter event requires schema_version 1")
    identifier(event.get("event_id"), "event_id")
    if not isinstance(event.get("provider"), str) or event["provider"] not in PROVIDERS:
        raise ValueError("unsupported provider")
    kind = event.get("kind")
    if not isinstance(kind, str) or not kind or len(kind) > 128:
        raise ValueError("invalid kind")
    result = dict(event)
    for field in IDENTITY_FIELDS:
        value = event.get(field)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 256):
            raise ValueError("invalid " + field)
        result[field] = value
    if not isinstance(event.get("payload", {}), dict):
        raise ValueError("payload must be an object")
    result["payload"] = event.get("payload", {})
    if kind not in KINDS:
        result.update(kind="provider.unknown", original_kind=kind)
    # Adapter facts are provider reports, not authenticated proof of causality.
    result["quality"] = {"coverage": "synthetic" if event["provider"] == "fixture" else "provider-reported",
                         "attribution": "unassigned"}
    # Reject NaN/Infinity and other non-JSON values even for in-process callers.
    json.dumps(result, allow_nan=False)
    return result


def coverage():
    categories = ["session", "prompt_turn", "read", "edit", "shell", "failure", "denial",
                  "interruption", "parallel_calls", "subagents", "existing_hook_composition"]
    return {"schema_version": 1, "providers": {
        provider: {category: {"status": "unverified", "evidence": None} for category in categories}
        for provider in ("claude", "codex")},
        "foundation": {"status": "synthetic-tested", "evidence": "tests/test_adapter_foundation.py"},
        "note": "Successful launches and synthetic fixtures do not establish native provider coverage."}
