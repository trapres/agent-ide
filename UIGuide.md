# Labradour UI guide

Labradour runs your agent in its native terminal and puts activity and review alongside it. This guide describes the current controls and identifies features planned for later phases.

## Start a session

From the repository root, launch an agent in your workspace:

```sh
python3 -m labradour run --workspace /path/to/project -- codex
python3 -m labradour run --workspace /path/to/project -- claude
```

To save filesystem history, choose a private recording directory. Reusing it creates a new session and preserves older sessions:

```sh
python3 -m labradour run --workspace /path/to/project \
  --record /path/to/labradour-recording -- codex
```

Add `--hooks codex` or `--hooks claude` for launch-scoped provider hooks. Follow the provider's normal trust and approval prompts. Filesystem recording works without hooks; it labels observations as external/unknown rather than assigning them to an agent tool. Hook coverage still needs provider-specific verification.

Recording stores included workspace contents locally, including untracked and already modified files. Default exclusions cover Git administration, dependency/build directories, common credential directories, `.env` files, and recorder data. See [Phase 1 notes](docs/Phase1.md) for the exact policy and limits. A recording is intended for one workspace.

For a disposable demo:

```sh
python3 -m labradour run --demo --watch
```

Type `run` in the Agent pane for sample activity, `size` for terminal dimensions, or `colors` for color samples. Add `--record /path/to/demo-recording` to try saved checkpoints. Each demo has a new temporary workspace, so use a fresh recording directory for each demo launch.

## Choose and inspect capture rules

Preview the effective rules without launching an agent or creating a recording:

```sh
python3 -m labradour policy --workspace /path/to/project \
  --record /path/to/labradour-recording \
  --exclude 'private/**' --metadata-only 'assets/**' \
  --max-file-bytes 4MiB --max-capture-bytes 32MiB --storage-budget 256MiB
```

Use the same options with `run --record ...` to apply them. Repeat `--exclude` and `--metadata-only` for additional rules. Patterns match workspace-relative paths and their ancestors, case-sensitively; `*` can match across directory separators. Use `private` to exclude that entire subtree, or `*.key` for matching filenames anywhere below the root. Quote patterns so your shell does not expand them.

For reusable rules, save a JSON file and pass `--capture-policy /path/to/capture.json`:

```json
{
  "schema_version": 1,
  "exclude": ["private", "*.key"],
  "metadata_only": ["assets/**"],
  "max_file_bytes": 4194304,
  "max_capture_bytes": 33554432,
  "storage_budget_bytes": 268435456,
  "max_event_bytes": 1048576
}
```

JSON limits use positive integer byte counts. CLI limits also accept `KiB`, `MiB`, and `GiB`. CLI size options override file values; CLI patterns extend the file's pattern lists. Invalid fields or limits fail before launching. Policy options on `run` require `--record`.

Default exclusions remain active unless you explicitly supply `--no-default-exclusions` or set `"use_default_exclusions": false` in the JSON file. Git administration, recorder/staging data, and the active hook spool remain excluded. The effective policy is printed before recording, initially shown in Visualization, and saved as a per-session `capture-policy-SESSION_ID.json` in the recording directory. Press **Ctrl-] then `p`** to toggle its view during a session.

Files above the file/scan limit and metadata-only matches have omission reasons in checkpoint details. `partial capture` in the footer reports omission and read-error counts. A previously captured file omitted from a later snapshot is labeled `file.omitted`, not `file.delete`; plain Git diffs can still show its absence as a deletion, so check snapshot metadata when reviewing a partial checkpoint. Captures process directories/files in sorted traversal order to make scan-limit choices repeatable.

## Storage exhaustion

The hard budget applies to the sum of regular-file bytes inside the recording directory, including journal, Git history, policy files, and a reserved health record. The default is 512MiB; the minimum configurable budget is 64KiB. Filesystem allocation overhead, temporary staging, and the hook spool are separate from this budget. Staging can temporarily duplicate the journal/Git history plus a new checkpoint and needs extra space on the same filesystem. Successful and rejected writes clean it up; a process crash can leave private `.labradour-stage-*` directories for later recovery/cleanup work.

If a write cannot fit, Labradour displays **RECORDING STOPPED: storage budget; agent continues**. Earlier history remains available; later activity/content is not recorded. A fixed-size health record preserves the reason even when the journal has no room. Inspect it after exit:

```sh
python3 -m labradour recording-status /path/to/labradour-recording
```

Restart with a larger `--storage-budget` or choose a fresh recording directory. There is no automatic pruning in this slice. A budget smaller than the existing store is rejected without deleting saved history. The hook spool must be outside the recording directory so external hook writes cannot bypass the store's budget.

## The three windows

```text
+-------------------------+-------------------------+
|                         | Activity                |
| Agent                   | Events and checkpoints  |
| Native CLI              +-------------------------+
|                         | Visualization           |
|                         | Selected event details  |
+-------------------------+-------------------------+
```

The Agent takes half the screen. Activity and Visualization share the other half. The title bar names the focused pane and shows whether selection follows live activity. The footer shows help, recording status, and coverage notices.

The **Agent** pane accepts normal typing, slash commands, paste, approvals, and Ctrl-C. Input goes to the agent while this pane has focus, except Labradour's reserved controls below.

The **Activity** pane lists observed events. With recording enabled, it includes snapshot intent/completion, filesystem observations, and collector health events. Select an older event to inspect it without stopping the agent. Selecting a snapshot completion shows its saved commit IDs, changed paths, scan interval, and capture quality.

The **Visualization** pane currently shows an event detail card as formatted JSON. It follows the selected Activity event. Source diffs and other specialized graphical views are planned; they are not yet embedded in this pane.

## Focus and navigation

Press **Ctrl-]**, release it, then press the command key within two seconds. For example, Ctrl-] followed by plain `l` focuses Activity. Commands work from every pane.

| Keys | Action |
| --- | --- |
| Ctrl-] then `a` | Focus Agent |
| Ctrl-] then `l` | Focus Activity |
| Ctrl-] then `v` | Focus Visualization |
| Ctrl-] then Tab | Cycle through the three panes |
| Ctrl-] then `?` | Show prefix help in the footer |
| Ctrl-] then `p` | Toggle effective capture policy in Visualization |
| Ctrl-] twice | Send a literal Ctrl-] to the agent |
| `j` / Down in Activity | Select the next event; pause live follow |
| `k` / Up in Activity | Select the previous event; pause live follow |
| `f` in Activity | Select the newest event and resume live follow |
| `j` / Down in Visualization | Scroll details down |
| `k` / Up in Visualization | Scroll details up |

Typing `j`, `k`, or `f` in the Agent pane sends those characters to the agent. Focus Activity first to browse history. New events do not move a historical selection while live follow is paused.

## Layout and terminal history

| Keys | Action |
| --- | --- |
| Ctrl-] then `m` | Move Agent to the opposite side |
| Ctrl-] then `z` | Maximize the focused pane; repeat to restore |
| Ctrl-] then `+` / `-` | Increase/decrease Agent width |
| Ctrl-] then `]` / `[` | Increase/decrease Activity height |
| Ctrl-] then `b` | Scroll Agent terminal history back |
| Ctrl-] then `n` | Return Agent terminal to live output |

Resize your outer terminal normally; Labradour updates the Agent's PTY dimensions. Below 100 columns or 28 rows, the focused pane fills the screen. Focus another pane to see it. Layout changes last for the current session; saved preferences and arbitrary pane arrangements are later work.

## Exit and review saved history

**Ctrl-Q** quits Labradour from any pane. Alternatively, press Ctrl-] followed by plain `q`. Quitting stops the launched process group and takes a final checkpoint when recording is enabled. Ctrl-C in Agent interrupts the agent using its normal terminal behavior.

When the agent exits on its own, the review panes stay open until you quit. Saved recordings remain on disk after Labradour closes. Review them without launching an agent:

```sh
python3 -m labradour history /path/to/labradour-recording
python3 -m labradour history /path/to/labradour-recording --session SESSION_ID
python3 -m labradour diff /path/to/labradour-recording BEFORE_COMMIT AFTER_COMMIT
```

Copy a session ID from the first command, then full `commit` IDs from `snapshot.completed` records. Compare adjacent checkpoints to see intermediate edits, including edits later reverted. Comparing only the baseline and final checkpoint can produce an empty diff even when work occurred between them.

Recording captures observed states rather than every write. Short-lived files and rapid changes between scans may be missed. `partial` captures, overflow events, and failed checkpoints identify gaps; an interrupted session is preserved and a later launch reconciles the current workspace into a new session.

## Visualizations planned for the review MVP

Phase 4 will turn Activity into an action/effect list with filters and historical selection, and route selected effects to built-in views:

| Selected effect | Planned view |
| --- | --- |
| File creation | Captured new content |
| File modification | Historical source diff |
| File deletion | Last captured content and deletion details |
| Shell command | Command, lifecycle, and available output |
| Read/search or unknown tool | Tool details and evidence card |
| Binary change or recording gap | Metadata and available evidence |

Multi-effect tool calls will offer a file/effect picker inside the review panes. Phase 5 adds optional gitdiffviz graphical exports through an explicit open action. Reviewing an event will use recorded evidence and will not rerun its command. Exact controls for these features will be added here when implemented.
