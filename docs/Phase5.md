# Phase 5: exports and graphical companions

The export foundation provides explicit, background JSON evidence exports from
live or saved review. An optional pinned gitdiffviz adapter now exports a
selected captured file pair as structured diff/scene JSON. Explicit static HTML
companions are available; the core UI retains built-in views.

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
and do not prevent recorder retention. Sharing remains a separate user action.
Exporting alone starts no browser/window/server; opening requires Ctrl-] o.

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

For the export foundation, all **169 tests passed** on macOS/Python 3.9 (57.776 seconds) and Linux/Python 3.11
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

Next: session exports and broader macOS/Linux companion acceptance
checks. External manifest/configuration/JSON-RPC, plugin-facing artifact chunk
RPC, arbitrary discovery, plugin-supplied HTML/PNG/SVG, whole-tree grants and
a cache inspector inside curses remain unimplemented. [VizApi.md](VizApi.md)
remains broader than the internal runtime.

## Optional gitdiffviz adapter

The supported source revision is
`1c5639469fdbadefca5b7dc4a93f260648d90ee1` of
[gitdiffviz](https://github.com/superstealthlogic/gitdiffviz/tree/1c5639469fdbadefca5b7dc4a93f260648d90ee1).
The backend reports version 0.1.0. A clean `git archive` of that revision was
built in a separate temporary directory using the already installed project
opam switch and `dune build ... bin/main.exe`; the original checkout was unchanged.
The rebuilt macOS executable's SHA-256 is recorded in
[gitdiffviz-adapter-acceptance.json](gitdiffviz-adapter-acceptance.json).
Unlike the earlier Phase 0 probe, this measurement used a fresh pinned-source
build, rather than an existing unverified build output.

Configuration is explicit; no executable discovery or installation occurs:

```json
{
  "schema_version": 1,
  "binary": "/absolute/path/to/gitdiffviz/_build/default/bin/main.exe",
  "source_revision": "1c5639469fdbadefca5b7dc4a93f260648d90ee1",
  "sha256": "REPLACE_WITH_THIS_EXECUTABLES_64_CHARACTER_SHA256"
}
```

Pass `--gitdiffviz-config FILE` to `run`, `review` or CLI `export`. Files are
bounded to 4 KiB, strict JSON with exactly those fields; unsupported revisions,
versions, duplicate keys, nonfinite numbers, nonregular/symlink direct paths,
relative executables and SHA mismatches are refused. Configuration is validated
only on an explicit optional export, so missing/broken tools cannot prevent the
core UI from starting. Runtime hashing verifies the supplied executable; the
source revision is a user build declaration, not a cryptographic certificate
embedded in the binary. Different platform/compiler builds need their own hash.
On a cache miss, a private executable copy is hashed again before invocation;
an external edit to the installed executable cannot retarget that job.

Ctrl-] then **g** requests the adapter; **x** remains ordinary JSON evidence.
Choose a file effect directly or with the existing effect picker. CLI equivalent:

```sh
python3 -m labradour export RECORDING --session SESSION_ID --row ROW_ID \
  --exporter gitdiffviz --gitdiffviz-config FILE --export-cache CACHE
```

Supported scope: complete non-stale regular-file creation/modification/deletion
effects with a captured before/after interval, including binary/empty/mode-only
changes. First-baseline content has no prior captured boundary and is refused.
Omissions, missing objects, stale reads, symlinks and control/surrogate filenames
remain available through built-in/JSON evidence instead. Rename inference,
semantic symbols, actor ownership, whole-project structure and timelines are
not claimed by this selected-file adapter.

Only the granted file bytes/modes cross the adapter boundary. It does not clone
the recording or pass the workspace/private history to the subprocess. In a
private disposable Git worktree it builds two deterministic synthetic commits
from the captured states, materializes the captured regular target for line
counting, runs `extract-diff`, then `build-scene`. System/global Git settings,
templates, replacement objects and credential/collector environment are removed
or disabled. No project command is rerun and no live workspace is consulted.
The original checkpoint pair stays in provenance; `synthetic_comparison` records
the separate analysis commits. Temporary repo roots are normalized in exported
documents. Validated v1 envelopes must match the synthetic pair and selected
path/ancestor scope. The scene is structural, without semantic extraction.

The artifact contains ordinary captured evidence plus
`evidence.gitdiffviz.diff` and `evidence.gitdiffviz.scene`. Exporter/version,
backend source/hash, selected-file scope and structural-only limitations are
part of the cache key. Verified request/content digests reuse unchanged scenes
without rerunning the backend, while still revalidating the executable and
captured evidence. Corrupt envelopes rebuild current payloads; a related cache
envelope-rebuild bug found during these checks was fixed in the foundation.

All subprocesses run off the input thread in owned process groups with a
10-second individual deadline and the export's 30-second cooperative aggregate
deadline. Captured stdout/stderr is capped at 1 MiB total and never copied into
user diagnostics. Each generated document is capped at 8 MiB. The executable
is capped at 32 MiB; temporary regular-file usage is monitored at 64 MiB/256
entries, including its copy, and discarded at completion/failure/cancellation.
Owned process groups are killed and the direct child is reaped on cleanup. These are application-level
bounds and sampled temporary-disk checks, not an OS sandbox or hard memory/disk
quota for an explicitly trusted executable.

**Measured native scope:** macOS pinned backend edit/revert/create/delete/empty/
binary/mode cases, successful structural scenes, verified cached repeats and
unchanged recording digests. Linux exercises deterministic executable fixtures
and real Git/process/cache behavior in the full suite. The acceptance container
has no OCaml/opam toolchain; native Linux backend execution, browser rendering,
semantic accuracy, distribution packaging and session timeline acceptance remain
unmeasured. The next companion gate retains these distinctions.

**Automated validation:** all 178 tests pass on macOS/Python 3.9 (67.617 seconds)
and Linux/Python 3.11 (24.385 seconds), including nine new adapter tests. Coverage
includes scoped scene/provenance/cache behavior, disabled/strict configuration,
binary pin/snapshot tampering, invalid grants and missing evidence, malformed/
widened/versioned output, bounded logs/temp usage, cancellation with descendants,
JSON/terminal fallback, corrupt-cache rebuilding, CLI routing and a rendered
saved-review optional export that returns to the original source view.

The pinned upstream project is
[MIT licensed](https://github.com/superstealthlogic/gitdiffviz/blob/1c5639469fdbadefca5b7dc4a93f260648d90ee1/LICENSE).
This repository does not bundle its executable or vendored dependencies. If a
later distribution includes binaries/source, preserve upstream MIT notices and
review dependency/vendor licenses separately. The upstream
[build instructions](https://github.com/superstealthlogic/gitdiffviz/blob/1c5639469fdbadefca5b7dc4a93f260648d90ee1/README.md)
describe the required opam/C/compiler dependencies.

## Graphical opening

Ctrl-] **o** opens the last completed export, separately from Ctrl-] x/g.
The source is frozen by ID and exact published SHA-256, read from its owned
private cache entry, and validated for envelope/content-key integrity.
Plugin-supplied paths and URLs are ignored. Opening reads no workspace or recorder
files and does not acquire a recording retention lease.

The host renders a self-contained static HTML page with provenance, recorded
payload, captured before/after states and, for gitdiffviz, an SVG grid of structure
cards. This is a bounded presentation of exported nodes, not the full upstream
interactive viewer. All recorded strings are escaped. The page has no scripts,
external assets or local server, and includes a script/network-blocking CSP.
Binary/non-UTF-8 content remains metadata-only; unavailable, absent, stale and
empty captured states stay distinct. Limits are 8 MiB HTML, 200 structure nodes,
5000 lines/262144 characters per side and 131072 characters of displayed metadata,
with truncation labels and complete source JSON retained subject to cache policy.

HTML is content-addressed by its exact bytes and atomically published with the same
private permissions, quota/count/age cleanup and cancellation behavior as JSON.
Both formats count against shared limits; publishing can evict an older source.
An open page does not pin either artifact against eviction/clear. Re-export a
missing source. Browser windows belong to the user and remain after IDE shutdown.

The daemon opening worker preserves focus/selection and shares the single-job
slot. The 30-second deadline/cancellation checks remain cooperative.
Desktop dispatch uses macOS /usr/bin/open or Linux xdg-open on only the owned
local HTML path, without a shell, and bounds the helper to five seconds.
A successful request is not a browser-rendering acknowledgement. Failure retains
the generated page and source result for retry/inspection; cancellation cannot
undo a window already dispatched.

CLI: python3 -m labradour open-export ARTIFACT_ID --sha256 SHA256 --export-cache CACHE.
It returns the source result plus companion metadata and open_status; a desktop
failure prints the ready result and exits 2. Generic validation failure exits 2.

All 187 tests pass on macOS and Linux, including source tampering/symlink refusal,
escaped markup, explicit opening, cache reuse/cleanup, failure fallback, CLI and
mocked platform dispatch. A rendered live PTY check also confirms native input
arrives during a deliberately slow opener and the page retains the earlier export.
A disposable page was dispatched with the real macOS opener and its captured
states, structure and provenance verified in Chrome. Real Linux desktop rendering,
native Linux gitdiffviz and broader human/provider acceptance remain pending;
see the manual guide. Session-wide exports remain unimplemented.
