# Phase 1 recorder: initial implementation

Date: 2026-10-02. Status: **recorder, capture policy/storage limits, and crash recovery/retention slices implemented; performance and cross-platform acceptance remain open**.

The user verified that Codex and Claude can run through Labradour's workspace launcher. This supports starting Phase 1; the remaining Phase 0 terminal, provider-hook, and Linux checks continue separately.

## Available now

`run --record DIRECTORY` enables a single-writer recorder in a background thread. Ordinary `run` and observation-only `--watch` remain available. The recorder uses a SQLite journal and a private bare Git repository. Journal updates are committed and fsynced in private sibling staging directories, then atomically replace the retained journal after a budget check. Readers open a consistent saved database. Existing WAL recordings are copied into this format with committed facts preserved. An exclusive writer lock prevents simultaneous recordings into one store; workspace identity prevents reuse for a different root.

The launcher forks a paused PTY child before starting watcher threads. It starts observation, captures a baseline and startup reconciliation, then releases the child to execute. Periodic reconciliation runs every two seconds; watcher batches trigger capture on approximately 100 ms worker ticks, subject to scan duration. Shutdown stops the child process group before the final scan.

The journal stores session lifecycle, filesystem observations, raw provider hook payloads when hooks are enabled, snapshot intent/completion/failure, collector overflow, and recovery records. Existing spool files are ignored at recorder construction so they are not assigned to a new launch. Event IDs deduplicate newly received hook records. Provider event normalization and attribution remain Phase 2 work; recording hooks does not establish their coverage.

Each checkpoint uses immutable captured bytes, file/symlink modes, a private index, and a per-session ref. Unchanged manifests reuse the previous commit. A new session starts a new baseline root. Captured creates, edits, deletions, binary contents, executable bits, and reverted edits can be inspected through `history` and `diff`. The live Visualization pane exposes checkpoint/event details; the full historical review UI remains Phase 4.

Recovery marks sessions left running as interrupted, identifies unfinished snapshot intents, records the retained session ref and whether the checkpoint advanced as recoverable evidence, and preserves that history. A persisted session-end event resolves a crash before the status-table update as completed. It starts a separate session from current workspace contents. It does not assert completion of the interrupted snapshot or recover objects that never reached a ref. This implements process-crash recovery, not a demonstrated guarantee against power loss.

## Capture policy and limits

Exclusions are applied before reading content, independently of project `.gitignore`:

- Any path component named `.git`, `.venv`, `__pycache__`, `.agentide-spike`, `.labradour`, `node_modules`, `vendor`, `build`, `dist`, `.aws`, `.ssh`, `.codex`, or `.agents`.
- `.env` and names beginning `.env.`.
- The recording directory and active hook spool.

Nested repository ordinary files are included; their Git administration is excluded. Symlink targets are stored without following directory/file links outside the workspace. Regular files use no-follow reads and stat checks with up to three attempts. Read failures retain previously captured bytes and report an issue rather than inventing deletions. Empty directories and unsupported file types are metadata in snapshot records.

Default limits are 8 MiB per file and 64 MiB of captured bytes per scan. `--capture-policy` accepts validated JSON rules; CLI options override sizes and extend exclusion/metadata-only patterns. The scanner and watcher share effective exclusions. `policy` previews rules without creating a store, and Ctrl-] then p displays them in the UI. Per-session policy files retain the exact effective rules even when event payloads are truncated. Larger files become metadata-only and the checkpoint is marked partial. Journal payloads above 1 MiB are reduced to scalar metadata with `payload_truncated`. Raw hook payloads and included workspace content may contain private information; the store is local with a mode-0700 directory. Exclusions are a stated policy, not comprehensive secret detection. Terminal output is not recorded by this slice.

A hard 512 MiB retained-file-byte budget covers the journal, Git objects/refs, per-session policy, and a fixed 4096-byte health slot. The minimum configurable budget is 64 KiB. Git work is staged outside the recording directory, and complete file batches are checked before any installation. Journal writes are similarly staged and checked; they cannot keep growing after exhaustion. File replacements are fsynced, with Git objects installed before refs. Checkpoint intent and completion still bracket Git installation, preserving recovery semantics when a completion cannot fit.

On exhaustion, recording stops, earlier data remains available, and the native agent continues. The footer and `recording-status` report the persistent gap through the health slot. Reusing a store with a smaller-than-current budget is rejected without pruning. A subsequent launch with a larger budget can reconcile the workspace into a new session.

This budget measures regular-file sizes, not allocated filesystem blocks, and excludes sibling staging directories and the hook spool. The spool must be outside the store. Staging temporarily copies the journal/Git history plus new checkpoint objects, requiring additional same-filesystem disk space. Normal success/failure removes staging; recovery and applied retention remove abandoned staging directories with the current store-specific hashed prefix under the writer lock. Legacy stages without an ownership identifier are preserved. No automatic deletion of saved sessions is performed. Explicit whole-session retention is available as described below.

Observation captures live states rather than every write syscall. Scans are not atomic across the workspace. Rapid intra-tool edits or short-lived files may disappear between captures. Watcher overflow is journaled and triggers reconciliation; reconciliation cannot reconstruct lost intermediate states.

## Crash recovery and retention slice

`prune DIRECTORY --keep-sessions N` previews a count-based policy; `--apply` executes it for an inactive recording. The minimum is one session, default ten. Newest sessions and running-status sessions are protected. Applying cleanup takes the same exclusive writer lock as the recorder. Recovery and the recorder's new baseline still occur before the child starts.

Pruning writes a bounded operation intent into the reserved health slot, then deletes the selected journal/session facts and compacts the staged database. It retains a journal audit table of removed IDs. Only afterward does it remove the corresponding session refs and policy files. Packed-ref edits are staged; loose ref deletions do not create in-store Git lock files. Each batch contains at most 32 sessions, allowing arbitrarily many removals without overflowing the operation slot.

The operation slot survives interrupted pruning. Resume repeats the journal/ref cleanup and reclaims loose objects using reachability from all surviving private refs and commit references in snapshot/session/bookmark records. Shared content and cross-session evidence remain reachable. Source roots are checked before facts are removed. Packed object files are preserved; full pack compaction is deferred and is explicitly reported. Current recorder-created histories use loose objects, so their unreferenced content is reclaimed immediately.

Startup also removes unreferenced loose fragments from interrupted object installation and repairs a partially initialized private repository. Owned abandoned stage directories use a store-specific prefix and are removed under its lock; another store's stages and symlinks are left alone. No saved project data or project Git state is touched.

I/O errors stop further recording, preserve saved journal/ref evidence, and keep the native agent interactive. The recorder attempts a fixed-size in-place health overwrite when staging is unavailable. If that too fails, the UI reports that health could not be persisted; the remaining running session/intent facts are reconciled after storage is restored. This fallback is best effort and may itself be interrupted. Filesystem corruption and power-loss durability are not claimed.

## Verification and remaining work

Run `python3 -m unittest discover -s tests -v`. **51 tests pass on macOS**, including policy and hard-budget tests alongside the original recorder checks. The recorder tests cover dirty-project index/ref isolation, intermediate edit/revert history, exclusions before object storage, binary bytes, symlinks/executable modes, metadata-only limits, single-writer locking, interrupted intent/ref recovery, actual polling observation, hook deduplication, overflow visibility, baseline launch gating, a recorded curses session and historical diff command, and visible quota failure. Policy/budget checks additionally cover CLI previews and validation, exclusion-before-read/watch/object-storage, metadata-only selection, deterministic scan-limit omissions, disabling optional defaults, rejection of entire Git batches, journal exhaustion, installation-by-installation size bounds, budget increases after exhaustion, legacy WAL migration, payload truncation, and preventing an in-store hook spool. Recovery/retention tests add real abrupt exits at intent, scan, object-install, ref-install, and completion boundaries; SIGKILL; partial repository initialization; interrupted session-end status updates; disk-full staging/ref-sync failures; unreadable capture retention; preview/apply; interrupted prune recovery at journal/ref/GC boundaries; packed refs/objects; active-writer exclusion; store-specific stage cleanup; and cross-session journal evidence protection.

Still open before accepting the complete Phase 1 milestone:

- Larger workspace performance measurements and incremental capture optimizations.
- Power-loss/corruption experiments and physical filesystem fault testing beyond deterministic error injection.
- Pack compaction and age-based/automatic retention if needed.
- Linux verification and continued native-provider terminal acceptance.

The authenticated collector protocol, synchronous provider-boundary snapshots, normalized provider lifecycle coverage, and native hook composition are subsequent adapter work. Saved-session navigation in curses and specialized visualizers belong to the review MVP. [UIGuide.md](../UIGuide.md) describes current navigation and the planned views.
