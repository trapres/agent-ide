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

Use the same `--layout` option when launching a native provider, with the usual workspace, recording and hook options. Explicit legacy `--side`, `--agent-width`, or `--activity-height` cannot be combined with `--layout`. Without `--layout`, the harness retains its original geometry and shortcuts.

With an explicit preset, prefix-Tab cycles by tree order and prefix-Shift-Tab cycles in reverse. Mirror reflects column splits throughout the tree. Width/height shortcuts change the target pane's nearest matching ancestor in 500-basis-point steps, bounded to 1000–9000. When no matching ancestor exists, a notice explains that no change occurred. Focus, event selection, paused follow and JSON scroll remain live through these operations.

The existing PTY/terminal remain in place; hidden Agent retains its last size and continues consuming output. Refocusing/resizing applies visible Agent content dimensions before subsequent input, including a focus command and typed text arriving in the same read batch. Duplicate PTY dimensions do not cause another ioctl. Review panes still display journal/JSON cards.

## Try it

At 140 × 40, `agent-top` places Agent above two side-by-side review panes. Type `size` in the demo: expect `columns=138, lines=17`. Focus Visualization with Ctrl-] then `v`, maximize with Ctrl-] then `z`, then focus Agent with Ctrl-] then `a`. Type `size` again: expect `columns=138, lines=36`. Toggle maximize off to return to the first dimensions. The demo remains the same running process.

Try `visualization-top`: Visualization should be above Activity. Prefix-Tab visits Agent → Visualization → Activity; reverse traversal reverses that order. Shrink below 100 × 28 and confirm only the focused pane appears with a compact reason, then grow to restore the preset. Quit with Ctrl-Q.

## Validation and remaining work

The suite has 116 tests, including ten new layout tests. Full macOS/Python 3.9 and Linux/Python 3.11 runs pass. Coverage includes all pane permutations, both nesting shapes and split axes, extreme/constrained ratios, complete area coverage without overlap, invalid/cyclic decoded trees, reverse traversal, hidden PTY sizing, preserved state, rendered Agent-above-review geometry and same-batch input after focus changes. Existing recorder, adapter, cleanup and legacy geometry regressions also pass.

Next slices implement configuration files and atomic persistence, the layout command editor, action/effect Activity projection, historical built-in visualizers and saved-session review/acceptance. `--layout-config`, custom layout names, file-layer validation, initial-focus preferences, menu editing/saves, historical source views and plugin transport are not implemented by this foundation. Native human rendering/input checks remain necessary after the broader UI integration.
