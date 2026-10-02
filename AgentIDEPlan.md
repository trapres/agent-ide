# Labradour implementation plan

Date: 2026-10-02. Status: proposed architecture; no implementation yet.

MVP UI specification: [AgentUI.md](AgentUI.md). That document defines layout, focus, activity selection, and the pluggable visualization contract; this plan defines capture, storage, and delivery. Updated to use the three-pane MVP layout.

## 1. Goal and initial decisions

Build an ncurses-based IDE that launches a CLI agent, preserves its interactive terminal experience, and lets the user inspect tool activity and evolving files alongside it. The central review path is **session → prompt/turn → actor/tool/action → selected effect → visualization**. Diffs are one visualization type alongside deletion cards, command/output views, and tool-specific views.

The agent occupies half the usable screen on a configurable left or right side. The other half contains an Activity log above a Visualization pane, each occupying a quarter of the default screen. Selecting a logged action drives the visualization while the native agent remains interactive.

Use three complementary records:

1. A PTY terminal stream for the agent's native interface.
2. An append-only event journal for tool calls, lifecycle events, filesystem observations, and coverage gaps.
3. A private scratch Git repository for captured file contents and intermediate checkpoints.

Git provides content history; the journal provides activity history. A read-only tool, a failed command, and an edit later reverted can all matter even when the final session diff is empty.

Start with macOS and Linux, one workspace and one launched agent at a time. Build Claude Code and Codex adapters plus a generic executable adapter. Support non-Git workspaces and already dirty repositories. Initial review and export are read-only; workspace restore and multi-agent execution come later.

## 2. Attachment: native CLI first, structured integration second

### Native terminal mode — the MVP

Launch the chosen executable under a pseudoterminal, with its working directory set to the real workspace. The IDE owns the outer terminal; only the child PTY receives agent input and terminal output.

Implementation requirements:

- Allocate a PTY and process group; propagate window dimensions and resize signals.
- Feed child output into a terminal emulator, then render its cell grid inside a curses window. Simply putting ANSI output in a text widget will break full-screen CLI interfaces.
- Handle alternate screens, cursor positioning, color, Unicode width, scrollback, bracketed paste, and terminal queries against the pane's dimensions.
- Provide a dedicated IDE key prefix, tentatively `Ctrl-]`, to change focus. Other keys and control sequences go to the agent when its pane has focus.
- Preserve native authentication, prompts, permission checks, slash commands, and interruption behavior. Observation must not grant permissions or alter tool inputs/results.
- Define shutdown choices: stop the launched process group cleanly, or detach only after a persistent supervisor exists. MVP closes with a clean stop; attaching to arbitrary already-running terminals is out of scope.

Collect structured activity through provider hooks alongside the PTY. Do not infer prompt boundaries from Enter presses: multiline input, slash commands, and approval prompts make this unreliable.

Current Codex documentation exposes prompt, tool, and session hooks. Local tool coverage includes shell, patches, MCP, and other function calls, but hosted tools and some specialized paths are excluded. Use hooks for native mode, test coverage on the installed version, and mark omissions explicitly. Treat transcript parsing as a version-specific fallback because its format is not stable. [Codex hooks](https://learn.chatgpt.com/docs/hooks)

Claude Code provides tool and lifecycle hooks, including `PreToolUse`, `PostToolUse`, and `PostToolUseFailure`. Rejected calls do not all traverse these hooks; permission denials and validation failures need separate handling where available. Use prompt/session hooks for boundaries and correlate records by provider call IDs. [Claude Code hooks](https://code.claude.com/docs/en/hooks)

Hook handlers should send JSON to a local authenticated collector, wait only for a durable acknowledgement, and return a successful empty response. They must compose with existing hooks without replacing them. Prefer supported launch-scoped configuration; otherwise provide an explicit setup operation that adds and can remove only Labradour's configuration entries. Check provider trust requirements during setup. Never silently rewrite global settings.

### Structured mode — later, optional

In this mode Labradour renders the conversation and owns prompt submission; it does not show the provider's native TUI.

Codex's app-server is the preferred structured backend: initialize the connection, start/resume a thread, start turns, consume item lifecycle/output notifications, and answer approval requests in the IDE. Relevant items include command execution, file changes, MCP calls, web search, and collaboration. Preserve provider IDs and distinguish proposed edits from completed edits. This is a separate backend, not a second observer attached to the native CLI process. [Codex app-server](https://learn.chatgpt.com/docs/app-server)

For batch integration experiments, Codex also offers `codex exec --json`; evaluate its event coverage separately from app-server. [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

Claude's programmatic CLI offers streaming JSON; its Agent SDK is another candidate when the IDE needs permission callbacks and persistent conversation control. Verify cancellation, approvals, resume, and subagent event coverage before selecting the interactive backend. [Claude Code programmatic execution](https://code.claude.com/docs/en/headless)

### Adapter contract and capability reporting

Each adapter implements executable/version discovery, launch configuration, event normalization, session mapping, health reporting, and supported interruption/resume operations. Keep provider-specific data behind the adapter boundary.

| Adapter mode | Native UI | Tool source | Prompt boundaries | Expected coverage |
| --- | --- | --- | --- | --- |
| Claude native | PTY | Hooks; optional transcript reconciliation | Provider hooks | Supported hook events, version dependent |
| Codex native | PTY | Hooks; optional transcript reconciliation | Provider hooks | Local tool paths; hosted coverage incomplete |
| Codex structured | Labradour UI | App-server protocol | Protocol | Exposed protocol items |
| Claude structured | Labradour UI | SDK/stream plus hooks as needed | Programmatic interface | Verify by version and configuration |
| Generic CLI | PTY | Plugin if available | Manual bookmarks by default | Terminal and filesystem observations only |

Display coverage in the UI: provider/version, active collectors, supported event categories, dropped events, and missing boundaries. Universal “every tool” tracking requires cooperation from the agent; PTY capture alone cannot deliver it. A shell command is one agent tool invocation, not automatically a complete inventory of its subprocesses or syscalls. An MCP proxy sees only the calls routed through it.

## 3. Tracking filesystem operations and content

Start recursive filesystem observation before taking the baseline and before launching the agent. Watcher events are hints; content hashes and reconciliation establish the captured state.

Capture:

- New, edited, and deleted paths, including untracked files.
- Symlink targets without following links outside the workspace.
- Executable-bit changes and file-type changes.
- Rename observations when available; otherwise infer similarity and label the inference.
- Binary content under configured size limits, with metadata-only records above those limits.
- Directory observations in the journal, including empty directories that Git cannot store.

Use native watcher support through a portable library, with platform-specific behavior tested. Reconcile at startup, tool boundaries, turn completion, session completion, watcher overflow, and periodically. Queue events during baseline scanning, then reconcile before releasing the child process. Track permission errors and unreadable paths rather than treating them as deletions.

Read dirty files promptly, store immutable bytes, and create checkpoints from those captured bytes. Re-stat/retry files that change during reads; label snapshots inconsistent if stability cannot be established. Serialize checkpoint writes and distinguish scan start/end times. A snapshot of a live workspace is not automatically an atomic filesystem snapshot.

### Fidelity contract

The MVP records **observed operations and captured intermediate states**, not every write syscall. Watchers may coalesce events; a temporary file created and removed inside one shell command can disappear before its bytes are read. Pre/post tool snapshots cannot recover writes that happen entirely between them.

Use two recording policies:

- **Live observation:** watcher-driven captures, a short configurable checkpoint debounce (initially 100–250 ms), and boundary reconciliation. Suitable for ordinary review.
- **Tool-boundary capture:** supported synchronous hooks request a completed pre/post snapshot before returning. More overhead, but clearer boundaries for sequential tool calls. It still does not capture all intra-tool states or prevent concurrent writers.

If literal every-operation fidelity becomes a requirement, add a separate research milestone for an instrumented filesystem or execution sandbox. Evaluate OS tracing, overlay/FUSE capture, write interception, and process attribution on each target OS. A faithful operation trace still requires preserving content before overwrite/removal; syscall names alone are insufficient. Report this as a stronger recording mode only after demonstrating its guarantees.

### Attribution

Store observation separately from attribution. Link provider-declared paths and tool IDs when available, then annotate watcher changes with candidate active calls. Use labels such as `provider-reported`, `boundary-correlated`, `overlapping-calls`, and `external-or-unknown`.

Parallel calls, background processes, and user edits can overlap. Do not assign every change to the most recent tool. Prefer checkpoints around the whole overlapping batch, preserving the individual call events. An agent failure can still leave real filesystem changes.

## 4. Scratch Git: private history independent of project commits

Store recording data outside the workspace in the user's application data directory:

```text
labradour/workspaces/<workspace-id>/
  workspace.json          # canonical path, recording policy, schema version
  events.sqlite           # append-only facts and rebuildable query indexes
  history.git/            # private bare Git repository, no remote
  payloads/               # permitted large event/output artifacts
  sessions/<session-id>/  # terminal recording and adapter metadata
  exports/                # generated visualization bundles
```

The scratch repository must have its own objects, refs, configuration, and identity. Labradour never stages, commits, resets, or checks out files in the project's repository as part of recording. Do not inject `GIT_DIR`, `GIT_INDEX_FILE`, or scratch configuration into the agent environment. Do not borrow project objects through alternates, which would make retention depend on project garbage collection.

Recommended snapshot algorithm:

1. Enumerate the capture policy's included paths and establish a manifest of immutable bytes, Git file modes, and exclusions.
2. Write blobs from the captured bytes through Git plumbing; construct trees from the manifest using a private temporary index or a tree builder. Disable content filters and attribute-driven conversions so history preserves captured bytes.
3. Create a commit only when the tree changes, using the prior checkpoint as parent, then atomically update `refs/heads/sessions/<id>` with an expected-old-value check.
4. Record the commit/tree OIDs, checkpoint reason, observation interval, journal sequence range, capture quality, and exclusions in the event database.

Build trees from the manifest rather than running `git add` over the live project. This avoids project ignore/filter behavior, changing files during staging, and nested repository handling surprises. Exclude Git administrative directories/files; define submodule and nested-workspace policy explicitly. Initial policy captures ordinary files under the chosen root, including files inside nested repositories when included, without pretending to reproduce their Git metadata.

Checkpoint triggers: initial baseline, captured mutation batches, pre/post tool boundaries where enabled, prompt/turn boundaries, manual bookmark, cancellation, crash recovery, and clean session end. A marker with no content change points to the existing tree/commit. The timeline retains events even if no new commit was needed.

Each new session starts with the actual current workspace contents, including pre-existing edits, and has its own baseline root. Project HEAD and working-tree Git status can be recorded as metadata; project commits remain separate milestones. Support comparisons against project HEAD later by importing a read-only tree into the scratch object store with exclusions documented.

Represent creates/deletes by manifest membership, renames as observations plus Git similarity inference, and binary changes through blob identity. Git does not preserve ownership, ACLs, general permissions, timestamps, or empty directories; store relevant metadata in the journal/manifest.

### Recovery and retention

SQLite and Git cannot share one transaction. Journal snapshot intent, write objects/commit/ref, then journal snapshot completion. On restart reconcile pending intents and refs, preserve recoverable commits, and mark interrupted captures. Keep durable refs for every retained checkpoint.

Apply workspace exclusion rules independently of project `.gitignore`. At setup show what will be captured; default exclusions cover caches, dependency/build output, credentials, and recorder data. Record exclusions so “all files” means all included observable files. Excluded content must never first be written into Git and then redacted: deleting a ref does not immediately remove a sensitive blob.

Set quotas for snapshots, output payloads, and terminal history. Record truncation and disk-full failures visibly. Terminal recordings and raw tool arguments can also contain secrets; allow recording suppression and metadata-only storage. Keep data local with restrictive permissions. Retention cleanup must consider journal references, retained refs, exported bundles, and Git garbage collection together.

## 5. Event model and recorder service

Use a local recorder service as the single journal and snapshot writer. The curses UI consumes its events and can replay past sessions without starting an agent. Hook processes connect over a Unix-domain socket with a per-launch token; a bounded local spool handles temporary collector outages. Validate payload size/schema, deduplicate provider event IDs, and keep unknown event types for forward compatibility.

Common event envelope:

```json
{
  "schema_version": 1,
  "event_id": "local-unique-id",
  "sequence": 142,
  "received_at": "2026-10-02T17:00:00Z",
  "workspace_id": "workspace-1",
  "session_id": "session-1",
  "turn_id": "turn-3",
  "agent_id": "main",
  "provider_call_id": "call-9",
  "source": "claude-hook",
  "kind": "tool.completed",
  "payload": {"tool": "Edit", "status": "succeeded"},
  "quality": {"coverage": "provider-reported"}
}
```

Also retain provider event time, local monotonic receive time, process identity where known, raw provider IDs, and permitted raw payloads. Nullable fields are normal for generic adapters. Local sequence defines ingest order, not a universal causal ordering across processes.

Event families: session and turn lifecycle; agent/subagent lifecycle; tool requested/started/completed/failed/denied/interrupted; approval state; terminal output references; filesystem observations; snapshot intent/completion/failure; user bookmarks; collector health and coverage gaps.

Reducers build query tables for sessions, turns, calls, file changes, checkpoints, attribution links, and bookmarks. Rebuild them from journal facts. Match call lifecycle records by IDs, distinguish repeated invocations, and mark calls without terminal outcomes as incomplete after interruption. Preserve read/search tools even when they produce no diff.

Add an action/effect projection for the UI: stable selection IDs, actor identity, normalized operation kinds (for example `file.create`, `file.delete`, and `shell.execute`), lifecycle state, parent call links, output references, immutable before/after evidence, and evidence revision. One tool invocation has one primary log row with selectable child effects; external/unknown filesystem observations remain distinguishable. This projection supplies visualizers without coupling them to provider-specific payloads.

Use separate queues for durable collection, snapshot work, and UI updates. Coalesce repaint requests, not facts. Payload limits, spool overflow, backpressure, and reconnect gaps need explicit health events. Launch IDs and workspace IDs prevent unrelated concurrent sessions from contaminating the recording.

## 6. Three-pane ncurses interface and action visualizations

Implement the layout and interaction contract in [AgentUI.md](AgentUI.md):

```text
+---------------- Labradour: workspace / session / coverage ----------------+
| Native agent terminal                 | Activity: actors/tools/actions  |
|                                       | Read       completed            |
|                                       | Write      completed  <         |
|                                       | Bash       running              |
|                                       +---------------------------------+
|                                       | Visualization: selected action  |
|                                       | Creation / deletion / command   |
|                                       | Diff / output / tool-specific   |
+---------------- focus / recording health / snapshot age ----------------+
```

Default geometry is 50:50 horizontally, with the review side split 50:50 vertically. Make agent placement left/right and ratios configurable and persistent. PTY dimensions always match its pane. Use full-screen/compact fallback on small terminals. Affected-file pickers and evidence details belong inside review panes or overlays, not a fourth primary pane.

The Activity pane shows actor, tool/action, target, state, and effect count. Select by stable ID; new events update rows without stealing historical selection. Expand calls into selectable effects, preserve actor/turn relationships, support filters and live-follow mode, and show attribution quality. Selecting an action immediately drives the Visualization pane.

Create a provider-neutral `Visualizer` registry with applicability checks, evidence requirements, cancellable preparation, embedded terminal rendering, and optional graphical export. Route creation to a creation/content view, modification to a source diff, deletion to a deletion/last-content card, and shell execution to a command/output view. Provide built-in fallbacks for reads/searches, renames, binaries, actor lifecycle, MCP/unknown tools, and recording gaps. Multi-effect calls expose an in-pane picker; selecting a child effect routes by operation rather than the parent tool name.

The resolver honors explicit choice, configuration, then applicable plugins and a generic fallback. Its context includes actor/call/effect IDs, operation kind, lifecycle, immutable snapshot and output references, evidence revision, and attribution quality. Preparation must not block the UI; stale results are discarded on selection changes. MVP plugins are registered built-ins and the explicitly installed gitdiffviz subprocess adapter; arbitrary dynamic plugin loading is deferred.

Retain adjacent checkpoint, tool/batch interval, turn, session, and arbitrary A/B comparisons inside file views. Read historical contents rather than the live workspace. A reverted edit remains discoverable even when session net change is zero. Show proposed edits, pending captures, ambiguous intervals, missing content, and plugin failures explicitly. Review never reruns a selected shell command.

## 7. gitdiffviz evaluation and integration

The repository currently describes an OCaml analysis backend and a JS/TS renderer, structured revision-diff extraction, scene JSON, a browser viewer, and a Tauri GUI. It also documents adjacent-commit timeline rendering and basic semantic extraction for Rust, C/C++, and Swift. This makes it a candidate for a graphical companion, rather than the curses renderer. [gitdiffviz repository and README](https://github.com/superstealthlogic/gitdiffviz)

Proposed integration: register gitdiffviz as an optional source-change visualizer/exporter using the contract in [AgentUI.md](AgentUI.md). Export a selected scratch revision pair or session commit chain, invoke a pinned build as a subprocess, and open the resulting visualization locally on user request. The quarter-screen pane retains an embedded creation/diff view and an explicit graphical-open action. Browser/Tauri rendering is a companion surface; consuming analysis output in a terminal renderer requires a separate schema spike. Keep event/attribution metadata in an Labradour sidecar unless its schema supports extension. Treat a scratch timeline's checkpoints as captured steps, not project commits.

Before adopting it, run a bounded compatibility spike:

1. Build a pinned revision and check license/distribution obligations.
2. Test bare scratch repository support; if it needs a checkout, materialize captured revisions in a separate temporary export directory.
3. Verify additions, deletions, renames, binaries, unusual filenames, and empty diffs.
4. Verify semantic extraction uses the selected historical contents rather than the live workspace. Disable semantic views if this cannot be ensured.
5. Benchmark checkpoint-heavy sessions, output size, cancellation, and lazy generation.
6. Confirm its revision/scene interfaces and whether provider metadata can link back to the IDE.

Keep it optional behind the visualizer contract's graphical-export capability. If compatibility or packaging costs are excessive, ship the built-in action visualizers and revisit integration later. Deletions default to a deletion card and shell calls to command/output views; gitdiffviz can be offered as an alternate for linked file effects. Tool reads, command failures, approvals, and non-file operations require the event timeline regardless of the visualization backend.

## 8. Implementation stack and modules

Start with **Python 3 + curses/ncurses**, SQLite, Git subprocess plumbing, `pyte` for the VT screen engine with a pane-local protocol adapter, and `watchdog` for filesystem observations. Phase 0 automated macOS checks now validate this terminal/watcher combination against deterministic fixtures. Authenticated provider fidelity and Linux support remain validation gates before locking in the stack. Use explicit subprocess argument arrays and NUL-safe filename handling.

Suggested modules: `supervisor`, `terminal`, `adapters/{generic,codex,claude}`, `collector`, `journal`, `filesystem`, `snapshots`, `actions`, `diffs`, `ui/{layout,activity,selection}`, `visualizers/{registry,file_create,source_diff,file_delete,command,generic}`, and `exporters/gitdiffviz`. Keep the recorder, adapters, action projection, and visualization preparation independently testable from curses.

If terminal-emulation fidelity or throughput blocks the spike, evaluate a libvterm-backed implementation or a Rust core with ncurses bindings. Preserve the provider-neutral event/storage contract so this choice does not require redesigning the history model.

## 9. Delivery milestones and acceptance gates

Phase 0 now includes a pyte-backed curses/PTY harness, native and polling watchdog probes, a fake-agent/hook-sink probe, immutable-manifest Git fixtures, and local gitdiffviz compatibility measurements. Core macOS automated checks pass. See [README.md](README.md) for commands and [docs/Phase0.md](docs/Phase0.md) for evidence and remaining gates. Authenticated native CLI hook delivery and Linux execution remain unverified because of environment restrictions; Phase 0 is not yet fully accepted.

| Phase | Deliverable | Acceptance gate |
| --- | --- | --- |
| 0: feasibility | PTY/curses three-pane layout/focus spike; Codex and Claude hook probes; Git snapshot fixture; gitdiffviz compatibility notes | Native CLIs render and accept paste/input in a half-width pane; mirror/resize/interrupt work; coverage matrix reflects measured events |
| 1: recorder | Journal, watcher, baseline, private Git history, crash recovery, generic launcher | Create/edit/delete and reverted edits are reviewable; dirty project index/refs remain untouched; overflow and exclusions are visible |
| 2: native adapters | Claude and Codex hooks, prompt/tool boundaries, correlation, collector setup/health | Read-only, success, failure, denial, interruption, parallel calls, and subagent fixtures produce correct or explicitly incomplete records |
| 3: arbitrary layout specification | Expand AgentUI.md with arbitrary pane arrangements, a split-tree configuration schema, presets, layout editing, persistence, focus/navigation, and resize/compact behavior | Worked layouts and configuration examples cover reordered panes and nested horizontal/vertical splits; minimum sizes, invalid layouts, PTY resizing, and state preservation are defined before review UI implementation |
| 4: review MVP | Three primary panes with the layout system specified in Phase 3; action/effect selection, actor/tool filters, built-in visualizer registry, checkpoint comparisons, session replay | Default half-screen CLI stays interactive; arbitrary arrangements preserve focus and selection; selecting create/delete/shell actions shows distinct views; stale render jobs cannot overwrite current selection |
| 5: graphical export | Optional gitdiffviz visualizer/exporter, lazy caches, explicit local open action | Historical pair/session exports work; embedded fallback remains usable; core IDE works without gitdiffviz |
| 6: extended modes | Structured provider backends, multi-session supervisor, stronger recording research | Approval/resume behavior preserved; concurrent sessions stay isolated; fidelity claims have demonstrated limits |

Phase 3 is a specification milestone required before Phase 4. Keep the current half-screen Agent and stacked quarter-screen review panes as the default preset, while allowing users to arrange the same three primary panes through nested horizontal/vertical splits and change their ordering and proportions. Specify layout editing commands, named presets, user/workspace persistence, schema validation and migration, and fallback for invalid or undersized layouts. Include examples with the Agent above the review panes and with Visualization above Activity. Define focus traversal and preservation of the running PTY, selected action, and visualization state when switching layouts. This phase updates the layout rules and acceptance scenarios in AgentUI.md; Phase 4 implements them.

High-value tests should cover rapid edit/revert, create/delete within one command, atomic-save rename, binary changes, symlinks, nested repositories, whitespace/newline filenames, executable modes, user edits during a call, long-running background writers, watcher overflow, collector failure, snapshot interruption, disk full, and exclusion-before-storage. Expect the transient-file test to expose a live-observation gap rather than claim impossible completeness.

Use recorded provider fixtures and a deterministic fake agent for automated tests; supplement with manual native CLI checks. Proposed performance targets for a representative workspace: terminal input overhead below 50 ms at p95, ordinary tool rows visible within 250 ms, and small-file snapshots within 500 ms. Benchmark with recording both enabled and disabled; measure queue depth, capture lag, storage growth, and missed boundaries before setting final budgets.

## 10. First concrete implementation slice

Build a generic PTY launcher in the configurable half-screen Agent pane, a filesystem watcher, the event database, a private Git baseline, an Activity list, and a selection-driven Visualization pane. Add built-in creation, source-diff, deletion, command/output, and generic cards through the registry. Drive it with a deterministic fake agent that creates, edits, deletes, reverts files, and runs a shell command. Verify mirror/focus/resize and historical selection using the acceptance scenarios in [AgentUI.md](AgentUI.md). Then connect Claude and Codex hooks independently and demonstrate one real session for each.

That slice proves the core value: seeing the agent work while retaining intermediate changes independently of project commits. Provider protocol backends and graphical export can follow once terminal fidelity, recording quality, and review navigation are established.
