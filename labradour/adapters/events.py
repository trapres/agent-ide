"""Validate adapter facts without guessing identity, causality, or coverage."""
import json
import re

KINDS = frozenset({"session.started", "session.ended", "turn.started", "turn.completed",
                   "turn.interrupted", "turn.failed", "adapter.health",
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
    if event.get("boundary") not in (None, "before", "after", "prompt", "turn-end", "session-start", "session-end", "interrupt"):
        raise ValueError("invalid boundary")
    if kind not in KINDS:
        result.update(kind="provider.unknown", original_kind=kind)
    # Adapter facts are provider reports, not authenticated proof of causality.
    result["quality"] = {"coverage": "synthetic" if event["provider"] == "fixture" else "provider-reported",
                         "attribution": "unassigned"}
    # Reject NaN/Infinity and other non-JSON values even for in-process callers.
    json.dumps(result, allow_nan=False)
    return result


def coverage():
    from .providers import MAPPINGS
    categories = ["session", "prompt_turn", "read", "edit", "shell", "failure", "denial",
                  "interruption", "parallel_calls", "subagents", "existing_hook_composition"]
    result = {"schema_version": 1, "providers": {
        provider: {category: {"status": "unverified", "evidence": None} for category in categories}
        for provider in ("claude", "codex")},
        "foundation": {"status": "synthetic-tested", "evidence": "tests/test_adapter_foundation.py"},
        "adapters": {provider: {"mapping": "documentation-fixture-tested", "hooks": list(hooks),
                     "native_delivery": "unverified", "fixture": "tests/fixtures/provider-hooks.json"}
                     for provider, hooks in MAPPINGS.items()},
        "limitations": {"claude": "PermissionDenied covers auto-mode denials only; turn IDs may be absent.",
                        "codex": "No dedicated failure/denial hook; opaque results do not establish success. Hosted tools may bypass hooks.",
                        "boundaries": "Live scans and concurrent hooks do not establish exclusive causality; receipt timeouts are gaps."},
        "note": "Native observations are historical measurements at the stated versions/platform, not full acceptance or current installed-client compatibility."}
    measured = {
        "claude": ("2.1.234", {"session", "prompt_turn", "read", "edit", "shell", "failure", "denial", "parallel_calls", "subagents", "existing_hook_composition"}),
        "codex": ("0.160.0", {"session", "prompt_turn", "read", "edit", "shell", "interruption", "parallel_calls", "subagents", "existing_hook_composition"})}
    for provider, (version, categories) in measured.items():
        for category in categories:
            result["providers"][provider][category] = {
                "status": "observed-limited", "evidence": "docs/broad-native-acceptance.json" if category == "parallel_calls" or (provider == "claude" and category == "denial") else "docs/provider-native-acceptance.json",
                "version": version, "platform": "macOS 26.7.1 arm64"}
        result["adapters"][provider]["native_delivery"] = "observed-at-tested-version"
        result["adapters"][provider]["native_fixture"] = "tests/fixtures/native-provider-hooks.json"
    result["additional_measurements"] = {
        "codex_linux": {"version": "0.160.0", "platform": "Docker Linux aarch64",
                        "categories": ["session", "prompt_turn", "read", "edit", "shell", "existing_hook_composition"],
                        "evidence": "docs/broad-native-acceptance.json",
                        "limitation": "Namespace sandbox unavailable; harmless commands individually approved in native UI; opaque outcomes remain unknown."}}
    result["limitations"].update(
        native_acceptance="Human terminal fidelity and authenticated Linux Claude remain open; Linux Codex is measured separately with Docker sandbox limitations. See docs/Phase2TestGuide.md.",
        measured_claude="Policy-hook denial and interrupted sleep had no terminal tool callback; parent actor and turn IDs were absent. Concurrent actors shared an external edit without ownership claims. Untrusted startup cleanup passed after releasing the PTY before final reap.",
        measured_codex="Shell exit 7 arrived as an opaque completion response and stayed unknown. Approval had no call ID. SessionEnd was not observed in the main run.")
    return result
