import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from labradour.exports import ArtifactCache, ExportError, ExportJob, evidence_export
from labradour.recorder import Recorder, read_history
from labradour.review import SavedReview

PROJECT = Path(__file__).resolve().parents[1]


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.store = self.root / 'recording'
        self.cache = ArtifactCache(self.root / 'cache')
        (self.workspace / 'source.py').write_text('alpha\n')
        recorder = Recorder(self.workspace, self.store, backend='polling')
        recorder.capture('baseline')
        (self.workspace / 'source.py').write_text('beta\n')
        recorder.capture('edit')
        recorder.close()
        self.view = SavedReview(self.store, export_cache=self.cache.path)
        self.row = next(r for r in self.view.activity.rows if r['kind'] == 'effect' and r['payload']['operation'] == 'file.modify')

    def export(self, row=None, revision=None):
        return evidence_export(self.store, self.view.activity.records, row or self.row,
                               revision or self.view.activity.revision, self.cache)

    def test_explicit_scoped_saved_bytes_provenance_hash_and_cache_reuse(self):
        before = {str(p): p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        (self.workspace / 'source.py').write_text('current live bytes\n')
        self.assertFalse(self.cache.path.exists())
        result = self.export()
        document = json.loads(Path(result['path']).read_text())
        self.assertEqual(base64.b64decode(document['evidence']['before']['content_base64']), b'alpha\n')
        self.assertEqual(base64.b64decode(document['evidence']['after']['content_base64']), b'beta\n')
        self.assertEqual(document['provenance']['selection_id'], self.row['id'])
        self.assertEqual(document['provenance']['session_id'], self.view.session)
        self.assertEqual(document['provenance']['file_attribution'], 'external-or-unknown')
        self.assertEqual(result['sha256'], hashlib.sha256(Path(result['path']).read_bytes()).hexdigest())
        self.assertEqual(self.cache.path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(result['path']).stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.export()['cached'])
        self.assertNotEqual(self.export(revision=(999, 999, 'completed'))['id'], result['id'])
        self.assertEqual({str(p): p.read_bytes() for p in self.store.rglob('*') if p.is_file()}, before)

    def test_unavailable_states_do_not_become_empty_bytes_and_bad_grants_fail(self):
        with patch('labradour.visualizers.EvidenceReader.git', side_effect=ValueError('missing')):
            document = json.loads(Path(self.export()['path']).read_text())
        self.assertEqual(document['evidence']['after']['state'], 'unavailable')
        self.assertNotIn('content_base64', document['evidence']['after'])
        row = dict(self.row, payload=dict(self.row['payload'], path='not-granted'))
        with self.assertRaisesRegex(ValueError, 'not granted'):
            self.export(row)

    def test_count_age_byte_limits_and_abandoned_partial_cleanup(self):
        cache = ArtifactCache(self.root / 'small', artifact_limit=1000, budget=1200, count=2, age=60)
        for i in range(4):
            cache.publish({'provenance': {'index': i}, 'evidence': 'x' * 400})
            self.assertLessEqual(cache.inspect()['bytes'], 1200)
            self.assertLessEqual(len(cache.inspect()['artifacts']), 2)
        path = next(cache.path.glob('*.json'))
        os.utime(path, (time.time() - 120, time.time() - 120))
        pending = cache.path / ('.pending-' + 'a' * 32 + '.tmp')
        pending.write_text('partial')
        pending.chmod(0o600)
        cache.publish({'provenance': {}, 'evidence': 'new'})
        self.assertFalse(pending.exists())
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ExportError, 'byte limit'):
            cache.publish({'provenance': {}, 'evidence': 'x' * 2000})
        self.assertFalse(list(cache.path.glob('.pending-*')))
        self.assertLessEqual(cache.inspect()['bytes'], 1200)

    def test_cancellation_and_failed_publication_remove_unfinished_files(self):
        def cancel():
            if self.cache.path.exists() and list(self.cache.path.glob('.pending-*')):
                raise ExportError('export cancelled')
        with self.assertRaises(ExportError):
            self.cache.publish({'provenance': {}, 'evidence': 'large' * 100000}, cancel)
        if self.cache.path.exists():
            self.assertEqual(list(self.cache.path.iterdir()), [])
        with patch('labradour.exports.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.export()
        self.assertEqual(list(self.cache.path.iterdir()), [])

    def test_corrupt_cache_is_rebuilt_and_unsafe_paths_are_refused(self):
        result = self.export()
        Path(result['path']).write_text('damaged')
        self.assertFalse(self.export()['cached'])
        for path in (self.store / 'exports', self.workspace / 'exports', self.root):
            with self.assertRaises(ExportError):
                ArtifactCache(path, protected=[self.workspace, self.store])
        link = self.root / 'linked'
        link.symlink_to(self.cache.path)
        with self.assertRaises(ExportError):
            ArtifactCache(link)
        foreign = self.cache.path / 'keep-me'
        foreign.write_text('foreign')
        foreign.chmod(0o600)
        with self.assertRaisesRegex(ExportError, 'unrecognized'):
            self.export()
        self.assertEqual(foreign.read_text(), 'foreign')

    def test_job_is_nonblocking_single_scope_and_cancellable_without_join(self):
        gate = threading.Event()
        selected = []
        def slow(directory, records, row, revision, cache, check):
            selected.append(row['id'])
            gate.wait(2)
            check()
            return {'cached': False, 'path': 'finished'}
        job = ExportJob(self.cache)
        with patch('labradour.exports.evidence_export', side_effect=slow):
            started = time.monotonic()
            self.assertTrue(job.start(self.store, self.view.activity.records, self.row, 'rev'))
            self.assertLess(time.monotonic() - started, .1)
            self.assertFalse(job.start(self.store, [], dict(self.row, id='other'), 'rev'))
            job.cancel()
            self.assertTrue(job.pending)
            gate.set()
            deadline = time.monotonic() + 3
            while job.pending and time.monotonic() < deadline:
                job.poll()
                time.sleep(.01)
        self.assertEqual(selected, [self.row['id']])
        self.assertIn('cancelled', job.message)
        self.assertIsNone(job.result)
        self.assertFalse(self.cache.path.exists())

    def test_deadline_and_cache_contention_fail_locally(self):
        def slow(directory, records, row, revision, cache, check):
            time.sleep(.05)
            check()
        job = ExportJob(self.cache)
        with patch('labradour.exports.TIMEOUT', .01), patch('labradour.exports.evidence_export', side_effect=slow):
            job.start(self.store, self.view.activity.records, self.row, 'rev')
            deadline = time.monotonic() + 2
            while job.pending and time.monotonic() < deadline:
                job.poll()
                time.sleep(.01)
        self.assertIn('deadline', job.message)
        self.assertFalse(self.cache.path.exists())
        with self.cache.locked():
            code = 'from labradour.exports import ArtifactCache; import sys; ArtifactCache(sys.argv[1]).publish({"provenance":{},"evidence":{}})'
            result = subprocess.run([sys.executable, '-c', code, str(self.cache.path)], cwd=PROJECT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'cache busy', result.stderr)
        self.assertFalse(list(self.cache.path.glob('*.json')))

    def test_action_export_includes_only_linked_observations_and_never_reruns(self):
        record = self.view.activity.records[0]
        row = {'id': 'action:call', 'kind': 'action', 'payload': {'observations': [record['event_id']],
                'tool': 'Bash', 'result_outcomes': ['unknown'], 'command': 'DO NOT EXECUTE'}}
        with patch('labradour.exports.EvidenceReader', side_effect=AssertionError('file scope widened')):
            document = json.loads(Path(self.export(row)['path']).read_text())
        self.assertEqual([r['event_id'] for r in document['evidence']['observations']], [record['event_id']])
        self.assertNotIn('after', document['evidence'])
        self.assertEqual(document['evidence']['selection']['payload']['result_outcomes'], ['unknown'])

    def test_ui_explicit_action_preserves_focus_selection_and_reports_status(self):
        self.view.activity.selected_id = self.row['id']
        self.view.focus = 'agent'
        self.assertFalse(self.cache.path.exists())
        self.view.handle('command', b'x')
        self.assertEqual(self.view.focus, 'agent')
        self.assertEqual(self.view.activity.selected_id, self.row['id'])
        deadline = time.monotonic() + 3
        while self.view.export_job.pending and time.monotonic() < deadline:
            self.view.export_job.poll()
            time.sleep(.01)
        self.assertIsNotNone(self.view.export_job.result)
        self.view.handle('command', b't')
        self.assertTrue(self.view.export_view)
        self.assertEqual(self.view.focus, 'visualization')

    def test_cli_enumerates_exports_and_explicit_cache_clear(self):
        command = [sys.executable, '-m', 'labradour', 'export', str(self.store), '--session', self.view.session]
        rows = json.loads(subprocess.check_output(command + ['--row', 'list'], cwd=PROJECT))
        self.assertIn(self.row['id'], [r['id'] for r in rows])
        result = json.loads(subprocess.check_output(command + ['--row', self.row['id'], '--export-cache', str(self.cache.path)], cwd=PROJECT))
        self.assertTrue(Path(result['path']).is_file())
        inspect = [sys.executable, '-m', 'labradour', 'export-cache', '--export-cache', str(self.cache.path)]
        self.assertEqual(len(json.loads(subprocess.check_output(inspect, cwd=PROJECT))['artifacts']), 1)
        self.assertEqual(json.loads(subprocess.check_output(inspect + ['--clear'], cwd=PROJECT))['bytes'], 0)

    def test_rendered_live_export_preserves_native_input_and_historical_scope(self):
        from labradour.pty_process import PtyProcess
        from labradour.terminal import Terminal
        code = '''
import curses, sys, time
from pathlib import Path
import labradour.exports as exports
from labradour.ui import Harness
original = exports.evidence_export
def slow(*args, **kwargs):
 time.sleep(.5)
 return original(*args, **kwargs)
exports.evidence_export = slow
agent = "import sys; from pathlib import Path; print('READY', flush=True);\\nfor line in sys.stdin: Path('source.py').write_text(line); print('ACK-'+line.strip(), flush=True)"
curses.wrapper(Harness([sys.executable, '-u', '-c', agent], Path(sys.argv[1]), Path(sys.argv[2]), recording=Path(sys.argv[3]), watch_backend='polling', export_cache=Path(sys.argv[4])).run)
'''
        events = self.root / 'events'
        events.mkdir()
        live_store = self.root / 'live-recording'
        child = PtyProcess([sys.executable, '-c', code, str(self.workspace), str(events), str(live_store), str(self.cache.path)],
                           PROJECT, 40, 140, dict(os.environ, TERM='xterm-256color'))
        terminal = Terminal(40, 140)
        def wait_frame(text, timeout=6):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                replies = terminal.feed(child.read())
                if replies:
                    child.send(replies)
                if text in '\n'.join(terminal.display):
                    return
                time.sleep(.01)
            self.fail('missing ' + text + ': ' + '\n'.join(terminal.display))
        try:
            wait_frame('READY')
            child.send(b'captured-before-export\r')
            wait_frame('file.modify')
            child.send(b'\x1dl/path:source.py tool:file.modify\rfd')
            wait_frame('+captured-before-export')
            child.send(b'\x1dx\x1dalater-native-input\r')
            wait_frame('ACK-later-native-input', timeout=.4)
            child.send(b'\x1dt')
            wait_frame('Export ready')
            artifact = json.loads(next(self.cache.path.glob('*.json')).read_text())
            self.assertEqual(base64.b64decode(artifact['evidence']['after']['content_base64']), b'captured-before-export\n')
            child.send(b'\x11')
            deadline = time.monotonic() + 4
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.02)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()

    def test_saved_review_quit_does_not_wait_for_pending_export(self):
        from labradour.pty_process import PtyProcess
        from labradour.terminal import Terminal
        code = '''
import curses, sys, time
import labradour.exports as exports
from labradour.review import SavedReview
original = exports.evidence_export
def slow(*args, **kwargs):
 time.sleep(5)
 return original(*args, **kwargs)
exports.evidence_export = slow
curses.wrapper(SavedReview(sys.argv[1], export_cache=sys.argv[2]).run)
'''
        child = PtyProcess([sys.executable, '-c', code, str(self.store), str(self.cache.path)],
                           PROJECT, 40, 140, dict(os.environ, TERM='xterm-256color'))
        terminal = Terminal(40, 140)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                terminal.feed(child.read())
                if 'SAVED REVIEW' in '\n'.join(terminal.display):
                    break
                time.sleep(.01)
            else:
                self.fail('saved review did not start')
            child.send(b'\x1dx\x1dt')
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                terminal.feed(child.read())
                if 'Export running' in '\n'.join(terminal.display):
                    break
                time.sleep(.01)
            else:
                self.fail('export did not start')
            started = time.monotonic()
            child.send(b'\x11')
            while child.poll() is None and time.monotonic() - started < 2:
                child.read()
                time.sleep(.01)
            self.assertEqual(child.poll(), 0)
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(self.cache.path.exists())
        finally:
            child.close()
