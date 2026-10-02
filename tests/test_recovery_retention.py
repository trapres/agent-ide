import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from labradour.recorder import Recorder, read_history
from labradour.retention import apply_retention, plan_retention
from labradour.storage import BudgetStore, file_bytes, read_health

PROJECT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def test_abrupt_process_loss_at_checkpoint_boundaries(self):
        code = '''
import os, sys
from pathlib import Path
from labradour.recorder import Recorder
workspace, store, boundary = map(str, sys.argv[1:])
r = Recorder(workspace, store)
r.capture("baseline")
Path(workspace, "source").write_text("changed")
original_append, original_scan, original_replace = r.append, r.scan, os.replace
def append(kind, payload, *args, **kwargs):
    result = original_append(kind, payload, *args, **kwargs)
    if payload.get("reason") == "mutation" and ((boundary == "intent" and kind == "snapshot.intent") or (boundary == "completion" and kind == "snapshot.completed")):
        os._exit(77)
    return result
def scan():
    result = original_scan()
    if boundary == "scan": os._exit(77)
    return result
def replace(source, destination):
    destination = str(destination)
    is_object = "/history.git/objects/" in destination
    if boundary == "before-object" and is_object: os._exit(77)
    original_replace(source, destination)
    if boundary == "after-object" and is_object: os._exit(77)
    if boundary == "ref" and "/refs/heads/sessions/" in destination: os._exit(77)
r.append, r.scan, os.replace = append, scan, replace
r.capture("mutation")
raise SystemExit("missing crash boundary")
'''
        for boundary in ("intent", "scan", "before-object", "after-object", "ref", "completion"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace, store = root / "workspace", root / "recording"
                workspace.mkdir()
                (workspace / "source").write_text("initial")
                result = subprocess.run([sys.executable, "-c", code, str(workspace), str(store), boundary], cwd=PROJECT, capture_output=True)
                self.assertEqual(result.returncode, 77, result.stderr.decode())
                old_session = read_history(store)[0]["id"]
                before_stages = list(root.glob(".labradour-stage-*"))
                recorder = Recorder(workspace, store)
                try:
                    events = read_history(store, old_session)
                    self.assertEqual(read_history(store)[0]["status"], "interrupted")
                    if boundary != "completion":
                        interrupted = [r for r in events if r["kind"] == "snapshot.interrupted"][-1]
                        self.assertEqual(interrupted["payload"]["checkpoint_installed"], boundary == "ref")
                    self.assertFalse(list(root.glob(".labradour-stage-*")))
                    recorder.capture("baseline")
                    self.assertEqual(recorder.history.git("show", recorder.history.head + ":source"), b"changed")
                    subprocess.check_call(["git", "--git-dir=" + str(store / "history.git"), "fsck", "--no-dangling"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                finally:
                    recorder.close()

    def test_partial_repository_initialization_is_repaired(self):
        code = """
import os, sys
from labradour.recorder import Recorder
original = os.replace
def replace(source, destination):
    original(source, destination)
    if str(destination).endswith("history.git/HEAD"): os._exit(80)
os.replace = replace
Recorder(sys.argv[1], sys.argv[2])
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store = root / "workspace", root / "recording"
            workspace.mkdir()
            process = subprocess.run([sys.executable, "-c", code, str(workspace), str(store)], cwd=PROJECT)
            self.assertEqual(process.returncode, 80)
            recorder = Recorder(workspace, store)
            try:
                recorder.capture("baseline")
                self.assertTrue(recorder.history.head)
                self.assertFalse(list(root.glob(".labradour-stage-*")))
            finally:
                recorder.close()

    def test_shutdown_completion_before_status_update_recovers_as_completed(self):
        code = '''
import os, sys
from labradour.recorder import Recorder
r = Recorder(sys.argv[1], sys.argv[2])
r.capture("baseline")
original = r.append
def append(kind, payload, *args, **kwargs):
    result = original(kind, payload, *args, **kwargs)
    if kind == "session.completed": os._exit(78)
    return result
r.append = append
r.close()
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            store = root / "recording"
            result = subprocess.run([sys.executable, "-c", code, str(workspace), str(store)], cwd=PROJECT)
            self.assertEqual(result.returncode, 78)
            session = read_history(store)[0]["id"]
            recorder = Recorder(workspace, store)
            try:
                self.assertEqual(read_history(store)[0]["status"], "completed")
                self.assertFalse(any(r["kind"] == "session.interrupted" for r in read_history(store, session)))
            finally:
                recorder.close()

    def test_disk_full_stops_recording_persists_health_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            store = root / "recording"
            recorder = Recorder(workspace, store)
            recorder.capture("baseline")
            before = recorder.history.head
            (workspace / "a").write_text("later")
            with patch.object(recorder.storage, "stage", side_effect=OSError(errno.ENOSPC, "disk full")):
                recorder.capture("mutation")
                self.assertTrue(recorder.suspended)
                self.assertEqual(recorder.history.head, before)
                self.assertEqual(read_health(store)["status"], "storage-failure")
                self.assertIn("disk full", read_health(store)["error"])
                recorder.close()
            later = Recorder(workspace, store)
            try:
                later.capture("baseline")
                self.assertEqual(later.history.git("show", later.history.head + ":a"), b"later")
            finally:
                later.close()

    def test_ref_sync_failure_is_recovered_even_when_in_memory_head_is_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store = root / "workspace", root / "recording"
            workspace.mkdir()
            (workspace / "source").write_text("initial")
            recorder = Recorder(workspace, store)
            recorder.capture("baseline")
            session, before = recorder.session, recorder.history.head
            (workspace / "source").write_text("changed")
            original = recorder.storage.sync_directory
            def fail_ref(directory):
                if str(directory).endswith("refs/heads/sessions"):
                    raise OSError(errno.ENOSPC, "ref sync failed")
                return original(directory)
            with patch.object(recorder.storage, "sync_directory", side_effect=fail_ref):
                recorder.capture("mutation")
            self.assertTrue(recorder.suspended)
            self.assertEqual(recorder.history.head, before)
            recorder.close()
            later = Recorder(workspace, store)
            try:
                interrupted = [r for r in read_history(store, session) if r["kind"] == "snapshot.interrupted"][-1]
                commit = interrupted["payload"]["recoverable_commit"]
                self.assertNotEqual(commit, before)
                self.assertEqual(later.history.git("show", commit + ":source"), b"changed")
            finally:
                later.close()

    def test_sigkill_releases_writer_and_cleans_owned_staging(self):
        code = """
import json, sys
from labradour.recorder import Recorder
r = Recorder(sys.argv[1], sys.argv[2])
r.capture("baseline")
r.append("snapshot.intent", {"reason": "pending", "previous_commit": r.history.head})
with r.storage.stage() as stage:
    (stage / "unfinished").write_text("pending")
    print(json.dumps({"session": r.session}), flush=True)
    sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store = root / "workspace", root / "recording"
            workspace.mkdir()
            process = subprocess.Popen([sys.executable, "-c", code, str(workspace), str(store)],
                                       cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready = json.loads(process.stdout.readline())
                process.kill()
                process.wait(timeout=5)
                self.assertEqual(process.returncode, -9)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()
            self.assertTrue(list(root.glob(".labradour-stage-*")))
            recorder = Recorder(workspace, store)
            try:
                self.assertFalse(list(root.glob(".labradour-stage-*")))
                self.assertEqual(read_history(store)[0]["status"], "interrupted")
                self.assertTrue(any(r["kind"] == "snapshot.interrupted" for r in read_history(store, ready["session"])))
            finally:
                recorder.close()

    def test_read_failure_retains_content_and_labels_partial_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "source").write_text("original")
            recorder = Recorder(workspace, root / "recording")
            try:
                recorder.capture("baseline")
                before = recorder.history.head
                original = os.open
                def fail_source(name, *args, **kwargs):
                    if name == "source":
                        raise PermissionError(errno.EACCES, "unreadable")
                    return original(name, *args, **kwargs)
                with patch("labradour.recorder.os.open", side_effect=fail_source):
                    recorder.capture("interrupted read")
                self.assertEqual(recorder.history.head, before)
                event = [r for r in read_history(recorder.directory, recorder.session) if r["kind"] == "snapshot.completed"][-1]
                self.assertEqual(event["payload"]["quality"], "partial")
                self.assertEqual(event["payload"]["changes"], [])
                self.assertEqual(event["payload"]["issues"][0]["path"], "source")
            finally:
                recorder.close()


class RetentionTests(unittest.TestCase):
    def make_sessions(self, root, count=3):
        workspace, store = root / "workspace", root / "recording"
        workspace.mkdir()
        result = []
        for index in range(count):
            (workspace / "unique").write_bytes(os.urandom(8192))
            (workspace / "shared").write_text("shared across sessions")
            recorder = Recorder(workspace, store)
            recorder.capture("baseline")
            result.append((recorder.session, recorder.history.head))
            recorder.close()
        return workspace, store, result

    def test_preview_apply_reclaims_and_preserves_retained_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store, sessions = self.make_sessions(root)
            before = file_bytes(store)
            preview = plan_retention(store, 1)
            self.assertEqual(set(preview["remove"]), {s[0] for s in sessions[:-1]})
            self.assertEqual(len(read_history(store)), 3)
            result = apply_retention(store, 1)
            self.assertEqual([s["id"] for s in read_history(store)], [sessions[-1][0]])
            self.assertLess(file_bytes(store), before)
            self.assertTrue(result["results"][0]["loose_objects_removed"])
            git = ["git", "--git-dir=" + str(store / "history.git")]
            self.assertEqual(subprocess.check_output(git + ["show", sessions[-1][1] + ":shared"]), b"shared across sessions")
            for session, commit in sessions[:-1]:
                self.assertFalse((store / ("capture-policy-" + session + ".json")).exists())
                self.assertNotEqual(subprocess.run(git + ["cat-file", "-e", commit], capture_output=True).returncode, 0)
            self.assertEqual(apply_retention(store, 1)["results"], [])

    def test_writer_lock_running_session_and_other_store_stages_are_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store, sessions = self.make_sessions(root, 1)
            active = Recorder(workspace, store)
            other = root / "other"
            other.mkdir()
            other_storage = BudgetStore(other, 131072)
            try:
                with other_storage.stage() as stage:
                    (stage / "must-stay").write_text("other writer")
                    with self.assertRaisesRegex(ValueError, "active writer"):
                        apply_retention(store, 1)
                    active.storage.cleanup_stages()
                    self.assertTrue((stage / "must-stay").exists())
                    self.assertIn(active.session, plan_retention(store, 1)["kept"])
            finally:
                active.close()

    def test_abrupt_prune_resume_after_journal_ref_and_gc_boundaries(self):
        code = '''
import os, sys
from labradour.retention import apply_retention
from labradour.storage import BudgetStore
from pathlib import Path
boundary = sys.argv[2]
original_journal, original_unlink = BudgetStore.journal, Path.unlink
def journal(self, callback, **kwargs):
    result = original_journal(self, callback, **kwargs)
    if kwargs.get("compact") and boundary == "journal": os._exit(79)
    return result
def unlink(path, *args, **kwargs):
    original_unlink(path, *args, **kwargs)
    path = str(path)
    if boundary == "ref" and "/refs/heads/sessions/" in path: os._exit(79)
    if boundary == "object" and "/history.git/objects/" in path: os._exit(79)
BudgetStore.journal, Path.unlink = journal, unlink
apply_retention(sys.argv[1], 1)
raise SystemExit("missing boundary")
'''
        for boundary in ("journal", "ref", "object"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace, store, sessions = self.make_sessions(root)
                process = subprocess.run([sys.executable, "-c", code, str(store), boundary], cwd=PROJECT, capture_output=True)
                self.assertEqual(process.returncode, 79, process.stderr.decode())
                self.assertEqual(read_health(store)["operation"], "prune")
                recorder = Recorder(workspace, store)
                try:
                    self.assertNotIn("operation", read_health(store))
                    self.assertEqual(len(read_history(store)), 2)
                    self.assertEqual(recorder.history.git("show", sessions[-1][1] + ":shared"), b"shared across sessions")
                    self.assertFalse(list(root.glob(".labradour-stage-*")))
                    recorder.capture("baseline")
                finally:
                    recorder.close()

    def test_failed_prune_is_resumable_and_packed_history_stays_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store, sessions = self.make_sessions(root)
            git = ["git", "--git-dir=" + str(store / "history.git")]
            subprocess.check_call(git + ["repack", "-ad"], stdout=subprocess.DEVNULL)
            subprocess.check_call(git + ["pack-refs", "--all", "--prune"])
            with patch.object(BudgetStore, "journal", side_effect=OSError(errno.ENOSPC, "disk full")):
                with self.assertRaises(OSError):
                    apply_retention(store, 1)
            self.assertEqual(len(read_history(store)), 3)
            self.assertEqual(read_health(store)["operation"], "prune")
            result = apply_retention(store, 1)
            self.assertTrue(result["resumed"])
            self.assertEqual(len(read_history(store)), 1)
            self.assertEqual(subprocess.check_output(git + ["show", sessions[-1][1] + ":shared"]), b"shared across sessions")
            self.assertIn("preserved", result["resumed"]["packed_gc"])
            subprocess.check_call(git + ["fsck", "--no-dangling"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_retained_journal_reference_protects_cross_session_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, store, sessions = self.make_sessions(root, 1)
            active = Recorder(workspace, store)
            active.capture("baseline")
            active.append("bookmark", {"commit": sessions[0][1]})
            active.close()
            apply_retention(store, 1)
            git = ["git", "--git-dir=" + str(store / "history.git")]
            self.assertEqual(subprocess.run(git + ["cat-file", "-e", sessions[0][1]], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
