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
                 max_capture=64 * 1024 * 1024, quota=512 * 1024 * 1024, interval=2, events=None, policy=None,
                 adapter_collector=None, adapter_provider=None):
        self.adapter_collector = adapter_collector
        self.adapter_provider = adapter_provider
        self.adapter_counts = {}
        self.adapter_gaps = 0
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
            self.storage.cleanup_stages()
            from .retention import resume_prune
            resume_prune(self.storage)
            self.suspended = False
            self.storage.journal(lambda db: db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started_ns INTEGER, status TEXT);
                CREATE TABLE IF NOT EXISTS retention(id TEXT PRIMARY KEY, completed_ns INTEGER, removed_sessions TEXT);
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
            self.signatures = {}
            self.force_read = True
            self.scan_count = 0
            self.last_metadata = []
            self.metrics = {"scan_files": 0, "read_files": 0, "read_bytes": 0, "cache_hits": 0,
                            "captures": 0, "watcher_queue": 0, "watcher_dropped": 0}
            self.notice = "recorder starting"
            self.history = None
            self.dropped = 0
            self.failed = False
            self._recover()
            if (self.directory / "history.git").exists():
                from .retention import reclaim_loose_objects
                from .leases import EvidenceBusy
                try:
                    reclaim_loose_objects(self.storage)
                except EvidenceBusy:
                    # Reclamation is optional; recording need not wait for a viewer.
                    pass
                self.storage.refresh_usage()
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
        self.last_error = exc
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

    def storage_failed(self, exc):
        self.suspended, self.failed = True, True
        self.last_error = exc
        self.notice = "RECORDING STOPPED: storage failure; agent continues"
        health = {"status": "storage-failure", "session_id": self.session, "error": str(exc)[:1000],
                  "budget_bytes": self.storage.limit, "last_commit": self.history.head if self.history else None}
        try:
            self.storage.health(health)
        except OSError:
            try:
                self.storage.health_in_place(health)
            except OSError:
                self.notice += " | health could not be persisted"
        try:
            self.output.put_nowait({"kind": "storage.failed", "event_id": uuid.uuid4().hex,
                                   "received_ns": time.time_ns(), "payload": health})
        except queue.Full:
            pass

    def append(self, kind, payload, source="recorder", event_id=None):
        records = self.append_many([(kind, payload, source, event_id)])
        return records[0] if records else None

    def append_many(self, events):
        if self.suspended:
            return []
        prepared = [self.prepare(kind, payload, source, event_id) for kind, payload, source, event_id in events]
        if not prepared:
            return []
        def insert(db):
            result = []
            for record, raw in prepared:
                cursor = db.execute("INSERT OR IGNORE INTO events(event_id, session_id, kind, received_ns, record) VALUES(?,?,?,?,?)",
                                    (record["event_id"], self.session, record["kind"], record["received_ns"], raw))
                result.append((cursor.rowcount, cursor.lastrowid))
            return result
        try:
            inserted = self.mutate(insert)
        except BudgetExceeded as exc:
            self.limit_reached(exc)
            return []
        except (OSError, sqlite3.OperationalError) as exc:
            self.storage_failed(exc)
            return []
        for (record, _), (count, sequence) in zip(prepared, inserted):
            if count:
                record["sequence"] = sequence
                try:
                    self.output.put_nowait(record)
                except queue.Full:
                    self.notice = "UI queue full; durable events retained; use history command"
        return [record for record, _ in prepared]

    def prepare(self, kind, payload, source, event_id):
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
            if kind == "adapter.event":
                essential = ("kind", "provider", "launch_id", "content_digest")
            elif kind == "adapter.boundary":
                essential = ("adapter_event_id", "launch_id", "phase", "completed", "status", "checkpoint", "snapshot_event_id", "quality")
            record["payload"] = {k: payload[k] for k in essential if k in payload and
                                 isinstance(payload[k], (str, int, bool, type(None))) and len(str(payload[k])) <= 256}
            record["payload"].update(payload_truncated=True, original_bytes=len(raw.encode()))
            raw = json.dumps(record)
        return record, raw

    def _recover(self):
        for (session,) in self.db.execute("SELECT id FROM sessions WHERE status='running'").fetchall():
            self.session = session
            history = self.storage.init_history(session)
            events = [json.loads(row[0]) for row in self.db.execute("SELECT record FROM events WHERE session_id=? ORDER BY sequence", (session,))]
            # A completed lifecycle event may have reached disk just before
            # the final sessions-table update was interrupted.
            terminal = next((r for r in reversed(events) if r["kind"] == "session.completed"), None)
            if terminal:
                status = "completed-with-gaps" if terminal["payload"].get("quality") == "capture gaps" else "completed"
                self.mutate(lambda db: db.execute("UPDATE sessions SET status=? WHERE id=?", (status, session)))
                continue
            finished = {r["payload"].get("intent") for r in events if r["kind"] in
                        ("snapshot.completed", "snapshot.failed", "snapshot.interrupted")}
            for record in events:
                if record["kind"] == "snapshot.intent" and record["event_id"] not in finished:
                    result = self.append("snapshot.interrupted", {"intent": record["event_id"], "recoverable_commit": history.head,
                                "previous_commit": record["payload"].get("previous_commit"),
                                "checkpoint_installed": history.head != record["payload"].get("previous_commit"),
                                "quality": "ref retained; capture completion was interrupted"})
                    if result is None:
                        raise self.last_error
            result = self.append("session.interrupted", {"last_commit": history.head, "quality": "crash gap; workspace will be reconciled in a new session"})
            if result is None:
                raise self.last_error
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
        signatures = {}
        metrics = {"scan_files": 0, "read_files": 0, "read_bytes": 0, "cache_hits": 0}
        started = time.perf_counter()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
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
                    metrics["scan_files"] += 1
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
                        cached = self.manifest.get(path)
                        if not self.force_read and cached is not None and self.signatures.get(path) == signature(before):
                            content, mode = cached[1], cached[0]
                            manifest[path] = mode, content
                            signatures[path] = signature(before)
                            total += len(content)
                            metrics["cache_hits"] += 1
                            continue
                        content = None
                        for _ in range(3):
                            read_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                            with os.fdopen(read_fd, "rb") as stream:
                                first = os.fstat(stream.fileno())
                                if not stat.S_ISREG(first.st_mode):
                                    raise OSError("file type changed during capture")
                                candidate = stream.read(self.max_file + 1)
                                metrics["read_files"] += 1
                                metrics["read_bytes"] += len(candidate)
                                last = os.fstat(stream.fileno())
                            after = os.stat(name, dir_fd=fd, follow_symlinks=False)
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
                    signatures[path] = signature(before)
                except OSError as exc:
                    issues.append({"path": path, "error": str(exc)})
                    retain(path)
        self.scanned_signatures = signatures
        self.force_read = False
        self.metrics.update(metrics, scan_ms=round((time.perf_counter() - started) * 1000, 3))
        return manifest, metadata, issues

    def capture(self, reason, force=False):
        if self.suspended:
            if reason == "baseline":
                raise getattr(self, "last_error", BudgetExceeded("baseline cannot be captured within storage budget"))
            return
        capture_started = time.perf_counter()
        self.scan_count += 1
        self.force_read = force or reason in ("baseline", "startup reconciliation", "session end", "watcher overflow") or self.scan_count % 15 == 0
        if reason == "periodic reconciliation":
            preview = self.scan()
            if preview[0] == self.manifest and preview[1] == self.last_metadata and not preview[2]:
                self.signatures = self.scanned_signatures
                return
        else:
            preview = None
        intent = self.append("snapshot.intent", {"reason": reason, "previous_commit": self.history.head})
        if intent is None:
            if reason == "baseline":
                raise getattr(self, "last_error", BudgetExceeded("baseline cannot be captured within storage budget"))
            return
        started = time.time_ns()
        try:
            manifest, metadata, issues = preview if preview is not None else self.scan()
            omitted = {m["path"] for m in metadata if m["type"] != "directory"}
            changes = [{"path": p, "operation": "file.create" if p not in self.manifest else
                        "file.omitted" if p in omitted else "file.delete" if p not in manifest else "file.modify"}
                       for p in sorted(set(manifest) | set(self.manifest)) if manifest.get(p) != self.manifest.get(p)]
            before = self.history.head
            commit = self.history.head if manifest == self.manifest and self.history.head else self.history.checkpoint(manifest, reason)
            self.manifest = manifest
            self.signatures = self.scanned_signatures
            self.last_metadata = metadata
            self.metrics["captures"] += 1
            self.metrics["capture_prepare_ms"] = round((time.perf_counter() - capture_started) * 1000, 3)
            completed = self.append("snapshot.completed", {"intent": intent["event_id"], "reason": reason,
                        "before_commit": before, "commit": commit, "tree": self.history.tree,
                        "scan_started_ns": started, "scan_finished_ns": time.time_ns(),
                        "quality": "partial" if issues or any(m["type"] != "directory" for m in metadata) else "observed-live-state",
                        "changes": changes, "metadata": metadata, "issues": issues, "metrics": {k: v for k, v in self.metrics.items() if k != "capture_ms"}})
            self.metrics["capture_ms"] = round((time.perf_counter() - capture_started) * 1000, 3)
            if self.suspended:
                if reason == "baseline":
                    raise getattr(self, "last_error", BudgetExceeded("baseline completion exceeded storage budget"))
                return
            self.notice = "recording %s | %s" % (self.session[:8], "partial capture: %d omissions, %d issues" % (len(omitted), len(issues)) if issues or omitted else "checkpoint " + commit[:8])
            return completed
        except BudgetExceeded as exc:
            self.limit_reached(exc)
            if reason == "baseline":
                raise
        except (OSError, sqlite3.OperationalError) as exc:
            self.storage_failed(exc)
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
        if self.adapter_collector:
            from .adapters.events import coverage
            self.append("adapter.foundation", {"launch_id": self.adapter_collector.launch_id,
                        "coverage": coverage(), "transport": "authenticated-unix-socket"})
            if self.adapter_provider:
                self.append("adapter.configured", {"provider": self.adapter_provider, "status": "awaiting-delivery",
                            "coverage": "native categories remain unverified", "boundary_timeout_ms": 1500})
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

    def collect_adapters(self):
        collector = self.adapter_collector
        if not collector or self.suspended:
            return
        pending = collector.pending()
        from .adapters.collector import encode
        batch = []
        boundaries = []
        for _, event in pending:
            event_id = "adapter:" + collector.launch_id + ":" + event["event_id"]
            digest = hashlib.sha256(encode(event)).hexdigest()
            prior = self.db.execute("SELECT record FROM events WHERE event_id=?", (event_id,)).fetchone()
            if prior and json.loads(prior[0])["payload"].get("content_digest") != digest:
                batch.append(("collector.conflict", {"adapter_event_id": event["event_id"],
                              "launch_id": collector.launch_id, "quality": "conflicting retry; original fact retained"},
                              "authenticated-collector", None))
                self.failed = True
            else:
                batch.append(("adapter.event", dict(event, launch_id=collector.launch_id, content_digest=digest),
                              "authenticated-collector", event_id))
                if not prior:
                    name = event["payload"].get("hook_event_name", event["kind"])
                    self.adapter_counts[name] = self.adapter_counts.get(name, 0) + 1
                    if event["kind"] == "adapter.health":
                        self.failed = True
                        self.adapter_gaps += 1
                if event.get("boundary"):
                    boundaries.append(event)
        records = self.append_many(batch)
        if len(records) == len(pending) and not self.suspended:
            for event in boundaries:
                boundary_id = "boundary:" + collector.launch_id + ":" + event["event_id"]
                prior = self.db.execute("SELECT record FROM events WHERE event_id=?", (boundary_id,)).fetchone()
                if prior:
                    result = json.loads(prior[0])["payload"]
                else:
                    completed = self.capture("adapter boundary " + event["boundary"], force=True)
                    result = {"adapter_event_id": event["event_id"], "launch_id": collector.launch_id, "phase": event["boundary"],
                              "completed": completed is not None, "status": "captured" if completed else "unavailable",
                              "checkpoint": completed["payload"].get("commit") if completed else None,
                              "snapshot_event_id": completed["event_id"] if completed else None,
                              "quality": completed["payload"].get("quality") if completed else "capture gap",
                              "attribution": "boundary-correlated; live scan may overlap other writers"}
                    saved = self.append("adapter.boundary", result, "recorder", boundary_id)
                    if saved is None:
                        result = dict(result, completed=False, status="unavailable")
                        self.failed = True
                if not result.get("completed"):
                    self.adapter_gaps += 1
                    self.failed = True
                collector.finish(event["event_id"], result)
        # ACK the spool only after the single writer committed this whole batch.
        if len(records) == len(pending) and not self.suspended:
            collector.acknowledge([path for path, _ in pending])
        health = collector.health()
        if health:
            self.failed = True
            self.append("collector.rejected", health)

    def _run(self):
        next_scan = time.monotonic() + self.interval
        try:
            while not self.stop.wait(.02):
                if self.suspended:
                    break
                self.collect_hooks()
                self.collect_adapters()
                records = self.watcher.drain()
                self.metrics.update(watcher_queue=self.watcher.events.qsize(), watcher_dropped=self.watcher.dropped)
                for record in records:
                    path = record["payload"]["path"]
                    self.signatures.pop(path, None)
                    if record["payload"].get("destination"):
                        self.signatures.pop(record["payload"]["destination"], None)
                self.append_many([("filesystem.observed", dict(r["payload"], observed_ns=r["received_ns"]),
                                   "watchdog", r["event_id"]) for r in records])
                overflow = self.watcher.dropped != self.dropped
                if overflow:
                    self.dropped = self.watcher.dropped
                    self.append("collector.overflow", {"dropped": self.dropped, "quality": "observation gap"})
                if records or overflow or time.monotonic() >= next_scan:
                    self.capture("watcher overflow" if overflow else "mutation batch" if records else "periodic reconciliation")
                    next_scan = time.monotonic() + self.interval
        except (OSError, sqlite3.OperationalError) as exc:
            self.storage_failed(exc)
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
            if getattr(self, "cleanup_gap", None):
                self.append("process.cleanup-gap", self.cleanup_gap)
            if self.history and not self.suspended:
                self.collect_hooks()
                # Collector is stopped by the host before shutdown drain.
                for _ in range(4):
                    self.collect_adapters()
                if self.adapter_provider:
                    self.append("adapter.delivery", {"provider": self.adapter_provider, "counts": self.adapter_counts,
                                "gaps": self.adapter_gaps, "status": "observed" if self.adapter_counts else "no-delivery",
                                "coverage": "observed callbacks do not establish full provider coverage"})
                    if not self.adapter_counts:
                        self.failed = True
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
        except (OSError, sqlite3.OperationalError) as exc:
            self.storage_failed(exc)
        finally:
            try:
                self.watcher.close()
            finally:
                self.db.close()
                self.lock.close()


def read_history(directory, session=None, record_limit=None, byte_limit=None):
    database = Path(directory).resolve() / "journal.sqlite"
    with closing(sqlite3.connect(database.as_uri() + ("?mode=ro" if Path(str(database) + "-wal").exists() else "?mode=ro&immutable=1"), uri=True)) as db:
        if session is None:
            sessions = [dict(id=r[0], started_ns=r[1], status=r[2]) for r in db.execute("SELECT * FROM sessions ORDER BY started_ns")]
            health = read_health(directory)
            if health.get("recording_health"):
                health = health["recording_health"]
            for session in sessions:
                if session["id"] == health.get("session_id") and health.get("status") in ("storage-limit", "storage-failure"):
                    session["status"] = health["status"]
            return sessions
        result = []
        bytes_read = 0
        for sequence, raw in db.execute("SELECT sequence, record FROM events WHERE session_id=? ORDER BY sequence", (session,)):
            bytes_read += len(raw.encode('utf-8'))
            if ((record_limit is not None and len(result) >= record_limit)
                    or (byte_limit is not None and bytes_read > byte_limit)):
                raise ValueError('session exceeds journal read limits; export selected rows instead')
            record = json.loads(raw)
            record["sequence"] = sequence
            result.append(record)
        return result
