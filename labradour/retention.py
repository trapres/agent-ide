"""Resumable whole-session retention. Journal removal precedes Git reclamation."""
from contextlib import contextmanager, closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import shutil
import time
import uuid

from .snapshots import ScratchHistory
from .storage import BudgetStore, file_bytes, read_health


@contextmanager
def writer_lock(directory):
    directory = Path(directory).resolve()
    if not (directory / "journal.sqlite").is_file():
        raise ValueError("not a recording directory")
    with (directory / "writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("recording directory already has an active writer")
        yield directory


def connect(directory):
    return sqlite3.connect((Path(directory) / "journal.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)


def plan_retention(directory, keep_sessions=10):
    if type(keep_sessions) is not int or keep_sessions < 1:
        raise ValueError("keep_sessions must be at least 1")
    directory = Path(directory).resolve()
    with closing(connect(directory)) as db:
        sessions = [dict(id=r[0], started_ns=r[1], status=r[2]) for r in db.execute(
            "SELECT id, started_ns, status FROM sessions ORDER BY started_ns DESC, id DESC")]
    # A running status can be left by a crash. Apply takes the exclusive lock;
    # preview protects it until recovery has explicitly marked it interrupted.
    protected = {s["id"] for s in sessions[:keep_sessions]} | {s["id"] for s in sessions if s["status"] == "running"}
    removed = [s["id"] for s in sessions if s["id"] not in protected]
    return {"keep_sessions": keep_sessions, "kept": [s["id"] for s in sessions if s["id"] in protected],
            "remove": removed, "bytes_before": file_bytes(directory), "apply_required": True,
            "pending_prune": read_health(directory).get("operation") == "prune"}


def journal_roots(directory, excluded_sessions=()):
    keys = {"commit", "before_commit", "previous_commit", "recoverable_commit", "last_commit"}
    roots = set()
    with closing(connect(directory)) as db:
        for session, raw in db.execute("SELECT session_id, record FROM events"):
            if session in excluded_sessions:
                continue
            record = json.loads(raw)
            if not (record["kind"].startswith("snapshot.") or record["kind"].startswith("session.") or
                    record["kind"] in ("bookmark", "user.bookmark")):
                continue
            payload = record.get("payload", {})
            for key in keys:
                value = payload.get(key)
                if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value):
                    roots.add(value)
    return sorted(roots)


def reclaim_loose_objects(storage):
    """Packed objects stay intact; sharing between sessions is never broken."""
    history = ScratchHistory(storage.directory, session="maintenance", resume=True)
    roots = journal_roots(storage.directory)
    reachable = {line.split()[0].decode() for line in history.git("rev-list", "--objects", "--all", *roots).splitlines()}
    removed, reclaimed = 0, 0
    objects = history.repo / "objects"
    for prefix in objects.iterdir():
        if prefix.is_symlink() or not prefix.is_dir() or len(prefix.name) != 2 or any(c not in "0123456789abcdef" for c in prefix.name):
            continue
        for path in prefix.iterdir():
            oid = prefix.name + path.name
            if path.is_symlink() or not path.is_file() or len(oid) != 40 or any(c not in "0123456789abcdef" for c in oid):
                continue
            if oid not in reachable:
                reclaimed += path.stat().st_size
                path.unlink()
                storage.sync_directory(prefix)
                removed += 1
    return {"loose_objects_removed": removed, "object_bytes_reclaimed": reclaimed,
            "packed_gc": "packs preserved; compaction is deferred"}


def resume_prune(storage):
    operation = read_health(storage.directory)
    if operation.get("operation") != "prune":
        return None
    targets = operation.get("sessions")
    if not isinstance(targets, list) or len(targets) > 32 or any(not isinstance(s, str) or len(s) != 32 or
            any(c not in "0123456789abcdef" for c in s) for s in targets):
        raise ValueError("invalid pending retention operation")
    # Reject a damaged plan before removing any facts.
    with closing(connect(storage.directory)) as db:
        running = {r[0] for r in db.execute("SELECT id FROM sessions WHERE status='running'")}
    if running.intersection(targets):
        raise ValueError("pending retention operation includes a running session")
    if (storage.directory / "history.git").exists():
        verifier = ScratchHistory(storage.directory, session="maintenance", resume=True)
        verifier.git("rev-list", "--objects", "--all", *journal_roots(storage.directory, targets))
    def remove_facts(db):
        for session in targets:
            db.execute("DELETE FROM events WHERE session_id=?", (session,))
            db.execute("DELETE FROM sessions WHERE id=?", (session,))
        db.execute("CREATE TABLE IF NOT EXISTS retention(id TEXT PRIMARY KEY, completed_ns INTEGER, removed_sessions TEXT)")
        db.execute("INSERT OR IGNORE INTO retention VALUES(?,?,?)", (operation["operation_id"], operation["started_ns"], json.dumps(targets)))
    storage.journal(remove_facts, compact=True)
    # Delete packed refs in staging, then remove loose refs directly. No
    # live Git lock/temp file can overshoot the retained storage budget.
    if (storage.directory / "history.git").exists():
        with storage.stage() as stage:
            shutil.copytree(storage.directory / "history.git", stage / "history.git")
            history = ScratchHistory(stage, session="maintenance", resume=True)
            for session in targets:
                history.git("update-ref", "-d", "refs/heads/sessions/" + session)
            storage.install(storage.git_files(stage))
        for session in targets:
            ref = storage.directory / "history.git" / "refs/heads/sessions" / session
            if ref.is_symlink():
                raise ValueError("session ref must not be a symlink")
            if ref.is_file():
                ref.unlink()
                storage.sync_directory(ref.parent)
    for session in targets:
        policy = storage.directory / ("capture-policy-" + session + ".json")
        if policy.is_file() and not policy.is_symlink():
            policy.unlink()
    storage.sync_directory(storage.directory)
    reclaimed = reclaim_loose_objects(storage) if (storage.directory / "history.git").exists() else {}
    storage.refresh_usage()
    result = {"status": "pruned", "removed_sessions": targets, "operation_id": operation["operation_id"],
              "bytes_after": file_bytes(storage.directory), "budget_bytes": storage.limit,
              "recording_health": operation.get("recording_health"), **reclaimed}
    storage.health(result)
    return result


def apply_retention(directory, keep_sessions=10):
    if type(keep_sessions) is not int or keep_sessions < 1:
        raise ValueError("keep_sessions must be at least 1")
    with writer_lock(directory) as directory:
        health = read_health(directory)
        # Cleanup must also work at an exhausted quota. It never increases the
        # allowed retained budget just to compact existing data.
        budget = health.get("budget_bytes", max(65536, file_bytes(directory)))
        recording_health = health.get("recording_health") if health.get("status") in ("pruned", "maintenance") else health
        storage = BudgetStore(directory, max(budget, file_bytes(directory)))
        cleaned = storage.cleanup_stages()
        resumed = resume_prune(storage)
        plan = plan_retention(directory, keep_sessions)
        results = []
        for offset in range(0, len(plan["remove"]), 32):
            targets = plan["remove"][offset:offset + 32]
            storage.health({"status": "maintenance", "operation": "prune", "operation_id": uuid.uuid4().hex,
                            "started_ns": time.time_ns(), "sessions": targets, "budget_bytes": storage.limit,
                            "recording_health": recording_health})
            results.append(resume_prune(storage))
        return {**plan, "apply_required": False, "results": results, "resumed": resumed,
                "stages_removed": cleaned, "bytes_after": file_bytes(directory)}
