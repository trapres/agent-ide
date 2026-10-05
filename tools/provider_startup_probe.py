"""Observe native startup only; submits no prompt and answers no trust dialog."""
import argparse
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labradour.pty_process import PtyProcess
from labradour.recorder import read_history
from labradour.terminal import Terminal


def probe(provider, seconds):
    executable = shutil.which(provider)
    if not executable:
        return {"status": "not-installed"}
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="labradour-native-startup-") as temporary:
        root = Path(temporary)
        workspace, store = root / "workspace", root / "recording"
        workspace.mkdir()
        child = PtyProcess([sys.executable, "-m", "labradour", "run", "--workspace", str(workspace),
                            "--record", str(store), "--hooks", provider, "--", executable],
                           Path(__file__).resolve().parents[1], 40, 140)
        terminal = Terminal(40, 140)
        def drain():
            # Emulate the outer terminal's query replies without retaining bytes.
            output = child.read()
            if output:
                replies = terminal.feed(output)
                if replies:
                    child.send(replies)
            child.flush()
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and child.poll() is None:
                drain()
                time.sleep(.02)
            child.send(b"\x11")
            deadline = time.monotonic() + 8
            while child.poll() is None and time.monotonic() < deadline:
                drain()
                time.sleep(.02)
            exit_code = child.poll()
        finally:
            child.close()
        result = {"version": version, "launcher_exit": exit_code,
                  "forced_cleanup": exit_code is None,
                  "prompt_submitted": False, "trust_dialog_answered": False,
                  "status": "startup-only", "callback_counts": {}, "boundary_count": 0, "cleanup_gaps": 0}
        if (store / "journal.sqlite").exists():
            sessions = read_history(store)
            if sessions:
                result["session_status"] = sessions[-1]["status"]
                for event in read_history(store, sessions[-1]["id"]):
                    if event["kind"] == "adapter.event":
                        hook = event["payload"].get("payload", {}).get("hook_event_name", event["payload"].get("kind"))
                        result["callback_counts"][hook] = result["callback_counts"].get(hook, 0) + 1
                    elif event["kind"] == "adapter.boundary":
                        result["boundary_count"] += 1
                    elif event["kind"] == "process.cleanup-gap":
                        result["cleanup_gaps"] += 1
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 20:
        parser.error("seconds must be between 1 and 20")
    report = {"schema_version": 1, "platform": platform.platform(), "python": platform.python_version(),
              "scope": "Startup only; no model prompt, no hook trust bypass, no terminal transcript retained.",
              "providers": {provider: probe(provider, args.seconds) for provider in ("claude", "codex")}}
    raw = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(raw + "\n")
    print(raw)


if __name__ == "__main__":
    main()
