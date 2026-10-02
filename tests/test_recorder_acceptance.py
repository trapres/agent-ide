import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from labradour.recorder import Recorder, read_history
from labradour.storage import file_bytes


class RecorderAcceptanceTests(unittest.TestCase):
    def wait_capture(self, recorder, path, content, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = read_history(recorder.directory, recorder.session)
            commits = [r["payload"].get("commit") for r in events if r["kind"] == "snapshot.completed"]
            for commit in reversed(commits):
                if commit:
                    try:
                        if recorder.history.git("show", commit + ":" + path) == content:
                            return commit
                    except Exception:
                        pass
            time.sleep(.02)
        self.fail("capture missing for " + path + "; " + recorder.notice)

    def test_large_workspace_reuses_bytes_and_idle_reconciliation_does_not_grow_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            for index in range(400):
                (workspace / ("file-%04d" % index)).write_bytes(b"content" + str(index).encode())
            recorder = Recorder(workspace, root / "recording")
            try:
                recorder.capture("baseline")
                size = file_bytes(recorder.directory)
                recorder.capture("periodic reconciliation")
                self.assertEqual(recorder.metrics["read_files"], 0)
                self.assertEqual(recorder.metrics["cache_hits"], 400)
                self.assertEqual(file_bytes(recorder.directory), size)
                (workspace / "file-0000").write_bytes(b"new")
                recorder.capture("mutation")
                self.assertEqual(recorder.metrics["read_files"], 1)
                self.assertEqual(recorder.history.git("show", recorder.history.head + ":file-0000"), b"new")
                recorder.capture("watcher overflow")
                self.assertEqual(recorder.metrics["read_files"], 400)
            finally:
                recorder.close()

    def test_same_size_write_with_restored_mtime_invalidates_cached_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            source = workspace / "source"
            source.write_bytes(b"first")
            recorder = Recorder(workspace, root / "recording")
            try:
                recorder.capture("baseline")
                before = source.stat()
                source.write_bytes(b"other")
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                preview, _, _ = recorder.scan()
                self.assertEqual(preview["source"][1], b"other")
                self.assertEqual(recorder.manifest["source"][1], b"first")
                recorder.capture("mutation")
                self.assertEqual(recorder.metrics["read_files"], 1)
                self.assertEqual(recorder.history.git("show", recorder.history.head + ":source"), b"other")
                self.assertEqual(recorder.storage.usage, file_bytes(recorder.directory))
            finally:
                recorder.close()

    def test_batch_journal_has_one_transaction_deduplicates_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = Recorder(workspace, root / "recording")
            try:
                records = [("filesystem.observed", {"index": i}, "test", "event-%d" % i) for i in range(256)]
                with patch.object(recorder.storage, "journal", wraps=recorder.storage.journal) as journal:
                    recorder.append_many(records)
                    self.assertEqual(journal.call_count, 1)
                recorder.append_many(records)
                events = read_history(recorder.directory, recorder.session)
                self.assertEqual(len(events), 256)
                self.assertEqual([r["payload"]["index"] for r in events], list(range(256)))
                self.assertEqual([r["sequence"] for r in events], sorted(r["sequence"] for r in events))
            finally:
                recorder.close()

    def test_live_atomic_save_edit_revert_and_overflow_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "source").write_bytes(b"original")
            recorder = Recorder(workspace, root / "recording", backend="polling", interval=.2)
            try:
                recorder.start()
                baseline = recorder.history.head
                (workspace / "save.tmp").write_bytes(b"changed")
                (workspace / "save.tmp").replace(workspace / "source")
                changed = self.wait_capture(recorder, "source", b"changed")
                (workspace / "source").write_bytes(b"original")
                reverted = self.wait_capture(recorder, "source", b"original")
                # wait_capture can find the earlier baseline, so explicitly wait
                # for a post-change checkpoint with the restored content.
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    events = read_history(recorder.directory, recorder.session)
                    completions = [r["payload"] for r in events if r["kind"] == "snapshot.completed"]
                    if any(p["before_commit"] == changed and p["commit"] != changed for p in completions):
                        reverted = [p["commit"] for p in completions if p["before_commit"] == changed][-1]
                        break
                    time.sleep(.02)
                self.assertNotEqual(changed, reverted)
                self.assertEqual(recorder.history.diff(baseline, reverted), "")
                recorder.watcher.dropped += 1
                (workspace / "missed").write_bytes(b"reconciled")
                self.wait_capture(recorder, "missed", b"reconciled")
                self.assertTrue(any(r["kind"] == "collector.overflow" for r in read_history(recorder.directory, recorder.session)))
            finally:
                recorder.close()

    def test_concurrent_background_writers_final_state_and_transient_gap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = Recorder(workspace, root / "recording", backend="polling", interval=.15)
            recorder.start()
            def writer(name):
                for index in range(30):
                    (workspace / name).write_text(str(index))
                    time.sleep(.005)
            threads = [threading.Thread(target=writer, args=(name,)) for name in ("external", "agent")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            recorder.close()
            for name in ("external", "agent"):
                self.assertEqual(recorder.history.git("show", recorder.history.head + ":" + name), b"29")
            observations = [r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "filesystem.observed"]
            self.assertTrue(all(r["payload"]["actor"] == "external/unknown" for r in observations))
            # A create/delete between explicit snapshots is an expected fidelity
            # gap, rather than evidence that every write was captured.
            second = Recorder(workspace, root / "recording")
            try:
                second.capture("baseline")
                before = second.history.head
                (workspace / "transient").write_bytes(b"gone between scans")
                (workspace / "transient").unlink()
                second.capture("after transient")
                self.assertEqual(second.history.head, before)
            finally:
                second.close()

    def test_nested_repository_unusual_names_binary_and_modes_survive_incremental_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            nested = workspace / "nested"
            nested.mkdir()
            (nested / ".git").mkdir()
            (nested / ".git/secret").write_bytes(b"excluded")
            path = nested / "tabs\tand\nlines"
            path.write_bytes(b"\x00binary\r\n")
            (workspace / "link").symlink_to("nested/tabs\tand\nlines")
            recorder = Recorder(workspace, root / "recording")
            try:
                recorder.capture("baseline")
                path.chmod(0o755)
                recorder.capture("mode change")
                self.assertEqual(recorder.manifest["nested/tabs\tand\nlines"], ("100755", b"\x00binary\r\n"))
                self.assertEqual(recorder.manifest["link"][0], "120000")
                self.assertNotIn(b"excluded", recorder.history.git("cat-file", "--batch-all-objects", "--batch"))
            finally:
                recorder.close()


if __name__ == "__main__":
    unittest.main()
