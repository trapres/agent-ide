"""Deterministic fake CLI; writes only to the demo workspace chosen by launcher."""
import json
import os
from pathlib import Path
import signal
import sys
import time

# Executed as a file in a temporary cwd, so import its adjacent hook sink.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hooks import emit


def event(kind, **fields):
    emit(dict(hook_event_name=kind, session_id="demo", **fields), os.environ["LABRADOUR_EVENTS_DIR"])


def run():
    event("SessionStart", actor="fake-agent")
    print("\x1b[?2004h\x1b[36mLabradour fixture\x1b[0m — type run, size, colors, or quit.", flush=True)
    signal.signal(signal.SIGWINCH, lambda *_: print("\r\nresize: %s" % (os.get_terminal_size(),), flush=True))
    signal.signal(signal.SIGINT, lambda *_: print("\r\ninterrupted; fixture remains interactive", flush=True))
    while True:
        print("fake> ", end="", flush=True)
        line = sys.stdin.readline()
        if not line or line.strip() == "quit":
            break
        line = line.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
        event("UserPromptSubmit", prompt=line)
        if line == "run":
            operations = [("Write", "file.create", "answer.py", b"answer = 41\n"),
                          ("Edit", "file.modify", "answer.py", b"answer = 42\n"),
                          ("Edit", "file.modify", "answer.py", b"answer = 41\n"),
                          ("Bash", "file.delete", "answer.py", None)]
            for i, (tool, operation, path, contents) in enumerate(operations):
                fields = dict(tool_name=tool, tool_use_id="demo-%s-%s" % (time.time_ns(), i),
                              tool_input={"path": path}, operation=operation)
                event("PreToolUse", **fields)
                if contents is None:
                    Path(path).unlink()
                    preview = "Deleted answer.py; prior contents: answer = 41"
                else:
                    Path(path).write_bytes(contents)
                    preview = contents.decode()
                time.sleep(.15)
                event("PostToolUse", tool_response={"preview": preview}, **fields)
                print("%s: %s" % (operation, path), flush=True)
        elif line == "size":
            print("PTY size: %s" % (os.get_terminal_size(),), flush=True)
        elif line == "colors":
            print("\x1b[1;31mred/bold\x1b[0m  \x1b[32mgreen\x1b[0m  unicode: café 界 é", flush=True)
        else:
            print("received: " + json.dumps(line, ensure_ascii=False), flush=True)
        event("Stop")
    event("SessionEnd")
    print("\x1b[?2004l", end="", flush=True)


if __name__ == "__main__":
    run()

