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

**Next slice:** action/effect Activity projection.
