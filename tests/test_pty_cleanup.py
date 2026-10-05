import signal
import unittest
from unittest.mock import patch
from labradour.pty_process import PtyProcess


class PtyCleanupTests(unittest.TestCase):
    def test_terminal_released_before_final_reap(self):
        child = PtyProcess.__new__(PtyProcess)
        child.pid, child.fd, child.status, child.gate = 12345, 999, None, None
        released = []
        def wait(pid, flags):
            return (pid, signal.SIGKILL) if released else (0, 0)
        with patch("labradour.pty_process.os.killpg"), patch("labradour.pty_process.os.kill"), \
             patch("labradour.pty_process.os.waitpid", side_effect=wait), \
             patch("labradour.pty_process.os.close", side_effect=lambda fd: released.append(fd)), \
             patch("labradour.pty_process.time.monotonic", side_effect=[0, 2, 3]):
            self.assertTrue(child.close())
        self.assertEqual(released, [999])

    def test_direct_child_is_killed_when_original_group_is_gone(self):
        child = PtyProcess.__new__(PtyProcess)
        child.pid, child.fd, child.status, child.gate = 12345, 999, None, None
        events = []
        def wait(pid, flags):
            events.append(("wait", flags))
            if ("kill", signal.SIGKILL) not in events:
                return (0, 0)
            return (pid, signal.SIGKILL)
        with patch("labradour.pty_process.os.killpg", side_effect=ProcessLookupError), \
             patch("labradour.pty_process.os.kill", side_effect=lambda pid, sig: events.append(("kill", sig))), \
             patch("labradour.pty_process.os.waitpid", side_effect=wait), \
             patch("labradour.pty_process.os.close") as close, \
             patch("labradour.pty_process.time.monotonic", side_effect=[0, 2, 3]):
            self.assertTrue(child.close())
        self.assertEqual(child.status, -signal.SIGKILL)
        self.assertIn(("kill", signal.SIGTERM), events)
        close.assert_called_once_with(999)

    def test_unreaped_child_does_not_block_shutdown(self):
        child = PtyProcess.__new__(PtyProcess)
        child.pid, child.fd, child.status, child.gate = 12345, 999, None, None
        with patch("labradour.pty_process.os.killpg"), patch("labradour.pty_process.os.kill"), \
             patch("labradour.pty_process.os.waitpid", return_value=(0, 0)) as wait, \
             patch("labradour.pty_process.os.close"), \
             patch("labradour.pty_process.time.monotonic", side_effect=[0, 2, 3, 5]):
            self.assertFalse(child.close())
        self.assertTrue(all(call.args[1] != 0 for call in wait.call_args_list))
