import argparse
import curses
import json
import importlib.metadata
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from .hooks import configuration, toml_value
from .snapshots import fixture
from .policy import byte_size, load_policy


def policy_arguments(parser):
    parser.add_argument("--capture-policy", type=Path, help="JSON capture policy file")
    parser.add_argument("--exclude", action="append", help="Additional workspace-relative exclusion glob (repeatable)")
    parser.add_argument("--metadata-only", action="append", help="Record metadata without contents for matching paths")
    parser.add_argument("--no-default-exclusions", action="store_false", dest="use_default_exclusions", default=None,
                        help="Disable optional default exclusions; Git and recorder internals remain excluded")
    for option, name, help_text in (
            ("--max-file-bytes", "max_file_bytes", "Maximum captured bytes per file"),
            ("--max-capture-bytes", "max_capture_bytes", "Maximum content bytes per scan"),
            ("--storage-budget", "storage_budget_bytes", "Hard retained recording-directory budget (minimum 64KiB)"),
            ("--max-event-bytes", "max_event_bytes", "Maximum journal event size (1KiB to 1MiB)")):
        parser.add_argument(option, dest=name, type=byte_size, help=help_text + "; accepts sizes such as 8MiB")


def policy_from_args(args):
    return load_policy(args.capture_policy, **{name: getattr(args, name) for name in
                       ("exclude", "metadata_only", "use_default_exclusions", "max_file_bytes",
                        "max_capture_bytes", "storage_budget_bytes", "max_event_bytes")})


def main():
    parser = argparse.ArgumentParser(description="Labradour native agent launcher and recorder")
    commands = parser.add_subparsers(dest="mode", required=True)
    run = commands.add_parser("run", help="Launch a native CLI or deterministic demo in a PTY")
    run.add_argument("--demo", action="store_true")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--record", type=Path, help="Persist journal and content checkpoints in this private directory")
    run.add_argument("--events", type=Path, help="Hook spool directory; temporary by default")
    run.add_argument("--hooks", choices=["claude", "codex"], help="Add observation-only launch-scoped hooks")
    run.add_argument("--collector", action="store_true", help="Enable Phase 2 authenticated adapter collection (requires --record)")
    run.add_argument("--side", choices=["left", "right"], default="left")
    run.add_argument("--agent-width", type=float, default=.5)
    run.add_argument("--activity-height", type=float, default=.5)
    run.add_argument("--watch", action="store_true", help="Display watchdog filesystem observations (no content capture)")
    run.add_argument("--watch-backend", choices=["native", "polling"], default="native")
    policy_arguments(run)
    run.add_argument("command", nargs=argparse.REMAINDER)
    preview = commands.add_parser("policy", help="Preview effective capture policy without recording or launching")
    preview.add_argument("--workspace", type=Path, default=Path.cwd())
    preview.add_argument("--record", type=Path, help="Include the recording path in the preview")
    preview.add_argument("--events", type=Path, help="Include the hook spool path in the preview")
    policy_arguments(preview)
    health = commands.add_parser("recording-status", help="Show persistent recording health, including storage exhaustion")
    health.add_argument("directory", type=Path)
    prune = commands.add_parser("prune", help="Preview or apply whole-session retention in an inactive recording")
    prune.add_argument("directory", type=Path)
    prune.add_argument("--keep-sessions", type=int, default=10)
    prune.add_argument("--apply", action="store_true", help="Remove the previewed old sessions and reclaim unreferenced loose Git objects")
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
    commands.add_parser("adapter-coverage", help="Show native provider categories still requiring verification")
    args = parser.parse_args()
    if args.mode == "adapter-coverage":
        from .adapters.events import coverage
        print(json.dumps(coverage(), indent=2))
    elif args.mode == "policy":
        try:
            policy = policy_from_args(args)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(policy.describe(args.workspace, [p for p in (args.record, args.events) if p]), indent=2))
    elif args.mode == "recording-status":
        from .storage import read_health
        print(json.dumps(read_health(args.directory), indent=2))
    elif args.mode == "prune":
        from .retention import apply_retention, plan_retention
        try:
            result = apply_retention(args.directory, args.keep_sessions) if args.apply else plan_retention(args.directory, args.keep_sessions)
        except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
            parser.exit(2, "Labradour: " + str(exc) + "\n")
        print(json.dumps(result, indent=2))
    elif args.mode == "history":
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
        if args.collector and not args.record:
            parser.error("--collector requires --record")
        try:
            policy = policy_from_args(args)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        if not args.record and any(getattr(args, name) is not None for name in
                ("capture_policy", "exclude", "metadata_only", "use_default_exclusions", "max_file_bytes",
                 "max_capture_bytes", "storage_budget_bytes", "max_event_bytes")):
            parser.error("capture policy options require --record; use policy to preview them")
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
            if args.record:
                print("Labradour effective capture policy:", file=sys.stderr)
                print(json.dumps(policy.describe(workspace, [args.record, events]), indent=2), file=sys.stderr)
            from .ui import Harness
            harness = Harness(command, workspace, events, args.side, args.agent_width, args.activity_height,
                              args.watch, args.watch_backend, args.record, policy, collector=args.collector)
            try:
                curses.wrapper(harness.run)
            except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
                parser.exit(2, "Labradour: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
