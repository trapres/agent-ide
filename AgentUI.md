# AgentIDE MVP UI specification

Date: 2026-10-02. Status: proposed. Companion architecture: [AgentIDEPlan.md](AgentIDEPlan.md).

## 1. Screen structure

The MVP has exactly three primary panes:

- **Agent:** the interactive native Codex, Claude, or other CLI, occupying half the usable screen width and its full height.
- **Activity:** a tool/actor/action log occupying the upper half of the other side.
- **Visualization:** the selected action's visual explanation occupying the lower half of the other side.

The default is agent on the left. Users can move it to the right; Activity remains above Visualization. Ratios apply to the area inside the shared header/footer, excluding borders. Default width ratio is 50:50 and the review side's height ratio is 50:50, producing one half-screen pane and two quarter-screen panes.

```text
Agent on left (default)
+-------------------------- AgentIDE: workspace / session ------------------+
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
+-------------------------- AgentIDE: workspace / session ------------------+
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

Persist UI preferences separately from the recording facts, with workspace overrides over user defaults. Proposed configuration:

```toml
[ui]
agent_side = "left"             # left | right
agent_width_fraction = 0.50
activity_height_fraction = 0.50 # fraction of the review side
initial_focus = "agent"
follow_activity = true

[ui.visualizers]
"file.create" = "file-create"
"file.modify" = "source-diff"
"file.delete" = "file-delete"
"shell.execute" = "command"
```

Expose mirror and ratio changes through the IDE command menu; keyboard adjustments are sufficient for MVP. Clamp ratios to keep panes usable. Save changed ratios, calculate integer dimensions deterministically, and give leftover columns/rows to the Agent/Activity panes. Resize the child PTY to the Agent pane's content dimensions after every layout change.

Proposed minimum for all three panes: 100 columns × 28 rows, subject to terminal-fidelity testing. Below this, show one full-screen pane at a time with the same focus/navigation commands and a visible compact-layout indicator. Restore the saved three-pane layout when space returns. Allow explicit maximize/unmaximize at any size without restarting the agent or losing selection.

## 3. Focus and input

Start with Agent focused. Show a visible focus label and border treatment, using text as well as color.

| Input | Behavior |
| --- | --- |
| Ordinary keys, paste, Tab, arrows, Ctrl-C in Agent | Pass to child PTY |
| `Ctrl-]` then `a`, `l`, or `v` | Focus Agent, Activity log, or Visualization |
| `Ctrl-]` then Tab | Cycle through the three panes |
| `Ctrl-]` then `m` | Mirror agent side |
| `Ctrl-]` then `z` | Maximize/restore focused pane |
| `Ctrl-]` then `:` | Open IDE command menu |
| `Ctrl-]` then `Ctrl-]` | Send a literal `Ctrl-]` to the Agent pane |
| Escape in a review overlay | Close overlay; preserve selection |

The prefix is configurable. Display prefix-mode hints, cancel the pending prefix on Escape, and expire it after a short timeout with no input. Do not reinterpret pasted text as IDE commands. Ctrl-C in review panes cancels a visualization job or filter operation; it does not interrupt the agent. Stopping the agent/session is a separate explicit command.

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

Implement this UI in the review MVP phase of [AgentIDEPlan.md](AgentIDEPlan.md); validate layout and focus in the initial PTY spike. The optional graphical exporter extends the same visualizer registry in the following phase.
