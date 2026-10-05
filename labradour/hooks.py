"""Standalone observation-only hook sink for Phase 0 connectivity probes.

No provider decisions or model context are returned. One atomic file per event
avoids interleaved writes by parallel hook processes. Not a production journal.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import uuid


def emit(payload, directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {"schema_version": 1, "event_id": uuid.uuid4().hex,
              "received_ns": time.time_ns(), "payload": payload}
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=str(root))
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(record, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, root / (record["event_id"] + ".json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record


def configuration(provider, python=None, directory=None, authenticated=False):
    command = "%s %s" % (shlex.quote(python or sys.executable), shlex.quote(str(Path(__file__).resolve())))
    if directory is not None and not authenticated:
        command += " --events " + shlex.quote(str(Path(directory).resolve()))
    if authenticated:
        command += " --provider " + shlex.quote(provider)
    events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
              "PermissionRequest", "SubagentStart", "SubagentStop", "Stop", "SessionEnd"]
    if provider == "claude":
        events += ["PostToolUseFailure"]
    elif provider != "codex":
        raise ValueError("unsupported provider")
    if authenticated:
        from .adapters.providers import MAPPINGS
        events = list(MAPPINGS[provider])
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command,
                                          "timeout": 3}]}] for event in events}}


def toml_value(value):
    if isinstance(value, dict):
        return "{" + ",".join(k + "=" + toml_value(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(toml_value(v) for v in value) + "]"
    return json.dumps(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", choices=["claude", "codex"])
    parser.add_argument("--events", type=Path)
    parser.add_argument("--provider", choices=["claude", "codex"])
    args = parser.parse_args()
    if args.provider:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if args.config:
        print(json.dumps(configuration(args.config, directory=args.events), indent=2))
        return 0
    event = None
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("hook payload exceeds Phase 0 limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        if args.provider:
            # File execution must work after the child changes its cwd.
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from labradour.adapters.providers import normalize
            from labradour.adapters.collector import submit, wait_receipt
            event = normalize(args.provider, payload)
            submit(event)
            if event.get("boundary") and not wait_receipt(event["event_id"], timeout=1.5).get("completed"):
                raise ValueError("boundary capture unavailable")
        else:
            directory = args.events or os.environ.get("LABRADOUR_EVENTS_DIR")
            if directory:
                emit(payload, directory)
    except Exception as exc:
        if args.provider:
            try:
                from labradour.adapters.collector import submit
                submit({"schema_version": 1, "event_id": uuid.uuid4().hex, "provider": args.provider,
                        "kind": "adapter.health", "payload": {"error": "hook capture or boundary unavailable",
                        "error_type": type(exc).__name__, "related_event_id": event.get("event_id") if event else None,
                        "phase": event.get("boundary") if event else None}})
            except Exception:
                pass
        # Observation never changes a tool's authorization or result.
        print("Labradour hook capture unavailable: %s" % type(exc).__name__, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
