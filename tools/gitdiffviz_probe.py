#!/usr/bin/env python3
"""Measure a user-built pinned gitdiffviz binary using disposable captured files."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labradour.exports import ArtifactCache
from labradour.gitdiffviz import SOURCE_REVISION, configuration, gitdiffviz_export
from labradour.recorder import Recorder
from labradour.review import SavedReview


def probe(binary):
    binary = binary.resolve()
    pin = hashlib.sha256(binary.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='labradour-gitdiffviz-probe-') as temporary:
        root = Path(temporary)
        workspace = root / 'workspace'
        workspace.mkdir()
        (workspace / 'source.c').write_text('int alpha;\n')
        (workspace / 'binary').write_bytes(b'\0old')
        (workspace / 'ungranted-private').write_text('never exported')
        recorder = Recorder(workspace, root / 'recording', backend='polling')
        recorder.capture('baseline')
        (workspace / 'source.c').write_text('int beta;\n')
        recorder.capture('edit')
        (workspace / 'source.c').write_text('int alpha;\n')
        recorder.capture('revert')
        (workspace / 'created.c').write_text('int created;\n')
        recorder.capture('create')
        (workspace / 'created.c').unlink()
        recorder.capture('delete')
        (workspace / 'empty').write_bytes(b'')
        recorder.capture('empty file')
        (workspace / 'binary').write_bytes(b'\0new')
        recorder.capture('binary edit')
        (workspace / 'source.c').chmod(0o755)
        recorder.capture('mode change')
        recorder.close()
        view = SavedReview(root / 'recording')
        config = root / 'adapter.json'
        config.write_text(json.dumps({'schema_version':1,'source_revision':SOURCE_REVISION,
                                     'binary':str(binary),'sha256':pin}))
        configuration(config)
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'recording').rglob('*') if p.is_file()}
        cases = []
        cache = ArtifactCache(root / 'cache')
        for row in view.activity.rows:
            if row['kind'] != 'effect' or row['payload']['before_commit'] is None or row['payload']['path'] == 'ungranted-private':
                continue
            started = time.monotonic()
            result = gitdiffviz_export(root / 'recording', view.activity.records, row, view.activity.revision, cache, config)
            document = json.loads(Path(result['path']).read_text())
            scene = document['evidence']['gitdiffviz']
            assert all(f['path'] == row['payload']['path'] for f in scene['diff']['files'])
            cases.append({'path':row['payload']['path'],'operation':row['payload']['operation'],
                          'binary':any(f.get('isBinary') for f in scene['diff']['files']),
                          'file_count':len(scene['diff']['files']), 'scene_nodes':len(scene['scene']['scene']['nodes']),
                          'artifact_bytes':result['bytes'], 'elapsed_ms':round((time.monotonic()-started)*1000,3),
                          'cached_repeat':gitdiffviz_export(root / 'recording', view.activity.records, row,
                              view.activity.revision, cache, config)['cached']})
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'recording').rglob('*') if p.is_file()}
        assert before == after
        return {'source_revision':SOURCE_REVISION, 'binary_sha256':pin, 'platform':platform.platform(),
                'python':platform.python_version(), 'license':'MIT (upstream; dependencies have their own licenses)',
                'scope':'real backend selected-file structural extraction and scene JSON; no browser/semantic/native Linux acceptance',
                'source_recording_unchanged':True, 'cases':cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = probe(args.binary)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
