# Phase 1 recorder acceptance and performance

The generic recorder's automated acceptance passes on macOS arm64/Python 3.9.6 and Linux arm64/Python 3.11.17 in Docker. **57 tests pass on each platform.** Linux uses its native inotify watcher for the native watcher fixture; latency probes use the explicitly selected polling backend on both platforms.

## Changes justified by the measurements

- Files with unchanged device/inode, size, nanosecond mtime/ctime, and mode reuse previously captured immutable bytes. Dirty watcher paths invalidate that cache. Startup, overflow, shutdown, and every fifteenth capture attempt force fresh reads. Full metadata reconciliation continues every two seconds; unchanged periodic states do not append redundant checkpoint markers.
- Scanner previews do not associate new signatures with old captured content. Capture caches advance with their corresponding immutable manifests. A same-size overwrite with restored mtime is covered by an acceptance test.
- Git hashes new content in a batch, with raw bytes and filters disabled, and caches blob IDs for the current manifest. Checkpoint staging reads old objects through a launch-local alternate object directory. It installs only new objects and the session ref, eliminating the full history copy. No alternates file is retained in the saved repository.
- Watcher facts are appended in batches using one staged journal transaction, with individual IDs and sequences preserved. The worker checks for work every 20 ms.
- Storage accounting scans the private store on opening and tracks each owned replacement exactly under the single-writer lock. Maintenance refreshes accounting after deletions. Budget rejection, interrupted installations, and disk-full recovery continue to pass. External mutations of an active private store are outside the cooperating-writer contract.

## Measurements

The original and final code were measured with 500 files and five iterations of the same direct recorder workload. These small-sample p95 values are the maximum sample; they establish the bottleneck and observed improvement rather than a production latency guarantee.

| macOS, 500 files | Before | After |
| --- | ---: | ---: |
| Initial baseline | 6,854 ms | 801 ms |
| One-file checkpoint p95 | 7,970 ms | 162 ms |
| 64 journal facts p95 | 2,488 ms | 3.9 ms |

Raw evidence: [before](performance-before.json), [after](performance-after.json).

The representative probe uses 1,000 files in 20 directories, approximately 2 MiB of initial content, and 20 iterations. It measures direct checkpoints, sequential watcher captures, and visible acknowledgements through the actual curses/PTY harness. The recording-enabled UI test includes a background writer changing a file every 20 ms. Watcher-to-visibility timings end when the consumer receives durable events, including journal acknowledgement rather than just the event's receive timestamp.

| Metric | macOS | Linux |
| --- | ---: | ---: |
| Initial baseline | 1,373 ms | 795 ms |
| Unchanged scan p95 | 32.3 ms | 16.5 ms |
| One-file checkpoint p95 | 206.9 ms | 43.2 ms |
| 64-event journal batch p95 | 3.8 ms | 3.8 ms |
| Live snapshot visibility p95 | 316.7 ms | 121.3 ms |
| Filesystem row visibility p95 | 135.8 ms | 78.2 ms |
| UI acknowledgement p95, recording disabled | 39.0 ms | 38.8 ms |
| UI acknowledgement p95, recording plus writer | 39.0 ms | 36.0 ms |
| Sampled maximum watcher queue depth | 1 | 1 |
| Watcher drops | 0 | 0 |

The observed UI p95 difference is 0.05 ms on macOS and -2.8 ms on Linux. A negative difference reflects scheduling/sampling variation; it is not evidence that recording improves responsiveness. Both runs meet the proposed overhead target of 50 ms. The small-file/live snapshot targets are 500 ms, and the filesystem-row proxy target is 250 ms. Provider hook/tool row latency remains Phase 2 work.

The direct store grew from approximately 210 kB to 1.05 MB while retaining 1,280 synthetic facts plus checkpoints. Live/UI probes use separate disposable stores. A 5,000-file macOS direct probe (~10 MiB content, ten iterations) measured baseline 5.32 s, unchanged scan p95 133 ms, and one-file checkpoint p95 365 ms. Raw evidence: [macOS](performance-macos.json), [Linux](performance-linux.json), [5,000-file scale probe](performance-macos-5000.json).

## Reproduce

```sh
python3 -m unittest discover -s tests -v
python3 tools/performance_probe.py --files 1000 --iterations 20 --output /tmp/labradour-performance.json
python3 tools/performance_probe.py --files 5000 --iterations 10 --direct-only
```

The probe creates and removes disposable workspaces. Its UI measurements allocate their own PTYs, so it does not require an interactive outer terminal. Latency targets are reported as observations, not timing assertions that fail on a busy shared machine.

For Linux, start Docker and run from the repository root:

```sh
docker build -f tools/Dockerfile.acceptance -t labradour-acceptance .
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  labradour-acceptance python -m unittest discover -s tests -v
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  labradour-acceptance python tools/performance_probe.py --files 1000 --iterations 20
```

The image installs the declared dependencies and Git; tests run with the project mounted read-only and network disabled. The measured image used Git 2.47.3, pyte 0.8.2, watchdog 6.0.0, and wcwidth 0.9.1. Future image builds may receive newer OS packages or a different wcwidth within the declared range.

## Acceptance scope and limits

The suite covers dirty project index/ref isolation; creates/edits/deletes/reverts; binary bytes, executable modes, symlinks, nested repositories and unusual names; cached/forced scans; atomic saves; concurrent external/background writers; overflow reconciliation; exclusion-before-storage; hard budget accounting; interruption and storage failure; and safe session retention. Actual curses/PTY rendering, resize, input, and interruption fixtures pass on Linux as well as macOS.

A create/delete between scans is explicitly tested as an expected observation gap. Snapshot scans are live observations rather than atomic filesystem snapshots. A row's external/unknown actor is preserved for concurrent writers. These tests do not imply every write syscall or provider tool boundary is captured.

The metadata walk and immutable manifest processing still scale with included paths/content. Staged journal updates still copy the database once per batch. Initial baselines and forced full reads cost more than ordinary one-file changes. No throughput claim is made for arbitrarily large workspaces or unlimited multi-hour histories. Packed-object compaction, age-based retention, physical filesystem faults/power loss, and human native-provider acceptance remain separate follow-ups. The generic recorder is ready for Phase 2 adapter integration within this measured scope.
