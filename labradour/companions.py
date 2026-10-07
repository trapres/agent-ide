"""Host-rendered static local companions; opening always needs explicit activation."""
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from .exports import ArtifactCache, ExportError, digest
from .gitdiffviz import strict_json

ID = re.compile(r'[0-9a-f]{64}')
HTML_LIMIT = 8 * 1024 * 1024


class OpenFailure(ExportError):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def escaped(value):
    value = str(value).encode('utf-8', 'backslashreplace').decode('utf-8')
    return html.escape(value, quote=True)


def read_artifact(cache, result, check):
    if not ID.fullmatch(str(result.get('id', ''))) or not ID.fullmatch(str(result.get('sha256', ''))):
        raise ExportError('opening requires a completed artifact ID and SHA-256')
    path = cache.path / (result['id'] + '.json')
    if not cache.path.exists():
        raise ExportError('completed export is missing or evicted; export again')
    with cache.locked():
        cache.entries()  # Reject unsafe entries before reading any completed payload.
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > cache.artifact_limit:
                raise ExportError('completed artifact exceeds limits')
            raw = stream.read(cache.artifact_limit + 1)
        check()
        if len(raw) > cache.artifact_limit or hashlib.sha256(raw).hexdigest() != result['sha256']:
            raise ExportError('completed artifact hash changed; export again')
        document = strict_json(raw)
        if (not isinstance(document, dict) or document.get('schema_version') != 1
                or not all(isinstance(document.get(key), dict) for key in ('artifact', 'provenance', 'evidence'))
                or document['artifact'].get('media_type') != 'application/json'
                or document['artifact'].get('id') != result['id']):
            raise ExportError('unsupported completed artifact envelope')
        if digest({k:document[k] for k in ('provenance', 'evidence')}, check) != result['id']:
            raise ExportError('completed artifact content key does not match')
        return document


def render(document, source_hash, check=lambda: None):
    provenance, evidence = document['provenance'], document['evidence']
    selection = evidence.get('selection', {})
    payload = selection.get('payload', {})
    session_view = selection.get('kind') == 'session'
    title = ('Session ' + str(payload.get('session_id')) if session_view else
             str(payload.get('path') or payload.get('tool') or 'Recorded evidence'))[:512]
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; object-src \'none\'; base-uri \'none\'; form-action \'none\'">',
        '<title>Labradour — ' + escaped(title) + '</title>',
        '<style>body{margin:0;background:#101923;color:#e6edf3;font:16px system-ui;line-height:1.5}main{max-width:1200px;margin:auto;padding:32px}h1{overflow-wrap:anywhere}h2{margin-top:32px}.meta{color:#acc0d0;overflow-wrap:anywhere}.badge{display:inline-block;border:1px solid #577183;padding:4px 10px;border-radius:8px}pre{background:#182634;padding:16px;overflow:auto;border-radius:8px;font:14px/1.5 monospace}svg{width:100%;height:auto}rect{fill:#20394b;stroke:#7197af}text{fill:#e6edf3;font:13px monospace}table{width:100%;table-layout:fixed}td,th{padding:8px;text-align:left;overflow-wrap:anywhere}details{margin:16px 0}summary{cursor:pointer;overflow-wrap:anywhere}.sides{display:grid;grid-template-columns:1fr 1fr;gap:20px}.sides>*{min-width:0}@media(max-width:700px){.sides{grid-template-columns:1fr}main{padding:16px}}</style></head><body><main>',
        '<div class="badge">Saved evidence · local companion</div><h1>' + escaped(title) + '</h1>',
        '<p>File attribution: external-or-unknown. Candidate intervals do not prove ownership.</p>',
        '<p class="meta">Session ' + escaped(provenance.get('session_id')) + '<br>Selection ' + escaped(provenance.get('selection_id')) +
        ('<br>Captured intervals: listed per effect' if session_view else
         '<br>Captured interval: ' + escaped(provenance.get('before_commit')) + ' → ' + escaped(provenance.get('checkpoint'))) +
        '<br>Source artifact SHA-256: ' + escaped(source_hash) + '</p>',
        '<p>This derived view uses captured evidence. It does not rerun commands or read today’s workspace.</p>']
    if session_view:
        projection = evidence['projection']
        parts += ['<h2>Session activity</h2><p>Status: ' + escaped(payload.get('status')) +
                  '. Journal sequence orders ingestion; candidate links do not prove causality. Terminal output was not recorded.</p>',
                  '<p>%d journal records · %d actions · %d effects · %d gaps</p>' % (
                      len(evidence['journal']), len(projection['actions']), len(projection['effects']), len(projection['gaps'])),
                  '<table><thead><tr><th>Sequence</th><th>Tool</th><th>State</th><th>Action / candidate effects</th></tr></thead><tbody>']
        sequences = {record['event_id']: record['sequence'] for record in evidence['journal']}
        for action in projection['actions']:
            check()
            parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s<br>%s</td></tr>' % (
                escaped(sequences.get(next(iter(action.get('observations', [])), None), 'unknown')),
                escaped(action.get('tool')), escaped(action.get('state')), escaped(action['id']),
                escaped(', '.join(action.get('effect_ids', [])))))
        parts.append('</tbody></table><h2>Recorded gaps</h2><pre>' +
                     escaped(json.dumps(projection['gaps'], ensure_ascii=True, indent=2)[:131072]) + '</pre><h2>Captured effects</h2>')
        effects = {effect['id']: effect for effect in projection['effects']}
        rendered_size = sum(len(part.encode('utf-8')) for part in parts)
        for pair in evidence['file_pairs']:
            check()
            effect = effects[pair['effect_id']]
            pair_document = {'provenance': dict(provenance, before_commit=effect.get('before_commit'),
                                               checkpoint=effect.get('checkpoint')),
                             'evidence': {'selection': {'kind': 'effect', 'payload': effect},
                                          'before': pair['before'], 'after': pair['after']}}
            page = render(pair_document, source_hash, check).decode('utf-8')
            states = '<h2>Captured states' + page.split('<h2>Captured states', 1)[1].split('<h2>Recorded payload', 1)[0]
            section = ('<details><summary>%s · %s · %s · sequence %s</summary><p class="meta">%s<br>%s → %s<br>'
                       'Candidate actions: %s</p>%s</details>') % (
                escaped(effect['path']), escaped(effect['operation']), escaped(effect['capture_quality']),
                escaped(sequences.get(effect['snapshot_event_id'], 'unknown')),
                escaped(effect['id']), escaped(effect.get('before_commit')), escaped(effect.get('checkpoint')),
                escaped(', '.join(effect.get('candidate_action_ids', []))), states)
            rendered_size += len(section.encode('utf-8'))
            if rendered_size > HTML_LIMIT:
                raise ExportError('session graphical view exceeds HTML limit; inspect JSON or export selected rows')
            parts.append(section)
    scene = evidence.get('gitdiffviz', {}).get('scene', {}).get('scene', {})
    nodes = scene.get('nodes', [])
    if nodes:
        visible = nodes[:200]
        height = ((len(visible) + 2) // 3) * 85
        parts += ['<h2>Captured file structure</h2><p>Structural analysis of the selected file only; synthetic analysis commits are recorded in provenance.</p>',
                  '<svg role="img" aria-label="Captured structure" viewBox="0 0 1080 %d">' % height]
        for i, node in enumerate(visible):
            check()
            x, y = (i % 3) * 360, (i // 3) * 85
            label = str(node.get('path') or node.get('name') or 'Captured root')[:512]
            parts.append('<g><title>%s</title><rect x="%d" y="%d" width="345" height="70" rx="8"/><text x="%d" y="%d">%s</text><text x="%d" y="%d">%s</text></g>' % (
                escaped(label), x, y, x + 12, y + 26, escaped(str(label)[:38]), x + 12, y + 50, escaped(node.get('kind', 'file'))))
        parts.append('</svg>')
        if len(nodes) > 200:
            parts.append('<p>Structure display truncated to 200 nodes.</p>')
    if 'before' in evidence or 'after' in evidence:
        parts.append('<h2>Captured states</h2><div class="sides">')
        for name in ('before', 'after'):
            check()
            side = evidence.get(name, {'state':'unavailable'})
            parts.append('<section><h3>' + name.title() + '</h3><p>' + escaped(side.get('state')) + ' · ' + escaped(side.get('reason') or side.get('mode', '')) + '</p>')
            if side.get('stale'):
                parts.append('<p>Warning: retained bytes are stale.</p>')
            if side.get('state') == 'available' and 'content_base64' in side:
                encoded = side['content_base64']
                if not isinstance(encoded, str) or len(encoded) > 350000:
                    raise ExportError('captured content exceeds companion limit')
                data = base64.b64decode(encoded, validate=True)
                try:
                    if b'\0' in data:
                        raise UnicodeError()
                    text = data.decode('utf-8')
                    lines = text.splitlines()
                    shown = (text if len(lines) <= 5000 else '\n'.join(lines[:5000]))[:262144]
                    parts.append('<pre>' + escaped(shown if data else '[empty captured file]') + '</pre>')
                    if len(lines) > 5000 or len(text) > 262144:
                        parts.append('<p>Content display truncated.</p>')
                except UnicodeError:
                    parts.append('<p>Binary/non-UTF-8 content: metadata only in this companion.</p>')
            parts.append('</section>')
        parts.append('</div>')
    parts += ['<h2>Recorded payload and provenance</h2>']
    details = json.dumps({'selection':selection, 'observations':evidence.get('observations', []),
                          'journal':evidence.get('journal', []), 'provenance':provenance}, ensure_ascii=True, indent=2)
    parts.append('<pre>' + escaped(details[:131072]) + '</pre>')
    if len(details) > 131072:
        parts.append('<p>Evidence display truncated; inspect the source JSON for complete exported details.</p>')
    parts.append('</main></body></html>')
    content = ''.join(parts).encode('utf-8')
    if len(content) > HTML_LIMIT:
        raise ExportError('graphical view exceeds HTML limit')
    check()
    return content


def launch(path, check=lambda: None):
    check()
    if sys.platform == 'darwin':
        command = ['/usr/bin/open', str(path)]
    elif sys.platform.startswith('linux'):
        executable = shutil.which('xdg-open')
        if not executable:
            raise ExportError('no xdg-open available; graphical artifact remains ready')
        command = [executable, Path(path).as_uri()]
    else:
        raise ExportError('graphical opening supports macOS and Linux only')
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise ExportError('desktop opener timed out; artifact remains ready') from exc
    except OSError as exc:
        raise ExportError('desktop opener unavailable; artifact remains ready') from exc
    if completed.returncode:
        raise ExportError('desktop opener failed; artifact remains ready')


def open_graphical(cache, source, check=lambda: None):
    document = read_artifact(cache, source, check)
    content = render(document, source['sha256'], check)
    companion = cache.publish_html(content, document['provenance'], check)
    with cache.locked():
        cache.entries()
        path = cache.path / (companion['id'] + '.html')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != companion['sha256']:
            raise ExportError('graphical artifact changed or was evicted; retry opening')
        try:
            launch(path, check)
        except ExportError as exc:
            raise OpenFailure(str(exc), dict(source, companion=companion, open_status='failed')) from exc
    return dict(source, companion=companion, open_status='requested')
