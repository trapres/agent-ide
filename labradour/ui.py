"""Three-pane native terminal and recording event cards."""
import curses
import json
import os
from pathlib import Path
import select
import signal
import unicodedata
import textwrap

from .input import InputRouter
from .layout import Rect, layout, arrange, pane_order, reflect, adjust_share, compact_reason, parse_tree, tree_dict
from .pty_process import PtyProcess
from .terminal import Terminal, cell_width


def safe_text(value):
    # Tool output must not execute terminal sequences in review panes.
    return "".join("    " if c == "\t" else "?" if unicodedata.category(c) == "Cc" else c
                   for c in str(value))


def clip(text, width):
    result, used = [], 0
    for char in safe_text(text):
        w = cell_width(char)
        if used + w > width:
            break
        result.append(char)
        used += w
    return "".join(result)


class Harness:
    def __init__(self, command, workspace, events, side="left", agent_fraction=.5, activity_fraction=.5,
                 watch=False, watch_backend="native", recording=None, policy=None, collector=False, adapter_provider=None,
                 layout_tree=None, layout_config=None, initial_focus="agent", export_cache=None, gitdiffviz_config=None):
        self.command, self.workspace, self.events = command, workspace, Path(events)
        self.side = side
        self.agent_fraction, self.activity_fraction = agent_fraction, activity_fraction
        self.layout_tree = parse_tree(tree_dict(layout_tree)) if layout_tree is not None else None
        if initial_focus not in ("agent", "activity", "visualization"):
            raise ValueError("unknown initial focus")
        self.focus = initial_focus
        self.layout_config = layout_config
        from .layout_editor import LayoutEditor
        self.layout_editor = LayoutEditor(layout_config) if layout_config is not None else None
        self.layout_view = False
        self.maximized = False
        self.actions = []
        self.seen = set()
        self.selected = 0
        self.follow = True
        from .activity import ActivityModel
        self.activity = ActivityModel()
        self.activity_replay = None
        self.activity_filter_edit = None
        self.activity_detail_cache = None
        self.visualizer = None
        self.visualizer_mode = 'auto'
        self.visualizer_effect = None
        self.visualizer_selection = None
        self.visualizer_column = 0
        self.visualizer_states = {}
        self.scroll = 0
        self.router = InputRouter()
        self.child = None
        self.watch_enabled, self.watch_backend = watch, watch_backend
        self.watcher = None
        self.recording = recording
        self.recorder = None
        self.collector_enabled = collector
        self.adapter_collector = None
        self.adapter_provider = adapter_provider
        from .policy import CapturePolicy
        self.policy = policy or CapturePolicy()
        self.policy_view = bool(recording)
        self.pairs = {}
        self.notice = "pyte terminal | provider coverage unverified"
        self.resize_notice = ""
        self.running = True
        from .exports import ExportJob
        self.export_job = ExportJob(export_cache, protected=(workspace, recording), gitdiffviz_config=gitdiffviz_config)
        self.export_view = False

    def export_selection(self, exporter='json'):
        if not self.recording:
            self.export_job.message = 'Export unavailable: recording is disabled'
            return
        if exporter == 'session':
            if not getattr(self, 'saved_review', False):
                self.export_job.message = 'Session export: open saved review first'
                return
            row = {'id': 'session:' + self.session, 'kind': 'session', 'payload': {'session_id': self.session}}
            self.export_job.start(self.recording, (), row, None, 'session')
            return
        row = self.activity.selected()
        if row and self.visualizer_selection == row['id'] and self.visualizer_effect is not None and row['children']:
            row = row['children'][self.visualizer_effect % len(row['children'])]
        self.export_job.start(self.recording, self.activity.records, row, self.activity.revision, exporter)

    def geometry(self, screen):
        rows, columns = screen.getmaxyx()
        if self.layout_tree is not None:
            return arrange(self.layout_tree, rows, columns, self.focus, self.maximized)
        return layout(rows, columns, self.side, self.agent_fraction,
                      self.activity_fraction, self.focus, self.maximized)

    def sync_agent_size(self, geometry):
        """Focus changes in a batch must resize before forwarding native input."""
        if self.child is not None and "agent" in geometry:
            h, w = geometry["agent"].content_size
            try:
                self.child.resize(h, w)
            except OSError as exc:
                self.notice = "Agent resize failed; previous PTY size retained: " + str(exc)
                self.resize_notice = self.notice
                return
            self.resize_notice = ""
            if (self.terminal.rows, self.terminal.columns) != (h, w):
                self.terminal.resize(h, w)

    def remember_visualizer(self):
        if self.visualizer_selection is not None:
            self.visualizer_states[self.visualizer_selection] = (self.scroll, self.visualizer_column,
                                                                self.visualizer_mode, self.visualizer_effect)
            if len(self.visualizer_states) > 128:
                self.visualizer_states.pop(next(iter(self.visualizer_states)))

    def collect(self):
        fresh = []
        for file in ([] if self.recorder else self.events.glob("*.json")):
            if file.name in self.seen:
                continue
            self.seen.add(file.name)
            try:
                record = json.loads(file.read_text())
                if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                    raise ValueError("invalid hook event")
                fresh.append(record)
            except (ValueError, OSError):
                self.notice = "Unreadable hook event; coverage incomplete"
        if self.recorder:
            fresh.extend(self.recorder.drain())
            self.notice = self.recorder.notice
        if self.watcher:
            fresh.extend(self.watcher.drain())
            if self.watcher.dropped:
                self.notice = "watcher dropped %s observations; coverage incomplete" % self.watcher.dropped
        if self.recorder:
            if self.activity_replay is None:
                from .activity import ActivityReplay
                self.activity_replay = ActivityReplay(self.recording, self.recorder.session)
            result = self.activity_replay.tick(bool(fresh), 'process-exited' if self.child.status is not None else 'running')
            if result is not None:
                self.activity.update(*result)
                self.follow = self.activity.follow
            # Recorded Activity comes from durable replay, not the lossy display queue.
            return
        for record in sorted(fresh, key=lambda r: r.get("received_ns", 0)):
            payload = record.get("payload", {})
            call = payload.get("tool_use_id")
            # Only merge when both session and call identity are present.
            key = (payload.get("session_id"), payload.get("agent_id"), payload.get("turn_id"), call) if call and payload.get("session_id") else None
            existing = next((a for a in self.actions if key and a.get("key") == key), None)
            if existing:
                existing["payload"].update(payload)
            else:
                self.actions.append(dict(record, key=key))
                if self.follow:
                    self.selected = len(self.actions) - 1

    def handle(self, kind, token):
        if kind == "command" and token == b"q":
            self.running = False
            return
        if self.layout_editor and self.layout_editor.open:
            candidate = self.layout_editor.input(kind, token, self.layout_tree)
            if candidate is not None:
                self.layout_tree = candidate
            return
        if self.activity_filter_edit is not None:
            if kind == "key" and token in (b"\x1b", b"\x03"):
                self.activity_filter_edit = None
            elif kind == "key" and token in (b"\r", b"\n"):
                self.activity.set_filter(self.activity_filter_edit.decode("utf-8", "replace"))
                self.activity_filter_edit = None
                self.follow = False
            elif kind == "key" and token == b"\x15":
                self.activity_filter_edit = bytearray()
            elif kind == "key" and token in (b"\x7f", b"\x08"):
                self.activity_filter_edit = bytearray(self.activity_filter_edit.decode("utf-8", "ignore")[:-1].encode())
            elif kind in ("key", "paste") and not token.startswith(b"\x1b"):
                self.activity_filter_edit.extend(b for b in token if b >= 32 and b != 127)
                del self.activity_filter_edit[512:]
            return
        if kind == "command":
            key = token.decode("ascii", "ignore")
            if key == ":" and self.layout_editor:
                self.layout_editor.begin()
                return
            if key in ("x", "g"):
                self.export_selection('gitdiffviz' if key == 'g' else 'json')
                return
            if key == "c":
                self.export_job.cancel()
                return
            if key == "o":
                self.export_job.open_graphical()
                return
            if key == "u":
                self.export_selection('session')
                return
            if key == "t":
                self.export_view = not self.export_view
                self.focus = 'visualization'
                self.scroll = 0
                return
            if self.layout_editor and self.layout_editor.pending and key in ("m", "+", "-", "[", "]"):
                self.notice = "Layout disk operation running; wait before changing layout"
                return
            if key in ("a", "l", "v"):
                self.focus = {"a": "agent", "l": "activity", "v": "visualization"}[key]
            elif key in ("\t", "\x1b[Z"):
                panes = pane_order(self.layout_tree) if self.layout_tree is not None else ("agent", "activity", "visualization")
                self.focus = panes[(panes.index(self.focus) + (1 if key == "\t" else -1)) % 3]
            elif key == "m":
                if self.layout_tree is not None:
                    self.layout_tree = reflect(self.layout_tree)
                else:
                    self.side = "right" if self.side == "left" else "left"
            elif key == "z":
                self.maximized = not self.maximized
            elif key == "b":
                self.terminal.scroll(max(1, self.terminal.rows // 2))
            elif key == "n":
                self.terminal.scroll(-1000)
            elif key in ("+", "-", "[", "]"):
                if self.layout_tree is not None:
                    width = key in ("+", "-")
                    self.layout_tree, found = adjust_share(self.layout_tree, "agent" if width else "activity",
                        "columns" if width else "rows", 500 if key in ("+", "]") else -500)
                    if not found:
                        self.notice = "No %s split for pane; use Ctrl-] : layout menu" % ("width" if width else "height")
                elif key in ("+", "-"):
                    self.agent_fraction = min(.7, max(.3, self.agent_fraction + (.05 if key == "+" else -.05)))
                else:
                    self.activity_fraction = min(.7, max(.3, self.activity_fraction + (.05 if key == "]" else -.05)))
            elif key == "p":
                self.layout_view = False
                self.policy_view = not self.policy_view
                self.focus = "visualization"
                self.scroll = 0
            elif key == "i" and self.layout_config is not None:
                self.layout_view = not self.layout_view
                self.focus = "visualization"
                self.scroll = 0
            elif key == "r" and self.recording:
                self.activity.toggle_journal()
                self.follow = self.activity.follow
            elif key == "q":
                self.running = False
            else:
                self.notice = "Prefix: : layout | i layout status | x row / u session / g gitdiffviz / o open / c cancel / t status | a/l/v focus | q stop"
            return
        if kind == "literal":
            self.child.send(token)
            return
        if self.focus == "agent":
            if kind in ("paste-start", "paste-end") and not self.terminal.bracketed_paste:
                return
            if kind == "key" and self.terminal.application_cursor:
                token = {b"\x1b[A": b"\x1bOA", b"\x1b[B": b"\x1bOB",
                         b"\x1b[C": b"\x1bOC", b"\x1b[D": b"\x1bOD"}.get(token, token)
            self.child.send(token)
        elif kind == "key":
            if self.focus == "activity":
                self.layout_view = False
                self.policy_view = False
                self.export_view = False
                if self.recording:
                    self.remember_visualizer()
                    if token in (b"j", b"\x1b[B", b"k", b"\x1b[A"):
                        self.activity.move(1 if token in (b"j", b"\x1b[B") else -1)
                        self.scroll = 0
                    elif token in (b"\x1b[C", b"\r", b"\n"):
                        self.activity.expand()
                    elif token == b"\x1b[D":
                        self.activity.expand(False)
                    elif token in (b"f", b"\x1b[F", b"\x1b[4~"):
                        self.activity.resume()
                    elif token == b"/":
                        self.activity_filter_edit = bytearray(self.activity.filter.encode())
                    elif token == b"d":
                        self.focus = "visualization"
                        self.scroll = 0
                    self.follow = self.activity.follow
                    return
                if token in (b"j", b"\x1b[B"):
                    self.selected = min(len(self.actions) - 1, self.selected + 1) if self.actions else 0
                    self.follow = False
                    self.scroll = 0
                elif token in (b"k", b"\x1b[A"):
                    self.selected = max(0, self.selected - 1)
                    self.follow = False
                    self.scroll = 0
                elif token == b"f":
                    self.follow = True
                    self.selected = max(0, len(self.actions) - 1)
            elif self.recording and token in (b"s", b"e", b"]", b"["):
                self.export_view = False
                self.layout_view = False
                self.policy_view = False
                row = self.activity.selected()
                if token in (b"s", b"e"):
                    self.visualizer_mode = 'auto' if token == b's' else 'evidence'
                    self.visualizer_effect = None
                elif row and row['children']:
                    count = len(row['children'])
                    current = self.visualizer_effect if self.visualizer_effect is not None else (-1 if token == b']' else 0)
                    self.visualizer_effect = (current + (1 if token == b']' else -1)) % count
                    self.visualizer_mode = 'auto'
                self.scroll = 0
                self.visualizer_column = 0
                self.remember_visualizer()
            elif self.recording and token in (b"\x1b[C", b"\x1b[D"):
                self.visualizer_column = max(0, self.visualizer_column + (20 if token == b'\x1b[C' else -20))
                self.remember_visualizer()
            elif token in (b"j", b"\x1b[B"):
                self.scroll += 1
                self.remember_visualizer()
            elif token in (b"k", b"\x1b[A"):
                self.scroll = max(0, self.scroll - 1)
                self.remember_visualizer()

    def add(self, window, y, x, text, attribute=0):
        h, w = window.getmaxyx()
        if y >= h or x >= w or y < 0 or x < 0:
            return
        try:
            window.addstr(y, x, clip(text, max(0, w - x - 1)), attribute)
        except curses.error:
            pass

    def color(self, cell):
        attribute = ((curses.A_BOLD if cell.bold else 0) | (curses.A_REVERSE if cell.reverse else 0)
                     | (curses.A_UNDERLINE if cell.underline else 0)
                     | (getattr(curses, "A_ITALIC", 0) if cell.italic else 0))
        if not curses.has_colors():
            return attribute
        fg = cell.fg if cell.fg < curses.COLORS else cell.fg % 8
        bg = cell.bg if cell.bg < curses.COLORS else cell.bg % 8
        key = fg, bg
        if key not in self.pairs and len(self.pairs) + 1 < curses.COLOR_PAIRS:
            number = len(self.pairs) + 1
            try:
                curses.init_pair(number, fg, bg)
                self.pairs[key] = number
            except curses.error:
                pass
        return attribute | curses.color_pair(self.pairs.get(key, 0))

    def paint(self, screen, geometry):
        screen.erase()
        rows, columns = screen.getmaxyx()
        self.add(screen, 0, 0, "Labradour | %s | focus: %s | %s" % (
            Path(self.recording if getattr(self, 'saved_review', False) else self.workspace).name, self.focus,
            "SAVED REVIEW" if getattr(self, 'saved_review', False) else "LIVE" if self.follow else "historical selection"), curses.A_BOLD)
        if self.layout_tree is not None:
            mode = "maximized" if self.maximized else compact_reason(self.layout_tree, rows, columns)
            if mode:
                self.add(screen, 0, 0, "Labradour | focus: %s | %s" % (self.focus, mode), curses.A_BOLD)
        for name, rect in geometry.items():
            window = screen.derwin(rect.height, rect.width, rect.y, rect.x)
            if rect.height >= 2 and rect.width >= 2:
                window.box()
            self.add(window, 0, 2, " %s%s " % (name.title(), " [focus]" if self.focus == name else ""),
                     curses.A_BOLD)
            h, w = rect.content_size
            if name == "agent":
                if getattr(self, 'saved_review', False):
                    for y, line in enumerate(self.session_lines()[:h]):
                        self.add(window, y + 1, 1, line)
                    continue
                for y, row in enumerate(self.terminal.grid[:h]):
                    for x, cell in enumerate(row[:w]):
                        if cell.text:
                            self.add(window, y + 1, x + 1, cell.text, self.color(cell))
            elif name == "activity":
                if self.recording:
                    visible = self.activity.visible()
                    selected = next((i for i, r in enumerate(visible) if r['id'] == self.activity.selected_id), 0)
                    info = ("Journal" if self.activity.journal else "Actions/effects") + " | unread %d" % self.activity.unread
                    if self.activity.filter:
                        info += " | filter: " + self.activity.filter
                    if self.activity.selection_outside():
                        info = "Selection outside filter/view | " + info
                    self.add(window, 1, 1, info)
                    start = max(0, selected - max(1, h - 1) + 1)
                    for y, row in enumerate(visible[start:start + max(0, h - 1)], 2):
                        marker = ("  candidate " if row.get('parent') else "  " if row['kind'] == 'effect' else
                                  ("- " if row['id'] in self.activity.expanded else "+ ") if row['children'] else "")
                        label = "%s%s %s %s" % (marker, row['actor'], row['tool'], row['state'])
                        if row['kind'] == 'action' and 'unknown' in row['payload']['result_outcomes']:
                            label += " result:unknown"
                        if row['children']:
                            label += " [%d effects; candidate interval]" % len(row['children'])
                        if row['kind'] == 'effect':
                            label += " [external-or-unknown]"
                        label += " " + row['target'][:256]
                        self.add(window, y, 1, label, curses.A_REVERSE if row['id'] == self.activity.selected_id else 0)
                    if not visible:
                        self.add(window, 2, 1, "No matching rows" if self.activity.filter else "Loading recorded activity")
                    continue
                start = max(0, self.selected - h + 1)
                for index, action in enumerate(self.actions[start:start + h], start):
                    p = action["payload"]
                    state = {"PreToolUse": "running", "PostToolUse": "completed",
                             "PostToolUseFailure": "failed"}.get(p.get("hook_event_name"), p.get("hook_event_name", action.get("kind", "event")))
                    label = "%s %s %s" % (p.get("actor", "main/unknown"),
                                            p.get("tool_name", p.get("operation", "session")), state)
                    if action.get("kind") == "adapter.event":
                        label = "%s %s %s" % (p.get("actor_id") or "unknown", p.get("payload", {}).get("tool") or "session", p.get("kind", "event"))
                    self.add(window, index - start + 1, 1, label,
                             curses.A_REVERSE if index == self.selected else 0)
                if not self.actions:
                    self.add(window, 1, 1, "No hook events; native tool coverage unverified")
            elif self.export_view:
                from .exports import ARTIFACT_LIMIT, CACHE_LIMIT, MAX_ARTIFACTS, MAX_AGE
                lines = ['Export status | Ctrl-] x export / c cancel / t return',
                         self.export_job.message or 'No export requested.',
                         'x row JSON / g gitdiffviz / u saved session / o open companion.',
                         'Artifact limit %d MiB; cache %d MiB / %d artifacts / %d days.' % (
                             ARTIFACT_LIMIT // 1048576, CACHE_LIMIT // 1048576, MAX_ARTIFACTS, MAX_AGE // 86400)]
                if self.export_job.result:
                    lines += json.dumps(self.export_job.result, indent=2).splitlines()
                wrapped = [part for line in lines for part in textwrap.wrap(safe_text(line), max(1, w - 1)) or ['']]
                for y, line in enumerate(wrapped[self.scroll:self.scroll + h]):
                    self.add(window, y + 1, 1, line)
            elif self.layout_view and self.layout_config is not None:
                lines = ["Layout configuration / source diagnostics"]
                lines += json.dumps(self.layout_config.describe(rows, columns, self.layout_tree,
                                                               focus=self.focus, maximized=self.maximized),
                                    ensure_ascii=False, indent=2).splitlines()
                for y, line in enumerate(lines[self.scroll:self.scroll + h]):
                    self.add(window, y + 1, 1, line)
            elif self.policy_view:
                lines = ["Effective capture policy", "Recording enabled" if self.recording else "Recording disabled"]
                lines += json.dumps(self.policy.describe(self.workspace, [p for p in (self.recording, self.events) if p]),
                                    ensure_ascii=False, indent=2).splitlines()
                for y, line in enumerate(lines[self.scroll:self.scroll + h]):
                    self.add(window, y + 1, 1, line)
            elif self.recording and self.activity.selected() is not None:
                row = self.activity.selected()
                if self.visualizer_selection != row['id']:
                    self.visualizer_selection = row['id']
                    self.scroll, self.visualizer_column, self.visualizer_mode, self.visualizer_effect = self.visualizer_states.get(row['id'], (0, 0, 'auto', None))
                if self.visualizer_effect is not None and row['children']:
                    row = row['children'][self.visualizer_effect % len(row['children'])]
                if self.visualizer is None:
                    from .visualizers import VisualizerJob
                    self.visualizer = VisualizerJob(self.recording)
                key = (self.activity.revision, row['id'], self.visualizer_mode)
                title, prepared = self.visualizer.tick(key, self.activity.records, row, self.visualizer_mode)
                lines = [title+' | s summary / e evidence / [ ] effects'] + prepared
                for y, line in enumerate(lines[self.scroll:self.scroll + h]):
                    clean = safe_text(line)
                    offset = 0
                    used = 0
                    while offset < len(clean) and used < self.visualizer_column:
                        used += cell_width(clean[offset])
                        offset += 1
                    self.add(window, y + 1, 1, clean[offset:])
            elif self.actions:
                p = self.actions[self.selected]["payload"]
                lines = ["View: " + p.get("operation", "tool/event card"),
                         "Evidence: " + ("watcher observation; no content snapshot" if
                                          p.get("quality") == "watcher-observed" else "durable checkpoint/event" if self.recorder else "hook payload (Phase 0)")]
                lines += json.dumps(p, ensure_ascii=False, indent=2).splitlines()
                for y, line in enumerate(lines[self.scroll:self.scroll + h]):
                    self.add(window, y + 1, 1, line)
            else:
                self.add(window, 1, 1, "Select an action; run the fake agent to emit events")
        footer = "Ctrl-Q quit | Ctrl-] ? help | " + self.notice
        if self.export_job.message:
            footer += ' | ' + self.export_job.message.split(':', 1)[0] + ' (Ctrl-] t)'
        if self.resize_notice:
            footer += " | " + self.resize_notice
        if self.activity_replay and self.activity_replay.error:
            footer += " | " + self.activity_replay.error
        if self.activity_filter_edit is not None:
            footer = "Filter (Enter apply, Escape cancel, Ctrl-U clear): " + self.activity_filter_edit.decode("utf-8", "replace")
        if self.layout_editor and not self.layout_editor.open and self.layout_editor.message.startswith(("Layout error:", "Layout saved:", "Layout reloaded")):
            footer += " | " + self.layout_editor.message
        if self.layout_config and self.layout_config.warnings:
            footer += " | LAYOUT WARNING (Ctrl-] i): " + self.layout_config.warnings[0]
        if self.recorder:
            metrics = self.recorder.metrics
            if self.adapter_provider:
                count = sum(self.recorder.adapter_counts.values())
                footer += " | %s hooks: %s" % (self.adapter_provider, str(count) if count else "awaiting delivery/trust")
                if self.recorder.adapter_gaps:
                    footer += " gaps %d" % self.recorder.adapter_gaps
            footer += " | scan %.0fms | read %d/cache %d | queue %d" % (
                metrics.get("scan_ms", 0), metrics.get("read_files", 0), metrics.get("cache_hits", 0),
                metrics.get("watcher_queue", 0))
        if self.terminal.unsupported:
            footer += " | unsupported VT: %s" % len(self.terminal.unsupported)
        if self.router.prefix:
            footer = "Ctrl-Q quit | PREFIX: : layout | x export / c cancel / t export status | a/l/v focus | m mirror | z maximize"
        self.add(screen, rows - 1, 0, footer)
        if self.layout_editor and self.layout_editor.open:
            # Overlay covers cells, not controllers: native dimensions and buffers stay live.
            for y in range(1, rows - 1):
                self.add(screen, y, 0, " " * columns)
            lines = self.layout_editor.lines(self.layout_tree, rows, columns, self.focus, self.maximized)
            for y, line in enumerate(lines[self.layout_editor.offset:self.layout_editor.offset + max(0, rows - 2)], 1):
                self.add(screen, y, 0, line, curses.A_BOLD if y == 1 else 0)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        elif "agent" in geometry and self.focus == "agent" and self.terminal.cursor_visible and not getattr(self, 'saved_review', False):
            rect = geometry["agent"]
            y = rect.y + 1 + min(self.terminal.y, rect.content_size[0] - 1)
            x = rect.x + 1 + min(self.terminal.x, rect.content_size[1] - 1)
            try:
                curses.curs_set(1)
                screen.move(min(rows - 1, y), min(columns - 1, x))
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        screen.refresh()

    def run(self, screen):
        curses.raw()
        curses.noecho()
        # Raw byte reads preserve native key/paste sequences.
        screen.keypad(False)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
        geometry = self.geometry(screen)
        if self.layout_tree is not None and "agent" not in geometry:
            rows, columns = screen.getmaxyx()
            ordinary = arrange(self.layout_tree, rows, columns, "agent")
            initial_agent = ordinary.get("agent", Rect(0, 0, 26, 82))
        else:
            initial_agent = geometry.get("agent", next(iter(geometry.values()), Rect(0, 0, 26, 82)))
        h, w = initial_agent.content_size
        self.terminal = Terminal(h, w)
        child_env = dict(os.environ, LABRADOUR_EVENTS_DIR=str(self.events.resolve()))
        for key in ("LABRADOUR_COLLECTOR_SOCKET", "LABRADOUR_COLLECTOR_TOKEN", "LABRADOUR_COLLECTOR_SPOOL",
                    "LABRADOUR_LAUNCH_ID", "LABRADOUR_WORKSPACE_ID"):
            child_env.pop(key, None)
        if self.collector_enabled:
            from .adapters.collector import Collector
            self.adapter_collector = Collector(self.workspace, self.events)
            child_env.update(self.adapter_collector.environment())
        try:
            self.child = PtyProcess(self.command, self.workspace, h, w, child_env, paused=bool(self.recording))
        except Exception:
            if self.adapter_collector:
                self.adapter_collector.close()
            raise
        # Fork the PTY child before starting watchdog's threads.
        old_handlers = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, lambda *_: setattr(self, "running", False))
        os.write(1, b"\x1b[?2004h")
        try:
            if self.recording:
                from .recorder import Recorder
                self.notice = "Capture policy shown in Visualization | Ctrl-] p toggles policy"
                self.paint(screen, geometry)
                self.recorder = Recorder(self.workspace, self.recording, excluded=[self.events],
                                         backend=self.watch_backend, events=self.events, policy=self.policy,
                                         adapter_collector=self.adapter_collector, adapter_provider=self.adapter_provider)
                self.recorder.start()
                if self.adapter_collector:
                    self.adapter_collector.start()
                self.child.release()
            elif self.watch_enabled:
                from .filesystem import WorkspaceWatch
                self.watcher = WorkspaceWatch(self.workspace, excluded=[self.events], backend=self.watch_backend)
                self.watcher.start()
                self.notice = "pyte | watcher: %s | observations only" % self.watcher.backend
            while self.running:
                self.export_job.poll()
                curses.update_lines_cols()
                actual = os.get_terminal_size(0)
                if screen.getmaxyx() != (actual.lines, actual.columns):
                    curses.resizeterm(actual.lines, actual.columns)
                geometry = self.geometry(screen)
                if self.layout_editor:
                    result = self.layout_editor.poll()
                    if result is not None:
                        self.layout_config, candidate = result
                        if candidate is not None:
                            self.layout_tree = candidate
                        geometry = self.geometry(screen)
                self.sync_agent_size(geometry)
                # Hidden agent retains its previous usable PTY size.
                output = self.child.read()
                if output:
                    replies = self.terminal.feed(output)
                    if replies:
                        self.child.send(replies)
                self.child.flush()
                self.collect()
                if self.child.poll() is not None:
                    self.notice = ((self.recorder.notice + " | ") if self.recorder else "") + (
                        "Agent exited %s; review remains open. Ctrl-Q quits" % self.child.status)
                for kind, token in self.router.expire():
                    self.handle(kind, token)
                self.paint(screen, geometry)
                if select.select([0], [], [], .02)[0]:
                    data = os.read(0, 65536)
                    if not data:
                        break
                    for kind, token in self.router.feed(data):
                        before = self.layout_tree
                        self.handle(kind, token)
                        if kind == "command" or self.layout_tree != before:
                            self.sync_agent_size(self.geometry(screen))
        finally:
            self.export_job.cancel()
            os.write(1, b"\x1b[?2004l")
            try:
                stopped = self.child.close()
                if not stopped and self.recorder:
                    self.recorder.failed = True
                    self.recorder.adapter_gaps += 1
                    self.recorder.cleanup_gap = {"pid": self.child.pid,
                        "quality": "owned child not reaped after TERM/KILL; shutdown is bounded"}
                if self.adapter_collector:
                    self.adapter_collector.close()
                if self.recorder:
                    self.recorder.close()
                elif self.watcher:
                    self.watcher.close()
            finally:
                if self.adapter_collector and not self.adapter_collector.stop.is_set():
                    self.adapter_collector.close()
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
