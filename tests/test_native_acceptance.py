import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labradour.adapters.providers import normalize

spec = importlib.util.spec_from_file_location('native_acceptance', Path(__file__).resolve().parents[1] / 'tools/native_acceptance.py')
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class NativeAcceptanceTests(unittest.TestCase):
    def test_prepare_is_private_and_adds_only_local_observer(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = acceptance.prepare('codex', temporary, True)
            workspace = Path(result['workspace'])
            self.assertEqual(workspace.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(workspace.joinpath('readme.txt').read_text(), 'LABRADOUR_NATIVE_READ_MARKER\n')
            config = json.loads(Path(result['existing_observer']['settings']).read_text())
            self.assertEqual(set(config['hooks']), {'SessionStart', 'Stop', 'SessionEnd'})
            self.assertNotIn('bypass', result['launch_command'])
            self.assertNotIn('skip-permissions', result['launch_command'])

    def test_report_does_not_publish_raw_payloads_or_claim_gate_passed(self):
        raw = dict(hook_event_name='PostToolUse', session_id='native', tool_use_id='call',
                   tool_name='Bash', tool_response='PRIVATE_OUTPUT', tool_input={'command':'PRIVATE_COMMAND'})
        event = normalize('codex', raw)
        event['launch_id'] = 'launch'
        records = [{'sequence':1, 'event_id':'journal', 'session_id':'host', 'kind':'adapter.event','payload':event}]
        def history(directory, session=None):
            return records if session else [{'id':'host','status':'completed','started_ns':1}]
        with tempfile.TemporaryDirectory() as temporary, patch.object(acceptance, 'read_history', side_effect=history), \
             patch.object(acceptance.shutil, 'which', return_value=None):
            result = acceptance.summarize(temporary)
        rendered = json.dumps(result)
        self.assertNotIn('PRIVATE_OUTPUT', rendered)
        self.assertNotIn('PRIVATE_COMMAND', rendered)
        self.assertEqual(result['result_outcomes'], {'unknown':1})
        self.assertIn('not inferred', result['native_gate'])

    def test_observer_report_detects_settings_mutation_and_filters_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = acceptance.prepare('claude', temporary, True)
            root = Path(fixture['recording']).parent
            Path(fixture['existing_observer']['settings']).write_text('{}')
            Path(fixture['existing_observer']['log']).write_text('\n'.join(json.dumps(x) for x in [
                {'hook':'SessionStart','session_id':'native'}, {'hook':'Stop','session_id':'other'}]))
            event = dict(normalize('claude', {'hook_event_name':'SessionStart','session_id':'native'}), launch_id='launch')
            rows = [{'sequence':1,'event_id':'j','session_id':'host','kind':'adapter.event','payload':event}]
            with patch.object(acceptance, 'read_history', side_effect=lambda d,s=None: rows if s else [{'id':'host','status':'completed','started_ns':1}]), \
                 patch.object(acceptance.shutil, 'which', return_value=None):
                result = acceptance.summarize(root/'recording')
            self.assertFalse(result['existing_observer']['settings_unchanged'])
            self.assertEqual(result['existing_observer']['hook_counts_for_native_session'], {'SessionStart':1})

    def test_unknown_session_is_rejected(self):
        with patch.object(acceptance, 'read_history', return_value=[{'id':'host','status':'completed'}]):
            with self.assertRaisesRegex(ValueError,'unknown session'):
                acceptance.summarize('/tmp/nonexistent-fixture', 'other')
