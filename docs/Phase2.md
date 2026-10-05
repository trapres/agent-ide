# Phase 2: native adapters

Date: 2026-10-05. Status: **adapter foundation implemented; native provider integration and correlation remain open**.

The slices and follow-ups are tracked in [Tasks.md](../Tasks.md). Phase 1's recorder remains the single writer for saved facts and checkpoints. This slice introduces a transport and provider-neutral event contract without changing existing provider hook configuration or assigning filesystem observations to tools.

## Available foundation

- Validated v1 adapter facts with nullable provider session, turn, actor/parent actor, and call IDs. Unknown kinds become `provider.unknown`, retaining their original kind and payload. Unknown optional fields are preserved; invalid versions, identities, non-object payloads, and non-JSON numbers are rejected.
- A private Unix-domain socket and per-launch HMAC credentials, bound to the canonical workspace identity. The child receives connection details through environment variables; tokens are not written to journal/configuration. Collection threads start after the PTY fork, and recording baseline/startup reconciliation completes before releasing the child.
- A signed, launch-specific fallback spool outside the recording store. Parallel callbacks use a file lock and atomic, fsynced event files. Socket acknowledgements mean **retained in the spool**, not committed to the journal. The recorder removes a spool file only after its single-writer journal transaction succeeds.
- Bounded messages (1 MiB including framing), spool (128 events / 8 MiB), connection deadlines, 32-event ingest batches, and explicit rejection facts. Retry IDs are scoped to a launch; identical retries deduplicate. Conflicting IDs preserve the original fact and record a conflict. Oversized journal payloads retain normalized kind/provider and a content digest while marking truncation.
- Deterministic synthetic events and tests for authenticated delivery, rejection, fallback/tampering, stale launch isolation, concurrency, retry deduplication/conflicts, limits, shutdown drain, recording suspension, and actual curses/PTY launch integration.

No synchronous snapshot boundary or provider normalization is implemented yet. A `tool.started` fact does not itself trigger or wait for a capture.

## Try the fixture

From the repository root in an interactive terminal:

```sh
export LABRADOUR_ADAPTER_TEST=$(mktemp -d /tmp/labradour-adapter-test.XXXXXX)
export LABRADOUR_ADAPTER_FIXTURE="$PWD/labradour/adapters/fixture.py"
mkdir "$LABRADOUR_ADAPTER_TEST/workspace"
python3 -m labradour run --workspace "$LABRADOUR_ADAPTER_TEST/workspace" \
  --record "$LABRADOUR_ADAPTER_TEST/recording" --collector -- \
  python3 "$LABRADOUR_ADAPTER_FIXTURE"
```

The child reports synthetic delivery and exits. Review remains open until Ctrl-Q. Activity contains `adapter.event` records; their JSON payloads carry normalized kinds including read completion, failure, denial, and an unknown future event. These are independent facts, not the future grouped action/effect UI. After closing:

```sh
python3 -m labradour history "$LABRADOUR_ADAPTER_TEST/recording"
python3 -m labradour adapter-coverage
```

Use a session ID with `history --session ID` to inspect the saved facts. The callback's `session.ended` stays nested inside an `adapter.event`; it cannot complete the host recorder session. `adapter.foundation` records the launch ID and the coverage matrix at startup.

`--collector` currently requires `--record`. It is an opt-in foundation endpoint for adapter clients. Existing `--hooks claude` / `--hooks codex` still use the Phase 0 raw spool; adding `--collector` does not migrate those callbacks. Their facts remain labeled `provider.event` from `hook-spool`, distinct from authenticated adapter facts. Normal hook trust and authorization behavior remains native to the provider.

## Event and client contract

An adapter calls `labradour.adapters.collector.submit(event)` with environment inherited from its launch:

```json
{
  "schema_version": 1,
  "event_id": "stable-callback-id",
  "provider": "generic",
  "kind": "tool.completed",
  "provider_session_id": "provider-session",
  "turn_id": null,
  "actor_id": null,
  "parent_actor_id": null,
  "call_id": "provider-call",
  "payload": {"tool": "Read", "result": {"summary": "captured response"}}
}
```

Supported kinds: `session.started`, `session.ended`, `turn.started`, `turn.completed`, `tool.requested`, `tool.started`, `tool.completed`, `tool.failed`, `tool.denied`, `tool.interrupted`, `approval.requested`, `actor.started`, `actor.ended`, and `provider.unknown`. Providers are `generic`, `fixture`, `claude`, and `codex`. Declaring a provider is not evidence that its native integration works.

Event IDs contain 1–128 ASCII letters/digits or `_.:-`. Retries must reuse the ID and identical fact; separate invocations need separate IDs. Provider identity fields are nullable nonempty strings up to 256 characters; missing IDs stay unknown. Do not invent a call ID from arrival order. Unknown event kinds and additional fields are kept for future mappings. Quality is assigned by the contract: synthetic or provider-reported coverage, with attribution unassigned.

The socket uses one newline-terminated JSON request and response per connection. Request:

```text
{"body":{"launch_id":...,"workspace_id":...,"event":...},"mac":...}
```

The MAC is HMAC-SHA256 over UTF-8 JSON with sorted keys, compact separators, and finite JSON values, using the inherited token. The Python client supplies framing/signing; provider wrappers should use it. The spool contains the same signed envelope. Wrong launch/workspace/token, invalid frames, malformed events, conflicting pending IDs, and full spools are rejected. Explicit server rejection does not fall back to disk. Temporary socket I/O failure uses the signed spool in the same launch.

The transport returns an ingestion receipt only; it does not return provider decisions, approval answers, or model context. The API raises on unavailable/full storage. The next slice's hook wrappers must catch/report those failures without changing tool authorization or results. An offline fallback that itself cannot write has no durable receipt; never claim that event was captured.

Journal records use host workspace/session identity and `authenticated-collector` source. The provider's session identity remains in the nested fact. A host content digest detects conflicting retries even after spool removal. Recorder sequence defines ingestion order; concurrent callback order is not causal order. Generic lifecycle facts do not establish exclusive attribution or complete provider coverage.

## Provider coverage matrix

`adapter-coverage` emits the same versioned matrix saved at authenticated collector startup:

| Category | Claude | Codex |
| --- | --- | --- |
| Session / prompt-turn lifecycle | Unverified | Unverified |
| Read / edit / shell | Unverified | Unverified |
| Failure / denial / interruption | Unverified | Unverified |
| Parallel calls / subagents | Unverified | Unverified |
| Existing-hook composition | Unverified | Unverified |

Basic native launches were user-verified previously. That does not establish these event categories. Foundation fixtures test the canonical contract and transport; they do not use real provider payloads or establish native delivery. The integration slice must add recorded provider fixtures, source/version provenance, and measured evidence before updating cells. Unsupported categories should be explicit rather than silently treated as covered.

## Lifecycle and limits

The host stops the child process group, stops accepting socket callbacks, drains pending adapter batches, and then performs the normal final capture/session close. Rejected envelopes create `collector.rejected`; conflicting durable IDs create `collector.conflict`. Such sessions finish with capture gaps. File changes remain external/unknown.

If recording stops for quota/storage failure, pending files remain unacknowledged. The bounded spool can then fill and callers fail visibly. It is separate from the recorder's retained-byte budget and needs its own disk space. `--events` can retain the spool directory for diagnosis; default temporary events are removed on launcher exit. Tokens are ephemeral: a subsequent launch cannot authenticate/replay an earlier launch's pending spool. Crash recovery preserves journaled history and reports interruption, but does not recover unjournaled adapter facts. Reliable cross-launch spool recovery is a follow-up if required.

Authentication isolates launch traffic; it is not proof that an event's declared actor is truthful or an operating-system sandbox. Child processes receive the launch credentials and share that trust. No credential/connection details should be put into model prompts or logs. Readers reject symlinks and non-regular spool files, including FIFOs. The normal recorder's writer lock, capture policy, truncation, and storage limits continue to apply.

## Verification and remaining slices

All **70 tests pass on macOS/Python 3.9.6 and Linux/Python 3.11 in the existing acceptance container**, including 13 new adapter-foundation tests. Both runs include the actual curses/PTY fixture launch. This accepts the generic foundation in its synthetic scope; it does not accept native provider delivery.

Run `python3 -m unittest discover -s tests -v`. Unix socket binding requires an environment that permits local sockets; this workspace's restrictive execution sandbox required approval to run socket tests outside it. Linux reproduction uses the existing [acceptance container](RecorderAcceptance.md#reproduce).

Next: Claude and Codex mappings and real hook composition/trust, authenticated hook wrappers, lifecycle/tool-boundary capture, and delivery health. Then correlation and acceptance cover failures, denials, interrupted/missing outcomes, parallel calls, subagents, and human native terminal checks. Historical visualization/plugin implementation remains later work; see [VizApi.md](VizApi.md).
