import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from labradour.leases import evidence_lease
from labradour.recorder import Recorder, read_history
from labradour.retention import apply_retention
from labradour.review import SavedReview, load_session

PROJECT = Path(__file__).resolve().parents[1]


def contents(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.store = self.root / 'recording'
        for text in ('first', 'second'):
            (self.workspace / 'source.py').write_text(text + '\n')
            recorder = Recorder(self.workspace, self.store, backend='polling')
            recorder.capture('baseline')
            (self.workspace / 'source.py').write_text(text + ' edited\n')
            recorder.capture('edit')
            recorder.close()
        self.sessions = read_history(self.store)

    def test_review_is_read_only_without_workspace_or_runtime_services(self):
        before = contents(self.store)
        (self.workspace / 'source.py').unlink()
        self.workspace.rmdir()
        with patch('labradour.ui.PtyProcess', side_effect=AssertionError('launched')), \
                patch('labradour.recorder.Recorder', side_effect=AssertionError('recorded')):
            view = SavedReview(self.store)
            self.assertEqual(view.session, self.sessions[-1]['id'])
            self.assertTrue(view.activity.records)
            view.focus = 'agent'
            for kind, token in [('paste', b'execute this'), ('literal', b'\x1d'), ('key', b'X')]:
                view.handle(kind, token)
            self.assertIsNone(view.child)
            self.assertIn('no agent launched', view.session_lines()[0])
        self.assertEqual(contents(self.store), before)

    def test_background_session_switch_and_pruned_refresh(self):
        view = SavedReview(self.store)
        view.focus = 'agent'
        view.handle('key', b'k')
        view.handle('key', b'\r')
        def finish():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                view.collect()
                if not view.pending and not view.reload:
                    return
                time.sleep(.01)
            self.fail('review load did not finish')
        finish()
        self.assertEqual(view.session, self.sessions[0]['id'])
        apply_retention(self.store, 1)
        view.handle('key', b'r')
        finish()
        self.assertIn('pruned', view.notice)
        self.assertEqual(view.session, self.sessions[0]['id'])

    def test_cross_process_read_lease_blocks_prune_and_releases_on_exit(self):
        code = 'from labradour.retention import apply_retention; import sys; apply_retention(sys.argv[1], 1)'
        before = contents(self.store)
        with evidence_lease(self.store):
            result = subprocess.run([sys.executable, '-c', code, str(self.store)], cwd=PROJECT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'evidence busy', result.stderr)
        self.assertEqual(contents(self.store), before)
        apply_retention(self.store, 1)
        self.assertEqual(len(read_history(self.store)), 1)

    def test_reader_coexists_with_new_recorder_and_bad_session_fails(self):
        with evidence_lease(self.store):
            recorder = Recorder(self.workspace, self.store, backend='polling')
            recorder.capture('baseline')
            recorder.close()
        with self.assertRaisesRegex(ValueError, 'unknown'):
            load_session(self.store, 'unknown')

    def test_refresh_preserves_selection_filter_and_unclosed_status_is_not_repaired(self):
        recorder = Recorder(self.workspace, self.store, backend='polling')
        try:
            recorder.capture('baseline')
            before = contents(self.store)
            view = SavedReview(self.store, recorder.session)
            self.assertEqual(view.sessions[-1]['status'], 'running')
            self.assertEqual(view.activity.projection['session_status'], 'saved-unclosed')
            view.activity.set_filter('path:source.py')
            selected = view.activity.selected_id
            view.scroll = 3
            view.install(load_session(self.store, recorder.session))
            self.assertEqual(view.activity.filter, 'path:source.py')
            self.assertEqual(view.activity.selected_id, selected)
            self.assertEqual(view.scroll, 3)
            self.assertEqual(contents(self.store), before)
        finally:
            recorder.close()

    def test_comparison_pair_is_leased_and_pruned_bytes_are_unavailable(self):
        from labradour.visualizers import BuiltinRegistry, EvidenceReader
        view = SavedReview(self.store, self.sessions[0]['id'])
        row = next(r for r in view.activity.rows if r['kind'] == 'effect' and r['payload']['operation'] == 'file.modify')
        git = EvidenceReader.git
        checked = []
        def read(reader, *args):
            if not checked:
                code = 'from labradour.retention import apply_retention; import sys; apply_retention(sys.argv[1], 1)'
                result = subprocess.run([sys.executable, '-c', code, str(self.store)], cwd=PROJECT, capture_output=True)
                self.assertIn(b'evidence busy', result.stderr)
                checked.append(True)
            return git(reader, *args)
        with patch.object(EvidenceReader, 'git', read):
            self.assertIn('+first edited', '\n'.join(BuiltinRegistry().prepare(self.store, view.activity.records, row)[1]))
        apply_retention(self.store, 1)
        lines = '\n'.join(BuiltinRegistry().prepare(self.store, view.activity.records, row)[1])
        self.assertIn('unavailable', lines)
        self.assertNotIn('+first edited', lines)

    def test_saved_review_pty_navigation_resize_and_exit_without_writes(self):
        from labradour.pty_process import PtyProcess
        from labradour.terminal import Terminal
        before = contents(self.store)
        child = PtyProcess([sys.executable, '-m', 'labradour', 'review', str(self.store),
                            '--ignore-layout-config'], PROJECT, 40, 140, dict(os.environ, TERM='xterm-256color'))
        terminal = Terminal(40, 140)
        def wait_frame(text):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                replies = terminal.feed(child.read())
                if replies:
                    child.send(replies)
                if text in '\n'.join(terminal.display):
                    return
                time.sleep(.02)
            self.fail('missing ' + text + ': ' + '\n'.join(terminal.display))
        try:
            wait_frame('no agent launched')
            child.send(b'/path:source.py tool:file.modify\rfd')
            wait_frame('Source diff')
            wait_frame('+second edited')
            child.send(b'\x1dak\r')
            wait_frame('>* ' + self.sessions[0]['id'])
            child.send(b'\x1dl/path:source.py tool:file.modify\rfd')
            wait_frame('+first edited')
            child.resize(20, 70)
            terminal.resize(20, 70)
            child.send(b'\x1dl')
            wait_frame('terminal below 100x28')
            child.send(b'\x11')
            deadline = time.monotonic() + 4
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.02)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()
        self.assertEqual(contents(self.store), before)
