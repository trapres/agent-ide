import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from labradour.recorder import Recorder, read_history
from labradour.pty_process import PtyProcess
from labradour.hooks import emit


class RecorderTests(unittest.TestCase):
    def test_intermediate_history_and_project_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store = root / "workspace", root / "recording"
            workspace.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(workspace), *args], stderr=subprocess.PIPE)
            git("init")
            (workspace / "source").write_bytes(b"one\r\n")
            git("add", "source")
            index = (workspace / ".git/index").read_bytes()
            (workspace / "source").write_bytes(b"dirty\r\n")
            recorder = Recorder(workspace, store)
            try:
                recorder.capture("baseline")
                baseline = recorder.history.head
                (workspace / "source").write_bytes(b"changed\r\n")
                (workspace / "created").write_bytes(b"\x00binary")
                recorder.capture("mutation")
                middle = recorder.history.head
                (workspace / "source").write_bytes(b"dirty\r\n")
                (workspace / "created").unlink()
                recorder.capture("revert")
                final = recorder.history.head
                self.assertNotEqual(baseline, middle)
                self.assertEqual(recorder.history.diff(baseline, final), "")
                self.assertIn("changed", recorder.history.diff(baseline, middle))
                self.assertEqual((workspace / ".git/index").read_bytes(), index)
                self.assertEqual(git("for-each-ref"), b"")
                self.assertEqual(recorder.history.git("show", baseline + ":source"), b"dirty\r\n")
            finally:
                recorder.close()
            sessions = read_history(store)
            events = read_history(store, sessions[0]["id"])
            self.assertEqual(sessions[0]["status"], "completed")
            self.assertTrue(any(r["payload"].get("reason") == "revert" for r in events))
            sequence = [r["sequence"] for r in events]
            self.assertEqual(sequence, sorted(set(sequence)))

    def test_exclusions_metadata_symlinks_and_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = workspace / ".labradour"
            (workspace / ".env").write_text("excluded-secret")
            (workspace / "node_modules").mkdir()
            (workspace / "node_modules/a").write_text("excluded-dependency")
            (workspace / "big").write_bytes(b"123456789")
            (workspace / "script").write_bytes(b"hi")
            (workspace / "script").chmod(0o755)
            (workspace / "link").symlink_to("/outside")
            recorder = Recorder(workspace, store, max_file=8)
            try:
                recorder.capture("baseline")
                self.assertEqual(set(recorder.manifest), {"script", "link"})
                self.assertEqual(recorder.manifest["script"][0], "100755")
                self.assertEqual(recorder.manifest["link"], ("120000", b"/outside"))
                events = read_history(store, recorder.session)
                completed = [r for r in events if r["kind"] == "snapshot.completed"][-1]
                self.assertEqual(completed["payload"]["quality"], "partial")
                self.assertTrue(any(m["path"] == "big" for m in completed["payload"]["metadata"]))
                objects = recorder.history.git("cat-file", "--batch-all-objects", "--batch").decode("utf-8", "replace")
                self.assertNotIn("excluded-secret", objects)
                self.assertNotIn("excluded-dependency", objects)
            finally:
                recorder.close()

    def test_recover_ref_after_completion_interruption_and_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "a").write_text("one")
            store = root / "recording"
            first = Recorder(workspace, store)
            with self.assertRaisesRegex(ValueError, "active writer"):
                Recorder(workspace, store)
            first.capture("baseline")
            session = first.session
            intent = first.append("snapshot.intent", {"reason": "interrupted", "previous_commit": first.history.head})
            recovered = first.history.checkpoint({"a": ("100644", b"two")}, "interrupted")
            # Simulate process loss after ref update, before journal completion.
            first.db.close()
            first.lock.close()
            second = Recorder(workspace, store)
            try:
                events = read_history(store, session)
                interrupted = [r for r in events if r["kind"] == "snapshot.interrupted"]
                self.assertEqual(interrupted[0]["payload"]["intent"], intent["event_id"])
                self.assertEqual(interrupted[0]["payload"]["recoverable_commit"], recovered)
                self.assertEqual(second.history.git("show", recovered + ":a"), b"two")
                self.assertEqual(read_history(store)[0]["status"], "interrupted")
                second.capture("baseline")
                self.assertEqual(second.history.git("show", second.history.head + ":a"), b"one")
            finally:
                second.close()

    def test_live_watcher_hook_dedup_and_overflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            events = root / "spool"
            events.mkdir()
            recorder = Recorder(workspace, root / "recording", backend="polling", interval=.2, events=events)
            try:
                recorder.start()
                emit({"tool_name": "Read", "hook_event_name": "PostToolUse"}, events)
                (workspace / "a").write_text("captured")
                recorder.watcher.dropped = 3
                deadline = time.monotonic() + 5
                captured = []
                while time.monotonic() < deadline:
                    captured = read_history(recorder.directory, recorder.session)
                    if any(r["kind"] == "collector.overflow" for r in captured) and any(
                            any(c["path"] == "a" for c in r["payload"].get("changes", [])) for r in captured):
                        break
                    time.sleep(.05)
                self.assertTrue(any(r["kind"] == "collector.overflow" for r in captured))
                self.assertTrue(any(any(c["path"] == "a" for c in r["payload"].get("changes", [])) for r in captured))
                self.assertEqual(sum(r["kind"] == "provider.event" for r in captured), 1)
            finally:
                recorder.close()

    def test_paused_launcher_does_not_write_before_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            child = PtyProcess([sys.executable, "-c", "open('startup', 'w').write('yes')"], workspace, paused=True)
            recorder = Recorder(workspace, root / "recording", backend="polling", interval=.2)
            try:
                recorder.start()
                self.assertFalse((workspace / "startup").exists())
                baseline = recorder.history.head
                child.release()
                deadline = time.monotonic() + 5
                while child.poll() is None and time.monotonic() < deadline:
                    child.read()
                    time.sleep(.02)
                self.assertEqual(child.poll(), 0)
            finally:
                child.close()
                recorder.close()
            events = read_history(recorder.directory, recorder.session)
            final = [r for r in events if r["kind"] == "snapshot.completed"][-1]["payload"]["commit"]
            self.assertNotEqual(baseline, final)

    def test_recorded_curses_launch_and_saved_diff_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store = root / "workspace", root / "recording"
            workspace.mkdir()
            project = Path(__file__).resolve().parents[1]
            child = PtyProcess([sys.executable, "-m", "labradour", "run", "--workspace", str(workspace),
                                "--record", str(store), "--watch-backend", "polling", "--", sys.executable,
                                "-c", "open('startup', 'w').write('saved'); print('RECORDER_READY')"], project, 40, 140)
            try:
                output = bytearray()
                deadline = time.monotonic() + 10
                while b"RECORDER_READY" not in output and time.monotonic() < deadline:
                    output.extend(child.read())
                    time.sleep(.02)
                self.assertIn(b"RECORDER_READY", output)
                child.send(b"\x11")
                while child.poll() is None and time.monotonic() < deadline:
                    child.read()
                    time.sleep(.02)
                self.assertEqual(child.poll(), 0, output.decode("utf-8", "replace")[-1000:])
            finally:
                child.close()
            session = read_history(store)[0]
            self.assertEqual(session["status"], "completed")
            events = read_history(store, session["id"])
            checkpoints = [r["payload"]["commit"] for r in events if r["kind"] == "snapshot.completed"]
            diff = subprocess.check_output([sys.executable, "-m", "labradour", "diff", str(store),
                                            checkpoints[0], checkpoints[-1]], cwd=project)
            self.assertIn(b"+saved", diff)

    def test_quota_failure_is_visible_and_preserves_prior_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = Recorder(workspace, root / "recording")
            try:
                recorder.capture("baseline")
                before = recorder.history.head
                recorder.quota = 1
                (workspace / "a").write_text("not captured")
                recorder.capture("mutation")
                self.assertEqual(recorder.history.head, before)
                self.assertIn("quota", recorder.notice)
                events = read_history(recorder.directory, recorder.session)
                self.assertEqual(events[-1]["kind"], "snapshot.failed")
            finally:
                recorder.close()


if __name__ == "__main__":
    unittest.main()
