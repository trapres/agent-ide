import base64
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from labradour.companions import open_graphical
from labradour.exports import ExportError, session_export
from labradour.leases import EvidenceBusy, evidence_lease
from labradour.recorder import Recorder
from labradour.retention import apply_retention
from tests import test_exports
PROJECT = test_exports.PROJECT


class SessionExportTests(unittest.TestCase):
    def setUp(self):
        test_exports.ExportTests.setUp(self)

    def export(self, check=lambda: None):
        return session_export(self.store, self.view.session, self.cache, check)

    def test_complete_durable_scope_cache_and_no_workspace_reads(self):
        originals = {str(p): p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        (self.workspace / 'source.py').write_text('today is different')
        result = self.export()
        doc = json.loads(Path(result['path']).read_text())
        self.assertEqual(doc['evidence']['selection']['kind'], 'session')
        self.assertEqual(doc['evidence']['journal'], self.view.activity.records)
        self.assertEqual(doc['evidence']['projection'], self.view.activity.projection)
        effects = doc['evidence']['projection']['effects']
        self.assertEqual(len(doc['evidence']['file_pairs']), len(effects))
        modified = next(e for e in effects if e['operation'] == 'file.modify')
        pair = next(p for p in doc['evidence']['file_pairs'] if p['effect_id'] == modified['id'])
        self.assertEqual(base64.b64decode(pair['before']['content_base64']), b'alpha\n')
        self.assertEqual(base64.b64decode(pair['after']['content_base64']), b'beta\n')
        self.assertTrue(self.export()['cached'])
        self.assertEqual(originals, {str(p): p.read_bytes() for p in self.store.rglob('*') if p.is_file()})

    def test_session_html_has_index_pairs_provenance_and_no_automatic_open(self):
        result = self.export()
        self.assertFalse(list(self.cache.path.glob('*.html')))
        with patch('labradour.companions.launch') as opener:
            ready = open_graphical(self.cache, result)
        opener.assert_called_once()
        text = Path(ready['companion']['path']).read_text()
        self.assertIn('Captured intervals: listed per effect', text)
        for expected in ('Session activity', 'Captured effects', '<details>', 'alpha', 'beta',
                         self.view.session, 'Terminal output was not recorded', 'Recorded gaps'):
            self.assertIn(expected, text)

    def test_limits_cancellation_and_publication_fail_without_partial_archive(self):
        for name in ('SESSION_RECORD_LIMIT', 'SESSION_EFFECT_LIMIT', 'SESSION_CONTENT_LIMIT'):
            with patch('labradour.exports.' + name, 1), self.assertRaisesRegex(ExportError, 'limit'):
                self.export()
            self.assertFalse(list(self.cache.path.glob('*.json')))
        def cancelled():
            raise ExportError('cancelled')
        with self.assertRaisesRegex(ExportError, 'cancelled'):
            self.export(cancelled)
        with patch('labradour.exports.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.export()
        self.assertFalse(list(self.cache.path.glob('.pending-*')))
        self.assertFalse(list(self.cache.path.glob('*.json')))

    def test_retention_blocks_during_read_and_runs_before_publication(self):
        from labradour.visualizers import EvidenceReader
        original = EvidenceReader.effect
        lease_checks = []
        def leased(reader, effect):
            def writer():
                try:
                    with evidence_lease(self.store, exclusive=True):
                        lease_checks.append('incorrectly acquired')
                except EvidenceBusy:
                    lease_checks.append('busy')
            worker = threading.Thread(target=writer)
            worker.start()
            worker.join()
            return original(reader, effect)
        publish = self.cache.publish
        def unlocked(*args, **kwargs):
            with evidence_lease(self.store, exclusive=True):
                pass
            return publish(*args, **kwargs)
        with patch.object(EvidenceReader, 'effect', leased), patch.object(self.cache, 'publish', unlocked):
            self.export()
        self.assertTrue(lease_checks)
        self.assertEqual(set(lease_checks), {'busy'})

    def test_export_survives_session_prune_without_pin(self):
        result = self.export()
        recorder = Recorder(self.workspace, self.store, backend='polling')
        recorder.capture('second session')
        recorder.close()
        apply_retention(self.store, 1)
        with self.assertRaisesRegex(ValueError, 'unknown|pruned'):
            self.export()
        with patch('labradour.companions.launch'):
            ready = open_graphical(self.cache, result)
        self.assertTrue(Path(ready['companion']['path']).exists())
        self.assertEqual(ready['session_id'], self.view.session)

    def test_saved_ui_freezes_requested_session_across_switch(self):
        requested = self.view.session
        selected, focus = self.view.activity.selected_id, self.view.focus
        original = session_export
        started, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(3)
            return original(*args, **kwargs)
        with patch('labradour.exports.session_export', delayed):
            self.view.handle('command', b'u')
            self.assertTrue(started.wait(1))
            self.assertEqual((self.view.activity.selected_id, self.view.focus), (selected, focus))
            self.view.session = 'a-later-selected-session'
            release.set()
            deadline = time.monotonic() + 3
            while self.view.export_job.pending and time.monotonic() < deadline:
                self.view.export_job.poll()
                time.sleep(.01)
        self.assertEqual(self.view.export_job.result['session_id'], requested)
        self.assertEqual(self.view.session, 'a-later-selected-session')

    def test_cli_scope_and_one_effect_adapter_rejection(self):
        command = [sys.executable, '-m', 'labradour', 'export', str(self.store),
                   '--session', self.view.session, '--whole-session', '--export-cache', str(self.cache.path)]
        result = json.loads(subprocess.check_output(command, cwd=PROJECT))
        self.assertEqual(result['selection_id'], 'session:' + self.view.session)
        rejected = subprocess.run(command + ['--exporter', 'gitdiffviz'], cwd=PROJECT, capture_output=True)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn(b'one effect', rejected.stderr)

    def test_missing_historical_objects_remain_explicit_and_no_current_fallback(self):
        from labradour.visualizers import EvidenceReader
        with patch.object(EvidenceReader, 'git', side_effect=ValueError('pruned')):
            result = self.export()
        doc = json.loads(Path(result['path']).read_text())
        for pair in doc['evidence']['file_pairs']:
            self.assertEqual(pair['after']['state'], 'unavailable')
            self.assertNotIn('content_base64', pair['after'])

    def test_unclosed_session_keeps_unknown_action_gaps_and_policy_omissions(self):
        from labradour.adapters.providers import normalize
        from labradour.policy import CapturePolicy
        recorder = Recorder(self.workspace, self.store, backend='polling',
                            policy=CapturePolicy(metadata_only=('source.py',)))
        try:
            recorder.capture('metadata baseline')
            fact = normalize('claude', {'hook_event_name':'PreToolUse', 'session_id':'fixture',
                             'tool_use_id':'unknown', 'tool_name':'Bash', 'tool_input':{'command':'DO-NOT-RUN'}})
            recorder.append('adapter.event', fact, source='fixture')
            recorder.append('collector.overflow', {'reason':'fixture gap'}, source='fixture')
            result = session_export(self.store, recorder.session, self.cache)
            doc = json.loads(Path(result['path']).read_text())
            self.assertEqual(doc['evidence']['selection']['payload']['status'], 'saved-unclosed')
            self.assertTrue(doc['evidence']['projection']['gaps'])
            self.assertNotEqual(doc['evidence']['projection']['actions'][0]['state'], 'completed')
            self.assertFalse(doc['evidence']['file_pairs'])
            self.assertIn('policy.metadata_only', json.dumps(doc['evidence']['journal']))
        finally:
            recorder.close()

    def test_cancelled_open_preserves_session_source_and_live_shortcut_is_refused(self):
        job = self.view.export_job
        job.result = self.export()
        original_id = job.result['id']
        from labradour.companions import render
        started, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(3)
            return render(*args, **kwargs)
        with patch('labradour.companions.render', delayed), patch('labradour.companions.launch') as opener:
            self.assertTrue(job.open_graphical())
            self.assertTrue(started.wait(1))
            job.cancel()
            release.set()
            deadline = time.monotonic() + 3
            while job.pending and time.monotonic() < deadline:
                job.poll()
                time.sleep(.01)
            opener.assert_not_called()
        self.assertEqual(job.result['id'], original_id)
        self.assertIn('cancelled', job.message)
        self.view.saved_review = False
        self.view.handle('command', b'u')
        self.assertFalse(job.pending)
        self.assertIn('saved review', job.message)
