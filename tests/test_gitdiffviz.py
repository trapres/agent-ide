import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from labradour.exports import ArtifactCache, ExportError, ExportJob
from labradour.gitdiffviz import SOURCE_REVISION, configuration, gitdiffviz_export, run
from labradour.recorder import Recorder
from labradour.review import SavedReview

FIXTURE = '''
import json, pathlib, subprocess, sys
args = sys.argv
def arg(name): return args[args.index(name)+1]
out = pathlib.Path(arg('--out'))
if args[1] == 'extract-diff':
 repo = arg('--repo'); base = arg('--base'); target = arg('--target')
 paths = subprocess.check_output(['git','ls-tree','-r','--name-only',target],cwd=repo).decode().splitlines()
 if not paths: paths = subprocess.check_output(['git','ls-tree','-r','--name-only',base],cwd=repo).decode().splitlines()
 doc = {'version':1,'repoRoot':repo,'comparison':{'base':base,'target':target},'files':[{'path':p} for p in paths]}
else:
 diff = json.loads(pathlib.Path(arg('--diff')).read_text())
 paths = [f['path'] for f in diff['files']]
 doc = {'version':1,'repoRoot':diff['repoRoot'],'comparison':diff['comparison'],'scene':{'nodes':[{'path':p} for p in paths],'edges':[]}}
out.write_text(json.dumps(doc))
'''


class GitdiffvizTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        workspace = self.root / 'workspace'
        workspace.mkdir()
        self.store = self.root / 'recording'
        (workspace / 'source.c').write_text('int old_value;\n')
        (workspace / 'ungranted-secret').write_text('not for the exporter')
        recorder = Recorder(workspace, self.store, backend='polling')
        recorder.capture('baseline')
        (workspace / 'source.c').write_text('int new_value;\n')
        recorder.capture('edit')
        recorder.close()
        self.view = SavedReview(self.store)
        self.row = next(r for r in self.view.activity.rows if r['kind'] == 'effect' and r['payload']['operation'] == 'file.modify')
        self.cache = ArtifactCache(self.root / 'cache')
        self.binary = self.root / 'gitdiffviz'
        self.config = self.root / 'adapter.json'
        self.install(FIXTURE)

    def install(self, source):
        self.binary.write_text('#!' + sys.executable + '\n' + source)
        self.binary.chmod(0o700)
        self.config.write_text(json.dumps({'schema_version':1,'binary':str(self.binary),
            'source_revision':SOURCE_REVISION,'sha256':hashlib.sha256(self.binary.read_bytes()).hexdigest()}))

    def export(self, row=None):
        return gitdiffviz_export(self.store, self.view.activity.records, row or self.row,
                                self.view.activity.revision, self.cache, self.config)

    def test_scoped_pair_structured_scene_provenance_and_verified_cache(self):
        before = {str(p):p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        result = self.export()
        document = json.loads(Path(result['path']).read_text())
        evidence = document['evidence']
        self.assertEqual([f['path'] for f in evidence['gitdiffviz']['diff']['files']], ['source.c'])
        self.assertEqual(base64.b64decode(evidence['after']['content_base64']), b'int new_value;\n')
        provenance = document['provenance']
        self.assertEqual(provenance['options']['source_revision'], SOURCE_REVISION)
        self.assertEqual(provenance['options']['scope'], 'selected-file-only')
        self.assertNotEqual(provenance['synthetic_comparison']['target'], self.row['payload']['checkpoint'])
        self.assertEqual(evidence['gitdiffviz']['scene']['repoRoot'], 'captured-effect')
        with patch('labradour.gitdiffviz.run', side_effect=AssertionError('cached export reran backend')):
            self.assertTrue(self.export()['cached'])
        self.assertEqual({str(p):p.read_bytes() for p in self.store.rglob('*') if p.is_file()}, before)

    def test_disabled_invalid_config_and_binary_tampering_fail_before_execution(self):
        with self.assertRaisesRegex(ExportError, 'disabled'):
            configuration(None)
        self.binary.write_text(self.binary.read_text() + '\n#changed')
        with self.assertRaisesRegex(ExportError, 'SHA-256'):
            self.export()
        self.assertFalse(self.cache.path.exists())
        for raw in ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}', 'x' * 4097):
            self.config.write_text(raw)
            with self.assertRaises(ExportError):
                configuration(self.config)

    def test_bad_scope_missing_evidence_symlink_and_unusual_path_use_fallback(self):
        row = dict(self.row, kind='action')
        with self.assertRaisesRegex(ExportError, 'file effect'):
            self.export(row)
        with patch('labradour.visualizers.EvidenceReader.git', side_effect=ValueError('missing')):
            with self.assertRaisesRegex(ExportError, 'complete'):
                self.export()
        from labradour.visualizers import EvidenceReader
        for before, after, path in [({'state':'unavailable'},{'state':'available','mode':'100644'},'source.c'),
                                   ({'state':'absent'},{'state':'available','mode':'120000'},'source.c'),
                                   ({'state':'absent'},{'state':'available','mode':'100644','bytes':b'ok'},'.GIT/config')]:
            row = dict(self.row, payload=dict(self.row['payload'], path=path))
            with patch.object(EvidenceReader, 'effect', return_value=(before, after)):
                with self.assertRaises(ExportError):
                    self.export(row)
        self.assertFalse(self.cache.path.exists())

    def test_malformed_or_widened_backend_output_never_publishes(self):
        for source in ["import sys; print('PRIVATE LOG',file=sys.stderr); sys.exit(7)",
                       FIXTURE.replace("out.write_text(json.dumps(doc))", "out.write_text('{invalid')"),
                       FIXTURE.replace("'path':p", "'path':'ungranted'"),
                       FIXTURE.replace("'version':1", "'version':99")]:
            self.install(source)
            with self.assertRaises(ExportError) as error:
                self.export()
            self.assertNotIn('PRIVATE LOG', str(error.exception))
            self.assertFalse(self.cache.path.exists())

    def test_pipe_limits_cancellation_and_temporary_limits_cleanup_process(self):
        env = dict(os.environ)
        check = lambda: None
        with self.assertRaisesRegex(ExportError, 'output exceeds'):
            run([sys.executable,'-c','import sys; sys.stdout.write("x"*2000000)'], self.root, env, check, self.root)
        deadline = time.monotonic() + .15
        def cancelled():
            if time.monotonic() > deadline:
                raise ExportError('cancelled')
        started = time.monotonic()
        with self.assertRaisesRegex(ExportError, 'cancelled'):
            run([sys.executable,'-c','import os,time; os.fork(); time.sleep(20)'], self.root, env, cancelled, self.root)
        self.assertLess(time.monotonic()-started, 2)
        with patch('labradour.gitdiffviz.TEMP_LIMIT', 1):
            with self.assertRaisesRegex(ExportError, 'temporary output'):
                self.export()

    def test_failed_optional_export_does_not_disable_json_or_selection(self):
        self.view.activity.selected_id = self.row['id']
        self.view.export_job = ExportJob(self.cache, gitdiffviz_config=self.config)
        self.install('import sys; sys.exit(7)')
        self.view.handle('command', b'g')
        def finish():
            deadline = time.monotonic() + 4
            while self.view.export_job.pending and time.monotonic() < deadline:
                self.view.export_job.poll()
                time.sleep(.01)
            self.assertFalse(self.view.export_job.pending)
        finish()
        self.assertIn('failed', self.view.export_job.message)
        self.assertEqual(self.view.activity.selected_id, self.row['id'])
        self.view.handle('command', b'x')
        finish()
        self.assertIsNotNone(self.view.export_job.result)

    def test_valid_json_with_corrupt_cache_envelope_rebuilds_current_payload(self):
        result = self.export()
        path = Path(result['path'])
        value = json.loads(path.read_text())
        value['schema_version'] = 99
        path.write_text(json.dumps(value))
        rebuilt = self.export()
        self.assertFalse(rebuilt['cached'])
        self.assertEqual(json.loads(path.read_text())['schema_version'], 1)

    def test_binary_snapshot_detects_change_and_cli_routes_optional_export(self):
        config = configuration(self.config)
        self.binary.write_text(self.binary.read_text() + '\n#tampered')
        with patch('labradour.gitdiffviz.configuration', return_value=config):
            with self.assertRaisesRegex(ExportError, 'changed'):
                self.export()
        self.install(FIXTURE)
        result = json.loads(subprocess.check_output([sys.executable, '-m', 'labradour', 'export',
            str(self.store), '--session', self.view.session, '--row', self.row['id'],
            '--exporter', 'gitdiffviz', '--gitdiffviz-config', str(self.config),
            '--export-cache', str(self.cache.path)]))
        document = json.loads(Path(result['path']).read_text())
        self.assertEqual(document['provenance']['exporter'], 'labradour-gitdiffviz')

    def test_rendered_optional_export_preserves_saved_review_and_reports_ready(self):
        from labradour.pty_process import PtyProcess
        from labradour.terminal import Terminal
        child = PtyProcess([sys.executable, '-m', 'labradour', 'review', str(self.store),
            '--ignore-layout-config', '--gitdiffviz-config', str(self.config), '--export-cache', str(self.cache.path)],
            Path(__file__).resolve().parents[1], 40, 140, dict(os.environ, TERM='xterm-256color'))
        terminal = Terminal(40, 140)
        def wait_frame(text):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                replies = terminal.feed(child.read())
                if replies:
                    child.send(replies)
                if text in '\n'.join(terminal.display):
                    return
                time.sleep(.01)
            self.fail('missing ' + text + ': ' + '\n'.join(terminal.display))
        try:
            wait_frame('SAVED REVIEW')
            child.send(b'/path:source.c tool:file.modify\rfd')
            wait_frame('Source diff')
            child.send(b'\x1dg\x1dt')
            wait_frame('Export ready')
            child.send(b's')
            wait_frame('Source diff')
            child.send(b'\x11')
            deadline = time.monotonic() + 4
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.01)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()
