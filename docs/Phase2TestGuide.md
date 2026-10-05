# Phase 2 testing guide

Phase 2 records native hooks, captures workspace checkpoints at tool boundaries, and replays correlated actions without assigning exclusive file ownership. The curses panes still show journal cards. The full historical review UI, layout editor, and visualization plugin runtime are later work; use `actions` to inspect correlation now.

## Measured results

Real macOS runs used **Claude Code 2.1.234** and **Codex CLI 0.160.0**, with normal workspace/hook trust review and no trust or permission bypass flags. [provider-native-acceptance.json](provider-native-acceptance.json) contains summaries without raw prompts/responses. [Captured native fixtures](../tests/fixtures/native-provider-hooks.json) retain redacted payloads for regression tests. The older [startup report](provider-startup.json) tested unanswered dialogs only; it is not the current tool-delivery result.

| Check | Claude | Codex |
| --- | --- | --- |
| Workspace/hook trust flow | Workspace dialog accepted for the private fixture; callbacks then arrived | Workspace and hook-review dialogs exercised; callbacks then arrived |
| Read / create / edit / revert / shell | Delivered; alpha → beta → alpha saved as intermediate checkpoints | Delivered through Bash/apply_patch; intermediate checkpoints saved |
| Shell exit 7 | `PostToolUseFailure`, action `failed` | `PostToolUse` with opaque response; result deliberately `unknown` |
| Subagents | Start, read callbacks and stop delivered; no parent/turn ID supplied | Start, read callbacks and stop delivered; turn ID supplied, no parent ID |
| Interruption / denial | Interrupted sleep lacked a terminal tool callback, leaving `incomplete`; attempted approval was automatically approved in this environment | A real approval was rejected with Escape; `PermissionRequest` and `Interrupt` arrived; no invented per-tool denial |
| Existing project hooks | Independent observer delivered alongside Labradour; settings unchanged | Independent observer delivered alongside Labradour; settings unchanged |
| Main session shutdown | `/exit`, then Ctrl-Q: completed recording, no cleanup gap; `SessionEnd` observed | Completed recordings, no cleanup gap; `SessionEnd` observed with Ctrl-D in the composition run |
| Focus and resize | Commands exercised and input remained usable | Commands exercised and input remained usable |

The additional [broad native evidence](broad-native-acceptance.json) now verifies:

- **Claude policy denial:** a separate native `PreToolUse` policy hook in explicit `--settings` denied only the marker command. The UI displayed the refusal, `denied.txt` stayed absent, and the project observer still delivered. No terminal tool callback arrived; replay correctly reports `incomplete`. This checks policy-hook denial, not manual approval rejection or the auto-mode `PermissionDenied` callback.
- **Claude overlapping writers:** two background agents each ran a foreground `sleep 35; printf … > …` Bash call. Both saved before receipts were present and both calls unfinished when the driver wrote `external2.txt`. Explicit actor IDs were distinct. Replay lists two candidate actions for that external edit and keeps attribution `external-or-unknown`; both agent files contain their separate markers.
- **Codex overlapping writers:** the native counterpart also completed both actor-scoped shell writes. The external edit has three candidate windows: both writers and the parent wait action. All retain `external-or-unknown` attribution.
- **Unanswered-dialog quit:** after closing the PTY master before the final reap, both native probes exit with no forced cleanup and zero cleanup gaps. Zero callbacks and `completed-with-gaps` are expected when trust was never granted; this is a shutdown check, not hook-delivery acceptance. The [earlier failure](provider-startup-before-pty-release.json) remains available; [provider-startup.json](provider-startup.json) is the passing repeat.
- **Linux Codex:** native 0.160.0 ran in Docker Linux/aarch64 with Python 3.11.17 and Node 22.23.3. Eight request/completion pairs, approval callbacks, SessionEnd, and alpha → beta snapshots arrived; project settings stayed unchanged. Docker could not start Codex's `bwrap` namespace sandbox. Each harmless read/write/exit command was approved individually through the native UI. Exit 7 still has an opaque result and stays `unknown`.
- **Settings composition:** native Claude explicit CLI settings composed with project/Labradour observers; Linux Codex user/project/launch hooks delivered together, with both file layers unchanged. Managed policies and plugin registrations remain unmeasured.
- **Terminal text cells:** private fixed-size Claude and Codex streams matched independent tmux replay at 36 × 68, with zero differing text rows. A regression also fixes pyte leaving stale text after deleting sparse blank rows. This does not certify colors, outer curses panes, resizing, or every interactive frame.

**Still open:** authenticated Linux Claude (macOS Keychain credentials cannot be used in the container), human visual acceptance and managed/plugin settings compatibility. These are explicitly tracked in Tasks.md; the broad gate is not marked fully accepted.

`python3 -m labradour adapter-coverage` now distinguishes `observed-limited` measurements from unverified categories and includes the tested version/platform. It does not automatically certify another installed version.

## 1. Prepare an isolated fixture

From the repository, run:

```sh
python3 tools/native_acceptance.py prepare claude --existing-observer
```

Repeat with `codex` for a separate fixture. The helper creates a private temporary root containing:

- `workspace/readme.txt`, with `LABRADOUR_NATIVE_READ_MARKER` and a newline.
- A project-local `.claude/settings.json` or `.codex/hooks.json` with a harmless existing observer.
- `observer.py`, `observer.jsonl` when hooks run, and `acceptance.json` outside the workspace.
- A `recording` directory after launch, also outside the workspace.

The observer writes only hook names and native session IDs, emits no model context, and makes no decisions. No global provider settings are edited by the helper. The native client's normal trust flow can persist your trust decision.

Copy and run the printed `launch_command` from the repository. Omit `--existing-observer` when you want a plain fixture. For a Claude approval test, append `--permission-mode default` to the printed command; existing policy may still automatically approve a harmless command.

## 2. Verify trust and delivery

Inspect the workspace and generated observer script before accepting the provider's normal trust dialog. In Codex, review the hook sources/commands when prompted, or use `/hooks`; trust the observers you have reviewed. Project trust and hook trust are separate. Do not add bypass flags to make this test pass. Codex's normal review requirement and layer composition are documented in [OpenAI Docs](https://learn.chatgpt.com/docs/hooks); Claude's hook behavior is documented in [its reference](https://code.claude.com/docs/en/hooks).

Expect the Agent pane to show the native client, Activity to gain `adapter.event` and `adapter.boundary` cards, and the footer to show a callback count. A waiting indicator means no callback has arrived; it is not proof of failure before trust review. If you trust hooks after startup, a startup callback may wait until a later launch—check a prompt/tool callback too.

## 3. Exercise read, edit/revert, and failure

For Claude, submit:

```text
In this disposable workspace, use Read on readme.txt. Use Write to create acceptance.txt containing alpha and a newline. Use a separate Edit to change alpha to beta, then another Edit to revert beta to alpha. Run separate Bash calls printf LABRADOUR_SHELL_OK and sh -c 'exit 7'. Do not touch other files, use network tools, install anything, or change settings. Report briefly.
```

For Codex, submit:

```text
Read readme.txt with a local shell command. Use apply_patch to create acceptance.txt with alpha and a newline, then a separate apply_patch to change alpha to beta, then another apply_patch to revert beta to alpha. Run separate shell calls printf LABRADOUR_SHELL_OK and sh -c 'exit 7'. Do not touch other files, use network tools, install anything, or change settings. Report briefly.
```

Expect `acceptance.txt` to finish with `alpha`, but the saved checkpoints to include `beta`. The model may choose different tools/order; record what actually happened rather than assuming an exact callback count. Read-only calls should not invent file effects. Claude's failure should be explicit when delivered. Codex may preserve an opaque completion response with `unknown` result even for exit 7; do not parse response prose to manufacture a structured exit code.

## 4. Exercise actors and ambiguous attribution

Ask the provider to delegate a read-only check of `readme.txt` while independently reading `acceptance.txt`. Expect explicit actor IDs when delivered. Missing parent IDs stay unknown; sharing a native session ID is not a parent link. Claude may omit turn IDs; Codex's subagent hooks can share the parent session while carrying their own turn IDs.

To repeat the writer check, request two concurrent harmless writes to distinct fixture files, using whatever parallel facility the provider exposes. During a long-running tool, edit another fixture file from a second editor. Confirm concurrency from the native UI/tool activity; interleaved callbacks alone do not prove physical overlap.

In `actions`, shared effects should list multiple candidate calls and `overlapping-calls`. **Even one candidate never proves ownership:** every effect remains `external-or-unknown`. Missing or incomplete windows produce unassigned effects. Saved intermediate effects are distinct even when a later operation reverts them.

## 5. Exercise approval rejection and interruption

Use the provider's normal approval settings. Ask it to request approval for a harmless `printf LABRADOUR_DENIAL_MARKER`, and reject the native prompt. In Codex, explicitly requesting `require_escalated` for this harmless command produced the approval prompt in our run. Do not authorize it if you are testing denial. If policy auto-approves it, the denial test has **not** passed; record that limitation.

Expect an approval observation if delivered. Codex's observed approval payload lacked a call ID, so replay kept it unlinked from the tool request. Rejecting it produced a turn interruption, not a dedicated denial outcome. Claude's configured `PermissionDenied` callback is limited to its auto-mode path; a manual rejection need not generate it. A missing terminal outcome becomes `incomplete` when the host recording closes, never an inferred `denied`.

To test cancellation during execution, request a foreground `sleep 120` with enough timeout, then press the provider's native interrupt key **while it is still running**. Confirm the active call in the UI before interrupting. In our Claude run, Escape stopped `sleep 30`, but no terminal tool callback arrived. Codex `Interrupt` concerns the main turn and does not establish every tool's outcome. A completed background shell invocation does not prove its process exited.

## 6. Verify terminal behavior and shutdown

Type normally, paste a multiline harmless prompt, cancel without submitting, switch focus with Ctrl-] l/v/a, and resize the terminal. Check borders, Unicode alignment, wrapping, cursor position, scrollback, and return to Agent input. Verify historical card selection survives focus/resize. Look for stale text after scrolling; record the client version and exact reproduction if it occurs. Pasted Ctrl-Q must not invoke Labradour's global quit command.

Exit the native client normally first (`/exit` in Claude; Ctrl-D at an empty prompt in the tested Codex version). Labradour should leave review open and show the agent exit status. Then Ctrl-Q should close Labradour and save the recording. Also test Ctrl-Q while an **unanswered** trust dialog is open in a fresh fixture; the latest automated repeat passes for both providers.

Cleanup sends TERM/KILL to the owned group and direct child and bounds its reap wait. If reap is unavailable, Labradour reports the PID, records `process.cleanup-gap`, and closes with gaps. That is an explicit failure of clean-child-termination acceptance even though saved history remains reviewable. Do not call it passed based only on launcher exit zero.

## 7. Inspect and save the evidence

Use the `recording` path printed during preparation:

```sh
python3 tools/native_acceptance.py report RECORDING --output /tmp/my-phase2-report.json
python3 -m labradour history RECORDING
python3 -m labradour history RECORDING --session SESSION_ID
python3 -m labradour actions RECORDING --session SESSION_ID
python3 -m labradour diff RECORDING BEFORE_COMMIT AFTER_COMMIT
```

Copy the session ID from `history`; copy full commit IDs from `actions`/journal snapshots for `diff`. The report defaults to the latest session; use `--session` to select another. It summarizes callbacks, tool observations, identity presence, receipt phases, states, effects, and gaps without including raw prompts/tool responses. `installed_versions_at_review` identifies the CLI installed when the report runs; record launch versions yourself if they changed afterward.

For composition, expect `existing_observer.settings_unchanged: true` and matching session `SessionStart`/`Stop` observations in both the journal and observer counts. `SessionEnd` may be absent—report it explicitly. CLI-supplied, user-level, managed, or plugin hook composition are separate from this project-layer measurement. Native `--settings` merge behavior has automated fixtures; do not extend that evidence to an untested settings layer.

Check `unavailable_boundaries`, `cleanup_gaps`, and `gap_kinds`. Action states may be completed, failed, interrupted, incomplete, or inconsistent. `result_outcomes: unknown` means the provider did not supply a recognized structured result. Missing output is labeled; an explicitly empty response is available output. Capture quality may be partial due to policy omissions or scan issues. A live scan is not an atomic workspace transaction.

Successful receipts reference saved snapshots. Candidate windows include all captures strictly after the before snapshot through the after snapshot. A missing, reversed, duplicate, failed, truncated, or timed-out receipt prevents complete correlation. A late capture does not repair a timed-out before receipt. `actions` reads saved data only; it never reexecutes tools, follows transcript files, or requires the original workspace to exist.

Keep raw journals private. The compact report still contains session IDs/version/platform metadata; review it before sharing. Write down scenario, platform, launch version, visible outcome, journal/session references, gaps, and **pass / limitation / fail / not run**. An unobserved category cannot become passed from aggregate callback counts.

## Automated reproduction and completion criteria

```sh
python3 -m unittest discover -s tests -v
python3 tools/provider_startup_probe.py --seconds 8
```

The current full suite passes 103 tests on macOS and Linux. The suite needs local Unix socket access. Linux fixture reproduction is in [RecorderAcceptance.md](RecorderAcceptance.md#reproduce); run the native guide separately on Linux with installed/authenticated clients.

The broad native gate stays open for the explicit remaining checks above. The measured scenarios and regression suite protect current observations; they do not replace sign-in-dependent runs or human visual checks.


## 8. Reproduce the broader gate tooling

Build the recorder image first, then the native image. Pinned CLI versions are part of the measured scope, not an assertion about the latest releases.

```sh
docker build -t labradour-acceptance -f tools/Dockerfile.acceptance .
docker build -t labradour-native-acceptance -f tools/Dockerfile.native-acceptance .
```

`tools/native_gate_driver.py PROVIDER ROOT -- NATIVE_ARGUMENTS` takes the private root returned by `native_acceptance.py prepare`. It accepts JSON lines on stdin, displays the current outer screen, and sends native keys. Wait for each trust dialog before submitting a model prompt. Example commands:

```json
{"send":"\r","wait":5}
{"send":"Your harmless test prompt","wait":1}
{"send":"\r","wait":15}
{"wait":10,"save_screen":"screen.json"}
```

For the Claude writer scenario, ask for two background agents, each with its own foreground Bash `sleep 35; printf LABRADOUR_PARALLEL_GATE_LEFT > left.txt` / `…RIGHT > right.txt`, 120000 ms timeout, and `run_in_background: false`. Launch both before waiting. Submit the prompt, then use:

```json
{"send":"\r","external_when_active":{"marker":"LABRADOUR_PARALLEL_GATE","count":2,"path":"external.txt","content":"EXTERNAL_WRITER_MARKER\n","timeout":60},"wait":10}
```

The marker synchronizes this controlled experiment only. The driver requires unfinished, explicitly scoped Bash calls with completed before receipts, excludes background invocations and Agent launches, and records the independent edit in `driver-evidence.jsonl`. A timeout means the external write was **not run**. Verify the two actor IDs and final files; do not infer overlap from callback interleaving.

For independent terminal replay, start a fresh driver with `--trace-terminal` **before** the provider argument. Keep one size throughout the trace, wait for a stable native screen, and run `python3 tools/terminal_oracle.py ROOT` where tmux is installed. It writes only summary results; the private trace remains inside ROOT. It checks text cells and trailing spaces only. Native query replies are not echoed into the replay screen. Never copy raw terminal traces, provider auth files, or unredacted journals into repository fixtures.

For Claude policy denial, use a separate native `--settings` hook, not Labradour's observers. A `PreToolUse` command hook matching Bash should read JSON stdin and, only for the experiment command, emit:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Native acceptance fixture denies only this marker command."}}
```

Ask for exactly `printf LABRADOUR_DENY_GATE > denied.txt`, stopping on refusal without retries or other writing tools. Expect an absent target file and an incomplete action if no terminal callback is emitted. The native denial decision belongs to the test policy; Labradour continues to observe only. See the [native hook decision contract](https://code.claude.com/docs/en/hooks).

The local `labradour-native-gate` container remains available for the outstanding Claude sign-in. Sign in directly without sharing credentials in chat:

```sh
docker exec -it labradour-native-gate claude auth login
docker exec -it labradour-native-gate sh
```

Inside the container, prepare a fresh Claude fixture and follow sections 1–6. Its repository is mounted read-only at `/workspace`; test files live in the container's private `/tmp` roots. Codex uses a read-only auth-file mount; no credentials are baked into the image or evidence. When finished, stop the test container with `docker stop labradour-native-gate`. A container stop does not export its temporary recordings; save redacted reports first if needed.
