"""Read-only, deterministic correlation of journal facts; never assigns file ownership."""
import hashlib
import json


TERMINAL = {"tool.completed": "completed", "tool.failed": "failed",
            "tool.denied": "denied", "tool.interrupted": "interrupted"}


def stable_id(category, identity):
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return category + ":" + hashlib.sha256(raw.encode()).hexdigest()


def project(records, session_status="running"):
    """Replay one host session. Sequence means journal order, not provider causal order.

    Explicit call IDs are scoped by launch, provider, provider session, actor and
    turn. Missing actor/turn IDs remain unknown; we never join them to known IDs.
    A checkpoint window is evidence of temporal proximity, not exclusive authorship.
    """
    records = sorted(records, key=lambda r: r["sequence"])
    snapshots = {r["event_id"]: r for r in records if r["kind"] == "snapshot.completed"}
    facts = [r for r in records if r["kind"] == "adapter.event"]
    boundaries = {}
    gaps = []
    gap_sequences = []
    late = set()
    closed = session_status != "running" or any(r["kind"] in
                ("session.completed", "session.interrupted") for r in records)
    for r in records:
        p = r["payload"]
        if r["kind"] == "adapter.boundary":
            launch = p.get("launch_id")
            # Older journals encoded launch scope in the receipt ID.
            if launch is None and r["event_id"].startswith("boundary:"):
                launch = r["event_id"].split(":", 2)[1]
            boundaries[(r["session_id"], launch, p.get("adapter_event_id"))] = r
        if p.get("payload_truncated") or r["kind"] in (
                "collector.invalid", "collector.conflict", "collector.rejected", "watcher.overflow",
                "snapshot.failed", "snapshot.interrupted", "collector.overflow", "collector.failed",
                "storage.limit", "storage.failed", "process.cleanup-gap"):
            gaps.append({"event_id": r["event_id"], "kind": r["kind"], "reason": "incomplete evidence"})
            gap_sequences.append(r["sequence"])
        if r["kind"] == "adapter.boundary" and not p.get("completed"):
            gaps.append({"event_id": r["event_id"], "kind": r["kind"], "reason": "unavailable boundary"})
            gap_sequences.append(r["sequence"])
        if r["kind"] == "adapter.event" and p.get("kind") == "adapter.health":
            detail = p.get("payload", {})
            late.add((r["session_id"], p.get("launch_id"), detail.get("related_event_id")))
            gaps.append({"event_id": r["event_id"], "kind": "adapter.health", "reason": "delivery or receipt gap"})
            gap_sequences.append(r["sequence"])

    actions = {}
    actors = {}
    lifecycle = []
    scoped_interrupts = set()
    for r in facts:
        p = r["payload"]
        scope = (r["session_id"], p.get("launch_id"), p.get("provider"),
                 p.get("provider_session_id"), p.get("actor_id"), p.get("turn_id"))
        if p.get("kind", "").startswith(("session.", "turn.", "actor.")):
            lifecycle.append({"event_id": r["event_id"], "kind": p["kind"], "scope": list(scope),
                              "quality": "provider-reported hook observation"})
        if p.get("kind") == "turn.interrupted":
            scoped_interrupts.add(scope)
        if p.get("actor_id"):
            actor_scope = scope[:5]
            actor_linked = all(scope[:4])
            actor_identity = actor_scope if actor_linked else (r["session_id"], r["event_id"], p["actor_id"])
            actor_id = stable_id("actor", actor_identity)
            actor = actors.setdefault(actor_id, {"id": actor_id, "scope": list(actor_scope),
                                               "identity_quality": "explicit-actor-scope" if actor_linked else "unlinked-observation",
                                               "parent_actor_ids": [], "observations": []})
            actor["observations"].append(r["event_id"])
            parent = p.get("parent_actor_id")
            if parent:
                parent_identity = scope[:4] + (parent,) if actor_linked else (r["session_id"], r["event_id"], parent)
                parent_id = stable_id("actor", parent_identity)
                if parent_id not in actor["parent_actor_ids"]:
                    actor["parent_actor_ids"].append(parent_id)
        if p.get("kind") not in set(TERMINAL) | {"tool.requested", "tool.started", "approval.requested"}:
            continue
        linked = bool(p.get("launch_id") and p.get("provider") and
                      p.get("provider_session_id") and p.get("call_id"))
        identity = scope + (p.get("call_id"),) if linked else (r["session_id"], r["event_id"])
        aid = stable_id("action", identity)
        action = actions.setdefault(aid, {"id": aid, "scope": list(scope), "call_id": p.get("call_id"),
                  "identity_quality": "explicit-call-scope" if linked else "unlinked-observation",
                  "state": "requested", "outcomes": [], "observations": [], "boundaries": [],
                  "tool": p.get("payload", {}).get("tool"), "result_outcomes": [],
                  "input_refs": [], "output_refs": [], "error_refs": [], "notes": [], "effect_ids": [], "_scope": scope, "_requests": [],
                  "_ends": [], "_starts": [], "_approval": False})
        action["observations"].append(r["event_id"])
        kind = p["kind"]
        if kind in TERMINAL:
            outcome = TERMINAL[kind]
            if outcome not in action["outcomes"]:
                action["outcomes"].append(outcome)
            action["_ends"].append(r["sequence"])
        elif kind == "tool.requested":
            action["_requests"].append(r["sequence"])
        elif kind == "tool.started":
            action["_starts"].append(r["sequence"])
        else:
            action["_approval"] = True
        detail = p.get("payload", {})
        raw = detail.get("raw", {})
        if not isinstance(raw, dict):
            raw = {}
        outcome = detail.get("outcome", "unknown")
        if kind in TERMINAL and outcome not in action["result_outcomes"]:
            action["result_outcomes"].append(outcome)
        for field, destination in (("tool_input", "input_refs"), ("error", "error_refs")):
            if field in raw:
                action[destination].append({"event_id": r["event_id"], "field": "payload.payload.raw." + field,
                                            "quality": "provider-reported"})
        if "tool_response" in raw:
            action["output_refs"].append({"event_id": r["event_id"], "field": "payload.payload.raw.tool_response",
                                          "quality": "provider-reported"})
        receipt_key = (r["session_id"], p.get("launch_id"), p.get("event_id"))
        receipt = boundaries.get(receipt_key)
        if receipt:
            b = receipt["payload"]
            snap = snapshots.get(b.get("snapshot_event_id"))
            usable = bool(b.get("completed") and snap and snap["session_id"] == r["session_id"]
                          and r["sequence"] < snap["sequence"] < receipt["sequence"]
                          and b.get("checkpoint") == snap["payload"].get("commit") and receipt_key not in late and
                          not b.get("payload_truncated") and not snap["payload"].get("payload_truncated"))
            action["boundaries"].append({"event_id": receipt["event_id"], "phase": b.get("phase"),
                "checkpoint": b.get("checkpoint"), "snapshot_event_id": b.get("snapshot_event_id"),
                "snapshot_sequence": snap["sequence"] if snap else None,
                "usable": usable, "quality": b.get("quality")})
        elif p.get("boundary"):
            action["notes"].append("missing boundary receipt: " + r["event_id"])
        if p.get("payload_truncated"):
            action["notes"].append("provider payload truncated")

    windows = []
    for action in actions.values():
        outcomes = action["outcomes"]
        action["state"] = ("inconsistent" if len(outcomes) > 1 else outcomes[0]) if outcomes else (
            "incomplete" if closed else "running" if action["_starts"] else
            "awaiting-approval" if action["_approval"] else "requested")
        if action["_scope"] in scoped_interrupts:
            action["notes"].append("turn interruption observed; per-tool outcome remains independent")
        if action["_ends"] and (not action["_requests"] or min(action["_ends"]) < min(action["_requests"])):
            action["notes"].append("outcome observed without an earlier request in journal order")
        if action["identity_quality"] == "unlinked-observation":
            action["notes"].append("missing identity; observation not joined to other calls")
        before = [b for b in action["boundaries"] if b["phase"] == "before"]
        after = [b for b in action["boundaries"] if b["phase"] == "after"]
        if (len(before) == 1 and len(after) == 1 and before[0]["usable"] and after[0]["usable"]
                and before[0]["snapshot_sequence"] < after[0]["snapshot_sequence"]
                and action["state"] != "inconsistent" and action["_ends"]
                and min(action["_ends"]) > before[0]["snapshot_sequence"]
                and action["identity_quality"] == "explicit-call-scope"
                and not any(before[0]["snapshot_sequence"] < s <= after[0]["snapshot_sequence"] for s in gap_sequences)):
            windows.append((before[0]["snapshot_sequence"], after[0]["snapshot_sequence"], action))
            action["correlation_quality"] = "boundary-correlated"
        else:
            action["correlation_quality"] = "incomplete-evidence"
        if not action["output_refs"]:
            action["notes"].append("no recorded tool output")

    effects = []
    for snapshot in snapshots.values():
        for change in snapshot["payload"].get("changes", []):
            eid = stable_id("effect", (snapshot["session_id"], snapshot["event_id"], change["path"], change["operation"]))
            candidates = [a for start, end, a in windows if start < snapshot["sequence"] <= end]
            effect = {"id": eid, "snapshot_event_id": snapshot["event_id"], "path": change["path"],
                      "operation": change["operation"], "before_commit": snapshot["payload"].get("before_commit"),
                      "checkpoint": snapshot["payload"].get("commit"), "candidate_action_ids": [a["id"] for a in candidates],
                      "capture_quality": snapshot["payload"].get("quality", "unknown"),
                      "attribution": "external-or-unknown", "quality": "overlapping-calls" if len(candidates) > 1 else
                      "boundary-correlated" if candidates else "unassigned"}
            effects.append(effect)
            for action in candidates:
                action["effect_ids"].append(eid)
    for action in actions.values():
        for key in list(action):
            if key.startswith("_"):
                del action[key]
        action["notes"] = list(dict.fromkeys(action["notes"]))
    return {"schema_version": 1, "session_status": session_status, "actions": list(actions.values()),
            "actors": list(actors.values()), "effects": effects, "gaps": gaps,
            "lifecycle": lifecycle,
            "limitations": ["Candidate calls describe checkpoint windows, never exclusive file ownership.",
                            "Unknown actor or turn IDs are not inferred; journal order is not provider causal order.",
                            "Tool completion describes a hook observation, not background process completion.",
                            "Stop is a completion candidate; other provider hooks can continue the turn."]}
