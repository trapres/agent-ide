"""Private Git history built from immutable manifests."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


class ScratchHistory:
    def __init__(self, directory, session="fixture", resume=False, storage=None):
        self.storage = storage
        self.blob_ids = {}
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.repo = self.directory / "history.git"
        if self.repo.exists() and not resume:
            raise ValueError("fixture requires a fresh directory; existing history is preserved")
        # Git-related inherited variables must not redirect fixture operations.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="Labradour", GIT_AUTHOR_EMAIL="recorder@localhost",
                        GIT_COMMITTER_NAME="Labradour", GIT_COMMITTER_EMAIL="recorder@localhost")
        if not self.repo.exists():
            subprocess.run(["git", "init", "--bare", str(self.repo)], env=self.env,
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not session or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in session):
            raise ValueError("invalid session ID")
        self.ref = "refs/heads/sessions/" + session
        result = subprocess.run(["git", "--git-dir=" + str(self.repo), "rev-parse", "--verify", self.ref],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.head = result.stdout.decode().strip() if result.returncode == 0 else None
        self.tree = self.git("rev-parse", self.head + "^{tree}").decode().strip() if self.head else None

    def git(self, *arguments, data=None, env=None):
        return subprocess.run(["git", "--git-dir=" + str(self.repo), *arguments],
                              input=data, env=env or self.env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def checkpoint(self, manifest, reason):
        """manifest: relative path -> (Git mode string, immutable bytes)."""
        if self.storage:
            return self.storage.checkpoint(self, manifest, reason)
        entries, contents = [], {}
        for path, (mode, content) in sorted(manifest.items()):
            parts = path.split("/")
            if (not path or path.startswith("/") or "\0" in path or
                    any(p in ("", ".", "..", ".git") for p in parts)):
                raise ValueError("invalid manifest path: %r" % path)
            if mode not in ("100644", "100755", "120000"):
                raise ValueError("unsupported mode")
            digest = hashlib.sha256(content).digest()
            contents[digest] = content
            entries.append((path, mode, digest))
        with tempfile.TemporaryDirectory(dir=str(self.directory)) as temporary:
            temporary = Path(temporary)
            pending = [digest for digest in contents if digest not in self.blob_ids]
            paths = []
            for index, digest in enumerate(pending):
                file = temporary / str(index)
                file.write_bytes(contents[digest])
                paths.append(json.dumps(str(file), ensure_ascii=False).encode() + b"\n")
            if paths:
                oids = self.git("hash-object", "-w", "--no-filters", "--stdin-paths", data=b"".join(paths)).splitlines()
                if len(oids) != len(pending):
                    raise RuntimeError("Git returned an incomplete blob batch")
                self.blob_ids.update(zip(pending, oids))
            env = dict(self.env, GIT_INDEX_FILE=str(temporary / "index"))
            self.git("read-tree", "--empty", env=env)
            index_entries = [mode.encode() + b" " + self.blob_ids[digest] + b"\t" + os.fsencode(path) + b"\0"
                             for path, mode, digest in entries]
            self.git("update-index", "-z", "--index-info", data=b"".join(index_entries), env=env)
            tree = self.git("write-tree", env=env).decode().strip()
        # Bound the cache to contents of the current manifest.
        self.blob_ids = {digest: self.blob_ids[digest] for digest in contents}
        if tree == self.tree:
            return self.head
        args = ["commit-tree", tree]
        if self.head:
            args += ["-p", self.head]
        commit = self.git(*args, data=(reason + "\n").encode()).decode().strip()
        self.git("update-ref", self.ref, commit, self.head or "0" * 40)
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
