"""Immutable-manifest Git spike. No filesystem watcher or retention policy yet."""
import os
from pathlib import Path
import subprocess
import tempfile


class ScratchHistory:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.repo = self.directory / "history.git"
        if self.repo.exists():
            raise ValueError("fixture requires a fresh directory; existing history is preserved")
        # Git-related inherited variables must not redirect fixture operations.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="Labradour fixture", GIT_AUTHOR_EMAIL="fixture@localhost",
                        GIT_COMMITTER_NAME="Labradour fixture", GIT_COMMITTER_EMAIL="fixture@localhost")
        subprocess.run(["git", "init", "--bare", str(self.repo)], env=self.env,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.head = None
        self.tree = None

    def git(self, *arguments, data=None, env=None):
        return subprocess.run(["git", "--git-dir=" + str(self.repo), *arguments],
                              input=data, env=env or self.env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def checkpoint(self, manifest, reason):
        """manifest: relative path -> (Git mode string, immutable bytes)."""
        entries = []
        for path, (mode, content) in sorted(manifest.items()):
            parts = path.split("/")
            if (not path or path.startswith("/") or "\0" in path or
                    any(p in ("", ".", "..", ".git") for p in parts)):
                raise ValueError("invalid manifest path: %r" % path)
            if mode not in ("100644", "100755", "120000"):
                raise ValueError("unsupported mode")
            oid = self.git("hash-object", "-w", "--stdin", data=content).strip()
            entries.append(mode.encode() + b" " + oid + b"\t" + os.fsencode(path) + b"\0")
        with tempfile.TemporaryDirectory(dir=str(self.directory)) as temporary:
            env = dict(self.env, GIT_INDEX_FILE=str(Path(temporary) / "index"))
            self.git("read-tree", "--empty", env=env)
            self.git("update-index", "-z", "--index-info", data=b"".join(entries), env=env)
            tree = self.git("write-tree", env=env).decode().strip()
        if tree == self.tree:
            return self.head
        args = ["commit-tree", tree]
        if self.head:
            args += ["-p", self.head]
        commit = self.git(*args, data=(reason + "\n").encode()).decode().strip()
        self.git("update-ref", "refs/heads/sessions/fixture", commit, self.head or "0" * 40)
        self.head, self.tree = commit, tree
        return commit

    def diff(self, before, after):
        return self.git("diff", "--no-ext-diff", "--no-textconv", before, after).decode("utf-8", "replace")


def fixture(directory):
    history = ScratchHistory(directory)
    initial = {"source.py": ("100644", b"value = 1\n"),
               "old.txt": ("100644", b"keep in history\n")}
    baseline = history.checkpoint(initial, "baseline")
    changed = dict(initial, **{"source.py": ("100644", b"value = 2\n"),
                              "created.py": ("100644", b"created = True\n")})
    del changed["old.txt"]
    middle = history.checkpoint(changed, "edit create delete")
    final = history.checkpoint(initial, "revert")
    return {"repo": str(history.repo), "baseline": baseline, "middle": middle,
            "final": final, "intermediate_diff": history.diff(baseline, middle),
            "net_diff": history.diff(baseline, final)}
