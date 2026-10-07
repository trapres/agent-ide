# Labradour visualization plugin API

Status: **proposed v1 contract, not an implemented plugin SDK or runtime**. Date: October 2, 2026.

This API lets a visualization tool present recorded Labradour evidence in the Visualization pane or produce a graphical companion. It separates presentation from provider hooks, recording, and pane layout. A plugin can implement a source diff, file structure view, image preview, command summary, or tool-specific explanation without accessing recorder internals.

Phase 4 has an internal built-in registry, effect-granted historical readers, background terminal views, JSON evidence cards and saved-session `review`, with short retention-coordinated read leases. [Phase 5's export foundation](Phase5.md) adds explicit background JSON evidence exports, provenance, private bounded artifacts, content-addressed caching and cleanup. These interfaces are not the complete SDK below: external scoped handles, JSON-RPC, plugin configuration, process isolation, plugin-facing artifact RPC and graphical formats/opening remain proposed implementation work. They refine [AgentUI.md, section 6](../AgentUI.md). Explicitly installed subprocess adapters follow; automatic third-party discovery is deferred.

## 1. Responsibilities and boundaries

```text
Recorder → journal + immutable checkpoints
                    ↓
          host action/effect projection
                    ↓ selected context
            visualizer registry
                    ↓
      plugin preparation ← read-only evidence service
                    ↓
       terminal view / explicit graphical export
```

Labradour owns selection, focus, layout, key routing, evidence quality labels, resource limits, artifact storage, and opening companion views. Plugins analyze evidence and return presentation data. They never write curses cells directly, receive Agent keystrokes, execute recorded commands, answer approvals, or mutate the journal/workspace through this API.

All review content comes from captured evidence. There is no fallback to current workspace files when historical content is unavailable. An omitted file is not an observed deletion; missing output is not empty output. Plugins must preserve these distinctions.

An installed executable still runs with its operating-system permissions. Read-only API access is an interface restriction, **not a subprocess sandbox**. Install plugins explicitly as trusted software. A future sandbox can narrow that trust without changing the evidence interface.

## 2. Registration and customization

Each plugin provides a manifest. Example:

```json
{
  "manifest_version": 1,
  "id": "example.source-map",
  "version": "0.1.0",
  "display_name": "Source map",
  "api_versions": [1],
  "launch": {
    "transport": "stdio-jsonrpc",
    "argv": ["/absolute/path/to/source-map", "--labradour-stdio"]
  },
  "operations": ["file.create", "file.modify", "file.delete"],
  "required_evidence": ["file_metadata"],
  "optional_evidence": ["before_content", "after_content"],
  "capabilities": ["terminal", "graphical_export"],
  "priority": 20,
  "options_schema": {
    "type": "object",
    "properties": {"show_symbols": {"type": "boolean"}},
    "additionalProperties": false
  }
}
```

IDs are unique, case-sensitive strings; versions identify cache compatibility. The host validates manifests before launch and rejects duplicate IDs, unsupported API versions, invalid capabilities, or invalid options. `argv` is an argument array, never shell text. The host launches with a private temporary working directory and a minimal environment, without provider credentials or recorder Git variables deliberately added. This is not protection against a trusted executable reading host files itself.

Proposed configuration, stored separately from recording facts:

```toml
[visualization.plugins."example.source-map"]
manifest = "/absolute/path/to/source-map/labradour-plugin.json"
enabled = true

[visualization.plugins."example.source-map".options]
show_symbols = true

[ui.visualizers]
"file.create" = "builtin.file-create"
"file.modify" = "example.source-map"
"file.delete" = "builtin.file-delete"
"shell.execute" = "builtin.command"

[visualization.limits]
prepare_timeout_ms = 5000
render_timeout_ms = 250
export_timeout_ms = 30000
max_artifact_bytes = 67108864
```

These names and built-in IDs are proposed, not currently recognized settings. Workspace configuration may customize routing/options, but enabling or replacing executable paths requires explicit user installation/trust. Unavailable configured plugins show a reason and fall back to a built-in card.

Resolution order:

1. User choice for the selected action/effect.
2. Configured operation mapping.
3. Applicable registered plugins, descending priority, then ascending ID.
4. Built-in generic evidence card, always available.

An explicit choice still has to be applicable. A selected child effect routes by its operation, independently of the parent tool. The host checks declared requirements first; `viz.supports` then handles details such as file type. Tool-specific preferences can be added later without changing contexts. Plugin choice/options and view state are UI preferences, not changes to recorded facts.

## 3. Selection context

The host converts recorded facts into a provider-neutral context. IDs are opaque; plugins must not parse them or assume a row number is identity. Example, with illustrative IDs:

```json
{
  "schema_version": 1,
  "selection_id": "effect-17",
  "evidence_revision": 3,
  "workspace_id": "workspace-1",
  "session_id": "session-1",
  "turn_id": null,
  "action_id": "action-9",
  "effect_id": "effect-17",
  "actor": {"id": null, "label": "external/unknown"},
  "operation": "file.modify",
  "lifecycle": "observed",
  "tool": null,
  "interval": {
    "before_checkpoint": "checkpoint-12",
    "after_checkpoint": "checkpoint-13",
    "basis": "adjacent-checkpoints"
  },
  "quality": {
    "capture": "observed-live-state",
    "attribution": "external-or-unknown",
    "gaps": []
  },
  "effects": [
    {
      "id": "effect-17",
      "operation": "file.modify",
      "path": {"display": "src/example.py", "bytes_base64": "c3JjL2V4YW1wbGUucHk="},
      "before": {"state": "available", "handle": "evidence-before", "size_bytes": 28},
      "after": {"state": "available", "handle": "evidence-after", "size_bytes": 29}
    }
  ],
  "raw_event": null,
  "outputs": [],
  "grants": ["evidence-before", "evidence-after"]
}
```

Contexts support file create/modify/delete/rename/omitted, shell execute, read/search, generic tool, actor lifecycle, recording health, and `event.raw`. Unknown operations route to the generic card unless a plugin explicitly supports them. `tool` is nullable; when available it carries provider/name and permitted arguments/result evidence handles. `raw_event` is a handle, not an unrestricted raw payload embedded in every selection.

`effect_id` is null for a parent action. `effects` may be empty, or paginated through an `effects_handle` for a large action. `effects_complete` and `outputs_complete` default to true; when false, the context supplies the corresponding collection handle and optional cursor. Plugins must not treat a page as the entire set. Raw-event mode uses journal event identity and nullable normalized fields. Phase 1 data must not manufacture tool IDs, causality, command output, or provider coverage.

`evidence_revision` increases when the selected action gains evidence or quality information. A changed comparison interval also creates a new revision. Every prepare/render/export request is tied to a particular selection and revision. Wall-clock timestamps, when supplied, are RFC 3339 strings; durations are integer milliseconds. Sequence numbers order ingestion, not universal causality.

Path identity uses base64-encoded original filename bytes; `display` is escaped, readable text. This supports non-UTF-8 filenames and names containing tabs/newlines. Plugins use handles for content access and never join display paths to a local filesystem root.

Evidence states are explicit:

| State | Meaning |
| --- | --- |
| `available` | Captured content can be read through its granted handle |
| `absent` | The path is known absent in the specified captured state |
| `pending` | Capture/result is not yet available |
| `excluded` | Capture policy excluded content |
| `metadata_only` | Metadata exists but content was intentionally omitted |
| `unavailable` | Content cannot be established/read, with a reason |
| `pruned` | Previously referenced evidence has been removed |

An available zero-byte blob is an empty file. `outputs: []` alone does not assert that a command emitted nothing; the output descriptor must establish capture/completeness. Truncation is a property of available evidence, with captured/known-total byte counts where known. Unreadable captures retaining older bytes label those bytes with their original checkpoint and a stale/read-error notice.

Capture quality, interval basis, attribution, and lifecycle are separate fields. A tool may complete before its snapshot is available. Overlapping calls can share a comparison interval; plugins must not label that interval as exclusive ownership or an exact tool-boundary capture. Only the host assigns quality labels from evidence. Mandatory labels remain visible in host chrome regardless of plugin output.

## 4. Logical interface and transport

Built-ins implement this logical interface in process:

```text
initialize(host_info) -> descriptor
supports(context_summary, options) -> applicability
prepare(context, options, cancellation) -> view
render(view_id, viewport, ui_state, cancellation) -> terminal_page
handle_input(view_id, input, ui_state, cancellation) -> interaction
export_graphical(view_id, format, cancellation) -> artifact_id
release(view_id)
shutdown()
```

Preparation and rendering run outside the curses/input thread. A prepared view is an immutable analysis of one evidence revision; interaction state is separate. Resize rerenders that view instead of restarting analysis. A plugin implementing only `terminal` need not implement export; input handling is optional. Graphical-only plugins retain a built-in terminal summary and an explicit companion action.

External plugins use bidirectional JSON-RPC 2.0 over stdin/stdout: one UTF-8 JSON object per newline, no batches. stdout contains protocol messages only; diagnostics go to bounded stderr. Host request IDs start with `h:`, plugin request IDs with `p:`. Both sides process responses while requests are outstanding, so an evidence callback during preparation cannot deadlock the connection. A single reader dispatches messages; only serialized writes touch each output stream.

Handshake: host calls `viz.initialize`, supplying supported API versions, locale, negotiated limits, and host capabilities; plugin returns its ID/version and selected API version. The response must agree with the registered manifest. Unsupported major versions fail cleanly. Unknown optional fields are ignored; unknown methods use JSON-RPC method-not-found. New required fields or incompatible meanings require a new API version.

| Direction | Method | Purpose |
| --- | --- | --- |
| Host → plugin | `viz.initialize` | Negotiate v1 and limits |
| Host → plugin | `viz.supports` | Return `applicable`, reason, and missing requirements |
| Host → plugin | `viz.prepare` | Return view ID, title, initial state, and supported actions |
| Host → plugin | `viz.render` | Return the requested terminal page |
| Host → plugin | `viz.input` | Handle an allowed Visualization input |
| Host → plugin | `viz.export` | Generate an artifact after explicit user action |
| Host → plugin | `viz.release` | Drop a view and associated resources |
| Host → plugin | `viz.shutdown` | Orderly process shutdown |
| Either direction | `$/cancelRequest` (notification) | Cancel an outstanding request ID |
| Plugin → host | `evidence.describe` | Describe a granted evidence handle |
| Plugin → host | `evidence.read` | Read a bounded byte range |
| Plugin → host | `evidence.list` | Page through granted directory/effect/event collections |
| Plugin → host | `evidence.diff` | Obtain a bounded historical comparison |
| Plugin → host | `artifact.create`, `artifact.write`, `artifact.finish` | Build a host-owned export artifact |
| Plugin → host | `viz.progress` (notification) | Bounded progress for an outstanding job |

Evidence and view-job requests reference a host-issued `scope_id`; initialization and process shutdown do not need one. Each scope binds plugin identity, selection/revision, granted handles, limits, and expiry. Handles cannot be guessed from Git OIDs or used across scopes. The host refuses arbitrary paths, SQL, Git revisions/ref expressions, and unrestricted session enumeration.

Example preparation message:

```json
{"jsonrpc":"2.0","id":"h:12","method":"viz.prepare","params":{"scope_id":"scope-8","context":{"schema_version":1,"selection_id":"effect-17","evidence_revision":3,"operation":"file.modify"},"options":{"show_symbols":true}}}
```

The abbreviated context above illustrates the envelope; real calls include the complete context from section 3. Every plugin result must echo `selection_id` and `evidence_revision`. Render results also echo host `render_generation`, incremented on resize, scroll, or interaction. View IDs are unique within a plugin process and become invalid on release/restart.

## 5. Read-only evidence service

`evidence.describe` accepts `{scope_id, handle}` and returns state, kind, provenance, original/captured sizes, mode, media/encoding hints, truncation, and quality. Optional Git object IDs are metadata only; a plugin cannot use them to bypass handle checks. Symlink evidence contains target bytes, never dereferenced destination content.

`evidence.read` accepts `{scope_id, handle, offset, length}`. Offsets and lengths are nonnegative byte integers; length is at most the negotiated chunk limit. It returns:

```json
{
  "handle": "evidence-after",
  "offset": 0,
  "bytes_base64": "dmFsdWUgPSAzCg==",
  "next_offset": 10,
  "eof": true,
  "captured_size_bytes": 10,
  "original_size_bytes": 10,
  "truncated": false
}
```

Base64 is authoritative; text decoding is a plugin choice and must handle binary/invalid UTF-8. Requests for unavailable content return its explicit state/reason rather than successful empty bytes. No method rereads the workspace. Raw arguments/results and output require separate grants and obey recording suppression/truncation.

`evidence.list` accepts a collection handle, opaque cursor, and bounded page size. It returns entries, `next_cursor`, and completeness. Checkpoint/path lookups are scoped to granted collections; related evidence requires a new host-issued grant rather than a plugin-supplied path outside that collection.

`evidence.diff` accepts granted before/after handles and options (`unified`, context lines, cursor, page size). It returns structured text hunks or binary/mode/symlink metadata, with completeness and evidence quality. A known absent side is valid for create/delete. Omitted/unavailable sides produce an incomplete-comparison state. It never runs external Git diff drivers, text conversion, or workspace filters.

The host must coordinate these reads with retention before implementing the external service. Proposed rule: an active job holds a short, bounded read lease for its evidence; applied prune refuses conflicting leases and reports them. Lease expiry cancels the job. A prepared view may retain its bounded analysis but cannot silently extend leases forever; later reads reacquire authorization or report `pruned`. No plugin ref, cache, or artifact implicitly makes a session permanent. Phase 4 internal readers now use shared directory locks across a captured before/after pair with a five-second aggregate Git deadline; cooperating prune/reclamation holds the exclusive lock. They release before derived analysis/display and do not write lease files. This is internal coordination, not an external lease-token API or hard plugin cancellation.

## 6. Terminal presentation and input

The host renders logical lines/spans into its own curses window. A page has a title, bounded rows, total-row information, and optional allowed actions:

```json
{
  "selection_id": "effect-17",
  "evidence_revision": 3,
  "render_generation": 7,
  "view_id": "view-4",
  "title": "Source map",
  "first_row": 0,
  "total_rows": 2,
  "complete": true,
  "rows": [
    {"id":"summary","spans":[{"text":"src/example.py","role":"heading"}]},
    {"id":"change","spans":[{"text":"+ value = 3","role":"added"}]}
  ],
  "actions": [{"id":"open-graphical","label":"Open graphical view","kind":"export","format":"text/html"}]
}
```

`viz.render` receives viewport content dimensions in terminal cells, row/column offsets, color capability, and opaque bounded `ui_state`. Return at most the requested row count, within message limits. `total_rows` may be null while unknown; `complete` and pagination describe availability. Row IDs support stable navigation. The host clips by Unicode cell width, handles horizontal/vertical scrolling, and escapes control characters. Paths, tool text, and plugin text never inject ANSI/OSC sequences.

Roles are semantic: `normal`, `heading`, `muted`, `added`, `removed`, `warning`, `error`, `link`. The host maps roles to its theme and monochrome presentation; raw terminal escape strings are forbidden. Essential meaning must also be in text, not color alone.

Only Visualization-focused, host-approved keys/actions reach `viz.input`, with normalized names/modifiers and bounded text. Global focus/quit controls and Agent input are reserved. The response can update `ui_state`, request a rerender, or request a host action from its declared list. Clicking/activating an action has the same contract as keyboard activation. Plugins cannot synthesize Agent input or open external resources themselves through this API.

Host actions initially cover selecting a related granted effect, switching applicable visualizer, and exporting/opening an artifact. The host validates every action target. It retains Activity selection unless the user explicitly navigates elsewhere. Pane state is keyed by selection, plugin version, and options; resize preserves it.

## 7. Graphical companions and artifacts

Graphical output is optional. Merely selecting an event must never open a browser/window. The user activates an action, the host calls `viz.export`, then offers/executes the explicit open action on the finished artifact.

`artifact.create` accepts `{scope_id, media_type, suggested_name}` and returns an opaque artifact ID. `artifact.write` uploads sequential bounded base64 chunks; `artifact.finish` validates total size/hash and publishes it. Names are display hints, not destination paths. Initially support single-file `text/html`, `image/png`, `image/svg+xml`, and `application/json`; multi-file bundles need a separate validated format. A plugin returns an artifact ID, never a shell command or arbitrary open URL.

Exports live in a separate host-owned cache with explicit size/retention policy; they do not bypass the recording budget by being written inside the private store. The host removes unfinished artifacts on cancellation/failure. Cache keys include plugin/version/options, evidence revision and immutable references, comparison interval, and output format. Cache entries are derived data, not additional evidence. An export may include captured private contents; sharing is a separate user action.

Current internal foundation: JSON only, one explicit background job, 64 MiB per artifact, 128 MiB total including pending files, 32 completed artifacts and seven-day lazy age cleanup. Ctrl-] x exports, c cancels and t shows status; CLI export/cache inspection/clear are available. Atomic private publication and cache digest checks are implemented. Cancellation/deadlines are cooperative in Python; no browser is opened. The artifact create/write/finish RPC and other formats above remain proposed, rather than callable plugin services.

Opening active HTML is a distinct trusted capability, declared as `active_html` in addition to `graphical_export`. The initial adapter must describe whether its companion uses scripts/network or starts a local server, and the host must expose that before the user enables it. A local HTML file alone is not a browser network sandbox. External URL launching and arbitrary plugin-supplied server commands are outside v1; a future server lifecycle capability needs its own authorization/cleanup contract.

For gitdiffviz or similar tools, the adapter can read permitted revision data and materialize a disposable input tree outside the workspace/store. Whole-tree analysis requires explicit grants to those captured path collections; selecting one effect does not automatically grant the entire session. Historical semantics must use captured bytes. It must not require mutation of project Git state or rerun the selected command. Licensing, pinned tool version, supported formats, and bare-repository compatibility remain adapter-specific checks; this API does not establish those tools' current capabilities.

## 8. Cancellation, failure, and limits

Selection/revision changes cancel obsolete preparation and revoke its scope. Resize/scroll supersedes earlier render generations. The host discards stale results even if cancellation was ignored. A released view cannot generate later UI updates. Graphical exports already explicitly requested use a separate bounded export scope and may finish independently of selection changes while that scope remains valid; they never replace the new selection's terminal view.

Proposed default upper bounds, negotiated downward if necessary:

| Resource | Default |
| --- | ---: |
| JSON message, including base64/framing | 1 MiB |
| Evidence/artifact byte chunk | 64 KiB |
| Collection page | 200 entries |
| Render page | Requested viewport rows, maximum 200 |
| UI state | 16 KiB |
| Aggregate evidence bytes read per job | 64 MiB |
| Artifact size | 64 MiB |
| Initialize/supports timeout | 1 second each |
| Prepare / render / export timeout | 5 seconds / 250 ms / 30 seconds |
| Cancellation grace before process termination | 500 ms |

Limits are independent of recorder capture limits. Large jobs can request a user-configured higher bound before starting; plugins cannot raise limits themselves. Oversized JSON is rejected before deserialization beyond the bound. Reads, queues, logs, temporary files, view counts, and caches also need host caps. Wall-time/protocol limits do not impose a hard OS memory limit on trusted subprocesses.

Use JSON-RPC standard errors for framing/method/argument failures. Domain errors use code `-32000` and structured `data.kind`: `missing_evidence`, `permission_denied`, `pruned`, `cancelled`, `timeout`, `resource_limit`, `plugin_failed`, or `unsupported`. Include a safe human message and request scope; never include captured bytes in diagnostic errors. Notifications have no response. Progress identifies an outstanding request and cannot extend its deadline.

Failures stay local to Visualization: retain host evidence labels, show the reason, and provide the generic card. A broken connection/process is terminated, unfinished artifacts removed, and outstanding requests failed. Repeated crashes disable the plugin for that session until explicit retry; restarting invalidates all old view IDs/scopes. Slow or failing plugins must not block Agent input, recorder queues, or shutdown.

## 9. Implementation slices and acceptance

1. **Internal contract:** add typed contexts/evidence states, built-in registry, resolver, read-only service, and host-rendered pages. Adapt existing JSON cards as the generic fallback. Project Phase 1 raw events remain usable without normalized provider fields.
2. **Review views:** add creation/diff/deletion/command cards, historical intervals, input/state handling, stale-result rejection, and retention/read coordination alongside the Phase 4 UI.
3. **External adapter:** implement manifest/config validation, stdio JSON-RPC, handshake, bounded subprocess lifecycle, and a deterministic sample plugin. Built-in and external implementations share conformance fixtures.
4. **Graphical companion:** add artifact lifecycle and explicit open actions, then an explicitly installed exporter such as gitdiffviz after compatibility checks. Arbitrary discovery and server-backed companions remain subsequent work.

Conformance must demonstrate:

- A recorded edit still renders correctly after the live file changes or is deleted.
- Empty, excluded, omitted, binary, symlink, truncated, pending, and pruned evidence remain distinguishable.
- A multi-effect/shared interval preserves identity and attribution labels.
- Rapid selection, revision changes, scrolling, and resize cannot display stale results.
- Plugin crashes, oversized messages, blocked reads, cancellation, and timeouts leave Agent/recording responsive.
- Malicious paths, terminal escapes, arbitrary revisions, cross-scope handles, and invalid action targets are rejected or escaped.
- Prune/read coordination preserves active reads without indefinite retention or blocking recording.
- Missing configured plugins and unsupported versions fall back clearly.
- Graphical export opens only on explicit activation, cleans failed artifacts, and works without changing project files/refs.
- A user can map an operation to a different applicable plugin without editing recorder/provider code.

This document defines the proposed integration boundary. Current manual checks remain in [Phase1TestGuide.md](Phase1TestGuide.md); the broader delivery milestones remain in [AgentIDEPlan.md](../AgentIDEPlan.md).

## Current correlation foundation

Phase 2 now provides `python3 -m labradour actions RECORDING --session SESSION_ID`, a read-only journal projection with stable opaque action/effect IDs, scoped actor identities, raw input/output/error references, immutable checkpoint references, candidate action lists, capture quality, and evidence gaps. See [Phase2.md](Phase2.md) for semantics. The output is an internal replay format; the scoped evidence handles, plugin transport, view revisions, and rendering API described above remain proposed Phase 4 work. A future context adapter must retain its ambiguous attribution and missing evidence rather than infer exclusive ownership.
