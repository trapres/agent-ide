# Phase 1 manual testing guide

This guide tests the implemented generic recorder: native terminal input, filesystem checkpoints, capture policy, storage limits, crash recovery, retention, and responsiveness. It describes the current implementation as of October 2, 2026. The automated acceptance suite passes 57 tests on macOS and Linux; these checks add human terminal and workflow coverage.

For all controls, see [UIGuide.md](../UIGuide.md). For implementation limits, see [Phase1.md](Phase1.md); for measured performance and Linux reproduction, see [RecorderAcceptance.md](RecorderAcceptance.md).

## What you should see now

At 100 columns by 28 rows or larger, Agent occupies the left half, with Activity above Visualization on the right. Smaller terminals display the focused pane full screen. The title identifies focus and live/historical selection. The footer displays recording health and, when space permits, scan/cache/queue metrics.

With recording enabled, the effective policy is printed before the UI opens and initially appears in Visualization. Selecting an Activity event replaces it with formatted JSON details. A completed snapshot includes commit IDs, changed paths, scan timing, capture quality, and metrics. **Visualization currently means detail cards; embedded source diffs and graphical views are still planned.**

Filesystem observations are attributed to external/unknown. Launching a shell provides filesystem evidence, not a structured log of shell commands or their output. Provider hooks can add raw events, but normalized tool attribution and reliable provider lifecycle coverage are Phase 2 work.

## 1. Prepare two terminals and a disposable workspace

Use a normal interactive terminal. Run launcher and review commands from the Labradour repository root, with Python 3.9+, Git, and the dependencies in `requirements.txt` installed:

```sh
python3 -m labradour doctor
```

Git and the Python packages should be available. Codex/Claude are needed only for the optional native-provider check.

In Terminal A, create a fixture with a staged version and a different working-tree version. This lets you check that recording leaves the project's index and refs alone:

```sh
export LABRADOUR_TEST_ROOT=$(mktemp -d /tmp/labradour-phase1-manual.XXXXXX)
mkdir "$LABRADOUR_TEST_ROOT/workspace"
git -C "$LABRADOUR_TEST_ROOT/workspace" init
printf 'value = 1\n' > "$LABRADOUR_TEST_ROOT/workspace/source.txt"
git -C "$LABRADOUR_TEST_ROOT/workspace" add source.txt
printf 'value = 2\n' > "$LABRADOUR_TEST_ROOT/workspace/source.txt"
printf 'dummy test value only\n' > "$LABRADOUR_TEST_ROOT/workspace/.env"
git hash-object "$LABRADOUR_TEST_ROOT/workspace/.git/index" > "$LABRADOUR_TEST_ROOT/index-before.txt"
git -C "$LABRADOUR_TEST_ROOT/workspace" for-each-ref > "$LABRADOUR_TEST_ROOT/refs-before.txt"
printf 'Test root: %s\n' "$LABRADOUR_TEST_ROOT"
```

In Terminal B, set `LABRADOUR_TEST_ROOT` to the exact printed path, and stay in the Labradour repository root:

```sh
export LABRADOUR_TEST_ROOT='/tmp/labradour-phase1-manual.PASTE_YOUR_SUFFIX'
```

Recordings live beside the fixture workspace. Preserve the test root until you finish reviewing evidence. All file edits and session removals below target these disposable fixtures.

## 2. Check navigation and terminal behavior

For a quick independent UI check, run in Terminal A:

```sh
python3 -m labradour run --demo --watch
```

At `fake>`, try `colors`, `size`, and `run`. Expect color samples, Unicode text, reported PTY dimensions, and synthetic tool/filesystem activity. The demo's rapid edits can be coalesced by filesystem capture; use the slower workflow in section 3 to test intermediate history.

Press Ctrl-], release it, then press the next key within two seconds:

| Check | Expected result |
| --- | --- |
| `a`, `l`, `v` | Focus Agent, Activity, Visualization; title changes |
| `j` / `k` in Activity | Select an event and pause live follow |
| `f` in Activity | Return to newest event and live follow |
| `j` / `k` in Visualization | Scroll long details |
| Prefix then `m` | Mirror the Agent side |
| Prefix then `z`, then repeat | Maximize focused pane, then restore |
| Prefix then `+` / `-`, `]` / `[` | Adjust Agent width and Activity height |
| Resize the outer terminal, then type `size` in Agent | PTY dimensions reflect the usable Agent pane |
| Shrink below 100×28 and switch focus | Focused pane occupies the screen |
| Ctrl-C in Agent | Demo reports interruption and remains usable |
| Type `quit` | Agent exits; review panes remain open |
| Ctrl-Q | Labradour closes and restores the outer terminal |

Prefix commands work from every pane. Plain letters in Agent go to the child. Historical selection should stay put while new events arrive. Layout changes are only for the current session. If you add `--record` to a demo, use a fresh store each launch: each demo creates a new temporary workspace.

## 3. Record create, edit, revert, and delete

Launch a shell in Terminal A:

```sh
python3 -m labradour run --workspace "$LABRADOUR_TEST_ROOT/workspace" \
  --record "$LABRADOUR_TEST_ROOT/recording" -- /bin/sh
```

`--record` automatically enables watching. Expect baseline/startup snapshot records before the child becomes usable. The captured baseline contains working-tree `value = 2`, even though the project index contains `value = 1`. `.env` and `.git` are excluded.

In the **Agent pane**, enter each command separately. After each one, wait for a corresponding `snapshot.completed` with the changed path before continuing. A three-second pause usually suffices for this small fixture; inspect details if it takes longer.

```sh
printf 'first content\n' > created.txt
printf 'value = 3\n' > source.txt
printf 'value = 2\n' > source.txt
rm created.txt
```

Expect captures of creation, modification, reversion, and deletion. Activity can contain multiple filesystem observations for one operation; it is not yet a grouped action/effect list. Focus Activity and select completed snapshots to inspect commit IDs and changes. Press Ctrl-] then `p` to revisit the policy, and select another event to return to details.

Now type `exit` in Agent. Review should remain open. Press Ctrl-Q to close. Normal shutdown takes a final checkpoint; unchanged states reuse an existing commit.

In Terminal B:

```sh
python3 -m labradour history "$LABRADOUR_TEST_ROOT/recording"
```

Copy the session ID into this variable, then inspect its journal:

```sh
LABRADOUR_TEST_SESSION='PASTE_SESSION_ID'
python3 -m labradour history "$LABRADOUR_TEST_ROOT/recording" \
  --session "$LABRADOUR_TEST_SESSION"
```

Expect a completed session with lifecycle and snapshot records. Copy full 40-character `commit` IDs from completed snapshots:

```sh
LABRADOUR_TEST_BEFORE='PASTE_BASELINE_COMMIT'
LABRADOUR_TEST_EDIT='PASTE_VALUE_3_COMMIT'
LABRADOUR_TEST_FINAL='PASTE_FINAL_COMMIT'
python3 -m labradour diff "$LABRADOUR_TEST_ROOT/recording" \
  "$LABRADOUR_TEST_BEFORE" "$LABRADOUR_TEST_EDIT"
python3 -m labradour diff "$LABRADOUR_TEST_ROOT/recording" \
  "$LABRADOUR_TEST_BEFORE" "$LABRADOUR_TEST_FINAL"
```

The first diff should show `value = 2` changing to `value = 3`, plus `created.txt` if still present at that checkpoint. The baseline-to-final diff should be empty: the intermediate work is still in history despite no net change.

Verify project Git isolation:

```sh
git hash-object "$LABRADOUR_TEST_ROOT/workspace/.git/index" > "$LABRADOUR_TEST_ROOT/index-after.txt"
git -C "$LABRADOUR_TEST_ROOT/workspace" for-each-ref > "$LABRADOUR_TEST_ROOT/refs-after.txt"
cmp "$LABRADOUR_TEST_ROOT/index-before.txt" "$LABRADOUR_TEST_ROOT/index-after.txt"
cmp "$LABRADOUR_TEST_ROOT/refs-before.txt" "$LABRADOUR_TEST_ROOT/refs-after.txt"
git -C "$LABRADOUR_TEST_ROOT/workspace" show :source.txt
cat "$LABRADOUR_TEST_ROOT/workspace/source.txt"
```

Both comparisons should exit successfully without output. Index content remains `value = 1`; working content remains `value = 2`. Recorder history is in the private store, not project refs.

## 4. Check capture rules and partial snapshots

After closing the previous run, prepare more fixture files in Terminal A:

```sh
mkdir -p "$LABRADOUR_TEST_ROOT/workspace/private" "$LABRADOUR_TEST_ROOT/workspace/assets"
printf 'excluded dummy content\n' > "$LABRADOUR_TEST_ROOT/workspace/private/hidden.txt"
printf 'metadata-only dummy content\n' > "$LABRADOUR_TEST_ROOT/workspace/assets/sample.bin"
python3 -c 'import os; from pathlib import Path; Path(os.environ["LABRADOUR_TEST_ROOT"], "workspace", "large.bin").write_bytes(b"x" * 2048)'
python3 -m labradour policy --workspace "$LABRADOUR_TEST_ROOT/workspace" \
  --record "$LABRADOUR_TEST_ROOT/policy-recording" \
  --exclude private --metadata-only 'assets/**' --max-file-bytes 1KiB
```

Preview should show defaults plus these rules, without creating `policy-recording`. Apply the same options:

```sh
python3 -m labradour run --workspace "$LABRADOUR_TEST_ROOT/workspace" \
  --record "$LABRADOUR_TEST_ROOT/policy-recording" \
  --exclude private --metadata-only 'assets/**' --max-file-bytes 1KiB -- /bin/sh
```

Expect a partial checkpoint: `assets/sample.bin` is metadata-only and `large.bin` exceeds the file limit. Their omission reasons should appear in details; their contents are absent from the captured Git tree. `private/hidden.txt` and `.env` are excluded. The saved `capture-policy-SESSION_ID.json` should reflect the effective rules.

For a read-only tree check after closing the run, copy a completed commit ID:

```sh
LABRADOUR_TEST_POLICY_COMMIT='PASTE_POLICY_CHECKPOINT_COMMIT'
git --git-dir="$LABRADOUR_TEST_ROOT/policy-recording/history.git" \
  ls-tree -r --name-only "$LABRADOUR_TEST_POLICY_COMMIT"
```

Expect `source.txt`, with none of the excluded/omitted paths above. Exclusions do not perform general secret detection. A formerly captured file that later exceeds a limit is reported as `file.omitted`; a plain Git diff can show absent content as deletion, so always inspect capture quality.

Additional checks: preview `--max-capture-bytes 1KiB` to inspect the scan-wide rule; launch with it to exercise scan-wide omissions. `--storage-budget 1KiB` should be rejected, since the minimum is 64KiB. Invalid JSON fields in `--capture-policy` should fail before a child launches. JSON sizes are integer bytes; CLI sizes accept KiB/MiB/GiB. These are expected validation failures.

## 5. Exhaust a small storage budget

Use a separate fixture in Terminal A:

```sh
mkdir "$LABRADOUR_TEST_ROOT/budget-workspace"
python3 -m labradour run --workspace "$LABRADOUR_TEST_ROOT/budget-workspace" \
  --record "$LABRADOUR_TEST_ROOT/budget-recording" --storage-budget 256KiB -- /bin/sh
```

In Agent, create incompressible content, then check that input still works:

```sh
python3 -c 'import os; from pathlib import Path; Path("random.bin").write_bytes(os.urandom(1024 * 1024))'
echo agent-still-usable
```

Expect **RECORDING STOPPED: storage budget; agent continues** once capture exceeds the budget. The echo should still work. Earlier saved evidence remains readable; later activity/content is not durable. In Terminal B:

```sh
python3 -m labradour recording-status "$LABRADOUR_TEST_ROOT/budget-recording"
python3 -m labradour history "$LABRADOUR_TEST_ROOT/budget-recording"
```

Expect persistent `storage-limit` health. Close Labradour and relaunch the same fixture with `--storage-budget 4MiB`. Expect a new session whose baseline captures the current file. Increasing the budget does not reconstruct changes missed during the stopped interval.

The budget counts regular-file bytes inside the store, not filesystem allocation overhead or sibling staging/hook spool space. Do not fill your actual disk to test I/O failure. Disk-full injection and interrupted storage operations are covered by the automated suite; physical disk failure and power-loss recovery remain unverified.

## 6. Check controlled process-crash recovery (optional)

Use the section 3 workspace/store with a fresh `/bin/sh` run. In Agent, make a small edit, wait for completion, and print the shell and launcher PIDs:

```sh
printf 'value = 4\n' > source.txt
printf 'Agent PID=%s; launcher PID=' "$$"
ps -o ppid= -p "$$"
```

In Terminal B, set the printed launcher PID and verify that its command is the Labradour run for this disposable fixture:

```sh
LABRADOUR_TEST_LAUNCHER_PID='PASTE_LAUNCHER_PID'
ps -p "$LABRADOUR_TEST_LAUNCHER_PID" -o pid=,args=
kill -KILL "$LABRADOUR_TEST_LAUNCHER_PID"
```

SIGKILL bypasses normal cleanup. If Terminal A is left with broken terminal settings, run `stty sane` and `reset`. Check the printed Agent PID with `ps`; if that fixture shell survived, terminate that specific shell before restarting. Avoid background jobs in this crash fixture.

Relaunch with the same workspace/store. Expect the previous running session to become `interrupted`, retained completed checkpoints to remain readable, and a separate new session baseline to contain current `value = 4`. If the kill happened during an unfinished snapshot, recovery includes its retained-ref evidence and whether the checkpoint was installed. Killing an idle launcher need not produce an interrupted snapshot record.

The test demonstrates process-crash recovery. It does not test filesystem corruption, power loss, or restoration of lost intermediate states. Recovery records history; it does not undo workspace edits.

## 7. Check whole-session retention and writer protection

Use the main fixture/store. Run and close enough shell sessions to have at least three entries in `history`; every launch creates a new session, even if contents are unchanged. Recover any crashed session first, then close all launchers.

In Terminal B:

```sh
python3 -m labradour history "$LABRADOUR_TEST_ROOT/recording"
python3 -m labradour prune "$LABRADOUR_TEST_ROOT/recording" --keep-sessions 2
```

Preview should identify older sessions for removal and the newest two for retention, without changing history. Running-status sessions are additionally protected. To delete the previewed old **test recordings**:

```sh
python3 -m labradour prune "$LABRADOUR_TEST_ROOT/recording" --keep-sessions 2 --apply
python3 -m labradour history "$LABRADOUR_TEST_ROOT/recording"
```

Expect only retained sessions in history. Their checkpoints/diffs should remain readable; removed session policy files and refs are removed. Workspace files and project Git state stay unchanged. Actual reclamation depends on shared objects and journal overhead; packed objects are preserved, so do not require a particular byte reduction.

For writer protection, start a shell recording into that store in Terminal A. In Terminal B, try a second recording launch into the same store and an applied prune. Both should refuse access while the first writer remains active. A prune preview can still report its plan. Reusing a store for a different workspace should also fail rather than mix histories.

Resuming interrupted pruning, cleanup of owned abandoned staging, and object-install interruption boundaries are covered deterministically in the automated suite. There is no automatic or age-based retention.

## 8. Check responsiveness and real agents

While recording the small fixture, type/paste commands, scroll details, change focus, and resize. Expect usable native terminal input and review navigation while captures happen in the background. Baseline and forced full reads can take longer than ordinary edits. Scan/cache metrics should be visible when the footer has room; unchanged periodic reconciliation should not continually add new checkpoint cards.

For repeatable measurements, run from the repository root outside Labradour:

```sh
python3 tools/performance_probe.py --files 1000 --iterations 20 \
  --output "$LABRADOUR_TEST_ROOT/performance.json"
```

The probe creates its own disposable fixtures and PTYs. Local acceptance measurements had snapshot visibility below 500 ms p95 and UI overhead below 50 ms; they are reference results, not guarantees on every machine/workspace. Record your environment and results if behavior is substantially slower. See [RecorderAcceptance.md](RecorderAcceptance.md) for methodology and scale limits.

For native-provider acceptance, launch one provider at a time in a disposable workspace:

```sh
python3 -m labradour run --workspace "$LABRADOUR_TEST_ROOT/workspace" \
  --record "$LABRADOUR_TEST_ROOT/codex-recording" --hooks codex -- codex
python3 -m labradour run --workspace "$LABRADOUR_TEST_ROOT/workspace" \
  --record "$LABRADOUR_TEST_ROOT/claude-recording" --hooks claude -- claude
```

Check prompts, approval dialogs, multiline paste, slash commands, Ctrl-C, resizing, and a small requested file edit. Follow each provider's native trust/approval workflow. Expect filesystem evidence and raw hooks where delivered. Record missing/duplicate hooks for Phase 2 verification; do not infer complete tool coverage or actor attribution from a successful launch. Provider-made Git operations can change project state independently of the recorder.

## What is not implemented yet

| Feature | Current behavior / planned work |
| --- | --- |
| Normalized tool/turn/subagent lifecycle and reliable attribution | Raw hooks and external/unknown filesystem facts; Phase 2 adapters |
| Synchronous captures at provider tool boundaries; authenticated collector | Observation-based captures; later adapter work |
| Every transient write or an atomic workspace snapshot | Live scans can miss rapid intermediate states; overflow reconciles current state |
| Saved-session navigation inside curses | Use `history` and `diff` after exit |
| Embedded source diffs, create/delete content views, command/output views | JSON cards now; Phase 4 review MVP |
| Action/effect grouping, filtering, multi-file effect picker | Raw activity now; Phase 4 |
| Durable Agent terminal transcript | Terminal scrollback is live UI state, not a saved transcript |
| Arbitrary layouts and saved layout preferences | Current pane controls are session-only; Phase 3 |
| gitdiffviz graphical open/export | Phase 5 |
| Workspace undo/restore, structured backends, multi-session orchestration | Outside this recorder slice |
| Automatic/age-based retention and packed-object compaction | Explicit whole-session prune; packed objects retained |
| Power-loss/corruption guarantees | Process-crash recovery and injected I/O tests only |

## Finish and report

Core manual acceptance is: navigation/input work; slow create/edit/revert/delete checkpoints are reviewable; project index/refs remain intact; capture exclusions/omissions are visible; quota failure leaves the child usable; relaunch recovers interrupted history; retention preserves surviving evidence; active writers are protected. Record optional crash and provider checks as tested or not tested.

For a useful issue report, include OS, Python/Git/provider versions, terminal size/type, exact launcher command and policy, steps, expected/actual behavior, session ID, relevant event sequence/commit IDs, and `recording-status` output. Note whether the watcher was native or polling and whether the footer reported partial capture, overflow, or stopped recording. Recordings contain included file contents and possibly raw hook payloads; use these dummy fixtures for shareable reproductions.

You can also run `python3 -m unittest discover -s tests -v` to compare with the documented 57-test automated acceptance baseline. Keep the temporary test root while investigating; remove it when its saved evidence is no longer needed.
