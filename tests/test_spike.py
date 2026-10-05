import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import unicodedata

from labradour.hooks import emit
from labradour.input import InputRouter
from labradour.layout import layout
from labradour.pty_process import PtyProcess
from labradour.snapshots import ScratchHistory, fixture
from labradour.terminal import Terminal
from labradour.filesystem import WorkspaceWatch, filesystem_probe


ROOT = Path(__file__).resolve().parents[1]


def wait_for(child, needle, timeout=5):
    output = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output.extend(child.read())
        if needle in output:
            return bytes(output)
        time.sleep(.02)
    raise AssertionError("missing %r in %r" % (needle, bytes(output)[-2000:]))


class GeometryTests(unittest.TestCase):
    def test_mirror_preserves_three_pane_dimensions(self):
        left = layout(40, 141)
        right = layout(40, 141, side="right")
        for name in left:
            self.assertEqual(left[name].content_size, right[name].content_size)
        self.assertEqual(left["agent"].width + left["activity"].width, 141)
        self.assertEqual(left["activity"].height + left["visualization"].height, 38)
        self.assertEqual(right["agent"].x, right["activity"].width)

    def test_small_and_maximized_layouts(self):
        self.assertEqual(layout(1, 1), {})
        self.assertEqual(set(layout(24, 80, focus="activity")), {"activity"})
        self.assertEqual(set(layout(40, 140, focus="visualization", maximized=True)), {"visualization"})


class TerminalTests(unittest.TestCase):
    def test_delete_lines_moves_sparse_blank_rows_without_stale_text(self):
        terminal = Terminal(4, 10)
        terminal.feed(b"stale\x1b[1;1H\x1b[M")
        self.assertEqual(terminal.display, [" " * 10] * 4)
        terminal.feed(b"\x1b[2;3r\x1b[2;1Hinside\x1b[2;1H\x1b[M")
        self.assertEqual(terminal.display, [" " * 10] * 4)

    def test_cursor_color_query_and_fragmented_unicode(self):
        terminal = Terminal(6, 20)
        terminal.feed(b"\x1b[2;4H\x1b[1;31m")
        text = "界é".encode()
        terminal.feed(text[:1])
        terminal.feed(text[1:])
        self.assertEqual(terminal.grid[1][3].text, "界")
        self.assertEqual(terminal.grid[1][5].text, unicodedata.normalize("NFC", "é"))
        self.assertEqual(terminal.grid[1][3].fg, 1)
        self.assertTrue(terminal.grid[1][3].bold)
        self.assertEqual(terminal.feed(b"\x1b[6n"), b"\x1b[2;7R")

    def test_alt_screen_restores_primary_after_resize(self):
        terminal = Terminal(6, 20)
        terminal.feed(b"primary\x1b[?1049hsecondary")
        terminal.resize(8, 25)
        terminal.feed(b"\x1b[?1049l")
        self.assertTrue(terminal.display[0].startswith("primary"))
        self.assertNotIn("secondary", "".join(terminal.display))

    def test_unknown_sequence_is_visible_to_diagnostics(self):
        terminal = Terminal(6, 20)
        terminal.feed(b"\x1b[99t")
        self.assertIn("CSI:99t", terminal.unsupported)

    def test_extensions_and_color_queries_fragmented(self):
        terminal = Terminal(6, 20)
        self.assertEqual(terminal.feed(b"\x1b]11;"), b"")
        self.assertEqual(terminal.feed(b"?\x1b\\"), b"\x1b]11;rgb:0000/0000/0000\x1b\\")
        self.assertEqual(terminal.feed(b"\x1b[18t"), b"\x1b[8;6;20t")
        self.assertEqual(terminal.feed(b"\x1b[?u"), b"\x1b[?0u")
        terminal.feed(b"\x1b[?2004h\x1b[?1h\x1b[4munderlined")
        self.assertTrue(terminal.bracketed_paste)
        self.assertTrue(terminal.application_cursor)
        self.assertTrue(terminal.grid[0][0].underline)
        terminal.feed(b"\x1b[?2004l\x1b[?1l")
        self.assertFalse(terminal.bracketed_paste)
        self.assertFalse(terminal.application_cursor)

    def test_pyte_scrolling_and_line_editing(self):
        terminal = Terminal(3, 20)
        terminal.feed(b"one\r\ntwo\r\nthree\r\nfour")
        self.assertTrue(terminal.display[0].startswith("two"))
        terminal.scroll(1)
        self.assertEqual("".join(c.text for c in terminal.grid[0]).strip(), "one")
        terminal.scroll(-1000)
        terminal.feed(b"\x1b[1;1H\x1b[Linserted")
        self.assertTrue(terminal.display[0].startswith("inserted"))
        terminal.feed(b"\x1b[1;1H\x1b[M")
        self.assertTrue(terminal.display[0].startswith("two"))


class FilesystemTests(unittest.TestCase):
    def test_native_and_polling_observe_mutation_fixture(self):
        for backend in ("native", "polling"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                result = filesystem_probe(temporary, backend=backend)
                self.assertTrue(all(result["observed"].values()), result)
                self.assertEqual(result["dropped"], 0)

    def test_exclusions_queue_overflow_and_move_into_scope(self):
        from watchdog.events import FileCreatedEvent, FileMovedEvent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            watch = WorkspaceWatch(root, excluded=[root / "spool"], capacity=1)
            watch.on_any_event(FileCreatedEvent(str(root / ".git/index")))
            watch.on_any_event(FileCreatedEvent(str(root / "spool/event.json")))
            self.assertEqual(watch.drain(), [])
            watch.on_any_event(FileMovedEvent(str(root / "spool/file"), str(root / "included.txt")))
            watch.on_any_event(FileCreatedEvent(str(root / "second.txt")))
            self.assertEqual(watch.dropped, 1)
            record = watch.drain()[0]["payload"]
            self.assertEqual((record["operation"], record["path"]), ("file.create", "included.txt"))


class InputTests(unittest.TestCase):
    def test_ctrl_q_quits_except_inside_paste(self):
        router = InputRouter()
        self.assertEqual(router.feed(b"\x11"), [("command", b"q")])
        self.assertEqual(router.feed(b"\x1d\x11"), [("command", b"q")])
        self.assertFalse(router.prefix)
        tokens = router.feed(b"\x1b[200~\x11\x1b[201~")
        self.assertIn(("paste", b"\x11"), tokens)
        self.assertNotIn(("command", b"q"), tokens)

    def test_prefix_inside_fragmented_paste_is_not_command(self):
        router = InputRouter()
        tokens = []
        for chunk in (b"\x1b[20", b"0~a\x1dm", b"\x1b[201~\x1d", b"l"):
            tokens.extend(router.feed(chunk))
        self.assertEqual([t for kind, t in tokens if kind == "command"], [b"l"])
        self.assertIn(("paste", b"\x1d"), tokens)

    def test_arrow_and_literal_prefix(self):
        router = InputRouter()
        self.assertEqual(router.feed(b"\x1b[A\x1d\x1d"), [("key", b"\x1b[A"), ("literal", b"\x1d")])

    def test_standalone_escape_expires(self):
        router = InputRouter()
        self.assertEqual(router.feed(b"\x1b"), [])
        router.escape_time -= 1
        self.assertEqual(router.expire(), [("key", b"\x1b")])


class SnapshotTests(unittest.TestCase):
    def test_intermediate_changes_survive_zero_net_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = fixture(temporary)
            self.assertIn("created.py", result["intermediate_diff"])
            self.assertIn("old.txt", result["intermediate_diff"])
            self.assertEqual(result["net_diff"], "")
            self.assertNotEqual(result["baseline"], result["final"])

    def test_bytes_names_modes_and_project_index_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            (project / "dirty.txt").write_text("staged\n")
            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            index = (project / ".git/index").read_bytes()
            os.environ["GIT_INDEX_FILE"] = str(project / ".git/index")
            try:
                history = ScratchHistory(root / "scratch")
                manifest = {"space\nand tab\t.bin": ("100644", b"\0\xff\r\n"),
                            "link": ("120000", b"outside-target"),
                            "script": ("100755", b"#!/bin/sh\n")}
                commit = history.checkpoint(manifest, "unusual paths")
                self.assertEqual(history.git("show", commit + ":space\nand tab\t.bin"), b"\0\xff\r\n")
                self.assertIn(b"120000", history.git("ls-tree", commit))
                self.assertEqual(history.checkpoint(manifest, "same tree"), commit)
                with self.assertRaises(ValueError):
                    history.checkpoint({"../escape": ("100644", b"no")}, "invalid")
            finally:
                os.environ.pop("GIT_INDEX_FILE", None)
            self.assertEqual((project / ".git/index").read_bytes(), index)
            self.assertFalse((project / ".git/refs/heads/main").exists())


class HookTests(unittest.TestCase):
    def test_parallel_sink_files_and_observation_only_callback(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, LABRADOUR_EVENTS_DIR=temporary)
            script = ROOT / "labradour/hooks.py"
            children = [subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                        for _ in range(5)]
            for child in children:
                out, err = child.communicate(b'{"hook_event_name":"PreToolUse","tool_use_id":"fixture"}')
                self.assertEqual((child.returncode, out, err), (0, b"", b""))
            files = list(Path(temporary).glob("*.json"))
            self.assertEqual(len(files), 5)
            self.assertEqual(len({json.loads(p.read_text())["event_id"] for p in files}), 5)


class PtyTests(unittest.TestCase):
    def test_cleanup_stops_background_writer_even_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = Path(temporary) / "heartbeat"
            writer = (
                "import signal,time,pathlib\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "signal.signal(signal.SIGHUP,signal.SIG_IGN)\n"
                "p=pathlib.Path(%r)\n" % str(heartbeat) +
                "while True:\n p.write_text(str(time.time_ns()))\n time.sleep(.02)\n"
            )
            leader = (
                "import subprocess,sys,time\n"
                "subprocess.Popen([sys.executable,'-c',%r])\n" % writer +
                "time.sleep(.1)\nprint('STARTED',flush=True)\n"
            )
            child = PtyProcess([sys.executable, "-c", leader], ROOT)
            try:
                wait_for(child, b"STARTED")
                deadline = time.monotonic() + 3
                while child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(child.poll(), 0)
                alive = heartbeat.read_text()
                time.sleep(.08)
                self.assertNotEqual(heartbeat.read_text(), alive)
            finally:
                child.close()
            time.sleep(.05)
            before = heartbeat.read_text()
            time.sleep(.1)
            self.assertEqual(heartbeat.read_text(), before)

    def test_window_resize_input_and_interrupt(self):
        code = (
            "import os,signal,sys,time\n"
            "signal.signal(signal.SIGWINCH,lambda *_: print('SIZE',os.get_terminal_size(),flush=True))\n"
            "signal.signal(signal.SIGINT,lambda *_: print('INTERRUPTED',flush=True))\n"
            "print('READY',os.get_terminal_size(),flush=True)\n"
            "for line in sys.stdin: print('ECHO',line.strip(),flush=True)\n"
        )
        child = PtyProcess([sys.executable, "-c", code], ROOT, 30, 70)
        try:
            self.assertIn(b"columns=70, lines=30", wait_for(child, b"READY"))
            child.resize(35, 90)
            self.assertIn(b"columns=90, lines=35", wait_for(child, b"SIZE"))
            child.send(b"hello\r")
            wait_for(child, b"ECHO hello")
            child.send(b"\x03")
            wait_for(child, b"INTERRUPTED")
        finally:
            child.close()

    def test_end_to_end_curses_harness(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = [sys.executable, "-m", "labradour", "run", "--demo", "--watch", "--events", temporary]
            child = PtyProcess(command, ROOT, 40, 140)
            try:
                wait_for(child, b"fake>")
                child.send(b"run\r")
                deadline = time.monotonic() + 5
                while len(list(Path(temporary).glob("*.json"))) < 11 and time.monotonic() < deadline:
                    child.read()
                    time.sleep(.03)
                records = [json.loads(p.read_text())["payload"] for p in Path(temporary).glob("*.json")]
                self.assertEqual(len([r for r in records if r.get("hook_event_name") == "PostToolUse"]), 4)
                # Mirror, adjust width, focus historical review, return to CLI.
                child.send(b"\x1dm\x1d+\x1dlk\x1dvj\x1da")
                time.sleep(.2)
                child.read()
                child.resize(50, 160)
                time.sleep(.2)
                child.read()
                child.send(b"size\r")
                wait_for(child, b"PTY size")
                child.send(b"\x03")
                wait_for(child, b"fixture remains interactive")
                child.send(b"\x1dl\x11")
                deadline = time.monotonic() + 3
                while child.poll() is None and time.monotonic() < deadline:
                    child.read()
                    time.sleep(.02)
                self.assertEqual(child.poll(), 0)
            finally:
                child.close()

    def test_quit_after_agent_exits(self):
        for shortcut in (b"\x11", b"\x1dq"):
            with self.subTest(shortcut=shortcut):
                child = PtyProcess([sys.executable, "-m", "labradour", "run", "--demo"], ROOT, 40, 140)
                try:
                    wait_for(child, b"fake>")
                    child.send(b"quit\r")
                    wait_for(child, b"review remains open")
                    self.assertIsNone(child.poll())
                    child.send(shortcut)
                    deadline = time.monotonic() + 3
                    while child.poll() is None and time.monotonic() < deadline:
                        child.read()
                        time.sleep(.02)
                    self.assertEqual(child.poll(), 0)
                finally:
                    child.close()

    def test_rendered_three_pane_geometry_and_mirror(self):
        child = PtyProcess([sys.executable, "-m", "labradour", "run", "--demo"], ROOT, 40, 140)
        terminal = Terminal(40, 140)
        def frame_until(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                replies = terminal.feed(child.read())
                if replies:
                    child.send(replies)
                if predicate(terminal.display):
                    return terminal.display
                time.sleep(.02)
            self.fail("rendered frame did not match: " + "\n".join(terminal.display))
        try:
            frame = frame_until(lambda rows: "fake>" in "\n".join(rows))
            self.assertLess(frame[1].find("Agent [focus]"), 70)
            self.assertGreater(frame[1].find("Activity"), 70)
            self.assertIn("Visualization", frame[20])
            self.assertEqual(frame[1][0], "┌")
            child.send(b"\x1dm")
            mirrored = frame_until(lambda rows: rows[1].find("Agent [focus]") > 70)
            self.assertLess(mirrored[1].find("Activity"), 70)
            self.assertIn("Visualization", mirrored[20][:70])
            child.send(b"\x1dq")
            deadline = time.monotonic() + 3
            while child.poll() is None and time.monotonic() < deadline:
                child.read()
                time.sleep(.02)
            self.assertEqual(child.poll(), 0)
        finally:
            child.close()


if __name__ == "__main__":
    unittest.main()
