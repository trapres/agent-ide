"""Validated capture policy shared by the scanner, watcher, and CLI preview."""
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re

DEFAULT_EXCLUDED = frozenset({".git", ".venv", "__pycache__", ".agentide-spike", ".labradour",
                             "node_modules", "vendor", "build", "dist", ".aws", ".ssh", ".codex", ".agents"})


def byte_size(value):
    match = re.fullmatch(r"([0-9]+)\s*(B|KiB|MiB|GiB)?", str(value), re.IGNORECASE)
    if not match:
        raise ValueError("use a positive byte count or a size such as 8MiB")
    number = int(match[1]) * {None: 1, "b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}[match[2].lower() if match[2] else None]
    if number <= 0:
        raise ValueError("size must be positive")
    return number


def matches(path, patterns):
    # Match a whole relative path or any ancestor, so a matched directory prunes its subtree.
    names = [path.as_posix(), *[p.as_posix() for p in path.parents if p != Path(".")]]
    return any(fnmatchcase(name, pattern) for name in names for pattern in patterns)


@dataclass(frozen=True)
class CapturePolicy:
    use_default_exclusions: bool = True
    exclude: tuple = field(default_factory=tuple)
    metadata_only: tuple = field(default_factory=tuple)
    max_file_bytes: int = 8 * 1024 * 1024
    max_capture_bytes: int = 64 * 1024 * 1024
    storage_budget_bytes: int = 512 * 1024 * 1024
    max_event_bytes: int = 1024 * 1024

    def __post_init__(self):
        if type(self.use_default_exclusions) is not bool:
            raise ValueError("use_default_exclusions must be a boolean")
        for name in ("max_file_bytes", "max_capture_bytes", "storage_budget_bytes", "max_event_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(name + " must be a positive integer")
        if self.storage_budget_bytes < 64 * 1024:
            raise ValueError("storage_budget_bytes must be at least 64KiB")
        if self.max_event_bytes < 1024 or self.max_event_bytes > 1024 * 1024:
            raise ValueError("max_event_bytes must be between 1KiB and 1MiB")
        for name in ("exclude", "metadata_only"):
            patterns = getattr(self, name)
            if not isinstance(patterns, (tuple, list)) or any(not isinstance(p, str) or not p or
                    p.startswith("/") or "\0" in p or ".." in p.split("/") for p in patterns):
                raise ValueError(name + " must contain nonempty workspace-relative glob patterns")
            object.__setattr__(self, name, tuple(patterns))

    def included(self, path):
        if any(p == ".git" or p.startswith(".labradour-stage-") for p in path.parts):
            return False
        if self.use_default_exclusions and any(p in DEFAULT_EXCLUDED or p == ".env" or p.startswith(".env.") for p in path.parts):
            return False
        return not matches(path, self.exclude)

    def metadata(self, path):
        return matches(path, self.metadata_only)

    def describe(self, workspace, excluded=()):
        return {"schema_version": 1, "workspace": str(Path(workspace).resolve()),
                "mandatory_exclusions": [".git", ".labradour-stage-*", "recording directory", "active hook spool"],
                "default_exclusions": sorted(DEFAULT_EXCLUDED | {".env", ".env.*"}) if self.use_default_exclusions else [],
                "exclude": list(self.exclude), "metadata_only": list(self.metadata_only),
                "excluded_paths": [str(Path(p).resolve()) for p in excluded],
                "max_file_bytes": self.max_file_bytes, "max_capture_bytes": self.max_capture_bytes,
                "storage_budget_bytes": self.storage_budget_bytes, "max_event_bytes": self.max_event_bytes,
                "budget_scope": "sum of regular-file bytes in the recording directory; staging and hook spool are separate",
                "on_budget_exhaustion": "stop recording; preserve saved history; agent continues"}


def load_policy(path=None, **overrides):
    values = {}
    if path:
        values = json.loads(Path(path).read_text())
        if not isinstance(values, dict):
            raise ValueError("capture policy must be a JSON object")
        values = dict(values)
        version = values.pop("schema_version", 1)
        if type(version) is not int or version != 1:
            raise ValueError("unsupported capture policy schema_version")
        unknown = set(values) - set(CapturePolicy.__dataclass_fields__)
        if unknown:
            raise ValueError("unknown capture policy fields: " + ", ".join(sorted(unknown)))
    policy = CapturePolicy(**values)
    changes = {k: v for k, v in overrides.items() if v is not None}
    for name in ("exclude", "metadata_only"):
        if name in changes:
            changes[name] = getattr(policy, name) + tuple(changes[name])
    return replace(policy, **changes)
