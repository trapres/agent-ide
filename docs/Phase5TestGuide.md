# Phase 5 manual testing guide

Currently available: explicit JSON evidence export, artifact-cache lifecycle and
an optional pinned gitdiffviz diff/scene JSON adapter, and explicit opening of
host-rendered static HTML companions, and bounded whole-session exports.

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
plugin manifest/RPC, plugin-supplied graphical formats, automatic discovery,
are not implemented. Existing human
native acceptance remains in [Phase4TestGuide.md](Phase4TestGuide.md).

## 5. Configure the optional gitdiffviz adapter

Use a clean gitdiffviz checkout at the supported revision:
`1c5639469fdbadefca5b7dc4a93f260648d90ee1`. Follow the pinned upstream
[build instructions](https://github.com/superstealthlogic/gitdiffviz/blob/1c5639469fdbadefca5b7dc4a93f260648d90ee1/README.md)
to install dependencies and build `bin/main.exe`. Check `git rev-parse HEAD` and
`git status --short` in that checkout; modified source needs a separately reviewed
supported revision rather than an invented pin. Labradour does not install tools.

Compute your built executable's hash without running it:

```sh
python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' /absolute/path/to/main.exe
```

Create an adapter JSON file, replacing the binary path and hash:

```json
{
  "schema_version": 1,
  "binary": "/absolute/path/to/main.exe",
  "source_revision": "1c5639469fdbadefca5b7dc4a93f260648d90ee1",
  "sha256": "YOUR_64_CHARACTER_LOWERCASE_SHA256"
}
```

The adapter pins your installed binary; the source revision is your build
declaration. The supplied macOS report hash applies to the measured local build,
not every compiler/platform build. No binary is bundled with Labradour.

Start `review RECORDING --gitdiffviz-config FILE --export-cache CACHE`, select a
known edit effect, then Ctrl-] **g** and Ctrl-] **t**. Expect Export ready and a JSON
artifact with `evidence.gitdiffviz.diff` and `evidence.gitdiffviz.scene`. No graphical
window opens from exporting; use the separate open action below. The terminal source diff remains available through `s`.

Check that only the selected path appears in the backend diff. Original checkpoint
IDs remain in provenance; `synthetic_comparison` differs because analysis uses a
disposable two-state, selected-file repository. Scene roots say captured-effect,
not a vanished temporary path. The captured before/after bytes should agree with
the terminal comparison even if today's workspace has changed. Repeat unchanged:
expect the same verified artifact with no backend rerun. Change the selection or
evidence revision to get a distinct artifact. Native analysis may take longer than
ordinary JSON export; input/recording must continue.

CLI and disposable real-backend checks:

```sh
python3 -m labradour export RECORDING --session SESSION_ID --row ROW_ID \
  --exporter gitdiffviz --gitdiffviz-config FILE --export-cache CACHE
python3 tools/gitdiffviz_probe.py --binary /absolute/path/to/main.exe
python3 -m unittest tests.test_gitdiffviz -q
```

The probe checks edit/revert/create/delete/empty/binary/mode-only captures,
single-file scope, scene generation, cached repeats and unchanged recording
digests. Its fixture data is disposable. Results from the rebuilt macOS pin are
in [gitdiffviz-adapter-acceptance.json](gitdiffviz-adapter-acceptance.json).

Try omitting configuration, supplying a bad hash, selecting a command row, a
first baseline, omitted content or a symlink. The optional export must fail locally;
the built-in view and Ctrl-] x JSON export remain usable. No empty/missing comparison
is invented. Tests inject backend nonzero exit, malformed/widened/versioned JSON,
log/temp overflow, executable tampering and cancellation with descendants.

Limits: binary 32 MiB, generated document 8 MiB each, captured logs 1 MiB total,
sampled temporary usage 64 MiB/256 entries, subprocess 10 seconds, export 30 seconds
with cooperative checks. The configured executable is trusted code, not OS-sandboxed.
Semantic symbols, cross-file rename inference, whole-workspace/session grants,
the full upstream interactive viewer and packaging remain subsequent work.

Native macOS backend cases are measured. Linux subprocess/adapter behavior is
tested with executable fixtures; the current container lacks OCaml/opam, so native
Linux backend execution is **pending**. On your Linux machine, build the same pin,
run this probe, then record its version/hash, platform and pass/fail results before
claiming native Linux acceptance. This does not replace the later companion visual
gate or the existing Phase 4 human native checks.

## 6. Open a completed graphical companion

Use the disposable recording/cache above, on macOS or a Linux desktop with a
working default browser and xdg-open. No gitdiffviz installation is needed for
ordinary captured evidence.

1. Before exporting, press Ctrl-] o. Expect “Open unavailable”; no window or cache
   artifact should appear. Select, scroll and resize views: none should open a browser.
2. Export with Ctrl-] x (or g for configured gitdiffviz), wait for “Export ready”,
   then press Ctrl-] o. Expect a separate local browser page and “Graphical opening
   requested”. Ctrl-] t shows source metadata plus companion path/hash/open_status.
   Focus and Activity selection remain unchanged inside Labradour; the OS may
   foreground the browser. Exporting alone still opens nothing.
3. Check the page title/path, session, selection, original captured interval and
   source SHA against the completed JSON. Before/after should match recorded
   evidence, even after editing/moving today's workspace. Unknown ownership stays
   explicit. Gitdiffviz adds structure cards for the selected file; ordinary JSON
   still gives captured states and provenance. Command/action exports show payload
   and observations without inventing file content or rerunning the command.
4. Select a different row before opening the completed result. The page must still
   describe the earlier export. Request a new export explicitly to open new evidence.
   Return to the terminal source view with s/e after focusing Visualization.
5. Repeat opening: expect the same HTML ID and cached companion, without rerunning
   gitdiffviz. Test UTF-8 text, creation/deletion, an empty file, binary and
   metadata-only captures. Absence and missing/stale bytes must not become empty
   files. Wide content scrolls horizontally; narrow browser windows stack sides.
   Large displays show truncation labels. Structure is a static node grid, not a
   zoomable upstream scene or semantic symbol graph.
6. In live recording, continue typing in Agent and change layouts during opening.
   Input/recording must continue; a second request while pending is refused.
   Ctrl-] c cancels preparation cooperatively, and Ctrl-Q remains responsive.
   A browser already opened remains user-owned after cancel/quit.
7. Inspect the HTML locally: no scripts, external assets, links or local server
   should exist. Captured markup should display as text. JSON and HTML are private
   files in the same separate bounded cache. Clear with export-cache --clear:
   both disappear, the recording stays intact, and an already loaded browser page
   may remain visible. Opening the removed result fails locally; export again.
8. On a headless Linux host or one without xdg-open, opening should fail locally.
   Ctrl-] t retains the generated companion path; inspect it or retry after the
   desktop is available. The ordinary terminal/JSON views remain usable. “Requested”
   means the desktop helper accepted the request, not proof a window rendered.

CLI opening uses the ID and exact SHA from a completed export, not its arbitrary
path or a URL:

```sh
python3 -m labradour open-export ARTIFACT_ID --sha256 SOURCE_SHA256 --export-cache CACHE
python3 -m unittest tests.test_companions tests.test_exports -q
```

Success returns source metadata plus companion metadata and open_status.
Desktop dispatch failure exits 2 but prints the ready companion result.
For a disposable cache, change one byte in the source JSON and try its previous
ID/SHA: validation must reject it before browser dispatch. Re-export to restore it.

Automated tests pass on macOS/Linux. Real macOS desktop dispatch and Chrome page
contents were checked with a disposable fixture; Linux desktop visual acceptance
and real native Linux gitdiffviz remain pending. Record OS/browser, tested cases,
input behavior and pass/fail results when checking those manually. Active plugin HTML, arbitrary URLs/server commands, PNG export and the
full upstream interactive viewer are not implemented.

## 7. Session exports and broad human sign-off

Start saved review of a disposable multi-edit recording. Press **Ctrl-] u** to
export the open session, then **Ctrl-] t** for status and **Ctrl-] o** to open it.
The request covers all durable session evidence, regardless of Activity filters,
expanded rows, selected effect, or pane layout. It does not launch an agent.
The live UI directs this shortcut to saved review instead.

CLI equivalent:

```sh
python3 -m labradour export RECORDING --session SESSION_ID --whole-session --export-cache CACHE
python3 tools/companion_probe.py
python3 -m unittest tests.test_session_exports.SessionExportTests tests.test_companions -q
```

Choose exactly one of --row and --whole-session. Whole-session JSON does not
invoke gitdiffviz or grant it the workspace/session; --exporter gitdiffviz is
rejected for that scope.

Check JSON evidence.journal against history --session, and projection against
the actions CLI. file_pairs has one entry per unique projected effect, not one
duplicate per candidate call. Each pair names its effect ID and retains the
recorded checkpoint interval. Provenance identifies the session and durable
journal revision. An unclosed saved session stays saved-unclosed, never
masquerading as complete. Terminal output is not reconstructed. Capture
metadata/policy omissions can exist in journal snapshots without a file effect;
the export must retain those facts without inventing a comparison.

The companion shows session status/counts, action states with sequence numbers
and candidate effect IDs, recorded gaps, and expandable captured effects.
Expand a few edit/revert/create/delete comparisons. Check original captured bytes,
binary/empty/missing states, unknown ownership and checkpoint intervals against
the built-in view. Metadata-only paths must expose no private bytes. Recorded
commands are displayed as evidence and never run.

While a session export is pending, switch sessions in the Agent/session pane,
filter Activity, resize or apply a layout. The result must retain the requested
session and leave the new selection/view intact. Repeating a pending request is
refused; cancellation/quit remain responsive. The journal is loaded by the
worker, so a saved unclosed recording may contain additional durable records
since its last UI refresh; provenance describes what was actually exported.

Limits: 10000 journal records, 256 unique effects, 8 MiB aggregate raw captured
pair bytes, existing 256 KiB-per-side reads, 64 MiB JSON and 8 MiB HTML. Content
counts repeated states in different comparisons. Exceeding a limit fails locally
with a selected-row fallback; it does not publish a partial session archive.
Large companion metadata has labeled display truncation; source JSON retains
the exported evidence. HTML can exceed its limit even when JSON fits.

Using only a disposable recording with a newer second session, export the older
session, then preview/apply prune --keep-sessions 1. The completed artifact must
still open and match its earlier bytes. Re-exporting the removed session must
fail. Prune may report busy during a short session evidence read: retry after the
job finishes. No export pins a session after publication. Cache clear removes
derived artifacts without touching the remaining recording.

The disposable probe records macOS/Linux timing and checks across history,
policy omission, unknown actions, gaps, escaped markup, cache reuse and pruning.
Reports: [macOS](companion-acceptance-macos.json) and
[Linux](companion-acceptance-linux.json). It does not open a desktop or certify
real provider behavior.

To close the human gate, record all applicable results below in your sign-off
notes. Report unavailable environments explicitly rather than marking them passed.

| Check | Expected result | Current evidence |
| --- | --- | --- |
| Session page in macOS browser, wide/narrow windows | Index and expanded states readable; hashes/provenance match JSON | Real macOS Chrome index/expanded comparison checked; narrow-window check pending |
| Session page on Linux desktop | xdg-open dispatch and readable page | Automated dispatch/static tests; desktop check pending |
| Native Linux gitdiffviz | Pinned real backend probe and selected-file page pass | Pending; container lacks OCaml/opam |
| Real Claude and Codex on each deployed OS | Row export/open during Agent input, paste/approval/tool traffic, layout changes; recorder stays responsive | Synthetic PTY checks pass; provider check pending |
| Saved session change, cancellation, limits, retention/cache cleanup | Original scope retained; local failure/fallback; no recorder mutation | Automated cases pass; manual workflow check pending |

For each row record OS/browser/provider/backend versions, recording fixture,
cases, observed behavior and pass/fail or explicitly accepted limitation. Earlier
Phase 2/4 human gates keep their own status; this slice does not close them.
