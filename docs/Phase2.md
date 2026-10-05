# Phase 2: native adapters

Date: 2026-10-05. Status: **adapter/correlation implementation and core macOS native delivery/composition measured; broader native gate remains open**.

The slices and follow-ups are tracked in [Tasks.md](../Tasks.md). Phase 1's recorder remains the single writer for saved facts and checkpoints. This slice introduces a transport and provider-neutral event contract without changing existing provider hook configuration or assigning filesystem observations to tools.

## Available foundation

- Validated v1 adapter facts with nullable provider session, turn, actor/parent actor, and call IDs. Unknown kinds become `provider.unknown`, retaining their original kind and payload. Unknown optional fields are preserved; invalid versions, identities, non-object payloads, and non-JSON numbers are rejected.
- A private Unix-domain socket and per-launch HMAC credentials, bound to the canonical workspace identity. The child receives connection details through environment variables; tokens are not written to journal/configuration. Collection threads start after the PTY fork, and recording baseline/startup reconciliation completes before releasing the child.
- A signed, launch-specific fallback spool outside the recording store. Parallel callbacks use a file lock and atomic, fsynced event files. Socket acknowledgements mean **retained in the spool**, not committed to the journal. The recorder removes a spool file only after its single-writer journal transaction succeeds.
- Bounded messages (1 MiB including framing), spool (128 events / 8 MiB), connection deadlines, 32-event ingest batches, and explicit rejection facts. Retry IDs are scoped to a launch; identical retries deduplicate. Conflicting IDs preserve the original fact and record a conflict. Oversized journal payloads retain normalized kind/provider and a content digest while marking truncation.
- Deterministic synthetic events and tests for authenticated delivery, rejection, fallback/tampering, stale launch isolation, concurrency, retry deduplication/conflicts, limits, shutdown drain, recording suspension, and actual curses/PTY launch integration.

Native normalization and bounded synchronous capture receipts are now implemented. Generic facts without a `boundary` field still only record observations.

## Claude and Codex integration

Recording with provider hooks automatically enables authenticated collection:

```sh
python3 -m labradour run --workspace /path/to/disposable-workspace \
  --record /path/to/claude-recording --hooks claude -- claude
python3 -m labradour run --workspace /path/to/disposable-workspace \
  --record /path/to/codex-recording --hooks codex -- codex
```

Contracts were checked against [Claude hooks](https://code.claude.com/docs/en/hooks) and [Codex hooks](https://learn.chatgpt.com/docs/hooks) on October 5, 2026. The [provider fixtures](../tests/fixtures/provider-hooks.json) have source provenance and are constructed examples, not captured native sessions.

| Hook | Normalized observation |
| --- | --- |
| SessionStart / SessionEnd | session.started / session.ended |
| UserPromptSubmit / Stop | turn.started / turn.completed |
| PreToolUse | tool.requested, without asserting execution began |
| PostToolUse | tool.completed; explicit Codex error/exit fields can report tool.failed |
| PermissionRequest | approval.requested |
| SubagentStart / SubagentStop | actor.started / actor.ended |
| Claude PostToolUseFailure | tool.failed or tool.interrupted |
| Claude PermissionDenied | tool.denied, with auto-mode scope |
| Claude StopFailure | turn.failed |
| Codex Interrupt | turn.interrupted |

Original input stays in `payload.raw`. Missing call/turn/actor identities remain null; no Claude turn ID is invented. Opaque Codex output retains unknown outcome; textual exit messages are not parsed as a stable schema. A returned invocation need not mean a background process finished. Stop is an observed candidate end and other hooks may cause continuation. Neither provider supplies a complete denial trail. [Claude semantics](https://code.claude.com/docs/en/hooks), [Codex semantics](https://learn.chatgpt.com/docs/hooks).

### Configuration and trust

Claude's explicit CLI JSON/file settings are composed into a private temporary file: hook arrays append and other options remain present. Original files are untouched. Codex receives an inline launch hook layer and `--no-daemon`; other arguments are preserved. Explicit Codex CLI `hooks` overrides are refused rather than silently discarding hooks; keep those definitions in native settings. Native file layers are left to each provider's documented composition. [Claude settings](https://code.claude.com/docs/en/settings), [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-advanced).

Trust, permission, and sandbox bypass flags are never added. Disabled/managed-only policies remain effective. Commands have stable script/provider arguments; ephemeral credentials/spool paths stay in the child environment. Native review may still be required after script/definition changes. Actual file-layer composition and trust behavior remain a manual gate, rather than an implication of our settings-composition tests.

### Boundaries and health

Session, prompt/turn, and before/after tool hooks request forced-read snapshots. After spool acknowledgement, the hook polls an authenticated receipt for up to 1.5 seconds. The single writer journals the fact, captures, and saves `adapter.boundary` with phase, checkpoint, snapshot event ID, and quality before reporting completion. Retries return original boundary evidence rather than scanning later workspace content. Polling does not block the socket server on the capture.

Scans remain live and can overlap other calls/writers. They establish boundary-correlated observations, not atomic states or exclusive attribution. On timeout the provider continues; a late capture cannot establish a true before-state. An attempted `adapter.health` fact links the affected event/phase. Inspect it when reviewing intervals. Receipt completion includes partial captures and does not imply full coverage.

The wrapper exits zero with no stdout decisions/context on malformed input, transport/capture failure, or timeout. It emits a short stderr diagnostic and attempts a health fact. Full/unwritable spools can prevent even that fact; providers also enforce their three-second handler deadline.

The footer shows callback count or `awaiting delivery/trust`, plus known gaps when space permits. Activity shows normalized kinds; Visualization remains JSON cards. `adapter.configured` records startup expectations; `adapter.delivery` records shutdown counts/gaps. No delivery yields `completed-with-gaps`. Counts are observations, not complete coverage, and summaries obey journal truncation limits.

### Native acceptance still required

The older [startup-only probe](provider-startup-before-pty-release.json) measured unanswered dialogs and no callbacks; its Claude quit case recorded a cleanup gap. Subsequent real native sessions reviewed normal trust dialogs, delivered tool/subagent callbacks and project observers, and saved completed recordings without cleanup gaps. The measured summaries are in [provider-native-acceptance.json](provider-native-acceptance.json); the broad gate remains open for the checks listed in [Phase2TestGuide.md](Phase2TestGuide.md).

Complete these checks in a normal terminal:

1. Inspect/trust the generated hook through native controls in a disposable workspace.
2. Check that an existing harmless hook runs alongside Labradour without settings rewrites.
3. Exercise read/edit/shell, failure, approvals/denial, Ctrl-C, resize, and normal quit; inspect facts/receipts and health.
4. Confirm exclusions and intermediate reverts, and preserve uncertainty for overlapping calls/missing outcomes.

Reproduce startup-only observation with `python3 tools/provider_startup_probe.py --seconds 8 --output /tmp/provider-startup.json`. An unanswered native dialog is left unanswered; no tool call is exercised.

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

`--collector` requires `--record` for generic adapter clients. With recording, `--hooks claude` / `--hooks codex` enable it automatically and use authenticated normalization. Without recording, these options retain Phase 0 raw spool behavior. Legacy facts stay labeled `provider.event` from `hook-spool`, distinct from authenticated adapter facts.

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
| Session / prompt-turn lifecycle | Observed at tested version | Observed at tested version |
| Read / edit / shell | Observed at tested version | Observed at tested version |
| Failure / denial / interruption | Failure observed; denial/interruption outcome limits | Opaque failure result; approval/Interrupt observed; no dedicated denial |
| Parallel calls / subagents | Distinct Claude actors with overlapping writes/external edit measured | Distinct Codex actors with overlapping writes/external edit measured |
| Existing-hook composition | Project and explicit CLI settings observed | Project and isolated user/launch layers observed |

Basic native launches were user-verified previously. That does not establish these event categories. Foundation fixtures test the canonical contract and transport; they do not use real provider payloads or establish native delivery. Captured native fixtures and version/platform-scoped summaries now support the observed cells; the broader gate still requires the remaining manual cases. Unsupported categories should be explicit rather than silently treated as covered.

## Lifecycle and limits

The host stops the child process group, stops accepting socket callbacks, drains pending adapter batches, and then performs the normal final capture/session close. Rejected envelopes create `collector.rejected`; conflicting durable IDs create `collector.conflict`. Such sessions finish with capture gaps. File changes remain external/unknown.

If recording stops for quota/storage failure, pending files remain unacknowledged. The bounded spool can then fill and callers fail visibly. It is separate from the recorder's retained-byte budget and needs its own disk space. `--events` can retain the spool directory for diagnosis; default temporary events are removed on launcher exit. Tokens are ephemeral: a subsequent launch cannot authenticate/replay an earlier launch's pending spool. Crash recovery preserves journaled history and reports interruption, but does not recover unjournaled adapter facts. Reliable cross-launch spool recovery is a follow-up if required.

Authentication isolates launch traffic; it is not proof that an event's declared actor is truthful or an operating-system sandbox. Child processes receive the launch credentials and share that trust. No credential/connection details should be put into model prompts or logs. Readers reject symlinks and non-regular spool files, including FIFOs. The normal recorder's writer lock, capture policy, truncation, and storage limits continue to apply.

## Verification and remaining slices

The integration slice had **80 tests passing on macOS/Python 3.9.6 and Linux/Python 3.11 in the existing acceptance container**, including 13 foundation and 10 provider integration tests. Both runs include curses/PTY launches. Provider tests cover documentation-derived mappings, settings composition, silent observation-only errors, forced before/after edit/revert captures, retries, receipt authentication/concurrency, timeout health, injected disk failure, absent delivery, and launcher wiring through a fake CLI. This accepts the implementation in its synthetic scope; real native delivery remains unverified.

Run `python3 -m unittest discover -s tests -v`. Unix socket binding requires an environment that permits local sockets; this workspace's restrictive execution sandbox required approval to run socket tests outside it. Linux reproduction uses the existing [acceptance container](RecorderAcceptance.md#reproduce).

Correlation and fixture acceptance are now implemented below. Real delivery/composition/trust and human terminal checks remain open. Historical visualization/plugin implementation remains later work; see [VizApi.md](VizApi.md).

## Correlation and acceptance slice

`python3 -m labradour actions RECORDING --session SESSION_ID` produces a deterministic, read-only JSON projection from the saved journal. It does not add a second database, mutate the workspace, follow provider transcript paths, or require the original workspace to exist. Stable SHA-256 IDs derive from explicit scope and immutable journal references; retention removes whole sessions as before. This internal projection is a foundation for Phase 4, not an implemented VizApi plugin endpoint.

Calls are scoped by host session, launch, provider, provider session, actor, turn, and call ID. Unknown actor/turn values remain unknown and separate from known values. Missing launch/provider-session/call identity leaves independent observations. States include requested, awaiting-approval, explicitly started/running, completed, failed, denied, interrupted, incomplete after host close, and inconsistent for conflicting terminal facts. Terminal facts arriving before requests retain that ordering note. Provider result outcomes remain separate from lifecycle states. Input/output/error references point into preserved raw facts; an explicitly empty response is available output, while absent/truncated output stays missing.

Actor parents require an explicit provider parent ID. Lifecycle entries retain provider hook observations, including turn interruption and Stop completion candidates. A turn interruption never fabricates tool outcomes, and a Stop observation does not prove the provider cannot continue.

One effect represents each recorded snapshot/path/operation change, with immutable before/after commit references and capture quality. Exactly one usable before and after receipt establishes a candidate window. All intermediate captures strictly after the before snapshot through the after snapshot participate, so watcher checkpoints and edit/revert history are retained. Boundary IDs now carry explicit launch scope, with replay support for older launch-scoped receipt IDs. Missing, reversed, duplicate, unavailable, truncated, or timed-out receipts prevent a complete window; relevant recorded capture/collector gaps also prevent it. A later captured receipt cannot repair a hook timeout.

Effects in overlapping windows list every candidate call and `overlapping-calls`. Even a single candidate is only `boundary-correlated`; attribution always remains `external-or-unknown`. This captures temporal evidence without claiming ownership of a concurrent user's edit. Capture omissions/partial scans remain explicit in `capture_quality`, and gap records are reported alongside the projection. Journal sequence defines replay order, not native causal order.

The added acceptance fixtures cover scoped identities, conflicting outcomes, incomplete approvals after interruption/crash, explicit actor parents, missing/empty output, overlapping effects, intermediate reverts, late receipts, failed scans, truncation, and real recorder/CLI replay. PTY cleanup fixtures cover direct-child signaling when the original process group is missing and bounded shutdown when a child cannot be reaped. The complete suite is measured on macOS and Linux; see the current result below and [Phase2TestGuide.md](Phase2TestGuide.md) for native checks.

The Claude quit investigation found a blocking wait in `PtyProcess.close` after Ctrl-Q had already reached cleanup. Cleanup now signals the direct child as well as its group, polls with a bounded TERM/KILL wait, and records `process.cleanup-gap` if reap remains unavailable. This fixes the launcher hang and saves a session with gaps; it does not prove that every native descendant terminated. The [earlier startup failure](provider-startup-before-pty-release.json) had one Claude cleanup gap. The subsequent PTY-release fix and passing repeat are documented below.

Automated acceptance: **92 tests passed on macOS/Python 3.9.6 (34.676 s) and Linux/Python 3.11 in the existing container (15.671 s)**. The final correlation refinements are additionally checked with the focused suite on both platforms. These results establish fixture behavior, not real native coverage.

## Real native measurements

Four macOS native runs now provide real evidence for Claude 2.1.234 and Codex 0.160.0: main tool/subagent scenarios plus independent project-hook composition scenarios. The normal trust screens were used without bypass flags. Both clients saved alpha → beta → alpha checkpoints; Claude delivered an explicit shell failure, while Codex preserved the opaque exit-7 completion as unknown. Subagent actor IDs were explicit, parent IDs absent, and Claude turn IDs absent. An interrupted Claude sleep had no terminal tool callback and replayed as incomplete. Codex approval rejection delivered PermissionRequest and Interrupt; the approval had no call ID and stayed unlinked.

Both project settings hashes were unchanged, and both independent observers delivered SessionStart/Stop/SessionEnd alongside Labradour. Claude `/exit` and Codex Ctrl-D let SessionEnd run before Ctrl-Q closed review; all four recordings completed without cleanup gaps. The unanswered-dialog case was subsequently fixed and repeated below; these runs alone do not certify all descendants/settings layers.

`adapter-coverage` now reports version/platform-scoped `observed-limited` evidence instead of claiming these observed categories are still wholly unverified. Additional Linux Codex, Claude writer and denial evidence follows below; human visual acceptance remains open. Native source payloads, with paths/opaque IDs redacted, are regression fixtures in `tests/fixtures/native-provider-hooks.json`. The fixture/report helper is `tools/native_acceptance.py`; it summarizes journal evidence and separately checks project observer delivery/settings hashes, without claiming a gate pass from counts.

That slice passed **97 tests on macOS/Python 3.9.6 (34.066 s) and Linux/Python 3.11 (15.602 s)**, including acceptance-helper and captured-native payload regression checks. See the current broader result below.


## Remaining broad native gate slice

[Additional native evidence](broad-native-acceptance.json) records a genuine Claude policy-hook denial with no file write, two overlapping Claude foreground Bash writers from distinct native actors plus an independent external edit, and real Linux Codex read/write/approval/failure-command delivery. The external edit belongs to two candidate windows and still has `external-or-unknown` attribution. Claude policy denial emitted no terminal tool callback and stays incomplete. This preserves the boundary between a manually observed native decision and provider-reported journal facts.

The native macOS Codex counterpart also completed two distinct actor-scoped writers. Both writer windows and the parent wait window span the external edit; all three are conservative candidates with no ownership claim.

Linux Codex ran in the pinned native Docker image. Its bwrap namespace sandbox could not start, so harmless commands were approved individually through native prompts. Intermediate alpha and beta checkpoint contents were verified from saved Git commits. Codex's opaque completion result remains unknown, including exit 7. Independent user/project hooks delivered SessionStart/Stop/SessionEnd alongside launch hooks; both settings files were unchanged. Claude's explicit CLI policy settings likewise composed without discarding project or Labradour hooks.

Closing the PTY master before the final KILL/reap wait fixes the macOS unanswered-Claude-trust-dialog gap. Both repeated startup probes now finish without forced cleanup or cleanup gaps. Recordings still say `completed-with-gaps` when no hooks were trusted or delivered. Bounded shutdown and explicit cleanup-gap reporting remain in place for unreapable children.

Pyte's sparse delete-line behavior could retain text where a blank source row should move up. ReplyScreen now materializes those rows before deletion; a regression covers full-screen and scrolling-margin cases. Private fixed-size native text streams from both providers matched independent tmux replay with zero differing text rows. Native query responses are not echoed into the oracle. This checks text cells, not colors or human perception of outer panes and resizing.

Reproduction tools: `tools/native_gate_driver.py`, `tools/terminal_oracle.py`, and `tools/Dockerfile.native-acceptance`. Detailed prompts, expected incomplete outcomes, normal trust review, container sign-in and trace privacy are in [Phase2TestGuide.md](Phase2TestGuide.md). The broad gate still needs authenticated Linux Claude, human visual acceptance, and managed/plugin settings compatibility. It is deliberately tracked separately from the implemented fixes and measured scenarios.

Current automated acceptance: **103 tests pass on macOS/Python 3.9.6 (34.576 s) and Linux/Python 3.11.17 (15.621 s)**. These include scoped concurrency-driver evidence and sparse-row terminal regressions.
