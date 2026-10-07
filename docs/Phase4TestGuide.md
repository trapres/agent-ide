# Phase 4 manual testing guide

Phase 4 implements the terminal review MVP: configurable three-pane layouts,
action/effect Activity, historical built-in views and saved-session review.
Automated PTY tests establish deterministic behavior; human native terminal
acceptance is still required. Record results in the table below rather than
treating the synthetic fixture or earlier provider checks as visual sign-off.

## 1. Start with a disposable recording

From the repository, use an interactive terminal with at least 140 columns and
40 rows. Set two separate disposable paths, outside your real project:

```sh
mkdir -p /tmp/labradour-mvp-work
printf 'alpha\n' > /tmp/labradour-mvp-work/example.py
python3 -m labradour run --workspace /tmp/labradour-mvp-work \
  --record /tmp/labradour-mvp-recording -- python3 -u -c \
  'from pathlib import Path; print("READY", flush=True)
while True:
 cmd = input("create/edit/revert/delete/quit> ")
 if cmd == "quit": break
 if cmd == "create": Path("created.py").write_text("new file\n")
 if cmd == "edit": Path("example.py").write_text("beta\n")
 if cmd == "revert": Path("example.py").write_text("alpha\n")
 if cmd == "delete": Path("created.py").unlink()
 print("DONE", flush=True)'
```

Use fresh paths if these already contain earlier work. The effective capture
policy appears before launch and initially in Visualization. Agent should show
READY and the prompt; Activity starts receiving durable events. This generic
fixture records filesystem effects, without provider tool/actor callbacks.

Enter `create`, `edit`, `revert`, then `delete`, waiting for the corresponding
Activity effect after each command. Waiting matters: the recorder captures
observed states rather than every write syscall. Edit/revert performed faster
than a checkpoint can legitimately disappear from captured content.

## 2. Browse captured effects

Press Ctrl-] then `l` to focus Activity. Press `/`, type
`path:example.py tool:file.modify`, and Enter; press `f` and `d` to open the latest
matching comparison. Use `k`/`j` in Activity to inspect the earlier edit and
revert. Expect alpha → beta and beta → alpha, although baseline-to-final content
is unchanged. File effects retain external-or-unknown attribution.

Filter `path:created.py tool:file.create` and then `path:created.py tool:file.delete`.
Expect captured new content and last captured deleted content respectively.
The initial example.py baseline says First captured file, because a creation
boundary before recording is unavailable. A raw filesystem observation is an
evidence card, not necessarily a content comparison: select the checkpoint
effect when checking captured bytes.

In Visualization, `e` shows recorded JSON evidence, `s` restores the built-in
view, Up/Down scroll and Left/Right move horizontally. With real tool callbacks,
Enter/Right in Activity expands candidate effects; `]`/`[` in Visualization
cycles effects while retaining the parent's selection. Candidate intervals never
prove which actor exclusively caused a file change.

Pause on an older row; generate another edit in Agent. Selection must stay on
the older identity and unread counts increase. An unmatched filter hides the row
but retains its detail selection with an outside-filter notice. `/`, Ctrl-U,
Enter clears the filter. Ctrl-] then `r` toggles raw journal rows.

## 3. Exercise layouts while native input continues

Ctrl-] then `:` opens the editor. Try `use agent-top`, `preview`, `cancel`,
then `use agent-top`, `apply`. The Agent moves above the review panes. Preview
and cancel must not change live geometry or answer an agent prompt. Try `status`,
`use default`, `apply`; Ctrl-] then `i` shows configuration sources and geometry.
See [UIGuide.md](../UIGuide.md#layout-and-terminal-history) for the other commands.

Resize below 100×28: only the focused pane is shown and the header explains the
size limit. Cycle panes with Ctrl-] then Tab, maximize/unmaximize with Ctrl-]
then `z`, then grow again. The requested tree, Activity filter/selection and
prepared historical view must survive. Hidden Agent output should continue and
be visible when you return; an ordinary native application redraw is allowed.

Persistence is explicit. To test without changing your project/user defaults,
start with a disposable `XDG_CONFIG_HOME` and the disposable workspace, then use
`save mvp-check workspace` in the editor. Reopen with `--layout mvp-check` and
verify the tree. Ordinary geometry changes must not silently save themselves.

## 4. Reopen saved sessions without an agent

Quit the fixture, then Ctrl-Q. Run it again with the same workspace/recording to
make a second session; quit normally. List and reopen:

```sh
python3 -m labradour history /tmp/labradour-mvp-recording
python3 -m labradour review /tmp/labradour-mvp-recording --ignore-layout-config
```

Expect SAVED REVIEW and Activity focus. Agent says no agent launched and no
terminal output recorded. Ctrl-] then `a`, `j`/`k`, Enter selects a session;
`>` marks the highlighted row and `*` the loaded one. The footer reports loading
and the loaded session/status. Reapply filters after changing sessions. `r` in
Agent refreshes the highlighted session; refreshing the same session preserves
filter, selected identity and view position. Activity and visualizer controls
remain available. Ctrl-Q exits cleanly.

After closing review, move the disposable original workspace aside and reopen
review from this repository. Historical content must still work. The default
layout workspace is your current directory; `review --workspace PATH` selects
an existing directory for layout preferences, not a source of historical files.
Do not pass a deleted original workspace as that layout option.

Review must not rewrite journal, health, Git refs/objects or policy files. It
does not recover a persisted running session; unclosed calls show incomplete
outcomes. There is no agent sign-in, watcher, hook installation or PTY process
in saved review. Explicit layout saves can write preference files.

## 5. Check retention and missing evidence

Only use the disposable recording. Keep review open on its older session, and
in a second terminal run:

```sh
python3 -m labradour prune /tmp/labradour-mvp-recording --keep-sessions 1
python3 -m labradour prune /tmp/labradour-mvp-recording --keep-sessions 1 --apply
```

Preview performs no removal. Apply rejects an active recorder. If an evidence
job currently holds a read lease, apply fails promptly with an evidence-busy
retry message; short reads mean you may not observe this race manually. The
automated tests force it deterministically. Retrying after preparation succeeds;
an idle open viewer does not pin the old session forever.

After pruning, cached analysis may remain visible. Request another uncached
comparison or refresh the removed session: expect unavailable/pruned evidence,
not empty content, a fabricated deletion, or bytes from today's workspace.
Open the retained session to resume normal review. The newest session's evidence
must still work. Packed Git objects are preserved; pruning does not promise
maximum disk compaction.

## 6. Missing/limited data and performance

Record a `.env` file, binary file, empty file, symlink and a path configured with
`--metadata-only`. Excluded content must not appear. Binary/non-UTF-8 and omitted
content use metadata/evidence fallback; an empty captured file is explicitly
empty, not missing. Symlinks show captured target bytes without following the
destination. Large views report truncation. Bounds: 256 KiB per historical blob,
4000 display lines, source diffs 64 KiB/2000 lines per side. Git reads have
two-second individual and five-second comparison deadlines; Python preparation
runs on a background worker without a hard process timeout.

Reproduce the disposable benchmark:

```sh
python3 tools/performance_probe.py --files 1000 --iterations 20
python3 -m unittest discover -s tests -q
```

The benchmark compares native input with recording off, recording plus background
writes, and recording plus review controls/preparation. Its saved-review fixture
contains 1,325 records and 2,302 rows; it times reopening and historical source
preparation. Reports: [macOS](mvp-performance-macos.json),
[Linux](mvp-performance-linux.json). These are local synthetic measurements,
not guarantees for huge sessions, slow disks or actual provider dialogs.

## 7. Human native MVP sign-off

Repeat sections 2–4 using normally authenticated/trusted native launches:

```sh
python3 -m labradour run --workspace /tmp/labradour-mvp-work \
  --record /tmp/labradour-claude-mvp --hooks claude -- claude
python3 -m labradour run --workspace /tmp/labradour-mvp-work \
  --record /tmp/labradour-codex-mvp --hooks codex -- codex
```

Ask for a harmless read, edit/revert, create/delete, successful shell command,
and failing command such as `exit 7`. Use normal trust/approval choices. Inspect
tool lifecycle, captured responses/errors and candidate effects. A missing or
opaque outcome stays unknown. Interrupt a harmless long-running command; inspect
its incomplete/interrupted evidence. Paste multiline text, scroll native history,
inspect an approval dialog, and switch/resize layouts with Agent hidden and
visible. No layout/review action may accept an approval or inject agent text.
Quit cleanly and verify saved review. Follow the existing
[Phase 2 runbook](Phase2TestGuide.md#9-manual-sign-off-runbook) for provider policy
and deployment-specific coverage.

| Acceptance row | macOS Claude | macOS Codex | Linux Claude | Linux Codex |
| --- | --- | --- | --- | --- |
| Human rendering, paste, approval/input and terminal history | Pending | Pending | Pending | Pending |
| Layout preview/apply, compact/maximize, hidden output, native redraw | Pending | Pending | Pending | Pending |
| Activity filters/selection, tool outcomes, candidate effects and built-ins | Pending | Pending | Pending | Pending |
| Clean exit and saved-session comparison agree with journal/actions | Pending | Pending | Pending | Pending |

For each row, record provider version, OS/terminal, date, recording/session IDs,
pass/fail and any explicitly accepted limitation. Prior automated provider runs
and the user's Linux Claude handoff remain evidence for their exercised scope;
they do not fill these new human checks automatically. Final MVP sign-off stays
open until applicable rows pass or their limitations are explicitly accepted.

## Not implemented

External visualization SDK/JSON-RPC or dynamic plugin loading, graphical/export
companions including gitdiffviz, terminal-stream replay, workspace restore,
attach/detach supervisor, multi-workspace browsing, remote/provider protocol
backends and complete write-syscall history remain later work. Internal read
leases do not create external plugin lease tokens or prevent unrelated tools
from deleting recording files.
