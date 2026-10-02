"""Preflight staged writes against a hard retained recording-directory byte budget.

Temporary staging lives beside the store on the same filesystem. File sizes,
not filesystem allocation units, define the budget. A fixed health slot remains
writable after the journal/content budget is exhausted.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

HEALTH_BYTES = 4096


class BudgetExceeded(OSError):
    pass


def file_bytes(directory):
    return sum(p.stat().st_size for p in Path(directory).rglob("*") if p.is_file())


class BudgetStore:
    def __init__(self, directory, limit):
        self.directory, self.limit = Path(directory), limit
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
        with tempfile.TemporaryDirectory(prefix=".labradour-stage-", dir=str(self.directory.parent)) as temporary:
            yield Path(temporary)

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
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(path, self.health_path)
            self.sync_directory(self.directory)

    def json_file(self, name, payload):
        with self.stage() as stage:
            source = stage / "payload.json"
            source.write_text(json.dumps(payload, indent=2))
            self.install({self.directory / name: source})

    def journal(self, callback):
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
        if not (self.directory / "history.git").exists():
            with self.stage() as stage:
                ScratchHistory(stage, session=session)
                self.install(self.git_files(stage))
                # Git requires these directories even before the first object/ref.
                for relative in ("objects/info", "objects/pack", "refs/heads", "refs/tags"):
                    (self.directory / "history.git" / relative).mkdir(parents=True, exist_ok=True)
        return ScratchHistory(self.directory, session=session, resume=True, storage=self)


def read_health(directory):
    path = Path(directory) / "health.json"
    return json.loads(path.read_text()) if path.exists() else {"status": "legacy recording; no storage health record"}
