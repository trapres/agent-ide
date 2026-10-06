import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
import sys

from labradour.activity import ActivityModel, ActivityReplay, build_rows
from labradour.adapters.correlation import project
from labradour.adapters.providers import normalize
from labradour.pty_process import PtyProcess
from labradour.terminal import Terminal
from labradour.ui import Harness

ROOT = Path(__file__).resolve().parents[1]


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.records = []

    def row(self, kind, payload):
        row = dict(sequence=len(self.records)+1, event_id='event-%d' % len(self.records),
                   session_id='host', kind=kind, payload=payload)
        self.records.append(row)
        return row

    def tool(self, kind, call='a', actor=None):
        fact = normalize('claude', dict(hook_event_name=kind, session_id='native',
            tool_use_id=call, agent_id=actor, tool_name='Write', tool_input={'file_path':'x.py'}))
        fact['launch_id'] = 'launch'
        return self.row('adapter.event', fact)

    def snapshot(self, changed=False):
        return self.row('snapshot.completed', dict(commit='a'*40, before_commit='b'*40,
            changes=[{'path':'x.py','operation':'modify'}] if changed else []))

    def boundary(self, tool, snapshot):
        self.row('adapter.boundary', dict(launch_id='launch', adapter_event_id=tool['payload']['event_id'],
            phase=tool['payload']['boundary'], checkpoint=snapshot['payload']['commit'],
            snapshot_event_id=snapshot['event_id'], completed=True, quality='observed-live-state'))

    def test_call_row_updates_in_place_selection_and_follow_are_independent(self):
        self.tool('PreToolUse')
        model = ActivityModel(); model.update(list(self.records))
        self.assertEqual(len(model.rows), 1)
        selected = model.selected_id
        self.assertEqual(model.selected()['state'], 'requested')
        model.move(0)
        self.tool('PostToolUse')
        model.update(list(self.records))
        self.assertEqual(len(model.rows), 1)
        self.assertEqual(model.selected_id, selected)
        self.assertEqual(model.selected()['state'], 'completed')
        self.assertEqual(model.unread, 0)
        self.assertIs(model.visible(), model.visible())
        self.assertEqual(len(model.details()['observations']), 2)
        self.tool('PreToolUse', call='b')
        model.update(list(self.records))
        self.assertEqual(model.selected_id, selected)
        self.assertEqual(model.unread, 1)
        model.resume()
        self.assertNotEqual(model.selected_id, selected)
        self.assertEqual(model.unread, 0)

    def test_shared_effects_are_candidates_not_ownership_or_duplicate_calls(self):
        a = self.tool('PreToolUse', actor='one'); self.boundary(a, self.snapshot())
        b = self.tool('PreToolUse', call='b', actor='two'); self.boundary(b, self.snapshot())
        changed = self.snapshot(True)
        a_end = self.tool('PostToolUse', actor='one'); self.boundary(a_end, self.snapshot())
        b_end = self.tool('PostToolUse', call='b', actor='two'); self.boundary(b_end, self.snapshot())
        model = ActivityModel(); model.update(list(self.records))
        actions = [r for r in model.rows if r['kind']=='action']
        self.assertEqual(len(actions), 2)
        for action in actions:
            self.assertEqual(len(action['children']), 1)
            child = action['children'][0]
            self.assertEqual(child['payload']['quality'], 'overlapping-calls')
            self.assertEqual(child['payload']['attribution'], 'external-or-unknown')
            self.assertEqual(len(child['payload']['candidate_action_ids']), 2)
            model.selected_id = action['id']; model.expand()
        children = [r for r in model.visible() if r['kind']=='effect']
        self.assertEqual(len(children), 2)
        self.assertEqual(len({r['payload']['id'] for r in children}), 1)
        self.assertEqual(len({r['id'] for r in children}), 2)
        model.selected_id = children[0]['id']; model.expand(False)
        self.assertEqual(model.selected_id, children[0]['parent'])

    def test_filters_preserve_hidden_selection_and_support_path_actor_state(self):
        self.tool('PreToolUse', actor='worker')
        self.tool('PostToolUse', actor='worker')
        self.snapshot(True)
        model = ActivityModel(); model.update(list(self.records))
        selected = model.selected_id
        model.set_filter('tool:Read')
        self.assertEqual(model.visible(), [])
        self.assertTrue(model.selection_outside())
        self.assertEqual(model.selected_id, selected)
        self.assertIsNotNone(model.selected())
        model.set_filter('actor:worker state:completed')
        self.assertEqual(len(model.visible()), 1)
        model.set_filter('path:x.py')
        self.assertTrue(model.visible())
        model.set_filter('')
        self.assertEqual(model.selected_id, selected)
        self.assertFalse(model.selection_outside())

    def test_unknown_identity_gaps_lifecycle_and_raw_mode_remain_inspectable(self):
        self.row('adapter.event', dict(kind='turn.started', provider='claude', payload={}))
        self.row('adapter.event', dict(kind='tool.requested', provider='claude', payload={}))
        self.row('adapter.event', dict(kind='tool.completed', provider='claude', payload={}))
        self.row('snapshot.failed', {'error':'fixture'})
        model = ActivityModel(); model.update(self.records, project(self.records, 'process-exited'))
        actions = [r for r in model.rows if r['kind']=='action']
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]['state'], 'incomplete')
        self.assertEqual(actions[0]['actor'], 'unknown')
        self.assertEqual(len(model.projection['gaps']), 1)
        self.assertTrue(any(r['state']=='turn.started' for r in model.rows))
        model.move(0); selected=model.selected_id
        model.toggle_journal()
        self.assertEqual(len(model.rows), 4)
        self.assertTrue(model.selection_outside())
        model.toggle_journal()
        self.assertEqual(model.selected_id, selected)
        self.assertFalse(model.selection_outside())

    def test_filter_input_unicode_paste_cancel_and_layout_preserve_state(self):
        self.tool('PreToolUse')
        harness = Harness([], ROOT, ROOT, recording=ROOT)
        harness.activity.update(self.records)
        harness.focus='activity'; harness.child=Mock(); harness.terminal=Terminal(36,68)
        selected=harness.activity.selected_id
        harness.handle('key', b'/')
        for kind, token in harness.router.feed('path:界.py'.encode()): harness.handle(kind,token)
        harness.handle('key', b'\r')
        self.assertEqual(harness.activity.filter, 'path:界.py')
        harness.handle('command', b'm')
        self.assertEqual(harness.activity.selected_id, selected)
        harness.handle('key', b'/'); harness.handle('key', b'\x15'); harness.handle('key', b'\x1b')
        self.assertEqual(harness.activity.filter, 'path:界.py')
        harness.child.send.assert_not_called()

    def test_background_replay_repairs_missing_notifications_and_closes_missing_outcome(self):
        from labradour.recorder import Recorder
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); workspace=root/'workspace'; workspace.mkdir()
            recorder=Recorder(workspace, root/'recording', backend='polling')
            try:
                fact=self.tool('PreToolUse')['payload']
                recorder.append('adapter.event', fact)
                replay=ActivityReplay(root/'recording',recorder.session)
                result=None; deadline=time.monotonic()+3
                while result is None and time.monotonic()<deadline:
                    result=replay.tick(status='process-exited'); time.sleep(.01)
                self.assertIsNotNone(result)
                self.assertEqual(result[1]['actions'][0]['state'],'incomplete')
                self.assertFalse(replay.error)
                completed = self.tool('PostToolUse')['payload']
                recorder.append('adapter.event', completed)
                replay.last = 0  # Periodic refresh, without a display notification.
                result = None; deadline=time.monotonic()+3
                while result is None and time.monotonic()<deadline:
                    result=replay.tick(status='process-exited'); time.sleep(.01)
                self.assertEqual(result[1]['actions'][0]['state'], 'completed')
            finally: recorder.close()


class ActivityPtyTests(unittest.TestCase):
    def test_authenticated_fixture_grouped_rows_filter_journal_and_shutdown(self):
        from labradour.recorder import read_history
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); workspace=root/'workspace'; workspace.mkdir()
            script=root/'fixture.py'
            script.write_text("import sys,time\nsys.path.insert(0,%r)\nfrom labradour.adapters.collector import submit\n"
                "base=dict(schema_version=1,provider='fixture',provider_session_id='native',actor_id='worker',call_id='call')\n"
                "submit(dict(base,event_id='before',kind='tool.requested',payload={'tool':'Read'}))\n"
                "print('READY',flush=True)\ntime.sleep(.2)\n"
                "submit(dict(base,event_id='after',kind='tool.completed',payload={'tool':'Read','outcome':'unknown'}))\n"
                "print('DONE',flush=True)\n" % str(ROOT))
            child=PtyProcess([sys.executable,'-m','labradour','run','--workspace',str(workspace),
                '--record',str(root/'recording'),'--collector','--',sys.executable,str(script)], ROOT,40,140,
                dict(os.environ,XDG_CONFIG_HOME=str(root/'xdg')))
            terminal=Terminal(40,140)
            def wait_frame(predicate):
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    replies=terminal.feed(child.read())
                    if replies: child.send(replies)
                    if predicate(terminal.display): return
                    time.sleep(.02)
                self.fail('missing frame: '+'\n'.join(terminal.display))
            try:
                wait_frame(lambda rows:'worker Read completed' in '\n'.join(rows))
                child.send(b'\x1dl/tool:Missing\r')
                wait_frame(lambda rows:'Selection outside' in '\n'.join(rows))
                child.send(b'/\x15\r')
                wait_frame(lambda rows:'worker Read completed' in '\n'.join(rows))
                child.send(b'\x1dr')
                wait_frame(lambda rows:'Journal' in '\n'.join(rows))
                child.send(b'\x1drf\x11')
                deadline=time.monotonic()+4
                while child.poll() is None and time.monotonic()<deadline: child.read();time.sleep(.02)
                self.assertEqual(child.poll(),0)
                sessions=read_history(root/'recording')
                self.assertEqual(sessions[0]['status'],'completed')
            finally: child.close()
