#!/usr/bin/env python3
"""Disposable saved-session/companion acceptance and timing; never opens a desktop."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labradour.companions import render
from labradour.exports import ArtifactCache, session_export
from labradour.policy import CapturePolicy
from labradour.recorder import Recorder
from labradour.retention import apply_retention
from labradour.adapters.providers import normalize


def probe():
    with tempfile.TemporaryDirectory(prefix='labradour-companion-probe-') as temporary:
        root = Path(temporary)
        workspace = root / 'workspace'
        workspace.mkdir()
        (workspace / 'source.py').write_text('alpha\n')
        (workspace / 'binary').write_bytes(b'\0old')
        (workspace / 'private').write_text('NOT-CAPTURED-SECRET')
        recorder = Recorder(workspace, root / 'recording', backend='polling',
                            policy=CapturePolicy(metadata_only=('private',)))
        recorder.capture('baseline')
        fact = normalize('claude', {'hook_event_name':'PreToolUse','session_id':'fixture',
                          'tool_use_id':'unknown-call','tool_name':'Bash','tool_input':{'command':'DO-NOT-RUN'}})
        recorder.append('adapter.event', fact, source='fixture')
        recorder.append('collector.overflow', {'reason':'fixture gap', 'dropped':1}, source='fixture')
        for index in range(20):
            (workspace / 'source.py').write_text('beta %d\n' % index)
            recorder.capture('edit')
        (workspace / 'source.py').write_text('alpha\n')
        (workspace / 'binary').write_bytes(b'\0new')
        (workspace / 'empty').write_bytes(b'')
        recorder.capture('revert binary empty')
        (workspace / 'created').write_text('<script>captured text</script>')
        recorder.capture('create')
        (workspace / 'created').unlink()
        recorder.capture('delete')
        recorder.close()
        store = root / 'recording'
        before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in store.rglob('*') if p.is_file()}
        cache = ArtifactCache(root / 'cache')
        started = time.monotonic()
        result = session_export(store, recorder.session, cache)
        export_ms = (time.monotonic() - started) * 1000
        doc = json.loads(Path(result['path']).read_text())
        started = time.monotonic()
        content = render(doc, result['sha256'])
        html_ms = (time.monotonic() - started) * 1000
        companion = cache.publish_html(content, doc['provenance'])
        repeated = session_export(store, recorder.session, cache)
        assert repeated['cached'] and repeated['id'] == result['id']
        assert cache.publish_html(content, doc['provenance'])['cached']
        assert b'<script>' not in content and b'&lt;script&gt;' in content
        assert 'NOT-CAPTURED-SECRET' not in Path(result['path']).read_text()
        assert doc['evidence']['projection']['actions'] and doc['evidence']['projection']['gaps']
        assert before == {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in store.rglob('*') if p.is_file()}
        newer = Recorder(workspace, store, backend='polling')
        newer.capture('newer')
        newer.close()
        apply_retention(store, 1)
        assert Path(result['path']).exists() and Path(companion['path']).exists()
        return {'platform':platform.platform(),'python':platform.python_version(),
                'scope':'durable session JSON/static HTML; no real desktop or provider dialogs',
                'records':len(doc['evidence']['journal']),'effects':len(doc['evidence']['file_pairs']),
                'actions':len(doc['evidence']['projection']['actions']),'gaps':len(doc['evidence']['projection']['gaps']),
                'export_ms':round(export_ms,3),'html_render_ms':round(html_ms,3),
                'json_bytes':result['bytes'],'html_bytes':companion['bytes'],
                'verified_cache_reuse':True,'source_recording_unchanged':True,'derived_artifacts_survive_prune':True,
                'cases':['baseline','edit','revert','creation','deletion','empty','binary','metadata-only',
                         'incomplete tool action','capture gap','escaped markup','cache reuse','session pruning']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = probe()
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
