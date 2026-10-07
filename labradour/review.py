"""Saved-session UI. No PTY, recorder, watcher, hooks or workspace reads."""
import curses
import os
from pathlib import Path
import queue
import select
import signal
import threading

from .activity import ActivityModel
from .adapters.correlation import project
from .leases import evidence_lease
from .recorder import read_history
from .terminal import Terminal
from .ui import Harness


def load_session(directory, session=None):
    with evidence_lease(directory):
        sessions = read_history(directory)
        if not sessions:
            raise ValueError('recording has no saved sessions')
        selected = session or sessions[-1]['id']
        info = next((s for s in sessions if s['id'] == selected), None)
        if info is None:
            raise ValueError('unknown or pruned recording session')
        records = read_history(directory, selected)
    # A persisted running status is a fact, not permission to launch/recover.
    status = 'saved-unclosed' if info['status'] == 'running' else info['status']
    return sessions, selected, records, project(records, status)


class SavedReview(Harness):
    saved_review = True

    def __init__(self, directory, session=None, layout_config=None, export_cache=None, gitdiffviz_config=None):
        self.directory = Path(directory).resolve()
        initial = load_session(self.directory, session)
        workspace = layout_config.workspace if layout_config else Path.cwd()
        super().__init__([], workspace, self.directory, recording=self.directory,
                         layout_config=layout_config,
                         layout_tree=layout_config.tree if layout_config else None,
                         initial_focus='activity', export_cache=export_cache, gitdiffviz_config=gitdiffviz_config)
        self.policy_view = False
        self.terminal = Terminal(1, 1)
        self.pending = False
        self.results = queue.Queue(maxsize=1)
        self.requested = initial[1]
        self.install(initial)

    def install(self, result):
        same_session = getattr(self, 'session', None) == result[1]
        self.sessions, self.session, records, projection = result
        self.session_cursor = next(i for i, s in enumerate(self.sessions) if s['id'] == self.session)
        if not same_session:
            self.activity = ActivityModel()
        self.activity.update(records, projection)
        self.activity.follow = self.follow = False
        if not same_session:
            self.visualizer_selection = None
            self.visualizer_states.clear()
            self.scroll = self.visualizer_column = 0
            self.visualizer_effect = None
        self.notice = 'Saved session %s | %s | r refresh in Agent pane' % (
            self.session, self.sessions[self.session_cursor]['status'])

    def session_lines(self):
        lines = ['Saved sessions; no agent launched.', 'Terminal output was not recorded.',
                 'j/k select, Enter open, r refresh.',
                 'Recorded workspace: ' + str(next((r['payload'].get('workspace')
                     for r in self.activity.records if r['kind'] == 'session.started'), 'unavailable'))]
        for i, session in enumerate(self.sessions):
            lines.append(('%s%s ' % ('>' if i == self.session_cursor else ' ',
                          '*' if session['id'] == self.session else ' ')) + session['id'] + ' ' + session['status'])
        # Keep the highlighted session visible in small/maximized layouts.
        start = max(0, self.session_cursor - 5)
        return lines[:4] + lines[4 + start:]

    def handle(self, kind, token):
        if not (self.layout_editor and self.layout_editor.open) and self.activity_filter_edit is None:
            if kind == 'literal':
                return
            if kind == 'command' and token in (b'p', b'b', b'n'):
                self.notice = 'Saved review: captured policy is in session evidence; terminal output is unavailable'
                return
            if kind != 'command' and self.focus == 'agent':
                if kind == 'key':
                    if token in (b'j', b'\x1b[B', b'k', b'\x1b[A'):
                        self.session_cursor = max(0, min(len(self.sessions)-1,
                            self.session_cursor + (1 if token in (b'j', b'\x1b[B') else -1)))
                    elif token in (b'\r', b'\n', b'r'):
                        self.requested = self.sessions[self.session_cursor]['id']
                        self.reload = True
                return
        super().handle(kind, token)

    def collect(self):
        try:
            requested, result, error = self.results.get_nowait()
            self.pending = False
            if requested == self.requested:
                if result is not None:
                    self.install(result)
                else:
                    self.notice = 'Saved review unavailable: ' + error
        except queue.Empty:
            pass
        if not self.pending and getattr(self, 'reload', False):
            self.reload = False
            self.pending = True
            requested = self.requested
            self.notice = 'Loading saved session ' + requested
            def load():
                try:
                    self.results.put((requested, load_session(self.directory, requested), ''))
                except Exception as exc:
                    self.results.put((requested, None, str(exc)))
            threading.Thread(target=load, name='labradour-review', daemon=True).start()

    def run(self, screen):
        curses.raw()
        curses.noecho()
        screen.keypad(False)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
        handlers = {sig: signal.signal(sig, lambda *_: setattr(self, 'running', False))
                    for sig in (signal.SIGTERM, signal.SIGHUP)}
        os.write(1, b'\x1b[?2004h')
        try:
            while self.running:
                self.export_job.poll()
                curses.update_lines_cols()
                actual = os.get_terminal_size(0)
                if screen.getmaxyx() != (actual.lines, actual.columns):
                    curses.resizeterm(actual.lines, actual.columns)
                if self.layout_editor:
                    result = self.layout_editor.poll()
                    if result is not None:
                        self.layout_config, candidate = result
                        if candidate is not None:
                            self.layout_tree = candidate
                self.collect()
                for kind, token in self.router.expire():
                    self.handle(kind, token)
                self.paint(screen, self.geometry(screen))
                if select.select([0], [], [], .02)[0]:
                    data = os.read(0, 65536)
                    if not data:
                        break
                    for kind, token in self.router.feed(data):
                        self.handle(kind, token)
        finally:
            self.export_job.cancel()
            os.write(1, b'\x1b[?2004l')
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
