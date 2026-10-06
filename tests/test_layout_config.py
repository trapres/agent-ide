import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from labradour.layout import preset, tree_dict
from labradour.layout_config import MAX_BYTES, decode, load_layout, read_file, user_path
from labradour.pty_process import PtyProcess
from labradour.terminal import Terminal

ROOT = Path(__file__).resolve().parents[1]


def document(name='custom', tree=None, **extras):
    return dict(schema_version=1, layouts={name: tree_dict(tree or preset('agent-top'))},
                active_layout=name, **extras)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.user = self.root / 'user/layout.json'
        self.project = self.workspace / '.labradour/layout.json'

    def load(self, **options):
        return load_layout(self.workspace, user_file=self.user, **options)

    def test_layer_precedence_whole_tree_replacement_and_inheritance(self):
        write(self.user, document(initial_focus='activity'))
        write(self.project, document(tree=preset('visualization-top')))
        explicit = self.root / 'explicit.json'
        write(explicit, {'schema_version': 1, 'active_layout': 'agent-right'})
        config = self.load(explicit=explicit)
        self.assertEqual(config.tree, preset('agent-right'))
        self.assertEqual(config.initial_focus, 'activity')
        self.assertEqual(config.layouts['custom'], preset('visualization-top'))
        self.assertEqual(self.load(name='custom').tree, preset('visualization-top'))
        self.assertEqual([s['status'] for s in config.sources], ['loaded'] * 3)

    def test_discovery_ignores_whole_invalid_layer_explicit_errors_and_missing_names(self):
        write(self.user, document())
        bad = document(tree=preset('agent-right'))
        bad['layouts']['broken'] = {'type': 'pane', 'pane': 'agent'}
        write(self.project, bad)
        config = self.load()
        self.assertEqual(config.tree, preset('agent-top'))
        self.assertNotIn('broken', config.layouts)
        self.assertTrue(config.warnings)
        with self.assertRaises(ValueError):
            self.load(explicit=self.project)
        with self.assertRaises(ValueError):
            self.load(explicit=self.root / 'missing')
        write(self.project, {'schema_version': 1, 'active_layout': 'missing'})
        self.assertEqual(self.load().tree, preset())
        with self.assertRaises(ValueError):
            self.load(name='missing')
        with self.assertRaises(ValueError):
            self.load(explicit=self.project)

    def test_bounded_strict_json_schema(self):
        cases = [b'{"schema_version":1,"schema_version":1}', b'{"schema_version":1,"layouts":{"a":NaN}}',
                 b'[]', b'{"schema_version":true}', b'{"schema_version":2}', b'\xff',
                 b'[' * 1500 + b']' * 1500, b' ' * (MAX_BYTES + 1)]
        for field, value in [('unexpected', 1), ('initial_focus', []), ('active_layout', '1bad'),
                             ('layouts', []), ('schema_version', 1.0)]:
            obj = {'schema_version': 1, field: value}
            cases.append(json.dumps(obj).encode())
        cases.append(json.dumps({'schema_version': 1, 'layouts': {
            'name%d' % i: tree_dict(preset()) for i in range(33)}}).encode())
        for data in cases:
            with self.subTest(data=data[:80]), self.assertRaises(ValueError):
                decode(data)

    def test_file_type_size_symlink_and_fifo_fail_without_blocking(self):
        write(self.user, {'schema_version': 1})
        self.project.parent.mkdir()
        self.project.symlink_to(self.user)
        self.assertEqual(self.load().sources[1]['status'], 'ignored')
        with self.assertRaises((ValueError, OSError)):
            self.load().save('new', 'workspace', confirm_replace=True)
        self.project.unlink()
        os.mkfifo(self.project)
        self.assertEqual(self.load().sources[1]['status'], 'ignored')
        self.project.unlink()
        self.project.write_bytes(b' ' * (MAX_BYTES + 1))
        self.assertEqual(self.load().sources[1]['status'], 'ignored')
        self.project.unlink()
        self.project.mkdir()
        self.assertEqual(self.load().sources[1]['status'], 'ignored')

    def test_ignore_xdg_and_legacy_selection(self):
        write(self.project, document(initial_focus='visualization'))
        config = self.load(ignore=True)
        self.assertEqual(config.tree, preset())
        self.assertEqual(config.initial_focus, 'agent')
        self.assertEqual(config.warnings, [])
        with self.assertRaises(ValueError):
            self.load(ignore=True, explicit=self.project)
        with self.assertRaises(ValueError):
            self.load(name='default', side='left')
        self.assertEqual(self.load(agent_width=.9).tree.ratio_bps, 7000)
        self.assertEqual(self.load(side='right').tree.ratio_bps, 5000)
        self.assertEqual(self.load(side='left').initial_focus, 'visualization')
        with self.assertRaises(ValueError):
            self.load(agent_width=float('nan'))
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root)}):
            self.assertEqual(user_path(), self.root / 'labradour/layout.json')
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': 'relative'}):
            self.assertEqual(user_path(), Path.home() / '.config/labradour/layout.json')

    def test_save_restart_preserves_names_and_only_preferences(self):
        write(self.user, document(name='existing', initial_focus='visualization'))
        config = self.load(name='agent-right')
        result = config.save('my-layout')
        self.assertEqual(result['scope'], 'user')
        reloaded = self.load()
        self.assertEqual(reloaded.tree, preset('agent-right'))
        self.assertEqual(reloaded.initial_focus, 'visualization')
        self.assertIn('existing', reloaded.layouts)
        saved = json.loads(self.user.read_text())
        self.assertEqual(set(saved), {'schema_version', 'layouts', 'active_layout', 'initial_focus'})
        self.assertEqual(self.user.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.project.exists())
        self.assertFalse(list(self.user.parent.glob('.layout-*')))

    def test_save_shadow_invalid_ignored_and_newer_versions(self):
        config = self.load()
        with self.assertRaisesRegex(ValueError, 'shadow'):
            config.save('default', 'workspace')
        config.save('default', 'workspace', confirm_shadow=True)
        self.project.write_text('{broken')
        config = self.load()
        with self.assertRaisesRegex(ValueError, 'confirm-replace'):
            config.save('mine', 'workspace')
        config.save('mine', 'workspace', confirm_replace=True)
        config = self.load(ignore=True)
        with self.assertRaisesRegex(ValueError, 'ignored'):
            config.save('another', 'workspace')
        config.save('another', 'workspace', confirm_replace=True)
        write(self.project, {'schema_version': 2})
        original = self.project.read_bytes()
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            self.load().save('future', 'workspace', confirm_replace=True)
        self.assertEqual(self.project.read_bytes(), original)

    def test_conflicting_changes_creation_and_parallel_saves_do_not_overwrite(self):
        import fcntl
        a, b = self.load(), self.load()
        a.save('first', 'workspace')
        first = self.project.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed'):
            b.save('second', 'workspace')
        self.assertEqual(self.project.read_bytes(), first)
        config = self.load()
        write(self.project, document(name='external'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            config.save('mine', 'workspace')
        self.assertEqual(self.load().active_layout, 'external')
        config = self.load()
        with (self.project.parent / '.layout.json.lock').open('wb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'another layout save'):
                config.save('parallel', 'workspace')
        self.assertEqual(self.load().active_layout, 'external')

    def test_staging_failure_cleans_up_without_changing_live_or_disk_state(self):
        write(self.user, document())
        config = self.load()
        before, tree = self.user.read_bytes(), config.tree
        with patch('labradour.layout_config.os.replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                config.save('failed')
        self.assertEqual(self.user.read_bytes(), before)
        self.assertEqual(config.tree, tree)
        self.assertEqual(config.active_layout, 'custom')
        self.assertFalse(list(self.user.parent.glob('.layout-*')))

    def test_save_reports_unsaved_tree_without_persisting_transient_state(self):
        config = self.load()
        self.assertTrue(config.describe(tree=preset('agent-top'))['unsaved_changes'])
        config.save('edited', 'workspace', tree=preset('agent-top'))
        self.assertFalse(config.describe()['unsaved_changes'])
        self.assertEqual(self.load().tree, preset('agent-top'))
        self.assertEqual(config.describe()['sources'][-1]['status'], 'saved')
        compact = config.describe(24, 80, focus='activity', maximized=True)
        self.assertEqual(set(compact['geometry']), {'activity'})
        self.assertIsNone(compact['splits'][0]['effective_ratio_bps'])
        obj = tree_dict(preset())
        obj['ratio_bps'] = 9000
        from labradour.layout import parse_tree
        constrained = config.describe(28, 100, tree=parse_tree(obj))
        self.assertEqual(constrained['splits'][0]['requested_ratio_bps'], 9000)
        self.assertEqual(constrained['splits'][0]['effective_ratio_bps'], 7000)

    def test_cli_status_and_save_roundtrip_and_explicit_failure_before_launch(self):
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'xdg'))
        base = [sys.executable, '-m', 'labradour']
        result = subprocess.run(base + ['layout', 'save', 'coding', '--scope', 'workspace',
            '--workspace', str(self.workspace), '--layout', 'agent-top'], env=env, cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = subprocess.run(base + ['layout', 'status', '--workspace', str(self.workspace)],
                                env=env, cwd=ROOT, capture_output=True)
        self.assertEqual(json.loads(status.stdout)['active_layout'], 'coding')
        self.assertEqual(json.loads(status.stdout)['geometry']['agent']['content_columns'], 138)
        self.project.write_text('{bad')
        failure = subprocess.run(base + ['run', '--demo', '--layout-config', str(self.project)],
                                 env=env, cwd=ROOT, capture_output=True)
        self.assertEqual(failure.returncode, 2)
        self.assertIn(str(self.project).encode(), failure.stderr)
        self.assertNotIn(b'requires an interactive terminal', failure.stderr)

    def test_custom_launch_initial_focus_and_diagnostics_keep_pty_live(self):
        import time
        write(self.project, document(initial_focus='visualization'))
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'xdg'))
        child = PtyProcess([sys.executable, '-m', 'labradour', 'run', '--demo',
                            '--workspace', str(self.workspace)], ROOT, 40, 140, env)
        terminal = Terminal(40, 140)
        def wait_frame(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                replies = terminal.feed(child.read())
                if replies:
                    child.send(replies)
                if predicate(terminal.display):
                    return
                time.sleep(.02)
            self.fail('missing frame: ' + '\n'.join(terminal.display))
        try:
            wait_frame(lambda rows: 'Visualization [focus]' in rows[20])
            child.send(b'\x1di')
            wait_frame(lambda rows: 'Layout configuration' in '\n'.join(rows))
            child.send(b'\x1dasize\r')
            wait_frame(lambda rows: 'columns=138, lines=17' in '\n'.join(rows))
            child.send(b'\x11')
            deadline = time.monotonic() + 3
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.02)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()
