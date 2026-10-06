import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import sys

from labradour.layout import pane_order, preset, reflect, tree_dict
from labradour.layout_config import load_layout
from labradour.layout_editor import LayoutEditor, edit_tree
from labradour.pty_process import PtyProcess
from labradour.recorder import read_history
from labradour.terminal import Terminal
from labradour.ui import Harness

ROOT = Path(__file__).resolve().parents[1]


def finished(editor):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = editor.poll()
        if not editor.pending:
            return result
        time.sleep(.01)
    raise AssertionError('layout job did not finish')


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = load_layout(self.root, user_file=self.root / 'user/layout.json')
        self.editor = LayoutEditor(self.config)

    def test_registered_operations_and_invalid_changes_are_atomic(self):
        tree = preset()
        swapped = self.editor.execute('layout swap agent activity', tree)
        self.assertEqual(pane_order(swapped), ('activity', 'agent', 'visualization'))
        flipped = self.editor.execute('layout flip review', tree)
        self.assertEqual(pane_order(flipped), ('agent', 'visualization', 'activity'))
        self.assertEqual(self.editor.execute('layout axis main rows', tree).axis, 'rows')
        self.assertEqual(self.editor.execute('layout ratio main 6500', tree).ratio_bps, 6500)
        self.assertEqual(self.editor.execute('layout use agent-top', tree), preset('agent-top'))
        for command in ('rm -rf anything', 'layout flip missing', 'layout ratio main 9999',
                        'layout axis main sideways', 'layout swap agent nope',
                        'layout ratio main true', 'layout save', 'layout reset nope'):
            self.assertIsNone(self.editor.execute(command, tree))
            self.assertIn('Layout error', self.editor.message)
        self.assertEqual(tree, preset())

    def test_preview_apply_cancel_and_reset_original_overridden_builtin(self):
        tree = preset()
        self.editor.execute('layout preview', tree)
        self.assertIsNone(self.editor.execute('layout use agent-top', tree))
        self.assertEqual(self.editor.preview, preset('agent-top'))
        self.assertIsNone(self.editor.execute('layout save draft workspace', tree))
        self.assertFalse(self.editor.pending)
        self.assertEqual(self.editor.execute('layout apply', tree), preset('agent-top'))
        self.assertIsNone(self.editor.preview)
        self.config.layouts['default'] = preset('agent-right')
        self.assertEqual(self.editor.execute('layout reset default', tree), preset())
        self.editor.execute('layout preview', tree)
        self.editor.execute('layout flip main', tree)
        self.editor.input('key', b'\x1b', tree)
        self.assertIsNone(self.editor.preview)
        self.assertFalse(self.editor.open)
        self.assertFalse((self.root / '.labradour').exists())

    def test_save_live_tree_reload_discard_and_file_conflict(self):
        current = preset('agent-top')
        self.editor.execute('layout save work workspace', current)
        result = finished(self.editor)
        self.assertIsNone(result[1])  # Save updates baseline, never changes live tree.
        self.assertEqual(self.editor.config.tree, current)
        disk = load_layout(self.root, user_file=self.config.paths['user'])
        self.assertEqual(disk.active_layout, 'work')
        self.assertEqual(disk.tree, current)
        self.editor.execute('layout reload', reflect(current))
        self.assertFalse(self.editor.pending)
        self.assertIn('--discard', self.editor.message)
        self.editor.execute('layout reload --discard', reflect(current))
        self.assertEqual(finished(self.editor)[1], current)
        path = self.root / '.labradour/layout.json'
        path.write_text('{bad')
        self.editor.execute('layout save conflict workspace', current)
        self.assertIsNone(finished(self.editor))
        self.assertIn('changed', self.editor.message)
        self.assertEqual(self.editor.config.tree, current)
        self.assertEqual(path.read_text(), '{bad')

    def test_overwrite_confirmation_and_failed_reload_preserve_tree(self):
        self.editor.execute('layout save default workspace', preset())
        finished(self.editor)
        self.assertIn('--confirm-shadow', self.editor.message)
        self.editor.execute('layout save default workspace --confirm-shadow', preset())
        self.assertIsNotNone(finished(self.editor))
        explicit = self.root / 'explicit.json'
        explicit.write_text(json.dumps({'schema_version': 1}))
        editor = LayoutEditor(load_layout(self.root, explicit=explicit, user_file=self.config.paths['user']))
        explicit.write_text('{bad')
        editor.execute('layout reload', preset())
        self.assertIsNone(finished(editor))
        self.assertEqual(editor.config.tree, preset())
        self.assertIn('Layout error', editor.message)

    def test_slow_disk_job_does_not_block_agent_or_allow_layout_race(self):
        gate = threading.Event()
        with patch('labradour.layout_editor.load_layout', side_effect=lambda **_: (gate.wait(2), self.config)[1]):
            self.editor.execute('layout reload', preset())
            harness = Harness([], self.root, self.root, layout_config=self.config, layout_tree=preset())
            harness.layout_editor = self.editor
            harness.child = Mock()
            harness.terminal = Terminal(36, 68)
            harness.handle('key', b'x')
            harness.child.send.assert_called_once_with(b'x')
            harness.handle('command', b'm')
            self.assertEqual(harness.layout_tree, preset())
            self.assertTrue(self.editor.pending)
            gate.set()
            self.assertIsNotNone(finished(self.editor))

    def test_overlay_input_paste_controls_and_global_quit_do_not_leak(self):
        harness = Harness([], self.root, self.root, layout_config=self.config, layout_tree=preset())
        harness.child = Mock()
        harness.terminal = Terminal(36, 68)
        harness.handle('command', b':')
        for kind, token in harness.router.feed(b'\x1b[200~layout use agent-top\n\x11\x1dm\x1b[201~'):
            harness.handle(kind, token)
        self.assertEqual(harness.layout_tree, preset())
        self.assertTrue(harness.running)
        self.assertEqual(harness.layout_editor.text, 'layout use agent-topm')
        harness.handle('key', b'\r')  # Invalid pasted suffix is an error, not shell/native input.
        harness.handle('literal', b'\x1d')
        harness.child.send.assert_not_called()
        harness.handle('key', b'\x1b')
        self.assertFalse(harness.layout_editor.open)
        harness.handle('key', b'x')
        harness.child.send.assert_called_once_with(b'x')
        harness.handle('command', b':')
        harness.handle('command', b'q')
        self.assertFalse(harness.running)

    def test_applied_edit_preserves_focus_selection_terminal_and_recorder(self):
        harness = Harness([], self.root, self.root, layout_config=self.config, layout_tree=preset())
        harness.focus, harness.follow, harness.selected, harness.scroll = 'activity', False, 1, 5
        harness.actions = [{'payload': {}}, {'payload': {'old': True}}]
        harness.child, harness.terminal, harness.recorder = Mock(), Terminal(36, 68), Mock()
        identities = (harness.child, harness.terminal, harness.recorder, harness.actions)
        harness.handle('command', b':')
        for kind, token in harness.router.feed(b'layout use agent-top\r'):
            harness.handle(kind, token)
        self.assertEqual(harness.layout_tree, preset('agent-top'))
        self.assertEqual((harness.focus, harness.follow, harness.selected, harness.scroll), ('activity', False, 1, 5))
        self.assertEqual(identities, (harness.child, harness.terminal, harness.recorder, harness.actions))
        harness.child.resize.side_effect = OSError('fixture resize failure')
        harness.sync_agent_size({'agent': __import__('labradour.layout', fromlist=['Rect']).Rect(1, 0, 19, 140)})
        self.assertEqual((harness.terminal.rows, harness.terminal.columns), (36, 68))
        self.assertIn('previous PTY size retained', harness.notice)


class EditorPtyTests(unittest.TestCase):
    def test_live_recording_preview_cancel_apply_save_reload_and_compact_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / 'workspace'; workspace.mkdir()
            recording = root / 'recording'
            child = PtyProcess([sys.executable, '-m', 'labradour', 'run', '--demo',
                '--workspace', str(workspace), '--record', str(recording)], ROOT, 40, 140,
                dict(os.environ, XDG_CONFIG_HOME=str(root / 'xdg')))
            terminal = Terminal(40, 140)
            def wait_frame(predicate):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    replies = terminal.feed(child.read())
                    if replies: child.send(replies)
                    if predicate(terminal.display): return
                    time.sleep(.02)
                self.fail('missing frame: ' + '\n'.join(terminal.display))
            def menu(command):
                child.send(b'\x1d:' + command.encode() + b'\r')
            def escape():
                child.send(b'\x1b')
                time.sleep(.08)
            try:
                wait_frame(lambda rows: 'fake>' in '\n'.join(rows))
                child.send(b'run\r')
                wait_frame(lambda rows: 'fake>' in '\n'.join(rows) and 'file.delete: answer.py' in '\n'.join(rows))
                menu('layout preview')
                child.send(b'layout use agent-top\r')
                wait_frame(lambda rows: 'PREVIEW updated' in '\n'.join(rows))
                escape()
                child.send(b'size\r')
                wait_frame(lambda rows: 'columns=68, lines=36' in '\n'.join(rows))
                menu('layout preview')
                child.send(b'layout use agent-top\rlayout apply\r')
                wait_frame(lambda rows: 'Preview applied' in '\n'.join(rows))
                child.send(b'layout save coding workspace\r')
                wait_frame(lambda rows: 'Layout saved: coding' in '\n'.join(rows))
                child.send(b'layout ratio main 6000\rlayout reload\r')
                wait_frame(lambda rows: 'reload --discard' in '\n'.join(rows))
                child.send(b'layout reload --discard\r')
                wait_frame(lambda rows: 'Layout reloaded' in '\n'.join(rows))
                escape()
                child.send(b'size\r')
                wait_frame(lambda rows: 'columns=138, lines=17' in '\n'.join(rows))
                child.resize(24, 80); terminal.resize(24, 80)
                wait_frame(lambda rows: 'terminal below 100x28' in rows[0])
                menu('layout preview')
                wait_frame(lambda rows: 'Layout menu' in '\n'.join(rows))
                child.send(b'\x11')
                deadline = time.monotonic() + 4
                while child.poll() is None and time.monotonic() < deadline:
                    child.read(); time.sleep(.02)
                self.assertEqual(child.poll(), 0)
                config = load_layout(workspace, user_file=root / 'xdg/labradour/layout.json')
                self.assertEqual(config.active_layout, 'coding')
                self.assertEqual(config.tree, preset('agent-top'))
                sessions = read_history(recording)
                self.assertEqual(sessions[0]['status'], 'completed')
                records = read_history(recording, sessions[0]['id'])
                self.assertTrue(any(r['kind'] == 'snapshot.completed' for r in records))
                self.assertFalse(any(r['kind'] == 'session.cleanup-gap' for r in records))
            finally:
                child.close()
