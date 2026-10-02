import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from labradour.policy import CapturePolicy, byte_size, load_policy
from labradour.recorder import Recorder, read_history
from labradour.storage import BudgetExceeded, BudgetStore, file_bytes, read_health
from watchdog.events import FileCreatedEvent


class CapturePolicyTests(unittest.TestCase):
    def test_file_policy_cli_overrides_and_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(json.dumps({"schema_version": 1, "exclude": ["private"],
                                       "max_file_bytes": 32, "storage_budget_bytes": 131072}))
            policy = load_policy(path, exclude=["*.key"], max_file_bytes=16)
            self.assertEqual(policy.exclude, ("private", "*.key"))
            self.assertEqual(policy.max_file_bytes, 16)
            self.assertFalse(policy.included(Path("private/nested/a")))
            self.assertFalse(policy.included(Path("keys/a.key")))
            self.assertEqual(byte_size("2MiB"), 2097152)
            for value in ("0", "-1", "2MB"):
                with self.assertRaises(ValueError):
                    byte_size(value)
            for values in ({"storage_budget_bytes": 1}, {"max_file_bytes": True},
                           {"exclude": ["../secret"]}, {"metadata_only": "*.key"},
                           {"max_event_bytes": 2}, {"use_default_exclusions": "false"}):
                with self.assertRaises(ValueError):
                    CapturePolicy(**values)
            path.write_text('{"schema_version": true}')
            with self.assertRaisesRegex(ValueError, "schema_version"):
                load_policy(path)
            path.write_text('{"typo": 1}')
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_policy(path)

    def test_custom_exclusions_and_metadata_are_applied_before_open_and_watch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "private").mkdir()
            (workspace / "private/key").write_bytes(b"private-secret")
            (workspace / "binary").write_bytes(b"metadata-secret")
            (workspace / "public").write_bytes(b"public-bytes")
            recorder = Recorder(workspace, root / "recording",
                                policy=CapturePolicy(exclude=("private",), metadata_only=("binary",)))
            opened = []
            original_open = os.open
            def observe_open(path, *args, **kwargs):
                opened.append(str(path))
                return original_open(path, *args, **kwargs)
            try:
                with patch("labradour.recorder.os.open", side_effect=observe_open):
                    recorder.capture("baseline")
                self.assertNotIn("binary", opened)
                self.assertNotIn("key", opened)
                self.assertEqual(set(recorder.manifest), {"public"})
                recorder.watcher.on_any_event(FileCreatedEvent(str(workspace / "private/key")))
                self.assertEqual(recorder.watcher.drain(), [])
                objects = recorder.history.git("cat-file", "--batch-all-objects", "--batch")
                self.assertNotIn(b"private-secret", objects)
                self.assertNotIn(b"metadata-secret", objects)
                completed = [r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "snapshot.completed"][-1]
                self.assertEqual(completed["payload"]["metadata"][0]["reason"], "policy.metadata_only")
            finally:
                recorder.close()

    def test_deterministic_scan_limit_and_omission_is_not_a_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "a").write_bytes(b"1234")
            (workspace / "b").write_bytes(b"5678")
            recorder = Recorder(workspace, root / "recording",
                                policy=CapturePolicy(max_file_bytes=8, max_capture_bytes=4))
            try:
                recorder.capture("baseline")
                self.assertEqual(set(recorder.manifest), {"a"})
                event = [r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "snapshot.completed"][-1]
                self.assertEqual(event["payload"]["metadata"][0]["reason"], "max_capture_bytes")
                (workspace / "a").write_bytes(b"now-too-large")
                recorder.capture("growth")
                event = [r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "snapshot.completed"][-1]
                self.assertIn({"path": "a", "operation": "file.omitted"}, event["payload"]["changes"])
                self.assertEqual(event["payload"]["quality"], "partial")
            finally:
                recorder.close()

    def test_disabling_defaults_keeps_git_and_internal_exclusions(self):
        policy = CapturePolicy(use_default_exclusions=False)
        self.assertTrue(policy.included(Path("vendor/source")))
        self.assertTrue(policy.included(Path(".env")))
        self.assertFalse(policy.included(Path("nested/.git/config")))
        self.assertFalse(policy.included(Path(".labradour-stage-abc/a")))

    def test_preview_creates_no_recording_and_invalid_config_does_not_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "recording"
            output = subprocess.check_output([sys.executable, "-m", "labradour", "policy", "--workspace", str(root),
                                              "--record", str(store), "--exclude", "private", "--storage-budget", "1MiB"])
            policy = json.loads(output)
            self.assertEqual(policy["storage_budget_bytes"], 1048576)
            self.assertIn(str(store.resolve()), policy["excluded_paths"])
            self.assertFalse(store.exists())
            bad = subprocess.run([sys.executable, "-m", "labradour", "run", "--record", str(store),
                                  "--storage-budget", "1", "--", "nonexistent"], capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn(b"at least 64KiB", bad.stderr)
            self.assertFalse(store.exists())


class HardBudgetTests(unittest.TestCase):
    def test_rejected_git_batch_leaves_head_and_objects_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = Recorder(workspace, root / "recording", quota=128 * 1024)
            try:
                recorder.capture("baseline")
                before = recorder.history.head
                objects = recorder.history.git("cat-file", "--batch-all-objects", "--batch-check")
                (workspace / "huge").write_bytes(os.urandom(256 * 1024))
                recorder.capture("cannot fit")
                self.assertTrue(recorder.suspended)
                self.assertEqual(recorder.history.head, before)
                self.assertEqual(recorder.history.git("cat-file", "--batch-all-objects", "--batch-check"), objects)
                self.assertLessEqual(file_bytes(recorder.directory), recorder.storage.limit)
                self.assertEqual(read_health(recorder.directory)["status"], "storage-limit")
                self.assertEqual(read_history(recorder.directory)[0]["status"], "storage-limit")
                self.assertTrue(any(r["kind"] == "storage.limit" for r in recorder.drain()))
                self.assertEqual(list(root.glob(".labradour-stage-*")), [])
            finally:
                recorder.close()
            status = subprocess.check_output([sys.executable, "-m", "labradour", "recording-status", str(root / "recording")])
            self.assertEqual(json.loads(status)["last_commit"], before)

    def test_journal_exhaustion_stops_all_writes_and_can_resume_with_larger_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            store = root / "recording"
            recorder = Recorder(workspace, store, quota=128 * 1024)
            recorder.capture("baseline")
            session, before = recorder.session, recorder.history.head
            try:
                for _ in range(100):
                    recorder.append("provider.event", {"tool": "Read", "data": os.urandom(4096).hex()})
                    self.assertLessEqual(file_bytes(store), recorder.storage.limit)
                    if recorder.suspended:
                        break
                self.assertTrue(recorder.suspended)
                size = file_bytes(store)
                for _ in range(5):
                    recorder.append("provider.event", {"ignored": True})
                    recorder.capture("ignored")
                self.assertEqual(file_bytes(store), size)
                self.assertEqual(recorder.history.head, before)
            finally:
                recorder.close()
            second = Recorder(workspace, store, quota=512 * 1024)
            try:
                second.capture("baseline")
                self.assertFalse(second.suspended)
                self.assertTrue(any(r["kind"] == "session.interrupted" for r in read_history(store, session)))
            finally:
                second.close()

    def test_preflight_rejects_whole_batch_before_first_install_and_smaller_budget_preserves_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "store"
            directory.mkdir()
            storage = BudgetStore(directory, 65536)
            (directory / "previous").write_bytes(b"keep")
            with storage.stage() as stage:
                small, large = stage / "small", stage / "large"
                small.write_bytes(b"small")
                large.write_bytes(b"x" * 65536)
                with self.assertRaises(BudgetExceeded):
                    storage.install({directory / "a": small, directory / "b": large})
            self.assertFalse((directory / "a").exists())
            self.assertFalse((directory / "b").exists())
            self.assertEqual((directory / "previous").read_bytes(), b"keep")
            old = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
            with self.assertRaises(BudgetExceeded):
                BudgetStore(directory, 1)
            self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}, old)

    def test_each_install_stays_bounded_and_readers_keep_a_consistent_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = Recorder(workspace, root / "recording", quota=256 * 1024)
            observed = []
            original_replace = os.replace
            def observe_replace(source, destination):
                original_replace(source, destination)
                observed.append(file_bytes(recorder.directory))
            try:
                with patch("labradour.storage.os.replace", side_effect=observe_replace):
                    recorder.capture("baseline")
                    (workspace / "small").write_bytes(os.urandom(16384))
                    recorder.capture("fits")
                    (workspace / "large").write_bytes(os.urandom(512 * 1024))
                    recorder.capture("too large")
                self.assertTrue(observed)
                self.assertLessEqual(max(observed), recorder.storage.limit)
                self.assertTrue(recorder.suspended)
                events = read_history(recorder.directory, recorder.session)
                self.assertTrue(any(r["kind"] == "snapshot.completed" for r in events))
            finally:
                recorder.close()

    def test_legacy_wal_journal_upgrade_preserves_committed_facts(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "store"
            directory.mkdir()
            legacy = sqlite3.connect(str(directory / "journal.sqlite"))
            try:
                legacy.execute("PRAGMA journal_mode=WAL")
                legacy.execute("CREATE TABLE facts(value TEXT)")
                legacy.execute("INSERT INTO facts VALUES('retained')")
                legacy.commit()
                self.assertTrue((directory / "journal.sqlite-wal").exists())
                storage = BudgetStore(directory, 128 * 1024)
                storage.journal(lambda db: db.execute("INSERT INTO facts VALUES('new')"))
                self.assertFalse((directory / "journal.sqlite-wal").exists())
                with sqlite3.connect((directory / "journal.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True) as db:
                    self.assertEqual(db.execute("SELECT value FROM facts").fetchall(), [("retained",), ("new",)])
                self.assertLessEqual(file_bytes(directory), storage.limit)
            finally:
                legacy.close()

    def test_oversize_event_truncation_and_spool_cannot_bypass_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            store = root / "recording"
            recorder = Recorder(workspace, store, policy=CapturePolicy(max_event_bytes=1024))
            try:
                event = recorder.append("provider.event", {"nested": {"raw": "x" * 10000}})
                self.assertTrue(event["payload"]["payload_truncated"])
                self.assertLessEqual(len(json.dumps({k: v for k, v in event.items() if k != "sequence"}).encode()), 1024)
                recorder.capture("baseline")
                completions = [r for r in read_history(store, recorder.session) if r["kind"] == "snapshot.completed"]
                self.assertEqual(completions[-1]["payload"]["commit"], recorder.history.head)
                self.assertEqual(json.loads((store / recorder.policy_file).read_text())["max_event_bytes"], 1024)
            finally:
                recorder.close()
            with self.assertRaisesRegex(ValueError, "spool must be outside"):
                Recorder(workspace, store, events=store / "spool")


if __name__ == "__main__":
    unittest.main()
