# Labradour + GitDiffViz demo

This demo connects Labradour's captured historical bytes to the real GitDiffViz
backend and its upstream interactive JS/SVG browser viewer. The launcher handles
the binary build, hash pin, adapter configuration, fixture recording and export.

## Start

From the Labradour repository:

```sh
.venv/bin/python tools/gitdiffviz_demo.py
```

Prerequisites: Labradour's `requirements.txt` installed in `.venv`, Git, opam,
and a sibling `../gitdiffviz` checkout whose local opam switch already has its
build dependencies installed. Use `--checkout /absolute/path/to/gitdiffviz` for
another checkout. No dependencies are installed by this launcher.

The launcher archives supported source revision
`1c5639469fdbadefca5b7dc4a93f260648d90ee1` into a fresh ignored
`.labradour-spike/gitdiffviz-demo-*` directory and builds it there. It does not use
the checkout's existing executable or edit the checkout. Each demo records an
edit, revert, creation and deletion, then exports the first `pricing.py` edit
through Labradour's real adapter. Explicit captures prevent rapid fixture writes
from losing the intermediate edit/revert.

Open the printed `http://127.0.0.1:PORT` URL. Expect a `pricing.py` card with
**+3 / -2**. Click the file to inspect its diff; try zoom, Up/Reset and the theme
toggle. The viewer shows the historical introduction of a discount parameter,
even though the fixture's current file has already been reverted. Ctrl-C stops
the loopback server. Only the pinned viewer assets and that exported scene are
served; recording and workspace directories are not web routes.

The directory persists for inspection and reuse. `demo.json` names the recording,
config, cache, backend revision and completed artifact/hash. The source checkout's
LICENSE and viewer dependency notices remain in the archived source.

## Review the same recording in Labradour

Use the directory printed by the launcher:

```sh
.venv/bin/python tools/gitdiffviz_demo.py --reuse DEMO_DIRECTORY --review
```

In Activity, select an edit/revert/create/delete effect with a captured interval.
Press Ctrl-] then `g` to run GitDiffViz, `t` for status and `o` to open the existing
static companion. The adapter is already configured; no manual SHA/config steps
are needed. Ctrl-Q closes review. Start the interactive viewer again with:

```sh
.venv/bin/python tools/gitdiffviz_demo.py --reuse DEMO_DIRECTORY
```

`--prepare-only` builds/captures/exports without starting a server or review.
`--port 4175` chooses a fixed port instead of a free one. The launcher prints a URL
and does not automatically open a browser.

## Scope and next integration step

This is a reproducible demo launcher. The interactive viewer displays one frozen
selected-file structural scene. It does not follow curses selections or live
agent changes, and it does not add semantic symbols, whole-project structure or
session timelines. Labradour's Ctrl-] o still opens its static companion.

The next product integration step is to give the interactive viewer an owned
lifecycle behind an explicit Labradour open action, preserve source artifact
provenance in the viewer, and update scenes on explicit selected-effect exports.
Broader workspace/timeline grants can follow after that selected-effect path.

Validated locally on macOS: clean pinned backend build, captured export and Chrome
repository/file navigation with the expected line counts. This does not close
Linux desktop or real-provider acceptance gates.
