# Labradour

An ncurses IDE for watching CLI agents work. Architecture: [AgentIDEPlan.md](AgentIDEPlan.md). UI specification: [AgentUI.md](AgentUI.md). User controls and review workflow: [UIGuide.md](UIGuide.md).

For a hands-on walkthrough of current behavior, recorder checks, and features still planned, see [Phase 1 manual testing guide](docs/Phase1TestGuide.md).

The proposed integration contract for customizable visualization plugins is in [VizApi.md](docs/VizApi.md).

Phase 2 work is tracked in [Tasks.md](Tasks.md). The authenticated collector, Claude/Codex mappings, boundary receipts, fixtures, and coverage matrix are described in [Phase2.md](docs/Phase2.md). Read-only correlation is available through `actions`; real macOS delivery, denial/concurrency and Linux Codex delivery have measured evidence; the broader native gate remains open. Follow [Phase2TestGuide.md](docs/Phase2TestGuide.md).

The repository contains the **Phase 0 harness and the accepted Phase 1 generic recorder**. The full review MVP remains later work. It requires Python 3.9+, Git, pyte, watchdog, and wcwidth. Dependency versions are in [requirements.txt](requirements.txt); use `python3 -m pip install -r requirements.txt` with the interpreter you will launch. Automated acceptance passes on macOS/Python 3.9.6 and Linux arm64/Python 3.11.17 in Docker. Measurements and reproduction commands are in [Recorder acceptance](docs/RecorderAcceptance.md).

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

Without recording, hook payloads are temporary; `--events /path/to/new-spool` retains the probe files. With `--record --hooks`, facts use the authenticated collector and persist in the journal; acknowledged spool files are removed. The native agent runs in the chosen project. Add `--watch` for in-memory filesystem observations, attributed to external/unknown. Use `--record` for durable checkpoints. Observation-only watching starts after launch and does not guarantee startup writes.

The default native watchdog backend is kqueue on macOS and the platform default on Linux. Use `--watch-backend polling` explicitly when needed. Git administration, `.venv`, Python caches, and the hook spool are excluded. This is a feasibility collector, with a bounded queue and a visible dropped-event count.

| Input | Harness action |
| --- | --- |
| `Ctrl-Q` | Quit Labradour from any pane; stop the child process group |
| `Ctrl-]` then `a` / `l` / `v` | Focus Agent / Activity / Visualization |
| `Ctrl-]` then Tab | Cycle focus |
| `Ctrl-]` then `m` | Mirror the agent side |
| `Ctrl-]` then `z` | Maximize/restore focused pane |
| `Ctrl-]` then `p` | Toggle effective capture policy in Visualization |
| `Ctrl-]` then `b` / `n` | Page back through terminal history / return to live terminal |
| `Ctrl-]` then `+` / `-` | Change agent width |
| `Ctrl-]` then `]` / `[` | Change activity height |
| `Ctrl-]` then `q` | Stop the child process group and close |
| `Ctrl-]` twice | Send literal prefix to the agent |
| `j` / `k` or arrows in Activity | Select an event; pause live follow |
| `f` in Activity | Resume live follow |
| `j` / `k` or arrows in Visualization | Scroll the payload |

When the agent exits, Labradour keeps the activity and visualization panes open for review. Press **Ctrl-Q** to close Labradour, or press **Ctrl-]**, release it, then press plain **q** within two seconds. Ctrl-Q is reserved for Labradour outside bracketed paste; pasted control bytes do not trigger quitting.

Save a session's journal and intermediate file contents:

```sh
python3 -m labradour run --workspace /path/to/project --record /path/to/private-recording -- codex
python3 -m labradour history /path/to/private-recording
python3 -m labradour history /path/to/private-recording --session SESSION_ID
python3 -m labradour diff /path/to/private-recording BEFORE_COMMIT AFTER_COMMIT
```

`--record` includes filesystem watching and waits for baseline capture before starting the agent. Reuse the recording directory for additional sessions in the same workspace. It preserves intermediate changes in private Git history without modifying the project's index or refs. Full checkpoint commit IDs are available in `snapshot.completed` journal records. The live Visualization pane currently shows detail cards; use `diff` for historical source comparisons.

Capture excludes Git administration, recorder data, common credential directories, `.env` files, and dependency/build directories. Large files are metadata-only; recording enforces a hard retained-file-byte budget. Temporary staging requires separate disk space. See [Phase 1 implementation and limits](docs/Phase1.md) before recording a workspace, and [UIGuide.md](UIGuide.md) for navigation.

Preview or customize capture policy:

```sh
python3 -m labradour policy --workspace /path/to/project --exclude private --storage-budget 256MiB
python3 -m labradour run --workspace /path/to/project --record /path/to/private-recording \
  --exclude private --metadata-only 'assets/**' --max-file-bytes 4MiB --storage-budget 256MiB -- codex
python3 -m labradour recording-status /path/to/private-recording
```

Use `--capture-policy FILE` for reusable JSON rules. Size options override file values; repeated exclusion/metadata patterns extend them. The effective policy is printed before recording and available with **Ctrl-] then p**. Budget exhaustion stops recording while the agent continues, preserves saved history, and reports the reason through a reserved health record. [UIGuide.md](UIGuide.md) documents policy examples and budget scope.

Preview and apply retention after closing the recorder:

```sh
python3 -m labradour prune /path/to/private-recording --keep-sessions 5
python3 -m labradour prune /path/to/private-recording --keep-sessions 5 --apply
```

Retention removes whole older sessions, compacts their journal rows, deletes their private session refs/policy files, and reclaims unreferenced loose Git objects. Shared/surviving evidence and packed objects remain protected. The writer lock prevents cleanup during recording. Interrupted pruning resumes on the next launch or applied prune; owned abandoned staging directories are cleaned automatically. Disk-full failures stop recording while the agent continues, with a best-effort reserved health-slot diagnostic. See [UIGuide.md](UIGuide.md) for recovery and retention limits.

Default geometry is half-width Agent plus stacked quarter-screen review panes. Terminals smaller than 100×28 use the focused pane full screen. Session layout adjustments are not yet persisted. [Phase 3's completed specification](docs/Phase3.md) defines arbitrary split trees, named presets, editing, persistence and focus/resize acceptance; Phase 4 implements those features.

[Phase 4's layout system](docs/Phase4.md) provides four presets through `--layout`: `default`, `agent-right`, `agent-top`, and `visualization-top`, plus custom names from layered v1 JSON files. Try `python3 -m labradour run --demo --layout agent-top`. Inspect with `python3 -m labradour layout status`, save explicitly with `layout save NAME --scope user|workspace`, or load a file with `run --layout-config FILE`. Ctrl-] then `i` displays source/geometry diagnostics. The in-session editor and full review views are subsequent slices.

Run verification and probes:

```sh
python3 -m unittest discover -s tests -v
python3 tools/performance_probe.py --files 1000 --iterations 20
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

The terminal backend now uses **pyte** for VT parsing, screen editing, UTF-8, wrapping, and history. Adapter code provides separate alternate/primary screens, bracketed paste/application cursor modes, DEC graphics, and pane-local xterm query replies. Curses approximates RGB colors to its palette. Mouse/focus reporting and enhanced keyboard modes remain unsupported and are reported. The user has verified that both native providers launch through Labradour. Core macOS delivery/denial/concurrency and Linux Codex delivery are measured. Human visual checks, Linux Claude sign-in and additional settings layers remain pending. [Phase 0 results and remaining gates](docs/Phase0.md) distinguish automated evidence from unverified behavior.
