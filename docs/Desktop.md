# Labradour desktop

The Cargo/Tauri frontend hosts the native agent terminal, Activity and the
interactive GitDiffViz renderer in one window. The Rust host owns a Python
backend process; the existing PTY, authenticated hooks, recorder, history and
exporter remain shared with the terminal frontend. xterm.js replaces curses/pyte
for display in the desktop app.

## Setup and launch

Prerequisites: Python 3.9+ and the repository requirements installed in `.venv`,
Node/npm, Cargo/Rust, and platform Tauri development dependencies. GitDiffViz
requires the sibling `../gitdiffviz` checkout and its installed local opam switch
on first preparation, as described in [GitDiffVizDemo.md](GitDiffVizDemo.md).

```sh
.venv/bin/python -m pip install -r requirements.txt
cd desktop
npm install
npm run dev
```

Subsequent launches from the repository root:

```sh
.venv/bin/python -m labradour desktop
```

`npm run build` bundles local frontend assets and prepares the pinned GitDiffViz
backend/configuration. It reuses a verified prepared demo when available, or
builds the supported source revision using the existing opam switch. It never
installs an opam switch or downloads a different GitDiffViz revision implicitly.

On macOS, `cd desktop && npm run app` creates a double-clickable development
bundle at `desktop/src-tauri/target/debug/Labradour.app`. It retains a dependency
on this checkout, its `.venv`, and prepared runtime files. It is not a signed,
standalone distribution. The build records the command search path so Finder
launches can find the same native agents as the development terminal.

## Try the demo

1. Click **Sample recording**. The captured `pricing.py` edit opens in GitDiffViz
   inside the Visualization panel. Click its file card, zoom, and use Up/Reset.
   Source shows the captured text diff. No browser or local HTTP server is used.
2. Select the other edit, creation or deletion in Activity, then click
   **GitDiffViz**. Once that view is requested, subsequent file selections also
   request it. Unsupported/missing comparisons retain the source/evidence view.
3. Drag the vertical or horizontal divider to resize the terminal and review
   panels. The terminal's PTY dimensions track its actual available space.
4. Click **Start demo**, then type `run` in the terminal. Live filesystem and hook
   evidence appears in Activity. `size` and `colors` test terminal display.
   Click **Stop agent** to terminate its process group and save a final capture.

The deterministic sample explicitly captures edit/revert/create/delete states.
The live fake agent writes rapidly, so some intermediate states can be coalesced
by ordinary filesystem observation. Neither frontend records every write syscall.

## Choose an agent and model

Click **Open project…**, choose an existing workspace and select **Codex**,
**Claude**, or **Shell**. The **Model** field accepts a model ID/alias supported by
your installed provider and account. Blank uses the provider's configured default.
The launcher passes a supplied value as `--model MODEL`; the native agent's
`/model` picker is also available in its terminal. The desktop does not maintain
a separate model catalog or change account credentials.

The hook checkbox composes launch-scoped observation hooks using the existing
adapter. Native sign-in, project trust, hook trust and tool approvals appear in
the terminal. Codex runs in a dedicated process. Recording is enabled for desktop
launches; a blank recording field creates a fresh private temporary directory and
the footer displays its path. Choose a durable recording path to retain it beyond
OS temporary-file cleanup. Existing stores can be reused only for the same
workspace. Stop the current agent before launching another or changing recordings.

## Review and export

In **Open project…**, enter/browse a recording and click **Review saved**. After
loading, its sessions are available in the Saved session selector. Select another
and review again. Review never launches an agent; past terminal output is not
replayed. Activity supports plain text and `path:`, `tool:`, `actor:`, `turn:` and
`state:` filters. Clicking a row turns Follow off and preserves that selection.

**GitDiffViz** exports a selected captured file pair and supplies its scene to the
embedded renderer. The panel identifies the original captured interval and
attribution. The upstream renderer's synthetic Git comparison is not a project
commit or evidence of exclusive actor ownership. A stale result cannot replace
the newly selected view. **Cancel** requests cancellation of pending preparation.

**Export JSON** exports the selected row. **Export session** requires stopping
the live agent and exports durable saved evidence. The footer shows completed
artifact paths. Cache policy, quotas and missing-data semantics are shared with
the CLI. Artifact-cache inspection/clear and retention remain available through
the CLI.

## Migration scope and validation

Implemented desktop paths: native PTY input/output/paste/resize, demo and
Codex/Claude/Shell launch, model argument selection, authenticated observation
hooks, durable captures, Activity filtering, historical source/evidence,
embedded selected-file GitDiffViz, row/session export, cancellation, saved-session
review and orderly agent/backend shutdown.

The terminal frontend remains available during migration. Its saved layout
presets/editor, capture-policy editor/toggles and cache-management controls have
not been reproduced as desktop dialogs. The desktop currently uses the recorder's
default capture policy. GitDiffViz remains selected-file structural analysis;
semantic symbols, whole-project structure and session timelines are future work.

Checks:

```sh
.venv/bin/python -m unittest tests.test_desktop -v
cd desktop
npm run check
```

The desktop tests exercise real PTYs, resize/Unicode, captured edits after revert,
saved review/exports, model argument construction, request-error recovery and
process/EOF cleanup. macOS native UI checks cover sample-scene loading, live demo
input/activity, and Stop agent. Real Codex/Claude authentication, model availability,
approval/paste behavior and Linux desktop rendering need deployment-specific
checks; the launcher does not claim those are certified by the fake-agent demo.
