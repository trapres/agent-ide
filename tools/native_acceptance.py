"""Prepare a private native fixture or summarize its journal without publishing raw payloads."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labradour.adapters.correlation import project
from labradour.recorder import read_history

PROJECT = Path(__file__).resolve().parents[1]


def prepare(provider, parent=None, existing_observer=False):
    root = Path(tempfile.mkdtemp(prefix="labradour-native-acceptance-", dir=parent)).resolve()
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "readme.txt").write_text("LABRADOUR_NATIVE_READ_MARKER\n")
    command = [sys.executable, "-m", "labradour", "run", "--workspace", str(workspace),
               "--record", str(root / "recording"), "--hooks", provider, "--", provider]
    observer = None
    if existing_observer:
        script = root / "observer.py"
        script.write_text('import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n'
                          'with Path(__file__).with_name("observer.jsonl").open("a") as f:\n'
                          ' f.write(json.dumps({"hook":p.get("hook_event_name"),"session_id":p.get("session_id")})+"\\n")\n')
        hook_command = shlex.join([sys.executable, str(script)])
        config = {"hooks": {event: [{"hooks": [{"type": "command", "command": hook_command, "timeout": 3}]}]
                            for event in ("SessionStart", "Stop", "SessionEnd")}}
        native = workspace / (".claude" if provider == "claude" else ".codex")
        native.mkdir(mode=0o700)
        settings = native / ("settings.json" if provider == "claude" else "hooks.json")
        settings.write_text(json.dumps(config, indent=2) + "\n")
        settings.chmod(0o600)
        observer = {"settings": str(settings), "sha256": hashlib.sha256(settings.read_bytes()).hexdigest(),
                    "log": str(root / "observer.jsonl"), "layer": "project"}
    manifest = {"schema_version": 1, "provider": provider, "workspace": str(workspace),
                "recording": str(root / "recording"), "launch_command": shlex.join(command), "existing_observer": observer}
    (root / "acceptance.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def summarize(directory, session=None):
    sessions = read_history(directory)
    if not sessions:
        raise ValueError("recording has no sessions")
    selected = next((s for s in sessions if s["id"] == session), None) if session else sessions[-1]
    if selected is None:
        raise ValueError("unknown session")
    records = read_history(directory, selected["id"])
    projection = project(records, selected["status"])
    facts = [r for r in records if r["kind"] == "adapter.event"]
    hooks = Counter()
    tools = Counter()
    providers = set()
    identities = Counter()
    for r in facts:
        p = r["payload"]
        providers.add(p.get("provider", "unknown"))
        detail = p.get("payload", {})
        hooks[detail.get("hook_event_name", p.get("kind", "unknown"))] += 1
        if detail.get("tool"):
            tools[detail["tool"]] += 1
        for key in ("provider_session_id", "turn_id", "actor_id", "parent_actor_id", "call_id"):
            if p.get(key):
                identities[key] += 1
    boundaries = [r for r in records if r["kind"] == "adapter.boundary"]
    configured = {r["payload"].get("provider") for r in records if r["kind"] == "adapter.configured"}
    versions = {}
    for provider in sorted((providers | configured) & {"claude", "codex"}):
        executable = shutil.which(provider)
        versions[provider] = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                            timeout=10).stdout.strip() if executable else "not-installed"
    # This is a machine evidence summary, never a substitute for human trust,
    # composition, concurrent invocation, or terminal-fidelity observations.
    result = {"schema_version": 1, "scope": "Journal evidence only; human acceptance checks remain separate.",
            "platform": platform.platform(), "python": platform.python_version(),
            "installed_versions_at_review": versions, "session": selected,
            "hook_counts": dict(hooks), "tool_observation_counts": dict(tools),
            "identity_field_counts": dict(identities),
            "boundary_counts": dict(Counter(r["payload"].get("phase", "unknown") for r in boundaries)),
            "unavailable_boundaries": sum(not r["payload"].get("completed") for r in boundaries),
            "action_states": dict(Counter(a["state"] for a in projection["actions"])),
            "result_outcomes": dict(Counter(o for a in projection["actions"] for o in a["result_outcomes"])),
            "action_correlation": dict(Counter(a["correlation_quality"] for a in projection["actions"])),
            "effects": {"count": len(projection["effects"]),
                        "shared": sum(len(e["candidate_action_ids"]) > 1 for e in projection["effects"]),
                        "exclusive_ownership_claims": sum(e["attribution"] != "external-or-unknown" for e in projection["effects"])},
            "actors": len(projection["actors"]), "gap_kinds": dict(Counter(g["kind"] for g in projection["gaps"])),
            "cleanup_gaps": sum(r["kind"] == "process.cleanup-gap" for r in records),
            "native_gate": "requires scenario and human evidence; not inferred from callback counts"}
    manifest = Path(directory).resolve().parent / "acceptance.json"
    if manifest.exists():
        setup = json.loads(manifest.read_text())
        observer = setup.get("existing_observer")
        if observer:
            settings = Path(observer["settings"])
            log = Path(observer["log"])
            native_sessions = {r["payload"].get("provider_session_id") for r in facts}
            delivered = []
            if log.exists():
                if log.stat().st_size > 256 * 1024:
                    raise ValueError("observer log exceeds acceptance limit")
                delivered = [json.loads(line) for line in log.read_text().splitlines()]
            result["existing_observer"] = {
                "layer": observer["layer"],
                "settings_unchanged": settings.exists() and hashlib.sha256(settings.read_bytes()).hexdigest() == observer["sha256"],
                "hook_counts_for_native_session": dict(Counter(r["hook"] for r in delivered if r.get("session_id") in native_sessions))}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    setup = modes.add_parser("prepare", help="Create a disposable workspace and print its launch command")
    setup.add_argument("provider", choices=("claude", "codex"))
    setup.add_argument("--parent", type=Path)
    setup.add_argument("--existing-observer", action="store_true", help="Add a harmless project-local native observer to check composition")
    report = modes.add_parser("report", help="Summarize a saved session without raw prompts/responses")
    report.add_argument("directory", type=Path)
    report.add_argument("--session")
    report.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = prepare(args.provider, args.parent, args.existing_observer) if args.mode == "prepare" else summarize(args.directory, args.session)
    raw = json.dumps(result, indent=2) + "\n"
    if args.mode == "report" and args.output:
        args.output.write_text(raw)
    print(raw, end="")


if __name__ == "__main__":
    main()
