# Phase 1 recorder: initial implementation

Date: 2026-10-02. Status: **first recorder slice implemented; broader Phase 1 hardening remains open**.

The user verified that Codex and Claude can run through Labradour's workspace launcher. This supports starting Phase 1; the remaining Phase 0 terminal, provider-hook, and Linux checks continue separately.

## Available now

`run --record DIRECTORY` enables a single-writer recorder in a background thread. Ordinary `run` and observation-only `--watch` remain available. The recorder uses SQLite WAL with FULL synchronous journaling and a private bare Git repository. An exclusive writer lock prevents simultaneous recordings into one store; workspace identity prevents reuse for a different root.

The launcher forks a paused PTY child before starting watcher threads. It starts observation, captures a baseline and startup reconciliation, then releases the child to execute. Periodic reconciliation runs every two seconds; watcher batches trigger capture on approximately 100 ms worker ticks, subject to scan duration. Shutdown stops the child process group before the final scan.

The journal stores session lifecycle, filesystem observations, raw provider hook payloads when hooks are enabled, snapshot intent/completion/failure, collector overflow, and recovery records. Existing spool files are ignored at recorder construction so they are not assigned to a new launch. Event IDs deduplicate newly received hook records. Provider event normalization and attribution remain Phase 2 work; recording hooks does not establish their coverage.

Each checkpoint uses immutable captured bytes, file/symlink modes, a private index, and a per-session ref. Unchanged manifests reuse the previous commit. A new session starts a new baseline root. Captured creates, edits, deletions, binary contents, executable bits, and reverted edits can be inspected through `history` and `diff`. The live Visualization pane exposes checkpoint/event details; the full historical review UI remains Phase 4.

Recovery marks sessions left running as interrupted, identifies unfinished snapshot intents, records the retained session ref as recoverable evidence, and preserves that history. It starts a separate session from current workspace contents. It does not assert completion of the interrupted snapshot or recover objects that never reached a ref. This implements process-crash recovery, not a demonstrated guarantee against power loss.

## Capture policy and limits

Exclusions are applied before reading content, independently of project `.gitignore`:

- Any path component named `.git`, `.venv`, `__pycache__`, `.agentide-spike`, `.labradour`, `node_modules`, `vendor`, `build`, `dist`, `.aws`, `.ssh`, `.codex`, or `.agents`.
- `.env` and names beginning `.env.`.
- The recording directory and active hook spool.

Nested repository ordinary files are included; their Git administration is excluded. Symlink targets are stored without following directory/file links outside the workspace. Regular files use no-follow reads and stat checks with up to three attempts. Read failures retain previously captured bytes and report an issue rather than inventing deletions. Empty directories and unsupported file types are metadata in snapshot records.

Default limits are 8 MiB per file and 64 MiB of captured bytes per scan. Larger files become metadata-only and the checkpoint is marked partial. Journal payloads above 1 MiB are reduced to scalar metadata with `payload_truncated`. Raw hook payloads and included workspace content may contain private information; the store is local with a mode-0700 directory. Exclusions are a stated policy, not comprehensive secret detection. Terminal output is not recorded by this slice.

A soft 512 MiB store threshold suspends new content checkpoints and records failures while preserving earlier history. One capture can exceed the threshold, and the journal continues to grow so health failures remain visible. This is not yet a hard disk-space bound or a retention policy. No automatic deletion or garbage collection is performed.

Observation captures live states rather than every write syscall. Scans are not atomic across the workspace. Rapid intra-tool edits or short-lived files may disappear between captures. Watcher overflow is journaled and triggers reconciliation; reconciliation cannot reconstruct lost intermediate states.

## Verification and remaining work

Run `python3 -m unittest discover -s tests -v`. **28 tests pass on macOS**, including seven new recorder tests. The recorder tests cover dirty-project index/ref isolation, intermediate edit/revert history, exclusions before object storage, binary bytes, symlinks/executable modes, metadata-only limits, single-writer locking, interrupted intent/ref recovery, actual polling observation, hook deduplication, overflow visibility, baseline launch gating, a recorded curses session and historical diff command, and visible quota failure.

Still open before accepting the complete Phase 1 milestone:

- Configurable capture/retention policy, hard storage bounds, coordinated pruning, and disk-full fault injection.
- Larger workspace performance measurements and incremental capture optimizations.
- Broader crash-point testing, including actual abrupt process termination and storage faults.
- Linux verification and continued native-provider terminal acceptance.

The authenticated collector protocol, synchronous provider-boundary snapshots, normalized provider lifecycle coverage, and native hook composition are subsequent adapter work. Saved-session navigation in curses and specialized visualizers belong to the review MVP. [UIGuide.md](../UIGuide.md) describes current navigation and the planned views.
