# Labradour

An ncurses IDE for watching CLI agents work. Architecture: [AgentIDEPlan.md](AgentIDEPlan.md). UI specification: [AgentUI.md](AgentUI.md).

The repository currently contains the **Phase 0 feasibility harness**, not the complete recorder or review MVP. It requires Python 3.9+, Git, pyte, watchdog, and wcwidth. Dependency versions are in [requirements.txt](requirements.txt); use `python3 -m pip install -r requirements.txt` with the interpreter you will launch. The tested macOS interpreter is Python 3.9.6; Linux verification remains pending.

Start the deterministic fake agent from the repository root:

```sh
python3 -m labradour run --demo --watch
```

Type `run` in the Agent pane to emit creation, edit, revert, and deletion events. Type `size` to inspect PTY dimensions, or `colors` for terminal rendering samples. Demo files live in a temporary workspace, not your project. Select earlier activity to inspect its captured payload in the Visualization pane.

Launch a real agent in an existing workspace:

```sh
python3 -m labradour run --workspace /path/to/project -- codex
python3 -m labradour run --workspace /path/to/project --side right -- claude
```

These commands wrap the native terminal; tool events require a hook source. Opt into the generated launch-scoped observation hooks with:

```sh
python3 -m labradour run --workspace /path/to/project --hooks codex -- codex
python3 -m labradour run --workspace /path/to/project --hooks claude -- claude
```

The harness writes its configuration in a temporary directory and does not install or rewrite user/project settings. Codex hook trust still applies: review the generated definition through its native `/hooks` workflow. The Codex probe uses `--no-daemon` to keep a dedicated child process. No tool permissions or hook trust checks are bypassed. Native hook delivery/merging is still a verification gate; see [Phase 0 results](docs/Phase0.md).

By default hook payloads are temporary and removed when the harness closes. Supply `--events /path/to/new-spool` to retain the hook files. The native agent itself runs normally in the chosen project. Add `--watch` to display filesystem observations; these are held in memory and attributed to external/unknown rather than assumed to belong to a tool. Content snapshots and durable filesystem history remain Phase 1 work. Watching starts after the PTY child is forked, so this probe does not guarantee capture of startup writes.

The default native watchdog backend is kqueue on macOS and the platform default on Linux. Use `--watch-backend polling` explicitly when needed. Git administration, `.venv`, Python caches, and the hook spool are excluded. This is a feasibility collector, with a bounded queue and a visible dropped-event count.

| Input | Harness action |
| --- | --- |
| `Ctrl-Q` | Quit Labradour from any pane; stop the child process group |
| `Ctrl-]` then `a` / `l` / `v` | Focus Agent / Activity / Visualization |
| `Ctrl-]` then Tab | Cycle focus |
| `Ctrl-]` then `m` | Mirror the agent side |
| `Ctrl-]` then `z` | Maximize/restore focused pane |
| `Ctrl-]` then `b` / `n` | Page back through terminal history / return to live terminal |
| `Ctrl-]` then `+` / `-` | Change agent width |
| `Ctrl-]` then `]` / `[` | Change activity height |
| `Ctrl-]` then `q` | Stop the child process group and close |
| `Ctrl-]` twice | Send literal prefix to the agent |
| `j` / `k` or arrows in Activity | Select an event; pause live follow |
| `f` in Activity | Resume live follow |
| `j` / `k` or arrows in Visualization | Scroll the payload |

When the agent exits, Labradour keeps the activity and visualization panes open for review. Press **Ctrl-Q** to close Labradour, or press **Ctrl-]**, release it, then press plain **q** within two seconds. Ctrl-Q is reserved for Labradour outside bracketed paste; pasted control bytes do not trigger quitting.

Default geometry is half-width Agent plus stacked quarter-screen review panes. Terminals smaller than 100×28 use the focused pane full screen. Session layout adjustments are not yet persisted. Arbitrary arrangements are specified in Phase 3.

Run verification and probes:

```sh
python3 -m unittest discover -s tests -v
python3 -m labradour doctor
python3 -m labradour snapshot-fixture /tmp/labradour-new-snapshot-fixture
python3 -m labradour watch-fixture /tmp/labradour-new-watch-fixture
python3 tools/phase0_probe.py --output /tmp/labradour-probe.json
```

The snapshot fixture requires a fresh directory. It stores immutable manifests in a private bare repository and demonstrates intermediate changes despite an empty net diff. It does not touch your project's index or refs.

To include gitdiffviz's already-built backend in the compatibility probe:

```sh
python3 tools/phase0_probe.py \
  --gitdiffviz /path/to/gitdiffviz/_build/default/bin/main.exe \
  --output /tmp/labradour-probe.json
```

The terminal backend now uses **pyte** for VT parsing, screen editing, UTF-8, wrapping, and history. Adapter code provides separate alternate/primary screens, bracketed paste/application cursor modes, DEC graphics, and pane-local xterm query replies. Curses approximates RGB colors to its palette. Mouse/focus reporting and enhanced keyboard modes remain unsupported and are reported. Real authenticated provider sessions still need validation outside the restricted execution environment. [Phase 0 results and remaining gates](docs/Phase0.md) distinguish automated evidence from unverified behavior.
