"""Observable split-tree contracts and live PTY integration."""
import copy
import itertools
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock

from labradour.input import InputRouter
from labradour.layout import (PANES, PRESETS, Pane, Split, adjust_share, arrange,
                             compact_reason, minimum_size, pane_order, parse_tree,
                             preset, reflect, tree_dict)
from labradour.pty_process import PtyProcess
from labradour.terminal import Terminal
from labradour.ui import Harness

ROOT = Path(__file__).resolve().parents[1]


class TreeTests(unittest.TestCase):
    def test_presets_are_independent_valid_trees(self):
        for name in PRESETS:
            tree = preset(name)
            self.assertEqual(parse_tree(tree_dict(tree)), tree)
            self.assertEqual(set(pane_order(tree)), set(PANES))
        self.assertEqual(pane_order(preset('visualization-top')), ('agent', 'visualization', 'activity'))
        with self.assertRaises(ValueError):
            preset('missing')

    def test_rejects_invalid_contract_without_unbounded_recursion(self):
        valid = tree_dict(preset())
        cases = [None, [], {}, {'type': 'pane', 'pane': 'agent'}]
        for field, value in [('ratio_bps', True), ('ratio_bps', 999), ('ratio_bps', 9001),
                             ('ratio_bps', 5000.0), ('axis', []), ('axis', 'horizontal'),
                             ('id', '1bad'), ('id', 'x' * 33), ('extra', 1)]:
            bad = copy.deepcopy(valid)
            bad[field] = value
            cases.append(bad)
        bad = copy.deepcopy(valid)
        bad['second']['first']['pane'] = 'agent'
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad['second']['id'] = bad['id']
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad['second']['second']['pane'] = []
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad['first'] = bad  # Even cyclic Python inputs must be depth-bounded.
        cases.append(bad)
        for case in cases:
            with self.subTest(case=str(case)[:80]), self.assertRaises(ValueError):
                parse_tree(case)

    def test_worked_geometry_and_constrained_ratios(self):
        default = arrange(preset(), 40, 140)
        self.assertEqual(default['agent'].content_size, (36, 68))
        self.assertEqual((default['activity'].y, default['activity'].height), (1, 19))
        top = arrange(preset('agent-top'), 40, 140)
        self.assertEqual(top['agent'].content_size, (17, 138))
        right = arrange(preset('agent-right'), 40, 141)
        self.assertEqual((right['activity'].width, right['agent'].width), (71, 70))
        obj = tree_dict(preset())
        obj['ratio_bps'] = 9000
        tree = parse_tree(obj)
        self.assertEqual(arrange(tree, 28, 100)['agent'].width, 70)
        self.assertEqual(tree.ratio_bps, 9000)

    def test_every_order_axis_and_nesting_covers_area_with_minimums(self):
        for names, axes, nested_first, ratio in itertools.product(
                itertools.permutations(PANES), itertools.product(('rows', 'columns'), repeat=2),
                (False, True), (1000, 5000, 9000)):
            a, b, c = map(Pane, names)
            nested = Split('review', axes[1], ratio, b, c)
            tree = Split('main', axes[0], ratio, nested if nested_first else a,
                         a if nested_first else nested)
            tree = parse_tree(tree_dict(tree))
            min_h, min_w = minimum_size(tree)
            rows, cols = max(28, min_h + 2), max(100, min_w)
            for extra in (0, 1, 13):
                geometry = arrange(tree, rows + extra, cols + extra)
                occupied = set()
                for name, rect in geometry.items():
                    h, w = minimum_size(Pane(name))
                    self.assertGreaterEqual(rect.height, h)
                    self.assertGreaterEqual(rect.width, w)
                    cells = {(y, x) for y in range(rect.y, rect.y + rect.height)
                             for x in range(rect.x, rect.x + rect.width)}
                    self.assertFalse(occupied & cells)
                    occupied |= cells
                self.assertEqual(occupied, {(y, x) for y in range(1, rows + extra - 1)
                                            for x in range(cols + extra)})

    def test_compact_depends_on_tree_and_preserves_requests(self):
        tree = Split('main', 'rows', 5000, Pane('agent'),
                     Split('review', 'rows', 5000, Pane('activity'), Pane('visualization')))
        self.assertIn('minimum', compact_reason(tree, 28, 100))
        self.assertEqual(set(arrange(tree, 28, 100, 'visualization')), {'visualization'})
        self.assertEqual(set(arrange(tree, 30, 100)), set(PANES))
        self.assertEqual(set(arrange(tree, 40, 140, 'activity', True)), {'activity'})
        self.assertEqual(arrange(tree, 2, 3), {})
        self.assertEqual(tree.ratio_bps, 5000)

    def test_reflection_and_nearest_ancestor_share(self):
        tree = preset('agent-top')
        self.assertEqual(reflect(reflect(tree)), tree)
        self.assertEqual(pane_order(reflect(tree)), ('agent', 'visualization', 'activity'))
        unchanged, found = adjust_share(tree, 'agent', 'columns', 500)
        self.assertFalse(found)
        self.assertEqual(unchanged, tree)
        changed, found = adjust_share(preset('agent-right'), 'agent', 'columns', 500)
        self.assertTrue(found)
        self.assertEqual(changed.ratio_bps, 4500)
        changed, found = adjust_share(reflect(preset('agent-top')), 'activity', 'columns', 500)
        self.assertEqual(changed.second.ratio_bps, 4500)


class LayoutInputTests(unittest.TestCase):
    def test_fragmented_reverse_traversal_and_escape_cancel(self):
        router = InputRouter()
        self.assertEqual(router.feed(b'\x1d\x1b['), [])
        self.assertEqual(router.feed(b'Z'), [('command', b'\x1b[Z')])
        self.assertEqual(router.feed(b'\x1b[Z'), [('key', b'\x1b[Z')])
        router.feed(b'\x1d\x1b')
        router.escape_time -= 1
        self.assertEqual(router.expire(), [])
        self.assertEqual(router.feed(b'x'), [('key', b'x')])
        self.assertEqual(router.feed(b'\x1d\x1b[\x11'), [('command', b'q')])
        self.assertEqual(router.feed(b'x'), [('key', b'x')])
        pasted = router.feed(b'\x1b[200~\x1b[\x11\x1b[201~')
        self.assertFalse(any(kind == 'command' for kind, _ in pasted))

    def test_layout_controls_preserve_live_state_and_hidden_pty_size(self):
        harness = Harness([], ROOT, ROOT, layout_tree=preset('visualization-top'))
        harness.child = Mock()
        harness.terminal = Terminal(36, 68)
        harness.actions = [{'payload': {'marker': 'old'}}]
        harness.follow, harness.selected, harness.scroll = False, 0, 4
        child, terminal, actions = harness.child, harness.terminal, harness.actions
        harness.handle('command', b'\t')
        self.assertEqual(harness.focus, 'visualization')
        harness.handle('command', b'\x1b[Z')
        self.assertEqual(harness.focus, 'agent')
        harness.handle('command', b'm')
        harness.handle('command', b'+')
        harness.handle('command', b'v')
        harness.handle('command', b'z')
        screen = Mock()
        screen.getmaxyx.return_value = (40, 140)
        harness.sync_agent_size(harness.geometry(screen))
        child.resize.assert_not_called()
        harness.handle('command', b'a')
        harness.sync_agent_size(harness.geometry(screen))
        child.resize.assert_called_with(36, 138)
        harness.handle('key', b'x')
        child.send.assert_called_with(b'x')
        self.assertIs(harness.child, child)
        self.assertIs(harness.terminal, terminal)
        self.assertIs(harness.actions, actions)
        self.assertEqual((harness.follow, harness.selected, harness.scroll), (False, 0, 4))


class LayoutPtyTests(unittest.TestCase):
    def test_new_layout_renders_and_keeps_agent_live_through_focus_and_resize(self):
        child = PtyProcess([sys.executable, '-m', 'labradour', 'run', '--demo',
                            '--layout', 'agent-top'], ROOT, 40, 140)
        terminal = Terminal(40, 140)
        def frame_until(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                replies = terminal.feed(child.read())
                if replies:
                    child.send(replies)
                if predicate(terminal.display):
                    return terminal.display
                time.sleep(.02)
            self.fail('missing frame: ' + '\n'.join(terminal.display))
        try:
            frame = frame_until(lambda rows: 'fake>' in '\n'.join(rows))
            self.assertIn('Agent [focus]', frame[1])
            self.assertIn('Activity', frame[20][:70])
            self.assertIn('Visualization', frame[20][70:])
            child.send(b'\x1dv\x1dz')
            frame_until(lambda rows: 'Visualization [focus]' in rows[1])
            # Focus and native input in the same read batch need the new PTY size.
            child.send(b'\x1dasize\r')
            frame_until(lambda rows: 'PTY size: os.terminal_size(columns=138, lines=36)' in '\n'.join(rows))
            child.send(b'\x1dzsize\r')
            frame_until(lambda rows: 'PTY size: os.terminal_size(columns=138, lines=17)' in '\n'.join(rows))
            child.send(b'\x11')
            deadline = time.monotonic() + 3
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.02)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()

    def test_legacy_flags_and_layout_conflict_before_launch(self):
        result = subprocess.run([sys.executable, '-m', 'labradour', 'run', '--demo',
                                 '--layout', 'agent-top', '--side', 'left'],
                                cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'cannot be combined', result.stderr)
