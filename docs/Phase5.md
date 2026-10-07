# Phase 5: exports and graphical companions

The export foundation provides explicit, background JSON evidence exports from
live or saved review. Graphical formats, gitdiffviz and companion opening are
the next slices; the core UI continues to use built-in terminal views.

## Export foundation

Ctrl-] then **x** exports the selected recorded Activity row. If Visualization
has an explicitly picked effect for that same parent, that effect is exported.
Otherwise the selected row is exported. This does not change focus, Activity
selection or the current historical renderer. Merely selecting, filtering,
resizing or preparing a view never exports anything.

Ctrl-] then **t** toggles export status in Visualization, including the completed
artifact path, SHA-256, size, cache-hit indicator, source selection/session and
evidence revision. Ctrl-] then **c** requests cancellation. The footer retains
recorder health and adds a short export status. Summary/evidence keys restore
the normal historical view.

One worker accepts one explicit job and has a one-result queue. Another request
while it runs is rejected with a wait/cancel message. The job captures its
original row and immutable replay records; later selection changes do not
retarget it. It can finish independently and only updates export status, never
the selected renderer. Scope validation, historical reads, hashing, encoding,
cache I/O and cleanup run off the input thread. Cancel/shutdown do not join
the worker. Cooperative checkpoints enforce a 30-second deadline; Git reads
retain the existing two-second subprocess/five-second comparison deadlines.
Python/file I/O is not hard process-isolated or forcibly preempted.

The built-in `labradour-evidence-json` version 1 exporter produces a single
`application/json` document:

- `schema_version: 1` and artifact ID/media type/creation time;
- `provenance`: exporter/version/options/format, recording directory, session,
  selection, evidence revision, checkpoint pair and attribution/limit labels;
- `evidence`: selected row and its recorded payload, linked raw observations for
  an action, and captured before/after states for a selected file effect.

File bytes are base64 encoded, with modes, checkpoint references, omission,
stale and missing-data states retained. An action export does not implicitly
read every linked file; select/pick an effect to export its captured bytes.
Generic rows retain recorded evidence. Unknown outcomes stay unknown. Commands
are never rerun. Path/checkpoint grants and read leases use the existing reader;
file content stays limited to 256 KiB per side. Once captured bytes are copied,
the read lease ends before artifact publication, so an export does not pin a
session for its entire lifetime. Missing data can yield a valid metadata-only
JSON artifact; a failed job is distinct from that evidence state.

## Private bounded artifact cache

Default location: `$XDG_CACHE_HOME/labradour/exports`, or
`~/.cache/labradour/exports`. `run`, `review` and `export` accept
`--export-cache DIRECTORY`. No cache is created until an explicit export or
explicit cache-clear operation. The cache must be separate from the recording,
live/layout workspace and recorded workspace, including ancestor/descendant
overlap. Unsafe paths fail locally without affecting recording.

Defaults are **64 MiB per artifact, 128 MiB total regular-file bytes, 32 completed
artifacts and seven days of age**. They are host constants for this slice, not
plugin-adjustable or exposed as CLI overrides. The byte budget includes pending
files. File/directory allocation overhead and OS page cache are not included.
The cache directory is private/owned (0700) and files are 0600. Symlink/nonregular,
foreign-owned, nonprivate or unrecognized direct entries are refused; the host
does not recursively delete an arbitrary directory.

Cooperating publishers take a nonblocking directory lock. Content-addressed keys
include exporter/version/options/format, evidence revision, selection/session,
comparison interval and the actual exported evidence. A cache hit verifies the
payload digest before reuse and returns a hash of the completed file bytes.
Corrupt recognized artifacts are rebuilt. JSON is preflighted for size, streamed
in at most 64 KiB chunks to a private pending file, fsynced, atomically published,
then the directory is fsynced. Failed/cancelled writes remove unfinished files.
A crash can leave a bounded pending file; the next explicit publish/clear cleans
it while holding the cache lock.

On explicit publication, expired artifacts and abandoned pending files are
removed; oldest artifacts are evicted to maintain count/byte limits. Cleanup is
lazy, not a background janitor: idle files older than seven days remain until
the next publish or explicit clear. Completed exports can retain captured private
bytes after source-session pruning; they are derived data, not journal roots,
and do not prevent recorder retention. Sharing and opening are separate future
actions. No browser/window/server is started in this slice.

## CLI workflow

```sh
python3 -m labradour history RECORDING
python3 -m labradour export RECORDING --session SESSION_ID --row list
python3 -m labradour export RECORDING --session SESSION_ID --row ROW_ID \
  --export-cache /path/to/private-export-cache
python3 -m labradour export-cache --export-cache /path/to/private-export-cache
python3 -m labradour export-cache --export-cache /path/to/private-export-cache --clear
```

`--row list` enumerates projected rows and child effects without creating a cache.
Export prints the artifact result, including its absolute path and SHA-256.
Cache inspection is read-only. Clear explicitly removes recognized completed
and unfinished artifacts from that cache, leaving recording evidence unchanged.
Neither command needs a native agent, the original workspace or gitdiffviz.

## Acceptance and next slices

All **169 tests pass** on macOS/Python 3.9 (57.776 seconds) and Linux/Python 3.11
(23.060 seconds), including 12 new export tests. Pending-export shutdown is also
checked in a rendered saved-review PTY: quitting does not wait for a deliberately
blocked worker and no artifact is published.

The cross-platform automated suite covers historical bytes after live mutation,
unchanged recording digests, scope/provenance/hashes, private permissions,
cache reuse and revision invalidation, omissions/missing grants, byte/count/age
limits, abandoned files, cancellation/failure cleanup, corrupt/unsafe caches,
one-job scope isolation, deadline and cross-process cache contention, linked
action observations without file scope widening, UI/CLI actions and explicit
cache clearing. A rendered PTY fixture injects a slow exporter and demonstrates
native input arriving before export completion; the artifact retains the earlier
selected checkpoint after a subsequent native edit.

See [Phase5TestGuide.md](Phase5TestGuide.md) for manual verification. Existing
[Phase 4 human native sign-off](Phase4TestGuide.md#7-human-native-mvp-sign-off)
retains its open status; synthetic export tests do not certify provider dialogs.

Next: verify a pinned gitdiffviz build and add an optional adapter for captured
revision pairs. Then add explicit companion opening/session exports and their
macOS/Linux acceptance checks. External manifest/configuration/JSON-RPC,
plugin-facing artifact chunk RPC, arbitrary discovery, HTML/PNG/SVG, whole-tree
grants, graphical opening and a cache inspector inside curses are not implemented
by this foundation. The proposed [VizApi.md](VizApi.md) remains broader than the
internal runtime.
