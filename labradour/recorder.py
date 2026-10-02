"""Single-writer local journal and immutable workspace checkpoint recorder."""
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import stat
import threading
import time
import uuid

from .filesystem import WorkspaceWatch
from .snapshots import ScratchHistory
from .policy import CapturePolicy, DEFAULT_EXCLUDED
from .storage import BudgetExceeded, BudgetStore, read_health

EXCLUDED = DEFAULT_EXCLUDED


class Recorder:
    def __init__(self, workspace, directory, excluded=(), backend="native", max_file=8 * 1024 * 1024,
                 max_capture=64 * 1024 * 1024, quota=512 * 1024 * 1024, interval=2, events=None, policy=None):
        self.policy = policy or CapturePolicy(max_file_bytes=max_file, max_capture_bytes=max_capture,
                                              storage_budget_bytes=quota)
        self.workspace = Path(workspace).resolve()
        self.directory = Path(directory).resolve()
        if self.directory == self.workspace or self.directory in self.workspace.parents:
            raise ValueError("recording directory must not contain the workspace")
        if events and (Path(events).resolve() == self.directory or self.directory in Path(events).resolve().parents):
            raise ValueError("hook spool must be outside the recording directory to enforce its budget")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.lock = open(self.directory / "writer.lock", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError("recording directory already has an active writer")
        self.db = None
        try:
            self.storage = BudgetStore(self.directory, self.policy.storage_budget_bytes)
            self.suspended = False
            self.storage.journal(lambda db: db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started_ns INTEGER, status TEXT);
                CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL,
                    received_ns INTEGER NOT NULL, record TEXT NOT NULL);
            '''))
            self._connect()
            identity = str(self.workspace)
            old = self.db.execute("SELECT value FROM metadata WHERE key='workspace'").fetchone()
            if old and old[0] != identity:
                raise ValueError("recording belongs to a different workspace")
            self.mutate(lambda db: db.execute("INSERT OR IGNORE INTO metadata VALUES('workspace', ?)", (identity,)))
            self.session = uuid.uuid4().hex
            self.workspace_id = hashlib.sha256(os.fsencode(identity)).hexdigest()
            self.excluded = [Path(p).resolve() for p in [self.directory, *excluded]]
            self.watcher = WorkspaceWatch(self.workspace, excluded=self.excluded, backend=backend, included=self.included)
            self.max_file, self.max_capture, self.interval = self.policy.max_file_bytes, self.policy.max_capture_bytes, interval
            self.events = Path(events) if events else None
            self.seen = {p.name for p in self.events.glob("*.json")} if self.events else set()
            self.output = queue.Queue(maxsize=4096)
            self.stop = threading.Event()
            self.thread = None
            self.manifest = {}
            self.notice = "recorder starting"
            self.history = None
            self.dropped = 0
            self.failed = False
            self._recover()
            if self.suspended:
                raise BudgetExceeded("recovery requires a larger storage budget")
            self.mutate(lambda db: db.execute("INSERT INTO sessions VALUES(?, ?, 'running')", (self.session, time.time_ns())))
            self.history = self.storage.init_history(self.session)
            self.policy_file = "capture-policy-" + self.session + ".json"
            self.storage.json_file(self.policy_file, self.policy.describe(self.workspace, self.excluded))
            self.storage.health({"status": "recording", "session_id": self.session, "budget_bytes": self.storage.limit})
        except Exception:
            if self.db is not None:
                self.db.close()
            self.lock.close()
            raise

    def _connect(self):
        self.db = sqlite3.connect((self.directory / "journal.sqlite").as_uri() + "?mode=ro&immutable=1",
                                  uri=True, check_same_thread=False)

    def mutate(self, callback):
        if self.db:
            self.db.close()
        try:
            return self.storage.journal(callback)
        finally:
            if (self.directory / "journal.sqlite").exists():
                self._connect()

    def limit_reached(self, exc):
        first = not self.suspended
        self.suspended, self.failed = True, True
        self.notice = "RECORDING STOPPED: storage budget; agent continues"
        health = {"status": "storage-limit", "session_id": self.session, "error": str(exc),
                  "budget_bytes": self.storage.limit, "last_commit": self.history.head if self.history else None}
        self.storage.health(health)
        if first:
            try:
                self.output.put_nowait({"kind": "storage.limit", "event_id": uuid.uuid4().hex,
                                       "received_ns": time.time_ns(), "payload": health,
                                       "persisted_in": "health.json"})
            except queue.Full:
                pass

    def append(self, kind, payload, source="recorder", event_id=None):
        if self.suspended:
            return None
        record = {"schema_version": 1, "event_id": event_id or uuid.uuid4().hex,
                  "workspace_id": self.workspace_id, "session_id": self.session,
                  "received_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
                  "kind": kind, "source": source, "payload": payload}
        if not isinstance(record["event_id"], str) or not record["event_id"] or len(record["event_id"]) > 256:
            raise ValueError("invalid event ID")
        raw = json.dumps(record)
        if len(raw.encode()) > self.policy.max_event_bytes:
            record["payload"] = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool, type(None)))
                                 and len(str(v)) < 4096}
            record["payload"]["payload_truncated"] = True
            raw = json.dumps(record)
        if len(raw.encode()) > self.policy.max_event_bytes:
            essential = ("intent", "commit", "before_commit", "tree", "reason", "quality", "capture_policy_file")
            record["payload"] = {k: payload[k] for k in essential if k in payload and
                                 isinstance(payload[k], (str, int, bool, type(None))) and len(str(payload[k])) <= 256}
            record["payload"].update(payload_truncated=True, original_bytes=len(raw.encode()))
            raw = json.dumps(record)
        def insert(db):
            cursor = db.execute("INSERT OR IGNORE INTO events(event_id, session_id, kind, received_ns, record) VALUES(?,?,?,?,?)",
                                (record["event_id"], self.session, kind, record["received_ns"], raw))
            return cursor.rowcount, cursor.lastrowid
        try:
            count, sequence = self.mutate(insert)
        except BudgetExceeded as exc:
            self.limit_reached(exc)
            return None
        if count:
            record["sequence"] = sequence
            try:
                self.output.put_nowait(record)
            except queue.Full:
                self.notice = "UI queue full; durable events retained; use history command"
        return record

    def _recover(self):
        for (session,) in self.db.execute("SELECT id FROM sessions WHERE status='running'").fetchall():
            self.session = session
            history = self.storage.init_history(session)
            events = [json.loads(row[0]) for row in self.db.execute("SELECT record FROM events WHERE session_id=? ORDER BY sequence", (session,))]
            finished = {r["payload"].get("intent") for r in events if r["kind"] in
                        ("snapshot.completed", "snapshot.failed", "snapshot.interrupted")}
            for record in events:
                if record["kind"] == "snapshot.intent" and record["event_id"] not in finished:
                    self.append("snapshot.interrupted", {"intent": record["event_id"], "recoverable_commit": history.head,
                                "quality": "ref retained; capture completion was interrupted"})
            self.append("session.interrupted", {"last_commit": history.head, "quality": "crash gap; workspace will be reconciled in a new session"})
            self.mutate(lambda db: db.execute("UPDATE sessions SET status='interrupted' WHERE id=?", (session,)))
        self.session = uuid.uuid4().hex

    def included(self, path):
        if not self.policy.included(path):
            return False
        absolute = self.workspace / path
        return not any(absolute == p or p in absolute.parents for p in self.excluded)

    def scan(self):
        manifest, metadata, issues = {}, [], []
        total = 0
        def retain(path):
            nonlocal total
            if path not in self.manifest:
                return
            mode, content = self.manifest[path]
            if len(content) <= self.max_file and total + len(content) <= self.max_capture:
                manifest[path] = mode, content
                total += len(content)
            else:
                metadata.append({"path": path, "type": "metadata-only", "size": len(content),
                                 "reason": "max_capture_bytes"})
        # A failed subtree scan retains previous content rather than fabricating deletions.
        def walk_error(exc):
            relative = os.path.relpath(exc.filename, self.workspace)
            issues.append({"path": relative, "error": type(exc).__name__})
            for path in sorted(self.manifest):
                if relative == "." or path == relative or path.startswith(relative + "/"):
                    retain(path)
        for directory, dirs, files, fd in os.fwalk(self.workspace, follow_symlinks=False, onerror=walk_error):
            relative_dir = Path(directory).relative_to(self.workspace)
            dirs[:] = sorted(d for d in dirs if self.included(relative_dir / d))
            for name in list(dirs) + sorted(files):
                relative = relative_dir / name
                if not self.included(relative):
                    continue
                path = str(relative)
                try:
                    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(before.st_mode):
                        metadata.append({"path": path, "type": "directory"})
                        continue
                    if self.policy.metadata(relative):
                        metadata.append({"path": path, "type": "metadata-only", "size": before.st_size,
                                         "reason": "policy.metadata_only"})
                        continue
                    if stat.S_ISLNK(before.st_mode):
                        content, mode = os.fsencode(os.readlink(name, dir_fd=fd)), "120000"
                    elif stat.S_ISREG(before.st_mode):
                        if before.st_size > self.max_file or total + before.st_size > self.max_capture:
                            metadata.append({"path": path, "type": "metadata-only", "size": before.st_size,
                                             "reason": "max_file_bytes" if before.st_size > self.max_file else "max_capture_bytes"})
                            continue
                        content = None
                        for _ in range(3):
                            read_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                            with os.fdopen(read_fd, "rb") as stream:
                                first = os.fstat(stream.fileno())
                                if not stat.S_ISREG(first.st_mode):
                                    raise OSError("file type changed during capture")
                                candidate = stream.read(self.max_file + 1)
                                last = os.fstat(stream.fileno())
                            after = os.stat(name, dir_fd=fd, follow_symlinks=False)
                            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
                            if signature(first) == signature(last) == signature(after) and len(candidate) <= self.max_file:
                                content = candidate
                                before = after
                                break
                        if content is None:
                            raise OSError("unstable read")
                        mode = "100755" if before.st_mode & 0o111 else "100644"
                    else:
                        metadata.append({"path": path, "type": "unsupported"})
                        continue
                    if len(content) > self.max_file or total + len(content) > self.max_capture:
                        metadata.append({"path": path, "type": "metadata-only", "size": len(content),
                                         "reason": "max_file_bytes" if len(content) > self.max_file else "max_capture_bytes"})
                        continue
                    total += len(content)
                    manifest[path] = mode, content
                except OSError as exc:
                    issues.append({"path": path, "error": str(exc)})
                    retain(path)
        return manifest, metadata, issues

    def capture(self, reason):
        if self.suspended:
            if reason == "baseline":
                raise BudgetExceeded("baseline cannot be captured within storage budget")
            return
        intent = self.append("snapshot.intent", {"reason": reason, "previous_commit": self.history.head})
        if intent is None:
            if reason == "baseline":
                raise BudgetExceeded("baseline cannot be captured within storage budget")
            return
        started = time.time_ns()
        try:
            manifest, metadata, issues = self.scan()
            omitted = {m["path"] for m in metadata if m["type"] != "directory"}
            changes = [{"path": p, "operation": "file.create" if p not in self.manifest else
                        "file.omitted" if p in omitted else "file.delete" if p not in manifest else "file.modify"}
                       for p in sorted(set(manifest) | set(self.manifest)) if manifest.get(p) != self.manifest.get(p)]
            before = self.history.head
            commit = self.history.head if manifest == self.manifest and self.history.head else self.history.checkpoint(manifest, reason)
            self.manifest = manifest
            self.append("snapshot.completed", {"intent": intent["event_id"], "reason": reason,
                        "before_commit": before, "commit": commit, "tree": self.history.tree,
                        "scan_started_ns": started, "scan_finished_ns": time.time_ns(),
                        "quality": "partial" if issues or any(m["type"] != "directory" for m in metadata) else "observed-live-state",
                        "changes": changes, "metadata": metadata, "issues": issues})
            if self.suspended:
                if reason == "baseline":
                    raise BudgetExceeded("baseline completion exceeded storage budget")
                return
            self.notice = "recording %s | %s" % (self.session[:8], "partial capture: %d omissions, %d issues" % (len(omitted), len(issues)) if issues or omitted else "checkpoint " + commit[:8])
        except BudgetExceeded as exc:
            self.limit_reached(exc)
            if reason == "baseline":
                raise
        except Exception as exc:
            self.failed = True
            self.append("snapshot.failed", {"intent": intent["event_id"], "reason": reason, "error": str(exc)})
            self.notice = "snapshot failed: " + str(exc)
            if reason == "baseline":
                raise

    def start(self):
        self.watcher.start()
        self.append("session.started", {"workspace": str(self.workspace), "backend": self.watcher.backend,
                    "capture_policy": self.policy.describe(self.workspace, self.excluded),
                    "capture_policy_file": self.policy_file})
        self.capture("baseline")
        self.capture("startup reconciliation")
        self.thread = threading.Thread(target=self._run, name="labradour-recorder", daemon=True)
        self.thread.start()

    def collect_hooks(self):
        if not self.events:
            return
        for path in self.events.glob("*.json"):
            if path.name in self.seen:
                continue
            try:
                if path.stat().st_size > self.policy.max_event_bytes:
                    raise ValueError("hook payload limit exceeded")
                record = json.loads(path.read_text())
                if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                    raise ValueError("invalid hook event")
                self.append("provider.event", record["payload"], "hook-spool", record.get("event_id"))
            except (ValueError, OSError) as exc:
                self.append("collector.invalid", {"path": path.name, "error": str(exc)})
            self.seen.add(path.name)

    def _run(self):
        next_scan = time.monotonic() + self.interval
        try:
            while not self.stop.wait(.1):
                if self.suspended:
                    break
                self.collect_hooks()
                records = self.watcher.drain()
                for record in records:
                    self.append("filesystem.observed", record["payload"], "watchdog", record["event_id"])
                overflow = self.watcher.dropped != self.dropped
                if overflow:
                    self.dropped = self.watcher.dropped
                    self.append("collector.overflow", {"dropped": self.dropped, "quality": "observation gap"})
                if records or overflow or time.monotonic() >= next_scan:
                    self.capture("watcher overflow" if overflow else "mutation batch" if records else "periodic reconciliation")
                    next_scan = time.monotonic() + self.interval
        except Exception as exc:
            self.failed = True
            self.notice = "recorder stopped: " + str(exc)
            try:
                self.append("collector.failed", {"error": str(exc)})
            except Exception:
                pass

    def drain(self):
        records = []
        for _ in range(256):
            try:
                records.append(self.output.get_nowait())
            except queue.Empty:
                break
        return records

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        try:
            if self.history and not self.suspended:
                self.collect_hooks()
                for record in self.watcher.drain(4096):
                    self.append("filesystem.observed", record["payload"], "watchdog", record["event_id"])
                self.capture("session end")
                self.append("session.completed", {"commit": self.history.head, "quality": "capture gaps" if self.failed else "observed-live-state"})
                if not self.suspended:
                    self.mutate(lambda db: db.execute("UPDATE sessions SET status=? WHERE id=?",
                                ("completed-with-gaps" if self.failed else "completed", self.session)))
                    self.storage.health({"status": "completed", "session_id": self.session, "budget_bytes": self.storage.limit})
        except BudgetExceeded as exc:
            self.limit_reached(exc)
        finally:
            try:
                self.watcher.close()
            finally:
                self.db.close()
                self.lock.close()


def read_history(directory, session=None):
    database = Path(directory).resolve() / "journal.sqlite"
    with closing(sqlite3.connect(database.as_uri() + ("?mode=ro" if Path(str(database) + "-wal").exists() else "?mode=ro&immutable=1"), uri=True)) as db:
        if session is None:
            sessions = [dict(id=r[0], started_ns=r[1], status=r[2]) for r in db.execute("SELECT * FROM sessions ORDER BY started_ns")]
            health = read_health(directory)
            for session in sessions:
                if session["id"] == health.get("session_id") and health.get("status") == "storage-limit":
                    session["status"] = "storage-limit"
            return sessions
        result = []
        for sequence, raw in db.execute("SELECT sequence, record FROM events WHERE session_id=? ORDER BY sequence", (session,)):
            record = json.loads(raw)
            record["sequence"] = sequence
            result.append(record)
        return result
