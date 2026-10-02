#!/usr/bin/env python3
"""Reproducible disposable recorder benchmark. Never touches a real workspace."""
import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import threading
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labradour.recorder import Recorder
from labradour.storage import file_bytes
from labradour.recorder import read_history
from labradour.pty_process import PtyProcess
from labradour.terminal import Terminal


def summary(values):
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": round(statistics.median(values) * 1000, 3),
            "p95_ms": round(ordered[max(0, int(len(values) * .95 + .999) - 1)] * 1000, 3),
            "max_ms": round(max(values) * 1000, 3)}


def ui_latency(workspace, store, iterations, recording):
    code = "import sys; print('AGENT_READY', flush=True);\nfor line in sys.stdin: print('ACK-' + line.strip(), flush=True)"
    command = [sys.executable, "-m", "labradour", "run", "--workspace", str(workspace)]
    if recording:
        command += ["--record", str(store), "--watch-backend", "polling"]
    command += ["--", sys.executable, "-u", "-c", code]
    child = PtyProcess(command, Path(__file__).resolve().parents[1], 40, 140)
    screen = Terminal(40, 140)
    stop = threading.Event()
    writer = None
    def wait_text(token, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = child.read()
            if raw:
                replies = screen.feed(raw)
                if replies:
                    child.send(replies)
            if token in "\n".join("".join(cell.text for cell in row) for row in screen.grid):
                return
            if child.poll() is not None:
                raise RuntimeError("UI exited before acknowledgement")
            time.sleep(.001)
        raise RuntimeError("UI acknowledgement timeout: " + token)
    def workload():
        counter = 0
        while not stop.wait(.02):
            (workspace / "background-load").write_text(str(counter))
            counter += 1
    try:
        wait_text("AGENT_READY")
        if recording:
            writer = threading.Thread(target=workload)
            writer.start()
        samples = []
        for index in range(iterations):
            token = "request-%05d" % index
            started = time.perf_counter()
            child.send((token + "\n").encode())
            wait_text("ACK-" + token)
            samples.append(time.perf_counter() - started)
        return summary(samples)
    finally:
        stop.set()
        if writer:
            writer.join()
        child.send(b"\x11")
        deadline = time.monotonic() + 8
        while child.poll() is None and time.monotonic() < deadline:
            child.read()
            time.sleep(.01)
        child.close()


def live_lag(workspace, store, iterations):
    recorder = Recorder(workspace, store, backend="polling", interval=.5)
    samples, rows = [], []
    seen = 0
    max_queue = 0
    try:
        recorder.start()
        for index in range(iterations):
            content = ("live-%d" % index).encode()
            started = time.time_ns()
            (workspace / "live-probe").write_bytes(content)
            deadline = time.monotonic() + 10
            observation, checkpoint = None, None
            while time.monotonic() < deadline:
                max_queue = max(max_queue, recorder.watcher.events.qsize())
                records = recorder.drain()
                visible_ns = time.time_ns()
                for record in records:
                    seen += 1
                    payload = record["payload"]
                    if record["kind"] == "filesystem.observed" and payload.get("path") == "live-probe" and record["received_ns"] >= started:
                        observation = observation or visible_ns
                    if record["kind"] == "snapshot.completed" and record["received_ns"] >= started:
                        commit = payload["commit"]
                        try:
                            if recorder.history.git("show", commit + ":live-probe") == content:
                                checkpoint = visible_ns
                        except Exception:
                            pass
                if checkpoint and observation:
                    break
                time.sleep(.005)
            if not checkpoint:
                raise RuntimeError("live capture acceptance failed: " + recorder.notice)
            samples.append((checkpoint - started) / 1e9)
            if observation:
                rows.append((observation - started) / 1e9)
        return {"snapshot_visible": summary(samples), "filesystem_row_visible": summary(rows) if rows else None,
                "max_queue_depth_sampled": max_queue, "watcher_dropped": recorder.watcher.dropped,
                "durable_events_consumed": seen, "metrics": dict(recorder.metrics),
                "fidelity": "waited for each checkpoint; intra-scan transient writes are not guaranteed"}
    finally:
        recorder.close()


def benchmark(files=1000, iterations=20, direct_only=False):
    with tempfile.TemporaryDirectory(prefix="labradour-performance-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        for index in range(files):
            directory = workspace / ("group-%02d" % (index % 20))
            directory.mkdir(exist_ok=True)
            (directory / ("file-%05d" % index)).write_bytes(("fixture %d\n" % index).encode() + b"x" * 2048)
        started = time.perf_counter()
        recorder = Recorder(workspace, root / "recording")
        startup = time.perf_counter() - started
        try:
            started = time.perf_counter()
            recorder.capture("baseline")
            baseline = time.perf_counter() - started
            sizes = [file_bytes(recorder.directory)]
            scans, captures, journals = [], [], []
            for index in range(iterations):
                started = time.perf_counter()
                recorder.scan()
                scans.append(time.perf_counter() - started)
                (workspace / "group-00/file-00000").write_bytes(("edit %d\n" % index).encode())
                started = time.perf_counter()
                recorder.capture("benchmark mutation")
                captures.append(time.perf_counter() - started)
                started = time.perf_counter()
                records = [("filesystem.observed", {"path": "probe", "operation": "file.modify"}, "benchmark", None) for _ in range(64)]
                if hasattr(recorder, "append_many"):
                    recorder.append_many(records)
                else:
                    for kind, payload, source, event_id in records:
                        recorder.append(kind, payload, source, event_id)
                journals.append(time.perf_counter() - started)
                recorder.drain()
                sizes.append(file_bytes(recorder.directory))
            report = {"platform": platform.platform(), "python": platform.python_version(),
                    "files": files, "iterations": iterations, "file_content_bytes": 2048,
                    "startup_ms": round(startup * 1000, 3), "baseline_ms": round(baseline * 1000, 3),
                    "unchanged_scan": summary(scans), "small_file_capture": summary(captures),
                    "journal_64_events": summary(journals), "store_bytes_initial": sizes[0], "store_bytes_final": sizes[-1],
                    "scope": "direct recorder timings; PTY and live capture lag are measured separately"}
        finally:
            recorder.close()
        if direct_only:
            report["scope"] = "direct recorder timings; no live/UI measurements"
            return report
        report["live"] = live_lag(workspace, root / "live-recording", iterations)
        report["ui_recording_disabled"] = ui_latency(workspace, root / "unused", iterations, False)
        report["ui_recording_with_background_writes"] = ui_latency(workspace, root / "ui-recording", iterations, True)
        report["ui_p95_overhead_ms"] = round(report["ui_recording_with_background_writes"]["p95_ms"] - report["ui_recording_disabled"]["p95_ms"], 3)
        report["targets"] = {"small_file_capture_p95_under_500ms": report["small_file_capture"]["p95_ms"] < 500,
                             "live_capture_p95_under_500ms": report["live"]["snapshot_visible"]["p95_ms"] < 500,
                             "filesystem_row_p95_under_250ms": report["live"]["filesystem_row_visible"] is not None and report["live"]["filesystem_row_visible"]["p95_ms"] < 250,
                             "ui_p95_overhead_under_50ms": report["ui_p95_overhead_ms"] < 50,
                             "no_watcher_drops": report["live"]["watcher_dropped"] == 0}
        report["scope"] = "disposable workspace; filesystem rows and synthetic native CLI; provider-hook coverage is separate"
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--direct-only", action="store_true", help="Skip live watcher and interactive UI measurements")
    args = parser.parse_args()
    if args.files < 1 or args.iterations < 1:
        parser.error("files and iterations must be positive")
    result = benchmark(args.files, args.iterations, args.direct_only)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
