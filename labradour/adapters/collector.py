"""Launch-scoped local collection. ACK means fsynced spool, not journal commit."""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import tempfile
import threading
import time

from .events import identifier, validate

MAX_MESSAGE = 1024 * 1024
MAX_SPOOL_BYTES = 8 * 1024 * 1024
MAX_SPOOL_FILES = 128


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def signature(token, body):
    return hmac.new(token.encode(), encode(body), hashlib.sha256).hexdigest()


def unpack(envelope, token, launch_id, workspace_id):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("body"), dict):
        raise ValueError("invalid envelope")
    body = envelope["body"]
    mac = envelope.get("mac")
    if not isinstance(mac, str) or not hmac.compare_digest(mac, signature(token, body)):
        raise ValueError("authentication failed")
    if body.get("launch_id") != launch_id or body.get("workspace_id") != workspace_id:
        raise ValueError("launch/workspace mismatch")
    return validate(body.get("event"))


@contextmanager
def spool_lock(root):
    fd = os.open(str(root / "spool.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def retain(root, envelope):
    """Serialize parallel callback writes and reject an entire over-budget event."""
    raw = encode(envelope)
    if len(raw) > MAX_MESSAGE:
        raise ValueError("message limit")
    event_id = identifier(envelope["body"]["event"]["event_id"], "event_id")
    with spool_lock(root):
        destination = root / (event_id + ".json")
        if destination.exists():
            fd = os.open(str(destination), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as existing:
                if existing.read(MAX_MESSAGE + 1) != raw:
                    raise ValueError("conflicting event ID")
            return
        files = list(root.glob("*.json"))
        if len(files) >= MAX_SPOOL_FILES or sum(p.lstat().st_size for p in files) + len(raw) > MAX_SPOOL_BYTES:
            raise ValueError("spool limit")
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=str(root))
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(str(root), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def receive(connection):
    data = bytearray()
    deadline = time.monotonic() + .5
    while b"\n" not in data:
        connection.settimeout(max(.001, deadline - time.monotonic()))
        chunk = connection.recv(min(65536, MAX_MESSAGE + 1 - len(data)))
        if not chunk:
            raise ValueError("incomplete frame")
        data.extend(chunk)
        if len(data) > MAX_MESSAGE or time.monotonic() > deadline:
            raise ValueError("message limit/deadline")
    raw, remainder = bytes(data).split(b"\n", 1)
    if remainder:
        raise ValueError("one frame per connection")
    return json.loads(raw)


class Collector:
    def __init__(self, workspace, events):
        self.launch_id = secrets.token_hex(16)
        self.token = secrets.token_hex(32)
        self.workspace_id = hashlib.sha256(os.fsencode(str(Path(workspace).resolve()))).hexdigest()
        self.spool = Path(events) / ("adapter-" + self.launch_id)
        self.spool.mkdir(mode=0o700)
        # Short private path avoids macOS Unix socket path-length limits.
        self.temporary = tempfile.TemporaryDirectory(prefix="labradour-c-", dir="/tmp")
        self.path = str(Path(self.temporary.name) / "socket")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind(self.path)
            os.chmod(self.path, 0o600)
            self.socket.listen(16)
            self.socket.settimeout(.1)
        except Exception:
            self.socket.close()
            self.temporary.cleanup()
            raise
        self.stop = threading.Event()
        self.thread = None
        self.errors = 0
        self.reported_errors = 0

    def environment(self):
        return {"LABRADOUR_COLLECTOR_SOCKET": self.path, "LABRADOUR_COLLECTOR_TOKEN": self.token,
                "LABRADOUR_LAUNCH_ID": self.launch_id, "LABRADOUR_WORKSPACE_ID": self.workspace_id,
                "LABRADOUR_COLLECTOR_SPOOL": str(self.spool.resolve())}

    def start(self):
        # Caller must fork the PTY before starting any collector threads.
        self.thread = threading.Thread(target=self._serve, name="labradour-collector", daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    envelope = receive(connection)
                    unpack(envelope, self.token, self.launch_id, self.workspace_id)
                    retain(self.spool, envelope)
                    response = {"accepted": True, "durability": "spool"}
                except (ValueError, OSError, TypeError, RecursionError):
                    self.errors += 1
                    response = {"accepted": False, "error": "invalid, unauthorized, or spool full"}
                try:
                    connection.sendall(encode(response) + b"\n")
                except OSError:
                    pass

    def pending(self):
        result = []
        # Do not hold this file lock while calling the recorder.
        with spool_lock(self.spool):
            for path in sorted(self.spool.glob("*.json"))[:32]:
                try:
                    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as incoming:
                        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                            raise ValueError("spool entry must be a regular file")
                        raw = incoming.read(MAX_MESSAGE + 1)
                    if len(raw) > MAX_MESSAGE:
                        raise ValueError("message limit")
                    event = unpack(json.loads(raw), self.token, self.launch_id, self.workspace_id)
                    if path.name != event["event_id"] + ".json":
                        raise ValueError("event ID/path mismatch")
                    result.append((path, event))
                except (ValueError, OSError, TypeError, RecursionError):
                    self.errors += 1
                    if not path.is_dir():
                        path.unlink(missing_ok=True)
        return result

    def acknowledge(self, paths):
        with spool_lock(self.spool):
            for path in paths:
                path.unlink(missing_ok=True)

    def health(self):
        if self.errors == self.reported_errors:
            return None
        self.reported_errors = self.errors
        return {"launch_id": self.launch_id, "rejected": self.errors,
                "quality": "collector rejection; coverage may be incomplete"}

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        self.socket.close()
        self.temporary.cleanup()


def submit(event, environment=None):
    """Use a stable event_id for retries. No provider decision is returned."""
    env = os.environ if environment is None else environment
    event = validate(event)
    body = {"launch_id": env["LABRADOUR_LAUNCH_ID"], "workspace_id": env["LABRADOUR_WORKSPACE_ID"], "event": event}
    envelope = {"body": body, "mac": signature(env["LABRADOUR_COLLECTOR_TOKEN"], body)}
    raw = encode(envelope)
    if len(raw) + 1 > MAX_MESSAGE:
        raise ValueError("message limit")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            connection.connect(env["LABRADOUR_COLLECTOR_SOCKET"])
            connection.sendall(raw + b"\n")
            response = receive(connection)
        if not response.get("accepted"):
            raise ValueError("collector rejected event")
        return response
    except OSError:
        # Authenticate fallback files too; a legacy raw spool cannot bypass this.
        retain(Path(env["LABRADOUR_COLLECTOR_SPOOL"]), envelope)
        return {"accepted": True, "durability": "spool", "transport": "fallback"}
