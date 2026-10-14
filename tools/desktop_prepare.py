#!/usr/bin/env python3
"""Prepare pinned local GitDiffViz assets/backend for the Tauri development app."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitdiffviz_demo import ROOT, prepare
from labradour.gitdiffviz import SOURCE_REVISION, configuration


def main():
    base = ROOT / '.labradour-spike'
    base.mkdir(exist_ok=True)
    manifest = None
    for path in sorted(base.glob('gitdiffviz-demo-*/demo.json'), reverse=True):
        candidate = json.loads(path.read_text())
        try:
            if candidate['source_revision'] != SOURCE_REVISION:
                continue
            configuration(Path(candidate['config']))
            manifest = candidate
            break
        except (OSError, ValueError):
            continue
    if manifest is None:
        directory = Path(tempfile.mkdtemp(prefix='gitdiffviz-demo-', dir=str(base)))
        manifest = prepare(ROOT.parent / 'gitdiffviz', directory)
    source = Path(manifest['root']) / 'source'
    destination = ROOT / 'desktop/dist/gitdiffviz'
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('index.html', 'styles.css', 'vendor'):
        origin, target = source / 'viewer' / name, destination / name
        if origin.is_dir():
            shutil.copytree(origin, target, dirs_exist_ok=True)
        else:
            shutil.copyfile(origin, target)
    # Keep upstream native repository commands out of the embedded captured-data panel.
    app = (source / 'viewer/app.js').read_text()
    start = app.index('async function loadSceneDocument() {')
    end = app.index('\nasync function boot()', start)
    app = app[:start] + '''async function loadSceneDocument() {
  return new Promise(resolve => {
    let loaded = false;
    window.addEventListener('message', event => {
      if (event.source !== parent || event.data?.type !== 'labradour-scene') return;
      if (!loaded) { loaded = true; resolve(event.data.scene); }
      else applyDocument(event.data.scene);
    });
    parent.postMessage({ type: 'gitdiffviz-ready' }, '*');
  });
}
''' + app[end:]
    app = app.replace('return Boolean(globalThis.__TAURI__?.core?.invoke);', 'return false;')
    app = app.replace('return globalThis.__TAURI__?.core?.invoke?.(command, args) ?? null;', 'return null;')
    (destination / 'app.js').write_text(app)
    # The upstream full-page breakpoint stacks its sidebar above the scene. In a
    # quarter-window panel the scene must remain visible without that scroll.
    with (destination / 'styles.css').open('a') as stream:
        stream.write('''
/* Labradour embedded-panel layout; renderer behavior stays upstream. */
html, body { height: 100%; margin: 0; overflow: hidden; }
body { display: flex; flex-direction: column; font-size: 12px; }
.topbar { padding: 8px 10px; gap: 8px; align-items: center; }
.topbar h1 { display: none; }
.topbar p { font-size: 11px; }
.controls { gap: 4px; }
.controls button { padding: 4px 8px; font-size: 11px; }
.layout { display: flex; flex: 1; min-height: 0; padding: 0 8px 8px; }
.sidebar, .commit-header { display: none; }
.canvas-panel { display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; }
.canvas-toolbar { min-height: 32px; padding: 3px 8px; font-size: 11px; }
#scene { height: 100%; min-height: 0; flex: 1; }
''')
    shutil.copyfile(source / 'LICENSE', destination / 'LICENSE')
    (base / 'desktop-runtime.json').write_text(json.dumps({
        'config': manifest['config'], 'recording': manifest['recording'],
        'cache': manifest['cache'], 'workspace': str(ROOT), 'source_revision': SOURCE_REVISION,
        'path': os.environ.get('PATH', os.defpath)
    }, indent=2) + '\n')
    print('Prepared pinned GitDiffViz renderer and backend for Labradour desktop')


if __name__ == '__main__':
    main()
