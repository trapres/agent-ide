import concurrent.futures
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from labradour.adapters.collector import Collector, submit, wait_receipt
from labradour.adapters.launch import compose
from labradour.adapters.providers import MAPPINGS, normalize
from labradour.hooks import configuration
from labradour.recorder import Recorder, read_history
from labradour.pty_process import PtyProcess

PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((PROJECT / "tests/fixtures/provider-hooks.json").read_text())


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.events = self.root / "events"
        self.events.mkdir()

    def collector(self):
        collector = Collector(self.workspace, self.events)
        self.addCleanup(collector.close)
        collector.start()
        return collector

    def recorder(self, collector, provider="claude"):
        recorder = Recorder(self.workspace, self.root / "recording", events=self.events,
                            backend="polling", adapter_collector=collector, adapter_provider=provider)
        self.addCleanup(lambda: recorder.close() if not recorder.lock.closed else None)
        recorder.start()
        return recorder

    def hook(self, collector, provider, raw):
        return subprocess.run([sys.executable, str(PROJECT / "labradour/hooks.py"), "--provider", provider],
                              input=json.dumps(raw), text=True, capture_output=True,
                              cwd=self.workspace, env=dict(os.environ, **collector.environment()), timeout=5)

    def test_documented_fixtures_preserve_raw_and_identity(self):
        for case in FIXTURES["cases"]:
            with self.subTest(provider=case["provider"], hook=case["raw"]["hook_event_name"]):
                result = normalize(case["provider"], case["raw"])
                self.assertEqual(result["kind"], case["kind"])
                self.assertEqual(result["payload"]["raw"], case["raw"])
                self.assertEqual(result["call_id"], case["raw"].get("tool_use_id"))
                self.assertEqual(result["actor_id"], case["raw"].get("agent_id"))
                self.assertIsNone(result["parent_actor_id"])
                if case["provider"] == "claude":
                    self.assertIsNone(result["turn_id"])

    def test_captured_native_fixtures_preserve_observations_and_limits(self):
        captured = json.loads((PROJECT / "tests/fixtures/native-provider-hooks.json").read_text())
        self.assertIn("normal trust", captured["provenance"])
        actors = set()
        for case in captured["cases"]:
            with self.subTest(provider=case["provider"], hook=case["raw"]["hook_event_name"]):
                result = normalize(case["provider"], case["raw"])
                self.assertEqual(result["kind"], case["kind"])
                self.assertEqual(result["payload"]["raw"], case["raw"])
                self.assertIsNone(result["parent_actor_id"])
                if case["raw"].get("agent_id"):
                    actors.add(case["provider"])
                if case["provider"] == "codex" and case["raw"]["hook_event_name"] == "PostToolUse":
                    self.assertEqual(result["payload"]["outcome"], "unknown")
                if case["provider"] == "claude":
                    self.assertIsNone(result["turn_id"])
        self.assertEqual(actors, {"claude", "codex"})

    def test_no_inferred_success_denial_or_call_identity(self):
        raw = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "Exit code: 1"}
        result = normalize("codex", raw)
        self.assertEqual(result["kind"], "tool.completed")
        self.assertEqual(result["payload"]["outcome"], "unknown")
        self.assertIsNone(result["call_id"])
        self.assertEqual(normalize("codex", {"hook_event_name": "PermissionDenied"})["kind"], "provider.unknown")
        self.assertEqual(normalize("claude", {"hook_event_name": "FutureEvent"})["kind"], "provider.unknown")
        self.assertNotEqual(normalize("claude", {"hook_event_name": "UserPromptSubmit", "prompt": "same"})["event_id"],
                            normalize("claude", {"hook_event_name": "UserPromptSubmit", "prompt": "same"})["event_id"])
        identified = dict(raw, tool_use_id="call-1", session_id="session-1")
        self.assertEqual(normalize("codex", identified), normalize("codex", identified))

    def test_launch_settings_preserve_cli_hooks_options_and_files(self):
        existing = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo existing"}]}]},
                    "disableAllHooks": True, "env": {"TEST_SETTING": "preserved"}}
        path = self.workspace / "settings.json"
        path.write_text(json.dumps(existing))
        before = path.read_bytes()
        result = compose("claude", ["claude", "--settings", "settings.json", "--model", "test-model"],
                         self.root, self.workspace, self.events, authenticated=True)
        saved = json.loads(Path(result[2]).read_text())
        self.assertEqual(len(saved["hooks"]["PreToolUse"]), 2)
        self.assertTrue(saved["disableAllHooks"])
        self.assertEqual(saved["env"], existing["env"])
        self.assertEqual(result[-2:], ["--model", "test-model"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(Path(result[2]).stat().st_mode & 0o777, 0o600)
        for args in [["codex", "-c", "hooks={}"], ["codex", "--config=hooks.PreToolUse=[]"]]:
            with self.assertRaises(ValueError):
                compose("codex", args, self.root, self.workspace, self.events, authenticated=True)
        codex = compose("codex", ["codex", "-c", "model=test-model"], self.root, self.workspace, self.events, True)
        self.assertEqual(codex[-2:], ["-c", "model=test-model"])
        self.assertIn("--no-daemon", codex)
        self.assertFalse(any("bypass" in arg for arg in codex))

    def test_hooks_are_synchronous_silent_observers(self):
        for provider in ("claude", "codex"):
            config = configuration(provider, authenticated=True)
            self.assertEqual(set(config["hooks"]), set(MAPPINGS[provider]))
            for groups in config["hooks"].values():
                handler = groups[0]["hooks"][0]
                self.assertNotIn("async", handler)
                self.assertEqual(handler["timeout"], 3)
                self.assertIn("--provider " + provider, handler["command"])
        result = subprocess.run([sys.executable, str(PROJECT / "labradour/hooks.py"), "--provider", "codex"],
                                input="not-json", capture_output=True, text=True, cwd=self.workspace)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("capture unavailable", result.stderr)

    def test_before_after_capture_preserves_intermediate_revert_and_retry(self):
        collector = self.collector()
        recorder = self.recorder(collector)
        target = self.workspace / "sample.txt"
        for index, text in enumerate(["one", "two", "one"]):
            fields = {"session_id": "session-1", "tool_use_id": "call-%d" % index, "tool_name": "Write"}
            pre = dict(fields, hook_event_name="PreToolUse", tool_input={"file_path": "sample.txt"})
            result = self.hook(collector, "claude", pre)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            target.write_text(text)
            post = dict(fields, hook_event_name="PostToolUse", tool_response={"type": "create"})
            result = self.hook(collector, "claude", post)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        events = read_history(recorder.directory, recorder.session)
        boundaries = [r for r in events if r["kind"] == "adapter.boundary"]
        self.assertEqual(len(boundaries), 6)
        after = [r["payload"]["checkpoint"] for r in boundaries if r["payload"]["phase"] == "after"]
        self.assertEqual([recorder.history.git("show", commit + ":sample.txt") for commit in after], [b"one", b"two", b"one"])
        old_commit = after[-1]
        target.write_text("later external edit")
        self.assertEqual(self.hook(collector, "claude", post).stderr, "")
        saved = read_history(recorder.directory, recorder.session)
        self.assertEqual(len([r for r in saved if r["kind"] == "adapter.boundary"]), 6)
        self.assertEqual(wait_receipt(normalize("claude", post)["event_id"], collector.environment())["checkpoint"], old_commit)

    def test_receipt_authentication_parallel_boundaries_and_timeout_health(self):
        collector = self.collector()
        self.assertEqual(wait_receipt("missing", collector.environment(), timeout=.05)["status"], "timeout")
        bad_env = dict(collector.environment(), LABRADOUR_COLLECTOR_TOKEN="wrong")
        self.assertEqual(wait_receipt("missing", bad_env, timeout=.1)["status"], "rejected")
        recorder = self.recorder(collector, "codex")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda index: self.hook(collector, "codex", {
                "hook_event_name": "PreToolUse", "session_id": "session-1", "turn_id": "turn-1",
                "tool_use_id": "parallel-%d" % index, "tool_name": "Bash", "tool_input": {"command": "echo dummy"}}), range(4)))
        self.assertTrue(all((r.returncode, r.stdout, r.stderr) == (0, "", "") for r in results))
        self.assertEqual(len([r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "adapter.boundary"]), 4)

    def test_unavailable_boundary_stays_observation_only_and_reports_gap(self):
        collector = self.collector()
        result = self.hook(collector, "claude", {"hook_event_name": "PreToolUse", "tool_use_id": "timeout", "tool_name": "Read"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("unavailable", result.stderr)
        self.assertTrue(any(e["kind"] == "adapter.health" for _, e in collector.pending()))
        recorder = self.recorder(collector)
        deadline = time.monotonic() + 3
        while not recorder.adapter_gaps and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertGreater(recorder.adapter_gaps, 0)

    def test_missing_provider_delivery_is_explicit_at_shutdown(self):
        collector = self.collector()
        recorder = self.recorder(collector)
        collector.close()
        recorder.close()
        session = read_history(recorder.directory)[0]
        self.assertEqual(session["status"], "completed-with-gaps")
        result = [r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "adapter.delivery"][-1]
        self.assertEqual(result["payload"]["status"], "no-delivery")

    def test_storage_failure_does_not_return_a_successful_boundary_receipt(self):
        collector = self.collector()
        recorder = self.recorder(collector)
        self.workspace.joinpath("force-change").write_text("new content")
        with patch.object(recorder.history, "checkpoint", side_effect=OSError(errno.ENOSPC, "injected disk full")):
            result = self.hook(collector, "claude", {"hook_event_name": "PreToolUse", "tool_use_id": "failed-capture", "tool_name": "Write"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("capture unavailable", result.stderr)
        self.assertTrue(recorder.suspended)
        self.assertTrue(any(e["kind"] == "adapter.health" for _, e in collector.pending()))

    def test_recorded_launcher_automatically_enables_native_observer(self):
        # A fake CLI executes the launch settings exactly as configured; this
        # exercises wiring but is not a native provider delivery measurement.
        executable = self.root / "claude"
        executable.write_text("#!" + sys.executable + "\n" + '''import json, subprocess, sys
from pathlib import Path
settings = json.loads(Path(sys.argv[sys.argv.index('--settings') + 1]).read_text())
def hook(name):
    raw = {'hook_event_name': name, 'session_id': 'fake-native', 'tool_use_id': 'write-1', 'tool_name': 'Write'}
    for group in settings['hooks'].get(name, []):
        for handler in group['hooks']:
            result = subprocess.run(handler['command'], shell=True, input=json.dumps(raw), text=True, capture_output=True)
            assert result.returncode == 0, result.stderr
hook('SessionStart')
hook('PreToolUse')
Path('sample.txt').write_text('native fixture')
hook('PostToolUse')
hook('SessionEnd')
print('NATIVE_ADAPTER_FIXTURE_READY', flush=True)
''')
        executable.chmod(0o700)
        settings = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo existing > existing.txt"}]}]}})
        store = self.root / "recording"
        child = PtyProcess([sys.executable, "-m", "labradour", "run", "--workspace", str(self.workspace),
                            "--record", str(store), "--hooks", "claude", "--watch-backend", "polling", "--",
                            str(executable), "--settings", settings], PROJECT, 40, 140)
        try:
            output = bytearray()
            deadline = time.monotonic() + 10
            while b"NATIVE_ADAPTER_FIXTURE_READY" not in output and time.monotonic() < deadline:
                output.extend(child.read())
                time.sleep(.02)
            self.assertIn(b"NATIVE_ADAPTER_FIXTURE_READY", output)
            child.send(b"\x11")
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.02)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()
        session = read_history(store)[0]
        saved = read_history(store, session["id"])
        self.assertEqual(session["status"], "completed")
        self.assertEqual(self.workspace.joinpath("existing.txt").read_text(), "existing\n")
        self.assertEqual(len([r for r in saved if r["kind"] == "adapter.event"]), 4)
        self.assertTrue(any(r["kind"] == "adapter.configured" for r in saved))


if __name__ == "__main__":
    unittest.main()
