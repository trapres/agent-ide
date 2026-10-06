# Phase 3: arbitrary layout specification

Date: 2026-10-06. Status: specification complete; implementation and runtime acceptance belong to Phase 4.

The authoritative contract is [AgentUI.md, section 2](../AgentUI.md#2-layout-configuration-and-resizing), with input rules in section 3. Phase 3 expands the same three primary panes into validated binary split trees. It does not ship new CLI options, persistence, an editor, source visualizers or a plugin runtime. The current harness keeps its side/ratio shortcuts and focused-pane fallback.

## Completed specification slices

1. **Tree schema and presets:** versioned, bounded JSON; exactly one Agent, Activity and Visualization; explicit rows/columns axes; integer basis-point ratios; named presets and complete/nested configuration examples.
2. **Editing, persistence and input:** keyboard menu commands; user/workspace/explicit-file precedence; whole-tree replacement; explicit atomic saves; legacy launch behavior; invalid-file fallback and version handling; traversal by tree order with focus preserved by identity.
3. **Geometry and acceptance:** recursive subtree minimums; deterministic integer allocation; compact/maximize behavior; visible/hidden PTY sizing; controller and selection preservation; stale render rejection; implementation gates below.

## Specification review and worked checks

These are contract examples checked during Phase 3, not measurements of a new runtime.

| Case | Required interpretation |
| --- | --- |
| `default`, terminal 140 × 40 | Shared area 140 × 38; Agent outer 70 × 38, content 68 × 36; each review outer 70 × 19 |
| `agent-right`, terminal 141 × 40 | Review subtree first gets 71 columns; Agent second gets 70; Agent content 68 × 36 |
| `agent-top`, terminal 140 × 40 | Agent outer 140 × 19, content 138 × 17; review panes each 70 × 19 |
| `visualization-top` | Same default geometry with review identities exchanged; traversal Agent → Visualization → Activity |
| Default ratio 9000 at 100 × 28 | Requested Agent width 90, constrained to 70 to leave review minimum 30; saved ratio stays 9000 |
| Three side-by-side panes at 100 × 28 | Combined minimum width 100; clamp root/nested splits to fit each leaf rather than reducing a leaf below its minimum |
| Three stacked panes at 100 × 28 | Combined minimum height 12 + 8 + 8 = 28 exceeds shared height 26; compact despite meeting global terminal floor |
| Three stacked panes at 100 × 30 | Shared height 28 fits minimums; each subtree allocation is constrained to the required leaf heights |
| Any preset at 99 × 40 or 140 × 27 | Compact by global 100 × 28 floor; same requested tree restored when space returns |
| Default at 2 × 3 | Too-small notice; no invalid rectangle/PTY size; controls and collection remain active |

Examples of invalid input include a repeated `agent` leaf, missing `visualization`, `axis: "horizontal"`, `ratio_bps: true`, a fourth pane, a duplicate split ID, an unknown property or `schema_version: 2`. Automatically discovered invalid files warn and are ignored as whole layers. An invalid explicit file fails before launching the child. Neither case rewrites the source file.

## Phase 4 implementation acceptance matrix

Automate pure schema/geometry/configuration cases first; use the deterministic PTY demo for live-state integration, then repeat input/render checks with real providers. Fixtures must test observable behavior and preserve evidence uncertainty.

| Gate | Exercise | Expected result |
| --- | --- | --- |
| Tree coverage | All four presets, both nested axes, all pane orders, three in one row/column | Each pane occurs once; non-overlapping geometry; tree-order traversal |
| Validation bounds | Invalid types/keys/versions, duplicates, oversized file, excess layouts/depth, symlink/non-regular file | Bounded rejection; source-specific warning or explicit-file startup error; no PTY launch on explicit error |
| Integer geometry | Odd dimensions, 1000/9000 ratios and constrained subtree minimums | Complete content-area coverage, deterministic first-child remainder, valid content sizes, unchanged requested ratios |
| Layer precedence | Same name at user/workspace/explicit layers; omitted preferences; invalid intermediate layer; unresolved active name | Whole-tree overrides with inheritance; no partial node merge; documented discovery fallback and explicit error |
| Legacy compatibility | Each legacy flag alone, all together, no flags, legacy flags plus explicit layout | Legacy defaults/clamping only when supplied; discovered tree otherwise; ambiguous combination rejected |
| Save/reload | Save to each scope, restart, modify file externally, unwritable path, invalid existing destination | Correct active tree restored; other names retained; atomic save; conflict/failure does not discard live edits; explicit replacement confirmation |
| Editor operations | Swap/flip/axis/ratio/use/reset; invalid IDs; canceled preview | Valid changes apply once; preview cancellation causes no resize; invalid operations leave state unchanged |
| Focus/input | Direct keys, forward/reverse traversal, menu Escape, prefix timeout, ordinary Tab/Ctrl-C, bracketed paste containing control bytes | Correct identity/traversal; native keys reach Agent only with Agent focus; menu text never leaks into PTY; pasted controls do not execute IDE commands |
| Mirror/shortcut semantics | Mirror twice, adjust Agent width/Activity height in reordered trees, missing matching ancestor | Original tree after two mirrors; correct first/second ratio direction; absent ancestor gives a notice |
| Compact/maximize | Shrink/grow through minima; maximize review; switch focus while maximized; terminal below drawable size | Focused-only indicator, requested tree retained, hidden Agent size retained, positive dimensions, quit always usable |
| PTY lifecycle | Demo `size` before/after layout change and focus restoration; rapid outer resize | Same child PID/PTY; dimensions match visible Agent content; unchanged size avoids extra resize; input and collection continue |
| Native terminal fidelity | Both providers: approvals, paste, alternate screen, history, resize, hidden output and return | Input/output remain usable; no lost queued bytes or stale review text; native redraw permitted; no approval answered by layout action |
| Review state | Historical selected action/effect, filters, expanded children, paused follow, tab/scroll; switch presets and compact/maximize | Stable selection and pane controller state; updates continue while hidden; only necessary scroll clamping |
| Render concurrency | Resize/selection/revision changes while a prepared view renders slowly | Generation checks discard obsolete results; resize uses prepared analysis; no live-file rereads |
| Recorder independence | Layout edits/save error during callback and checkpoint activity | Same recorder/collector session; no gaps caused by UI blocking; workspace save may be recorded as an ordinary user change |

Human native checks must state terminal/provider/platform versions and observed failures. Phase 3 completion means these expectations are defined and internally consistent; it does not mean the matrix has passed in the current application.

## Phase 4 starting point

Implement the pure split-tree validator and geometry engine first, preserving the current `layout()` caller compatibility until the harness adopts the new tree. Add configuration loading and explicit saves next, then stable pane controllers/editor/resize integration. Build the action/effect Activity projection and historical visualizer service on those controllers. External plugin transport and graphical opening stay in their separately scoped later work.

## Phase 2 handoff

On October 6 the user reported that the Linux Claude sign-in/check workflow looked good and authorized Phase 3. This is user-reported acceptance of the exercised workflow; no report artifact, exact checklist coverage or deployment-policy details were supplied. It is sufficient to proceed with this specification milestone. The existing measured evidence remains in [native-signoff-acceptance.json](native-signoff-acceptance.json); unreported managed-policy, human-checklist and deployment-specific rows remain tracked in [Phase2TestGuide.md](Phase2TestGuide.md). Do not convert this handoff into a claim that every broad native case was measured or passed.
