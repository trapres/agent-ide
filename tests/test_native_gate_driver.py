import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_gate_driver', Path(__file__).resolve().parents[1] / 'tools/native_gate_driver.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def fact(event, actor='left', kind='tool.requested', tool='Bash', launch='launch'):
    return {'kind':'adapter.event','payload':{'event_id':event,'kind':kind,'launch_id':launch,
            'provider':'claude','provider_session_id':'native','actor_id':actor,'turn_id':None,
            'call_id':'reused-call','payload':{'tool':tool,'raw':{'tool_input':{'command':'MARKER'}}}}}


def boundary(event, completed=True, launch='launch'):
    return {'kind':'adapter.boundary','payload':{'phase':'before','completed':completed,'adapter_event_id':event,'launch_id':launch}}


class NativeGateDriverTests(unittest.TestCase):
    def active(self, rows):
        with tempfile.TemporaryDirectory() as temporary:
            store=Path(temporary); (store/'journal.sqlite').touch()
            with patch.object(driver,'read_history',side_effect=lambda p,s=None: rows if s else [{'id':'host'}]):
                return driver.active_calls(store,'MARKER')

    def test_requires_saved_before_boundary_and_foreground_tool_type(self):
        rows=[fact('captured'),boundary('captured'),fact('not-captured',actor='right'),
              fact('agent',actor='agent',tool='Agent'),boundary('agent')]
        self.assertEqual([p['event_id'] for p in self.active(rows)],['captured'])

    def test_terminal_outcomes_are_scoped_by_actor_and_launch(self):
        rows=[fact('left'),boundary('left'),fact('right',actor='right'),boundary('right'),
              fact('other-launch',actor='left',launch='other'),boundary('other-launch',launch='other'),
              fact('left-end',kind='tool.completed')]
        self.assertEqual({p['event_id'] for p in self.active(rows)},{'right','other-launch'})

    def test_unavailable_boundary_is_not_overlap_evidence(self):
        self.assertEqual(self.active([fact('start'),boundary('start',False)]),[])

    def test_background_invocation_and_other_launch_receipt_are_not_active_evidence(self):
        background=fact('background'); background['payload']['payload']['raw']['tool_input']['run_in_background']=True
        self.assertEqual(self.active([background,boundary('background'),fact('start'),boundary('start',launch='other')]),[])
