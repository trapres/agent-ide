"""Tokenize raw input without treating pasted IDE prefixes as commands."""
import time


class InputRouter:
    def __init__(self):
        self.prefix = False
        self.prefix_time = 0
        self.escape = bytearray()
        self.escape_time = 0
        self.paste = False

    def feed(self, data):
        result = []
        for byte in data:
            if self.escape:
                self.escape.append(byte)
                token = bytes(self.escape)
                if token == b"\x1b[":
                    continue
                if len(token) == 2 and byte == ord("O"):
                    continue
                if len(token) >= 3 and not 64 <= byte <= 126 and len(token) < 128:
                    continue
                self.escape.clear()
                if token == b"\x1b[200~":
                    self.paste = True
                    result.append(("paste-start", token))
                elif token == b"\x1b[201~":
                    self.paste = False
                    result.append(("paste-end", token))
                else:
                    result.append(("paste" if self.paste else "key", token))
            elif byte == 17 and not self.paste:
                # A global exit key must work even with an IDE prefix pending.
                self.prefix = False
                result.append(("command", b"q"))
            elif self.prefix and not self.paste:
                self.prefix = False
                if byte == 27:
                    continue
                result.append(("literal" if byte == 29 else "command", bytes([byte])))
            elif byte == 29 and not self.paste:
                self.prefix = True
                self.prefix_time = time.monotonic()
            elif byte == 27:
                self.escape.append(byte)
                self.escape_time = time.monotonic()
            else:
                result.append(("paste" if self.paste else "key", bytes([byte])))
        return result

    def expire(self):
        if self.prefix and time.monotonic() - self.prefix_time > 2:
            self.prefix = False
        # A standalone Escape must reach the CLI without waiting for another key.
        if self.escape and time.monotonic() - self.escape_time > .05:
            token = bytes(self.escape)
            self.escape.clear()
            return [("paste" if self.paste else "key", token)]
        return []
