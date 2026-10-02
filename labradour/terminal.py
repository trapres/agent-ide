"""Pyte screen engine plus bounded framing for pane-local xterm extensions."""
import re
from dataclasses import dataclass
from functools import lru_cache

import pyte
from wcwidth import wcwidth


def cell_width(char):
    return max(0, wcwidth(char))


@dataclass(frozen=True)
class Cell:
    text: str = " "
    fg: int = -1
    bg: int = -1
    bold: bool = False
    reverse: bool = False
    underline: bool = False
    italic: bool = False


@lru_cache(maxsize=1024)
def color_number(color):
    names = ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")
    if color == "default":
        return -1
    if color in names:
        return names.index(color)
    if color.startswith("bright") and color[6:] in names:
        return 8 + names.index(color[6:])
    if re.fullmatch(r"[0-9a-fA-F]{6}", color):
        rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
        levels = (0, 95, 135, 175, 215, 255)
        cube = tuple(min(range(6), key=lambda n: abs(levels[n] - c)) for c in rgb)
        cube_error = sum((c - levels[n]) ** 2 for c, n in zip(rgb, cube))
        gray = min(range(24), key=lambda n: sum((c - (8 + n * 10)) ** 2 for c in rgb))
        gray_error = sum((c - (8 + gray * 10)) ** 2 for c in rgb)
        return 232 + gray if gray_error < cube_error else 16 + 36 * cube[0] + 6 * cube[1] + cube[2]
    return -1


class ReplyScreen(pyte.HistoryScreen):
    def __init__(self, columns, rows, replies, history=1000):
        self.replies = replies
        super().__init__(columns, rows, history=history)

    def write_process_input(self, data):
        self.replies.extend(data.encode("utf-8"))


class Terminal:
    backend = "pyte"

    def __init__(self, rows, columns):
        self.replies = bytearray()
        self.primary = ReplyScreen(columns, rows, self.replies)
        self.alternate = ReplyScreen(columns, rows, self.replies, history=0)
        self.streams = {s: pyte.ByteStream(s) for s in (self.primary, self.alternate)}
        self.screen = self.primary
        self.sequence = bytearray()
        self.state = "text"
        self.unsupported = set()
        self.bracketed_paste = False
        self.application_cursor = False
        self.scroll_offset = 0

    @property
    def rows(self):
        return self.screen.lines

    @property
    def columns(self):
        return self.screen.columns

    @property
    def x(self):
        return self.screen.cursor.x

    @property
    def y(self):
        return self.screen.cursor.y

    @property
    def cursor_visible(self):
        return not self.screen.cursor.hidden and self.scroll_offset == 0

    @property
    def display(self):
        return self.screen.display

    @property
    def grid(self):
        lines = list(self.screen.history.top) + [self.screen.buffer[y] for y in range(self.rows)]
        end = len(lines) - min(self.scroll_offset, len(self.screen.history.top))
        visible = lines[max(0, end - self.rows):end]
        return [[Cell(c.data, color_number(c.fg), color_number(c.bg), c.bold, c.reverse,
                      c.underscore, c.italics) for c in (line[x] for x in range(self.columns))]
                for line in visible]

    def scroll(self, amount):
        self.scroll_offset = min(len(self.screen.history.top), max(0, self.scroll_offset + amount))

    def resize(self, rows, columns):
        for screen in (self.primary, self.alternate):
            screen.resize(lines=max(1, rows), columns=max(1, columns))
            screen.cursor.x = min(screen.cursor.x, screen.columns - 1)
            screen.cursor.y = min(screen.cursor.y, screen.lines - 1)

    def record_unsupported(self, token):
        if len(self.unsupported) < 100:
            self.unsupported.add(token)

    def feed(self, data):
        # Framing preserves complete controls while pyte handles all screen edits.
        plain = bytearray()
        def flush():
            if plain:
                self.streams[self.screen].feed(bytes(plain))
                plain.clear()
        for byte in data:
            if self.state == "text":
                if byte == 27:
                    flush()
                    self.sequence = bytearray([byte])
                    self.state = "escape"
                else:
                    plain.append(byte)
                continue
            if len(self.sequence) < 4096:
                self.sequence.append(byte)
            else:
                self.record_unsupported("overlong control sequence")
            if self.state == "escape":
                self.state = {91: "csi", 93: "osc", 80: "string", 95: "string",
                              94: "string", 40: "charset", 41: "charset", 35: "charset",
                              37: "charset"}.get(byte, "text")
                if self.state == "text":
                    if byte in (61, 62):
                        self.application_cursor = byte == 61
                    else:
                        self.streams[self.screen].feed(bytes(self.sequence))
            elif self.state == "charset":
                if self.sequence[1] in (40, 41):
                    # Pyte skips charset designators in UTF-8 mode; native TUIs
                    # still use DEC graphics alongside UTF-8 for borders.
                    self.screen.define_charset(chr(byte), mode=chr(self.sequence[1]))
                else:
                    self.streams[self.screen].feed(bytes(self.sequence))
                self.state = "text"
            elif self.state == "csi":
                if 64 <= byte <= 126:
                    self.csi(bytes(self.sequence))
                    self.state = "text"
                elif byte in (24, 26):
                    self.state = "text"
            elif self.state in ("osc", "string"):
                if byte == 7:
                    if self.state == "osc":
                        self.osc(bytes(self.sequence))
                    self.state = "text"
                elif byte == 27:
                    self.string_kind = self.state
                    self.state = "string-end"
            elif self.state == "string-end":
                if byte == 92:
                    if self.string_kind == "osc":
                        self.osc(bytes(self.sequence))
                    else:
                        self.record_unsupported("DCS/APC/PM")
                    self.state = "text"
                else:
                    self.state = self.string_kind
        flush()
        replies = bytes(self.replies)
        self.replies.clear()
        return replies

    def csi(self, sequence):
        raw = sequence[2:-1].decode("ascii", "replace")
        final = chr(sequence[-1])
        if final in "hl" and raw.startswith("?"):
            for part in raw[1:].split(";"):
                mode = int(part) if part.isdigit() else 0
                enabled = final == "h"
                if mode in (47, 1047, 1049):
                    if enabled and self.screen is self.primary:
                        self.alternate.reset()
                        self.screen = self.alternate
                    elif not enabled:
                        self.screen = self.primary
                    self.scroll_offset = 0
                elif mode == 1048:
                    self.screen.save_cursor() if enabled else self.screen.restore_cursor()
                elif mode == 2004:
                    self.bracketed_paste = enabled
                elif mode == 1:
                    self.application_cursor = enabled
                elif mode in (5, 6, 7, 25):
                    self.streams[self.screen].feed(b"\x1b[?" + str(mode).encode() + sequence[-1:])
                elif mode == 2026:
                    pass  # Curses commits complete frames.
                elif enabled:
                    self.record_unsupported("mode:%s" % mode)
            return
        if final == "c" and raw.startswith(">"):
            self.replies.extend(b"\x1b[>0;0;0c")
        elif final == "q" and raw.startswith(">"):
            self.replies.extend(b"\x1bP>|Labradour (pyte)\x1b\\")
        elif final == "q" and raw.endswith(" "):
            pass  # Cursor shape does not change pane content.
        elif final == "p" and raw.startswith("?") and raw.endswith("$"):
            mode = raw[1:-1]
            status = 1 if mode == "2004" and self.bracketed_paste else 2 if mode == "2004" else 0
            self.replies.extend(("\x1b[?%s;%s$y" % (mode, status)).encode())
        elif final == "u" and raw == "?":
            self.replies.extend(b"\x1b[?0u")  # Enhanced keyboard protocol disabled.
        elif final == "u" and raw.startswith((">", "<", "=")):
            self.record_unsupported("enhanced keyboard protocol")
        elif final in "su" and not raw:
            self.screen.save_cursor() if final == "s" else self.screen.restore_cursor()
        elif final == "t" and raw == "18":
            self.replies.extend(("\x1b[8;%s;%st" % (self.rows, self.columns)).encode())
        elif final not in pyte.Stream.csi:
            self.record_unsupported("CSI:%s%s" % (raw, final))
        else:
            if final == "m" and ":" in raw:
                sequence = b"\x1b[" + raw.replace(":", ";").replace(";2;;", ";2;").encode() + b"m"
            self.streams[self.screen].feed(sequence)

    def osc(self, sequence):
        body = sequence[2:].rstrip(b"\a")
        if body.endswith(b"\x1b\\"):
            body = body[:-2]
        if body in (b"10;?", b"11;?"):
            color = b"ffff/ffff/ffff" if body.startswith(b"10") else b"0000/0000/0000"
            self.replies.extend(b"\x1b]" + body[:2] + b";rgb:" + color + b"\x1b\\")
        elif body.startswith((b"0;", b"1;", b"2;")):
            self.streams[self.screen].feed(sequence)
        elif not body.startswith(b"8;"):  # Hyperlink targets remain inert.
            self.record_unsupported("OSC:" + body.split(b";", 1)[0].decode("ascii", "replace"))
