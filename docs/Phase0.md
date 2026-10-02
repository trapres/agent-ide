# Phase 0 feasibility results

Date: 2026-10-02. Status: **local implementation and automated feasibility checks pass; native-provider and Linux acceptance remain open**.

## Implemented probes

- Three-pane curses harness with a half-width native PTY, mirror/ratio controls, focus switching, maximize, and compact fallback.
- Raw terminal input forwarding with paste-aware IDE prefix handling, window-size propagation, process-group interruption/cleanup, and bounded output processing.
- Pyte-backed VT screen engine with separate primary/alternate screens, scrollback, fragmented UTF-8, wide/combining characters, SGR styles, DEC graphics, pane-local xterm queries, and unsupported-sequence diagnostics. The handwritten VT screen engine has been removed.
- Watchdog observation collector with a bounded queue, metadata-only events, recording-directory/Git exclusions, and overflow visibility. The optional `--watch` flag adds observations to the Activity pane without claiming tool attribution or content history.
- Fake agent in a temporary workspace and an observation-only hook sink using immutable, atomic per-event files. Tool lifecycle records merge by session/call identity in the simple Activity projection.
- Immutable-manifest scratch Git fixture using a private bare repository, Git plumbing, and a private index. Demonstrates creation, edit, deletion, and revert without changing project Git state.
- Reproducible native startup and gitdiffviz backend probes in [phase0_probe.py](../tools/phase0_probe.py).

## Automated verification

Run `python3 -m unittest discover -s tests -v`. **19 tests pass on macOS.** They cover mirror geometry, compact layout, pyte colors/cursor queries, fragmented Unicode, alternate screens through resize, history/line editing, xterm extension replies, fragmented paste, literal prefix/standalone Escape, concurrent hook processes, PTY sizing and Ctrl-C, and an end-to-end curses/fake-agent session with watching enabled.

The rendered-frame test checks the actual curses output, including DEC borders and the half-width Agent/stacked review geometry before and after mirroring. Cleanup is verified with a background writer that ignores TERM/HUP and outlives its process-group leader; closing the PTY wrapper stops it.

Native kqueue and polling watcher fixtures each observed create, modify, rename, and delete with no queue drops. Additional tests cover excluded paths, a move into scope, and overflow reporting.

Snapshot tests verify historical intermediate changes with zero final net diff, byte preservation for binary/CRLF content, tab/newline filenames, symlink/executable modes, identical-tree checkpoint reuse, path validation, and isolation from an inherited project index.

These are macOS automated checks; Linux and a human visual review remain unverified. Synthetic terminal assertions do not prove that the entire Codex/Claude native interface renders correctly. The quarter-screen view currently shows event payload cards; full operation visualizers and historical file selection belong to Phase 4.

## Local native CLI measurements

The measured startup report is [phase0-probe.json](phase0-probe.json). Probes submit no model prompt, do not bypass trust or tool permissions, and do not edit global/project hook configuration.

| Provider | Installed version | Measured startup | Provider-delivered hook events |
| --- | --- | --- | --- |
| Codex | 0.160.0 | Emits terminal output, then exits with `Operation not permitted`, even with probe-local SQLite/log directories | None; connectivity and actual tool coverage unverified |
| Claude Code | 2.1.234 | Welcome/error screen received; exits after `api.anthropic.com` DNS failure | None; connectivity and actual tool coverage unverified |

The direct hook-sink test verifies callback transport using synthetic payloads. It does not establish provider invocation, trust/configuration merging, successful/failed/denied call coverage, or subagent coverage. Keep these categories marked unverified in the coverage matrix until a native session delivers actual events.

The probe uses Codex's documented `sqlite_home` and `log_dir` launch overrides to keep those writes in temporary probe directories, but startup still fails with a permission error. No persistent configuration changes or database repairs were attempted. [Official Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)

Codex documents local tool/session hooks, launch configuration, and trust review; some tool paths are excluded from hook coverage. [Official Codex hooks](https://learn.chatgpt.com/docs/hooks). Claude documents tool/lifecycle hooks and their failure distinctions. [Claude hooks](https://code.claude.com/docs/en/hooks). These are documented capabilities, not measured outcomes on this machine.

Use the README's `--hooks` launch commands in a normal terminal to complete native connectivity testing. Validate actual read/edit/shell events, callback IDs, failures/denials, and normal user approval behavior, with existing hooks still present. Do not mark this gate complete on the basis of version detection alone.

## Terminal backend decision

The user's installed packages are now available to Python 3.9.6: pyte 0.8.2, watchdog 6.0.0, and wcwidth 0.9.1. These are recorded in the probe report, and requirements are declared in [requirements.txt](../requirements.txt).

Pyte now handles core VT editing, wrapping, history, and decoding. The adapter implements extensions needed by the pane model: alternate screen buffers, bracketed paste/application cursor flags, DEC graphics in UTF-8 mode, color/size/version queries, and protocol negotiation replies. Pyte supplies the core screen/parser interface described in its [API reference](https://pyte.readthedocs.io/en/latest/api.html).

Remaining limitations: no line reflow on resize; curses palette approximation for RGB; no mouse/focus event forwarding or enhanced keyboard mode; unsupported controls are diagnosed rather than relayed to the outer terminal. Authenticated native CLI fidelity remains an acceptance gate, rather than a missing dependency or handwritten-renderer issue.

## Watchdog backend decision

Watchdog's default macOS FSEvents emitter failed to initialize in the restricted environment. The feasibility collector therefore selects watchdog's native **KqueueObserver** on macOS; both kqueue and an explicitly requested **PollingObserver** pass the mutation fixture. Linux uses watchdog's platform-default observer and still needs a measured run. This follows watchdog's observer/event-handler model. [Watchdog quickstart](https://python-watchdog.readthedocs.io/en/stable/quickstart.html)

The collector watches included paths without following file symlinks out of the workspace, excludes recorder/Git/cache paths, and reports queue drops. It starts after the PTY fork to avoid forking with watchdog threads running. Startup writes and coalesced/transient operations may be missed, and only metadata is collected. Baseline reconciliation, immutable content capture, attribution, and durable recovery are Phase 1 work. Run `python3 -m labradour watch-fixture /tmp/new-empty-fixture` for a standalone probe.

## gitdiffviz backend compatibility

A local checkout was available at `/Users/jared.carlson/Projects/gitdiffviz`, source revision `1c5639469fdbadefca5b7dc4a93f260648d90ee1`, with an existing `_build/default/bin/main.exe`. The probe used that binary; it did not rebuild it or verify that its build exactly matches the checkout revision.

Measured outcomes:

- Extracting a diff from the private bare repository fails: Git requires a working tree.
- Cloning retained scratch history into an independent temporary export checkout (`--no-hardlinks`) allows revision extraction.
- Creation, deletion, and modification in the fixture produce structured diff JSON; scene generation succeeds.
- A baseline-to-reverted-final empty diff also extracts successfully.

This confirms a usable export bridge. It does not yet validate browser/Tauri rendering, semantic accuracy, rename/binary edge cases, timeline scaling, distribution packaging, or historical semantic content selection. The exporter stays optional. The project's analysis/renderer separation is documented in its [README](https://github.com/superstealthlogic/gitdiffviz); the bare-repo failure and checkout success above are local measurements.

## User verification update

The user subsequently verified that both Codex and Claude run through Labradour's workspace launcher. This resolves the basic native-launch blocker and supports beginning Phase 1. It does not establish the complete terminal checklist, measured hook delivery, or Linux acceptance. Phase 1 progress is recorded in [Phase1.md](Phase1.md).

## Remaining Phase 0 acceptance gates

1. Run authenticated Codex and Claude sessions through the harness outside the restrictive execution environment; visually verify input, multiline paste, colors, cursor, alternate screens, resize/mirror, approvals, and interruption.
2. Measure provider-delivered hook events and existing-hook composition/trust behavior, updating the coverage table by event category.
3. Repeat the core PTY/layout/watcher/snapshot checks on Linux. A local Docker executable is present, but its daemon is not running, so it could not provide a Linux test environment here.

The durable recorder, journal, retention/recovery, saved UI preferences, full visualizer registry, and arbitrary layouts are later phases. The dependency-related work and local feasibility implementation are complete; full Phase 0 acceptance still depends on the native-provider and Linux checks above.
