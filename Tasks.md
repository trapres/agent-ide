# Labradour tasks

## Phase 2: native adapters

Phase 1's generic recorder is accepted in its measured automated scope (57 tests on macOS and Linux). Human terminal fidelity and real provider hook coverage remain open and will be checked alongside Phase 2. See [the implementation plan](AgentIDEPlan.md) and [manual test guide](docs/Phase1TestGuide.md).

- [x] **Adapter foundation**: versioned normalized event contract; explicit provider coverage matrix; launch-scoped authenticated collector with bounded fallback spool, validation/deduplication, health reporting, and deterministic fixtures. Integrated with the existing single-writer recorder without attributing filesystem changes to tools. All 70 tests pass on macOS and Linux; details and current limits are in [Phase2.md](docs/Phase2.md).
- [x] **Claude and Codex integration implementation**: native mappings and lifecycle hooks; bounded synchronous prompt/tool receipts; private launch configuration and CLI hook composition; authenticated setup and delivery health. Documentation-derived fixtures pass. Native startup delivered no callbacks; real composition/trust and terminal responsiveness remain acceptance gates, documented in [Phase2.md](docs/Phase2.md).
- [x] **Correlation implementation and fixture acceptance**: deterministic, read-only `actions` replay; failures, denials, interruption, missing/conflicting outcomes, scoped actors/calls, overlapping checkpoint windows, output references, and explicit attribution limits. All 92 automated tests pass on macOS/Linux. Shutdown is bounded and reports unreaped children as a durable cleanup gap. See [Phase2.md](docs/Phase2.md).
- [x] **Native acceptance tooling and measured macOS runs**: real Claude 2.1.234/Codex 0.160.0 delivery, normal trust review, read/edit/revert/shell, subagent callbacks, project-hook composition without settings rewrites, Codex approval rejection/Interrupt, and graceful completed recordings without cleanup gaps. Added captured native fixtures and private fixture/report helper; all 97 tests pass on macOS/Linux. Evidence: [provider-native-acceptance.json](docs/provider-native-acceptance.json).
- [x] **Broad native gate implementation and additional measurements**: native Claude policy denial; two overlapping foreground Bash writers with distinct actors and an external edit in both providers; unanswered trust-dialog cleanup fix; Linux Codex delivery, alpha → beta checkpoints, approvals and exit 7; project/user/launch hook composition; independent tmux text-cell replay for both providers; sparse-row deletion fix. All 103 tests pass on macOS/Linux. Evidence: [broad-native-acceptance.json](docs/broad-native-acceptance.json).
- [ ] **Broad native acceptance sign-off**: additional real Claude plugin and Linux Codex plugin/additive-managed/managed-only-policy checks are measured, with 106 automated tests passing on macOS/Linux. Remaining manual work: Linux Claude sign-in and tool/policy cases, human terminal visual/input review, and any deployment-specific MDM/cloud policies or other plugins. Use the [manual sign-off runbook and pass/fail table](docs/Phase2TestGuide.md#9-manual-sign-off-runbook); evidence: [native-signoff-acceptance.json](docs/native-signoff-acceptance.json). Do not check this task off until applicable rows pass or their scoped limitations are explicitly accepted.

**October 6 handoff:** the user reported that the Linux Claude sign-in/check workflow looked good and authorized Phase 3. The exercised workflow is user-accepted; reports and exact managed-policy/visual/deployment checklist coverage have not been supplied. Keep the broader evidence task open for those unreported rows without blocking Phase 3 specification work. See [Phase3.md](docs/Phase3.md#phase-2-handoff).

## Phase 3: arbitrary layout specification

- [x] **Split-tree schema and presets**: bounded v1 JSON, exactly three unique pane identities, nested rows/columns, named defaults, reordered and Agent-above-review examples. Defined in [AgentUI.md](AgentUI.md#2-layout-configuration-and-resizing).
- [x] **Editing, persistence and focus contract**: keyboard menu, tree-order traversal, explicit atomic user/workspace saves, configuration precedence, legacy flag compatibility, validation/version fallback and preserved live state.
- [x] **Geometry and implementation acceptance**: recursive minimums, deterministic cell allocation, compact/maximize rules, hidden/visible PTY behavior, prepared-view render generations and Phase 4 acceptance matrix. Worked checks and runtime gates are in [Phase3.md](docs/Phase3.md).

Phase 3 is a completed specification milestone. Its new configuration/commands are not runtime features yet; Phase 4 implements and verifies them.

## Later phases

- [ ] Phase 4: review MVP, action/effect projection, saved-session review, built-in visualizers.
- [ ] Phase 5: optional graphical exporter and explicit companion opening.
- [ ] Phase 6: extended provider backends and multi-session work.

The proposed visualization integration contract is in [VizApi.md](docs/VizApi.md). Writing that contract did not implement a plugin runtime.
