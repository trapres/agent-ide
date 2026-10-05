import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from labradour.adapters.collector import Collector, encode, signature, submit
from labradour.adapters.events import coverage, validate
from labradour.recorder import Recorder, read_history
from labradour.pty_process import PtyProcess
from labradour.policy import CapturePolicy


def event(event_id="event-1", **fields):
    return dict(schema_version=1, event_id=event_id, provider="fixture", kind="tool.completed",
                payload={"tool": "Read"}, **fields)


class AdapterFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.spool = self.root / "events"
        self.spool.mkdir()
        self.collector = Collector(self.workspace, self.spool)
        self.addCleanup(self.collector.close)
        self.env = self.collector.environment()

    def test_contract_preserves_unknown_and_missing_identity(self):
        unknown = event()
        unknown["kind"] = "future.tool.result"
        unknown["payload"]["future_field"] = [1, 2]
        normalized = validate(unknown)
        self.assertEqual(normalized["kind"], "provider.unknown")
        self.assertEqual(normalized["original_kind"], "future.tool.result")
        self.assertEqual(normalized["payload"]["future_field"], [1, 2])
        self.assertIsNone(normalized["call_id"])
        self.assertEqual(normalized["quality"]["attribution"], "unassigned")
        for provider in coverage()["providers"].values():
            for item in provider.values():
                self.assertIn(item["status"], ("unverified", "observed-limited"))
                if item["status"] == "observed-limited":
                    self.assertTrue(item["version"])
                    self.assertTrue(item["platform"])
                    self.assertTrue(item["evidence"])
        self.assertEqual(coverage()["providers"]["claude"]["parallel_calls"]["status"], "observed-limited")
        self.assertEqual(coverage()["providers"]["codex"]["parallel_calls"]["status"], "observed-limited")

    def test_contract_rejects_bad_version_identity_and_non_json(self):
        for field, value in [("schema_version", True), ("schema_version", 2), ("event_id", "../escape"),
                             ("provider", []), ("payload", []), ("call_id", 123),
                             ("payload", {"bad": float("nan")})]:
            value_event = event()
            value_event[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate(value_event)

    def test_socket_authenticated_receipt_and_conflicting_id(self):
        self.collector.start()
        self.assertEqual(submit(event(), self.env)["durability"], "spool")
        submit(event(), self.env)
        self.assertEqual(len(self.collector.pending()), 1)
        changed = event()
        changed["payload"] = {"tool": "Write"}
        with self.assertRaises(ValueError):
            submit(changed, self.env)
        self.assertEqual(len(self.collector.pending()), 1)
        self.assertEqual(self.collector.health()["rejected"], 1)
        self.assertNotIn(self.collector.token, next(self.collector.spool.glob("*.json")).read_text())

    def test_wrong_token_and_workspace_are_rejected_without_fallback(self):
        self.collector.start()
        for key in ("LABRADOUR_COLLECTOR_TOKEN", "LABRADOUR_WORKSPACE_ID", "LABRADOUR_LAUNCH_ID"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                submit(event(), dict(self.env, **{key: "wrong"}))
        self.assertEqual(self.collector.pending(), [])
        self.assertEqual(self.collector.health()["rejected"], 3)

    def test_authenticated_fallback_and_tampered_file(self):
        env = dict(self.env, LABRADOUR_COLLECTOR_SOCKET=str(self.root / "offline"))
        self.assertEqual(submit(event(), env)["transport"], "fallback")
        path, accepted = self.collector.pending()[0]
        self.assertEqual(accepted["kind"], "tool.completed")
        raw = json.loads(path.read_text())
        raw["body"]["event"]["payload"] = {"tool": "forged"}
        path.write_text(json.dumps(raw))
        self.assertEqual(self.collector.pending(), [])
        self.assertIsNotNone(self.collector.health())

    def test_stale_launch_spool_and_symlink_are_rejected(self):
        body = {"launch_id": "stale", "workspace_id": self.collector.workspace_id, "event": event()}
        path = self.collector.spool / "event-1.json"
        path.write_bytes(encode({"body": body, "mac": signature(self.collector.token, body)}))
        self.assertEqual(self.collector.pending(), [])
        target = self.root / "target"
        target.write_text("do not read/delete")
        path.symlink_to(target)
        self.assertEqual(self.collector.pending(), [])
        self.assertEqual(target.read_text(), "do not read/delete")

    def test_parallel_callbacks_and_spool_limits(self):
        self.collector.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: submit(event("parallel-%d" % index), self.env), range(16)))
        self.assertTrue(all(result["accepted"] for result in results))
        self.assertEqual(len(self.collector.pending()), 16)
        with patch("labradour.adapters.collector.MAX_SPOOL_FILES", 16):
            with self.assertRaises(ValueError):
                submit(event("overflow"), self.env)
        self.assertEqual(len(self.collector.pending()), 16)
        self.assertIsNotNone(self.collector.health())

    def test_bad_socket_frame_does_not_stop_server(self):
        self.collector.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(self.collector.path)
            connection.sendall(b"not-json\n")
            self.assertFalse(json.loads(connection.recv(4096))["accepted"])
        self.assertTrue(submit(event(), self.env)["accepted"])

    def test_byte_limits_and_fifo_do_not_block_collection(self):
        offline = dict(self.env, LABRADOUR_COLLECTOR_SOCKET=str(self.root / "offline"))
        with patch("labradour.adapters.collector.MAX_SPOOL_BYTES", 1):
            with self.assertRaises(ValueError):
                submit(event(), offline)
        huge = event()
        huge["payload"] = {"text": "x" * (1024 * 1024)}
        with self.assertRaises(ValueError):
            submit(huge, offline)
        os.mkfifo(str(self.collector.spool / "fifo.json"))
        self.assertEqual(self.collector.pending(), [])

    def test_curses_launcher_wires_credentials_and_fixture(self):
        project = Path(__file__).resolve().parents[1]
        store = self.root / "recording"
        child = PtyProcess([sys.executable, "-m", "labradour", "run", "--workspace", str(self.workspace),
                            "--record", str(store), "--collector", "--watch-backend", "polling", "--",
                            sys.executable, str(project / "labradour/adapters/fixture.py")], project, 40, 140)
        try:
            output = bytearray()
            deadline = time.monotonic() + 10
            while b"Synthetic adapter fixture delivered" not in output and time.monotonic() < deadline:
                output.extend(child.read())
                time.sleep(.02)
            self.assertIn(b"Synthetic adapter fixture delivered", output)
            child.send(b"\x11")
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.02)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()
        session = read_history(store)[0]
        records = read_history(store, session["id"])
        self.assertEqual(session["status"], "completed")
        self.assertEqual(len([r for r in records if r["kind"] == "adapter.event"]), 9)
        self.assertTrue(any(r["kind"] == "adapter.foundation" for r in records))
        self.assertNotIn("LABRADOUR_COLLECTOR_TOKEN", json.dumps(records))

    def test_recorder_commits_deduplicates_and_drains_fixture_on_close(self):
        recorder = Recorder(self.workspace, self.root / "recording", events=self.spool,
                            backend="polling", adapter_collector=self.collector)
        try:
            # Direct capture/collection keeps test ordering deterministic.
            recorder.capture("baseline")
            self.collector.start()
            script = Path(__file__).resolve().parents[1] / "labradour/adapters/fixture.py"
            result = subprocess.run([sys.executable, str(script)], env=dict(os.environ, **self.env),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            recorder.collect_adapters()
            records = read_history(recorder.directory, recorder.session)
            facts = [r for r in records if r["kind"] == "adapter.event"]
            self.assertEqual(len(facts), 9)
            self.assertTrue(any(r["payload"]["kind"] == "provider.unknown" for r in facts))
            self.assertTrue(all(r["session_id"] == recorder.session for r in facts))
            # The callback's session end must not end the recorder's session.
            self.assertEqual(recorder.db.execute("SELECT status FROM sessions WHERE id=?", (recorder.session,)).fetchone()[0], "running")
            retry = event("fixture-02")
            retry.update(kind="tool.started", provider_session_id="fixture-session", call_id="read-1")
            submit(retry, self.env)
            recorder.collect_adapters()
            self.assertEqual(len([r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "adapter.event"]), 9)
            retry["payload"] = {"tool": "conflicting-tool"}
            submit(retry, self.env)
            recorder.collect_adapters()
            self.assertTrue(any(r["kind"] == "collector.conflict" for r in read_history(recorder.directory, recorder.session)))
            for index in range(40):
                submit(event("shutdown-%d" % index), self.env)
            self.collector.close()
        finally:
            recorder.close()
        saved = read_history(recorder.directory, recorder.session)
        self.assertEqual(len([r for r in saved if r["kind"] == "adapter.event"]), 49)
        self.assertEqual(list(self.collector.spool.glob("*.json")), [])

    def test_suspended_recording_keeps_pending_evidence(self):
        recorder = Recorder(self.workspace, self.root / "recording", backend="polling",
                            adapter_collector=self.collector)
        try:
            submit(event(), dict(self.env, LABRADOUR_COLLECTOR_SOCKET=str(self.root / "offline")))
            recorder.suspended = True
            recorder.collect_adapters()
            self.assertEqual(len(self.collector.pending()), 1)
        finally:
            recorder.close()

    def test_truncated_adapter_fact_keeps_digest_for_retry_validation(self):
        recorder = Recorder(self.workspace, self.root / "recording", backend="polling",
                            adapter_collector=self.collector, policy=CapturePolicy(max_event_bytes=1024))
        try:
            oversized = event()
            oversized.update({field: "x" * 256 for field in ("provider_session_id", "turn_id", "actor_id", "parent_actor_id", "call_id")})
            oversized["payload"] = {"text": "x" * 20000}
            offline = dict(self.env, LABRADOUR_COLLECTOR_SOCKET=str(self.root / "offline"))
            submit(oversized, offline)
            recorder.collect_adapters()
            submit(oversized, offline)
            recorder.collect_adapters()
            saved = read_history(recorder.directory, recorder.session)
            facts = [r for r in saved if r["kind"] == "adapter.event"]
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]["payload"]["kind"], "tool.completed")
            self.assertEqual(len(facts[0]["payload"]["content_digest"]), 64)
            self.assertTrue(facts[0]["payload"]["payload_truncated"])
            self.assertFalse(any(r["kind"] == "collector.conflict" for r in saved))
            for (raw,) in recorder.db.execute("SELECT record FROM events"):
                self.assertLessEqual(len(raw.encode()), 1024)
        finally:
            recorder.close()


if __name__ == "__main__":
    unittest.main()
