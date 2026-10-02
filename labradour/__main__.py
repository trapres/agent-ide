import argparse
import curses
import json
import importlib.metadata
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .hooks import configuration, toml_value
from .snapshots import fixture


def main():
    parser = argparse.ArgumentParser(description="Labradour native agent launcher and recorder")
    commands = parser.add_subparsers(dest="mode", required=True)
    run = commands.add_parser("run", help="Launch a native CLI or deterministic demo in a PTY")
    run.add_argument("--demo", action="store_true")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--record", type=Path, help="Persist journal and content checkpoints in this private directory")
    run.add_argument("--events", type=Path, help="Hook spool directory; temporary by default")
    run.add_argument("--hooks", choices=["claude", "codex"], help="Add observation-only launch-scoped hooks")
    run.add_argument("--side", choices=["left", "right"], default="left")
    run.add_argument("--agent-width", type=float, default=.5)
    run.add_argument("--activity-height", type=float, default=.5)
    run.add_argument("--watch", action="store_true", help="Display watchdog filesystem observations (no content capture)")
    run.add_argument("--watch-backend", choices=["native", "polling"], default="native")
    run.add_argument("command", nargs=argparse.REMAINDER)
    history = commands.add_parser("history", help="List saved sessions or print a session journal")
    history.add_argument("directory", type=Path)
    history.add_argument("--session")
    diff = commands.add_parser("diff", help="Compare two saved checkpoint commits")
    diff.add_argument("directory", type=Path)
    diff.add_argument("before")
    diff.add_argument("after")
    snapshot = commands.add_parser("snapshot-fixture", help="Create/edit/delete/revert in a private bare repo")
    snapshot.add_argument("directory", type=Path)
    watch_fixture = commands.add_parser("watch-fixture", help="Probe create/edit/rename/delete in an empty directory")
    watch_fixture.add_argument("directory", type=Path)
    watch_fixture.add_argument("--backend", choices=["native", "polling"], default="native")
    hooks = commands.add_parser("hook-config", help="Print observation-only probe configuration; installs nothing")
    hooks.add_argument("provider", choices=["codex", "claude"])
    commands.add_parser("doctor", help="List prerequisites without reading credentials")
    args = parser.parse_args()
    if args.mode == "history":
        from .recorder import read_history
        print(json.dumps(read_history(args.directory, args.session), indent=2))
    elif args.mode == "diff":
        # Read-only Git operation; do not create a store when reviewing.
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for revision in (args.before, args.after):
            if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
                parser.error("diff requires full checkpoint commit IDs from the journal")
        result = subprocess.run(["git", "--git-dir=" + str(args.directory.resolve() / "history.git"),
                                 "diff", "--no-ext-diff", "--no-textconv", args.before, args.after, "--"], env=env)
        if result.returncode:
            raise SystemExit(result.returncode)
    elif args.mode == "snapshot-fixture":
        print(json.dumps(fixture(args.directory), indent=2))
    elif args.mode == "watch-fixture":
        from .filesystem import filesystem_probe
        print(json.dumps(filesystem_probe(args.directory, backend=args.backend), indent=2))
    elif args.mode == "hook-config":
        print(json.dumps(configuration(args.provider), indent=2))
    elif args.mode == "doctor":
        for name in ("git", "codex", "claude", "opam"):
            path = shutil.which(name)
            print("%s: %s" % (name, path or "not installed"))
            if path and name in ("git", "codex", "claude"):
                result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
                print("  " + result.stdout.strip())
        for package in ("pyte", "watchdog", "wcwidth"):
            try:
                print("%s: %s" % (package, importlib.metadata.version(package)))
            except importlib.metadata.PackageNotFoundError:
                print("%s: missing; install requirements.txt with this Python interpreter" % package)
        print("Terminal backend: pyte with xterm extensions")
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("run requires an interactive terminal")
        command = args.command
        if command and command[0] == "--":
            command = command[1:]
        if args.demo and command:
            parser.error("choose --demo or a command")
        if not args.demo and not command:
            parser.error("supply --demo or -- codex / -- claude / another command")
        with tempfile.TemporaryDirectory(prefix="labradour-phase0-") as temporary:
            root = Path(temporary)
            events = args.events or root / "events"
            events.mkdir(parents=True, exist_ok=True, mode=0o700)
            workspace = args.workspace.resolve()
            if args.demo:
                workspace = root / "workspace"
                workspace.mkdir()
                command = [sys.executable, str(Path(__file__).with_name("demo.py"))]
            if not workspace.is_dir():
                parser.error("workspace must be an existing directory")
            if args.hooks:
                if args.demo or Path(command[0]).name != args.hooks:
                    parser.error("--hooks must match the launched codex or claude executable")
                config = configuration(args.hooks, directory=events)
                if args.hooks == "claude":
                    settings = root / "claude-hooks.json"
                    settings.write_text(json.dumps(config))
                    command = [command[0], "--settings", str(settings), *command[1:]]
                else:
                    command = [command[0], "--no-daemon", "-c", "hooks=" + toml_value(config["hooks"]), *command[1:]]
            from .ui import Harness
            harness = Harness(command, workspace, events, args.side, args.agent_width, args.activity_height,
                              args.watch, args.watch_backend, args.record)
            curses.wrapper(harness.run)


if __name__ == "__main__":
    main()
