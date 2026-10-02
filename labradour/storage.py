"""Preflight staged writes against a hard retained recording-directory byte budget.

Temporary staging lives beside the store on the same filesystem. File sizes,
not filesystem allocation units, define the budget. A fixed health slot remains
writable after the journal/content budget is exhausted.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

HEALTH_BYTES = 4096


class BudgetExceeded(OSError):
    pass


def file_bytes(directory):
    return sum(p.stat().st_size for p in Path(directory).rglob("*") if p.is_file())


class BudgetStore:
    def __init__(self, directory, limit):
        self.directory, self.limit = Path(directory), limit
        self.stage_prefix = ".labradour-stage-" + hashlib.sha256(os.fsencode(str(self.directory.resolve()))).hexdigest()[:16] + "-"
        self.health_path = self.directory / "health.json"
        self.check({self.health_path: HEALTH_BYTES})
        if not self.health_path.exists():
            self.health({"status": "ready"})

    def check(self, sizes):
        # Count only growth: installation order cannot exceed this bound even
        # when another staged file shrinks. Existing data is never pruned here.
        growth = sum(max(0, size - (p.stat().st_size if p.exists() else 0)) for p, size in sizes.items())
        usage = file_bytes(self.directory)
        if usage + growth > self.limit:
            raise BudgetExceeded("storage budget exceeded (%d + %d > %d bytes); recording stopped" % (usage, growth, self.limit))

    @contextmanager
    def stage(self):
        with tempfile.TemporaryDirectory(prefix=self.stage_prefix, dir=str(self.directory.parent)) as temporary:
            yield Path(temporary)

    def cleanup_stages(self):
        # Call only while holding this store's writer lock. The hashed prefix
        # prevents touching another store's live staging directories.
        removed = []
        for path in self.directory.parent.glob(self.stage_prefix + "*"):
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
                continue
            shutil.rmtree(path)
            removed.append(path.name)
        return removed

    @staticmethod
    def sync_directory(directory):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def install(self, files):
        """Destination -> prepared source. Preflight the complete batch first."""
        self.check({dst: src.stat().st_size for dst, src in files.items()})
        # Objects go first, refs second, journal last. Interrupted batches are
        # recoverable using the preceding journal intent and retained ref.
        def order(item):
            path = item[0].relative_to(self.directory).as_posix()
            return (2 if path == "journal.sqlite" else 1 if "/refs/" in path else 0, path)
        for destination, source in sorted(files.items(), key=order):
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(source, destination)
            self.sync_directory(destination.parent)

    def health(self, payload):
        raw = json.dumps(payload).encode()
        if len(raw) > HEALTH_BYTES:
            raw = json.dumps({"status": payload.get("status"), "session_id": payload.get("session_id"),
                              "error": str(payload.get("error", ""))[:1000], "truncated": True}).encode()
        with self.stage() as stage:
            path = stage / "health.json"
            path.write_bytes(raw + b" " * (HEALTH_BYTES - len(raw)))
            # Its already-reserved size never grows, even after exhaustion.
            try:
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(path, self.health_path)
                self.sync_directory(self.directory)
            except OSError:
                self.health_in_place(raw)

    def health_in_place(self, raw):
        if isinstance(raw, dict):
            raw = json.dumps(raw).encode()
        raw = raw[:HEALTH_BYTES] + b" " * max(0, HEALTH_BYTES - len(raw))
        if not self.health_path.exists() or self.health_path.stat().st_size != HEALTH_BYTES:
            raise OSError("reserved health slot unavailable")
        with self.health_path.open("r+b", buffering=0) as stream:
            stream.write(raw)
            os.fsync(stream.fileno())

    def json_file(self, name, payload):
        with self.stage() as stage:
            source = stage / "payload.json"
            source.write_text(json.dumps(payload, indent=2))
            self.install({self.directory / name: source})

    def journal(self, callback, compact=False):
        """Build SQLite mutations outside the store and atomically replace it."""
        destination = self.directory / "journal.sqlite"
        with self.stage() as stage:
            path = stage / "journal.sqlite"
            db = sqlite3.connect(str(path))
            try:
                if destination.exists():
                    source = sqlite3.connect(destination.as_uri() + ("?mode=ro" if Path(str(destination) + "-wal").exists() else "?mode=ro&immutable=1"), uri=True)
                    try:
                        source.backup(db)
                    finally:
                        source.close()
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                db.execute("PRAGMA max_page_count=%d" % max(1, self.limit // page_size))
                with db:
                    result = callback(db)
                if compact:
                    db.execute("VACUUM")
            except sqlite3.OperationalError as exc:
                if "full" in str(exc):
                    raise BudgetExceeded("journal reached storage budget; recording stopped") from exc
                raise
            finally:
                db.close()
            self.install({destination: path})
            # Upgrade legacy WAL stores only after a successful journal install.
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(destination) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            return result

    def checkpoint(self, history, manifest, reason):
        from .snapshots import ScratchHistory
        with self.stage() as stage:
            shutil.copytree(history.repo, stage / "history.git")
            candidate = ScratchHistory(stage, session=history.ref.rsplit("/", 1)[-1], resume=True)
            commit = candidate.checkpoint(manifest, reason)
            files = self.git_files(stage)
            self.install(files)
            history.head, history.tree = candidate.head, candidate.tree
            return commit

    def git_files(self, stage):
        files = {}
        for source in (stage / "history.git").rglob("*"):
            if source.is_file():
                destination = self.directory / source.relative_to(stage)
                if not destination.exists() or ("objects" not in source.relative_to(stage).parts and
                                                destination.read_bytes() != source.read_bytes()):
                    files[destination] = source
        return files

    def init_history(self, session):
        from .snapshots import ScratchHistory
        repo = self.directory / "history.git"
        needs_init = not repo.exists()
        if not needs_init:
            current = ScratchHistory(self.directory, session=session, resume=True, storage=self)
            try:
                needs_init = current.git("rev-parse", "--is-bare-repository").strip() != b"true"
            except subprocess.CalledProcessError:
                needs_init = True
        if needs_init:
            with self.stage() as stage:
                if repo.exists():
                    shutil.copytree(repo, stage / "history.git")
                    candidate = ScratchHistory(stage, session=session, resume=True)
                    candidate.git("init", "--bare", str(candidate.repo))
                else:
                    ScratchHistory(stage, session=session)
                self.install(self.git_files(stage))
                for relative in ("objects/info", "objects/pack", "refs/heads", "refs/tags"):
                    (repo / relative).mkdir(parents=True, exist_ok=True)
        return ScratchHistory(self.directory, session=session, resume=True, storage=self)


def read_health(directory):
    path = Path(directory) / "health.json"
    if not path.exists():
        return {"status": "legacy recording; no storage health record"}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {"status": "unreadable-health", "error": "health update was interrupted or unavailable"}
