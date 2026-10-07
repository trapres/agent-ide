"""Short advisory evidence leases; directory locks require no recording writes.

Readers never pin sessions for the lifetime of a UI. Cooperating maintenance
fails promptly and can be retried after the current evidence read finishes.
"""
from contextlib import contextmanager
from functools import wraps
import fcntl
import os
from pathlib import Path
import threading

_held = threading.local()


class EvidenceBusy(ValueError):
    pass


@contextmanager
def evidence_lease(directory, exclusive=False):
    path = str(Path(directory).resolve())
    held = getattr(_held, 'paths', {})
    if path in held:
        if exclusive and not held[path]:
            raise EvidenceBusy('cannot upgrade an evidence read lease')
        yield
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as exc:
            raise EvidenceBusy('recording evidence busy; retry after readers/maintenance finish') from exc
        _held.paths = held
        held[path] = exclusive
        try:
            yield
        finally:
            del held[path]
    finally:
        os.close(fd)


def maintenance_lease(function):
    @wraps(function)
    def guarded(storage, *args, **kwargs):
        with evidence_lease(storage.directory, exclusive=True):
            return function(storage, *args, **kwargs)
    return guarded
