# Labradour MVP UI specification

Date: 2026-10-06. Status: Phase 3 layout specification complete; runtime implementation belongs to Phase 4. Companion architecture: [AgentIDEPlan.md](AgentIDEPlan.md).

The layout contract below supports arbitrary binary arrangements of the same three panes. Activity-above-Visualization is the default preset. Phase 4 now implements validated trees, geometry, four presets, layered file configuration, initial focus, diagnostics and explicit CLI persistence alongside legacy side/ratio controls. The in-session command editor, preview/apply/cancel and save/reload/reset flows are now implemented. Recorded Activity now implements stable action/effect selection, filters, candidate effect expansion and raw journal fallback; historical built-in visualizers now use granted saved checkpoints with background preparation and explicit evidence states. Current availability is documented in [docs/Phase4.md](docs/Phase4.md); the full implementation gates are in [docs/Phase3.md](docs/Phase3.md).

## 1. Screen structure

The MVP has exactly three primary panes:

- **Agent:** the interactive native Codex, Claude, or other CLI.
- **Activity:** a tool/actor/action log.
- **Visualization:** the selected action's visual explanation.

The default is Agent on the left and Activity above Visualization on the right. Default ratios are 50:50, producing one half-screen pane and two quarter-screen panes. A split divides the area inside the shared header/footer, including each leaf's border; pane content excludes that border. Every terminal cell belongs to one pane rectangle, without overlapping shared borders. Other presets can reorder panes and change split axes.

```text
Agent on left (default)
+-------------------------- Labradour: workspace / session ------------------+
| Agent CLI                            | Activity                          |
|                                      | actor  tool/action  state         |
|                                      | main   Read         completed     |
|                                      | main   Write        completed  <  |
|                                      | main   Bash         running       |
|                                      +-----------------------------------+
|                                      | Visualization: file created       |
|                                      | src/widget.py     +32 lines       |
|                                      | selected captured contents        |
|                                      | + def render_widget(...):         |
|                                      | + ...                             |
+---------------- focus / recording health / keys -------------------------+

Agent on right
+-------------------------- Labradour: workspace / session ------------------+
| Activity                             | Agent CLI                         |
| actor  tool/action  state             |                                   |
| main   Read         completed        |                                   |
| main   Write        completed  <      |                                   |
| main   Bash         running          |                                   |
+--------------------------------------+                                   |
| Visualization: file created          |                                   |
| src/widget.py     +32 lines           |                                   |
| selected captured contents            |                                   |
| + def render_widget(...):             |                                   |
| + ...                                |                                   |
+---------------- focus / recording health / keys -------------------------+
```

An affected-file selector, action details, filters, and checkpoint controls live inside these panes as tabs or overlays. They do not introduce a permanent fourth pane. The Agent pane stays live while the user reviews past actions.

## 2. Layout configuration and resizing

### 2.1 Split-tree contract

A layout is a binary tree with exactly three leaves: `agent`, `activity`, and `visualization`, each occurring once. A `columns` split places `first` on the left and `second` on the right; a `rows` split places `first` above `second`. Axis names describe the dimension being divided. Two splits suffice for every supported arrangement; arbitrary additional panes, floating windows and hidden persistent leaves are outside v1. Maximize and compact mode temporarily change visibility without modifying this tree.

The configuration is UTF-8 JSON, at most 64 KiB. The complete v1 shape is:

```json
{
  "schema_version": 1,
  "active_layout": "default",
  "initial_focus": "agent",
  "layouts": {
    "default": {
      "type": "split",
      "id": "main",
      "axis": "columns",
      "ratio_bps": 5000,
      "first": {"type": "pane", "pane": "agent"},
      "second": {
        "type": "split",
        "id": "review",
        "axis": "rows",
        "ratio_bps": 5000,
        "first": {"type": "pane", "pane": "activity"},
        "second": {"type": "pane", "pane": "visualization"}
      }
    }
  }
}
```

`ratio_bps` is the first child's requested share in integer basis points: 5000 means 50%. Valid values are 1000–9000 inclusive. Booleans are not integers. Leaf fields are exactly `type` and `pane`; split fields are exactly the six fields shown. Split IDs are unique within a tree. Layout names and split IDs contain 1–32 ASCII letters/digits/underscores/hyphens and begin with a letter. There are at most 32 named layouts per file. Depth is at most three nodes on a root-to-leaf path. The active name must resolve to a built-in or a custom definition after layers are combined. A custom definition can shadow a built-in name; reset can always recover the original built-in.

Reject duplicate JSON keys, unknown fields, non-finite numbers, wrong types, unknown panes/axes, duplicate or missing panes, repeated split IDs, and unsupported versions. Validate the size before parsing and the structure before recursively traversing it. A tree is replaced as a whole; never merge partial nodes across configuration layers. No commands, providers, visualizer plugins, environment variables or executable expressions are accepted in this file. Visualizer preferences remain a separate future configuration contract.

### 2.2 Presets and worked arrangements

Built-ins are `default`, `agent-right`, `agent-top`, and `visualization-top`. All start with 5000 ratios. `agent-right` exchanges the root children of `default`. `visualization-top` exchanges the two review leaves of `default`:

```json
{
  "type": "split", "id": "main", "axis": "columns", "ratio_bps": 5000,
  "first": {"type": "pane", "pane": "agent"},
  "second": {
    "type": "split", "id": "review", "axis": "rows", "ratio_bps": 5000,
    "first": {"type": "pane", "pane": "visualization"},
    "second": {"type": "pane", "pane": "activity"}
  }
}
```

`agent-top` uses a horizontal divider with side-by-side review panes below:

```json
{
  "type": "split", "id": "main", "axis": "rows", "ratio_bps": 5000,
  "first": {"type": "pane", "pane": "agent"},
  "second": {
    "type": "split", "id": "review", "axis": "columns", "ratio_bps": 5000,
    "first": {"type": "pane", "pane": "activity"},
    "second": {"type": "pane", "pane": "visualization"}
  }
}
```

Add either tree under its name in `layouts` in the complete file above, then set `active_layout` to that name. A custom tree can also place Visualization full-height on the left with Agent above Activity on the right, or put all three panes in a single row using two nested `columns` splits. Pane identity does not depend on its position.

```text
agent-top                         visualization-top
+--------------------------------+ +----------------+----------------+
| Agent                          | | Agent          | Visualization  |
+----------------+---------------+ |                +----------------+
| Activity       | Visualization | |                | Activity       |
+----------------+---------------+ +----------------+----------------+
```

At 140 columns × 40 rows, reserve one header and one footer row, leaving 140 × 38. `default` yields Agent 70 × 38, Activity 70 × 19 and Visualization 70 × 19. Agent content is 68 × 36. `agent-top` yields Agent 140 × 19 (content 138 × 17) and each review pane 70 × 19. At 141 columns the first root child receives 71 columns and the second 70. A 6000 root ratio at 140 columns requests 84/56; subtree minimums may constrain the effective division.

### 2.3 Persistence, precedence and migration

The launch options are `--layout NAME`, `--layout-config FILE`, and `--ignore-layout-config`. Discover configuration once at startup in this order, lowest to highest:

1. Built-in presets and defaults.
2. User file: `$XDG_CONFIG_HOME/labradour/layout.json`, or `~/.config/labradour/layout.json` when XDG_CONFIG_HOME is unset or not absolute, on macOS and Linux.
3. Workspace file: `.labradour/layout.json` directly under the canonical launched workspace; do not search parent directories.
4. Explicit `--layout-config FILE`, resolved relative to the launcher working directory.
5. Explicit `--layout NAME` or the legacy side/ratio launch flags.
6. Current-session edits.

At each file layer, `schema_version` is required; `layouts`, `active_layout`, and `initial_focus` are optional. Supplied layouts replace matching names, while omitted names and top-level preferences inherit. Defaults are active `default` and initial focus `agent`. An active name is checked after all valid file layers are resolved; an unresolved name from discovery warns and falls back to built-in `default`. An explicitly requested unresolved name fails before PTY launch. `--ignore-layout-config` skips all discovered files; combining it with an explicit file is an argument error.

Malformed automatically discovered files are ignored as whole layers with a visible warning and source path. An unreadable or invalid explicit file is a startup error. Unknown schema versions are never rewritten. Symlinks and non-regular config files are refused. Project configuration is declarative data; reading it does not launch processes or save files. The command menu's layout status shows each source, effective tree, requested/effective ratios, ignored-layer reasons and unsaved changes.

Legacy `--side`, `--agent-width` and `--activity-height` remain supported. If any is explicitly supplied, construct the legacy default tree, using left/0.5/0.5 for omitted values; do not apply positional width/height changes to an arbitrary tree. Reject combining these flags with explicit `--layout`. Existing clamping to 0.3–0.7 is retained for these legacy flags, converting the result to basis points. No flag means the discovered active tree wins. The previously proposed `[ui]` TOML example was never a persisted implementation and is not an auto-loaded format. No v0 disk migration exists; future versions require an explicit, documented migration that preserves the original file and rejects newer unknown versions.

Edits are session-local until explicit `layout save NAME user|workspace`. Save the current tree under NAME and select it as active, preserving other valid definitions in that destination. User saves write the user file; workspace saves write only the launched workspace file and may appear as ordinary workspace changes in recordings. Never save automatically during resize, maximize, focus, launch, or shutdown. Saving an ignored/invalid destination requires explicit confirmation to replace it; warn before shadowing an inherited name. Use a same-directory temporary file, restrictive permissions, flush/fsync and atomic replace. Refuse a destination changed since it was read; let the user reload/retry. A failed save leaves the session layout usable with an unsaved/error notice. Do not write to the recording store or native provider settings.

Current slice availability: `python3 -m labradour layout status` inspects the resolved configuration; `layout save NAME --scope user|workspace` explicitly saves the CLI-selected tree, with `--confirm-replace`/`--confirm-shadow` for the cases above. Ctrl-] then `i` displays live source/geometry diagnostics. The persistence API accepts an edited session tree, and in-session menu saves/reload are available through Ctrl-] then `:`. Non-regular/symlinked config files and symlinked direct config directories are refused; oversized destinations are not replaced. Cooperating saves use a nonblocking advisory lock plus identity/content checks. An unrelated external editor can race the final check/replace; this is not an operating-system transaction against arbitrary uncooperative writers. Workspace layout changes obey capture policy; `.labradour` is excluded by default.

Persist tree/name/initial-focus preferences only. Current focus, compact/maximize state, terminal dimensions, action selection, filters, follow mode, visualization tabs/scroll/job state and provider credentials are not layout-file fields. A loaded layout cannot replace those live states.

### 2.4 Layout editing

`Ctrl-]` then `:` opens the IDE command menu. It accepts only registered IDE commands, never a shell command. The editor offers a tree preview labelled with split IDs, pane names, axes and requested/effective ratios. All operations are keyboard accessible. Target split IDs belong to the active tree; invalid targets leave it unchanged with a reason.

| Menu command | Effect |
| --- | --- |
| `layout use NAME` | Switch to a named built-in/custom tree |
| `layout swap PANE PANE` | Exchange two leaf identities; ratios remain attached to split nodes |
| `layout flip SPLIT_ID` | Exchange children and replace ratio with 10000 minus ratio, preserving their requested sizes |
| `layout axis SPLIT_ID rows|columns` | Change the split axis, retaining its requested ratio |
| `layout ratio SPLIT_ID BPS` | Set the requested first-child share within 1000–9000 |
| `layout reset NAME` | Load the original named built-in into session state; do not write a file |
| `layout save NAME user|workspace` | Explicitly persist the current valid tree |
| `layout reload` | Reload validated disk layers; ask before discarding unsaved edits |
| `layout status` | List available names; tree/ratios/compact/save state remain visible; Ctrl-] `i` exposes full source diagnostics outside the menu |
| `layout preview` / `layout apply` / `layout cancel` | Begin preview, commit candidate, or discard candidate |

Swaps, flips and axis changes can produce all binary arrangements of the three leaves. Repeated axis changes allow a row/column of three panes. New named arrangements are saved from the session tree. There is no arbitrary JSON or shell evaluation inside the menu.

Each change validates a candidate tree and computes geometry before the next draw/input transaction. An editor preview does not resize the PTY until Apply; Escape cancels the preview. Menu commands apply immediately after validation. They preserve focused pane identity. Switching to a layout that does not fit enters compact mode without altering the saved ratios.

Keep current shortcuts with explicit arbitrary-tree semantics. `m` horizontally reflects the whole tree by flipping every `columns` node and complementing its ratio; `rows` nodes stay unchanged. `+`/`-` increase/decrease Agent's requested share by 500 basis points at its nearest `columns` ancestor. `]`/`[` increase/decrease Activity's requested share at its nearest `rows` ancestor. If the pane is in `second`, adjust the stored first ratio in the opposite direction. At an absent matching ancestor, report “no width/height split; use layout menu” without changing geometry. Clamp shortcuts to the valid range. At constrained sizes show requested and effective ratios; physical resize never rewrites the requested ratio. `z` toggles maximize for the focused pane independently of compact mode.

### 2.5 Geometry, compact mode and PTY size

Rectangles use integer terminal cells `(y, x, height, width)`. Reserve header row 0 and footer row rows−1. Leaf minimum outer sizes (including two border rows/columns) are Agent 40 × 12, Activity 30 × 8, and Visualization 30 × 8. Their minimum content sizes are 38 × 10, 28 × 6 and 28 × 6. These are usability floors, not certification that every native screen works at that size.

For `columns`, subtree minimum width is the sum of child widths and minimum height their maximum; for `rows`, height is the sum and width the maximum. An all-pane layout requires both the current 100 × 28 terminal floor and its tree's minimum inside the header/footer. Otherwise compact mode shows only the focused pane, with a text indicator and reason. Do not drop just one leaf or silently substitute a different preset. When the terminal grows, restore the requested tree with the same focus and review state. Maximize remains explicit until toggled off even after leaving compact mode.

Allocate each split recursively: for extent E and ratio R, requested first extent is `ceil(E * R / 10000)` using integer arithmetic. Clamp it between the first subtree's minimum and E minus the second subtree's minimum. The second child gets the remainder. Do not allocate negative/zero content, gaps or overlaps. Border cells belong to their leaf; adjacent panes have separate borders. Positions are relative to the shared content area. If the tree cannot meet minima, use compact mode before allocation rather than relaxing minima piecemeal.

In focused-only modes the visible pane fills the content area regardless of its ordinary minimum. If fewer than three rows or four columns exist, draw only a best-effort “terminal too small” notice; keep quit/prefix controls active and retain the last positive PTY size. Content sizes smaller than normal minima remain at least 1 × 1 wherever a bordered pane can be drawn.

When Agent is visible, resize its existing PTY and terminal model to its content rectangle before drawing the next frame. Send a resize only when content dimensions actually change; the normal PTY resize delivers SIGWINCH. Coalesce physical resize bursts for at most 50 ms without blocking input or capture. When Agent is hidden because review is maximized/compact, retain its last visible positive dimensions and continue consuming output; do not resize it to the review rectangle or 0 × 0. If initially hidden, initialize at the Agent's ordinary tree geometry when it fits, otherwise the focused-only content size. Refocusing Agent applies its visible content size before forwarding newly focused input. No layout operation respawns/replays the child or alters its arguments.

Terminal history and native alternate-screen state remain owned by the existing terminal model. Reflow/cursor clamping follows the terminal backend's capabilities; do not promise preservation of identical native line wrapping across sizes. Preserve buffers, modes and queued bytes, and let the provider redraw through its normal resize behavior. If a resize fails, retain the previous valid PTY/model dimensions, clip existing content to the new viewport, show a recoverable notice and retry on later frames.

### 2.6 Live state and rendering ownership

Keep pane controllers keyed by `agent`, `activity`, and `visualization` independent of curses windows. A layout transaction may replace windows but must retain child PID/process group, PTY descriptor, terminal buffers/input modes, collector credentials, recorder session and queues. Preserve Activity's stable selected action/effect ID, filters, expansions, paused follow/unread count, and Visualization's selection/revision, view ID, tab, scroll and bounded interaction state. Clamp scroll only when required by content bounds.

Resize rerenders prepared visualization analysis for the new viewport; it does not restart evidence analysis or reread the workspace. Each viewport change increments render generation, and stale results cannot paint new windows. Hidden review panes retain their selection/state; expensive hidden rendering can pause, while collection and evidence revisions continue. The renderer receives the latest revision on visibility restoration. Selection/revision changes still cancel obsolete preparation as required by [VizApi.md](docs/VizApi.md).

Layout edits and file errors are presentation-local. They cannot suspend recording, answer approvals, synthesize agent input, start plugins or open graphical companions. Escape closes the command overlay and restores the previous focus; quit remains available throughout preview, compact mode and save errors.

## 3. Focus and input

Start with Agent focused unless `initial_focus` is configured. Show a visible focus label and border treatment, using text as well as color. Prefix-Tab traverses leaves in depth-first `first` then `second` order, wrapping; prefix-Shift-Tab reverses that order. This is left-to-right/top-to-bottom tree order, not the fixed Agent/Activity/Visualization order of the current harness. In `agent-top`, the order is Agent → Activity → Visualization; in `visualization-top`, Agent → Visualization → Activity. Direct focus keys remain independent of position. Layout changes preserve focused pane identity and determine the next traversal from the new tree. In compact/maximized mode traversal includes hidden leaves and makes the newly focused pane visible; it does not exit maximize. The command overlay consumes its own navigation keys and returns focus to its owning pane on close.

| Input | Behavior |
| --- | --- |
| Ordinary keys, paste, Tab, arrows, Ctrl-C in Agent | Pass to child PTY |
| `Ctrl-]` then `a`, `l`, or `v` | Focus Agent, Activity log, or Visualization |
| `Ctrl-]` then Tab | Cycle through the three panes |
| `Ctrl-]` then Shift-Tab | Cycle in reverse tree order |
| `Ctrl-]` then `m` | Mirror agent side |
| `Ctrl-]` then `z` | Maximize/restore focused pane |
| `Ctrl-]` then `:` | Open IDE command menu |
| `Ctrl-]` then `Ctrl-]` | Send a literal `Ctrl-]` to the Agent pane |
| Escape in a review overlay | Close overlay; preserve selection |

Reserve `Ctrl-]` as the v1 prefix and `Ctrl-Q` as global quit, consistent with the current router; prefix customization is deferred. Display prefix-mode hints, cancel the pending prefix on Escape, and expire it after two seconds with no input. Do not reinterpret bracketed pasted text as IDE commands. Ctrl-C in review panes cancels a visualization job or filter operation; it does not interrupt the agent. Stopping the agent/session is a separate explicit command. Closing a menu must not replay its typed text into the Agent.

Native approval prompts stay inside Agent. Surface an “agent awaiting input” indicator only when a provider event establishes it; clicking/focusing the indicator returns to Agent without answering the prompt.

## 4. Activity pane: actors, tools, and actions

The log is an ordered, filterable projection of the event journal. Show session/turn headers and one row per tool invocation; update that row as its lifecycle advances instead of adding a new primary row for each lifecycle event. Preserve the underlying events for detail inspection.

Compact columns: time, actor, tool/action, short target or command, state, and file-effect count. States include requested, awaiting approval, running, completed, failed, denied, interrupted, and incomplete. Hide lower-priority columns as width decreases. Escape control characters and truncate long summaries; complete values remain inspectable.

Actors include the main agent, identified subagents, and external/unknown writers. Actor grouping does not imply that MVP launches multiple agents. Where actor identity is missing, show unknown rather than infer it from arrival order.

Selecting a tool expands child effects such as file creation, modification, deletion, or rename. These child rows select operation-specific visualizers. A shell call that creates three files remains one call with three linked effects; it must not become three apparent tool invocations. Standalone filesystem observations appear under external/unknown unless linked by recorded evidence. Show correlation quality for ambiguous links.

Review controls:

- Up/Down or `j`/`k`: move selection and immediately update Visualization.
- Left/Right: collapse/expand an action's effects; Enter: expand or open detail.
- `/`: filter by actor, tool, state, path, turn, or text; clearing filters restores the prior selection when possible.
- End or `f`: resume live follow; scrolling/selecting an earlier row pauses follow.
- `d`: inspect arguments, lifecycle, result, linked checkpoints, and coverage in an overlay.

Selection is keyed by stable action/effect ID, never by row number. New activity does not replace a historical selection. Show an unread count while follow is paused. In follow mode, select the newest actionable row; as a running call completes its visualizer updates in place. A selection that is filtered out leaves a visible “selection outside filter” state until the user selects another row.

## 5. Visualization pane: selection and evidence

Selecting an action supplies its actor, normalized operation, tool arguments/result, lifecycle state, affected paths, immutable snapshot references, output references, and attribution quality to the visualization resolver.

The pane header identifies the selected action, visualizer, path/effect count, and evidence quality. Use tabs within the pane for Summary, operation-specific view, Output, Files, and Evidence when relevant. Prefer the operation-specific view on first selection. Remember the user's choice for that action.

For an action with multiple effects, show a compact summary and an in-pane file/effect picker. Selecting a child row in Activity goes directly to that effect. Batch-correlated changes are labeled as an interval shared with other calls, not exclusive effects of one call. Proposed edits are displayed as proposals until confirmed; canceled proposals are not shown as observed creations.

Resolve history from captured snapshots, not current workspace files. The default file comparison is the best available before/after interval for the selected effect. Allow adjacent checkpoint, turn, session, and arbitrary A/B comparisons via an overlay. Always label the selected interval and whether it is exact, correlated, or incomplete. A file later deleted still opens from its historical blob.

### Default visualization routing

| Selected operation | Default embedded visualization | Optional companion |
| --- | --- | --- |
| Source file created | Creation card: path/type/size, added contents, file structure when supported | gitdiffviz for a captured revision pair |
| Source file modified | Unified diff with line counts; optional side-by-side mode | gitdiffviz source/hierarchy view |
| File deleted | Deletion card: removed path/type/size, last captured contents, removal interval | Optional diff export; deletion card remains the default |
| File renamed/moved | Old → new path map, rename evidence, linked content changes | Revision-diff export when supported |
| Shell command/script run | Command card: command, cwd, actor, duration, exit/signal, scrollable output, linked effects | Optional specialized output visualizer |
| File read/search | Paths/query and captured returned snippets/results where available | Language-aware reader or result navigator |
| Binary/oversize file changed | Before/after metadata and available hashes, size delta, capture/exclusion reason | Type-specific preview plugin |
| MCP or unknown tool | Tool card with structured arguments, result/error, and related evidence | Tool-specific plugin |
| Agent/subagent lifecycle | Actor relationship and lifecycle summary | Richer actor graph later |
| Recording gap/health event | Missing evidence, scope, interval, and recovery status | None required |

Deletion is a distinct presentation even though Git represents it as a diff against an absent path. A shell visualizer displays captured command activity; it never reruns the script to produce its visualization. A read/search action remains inspectable without a filesystem diff.

### Terminal and graphical renderers

Every MVP operation has an embedded curses-compatible view in the quarter-screen Visualization pane. Treat gitdiffviz as an optional backend/companion: its browser/Tauri interface is not directly a curses widget. Its analysis output could feed a future terminal renderer after a schema compatibility spike.

When a graphical companion is available, show an explicit “Open graphical view” action inside the pane. Open a local view on user activation while retaining the embedded summary and current selection. Do not launch a browser for every selection or promise graphical embedding on ordinary terminals. The core selection → visualization flow must work when gitdiffviz is absent.

## 6. Pluggable visualizer contract

The detailed proposed API, plugin manifests, evidence access, terminal rendering, subprocess protocol, and export lifecycle are defined in [docs/VizApi.md](docs/VizApi.md). This section describes how that contract fits the review UI; the plugin runtime is not implemented yet.

Separate provider adapters (event capture) from visualizer plugins (presentation). Proposed logical interface:

```text
VisualizerDescriptor:
  id, version, supported_operation_kinds, required_evidence,
  priority, capabilities {terminal, graphical_export}

Visualizer:
  supports(context) -> applicability and reason
  prepare(context, cancellation) -> immutable view model
  render_terminal(view_model, viewport, ui_state) -> cells/widgets
  export_graphical(context, destination, cancellation) -> local artifact [optional]
```

Context includes schema version, selection ID, session/actor/call IDs, operation, lifecycle state, evidence revision, snapshot/tree/blob OIDs, output references, file metadata, and correlation labels. Expose evidence through a read-only service with capture-policy checks. Missing evidence is represented explicitly rather than supplied as an empty diff or empty output.

Resolution order: explicit user choice for the action; configured operation/tool mapping; applicable registered plugins by deterministic priority; built-in generic card. File effects override the parent call's tool category when a child effect is selected. If a configured plugin is unavailable or lacks evidence, show the reason and use the built-in fallback. Users can select another applicable visualizer from the pane menu.

Preparation/export runs off the curses UI thread. Cancel obsolete work on selection changes and discard results whose selection ID/evidence revision no longer matches. Cache by immutable evidence references, plugin version, and options. Resize rerenders the view model rather than regenerating the analysis. Escape untrusted output and keep terminal control sequences out of review cells.

For MVP, use an internal registry of built-ins plus a configured, explicitly installed gitdiffviz subprocess adapter. Defer arbitrary third-party plugin discovery and dynamic loading. A future out-of-process plugin protocol can implement this contract without changing the UI; do not claim Python plugins are sandboxed. Plugin errors/timeouts become pane-local fallback states and never block terminal input or recording.

## 7. Loading, missing data, and accessibility

Represent loading, running, no file effects, missing baseline, excluded content, truncated output, ambiguous attribution, plugin unavailable, and plugin failure as distinct states. A tool can be completed while its snapshot is still pending: show recorded results immediately and “capturing file changes” until the evidence arrives.

Keep a historical selection stable while updating its evidence revision. After interruption, preserve partial outputs and captured changes, and mark the action interrupted/incomplete. Highlight recording gaps in the shared footer and the relevant action's Evidence tab.

All operations must be keyboard accessible. Use visible text labels for focus/status, support monochrome terminals, respect Unicode cell widths, provide horizontal/vertical scrolling, and expose full paths/commands without requiring hover. Neither filters nor visualizer jobs pause collection.

## 8. MVP acceptance scenarios

1. Codex or Claude occupies half the usable window, accepts input/paste, and remains live while the other half displays Activity above Visualization.
2. Mirroring preserves agent process, PTY contents, log selection, visualization state, and persisted preferences. Ratios and maximize/compact mode resize the child PTY correctly.
3. Selecting a creation, deletion, and shell action produces three distinct embedded views. Shell output and file effects remain navigable from the same invocation.
4. A historical selection stays selected as new actions arrive; returning to follow selects live activity. Filters preserve stable IDs.
5. A multi-file call and a shared parallel-call interval expose their effects and attribution limits without duplicating tool calls or implying exclusive ownership.
6. Pending snapshots, unavailable plugins, failures, exclusions, and empty net diffs show meaningful states. A reverted edit is reviewable from captured intermediate history.
7. Rapid selection changes cannot display an obsolete visualizer result. Slow export cannot delay agent typing or event capture.
8. With gitdiffviz unavailable, every action is still inspectable in the quarter-screen pane. With it installed, graphical opening is explicit and uses the selected historical evidence.

The Phase 3 layout acceptance matrix in [docs/Phase3.md](docs/Phase3.md) adds reordered/nested layouts, validation, persistence, constrained geometry and state preservation. Phase 3 defines those scenarios; passing the runtime gates requires Phase 4 implementation in [AgentIDEPlan.md](AgentIDEPlan.md). The optional graphical exporter extends the same visualizer registry in Phase 5.


## Current layout editor acceptance

The layout menu is an overlay over existing pane controllers. It consumes its own bounded ASCII command buffer and ignores pasted control bytes. Preview commands edit a separate immutable tree; Apply changes live geometry before subsequent native input. Escape cancels preview and closes the overlay without leaking keys. Disk save/reload jobs run on a single background job per editor; layout edits are temporarily blocked during the job while native input and collection continue. Reload requires `--discard` for unsaved changes and preserves live focus. It rereads disk layers with the original explicit-file/ignore settings, without reapplying startup name/legacy-ratio overrides.

Automated acceptance covers schema/editor semantics, same-session state, disk failures/conflicts, blocked disk work with continued input, and rendered recorder/PTY preview/save/reload/compact behavior. Human native colors, alternate-screen/history/paste and approval usability still need the [Phase 4 checklist](docs/Phase4.md#layout-editor-and-interaction-acceptance-slice). Grouped action selection and visualization generations will be tested when those later controllers exist.
