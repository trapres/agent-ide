# Labradour UI guide

Labradour runs your agent in its native terminal and puts activity and review alongside it. This guide describes the current controls and identifies features planned for later phases.

## Start a session

From the repository root, launch an agent in your workspace:

```sh
python3 -m labradour run --workspace /path/to/project -- codex
python3 -m labradour run --workspace /path/to/project -- claude
```

To save filesystem history, choose a private recording directory. Reusing it creates a new session and preserves older sessions:

```sh
python3 -m labradour run --workspace /path/to/project \
  --record /path/to/labradour-recording -- codex
```

Add `--hooks codex` or `--hooks claude` for launch-scoped provider hooks. With `--record`, these automatically enable authenticated normalization and bounded boundary captures. Without recording, they retain observation-only raw events. Follow normal provider trust and approval prompts. Filesystem changes remain external/unknown. Core macOS native delivery, denial/concurrency and Linux Codex delivery now have measured evidence; the broader native gate remains open; see [Phase 2 notes](docs/Phase2.md) for receipts, gaps, and the manual gate.

Recording stores included workspace contents locally, including untracked and already modified files. Default exclusions cover Git administration, dependency/build directories, common credential directories, `.env` files, and recorder data. See [Phase 1 notes](docs/Phase1.md) for the exact policy and limits. A recording is intended for one workspace.

For a disposable demo:

```sh
python3 -m labradour run --demo --watch
```

Type `run` in the Agent pane for sample activity, `size` for terminal dimensions, or `colors` for color samples. Add `--record /path/to/demo-recording` to try saved checkpoints. Each demo has a new temporary workspace, so use a fresh recording directory for each demo launch.

## Choose and inspect capture rules

Preview the effective rules without launching an agent or creating a recording:

```sh
python3 -m labradour policy --workspace /path/to/project \
  --record /path/to/labradour-recording \
  --exclude 'private/**' --metadata-only 'assets/**' \
  --max-file-bytes 4MiB --max-capture-bytes 32MiB --storage-budget 256MiB
```

Use the same options with `run --record ...` to apply them. Repeat `--exclude` and `--metadata-only` for additional rules. Patterns match workspace-relative paths and their ancestors, case-sensitively; `*` can match across directory separators. Use `private` to exclude that entire subtree, or `*.key` for matching filenames anywhere below the root. Quote patterns so your shell does not expand them.

For reusable rules, save a JSON file and pass `--capture-policy /path/to/capture.json`:

```json
{
  "schema_version": 1,
  "exclude": ["private", "*.key"],
  "metadata_only": ["assets/**"],
  "max_file_bytes": 4194304,
  "max_capture_bytes": 33554432,
  "storage_budget_bytes": 268435456,
  "max_event_bytes": 1048576
}
```

JSON limits use positive integer byte counts. CLI limits also accept `KiB`, `MiB`, and `GiB`. CLI size options override file values; CLI patterns extend the file's pattern lists. Invalid fields or limits fail before launching. Policy options on `run` require `--record`.

Default exclusions remain active unless you explicitly supply `--no-default-exclusions` or set `"use_default_exclusions": false` in the JSON file. Git administration, recorder/staging data, and the active hook spool remain excluded. The effective policy is printed before recording, initially shown in Visualization, and saved as a per-session `capture-policy-SESSION_ID.json` in the recording directory. Press **Ctrl-] then `p`** to toggle its view during a session.

Files above the file/scan limit and metadata-only matches have omission reasons in checkpoint details. `partial capture` in the footer reports omission and read-error counts. A previously captured file omitted from a later snapshot is labeled `file.omitted`, not `file.delete`; plain Git diffs can still show its absence as a deletion, so check snapshot metadata when reviewing a partial checkpoint. Captures process directories/files in sorted traversal order to make scan-limit choices repeatable.

## Storage exhaustion

The hard budget applies to the sum of regular-file bytes inside the recording directory, including journal, Git history, policy files, and a reserved health record. The default is 512MiB; the minimum configurable budget is 64KiB. Filesystem allocation overhead, temporary staging, and the hook spool are separate from this budget. Normal capture staging copies the journal and materializes newly hashed content/objects; it reads retained Git objects without copying the whole repository. Initialization repair and retention can stage a repository copy. These operations need extra space on the same filesystem. Successful and rejected writes clean it up. On the next launch or applied prune, Labradour removes abandoned staging directories identified as belonging to that store. Older staging directories without a store identifier remain untouched.

If a write cannot fit, Labradour displays **RECORDING STOPPED: storage budget; agent continues**. Earlier history remains available; later activity/content is not recorded. A fixed-size health record preserves the reason even when the journal has no room. Inspect it after exit:

```sh
python3 -m labradour recording-status /path/to/labradour-recording
```

Restart with a larger `--storage-budget` or choose a fresh recording directory. Retention cleanup is explicit; use the preview/apply workflow below. A budget smaller than the existing store is rejected without deleting saved history. The hook spool must be outside the recording directory so external hook writes cannot bypass the store's budget.

## Recover and retain saved sessions

After a crash, launch Labradour with the same workspace and recording directory. It releases no agent input until recovery and the new baseline finish. The previous session remains available as `interrupted`; unfinished snapshots record the retained commit and whether a new checkpoint was installed. A committed session-end event is recognized as completed even if its status-table update was interrupted. Recovery starts a separate session from current workspace contents.

On disk-full or another storage error, the footer shows **RECORDING STOPPED: storage failure; agent continues**. `recording-status` shows the reason. Labradour tries an overwrite of the reserved health slot if it cannot create staging; if even that write fails, the footer says health could not be persisted. Restore writable storage/free space before restarting. This does not claim recovery from arbitrary filesystem corruption or power loss.

Preview whole-session retention after closing the recorder:

```sh
python3 -m labradour prune /path/to/labradour-recording --keep-sessions 5
```

The JSON result lists sessions to keep/remove and the current retained bytes. To execute that policy:

```sh
python3 -m labradour prune /path/to/labradour-recording --keep-sessions 5 --apply
```

`--apply` removes the old sessions' journal rows, policy files, and private Git session refs, compacts the journal, and reclaims unreachable loose Git objects. Earlier checkpoints within each surviving session remain available. Shared objects and commits explicitly referenced by surviving journal evidence remain protected. Packed Git objects stay intact; pack compaction is deferred, so reclaimed space may be smaller for a store packed with external Git tools. The result reports actual retained bytes and object reclamation.

The default retains the newest 10 sessions; the minimum is one. Running-status sessions are protected in previews, and applying retention while a writer is active is refused. If a crash left a session marked running, restart once to recover its status before considering it for removal. The preview reports a pending prune; the next launch or applied prune resumes its originally recorded removals before proceeding.

Pruning is resumable: the health record holds its intent, the journal is pruned before refs/content are removed, and repeated cleanup is safe. A journal audit table retains the removed session IDs. The next launch also reclaims unreferenced loose fragments from incomplete checkpoint installations. Cleanup never modifies the workspace or project Git repository. There is no age-based or automatic retention in this slice.

## The three windows

```text
+-------------------------+-------------------------+
|                         | Activity                |
| Agent                   | Events and checkpoints  |
| Native CLI              +-------------------------+
|                         | Visualization           |
|                         | Selected event details  |
+-------------------------+-------------------------+
```

The Agent takes half the screen. Activity and Visualization share the other half. The title bar names the focused pane and shows whether selection follows live activity. The footer shows help, recording status, and coverage notices. With recording enabled it also reports the latest scan duration, files read/reused from cache, and watcher queue depth when screen space permits. Checkpoint details include scan/capture preparation metrics.

The **Agent** pane accepts normal typing, slash commands, paste, approvals, and Ctrl-C. Input goes to the agent while this pane has focus, except Labradour's reserved controls below.

The **Activity** pane shows a grouped action/effect projection when recording is enabled: one row per scoped tool call, lifecycle updates in place, expandable candidate effects, external/unknown changes and lifecycle/health observations. Unknown identities and outcomes stay explicit. Without recording, it retains the raw hook/event display. Select an older row without stopping the agent; Ctrl-] then `r` switches recorded Activity to the underlying journal view.

The **Visualization** pane renders captured creation content, source diffs, deletion content, command/tool cards, or generic evidence for the selected recorded row. Views use saved checkpoints and recorded results, never current workspace files. Evidence/attribution labels remain visible. Explicit local companions are available after exporting; see the controls below.

## Focus and navigation

Press **Ctrl-]**, release it, then press the command key within two seconds. For example, Ctrl-] followed by plain `l` focuses Activity. Commands work from every pane.

| Keys | Action |
| --- | --- |
| Ctrl-] then `a` | Focus Agent |
| Ctrl-] then `l` | Focus Activity |
| Ctrl-] then `v` | Focus Visualization |
| Ctrl-] then Tab | Cycle through the three panes |
| Ctrl-] then `?` | Show prefix help in the footer |
| Ctrl-] then `p` | Toggle effective capture policy in Visualization |
| Ctrl-] twice | Send a literal Ctrl-] to the agent |
| `j` / Down in Activity | Select the next event; pause live follow |
| `k` / Up in Activity | Select the previous event; pause live follow |
| `f` in Activity | Select the newest event and resume live follow |
| `j` / Down in Visualization | Scroll details down |
| `k` / Up in Visualization | Scroll details up |

Typing `j`, `k`, or `f` in the Agent pane sends those characters to the agent. Focus Activity first to browse history. New rows do not move a historical selection while live follow is paused. Call lifecycle/evidence updates still refresh that selected row.

## Browse recorded actions and effects

Focus Activity with Ctrl-] then `l`. Use Up/Down or `j`/`k` to select a row and pause follow. Right or Enter expands a call's candidate effects; Left collapses, returning a selected child to its parent. `f` or End resumes follow and selects the newest visible call (or latest observation when no calls exist). The unread counter counts newly added primary rows while follow is paused.

Press `/` to edit a filter. Space-separated terms are combined; use `actor:worker tool:Write state:completed`, `path:src/`, `turn:ID`, or ordinary text. Enter applies, Escape/Ctrl-C cancels, and Ctrl-U clears the input. Filters are case-insensitive substring matches. A path match can reveal child effects with their parent context. Unknown actor/turn identities remain `unknown`. Clearing a filter preserves the previous stable selection. If the selected row is hidden, Activity says **Selection outside filter/view** and Visualization retains its details until you navigate elsewhere.

Press `d` to focus Visualization on the selected details. Use its normal scroll keys. Ctrl-] then `r` toggles the underlying journal view, retaining the selection identity for return. This is useful for inspecting raw lifecycle, snapshots, adapter boundaries and health observations.

Child effects are **candidate intervals**, not proof that a call wrote the file. Their attribution stays external/unknown. A shared checkpoint effect can appear under several candidate calls, with the same canonical effect ID; each remains one tool invocation. Unassigned effects appear independently. A completed hook with opaque results displays `result:unknown`; missing outcomes remain requested/running/awaiting approval while live, or incomplete after process exit. Details preserve missing boundaries and conflicting outcomes. Select a checkpoint effect to see its historical source view; a raw watcher observation remains a generic event card.

## View historical content

Select a recorded effect in Activity, then focus Visualization with Ctrl-] then `v` or `d` from Activity. Creation shows captured new content; deletion shows last captured content; modification shows a unified diff. First captured baseline content is labelled **First captured file** when no earlier absence is established. Command/tool cards show captured inputs, lifecycle, results and evidence gaps; they never execute the recorded command.

| Visualization keys | Action |
| --- | --- |
| `s` | Built-in summary/content view |
| `e` | JSON evidence and recorded observations |
| `]` / `[` | Next/previous candidate effect within a selected multi-effect call |
| Up/Down or `j`/`k` | Vertical scroll |
| Left/Right | Horizontal scroll in 20-cell steps |

Activity keeps its parent selection when you use the in-pane effect picker. Summary/evidence choice and scroll positions are remembered for up to 128 selections. Layout/resize reuses prepared analysis. Rapid selection changes show loading while obsolete work finishes; an obsolete result cannot replace the new selection.

State labels distinguish captured empty content, absent paths, metadata-only/omitted content, unavailable checkpoints, retained stale bytes, binary/non-UTF-8 content, and symlink targets. Missing output is explicitly unavailable rather than empty. Opaque native completion results remain unknown. Partial/shared intervals never establish exclusive file ownership. Symlink destinations are never followed.

Views are bounded: 256 KiB per file read, 4000 display lines, and text diffs limited to 64 KiB/2000 lines per side. Limit notices describe an incomplete display, not a complete comparison. For larger evidence, use the saved history/diff CLI. Capture policy exclusions cannot be bypassed by choosing a view. Local static companions display exported evidence; external plugin transport remains future work.

## Layout and terminal history

| Keys | Action |
| --- | --- |
| Ctrl-] then `m` | Move Agent to the opposite side |
| Ctrl-] then `z` | Maximize the focused pane; repeat to restore |
| Ctrl-] then `+` / `-` | Increase/decrease Agent width |
| Ctrl-] then `]` / `[` | Increase/decrease Activity height |
| Ctrl-] then `b` | Scroll Agent terminal history back |
| Ctrl-] then `n` | Return Agent terminal to live output |

Resize your outer terminal normally; Labradour updates the Agent's PTY dimensions. Below 100 columns or 28 rows, the focused pane fills the screen. Focus another pane to see it. Layout changes last for the current session.

Phase 4's layout foundation now supports `--layout default`, `agent-right`, `agent-top`, or `visualization-top`. For example, run `python3 -m labradour run --demo --layout agent-top` to put Agent above the two review panes. With a preset, prefix-Tab follows pane order in the tree and prefix-Shift-Tab reverses it. Mirror reflects columns; width/height shortcuts adjust the nearest matching split, or report that none exists. Do not combine `--layout` with the legacy side/ratio launch flags.

Layout files and explicit saves are now available. Load a custom v1 JSON file with `--layout-config FILE`, or select a name defined in user/workspace settings with `--layout NAME`. Preferences resolve from built-ins, then user settings, workspace settings, and the explicit file. `--ignore-layout-config` skips discovery and cannot be combined with an explicit file.

The user file is `$XDG_CONFIG_HOME/labradour/layout.json` (when XDG_CONFIG_HOME is absolute), otherwise `~/.config/labradour/layout.json`. The workspace file is `.labradour/layout.json` directly in the requested workspace. Demo launches also use the requested `--workspace` for layout discovery; their generated files still live in a disposable demo workspace.

Inspect settings without starting an agent:

```sh
python3 -m labradour layout status --workspace /path/to/project
```

To save a selected tree under a new name and make it the active workspace layout:

```sh
python3 -m labradour layout save coding --scope workspace \
  --workspace /path/to/project --layout agent-top
python3 -m labradour run --workspace /path/to/project -- claude
```

Use `--scope user` for a user default (also the save command's default scope). An existing higher-priority workspace file can override a user save. Saves preserve other definitions and use an atomic replacement. They never occur automatically on launch, resize, focus or exit. A file changed since loading causes a refusal; reload/retry instead of discarding that change. `--confirm-shadow` explicitly permits shadowing an inherited name; `--confirm-replace` permits replacing an ignored/invalid destination. Unsupported schema versions, oversized files and non-regular/symlinked destinations remain refused.

Press **Ctrl-] then `i`** during a session to toggle layout diagnostics in Visualization: sources, warning reasons, current tree, requested/effective ratios, geometry and unsaved changes. Ordinary session shortcuts remain transient. The in-session editor is available through **Ctrl-] then `:`**; commands below edit and save the current live tree. See [Phase 4 checks](docs/Phase4.md), [the layout contract and JSON examples](AgentUI.md#2-layout-configuration-and-resizing), and [Phase 3 acceptance scenarios](docs/Phase3.md).

## Edit layouts during a session

Press **Ctrl-] then `:`** from any pane. The layout menu covers the display while the agent and recorder stay live at their existing dimensions. Type a registered command and press Enter; the menu stays open. Escape or Ctrl-C closes it and preserves focus/selection. Menu text, paste and navigation do not reach the agent. Ctrl-Q still quits Labradour.

```text
layout use agent-top
layout swap activity visualization
layout axis main columns
layout ratio main 6000
layout flip review
layout reset default
layout status
```

The tree lists pane names and split IDs (`main`/`review` in the presets), axes and requested/effective ratios. Use those IDs in commands. Invalid commands leave the tree unchanged with a visible reason. `reset` selects the original built-in even when configuration shadows its name. Up/Down and PageUp/PageDown scroll menu details in small terminals; typing returns to the command prompt.

For a preview, enter `layout preview`, then your normal edit commands. The preview tree changes while the applied windows and Agent PTY size stay unchanged. `layout apply` commits it; `layout cancel` or Escape discards it. Commands outside preview mode apply immediately. Focus, paused follow, selected event and detail scroll remain intact.

Save the applied current tree explicitly:

```text
layout save coding workspace
layout save personal user
layout reload
```

Saves run off the input thread, preserve other definitions, and use the same conflict checks as CLI saves. Add `--confirm-shadow` when intentionally shadowing an inherited name, or `--confirm-replace` when replacing an ignored/invalid file; unsupported versions remain protected. Saving is refused while previewing. If reload would discard unsaved edits or a preview, repeat `layout reload --discard` only when you intend to discard them.

Reload rereads discovery plus the original explicit file/ignore setting; it selects the active disk preference rather than reapplying the initial CLI name/legacy ratios. Live focus stays where it was, even if the file's initial-focus preference changes. Disk errors leave the current tree usable. Layout edits wait while a save/reload runs; native input, collection, focus and maximize continue after closing the menu. Quitting during a pending save remains available; inspect the file/status afterward because the disk job may have completed or been interrupted. No automatic save occurs on menu close or exit.

## Exit and review saved history

**Ctrl-Q** quits Labradour from any pane. Alternatively, press Ctrl-] followed by plain `q`. Quitting stops the launched process group and takes a final checkpoint when recording is enabled. Ctrl-C in Agent interrupts the agent using its normal terminal behavior.

When the agent exits on its own, the review panes stay open until you quit. Saved recordings remain on disk after Labradour closes. Review them without launching an agent:

```sh
python3 -m labradour history /path/to/labradour-recording
python3 -m labradour review /path/to/labradour-recording
python3 -m labradour review /path/to/labradour-recording --session SESSION_ID --layout agent-top
python3 -m labradour history /path/to/labradour-recording --session SESSION_ID
python3 -m labradour diff /path/to/labradour-recording BEFORE_COMMIT AFTER_COMMIT
```

Copy a session ID from the first command, then full `commit` IDs from `snapshot.completed` records. Compare adjacent checkpoints to see intermediate edits, including edits later reverted. Comparing only the baseline and final checkpoint can produce an empty diff even when work occurred between them.

`review` starts focused on Activity and opens the latest saved session by default. It launches no agent, recorder, watcher or hooks and never reads the original workspace. Agent becomes a session selector: Ctrl-] then `a`, `j`/`k` to highlight, Enter to open, `r` to refresh the highlighted session. `>` marks the highlighted session; `*` marks the loaded session. Session loading runs in the background; the footer reports loading/errors. Refreshing the same session preserves filters, selection and view position; changing sessions resets them. Activity and Visualization use the same controls as live recordings. Terminal output was not recorded and cannot be replayed.

Saved review does not repair interrupted recordings or change a persisted `running` status. Unclosed calls are shown with incomplete outcomes in this historical view. Refresh is explicit, including when reviewing an active recording. Layout options and the editor also work; `--workspace` selects an **existing directory for layout preferences only**, defaulting to your current directory. The original workspace may have been moved or deleted. An explicit layout save can write preferences there; ordinary review does not write recording files.

Historical comparisons hold a short read lease across captured before/after bytes. Applied retention and destructive startup maintenance use the matching exclusive lease. A busy prune fails promptly with a retry message; retry when preparation finishes. An open review window does not pin sessions indefinitely. If its session is pruned between reads, cached facts may remain visible, but new reads/refresh report unavailable or pruned evidence. No current-workspace content substitutes for missing captured bytes. See [Phase4TestGuide.md](docs/Phase4TestGuide.md) for MVP acceptance checks.

Metadata is reconciled every two seconds. Unchanged files reuse captured bytes after stat checks; dirty paths, startup, overflow, shutdown, and every fifteenth capture attempt trigger fresh reads. Unchanged periodic reconciliations reuse the existing checkpoint without adding detail cards.

Recording captures observed states rather than every write. Short-lived files and rapid changes between scans may be missed. `partial` captures, overflow events, and failed checkpoints identify gaps; an interrupted session is preserved and a later launch reconciles the current workspace into a new session.

## Built-in review visualizations

Phase 4 now provides the recorded action/effect list, filters, stable selection and historical built-in views:

| Selected effect | Built-in view |
| --- | --- |
| File creation | Captured new content |
| File modification | Historical source diff |
| File deletion | Last captured content and deletion details |
| Shell command | Command, lifecycle, and available output |
| Read/search or unknown tool | Tool details and evidence card |
| Binary change or recording gap | Metadata and available evidence |

Multi-effect tool calls offer expanded Activity effects and an in-pane effect picker. Phase 5 adds optional gitdiffviz structural exports and a separate companion open action. Reviewing an event uses recorded evidence without rerunning its command. External plugin transport remains proposed.

## Export selected recorded evidence

In live or saved review, **Ctrl-] then x** explicitly exports the selected row as a JSON evidence artifact. A picked effect in Visualization exports that effect when its parent is still selected. Export keeps focus and selection; it runs in the background while Agent input/recording continue. **Ctrl-] then t** toggles export status in Visualization, including the artifact path, source selection/session, size and SHA-256. **Ctrl-] then c** requests cancellation. `s`/`e` restores the historical view. Selecting or resizing a view never exports or opens a window.

The default private cache is `$XDG_CACHE_HOME/labradour/exports` or `~/.cache/labradour/exports`. Set `run`/`review --export-cache DIRECTORY` to choose a separate cache outside the workspace and recording. It is created only on an explicit export. Defaults: 64 MiB per artifact, 128 MiB total including unfinished files, 32 completed artifacts, seven-day lazy expiry. Repeated unchanged exports reuse verified content-addressed artifacts. Expired/oldest artifacts and abandoned pending files are cleaned on a later explicit export; the cache is not a permanent archive.

Artifacts include provenance and recorded payloads/linked observations; a selected effect includes base64 captured before/after bytes and explicit omission/missing-data states. File reads retain the 256 KiB-per-side historical limit. Commands are not rerun. Exports can retain captured private contents after source-session pruning; remove those derived copies explicitly when no longer needed:

```sh
python3 -m labradour export RECORDING --session SESSION_ID --row list
python3 -m labradour export RECORDING --session SESSION_ID --row ROW_ID --export-cache CACHE
python3 -m labradour export-cache --export-cache CACHE
python3 -m labradour export-cache --export-cache CACHE --clear
```

The optional gitdiffviz adapter exports structural diff/scene JSON for a selected captured regular-file pair: configure `run`/`review --gitdiffviz-config FILE`, then **Ctrl-] g**. CLI export accepts `--exporter gitdiffviz --gitdiffviz-config FILE`. Configuration explicitly pins an absolute executable, SHA-256 and supported source revision. Missing/broken tools fail locally; `x` and built-in terminal views still work. No whole workspace/history is passed to the backend. [Phase5TestGuide.md](docs/Phase5TestGuide.md#5-configure-the-optional-gitdiffviz-adapter) covers build/configuration and measured limits. External plugin RPC remains proposed.

After either JSON export completes, **Ctrl-] o** prepares and opens a host-rendered static HTML companion. It opens the last completed export, even if Activity selection has changed. Ctrl-] t shows source metadata plus the `companion` path/hash and `open_status`. The page shows captured states, evidence/provenance and optional structure cards, with no scripts, network resources or local server. Opening stays in the background. Opener failures retain the page for inspection/retry. Browser windows belong to you and stay open after quitting Labradour. HTML and JSON share cache limits; eviction/clear can remove either, so re-export a missing source. CLI opening requires its completed JSON ID and exact SHA-256: `python3 -m labradour open-export ARTIFACT_ID --sha256 SHA256 --export-cache CACHE`.

## Inspecting correlation before the review UI

Run `python3 -m labradour history RECORDING` to find a session, then `python3 -m labradour actions RECORDING --session SESSION_ID`. This prints replayed tool states, explicit actor relationships, output references, checkpoint effects, candidate calls, and evidence gaps. Recorded Activity now displays this projection, with built-in historical views and JSON evidence details; Ctrl-] then `r` retains raw journal inspection. The CLI is still useful for complete replay/export. A candidate call never establishes exclusive file ownership. See [Phase2TestGuide.md](docs/Phase2TestGuide.md) for scenarios and interpretation.
