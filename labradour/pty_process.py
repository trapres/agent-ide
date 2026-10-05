"""PTY ownership, nonblocking IO, window resizing, and process-group cleanup."""
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time


class PtyProcess:
    def __init__(self, command, cwd, rows=24, columns=80, env=None, paused=False):
        gate_read, gate_write = os.pipe() if paused else (None, None)
        self.gate = gate_write
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                if paused:
                    os.close(gate_write)
                    if os.read(gate_read, 1) != b"1":
                        os._exit(125)
                    os.close(gate_read)
                os.chdir(cwd)
                # Set dimensions before exec, not just after the first render.
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
                child_env = dict(os.environ if env is None else env)
                child_env["TERM"] = "xterm-256color"
                os.execvpe(command[0], command, child_env)
            except Exception as exc:
                os.write(2, ("Labradour launch failed: %s\r\n" % exc).encode())
                os._exit(127)
        if paused:
            os.close(gate_read)
        os.set_blocking(self.fd, False)
        self.status = None
        self.eof = False
        self.pending = bytearray()
        self.size = None
        self.resize(rows, columns)

    def release(self):
        if self.gate is not None:
            os.write(self.gate, b"1")
            os.close(self.gate)
            self.gate = None

    def resize(self, rows, columns):
        size = (max(1, rows), max(1, columns))
        if size != self.size:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
            self.size = size

    def send(self, data):
        if self.eof or self.status is not None:
            return
        self.pending.extend(data)
        self.flush()

    def flush(self):
        if not self.pending or self.eof:
            return
        try:
            sent = os.write(self.fd, self.pending)
            del self.pending[:sent]
        except BlockingIOError:
            pass
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                self.eof = True
            else:
                raise

    def read(self):
        chunks = []
        # Bound each repaint's work so a noisy command cannot starve input.
        for _ in range(16):
            if not select.select([self.fd], [], [], 0)[0]:
                break
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EIO:
                    data = b""
                else:
                    raise
            if not data:
                self.eof = True
                break
            chunks.append(data)
        return b"".join(chunks)

    def poll(self):
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status

    def close(self):
        if self.gate is not None:
            os.close(self.gate)
            self.gate = None
        try:
            # Descendants may outlive the direct child; signal its group too.
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # A native launcher can change its process group. Always terminate the
        # owned direct child as well, before waiting for it.
        if self.poll() is None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 1
        while self.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        # Release the controlling terminal before the final reap. On macOS,
        # a native process at its trust dialog can finish dying only once the
        # master closes; waiting with it open produces a false cleanup gap.
        os.close(self.fd)
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 1
            while self.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
        return self.status is not None
