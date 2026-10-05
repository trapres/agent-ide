"""Native hook mappings; documentation-derived, not a coverage guarantee."""
import hashlib
import uuid

from .collector import encode
from .events import validate

COMMON = {"SessionStart": "session.started", "SessionEnd": "session.ended",
          "UserPromptSubmit": "turn.started", "Stop": "turn.completed",
          "PreToolUse": "tool.requested", "PostToolUse": "tool.completed",
          "PermissionRequest": "approval.requested", "SubagentStart": "actor.started",
          "SubagentStop": "actor.ended"}
MAPPINGS = {"claude": dict(COMMON, PostToolUseFailure="tool.failed", PermissionDenied="tool.denied",
                           StopFailure="turn.failed"),
            "codex": dict(COMMON, Interrupt="turn.interrupted")}
BOUNDARIES = {"SessionStart": "session-start", "SessionEnd": "session-end",
              "UserPromptSubmit": "prompt", "Stop": "turn-end", "PreToolUse": "before",
              "PostToolUse": "after", "PostToolUseFailure": "after", "Interrupt": "interrupt",
              "StopFailure": "turn-end"}


def normalize(provider, raw):
    if provider not in MAPPINGS or not isinstance(raw, dict):
        raise ValueError("unsupported provider or payload")
    hook = raw.get("hook_event_name")
    if not isinstance(hook, str) or not hook or len(hook) > 128:
        raise ValueError("missing hook event name")
    kind = MAPPINGS[provider].get(hook, "provider.unknown")
    outcome = "unknown"
    if provider == "claude" and hook == "PostToolUseFailure":
        kind = "tool.interrupted" if raw.get("is_interrupt") is True else "tool.failed"
        outcome = "interrupted" if kind == "tool.interrupted" else "failed"
    elif hook == "PostToolUse" and provider == "claude":
        outcome = "succeeded"
    elif hook == "PostToolUse" and provider == "codex":
        # Responses are arbitrary JSON/model-facing output. Never parse prose
        # as a stable exit-code schema or equate receipt with successful Bash.
        response = raw.get("tool_response")
        if isinstance(response, dict):
            code = response.get("exit_code")
            if response.get("isError") is True or (type(code) is int and code != 0):
                kind, outcome = "tool.failed", "failed"
            elif type(code) is int and code == 0:
                outcome = "succeeded"
    elif hook == "PermissionDenied" and provider == "claude":
        outcome = "denied"
    # Stable identities permit transport retry deduplication. With no provider
    # event/call/turn identity, keep separate observations rather than collapse
    # identical prompts or session notifications.
    stable = raw.get("event_id") or raw.get("tool_use_id") or raw.get("turn_id")
    event_id = hashlib.sha256(encode({"provider": provider, "raw": raw})).hexdigest() if stable else uuid.uuid4().hex
    return validate({"schema_version": 1, "event_id": event_id, "provider": provider,
                     "kind": kind, "provider_session_id": raw.get("session_id"),
                     "turn_id": raw.get("turn_id"), "actor_id": raw.get("agent_id"),
                     "parent_actor_id": raw.get("parent_agent_id"), "call_id": raw.get("tool_use_id"),
                     "payload": {"hook_event_name": hook, "tool": raw.get("tool_name"), "outcome": outcome,
                                 "lifecycle_basis": "hook-observation", "raw": raw},
                     "boundary": BOUNDARIES.get(hook) if hook in MAPPINGS[provider] else None})
