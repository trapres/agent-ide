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
    def test_plugin_fixture_is_local_and_does_not_install_globally(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = acceptance.prepare('claude', temporary, True, plugin_observer=True)
            self.assertIn('--plugin-dir', fixture['launch_command'])
            self.assertEqual(fixture['plugin_observer']['layer'], 'plugin')
            config = json.loads(Path(fixture['plugin_observer']['settings']).read_text())
            self.assertEqual(set(config['hooks']), {'SessionStart', 'Stop', 'SessionEnd'})
            fixture = acceptance.prepare('codex', temporary, plugin_observer=True)
            self.assertEqual(len(fixture['plugin_setup_commands']), 2)
            root = Path(fixture['workspace']).parent
            catalog = json.loads((root/'.agents/plugins/marketplace.json').read_text())
            source = catalog['plugins'][0]['source']['path']
            self.assertTrue((root/source/'.codex-plugin/plugin.json').is_file())

    def test_managed_fixture_does_not_install_policy_or_allow_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            for provider in ('claude', 'codex'):
                fixture = acceptance.prepare(provider, temporary, managed_observer=True, managed_only=True)
                self.assertFalse(fixture['managed_policy']['installed'])
                self.assertTrue(fixture['managed_policy']['managed_only'])
                path = Path(fixture['managed_observer']['settings'])
                self.assertIn(Path(temporary).resolve(), path.parents)
                self.assertNotIn('bypass', path.read_text())
                if provider == 'claude':
                    self.assertTrue(json.loads(path.read_text())['allowManagedHooksOnly'])
                else:
                    self.assertIn('allow_managed_hooks_only = true', path.read_text())
            with self.assertRaisesRegex(ValueError, 'requires'):
                acceptance.prepare('claude', temporary, managed_only=True)

    def test_suppressed_delivery_keeps_independent_observer_evidence_unmatched(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = acceptance.prepare('codex', temporary, plugin_observer=True, managed_observer=True, managed_only=True)
            root = Path(fixture['recording']).parent
            Path(fixture['managed_observer']['log']).write_text(json.dumps({'hook':'SessionStart','session_id':'native'})+'\n')
            Path(fixture['plugin_observer']['log']).write_text(json.dumps({'hook':'Stop','session_id':None})+'\n')
            delivery = [{'sequence':1,'event_id':'delivery','session_id':'host','kind':'adapter.delivery',
                         'payload':{'provider':'codex','status':'no-delivery','counts':{}}}]
            with patch.object(acceptance, 'read_history', side_effect=lambda d,s=None: delivery if s else [{'id':'host','status':'completed-with-gaps','started_ns':1}]):
                result = acceptance.summarize(root/'recording')
            self.assertEqual(result['adapter_delivery_status'], [{'provider':'codex','status':'no-delivery','counts':{}}])
            self.assertEqual(result['managed_observer']['hook_counts_for_native_session'], {})
            self.assertEqual(result['managed_observer']['unmatched_hook_counts'], {'SessionStart':1})
            self.assertEqual(result['plugin_observer']['hook_counts_for_native_session'], {})
            self.assertEqual(result['plugin_observer']['unmatched_hook_counts'], {'Stop':1})

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
