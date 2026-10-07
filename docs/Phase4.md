# Phase 4: review MVP

Status: in progress. The layout contract is in [AgentUI.md](../AgentUI.md); the full implementation acceptance matrix is in [Phase3.md](Phase3.md).

## Layout foundation slice

The first slice implements immutable pane/split nodes, bounded decoded-tree validation, recursive minimum sizes and deterministic integer-cell allocation. Each tree has exactly one Agent, Activity and Visualization. Ratios remain requested values even when the effective geometry is constrained. Compact mode considers both the terminal floor and the tree's minimum size.

The launcher now supports four built-in presets:

```sh
python3 -m labradour run --demo --layout default
python3 -m labradour run --demo --layout agent-right
python3 -m labradour run --demo --layout agent-top
python3 -m labradour run --demo --layout visualization-top
```

Use the same `--layout` option when launching a native provider, with the usual workspace, recording and hook options. Explicit legacy `--side`, `--agent-width`, or `--activity-height` cannot be combined with `--layout`. Explicit legacy side/ratio flags now construct the legacy default split tree with the documented clamping; without layout flags, discovered preferences select the active tree.

With an explicit preset, prefix-Tab cycles by tree order and prefix-Shift-Tab cycles in reverse. Mirror reflects column splits throughout the tree. Width/height shortcuts change the target pane's nearest matching ancestor in 500-basis-point steps, bounded to 1000–9000. When no matching ancestor exists, a notice explains that no change occurred. Focus, event selection, paused follow and JSON scroll remain live through these operations.

The existing PTY/terminal remain in place; hidden Agent retains its last size and continues consuming output. Refocusing/resizing applies visible Agent content dimensions before subsequent input, including a focus command and typed text arriving in the same read batch. Duplicate PTY dimensions do not cause another ioctl. Review panes still display journal/JSON cards.

## Try it

At 140 × 40, `agent-top` places Agent above two side-by-side review panes. Type `size` in the demo: expect `columns=138, lines=17`. Focus Visualization with Ctrl-] then `v`, maximize with Ctrl-] then `z`, then focus Agent with Ctrl-] then `a`. Type `size` again: expect `columns=138, lines=36`. Toggle maximize off to return to the first dimensions. The demo remains the same running process.

Try `visualization-top`: Visualization should be above Activity. Prefix-Tab visits Agent → Visualization → Activity; reverse traversal reverses that order. Shrink below 100 × 28 and confirm only the focused pane appears with a compact reason, then grow to restore the preset. Quit with Ctrl-Q.

## Validation and remaining work

The suite has 116 tests, including ten new layout tests. Full macOS/Python 3.9 and Linux/Python 3.11 runs pass. Coverage includes all pane permutations, both nesting shapes and split axes, extreme/constrained ratios, complete area coverage without overlap, invalid/cyclic decoded trees, reverse traversal, hidden PTY sizing, preserved state, rendered Agent-above-review geometry and same-batch input after focus changes. Existing recorder, adapter, cleanup and legacy geometry regressions also pass.

The configuration/persistence slice below extends this foundation. The editor slice below extends the configuration slice. Subsequent slices add action/effect Activity projection, historical built-in visualizers and saved-session review/acceptance. Native human rendering/input checks remain necessary after the broader UI integration.


## Layout configuration and persistence slice

The launcher now reads bounded UTF-8 v1 JSON using the [Phase 3 schema](../AgentUI.md#21-split-tree-contract). Duplicate keys, unknown fields/versions, non-finite numbers, invalid pane trees, excessive depth, more than 32 named layouts per file and files above 64 KiB are rejected. Files and their direct config directories cannot be symlinks; FIFO/non-regular files are refused without blocking.

Settings resolve built-ins → user file → workspace file → explicit file → explicit name/legacy flags. Definitions replace whole trees; omitted preferences inherit. Invalid discovered layers are ignored with source warnings. Invalid/missing explicit files and explicitly unresolved names fail before launching a PTY. An unresolved discovered active name falls back to the original built-in default. Unsupported versions are never rewritten.

User settings live at `$XDG_CONFIG_HOME/labradour/layout.json` for an absolute XDG_CONFIG_HOME, otherwise `~/.config/labradour/layout.json`; workspace settings live at `.labradour/layout.json` under the requested canonical workspace. Demo launches discover layout settings from the requested workspace before creating their separate disposable agent workspace. `--ignore-layout-config` skips discovery, and cannot combine with an explicit file.

```sh
python3 -m labradour layout status --workspace /path/to/project
python3 -m labradour layout save coding --scope workspace \
  --workspace /path/to/project --layout agent-top
python3 -m labradour run --workspace /path/to/project -- claude
```

The save command persists the CLI-selected tree, active name and initial-focus preference, preserving other destination definitions. It does not persist live focus, selection, maximize or scroll state. `--scope user` writes the user file instead. `--confirm-shadow` permits an inherited-name shadow; `--confirm-replace` permits ignored/invalid destinations. Future versions, oversized and non-regular/symlinked destinations are refused even with confirmation. A user save can be overridden by a higher-priority workspace/explicit file on the next launch.

Writes use a 0600 same-directory staging file, file/directory fsync, atomic replacement and a nonblocking `.layout.json.lock`. Loaded file identity and content stamps detect changes, creations and conflicting cooperating saves; refusal leaves live preferences unchanged. Failed staging removes the temporary file. The advisory lock cannot exclude the final check/replace race with an unrelated editor that ignores it. A directory-fsync failure after replacement can leave the new file installed while reporting a save failure; reload before retrying. No automatic saves occur. Workspace changes follow recorder capture policy (`.labradour` is excluded by default).

Ctrl-] then `i` toggles live layout diagnostics in Visualization. They show all sources, ignored reasons, active name, available names, initial focus, current tree, requested/effective split sizes, compact reason, geometry and unsaved changes. A configured initial review focus initializes the hidden Agent PTY at its ordinary positive geometry and leaves it live. Layout warnings remain separate from recording health, which takes footer priority.

Manual check: run the save command above in a disposable workspace, then inspect `layout status` and launch the demo with that workspace. Confirm Agent is above the review panes. Change a ratio with the existing shortcut and inspect Ctrl-] `i`: unsaved changes should become true; closing/relaunching restores the saved ratio. To customize further, edit the saved JSON and use `initial_focus: "visualization"`; a new launch should focus Visualization without sending its navigation keys to Agent. Introduce an unknown field in a discovered file to see a warning/fallback; pass that same file explicitly to see a startup error. Inspect an untouched valid destination definition to confirm it survives a save.

**Validation:** all 128 tests pass on macOS/Python 3.9 and Linux/Python 3.11, including 12 configuration tests covering layer precedence, strict/bounded decoding, files/FIFOs/symlinks, legacy flags, save/restart, overwrite confirmations, newer-version preservation, lock contention, external changes, staging failure, diagnostics, CLI roundtrip and a custom-layout PTY launch with initial review focus.

**Next after the configuration slice:** the in-session editor, implemented below. Historical review views and external plugin transport remain later work.


## Layout editor and interaction acceptance slice

Ctrl-] then `:` opens an in-session layout overlay, preserving its owning pane's focus. It accepts bounded ASCII registered commands, never shell execution. Enter executes; Escape/Ctrl-C closes without sending text to the native CLI; Ctrl-Q quits globally. Up/Down and PageUp/PageDown scroll details; typing restores the prompt. The tree shows pane identities, split IDs, axes, requested/effective ratios, unsaved state and compact reason.

Implemented commands: `layout use NAME`, `swap PANE PANE`, `flip SPLIT_ID`, `axis SPLIT_ID rows|columns`, `ratio SPLIT_ID BPS`, `reset NAME`, `status`, `preview`, `apply`, `cancel`, `save NAME user|workspace`, and `reload`. Prefix every operation with `layout`. Saves accept explicit `--confirm-shadow`/`--confirm-replace`; reload requires `--discard` before dropping unsaved edits/preview. Original built-in reset bypasses a shadowing custom definition without writing a file.

Edits outside preview apply immediately. Preview changes only its candidate tree; Apply commits, Cancel/Escape discards. Input after Apply sees the resized existing PTY. The terminal, recorder/collector, current focus, selected event, paused follow and detail scroll stay attached to their original controllers. A failed PTY resize retains the previous positive dimensions with a recoverable notice; drawing clips to the new viewport while later frames retry.

Save/reload work runs outside the input thread on a cloned configuration, with one outstanding disk job and no queued layout edits. A completed save updates the saved baseline without replacing the live tree. Reload rereads user/workspace/original explicit-file layers, honoring the original ignore setting, and selects their active preference without startup name/legacy-ratio overrides. Neither completion replaces live focus. Disk errors keep the applied tree and unsaved state usable. Closing the menu does not cancel an already authorized disk job. Quit remains bounded; quitting during a pending save may leave an installed file or an unfinished staging file, so inspect disk status on the next launch. No disk completion is promised after process exit.

Automated acceptance adds eight editor tests: all operations and invalid edits, preview/reset, save/reload/conflict and confirmation flows, failed reload, slow disk with responsive native input, paste/control isolation, preserved controllers/selection and recoverable resize failure, and a rendered live-recorder run through canceled/applied previews, persistence, reload confirmation, compact menu and clean shutdown. Existing broad layout, recorder and native-hook regressions remain required.

Full regression validation: **136 tests pass on macOS/Python 3.9 (37.677 seconds) and Linux/Python 3.11 (17.748 seconds)**. Human native review remains the checklist below.

### Manual native interaction checks

Repeat in a disposable recorded workspace with each native provider in your actual terminal. Keep normal trust/approval settings. Record terminal/provider/platform versions and PASS/FAIL with a reproduction for any defect:

| Check | Expected result |
| --- | --- |
| Open menu from each pane, then Escape | Same focused pane, historical selection and scroll; no menu text reaches native input |
| Native approval visible, open/cancel menu | Same pending approval on return; no approval answered by editing |
| `layout preview`, `layout use agent-top`, Escape | Original geometry and native dimensions retained |
| Repeat preview then `layout apply` | New geometry; same native session; correct `size` in demo and usable native redraw |
| Swap/flip/axis/ratio, invalid ID/range, reset | Correct tree ordering/ratios; invalid command leaves it unchanged; reset original preset |
| Select past activity, apply/mirror/maximize/compact | Paused selection/detail scroll retained; collection continues |
| Paste control-containing text in menu and in Agent | Menu controls do not submit/quit; normal native bracketed paste remains usable |
| Save under a new workspace name, reload, restart | Saved current tree restored; other destination definitions retained |
| Edit after save, `layout reload`, then `--discard` | First attempt refuses, second reloads; focus retained |
| Externally edit destination, attempt save | Conflict/error shown, live tree and external file preserved |
| Resize while menu open, shrink/grow, scroll details | Menu stays usable; preview alone causes no PTY resize; compact/maximize indicators are consistent |
| Alternate screen, colors, terminal history, normal quit | No lost native input/buffers or stale text; completed recording with no cleanup gap |

These real-provider visual/approval checks are human acceptance, not implied by demo tests. Grouped action/effect selection and prepared visualizer-state acceptance remain in their next slices.

**Next after the editor:** action/effect Activity projection, implemented below.


## Action/effect Activity projection slice

Recorded Activity now displays the provider-neutral correlation projection. It reads the durable session journal off the curses/input thread, with one background replay at a time. New notifications trigger replay at up to 10 Hz, and a two-second periodic reread repairs missed/dropped display notifications. Journal sequence orders rows; it does not imply causal order. Process exit marks still-unfinished calls incomplete in the live view, independently of the recorder's final durable session status.

One primary row represents each scoped call and updates through its observed lifecycle. Identity comes from the existing projector; missing scope stays unlinked/unknown. Session/turn/actor observations and health gaps remain inspectable. Effects expand under candidate calls; shared effects retain a common canonical ID and separate parent-context row IDs. Every effect retains external-or-unknown attribution, including single-call candidates. Unassigned checkpoint changes appear independently. Opaque completion results explicitly display unknown result; request hooks never manufacture a running outcome.

Selection uses stable row/action/effect identities. New activity refreshes selected evidence without moving paused selection; unread counts track new primary rows. Expanded state survives replay and layout changes. Filtering by actor/tool/state/path/turn/text preserves a hidden selection with a visible outside-filter notice; child matches retain parent context. `f`/End resumes follow to the newest visible call or observation. Left/Right/Enter collapse/expand; `d` focuses details. Ctrl-] `r` toggles raw journal rows without destroying the prior identity. Non-recorded launches retain their legacy event-card behavior.

Visualization still presents JSON rather than historical source views. Action details include the projected lifecycle, correlation/identity quality, boundaries, raw recorded observations/arguments/results, linked effects and limitations. Effect details include the canonical effect, checkpoint interval and snapshot metadata. Summary targets are clipped while details retain recorded values. Unchanged projections, filtered row lists and detail serialization are cached across paint/resize; replay and file reads remain outside the input thread. Large-session performance and richer background visualizer preparation remain later acceptance work.

**Validation:** all 143 tests pass on macOS/Python 3.9 (38.764 seconds) and Linux/Python 3.11 (18.139 seconds), including seven new Activity tests. They cover in-place lifecycle updates, unread/follow behavior, shared intervals without ownership, child selection/collapse, filter-hidden selection, unknown identities and final missing outcomes, raw journal restoration, Unicode filter input, durable replay without display notifications, and a rendered authenticated PTY fixture through grouped completion/filter/journal/clean shutdown. Final cache changes are separately rechecked on both platforms.

Manual check: launch a disposable recording with native hooks, execute two harmless read calls and an edit/revert, then focus Activity. Select the first call; later callbacks must update the correct row while selection stays paused. Expand an edit's effects and confirm candidate/unknown attribution. Filter `tool:Read`, then an unmatched term: hidden selection must stay visible in details. Clear with `/`, Ctrl-U, Enter; switch journal/action views with Ctrl-] `r`. Apply a layout change and confirm selection/expanded/filter state survives. Continue native input while browsing. Compare IDs/evidence with `actions RECORDING --session ID` after exit. Normal native trust/approval behavior still applies.

**Next slice:** historical built-in visualizers and read-only evidence access. Saved-session curses review, retention/read coordination and full MVP performance/native visual acceptance remain subsequent work.

## Historical built-in visualizers slice

The internal built-in registry now prepares terminal creation, unified-diff, deletion, command/tool and generic evidence views. Selected effects grant only their recorded path and checkpoint pair; requests must match the original snapshot change and commit references. File reads use the private Git store with inherited Git settings/replacements disabled, never the live workspace or external diff/text-conversion drivers. Symlinks expose captured target bytes only. Non-UTF-8 filenames retain their original bytes and have escaped display names.

Before/after states use the recorded snapshot metadata, including omissions and read-error notices. Empty available files differ from absent paths, metadata-only content, missing checkpoints/objects and retained stale bytes. Baseline creation with no prior capture is labelled First captured file. Missing Git objects/read failures never become empty content or deletion. Binary/non-UTF-8 content uses metadata/evidence fallback. Mode and final-newline changes remain inspectable. Partial and shared intervals keep capture/correlation labels and external-or-unknown attribution.

Command/tool cards show captured arguments, lifecycle, identity/coverage quality, recorded responses/errors and missing-boundary notes. Missing response is unavailable, not empty output; opaque result stays unknown. Commands are never rerun. Multi-effect calls offer an in-pane picker with `]`/`[` while preserving Activity's parent selection; selecting a child directly opens its file view. `s` selects summary/content, `e` JSON evidence, Up/Down scroll vertically, Left/Right horizontally. Modes/positions/picker choices are remembered for up to 128 selections.

Preparation runs off the curses/input thread with one active job, coalescing selection/revision changes. A mismatched result is discarded; loading replaces old content until the current result arrives. Resize/scroll reuses prepared analysis instead of rereading evidence. Historical reads are bounded to 256 KiB per blob; display is bounded to 4000 lines/256 KiB of text, command observations to 64, diffs to 64 KiB/2000 lines per side. Limit notices describe truncated views rather than complete evidence. Git subprocess reads have two-second deadlines; pure-Python analysis runs on the worker without a hard process-level timeout.

This is an internal foundation, not the full proposed VizApi SDK: external scoped handles, JSON-RPC/plugin configuration, cancellation/process limits and export artifacts are not implemented. Live recordings exclude prune through their writer lock. The following slice adds saved review and internal read leases; missing objects are surfaced as unavailable, with no live-file fallback.

**Validation:** all 150 tests pass on macOS/Python 3.9 (44.803 seconds) and Linux/Python 3.11 (19.335 seconds). Seven visualizer tests cover historical edit/create/delete after live-file mutation, empty/binary/invalid UTF-8/symlink/large files, omission/missing baseline/stale content, invalid grants, unusual byte paths/missing objects, command empty/missing/opaque output, stale background results and resize reuse. A rendered PTY recording demonstrates a saved source diff while native input stays usable. Final UI controls are rechecked with targeted tests.

Manual check: record a disposable edit/revert/create/delete sequence, filter Activity by `path:NAME tool:file.modify` to select a checkpoint effect rather than a raw watcher observation, then press `d`. Confirm the saved intermediate diff, creation/deletion content, metadata/missing-data labels and external/unknown attribution. Switch `e`/`s`, scroll in both directions, change layouts, and rapidly change selection while the agent continues. For a multi-effect call, use `]`/`[` and confirm Activity's parent stays selected. Compare a known recorded command failure/opaque outcome with its raw journal card; no renderer should infer success or rerun it. Human native visual checks still apply.

**Next slice:** saved-session review and MVP acceptance, including retention/read coordination and performance/native gates.

## Saved-session review and MVP acceptance slice

`python3 -m labradour review DIRECTORY [--session ID]` opens the latest or chosen
saved session using the existing layout, Activity projection and historical
built-in visualizers. It creates no PTY, agent, watcher, recorder or collector,
does not read the original workspace, and does not write recording files.
The original workspace may be gone. The Agent pane becomes a session selector
with highlighted/loaded markers, persisted status and recorded workspace facts;
terminal output was not recorded and cannot be replayed. Activity starts focused.

In Agent, j/k and Enter switch sessions; r explicitly reloads the highlighted
session, including an active recording's newer facts. Switching loads in one
background job with a bounded result queue and coalesces subsequent requests;
obsolete results cannot replace the requested session. Refresh of the same
session preserves filter/selection/view state; switching sessions resets them.
Loading/errors appear in the footer. A failed refresh retains the prior cached
facts with an error notice. A persisted running status is not repaired by review;
its historical projection closes unfinished calls as incomplete. Layout options
and the editor work as in live mode; `--workspace` is the existing layout
preferences directory (current directory by default), not historical evidence.
Explicit layout saves are the only review interaction that writes preferences.

Before/after file reads acquire a shared advisory directory lock; this needs no
lease file or write access. Git reads have a two-second individual timeout and
five-second aggregate comparison deadline. The lease is released before Python
analysis and display; cached views never indefinitely pin sessions. Applied
retention, resumed prune and loose-object reclamation take the matching exclusive
lock and reject busy evidence promptly. Nested maintenance uses a thread-local
reentrant scope. Optional startup object reclamation skips busy readers rather
than preventing a new recording; pending destructive prune must finish safely
or fail with a retry notice. Read-only CLI diff also holds a short lease.
Locks coordinate cooperating Labradour code, not unrelated deletion tools.

An idle viewer can outlive a pruned session. Cached analysis may remain, while
new reads report unavailable/pruned bytes and refresh rejects the removed
session. Missing bytes never become empty content or evidence of deletion and
never fall back to live workspace files. Prepared jobs still reject stale
selection/revision results. External plugin lease tokens, hard Python worker
cancellation, arbitrary plugin transport and graphical export remain later work.

**Validation:** all 157 tests pass on macOS/Python 3.9 (50.251 seconds) and
Linux/Python 3.11 (20.638 seconds). Seven new tests cover missing original
workspace/read-only recording digests, no runtime services, background session
switching/pruned refresh, cross-process prune refusal/release, reader/recorder
coexistence, persisted running status without repair, same-session state
preservation, leases across actual comparison pairs, pruned-byte fallback and
rendered saved-session filtering/diff/navigation/compact resize/clean exit.
The existing suite covers live adapter Activity, historical views, layout editor
input isolation, process cleanup, storage limits and crash recovery.

The extended disposable performance probe uses 1,000 files, 20 iterations,
background writes every 20 ms and synthetic native input while alternating
summary/evidence preparation. Saved replay has 1,325 records/2,302 rows and
20 historical source comparisons. Full suites were also running during these
measurements; results are local samples, not performance guarantees.

| Measurement | macOS | Linux | Target |
| --- | --- | --- | --- |
| Small changed-file capture p95 | 222.677 ms | 53.894 ms | <500 ms |
| Live checkpoint visibility p95 | 312.661 ms | 123.741 ms | <500 ms |
| Filesystem row visibility p95 | 131.237 ms | 72.789 ms | <250 ms |
| Input p95 overhead with recording/writes | 5.080 ms | 4.014 ms | <50 ms |
| Input p95 overhead with recording/review/writes | 12.968 ms | 4.554 ms | <50 ms |
| Saved replay initial load | 9.172 ms | 8.346 ms | Reported; no prior target |
| Historical source preparation p95 | 142.541 ms | 5.341 ms | Reported; off input thread |
| Watcher drops | 0 | 0 | 0 |

Initial baseline capture took about 1.2 seconds on both hosts and precedes agent
release; incremental visibility targets do not cover cold startup. The probe
does not certify arbitrary session sizes, provider dialogs, managed policy or
human color/input fidelity. Raw measured reports:
[macOS](mvp-performance-macos.json), [Linux](mvp-performance-linux.json).

**MVP status:** implementation and automated acceptance for Phase 4 are complete
in this measured scope. Human native sign-off remains open in
[Phase4TestGuide.md](Phase4TestGuide.md#7-human-native-mvp-sign-off). That guide
provides disposable walkthroughs, expected views, missing-data/retention checks,
performance reproduction and explicit unimplemented features. Applicable human
rows must pass or receive explicit scoped acceptance before claiming full MVP
native acceptance. Earlier Phase 2 deployment-policy gaps retain their status.
