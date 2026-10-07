# Phase 5 manual testing guide

Currently available: explicit JSON evidence export and artifact-cache lifecycle.
gitdiffviz, graphical formats and companion opening are still upcoming slices.

## 1. Export selected historical evidence

Create a disposable recording with an edit/revert using the
[Phase 4 walkthrough](Phase4TestGuide.md#1-start-with-a-disposable-recording), or
use an existing private recording. Start from the repository:

```sh
python3 -m labradour review RECORDING --export-cache /tmp/labradour-export-check
```

Choose a fresh cache path. Until you explicitly export, it should not exist.
Focus Activity (Ctrl-] then `l`), filter `path:example.py tool:file.modify`, select
the desired effect with `j`/`k`, then press `d` to see its historical comparison.

Press Ctrl-] then `x`. Expect a short running/ready status; focus and selection
stay unchanged. Ctrl-] then `t` shows Export status, the complete result/path,
selection/session, size and SHA-256. Scroll with `j`/`k` if needed. No browser,
window or server opens. Press `s` or `e` to return to the historical view.

In a live recording, continue Agent input while exporting. Switch selection or
layouts during the job: the export keeps the originally requested scope and must
not replace the newly selected historical renderer. A second export while pending
gets a wait/cancel message. Ctrl-] then `c` requests cancellation; small jobs may
finish before cancellation arrives. Ctrl-Q must remain responsive.

## 2. Inspect artifact contents and cache behavior

The status path points to JSON. Inspect it locally with `python3 -m json.tool PATH`.
Expect `schema_version`, `artifact`, `provenance` and `evidence`. Check the session,
selection and checkpoint pair against the journal/actions. A selected effect has
`before`/`after` states; available bytes use `content_base64`, not current-workspace
reads. Decode locally if needed:

```sh
python3 -c 'import base64,json,sys; d=json.load(open(sys.argv[1])); print(base64.b64decode(d["evidence"]["after"]["content_base64"]).decode("utf-8"))' ARTIFACT_PATH
```

Only use this text display for a known ordinary UTF-8 fixture. Binary/symlink data
also uses base64. Metadata-only/unavailable sides have no `content_base64` and
must not masquerade as empty files. An action export includes its recorded payload
and linked observations without automatically exporting every linked file's bytes.
Pick an effect in Visualization to request its content. Unknown outcomes and
external-or-unknown attribution remain explicit; export does not rerun a command.

Repeat the same request on unchanged evidence: expect the same artifact ID/path
and a cached result. Changing selection/evidence revision yields another key.
For a live session, unrelated new durable rows can change the revision and prevent
reuse; this is conservative invalidation. Mutate or move the original workspace
after recording and repeat from saved review: historical bytes must stay captured.
Journal, Git refs/objects and policy files must not change from exporting.

## 3. CLI and explicit cache cleanup

```sh
python3 -m labradour export RECORDING --session SESSION_ID --row list
python3 -m labradour export RECORDING --session SESSION_ID --row ROW_ID \
  --export-cache /tmp/labradour-export-check
python3 -m labradour export-cache --export-cache /tmp/labradour-export-check
python3 -m labradour export-cache --export-cache /tmp/labradour-export-check --clear
```

Use an ID from the row listing. The export command returns completed artifact
metadata. Listing/inspection creates no artifacts. Clear removes only recognized
cache files and returns zero bytes/artifacts; the source recording stays intact.
The cache directory remains. Trying to export into the recording or workspace
must fail without affecting native input or recording.

Completed exports may retain private bytes after session pruning. Clear the cache
explicitly when those derived copies are no longer wanted. Defaults: 64 MiB per
artifact, 128 MiB total including pending files, 32 completed artifacts, seven-day
age. Expiry/oldest eviction and abandoned-partial cleanup occur on the next explicit
publish/clear; idle caches have no timer-based janitor. No CLI quota overrides are
implemented yet. The tests exercise small injected limits to force quota/age races
without asking you to generate huge private data.

## 4. Automated checks and current limits

```sh
python3 -m unittest tests.test_exports -q
python3 -m unittest discover -s tests -q
```

Tests deliberately inject slow exporters, cancellation, write failure, deadlines,
corrupt caches, unsafe paths and cross-process contention. Unfinished artifacts
must be cleaned; prior completed artifacts and source evidence remain valid.
Cached items can be evicted to satisfy policy. A crash may leave a private pending
file for cleanup on the next explicit publish/clear.

The internal worker uses cooperative cancellation and a 30-second deadline; it
cannot forcibly interrupt arbitrary Python/file I/O. External subprocess isolation,
plugin manifest/config/RPC, graphical formats, gitdiffviz, automatic discovery,
graphical opening and session-wide export are not implemented. Existing human
native acceptance remains in [Phase4TestGuide.md](Phase4TestGuide.md).
