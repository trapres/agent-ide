"""Explicit, local evidence exports. No opening, command replay or plugins.

Artifacts are disposable derived data in a separate bounded private cache.
Only completed single-file JSON artifacts are published in this first slice.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time
import uuid

from .visualizers import EvidenceReader

ARTIFACT_LIMIT = 64 * 1024 * 1024
CACHE_LIMIT = 128 * 1024 * 1024
MAX_ARTIFACTS = 32
MAX_AGE = 7 * 86400
TIMEOUT = 30
CHUNK = 64 * 1024
EXPORTER = 'labradour-evidence-json'
VERSION = '1'
ARTIFACT_NAME = re.compile(r'[0-9a-f]{64}\.json')
PENDING_NAME = re.compile(r'\.pending-[0-9a-f]{32}\.tmp')


class ExportError(ValueError):
    pass


def default_cache():
    return Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'labradour' / 'exports'


def cache_path(path=None, protected=()):
    candidate = Path(path) if path is not None else default_cache()
    if candidate.is_symlink():
        raise ExportError('export cache must not be a symlink')
    candidate = candidate.resolve()
    for root in protected:
        if root is None:
            continue
        root = Path(root).resolve()
        if candidate == root or root in candidate.parents or candidate in root.parents:
            raise ExportError('export cache must be separate from workspace and recording')
    return candidate


def chunks(value):
    # ASCII JSON also preserves non-UTF-8 path names escaped by surrogateescape.
    for piece in json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(',', ':')).iterencode(value):
        for offset in range(0, len(piece), CHUNK):
            yield piece[offset:offset + CHUNK].encode('ascii')


def digest(value, check=lambda: None):
    result = hashlib.sha256()
    size = 0
    for chunk in chunks(value):
        check()
        size += len(chunk)
        if size > ARTIFACT_LIMIT:
            raise ExportError('export exceeds artifact byte limit')
        result.update(chunk)
    return result.hexdigest()


class ArtifactCache:
    def __init__(self, path=None, protected=(), artifact_limit=ARTIFACT_LIMIT,
                 budget=CACHE_LIMIT, count=MAX_ARTIFACTS, age=MAX_AGE):
        self.path = cache_path(path, protected)
        if min(artifact_limit, budget, count, age) <= 0 or artifact_limit > budget:
            raise ExportError('invalid export cache limits')
        self.artifact_limit, self.budget, self.count, self.age = artifact_limit, budget, count, age

    @contextmanager
    def locked(self):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ExportError('export cache must be an owned private directory (mode 0700)')
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ExportError('export cache busy; retry when the other job finishes') from exc
            yield
            os.fsync(fd)
        finally:
            os.close(fd)

    def entries(self):
        result = []
        for path in self.path.iterdir():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ExportError('export cache contains an unsafe entry')
            if not ARTIFACT_NAME.fullmatch(path.name) and not PENDING_NAME.fullmatch(path.name):
                raise ExportError('export cache contains an unrecognized entry')
            result.append((path, info))
        return sorted(result, key=lambda entry: (entry[1].st_mtime_ns, entry[0].name))

    def clean(self, clear=False):
        removed = 0
        for path, info in self.entries():
            if clear or PENDING_NAME.fullmatch(path.name) or time.time() - info.st_mtime > self.age:
                path.unlink()
                removed += 1
        return removed

    def inspect(self):
        if not self.path.exists():
            return {'directory': str(self.path), 'bytes': 0, 'artifacts': []}
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ExportError('export cache must be an owned private directory (mode 0700)')
        entries = self.entries()
        return {'directory': str(self.path), 'bytes': sum(i.st_size for _, i in entries),
                'artifacts': [{'id': p.stem, 'bytes': i.st_size, 'path': str(p)}
                              for p, i in entries if ARTIFACT_NAME.fullmatch(p.name)]}

    def publish(self, core, check=lambda: None):
        key = digest(core, check)
        document = {'schema_version': 1, 'artifact': {'id': key, 'media_type': 'application/json',
                    'created_ns': time.time_ns()}, **core}
        total = 0
        for chunk in chunks(document):
            check()
            total += len(chunk)
            if total > self.artifact_limit:
                raise ExportError('export exceeds artifact byte limit')
        destination = self.path / (key + '.json')
        with self.locked():
            self.clean()
            entries = self.entries()
            while entries and (len(entries) > self.count or sum(i.st_size for _, i in entries) > self.budget):
                entries.pop(0)[0].unlink()
            if destination.exists():
                # Content-addressed reuse verifies the cached payload, not just its name.
                if destination.stat().st_size <= self.artifact_limit:
                    check()
                    try:
                        document = json.loads(destination.read_bytes())
                        if document['schema_version'] == 1 and document['artifact']['media_type'] == 'application/json' and document['artifact']['id'] == key and digest(
                                {k: document[k] for k in ('provenance', 'evidence')}, check) == key:
                            return self.result(destination, True, core['provenance'])
                    except ExportError:
                        raise
                    except (KeyError, ValueError, TypeError):
                        pass
                destination.unlink()
                entries = self.entries()
            while len(entries) >= self.count:
                entries.pop(0)[0].unlink()
            used = sum(info.st_size for _, info in entries)
            pending = self.path / ('.pending-' + uuid.uuid4().hex + '.tmp')
            size = 0
            try:
                fd = os.open(str(pending), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    for chunk in chunks(document):
                        check()
                        if size + len(chunk) > self.artifact_limit:
                            raise ExportError('export exceeds artifact byte limit')
                        while used + size + len(chunk) > self.budget and entries:
                            path, info = entries.pop(0)
                            path.unlink()
                            used -= info.st_size
                        if used + size + len(chunk) > self.budget:
                            raise ExportError('export cache byte budget exhausted')
                        output.write(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                check()
                os.replace(str(pending), str(destination))
                return self.result(destination, False, core['provenance'])
            finally:
                if pending.exists():
                    pending.unlink()

    def result(self, path, cached, provenance):
        # Hash exactly the published bytes for downstream inspection/opening.
        result = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(CHUNK), b''):
                result.update(chunk)
        return {'id': path.stem, 'path': str(path), 'media_type': 'application/json',
                'bytes': path.stat().st_size, 'sha256': result.hexdigest(), 'cached': cached,
                'selection_id': provenance.get('selection_id'), 'session_id': provenance.get('session_id'),
                'evidence_revision': provenance.get('evidence_revision')}


def evidence_export(directory, records, row, revision, cache, check=lambda: None):
    """Snapshot the explicitly selected row; a file picker grants one effect only."""
    check()
    cache_path(cache.path, [directory] + [r['payload']['workspace'] for r in records
        if r['kind'] == 'session.started' and isinstance(r['payload'].get('workspace'), str)])
    payload = row['payload']
    ids = set(payload.get('observations', [])) if row['kind'] == 'action' else set()
    observations = [r for r in records if r['event_id'] in ids]
    session = next((r.get('session_id') for r in records if r.get('session_id')), None)
    if len({r.get('session_id') for r in records if r.get('session_id')}) > 1:
        raise ExportError('export requires evidence from one session')
    evidence = {'selection': {'id': row['id'], 'kind': row['kind'], 'payload': payload},
                'observations': observations}
    if row['kind'] == 'effect':
        before, after = EvidenceReader(directory, records).effect(payload)
        check()
        for item in (before, after):
            if 'bytes' in item:
                item['content_base64'] = base64.b64encode(item.pop('bytes')).decode('ascii')
                item['content_encoding'] = 'base64'
        evidence.update(before=before, after=after)
    provenance = {'exporter': EXPORTER, 'exporter_version': VERSION, 'format': 'application/json',
                  'options': {}, 'recording_directory': str(Path(directory).resolve()),
                  'session_id': session, 'selection_id': row['id'], 'evidence_revision': revision,
                  'checkpoint': payload.get('checkpoint'), 'before_commit': payload.get('before_commit'),
                  'file_attribution': 'external-or-unknown',
                  'limitations': ['Derived export, not a new recording or proof of ownership.',
                                  'Candidate intervals do not establish exclusive causality.',
                                  'File contents retain capture policy and historical read limits.']}
    return cache.publish({'provenance': provenance, 'evidence': evidence}, check)


class ExportJob:
    """One explicit job, one result; selection changes never retarget its scope."""
    def __init__(self, cache=None, protected=()):
        self.cache = cache
        self.protected = protected
        self.pending = False
        self.results = queue.Queue(maxsize=1)
        self.cancelled = threading.Event()
        self.message = ''
        self.result = None

    def start(self, directory, records, row, revision):
        if self.pending:
            self.message = 'Export already running; wait or cancel with Ctrl-] c'
            return False
        if row is None:
            self.message = 'Export unavailable: select recorded evidence first'
            return False
        self.cancelled = threading.Event()
        cancelled = self.cancelled
        deadline = time.monotonic() + TIMEOUT
        copied = tuple(records)
        selected = {k: row[k] for k in ('id', 'kind', 'payload')}
        def check():
            if cancelled.is_set():
                raise ExportError('export cancelled')
            if time.monotonic() >= deadline:
                raise ExportError('export deadline exceeded')
        self.pending = True
        self.result = None
        self.message = 'Export running: ' + row['id']
        def work():
            try:
                cache = self.cache if isinstance(self.cache, ArtifactCache) else ArtifactCache(self.cache, self.protected)
                result = evidence_export(directory, copied, selected, revision, cache, check)
                self.results.put((result, 'Export ready' + (' (cached)' if result['cached'] else '') + ': ' + result['path']))
            except ExportError as exc:
                self.results.put((None, 'Export failed: ' + str(exc)))
            except Exception:
                self.results.put((None, 'Export failed: evidence/cache unavailable'))
        threading.Thread(target=work, name='labradour-export', daemon=True).start()
        return True

    def poll(self):
        try:
            self.result, self.message = self.results.get_nowait()
            self.pending = False
        except queue.Empty:
            pass

    def cancel(self):
        self.cancelled.set()
        if self.pending:
            self.message = 'Export cancellation requested'
