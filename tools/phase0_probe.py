"""Reproducible local probes; never submit a model prompt or edit user config."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import importlib.metadata
import platform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labradour.hooks import configuration, toml_value
from labradour.pty_process import PtyProcess
from labradour.snapshots import fixture
from labradour.terminal import Terminal
from labradour.filesystem import filesystem_probe


def native_probe(provider, directory):
    executable = shutil.which(provider)
    if not executable:
        return {"available": False}
    root = Path(directory)
    events = root / "events"
    events.mkdir()
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    config = configuration(provider, directory=events)
    if provider == "codex":
        command = [executable, "--no-daemon", "--strict-config", "-s", "read-only",
                   "-c", "hooks=" + toml_value(config["hooks"]),
                   "-c", "sqlite_home=" + json.dumps(str(root / "state")),
                   "-c", "log_dir=" + json.dumps(str(root / "logs"))]
    else:
        settings = root / "settings.json"
        settings.write_text(json.dumps(config))
        command = [executable, "--setting-sources", "", "--settings", str(settings)]
    env = dict(os.environ, LABRADOUR_EVENTS_DIR=str(events))
    if provider == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(root / "claude-config")
    terminal = Terminal(36, 68)
    child = PtyProcess(command, root, 36, 68, env)
    received = 0
    deadline = time.monotonic() + 4
    try:
        while time.monotonic() < deadline:
            data = child.read()
            received += len(data)
            replies = terminal.feed(data)
            if replies:
                child.send(replies)
            if child.poll() is not None:
                break
            time.sleep(.02)
        screen = [line.rstrip() for line in terminal.display if line.strip()]
        event_names = [json.loads(p.read_text())["payload"].get("hook_event_name") for p in events.glob("*.json")]
        result = {"available": True, "version": version, "startup_bytes": received,
                  "process_exited_during_probe": child.status,
                  "observed_hooks": sorted(event_names), "unsupported_vt": sorted(terminal.unsupported),
                  "startup_screen": screen, "model_prompt_submitted": False,
                  "hook_trust_bypassed": False}
    finally:
        child.close()
    return result


def gitdiffviz_probe(binary, directory):
    root = Path(directory)
    result = fixture(root / "scratch")
    base, target = result["baseline"], result["middle"]
    def invoke(*args):
        p = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=20)
        return {"exit_code": p.returncode, "stderr": p.stderr.strip()}
    report = {"binary": str(binary), "bare_extract": invoke(
        "extract-diff", "--repo", result["repo"], "--base", base, "--target", target,
        "--out", str(root / "bare.json"))}
    checkout = root / "checkout"
    # --no-hardlinks makes the export independent of the retained scratch repo.
    subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", "--branch", "sessions/fixture",
                    result["repo"], str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", target], check=True, capture_output=True)
    diff = root / "diff.json"
    report["checkout_extract"] = invoke("extract-diff", "--repo", str(checkout), "--base", base,
                                        "--target", target, "--out", str(diff))
    if report["checkout_extract"]["exit_code"] == 0:
        report["scene_build"] = invoke("build-scene", "--diff", str(diff), "--out", str(root / "scene.json"))
        report["structured_diff_top_level"] = list(json.loads(diff.read_text()))
    report["empty_diff_extract"] = invoke("extract-diff", "--repo", str(checkout), "--base", base,
                                          "--target", result["final"], "--out", str(root / "empty.json"))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitdiffviz", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="labradour-probes-") as temporary:
        root = Path(temporary)
        report = {"platform": platform.system(), "python": platform.python_version(),
                  "dependencies": {n: importlib.metadata.version(n) for n in ("pyte", "watchdog", "wcwidth")},
                  "terminal_backend": Terminal.backend,
                  "filesystem": {b: filesystem_probe(root / ("filesystem-" + b), backend=b)
                                 for b in ("native", "polling")}}
        for provider in ("codex", "claude"):
            (root / provider).mkdir()
            report[provider] = native_probe(provider, root / provider)
        if args.gitdiffviz:
            (root / "gitdiffviz").mkdir()
            report["gitdiffviz"] = gitdiffviz_probe(args.gitdiffviz.resolve(), root / "gitdiffviz")
        # Temporary paths are artifacts, not meaningful persisted identifiers.
        encoded = json.dumps(report, indent=2).replace(str(root), "<probe-dir>")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
        print(encoded)


if __name__ == "__main__":
    main()
