import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from labradour.adapters.correlation import project
from labradour.adapters.providers import normalize
from labradour.adapters.collector import Collector, submit
from labradour.recorder import Recorder, read_history


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.rows = []

    def row(self, kind, payload, event_id=None):
        r = {"sequence": len(self.rows) + 1, "session_id": "host", "kind": kind,
             "event_id": event_id or "row-" + str(len(self.rows)), "payload": payload}
        self.rows.append(r)
        return r

    def tool(self, hook, call="a", provider="claude", actor=None, turn=None, session="native", **raw):
        p = normalize(provider, dict(hook_event_name=hook, tool_use_id=call,
                                    session_id=session, agent_id=actor, turn_id=turn, **raw))
        p["launch_id"] = "launch"
        return self.row("adapter.event", p)

    def snapshot(self, changes=(), **payload):
        return self.row("snapshot.completed", dict(changes=[dict(path=p, operation=o) for p, o in changes],
                                                  commit="a" * 40, before_commit="b" * 40, **payload))

    def boundary(self, event, snapshot, **fields):
        return self.row("adapter.boundary", dict(adapter_event_id=event["payload"]["event_id"],
            launch_id=event["payload"]["launch_id"], phase=event["payload"]["boundary"],
            completed=True, checkpoint=snapshot["payload"]["commit"],
            snapshot_event_id=snapshot["event_id"], quality="observed-live-state", **fields))

    def test_terminal_outcomes_and_contradiction(self):
        for hook, state in (("PostToolUse", "completed"), ("PostToolUseFailure", "failed"),
                            ("PermissionDenied", "denied")):
            self.tool("PreToolUse", call=state)
            self.tool(hook, call=state)
        self.tool("PreToolUse", call="interrupted")
        self.tool("PostToolUseFailure", call="interrupted", is_interrupt=True)
        self.tool("PostToolUseFailure", call="completed")
        actions = project(self.rows)["actions"]
        self.assertEqual([a["state"] for a in actions], ["inconsistent", "failed", "denied", "interrupted"])

    def test_explicit_scope_and_missing_identity(self):
        for provider, actor, turn, session in (("claude", None, None, "s"), ("codex", None, None, "s"),
                ("claude", "child", None, "s"), ("claude", None, "t", "s"), ("claude", None, None, "other")):
            self.tool("PreToolUse", provider=provider, actor=actor, turn=turn, session=session)
            self.tool("PostToolUse", provider=provider, actor=actor, turn=turn, session=session)
        self.tool("PreToolUse", call=None)
        self.tool("PostToolUse", call=None)
        actions = project(self.rows, "interrupted")["actions"]
        self.assertEqual(len(actions), 7)
        self.assertEqual([a["state"] for a in actions[:5]], ["completed"] * 5)
        self.assertEqual(actions[5]["state"], "incomplete")
        self.assertEqual(actions[6]["identity_quality"], "unlinked-observation")

    def test_approval_interrupt_and_crash_are_not_denials(self):
        self.tool("PreToolUse", provider="codex", turn="t")
        self.tool("PermissionRequest", provider="codex", turn="t")
        p = normalize("codex", dict(hook_event_name="Interrupt", session_id="native", turn_id="t"))
        self.row("adapter.event", dict(p, launch_id="launch"))
        action = project(self.rows)["actions"][0]
        self.assertEqual(action["state"], "awaiting-approval")
        self.assertIn("turn interruption", " ".join(action["notes"]))
        self.assertEqual(project(self.rows, "interrupted")["actions"][0]["state"], "incomplete")

    def test_parallel_windows_share_effects_include_intermediate_reverts(self):
        a = self.tool("PreToolUse", call="a")
        baseline = self.snapshot([("before.txt", "create")])
        self.boundary(a, baseline)
        b = self.tool("PreToolUse", call="b", actor="child")
        self.boundary(b, self.snapshot())
        self.snapshot([("shared.txt", "modify")])
        self.snapshot([("shared.txt", "modify")])  # Revert is a distinct saved effect.
        end_a = self.tool("PostToolUse", call="a")
        self.boundary(end_a, self.snapshot())
        end_b = self.tool("PostToolUse", call="b", actor="child")
        self.boundary(end_b, self.snapshot([("external.txt", "create")]))
        result = project(self.rows, "completed")
        shared = [e for e in result["effects"] if e["path"] == "shared.txt"]
        self.assertEqual(len(shared), 2)
        self.assertNotEqual(shared[0]["id"], shared[1]["id"])
        self.assertTrue(all(len(e["candidate_action_ids"]) == 2 for e in shared))
        self.assertTrue(all(e["quality"] == "overlapping-calls" for e in shared))
        self.assertTrue(all(e["attribution"] == "external-or-unknown" for e in result["effects"]))
        self.assertEqual(result["effects"][0]["candidate_action_ids"], [])
        self.assertEqual(len(result["effects"][-1]["candidate_action_ids"]), 1)
        self.assertEqual(result, project(list(reversed(self.rows)), "completed"))

    def test_late_receipt_and_unavailable_checkpoint_remove_window(self):
        start = self.tool("PreToolUse")
        self.boundary(start, self.snapshot())
        end = self.tool("PostToolUse")
        self.boundary(end, self.snapshot([("x", "create")]))
        self.row("adapter.event", dict(kind="adapter.health", launch_id="launch",
            payload=dict(related_event_id=start["payload"]["event_id"], phase="before")))
        result = project(self.rows)
        self.assertEqual(result["actions"][0]["correlation_quality"], "incomplete-evidence")
        self.assertEqual(result["effects"][0]["candidate_action_ids"], [])
        self.assertEqual(len(result["gaps"]), 1)

    def test_actor_parent_is_only_explicit_and_empty_output_is_available(self):
        self.tool("PostToolUse", actor="child", tool_response="")
        p = normalize("claude", dict(hook_event_name="SubagentStart", session_id="native", agent_id="child",
                                     parent_agent_id="parent"))
        self.row("adapter.event", dict(p, launch_id="launch"))
        result = project(self.rows)
        self.assertEqual(len(result["actors"]), 1)
        self.assertEqual(len(result["actors"][0]["parent_actor_ids"]), 1)
        self.assertEqual(len(result["actions"][0]["output_refs"]), 1)
        self.assertIn("without an earlier request", " ".join(result["actions"][0]["notes"]))

    def test_truncated_facts_and_failed_scans_are_gaps(self):
        self.row("adapter.event", dict(kind="tool.completed", provider="claude", payload_truncated=True))
        self.row("snapshot.failed", {})
        self.row("collector.overflow", {})
        self.row("adapter.event", dict(kind="tool.started", provider="generic", payload={"raw": None}))
        result = project(self.rows, "completed-with-gaps")
        self.assertEqual(len(result["gaps"]), 3)
        self.assertEqual(result["actions"][0]["identity_quality"], "unlinked-observation")
        self.assertEqual(result["actions"][0]["correlation_quality"], "incomplete-evidence")

    def test_launch_and_host_identity_are_separate(self):
        for host, launch in (("host", "launch"), ("host", "other"), ("other-host", "launch")):
            for hook in ("PreToolUse", "PostToolUse"):
                r = self.tool(hook)
                r["session_id"] = host
                r["payload"]["launch_id"] = launch
        result = project(self.rows)
        self.assertEqual(len(result["actions"]), 3)
        self.assertEqual(len({a["id"] for a in result["actions"]}), 3)
        self.assertTrue(all(a["state"] == "completed" for a in result["actions"]))

    def test_failed_capture_in_window_prevents_complete_correlation(self):
        before = self.tool("PreToolUse")
        self.boundary(before, self.snapshot())
        self.row("snapshot.failed", {})
        after = self.tool("PostToolUse")
        self.boundary(after, self.snapshot([("x", "create")]))
        result = project(self.rows)
        self.assertEqual(result["actions"][0]["correlation_quality"], "incomplete-evidence")
        self.assertEqual(result["effects"][0]["candidate_action_ids"], [])

    def test_real_journal_replay_and_cli_are_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, spool, store = root / "workspace", root / "spool", root / "store"
            workspace.mkdir()
            spool.mkdir()
            collector = Collector(workspace, spool)
            recorder = Recorder(workspace, store, events=spool, backend="polling", interval=60,
                                adapter_collector=collector, adapter_provider="claude")
            try:
                recorder.start()
                # Stop background collection to exercise exact receipt windows with the single writer.
                recorder.stop.set()
                recorder.thread.join()
                before = normalize("claude", dict(hook_event_name="PreToolUse", session_id="s", tool_use_id="c"))
                submit(before, collector.environment())
                recorder.collect_adapters()
                (workspace / "a.txt").write_text("changed")
                after = normalize("claude", dict(hook_event_name="PostToolUse", session_id="s", tool_use_id="c"))
                submit(after, collector.environment())
                recorder.collect_adapters()
                session = recorder.session
                recorder.cleanup_gap = {"pid": 12345, "quality": "fixture cleanup gap"}
                recorder.failed = True
            finally:
                collector.close()
                recorder.close()
            rows = read_history(store, session)
            expected = project(rows, read_history(store)[0]["status"])
            cli = subprocess.run([sys.executable, "-m", "labradour", "actions", str(store), "--session", session],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(cli.stdout), expected)
            self.assertEqual(rows, read_history(store, session))
            self.assertEqual(expected["actions"][0]["state"], "completed")
            self.assertEqual(len(expected["actions"][0]["effect_ids"]), 1)
            self.assertEqual(expected["session_status"], "completed-with-gaps")
            self.assertEqual(expected["gaps"][0]["kind"], "process.cleanup-gap")


if __name__ == "__main__":
    unittest.main()
