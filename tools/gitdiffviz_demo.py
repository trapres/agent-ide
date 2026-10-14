#!/usr/bin/env python3
"""Build the pinned backend and demo Labradour evidence in GitDiffViz's viewer."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from labradour.exports import ArtifactCache
from labradour.gitdiffviz import SOURCE_REVISION, gitdiffviz_export
from labradour.recorder import Recorder
from labradour.review import SavedReview


def prepare(checkout, root):
    """Use committed pinned sources, never another checkout's unverified binary."""
    source = root / 'source'
    source.mkdir()
    archive = subprocess.check_output([
        'git', '-C', str(checkout), 'archive', SOURCE_REVISION])
    with tarfile.open(fileobj=io.BytesIO(archive)) as members:
        # Only regular files/directories from the pin; no archive links or traversal.
        for member in members.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('unsupported pinned source archive entry')
        members.extractall(str(source))
    print('Building pinned GitDiffViz backend…', flush=True)
    subprocess.run(['opam', 'exec', '--switch=' + str(checkout), '--',
                    'dune', 'build', '--root', str(source), 'bin/main.exe'],
                   cwd=str(root), check=True)
    binary = source / '_build/default/bin/main.exe'
    config = root / 'gitdiffviz.json'
    config.write_text(json.dumps({
        'schema_version': 1, 'source_revision': SOURCE_REVISION,
        'binary': str(binary), 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()
    }, indent=2) + '\n')

    workspace = root / 'workspace'
    workspace.mkdir()
    target = workspace / 'pricing.py'
    target.write_text('''def total(items):
    subtotal = sum(items)
    return subtotal
''')
    recording = root / 'recording'
    recorder = Recorder(workspace, recording, backend='polling')
    try:
        recorder.capture('demo baseline')
        target.write_text('''def total(items, discount=0):
    subtotal = sum(items)
    discounted = subtotal * (1 - discount)
    return round(discounted, 2)
''')
        recorder.capture('demo edit')
        target.write_text('''def total(items):
    subtotal = sum(items)
    return subtotal
''')
        recorder.capture('demo revert')
        (workspace / 'receipt.py').write_text('def receipt(amount):\n    return f"Total: ${amount:.2f}"\n')
        recorder.capture('demo create')
        (workspace / 'receipt.py').unlink()
        recorder.capture('demo delete')
    finally:
        recorder.close()
    view = SavedReview(recording)
    row = next(row for row in view.activity.rows if row['kind'] == 'effect'
               and row['payload']['operation'] == 'file.modify'
               and row['payload']['path'] == 'pricing.py')
    cache = root / 'exports'
    result = gitdiffviz_export(recording, view.activity.records, row,
                              view.activity.revision, ArtifactCache(cache), config)
    document = json.loads(Path(result['path']).read_text())
    scene = document['evidence']['gitdiffviz']['scene']
    (root / 'scene.json').write_text(json.dumps(scene) + '\n')
    manifest = dict(root=str(root), recording=str(recording), config=str(config),
                    cache=str(cache), artifact=result, source_revision=SOURCE_REVISION,
                    scope='captured selected-file structural scene; no semantic extraction')
    (root / 'demo.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def serve(root, port):
    """Serve only the pinned viewer assets and the exported scene on loopback."""
    viewer = root / 'source/viewer'
    routes = {
        '/': (viewer / 'index.html', 'text/html'),
        '/index.html': (viewer / 'index.html', 'text/html'),
        '/app.js': (viewer / 'app.js', 'text/javascript'),
        '/styles.css': (viewer / 'styles.css', 'text/css'),
        '/vendor/highlight.min.js': (viewer / 'vendor/highlight.min.js', 'text/javascript'),
        '/scene.json': (root / 'scene.json', 'application/json'),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = routes.get(urlsplit(self.path).path)
            if route is None:
                self.send_error(404)
                return
            path, content_type = route
            content = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', port), Handler) as server:
        print('GitDiffViz viewer: http://127.0.0.1:%d' % server.server_port, flush=True)
        print('Open that URL; click pricing.py, try zoom and theme. Ctrl-C stops the viewer.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=ROOT.parent / 'gitdiffviz',
                        help='GitDiffViz checkout with an installed local opam switch')
    parser.add_argument('--prepare-only', action='store_true', help='Build/export without starting a viewer')
    parser.add_argument('--reuse', type=Path, help='Reuse a previously prepared demo directory')
    parser.add_argument('--review', action='store_true', help='Open Labradour saved review with GitDiffViz configured')
    parser.add_argument('--port', type=int, default=0, help='Loopback viewer port; default chooses a free port')
    args = parser.parse_args()
    if args.reuse:
        root = args.reuse.resolve()
        manifest = json.loads((root / 'demo.json').read_text())
    else:
        base = ROOT / '.labradour-spike'
        base.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix='gitdiffviz-demo-', dir=str(base)))
        manifest = prepare(args.checkout.resolve(), root)
    print('Demo directory: ' + str(root), flush=True)
    print('Captured edit/revert/create/delete; real GitDiffViz artifact: ' + manifest['artifact']['path'], flush=True)
    if args.prepare_only:
        return
    if args.review:
        subprocess.run([sys.executable, '-m', 'labradour', 'review', manifest['recording'],
                        '--gitdiffviz-config', manifest['config'],
                        '--export-cache', manifest['cache'], '--ignore-layout-config'],
                       cwd=str(ROOT), check=True)
    else:
        serve(root, args.port)


if __name__ == '__main__':
    main()
