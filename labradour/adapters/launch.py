"""Compose launch-only hook configuration; never edit provider settings files."""
import copy
import json
from pathlib import Path

from labradour.hooks import configuration, toml_value


def merge_settings(existing, added):
    result = copy.deepcopy(existing)
    for key, value in added.items():
        if key == "hooks":
            if not isinstance(value, dict) or not isinstance(result.get(key, {}), dict):
                raise ValueError("hooks settings must be an object")
            hooks = result.setdefault(key, {})
            for event, groups in value.items():
                if not isinstance(groups, list) or not isinstance(hooks.get(event, []), list):
                    raise ValueError("hook matcher groups must be arrays")
                hooks[event] = hooks.get(event, []) + copy.deepcopy(groups)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_settings(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def compose(provider, command, temporary, workspace, events, authenticated=False):
    config = configuration(provider, directory=events, authenticated=authenticated)
    if provider == "codex":
        # File-layer hooks are composed by Codex. Ambiguous explicit CLI hook
        # overrides are refused rather than discarding another observation hook.
        for index, arg in enumerate(command[1:], 1):
            if arg == "--":
                break
            value = command[index + 1] if arg in ("-c", "--config") and index + 1 < len(command) else (
                arg.split("=", 1)[1] if arg.startswith("--config=") else arg[2:] if arg.startswith("-c") else "")
            if value.split("=", 1)[0].strip().startswith("hooks"):
                raise ValueError("--hooks cannot compose explicit Codex CLI hooks overrides; keep existing hooks in native settings")
        return [command[0], "--no-daemon", "-c", "hooks=" + toml_value(config["hooks"]), *command[1:]]
    if provider != "claude":
        raise ValueError("unsupported provider")
    settings, arguments = {}, []
    index = 1
    while index < len(command):
        arg = command[index]
        if arg == "--":
            arguments.extend(command[index:])
            break
        if arg == "--settings" or arg.startswith("--settings="):
            if arg == "--settings":
                index += 1
                if index == len(command):
                    raise ValueError("--settings requires a value")
                value = command[index]
            else:
                value = arg.split("=", 1)[1]
            content = json.loads(value) if value.lstrip().startswith("{") else json.loads((Path(workspace) / value).read_text())
            if not isinstance(content, dict):
                raise ValueError("--settings must contain an object")
            settings = merge_settings(settings, content)
        else:
            arguments.append(arg)
        index += 1
    settings = merge_settings(settings, config)
    path = Path(temporary) / "claude-hooks.json"
    path.write_text(json.dumps(settings))
    path.chmod(0o600)
    return [command[0], "--settings", str(path), *arguments]
