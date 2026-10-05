"""Synthetic transport fixture. Does not establish native hook coverage."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from labradour.adapters.collector import submit


def run():
    for index, (kind, call) in enumerate([("session.started", None), ("turn.started", None),
                       ("tool.started", "read-1"), ("tool.completed", "read-1"),
                       ("tool.failed", "failed-1"), ("tool.denied", "denied-1"),
                       ("future.fixture.event", None), ("turn.completed", None), ("session.ended", None)]):
        submit({"schema_version": 1, "event_id": "fixture-%02d" % index, "provider": "fixture",
                "provider_session_id": "fixture-session", "kind": kind, "call_id": call,
                "payload": {"tool": "Read"} if call else {}})
    print("Synthetic adapter fixture delivered; native provider coverage remains unverified.")


if __name__ == "__main__":
    run()
