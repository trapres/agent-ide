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
from labradour.hooks import toml_value

PROJECT = Path(__file__).resolve().parents[1]


def observer_config(root, script, log, events):
    script.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    script.write_text('import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n'
                      'with Path(sys.argv[1]).open("a") as f:\n'
                      ' f.write(json.dumps({"hook":p.get("hook_event_name"),"session_id":p.get("session_id")})+"\\n")\n')
    script.chmod(0o600)
    command = shlex.join([sys.executable, str(script), str(log)])
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
                      for event in events}}


def observer_manifest(settings, log, layer):
    return {"settings": str(settings), "sha256": hashlib.sha256(settings.read_bytes()).hexdigest(),
            "log": str(log), "layer": layer}


def prepare(provider, parent=None, existing_observer=False, plugin_observer=False,
            managed_observer=False, managed_only=False):
    if managed_only and not managed_observer:
        raise ValueError("managed-only requires a managed observer fixture")
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
    if plugin_observer:
        plugin = root / 'plugins' / 'labradour-acceptance-observer'
        config = observer_config(root, plugin / 'observer.py', root / 'plugin-observer.jsonl',
                                 ('SessionStart', 'Stop', 'SessionEnd'))
        hooks = plugin / 'hooks' / 'hooks.json'
        hooks.parent.mkdir(mode=0o700)
        hooks.write_text(json.dumps(config, indent=2) + '\n')
        native = plugin / ('.claude-plugin' if provider == 'claude' else '.codex-plugin')
        native.mkdir(mode=0o700)
        (native / 'plugin.json').write_text(json.dumps({
            'name': 'labradour-acceptance-observer', 'version': '1.0.0',
            'description': 'Disposable observation-only native acceptance fixture',
            'author': {'name': 'Labradour acceptance'}}) + '\n')
        manifest['plugin_observer'] = observer_manifest(hooks, root / 'plugin-observer.jsonl', 'plugin')
        if provider == 'claude':
            command.extend(['--plugin-dir', str(plugin)])
        else:
            marketplace = root / '.agents' / 'plugins' / 'marketplace.json'
            marketplace.parent.mkdir(parents=True, mode=0o700)
            marketplace.write_text(json.dumps({'name': 'labradour-acceptance', 'plugins': [{
                'name': 'labradour-acceptance-observer',
                'source': {'source': 'local', 'path': './plugins/labradour-acceptance-observer'},
                'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
                'category': 'Productivity'}]}, indent=2) + '\n')
            manifest['plugin_setup_commands'] = [
                shlex.join(['codex', 'plugin', 'marketplace', 'add', str(root)]),
                'codex plugin add labradour-acceptance-observer@labradour-acceptance']
        manifest['launch_command'] = shlex.join(command)
    if managed_observer:
        managed = root / 'managed'
        config = observer_config(root, managed / 'observer.py', root / 'managed-observer.jsonl',
                                 ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd'))
        if provider == 'claude':
            settings = managed / 'managed-settings.json'
            config['allowManagedHooksOnly'] = managed_only
            settings.write_text(json.dumps(config, indent=2) + '\n')
            target = '/etc/claude-code/managed-settings.json'
        else:
            settings = managed / 'requirements.toml'
            settings.write_text('allow_managed_hooks_only = ' + str(managed_only).lower() + '\n'
                                + 'hooks = ' + toml_value(dict(managed_dir=str(managed), **config['hooks'])) + '\n'
                                + '[features]\nhooks = true\n')
            target = '/etc/codex/requirements.toml'
        settings.chmod(0o600)
        manifest['managed_observer'] = observer_manifest(settings, root / 'managed-observer.jsonl', 'managed')
        manifest['managed_policy'] = {'managed_only': managed_only, 'linux_target': target,
                                     'installed': False, 'scope': 'Fixture only; no system policy files modified.'}
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
            "adapter_delivery_status": [{"provider": r['payload'].get('provider'),
                "status": r['payload'].get('status'), "counts": r['payload'].get('counts', {})}
                for r in records if r['kind'] == 'adapter.delivery'],
            "native_gate": "requires scenario and human evidence; not inferred from callback counts"}
    manifest = Path(directory).resolve().parent / "acceptance.json"
    if manifest.exists():
        setup = json.loads(manifest.read_text())
        for name in ('existing_observer', 'plugin_observer', 'managed_observer'):
            observer = setup.get(name)
            if not observer:
                continue
            settings = Path(observer["settings"])
            log = Path(observer["log"])
            native_sessions = {r["payload"].get("provider_session_id") for r in facts
                               if r["payload"].get("provider_session_id")}
            delivered = []
            if log.exists():
                if log.stat().st_size > 256 * 1024:
                    raise ValueError("observer log exceeds acceptance limit")
                delivered = [json.loads(line) for line in log.read_text().splitlines()]
            result[name] = {
                "layer": observer["layer"],
                "settings_unchanged": settings.exists() and hashlib.sha256(settings.read_bytes()).hexdigest() == observer["sha256"],
                "hook_counts_for_native_session": dict(Counter(r["hook"] for r in delivered if r.get("session_id") in native_sessions)),
                "unmatched_hook_counts": dict(Counter(r["hook"] for r in delivered if r.get("session_id") not in native_sessions))}
            if name == 'managed_observer':
                target = Path(setup['managed_policy']['linux_target'])
                result[name]['installed_policy_matches_fixture'] = (target.exists()
                    and hashlib.sha256(target.read_bytes()).hexdigest() == observer['sha256'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    setup = modes.add_parser("prepare", help="Create a disposable workspace and print its launch command")
    setup.add_argument("provider", choices=("claude", "codex"))
    setup.add_argument("--parent", type=Path)
    setup.add_argument("--existing-observer", action="store_true", help="Add a harmless project-local native observer to check composition")
    setup.add_argument("--plugin-observer", action="store_true", help="Prepare an observation-only local plugin; Codex installation commands are printed, not executed")
    setup.add_argument("--managed-observer", action="store_true", help="Prepare a Linux managed-policy fixture; never install it on the host")
    setup.add_argument("--managed-only", action="store_true", help="Make the managed fixture suppress non-managed hooks")
    report = modes.add_parser("report", help="Summarize a saved session without raw prompts/responses")
    report.add_argument("directory", type=Path)
    report.add_argument("--session")
    report.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == 'prepare' and args.managed_only and not args.managed_observer:
        setup.error('--managed-only requires --managed-observer')
    result = prepare(args.provider, args.parent, args.existing_observer, args.plugin_observer,
                     args.managed_observer, args.managed_only) if args.mode == "prepare" else summarize(args.directory, args.session)
    raw = json.dumps(result, indent=2) + "\n"
    if args.mode == "report" and args.output:
        args.output.write_text(raw)
    print(raw, end="")


if __name__ == "__main__":
    main()
