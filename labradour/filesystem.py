"""Watchdog feasibility collector: observations, not a durable content journal."""
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver


class WorkspaceWatch(FileSystemEventHandler):
    def __init__(self, workspace, excluded=(), backend="native", capacity=4096, included=None):
        self.root = Path(workspace).resolve()
        self.policy_included = included
        self.excluded = [Path(p).resolve() for p in excluded]
        self.events = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.drop_lock = threading.Lock()
        observer_type = Observer
        if sys.platform == "darwin":
            # FSEvents streams cannot initialize in some macOS sandboxes.
            # Kqueue is another native watchdog backend; no silent polling.
            from watchdog.observers.kqueue import KqueueObserver
            observer_type = KqueueObserver
        self.observer = PollingObserver(timeout=.1) if backend == "polling" else observer_type(timeout=.1)
        self.backend = type(self.observer).__name__
        self.started = False

    def included_path(self, path):
        if not path:
            return None
        # Do not resolve event paths: that would follow symlinks out of scope.
        absolute = Path(os.path.abspath(os.fsdecode(path)))
        try:
            relative = absolute.relative_to(self.root)
        except ValueError:
            return None
        internal = (".git",) if self.policy_included else (".git", ".venv", "__pycache__", ".agentide-spike")
        if not relative.parts or any(p in internal for p in relative.parts):
            return None
        if self.policy_included and not self.policy_included(relative):
            return None
        if any(absolute == p or p in absolute.parents for p in self.excluded):
            return None
        return str(relative)

    def on_any_event(self, event):
        if event.event_type not in ("created", "modified", "deleted", "moved"):
            return
        source = self.included_path(event.src_path)
        destination = self.included_path(getattr(event, "dest_path", None))
        kind = event.event_type
        if kind == "moved":
            if source is None and destination is not None:
                kind, source = "created", destination
            elif source is not None and destination is None:
                kind = "deleted"
        if source is None:
            return
        payload = {"hook_event_name": "filesystem.observed", "actor": "external/unknown",
                   "operation": ("directory." if event.is_directory else "file.") + {
                       "created": "create", "modified": "modify", "deleted": "delete", "moved": "rename"}[kind],
                   "path": source, "backend": self.backend, "quality": "watcher-observed"}
        if kind == "moved":
            payload["destination"] = destination
        record = {"schema_version": 1, "event_id": uuid.uuid4().hex,
                  "received_ns": time.time_ns(), "source": "watchdog", "payload": payload}
        try:
            self.events.put_nowait(record)
        except queue.Full:
            with self.drop_lock:
                self.dropped += 1

    def start(self):
        self.observer.schedule(self, str(self.root), recursive=True)
        try:
            self.observer.start()
        except Exception:
            self.observer.stop()
            raise
        self.started = True

    def drain(self, limit=256):
        result = []
        for _ in range(limit):
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        return result

    def close(self):
        if self.started:
            self.observer.stop()
            self.observer.join(timeout=3)
            if self.observer.is_alive():
                raise RuntimeError("watchdog observer did not stop")
            self.started = False


def filesystem_probe(workspace, backend="native", timeout=5):
    """Mutate only a caller-provided fixture directory; wait between operations."""
    root = Path(workspace)
    if root.exists() and any(root.iterdir()):
        raise ValueError("watch fixture requires an empty directory")
    root.mkdir(parents=True, exist_ok=True)
    watch = WorkspaceWatch(root, backend=backend)
    records = []
    measured = {}
    def wait_for(operation, path):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records.extend(watch.drain())
            if any(r["payload"]["operation"] == operation and r["payload"]["path"] == path for r in records):
                return True
            time.sleep(.02)
        return False
    try:
        watch.start()
        original = root / "probe.txt"
        original.write_text("initial\n")
        measured["create"] = wait_for("file.create", "probe.txt")
        records.clear()
        original.write_text("changed\n")
        measured["modify"] = wait_for("file.modify", "probe.txt")
        records.clear()
        destination = root / "renamed.txt"
        original.rename(destination)
        measured["rename"] = wait_for("file.rename", "probe.txt")
        records.clear()
        destination.unlink()
        measured["delete"] = wait_for("file.delete", "renamed.txt")
        return {"backend": watch.backend, "observed": measured, "dropped": watch.dropped}
    finally:
        watch.close()
