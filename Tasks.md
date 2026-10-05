# Labradour tasks

## Phase 2: native adapters

Phase 1's generic recorder is accepted in its measured automated scope (57 tests on macOS and Linux). Human terminal fidelity and real provider hook coverage remain open and will be checked alongside Phase 2. See [the implementation plan](AgentIDEPlan.md) and [manual test guide](docs/Phase1TestGuide.md).

- [x] **Adapter foundation**: versioned normalized event contract; explicit provider coverage matrix; launch-scoped authenticated collector with bounded fallback spool, validation/deduplication, health reporting, and deterministic fixtures. Integrated with the existing single-writer recorder without attributing filesystem changes to tools. All 70 tests pass on macOS and Linux; details and current limits are in [Phase2.md](docs/Phase2.md).
- [ ] **Claude and Codex integration**: provider-specific normalization and lifecycle hooks; prompt/tool boundaries; launch-scoped configuration and existing-hook composition/trust; collector setup and provider health. Measure actual delivered events and document unsupported categories.
- [ ] **Correlation and acceptance**: failures, denials, interruption, overlapping calls, subagents, and explicit attribution limits; native manual acceptance and macOS/Linux fixtures. Avoid exclusive attribution where evidence is ambiguous.

## Later phases

- [ ] Phase 3: arbitrary layout specification, persistence, focus/resize rules.
- [ ] Phase 4: review MVP, action/effect projection, saved-session review, built-in visualizers.
- [ ] Phase 5: optional graphical exporter and explicit companion opening.
- [ ] Phase 6: extended provider backends and multi-session work.

The proposed visualization integration contract is in [VizApi.md](docs/VizApi.md). Writing that contract did not implement a plugin runtime.
