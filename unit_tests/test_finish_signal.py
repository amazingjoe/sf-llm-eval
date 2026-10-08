from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import (
    FINISH_FUNCTION,
    FINISH_INSTRUCTION,
    apply_completion_settings,
    load_test_file,
)
from evaluator import self_assessment


ROOT = Path(__file__).resolve().parents[1]


def tool_call(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def finish_call(status="answered", note="", call_id="call_finish"):
    return tool_call(FINISH_FUNCTION, {"status": status, "note": note}, call_id)


def response(content=None, tool_calls=None, finish_reason="stop"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"finish_reason": finish_reason, "message": message}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def cfg(tools=None, max_rounds=4, required=True):
    raw = {
        "request": {
            "model": "test/model",
            "system_prompt": "You are a test.",
            "messages": [],
            "parameters": {},
            "tools": tools or [],
            "max_rounds": max_rounds,
            "timeout_seconds": 5,
        },
        "test": {"prompt": "What is 2 + 3?"},
        "completion": {"required": required},
    }
    return apply_completion_settings(raw)


class LoopHarness:
    """Runs run_inference_loop against scripted responses."""

    def __init__(self, test_case, responses, config, tool_context=None):
        from runner import run_inference_loop

        self.requests = []
        scripted = list(responses)

        def fake_post_chat(**kwargs):
            self.requests.append(kwargs["payload"])
            body = scripted.pop(0)
            round_dir = kwargs["round_dir"]
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / "response.raw.txt").write_text(json.dumps(body), encoding="utf-8")
            return body, 1.0, 200

        tmp = tempfile.TemporaryDirectory()
        test_case.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name)
        with patch("runner.post_chat", side_effect=fake_post_chat):
            _, self.metrics, _ = run_inference_loop(
                cfg=config,
                endpoint="https://example.invalid",
                headers={},
                run_dir=self.run_dir,
                tool_context=tool_context,
            )
        self.remaining = scripted

    def text(self, name):
        return (self.run_dir / name).read_text(encoding="utf-8")

    def json(self, name):
        return json.loads(self.text(name))


class FinishLoopTests(unittest.TestCase):
    def test_finish_with_answer_ends_run(self) -> None:
        run = LoopHarness(
            self,
            [response("2 + 3 = 5.", [finish_call(note="easy")], "tool_calls")],
            cfg(),
        )
        self.assertEqual(run.metrics["rounds"], 1)
        self.assertEqual(run.text("answer.txt"), "2 + 3 = 5.")
        completion = run.json("completion.json")
        self.assertTrue(completion["called"])
        self.assertEqual(completion["status"], "answered")
        self.assertEqual(completion["note"], "easy")
        self.assertEqual(run.metrics["stop_reason"], "completed")
        self.assertEqual(run.json("tool_trace.json")["stop_reason"], "completed")

    def test_request_offers_finish_tool_and_instruction(self) -> None:
        run = LoopHarness(self, [response("5", [finish_call()], "tool_calls")], cfg())
        payload = run.requests[0]
        names = [tool["function"]["name"] for tool in payload["tools"]]
        self.assertEqual(names, [FINISH_FUNCTION])
        self.assertIn(FINISH_INSTRUCTION, payload["messages"][0]["content"])

    def test_answer_then_bare_finish_is_empty_final(self) -> None:
        run = LoopHarness(
            self,
            [
                response("The answer is 5.", [tool_call("salesforce_org_info", {})], "tool_calls"),
                response(None, [finish_call()], "tool_calls"),
            ],
            cfg(tools=["salesforce.org_info"]),
        )
        self.assertEqual(run.text("answer.txt"), "")
        self.assertEqual(run.metrics["stop_reason"], "empty_final")
        self.assertIn("The answer is 5.", run.text("transcript.txt"))
        self.assertTrue(run.json("completion.json")["called"])

    def test_invalid_finish_returns_error_and_continues(self) -> None:
        run = LoopHarness(
            self,
            [
                response("5", [finish_call(status="done")], "tool_calls"),
                response("5", [finish_call(status="answered")], "tool_calls"),
            ],
            cfg(),
        )
        self.assertEqual(run.metrics["rounds"], 2)
        completion = run.json("completion.json")
        self.assertEqual(completion["status"], "answered")
        self.assertEqual(len(completion["invalid_calls"]), 1)
        tool_messages = json.loads(
            (run.run_dir / "rounds" / "00" / "tool_messages.json").read_text()
        )
        self.assertIn("status must be one of", tool_messages[0]["content"])

    def test_no_finish_records_missing_claim(self) -> None:
        run = LoopHarness(self, [response("It is 5.")], cfg())
        self.assertEqual(run.text("answer.txt"), "It is 5.")
        completion = run.json("completion.json")
        self.assertTrue(completion["offered"])
        self.assertFalse(completion["called"])
        self.assertEqual(run.metrics["stop_reason"], "completed")

    def test_max_rounds_is_recorded_not_raised(self) -> None:
        loop = [
            response("Checking...", [tool_call("salesforce_org_info", {}, f"c{i}")], "tool_calls")
            for i in range(2)
        ]
        run = LoopHarness(self, loop, cfg(tools=["salesforce.org_info"], max_rounds=2))
        self.assertEqual(run.metrics["stop_reason"], "max_rounds")
        self.assertEqual(run.text("answer.txt"), "")
        self.assertIn("Checking...", run.text("transcript.txt"))

    def test_provider_length_cutoff_is_max_tokens(self) -> None:
        run = LoopHarness(self, [response("The answer is", finish_reason="length")], cfg())
        self.assertEqual(run.metrics["stop_reason"], "max_tokens")

    def test_finish_skips_other_calls_in_same_message(self) -> None:
        run = LoopHarness(
            self,
            [
                response(
                    "5",
                    [tool_call("salesforce_org_info", {}), finish_call()],
                    "tool_calls",
                )
            ],
            cfg(tools=["salesforce.org_info"]),
        )
        trace = run.json("tool_trace.json")
        self.assertEqual(trace["client_tool_executions"], 0)
        self.assertEqual(trace["rounds"][0]["ignored_tool_calls"], ["salesforce_org_info"])

    def test_legacy_test_without_finish(self) -> None:
        run = LoopHarness(self, [response("5")], cfg(required=False))
        completion = run.json("completion.json")
        self.assertFalse(completion["offered"])
        self.assertNotIn("tools", run.requests[0])
        self.assertEqual(run.requests[0]["messages"][0]["content"], "You are a test.")


class CompletionSettingsTests(unittest.TestCase):
    def test_does_not_duplicate_listed_finish_tool(self) -> None:
        raw = {
            "request": {"system_prompt": "Hi.", "tools": ["benchmark.finish"]},
            "completion": {"required": True},
        }
        out = apply_completion_settings(apply_completion_settings(raw))
        self.assertEqual(out["request"]["tools"], ["benchmark.finish"])
        self.assertEqual(out["request"]["system_prompt"].count(FINISH_INSTRUCTION), 1)

    def test_committed_arithmetic_task_requires_finish(self) -> None:
        with patch.dict(os.environ, {"openrouter_key": "test-key"}):
            _, loaded = load_test_file(
                ROOT / "benchmarks" / "tasks" / "harness_arithmetic_001.yaml",
                suite_path=ROOT / "test-sets" / "benchmark_health.yaml",
                config_path=ROOT / "config.yaml",
            )
        self.assertIn("benchmark.finish", loaded["request"]["tools"])
        self.assertIn(FINISH_INSTRUCTION, loaded["request"]["system_prompt"])


class SelfAssessmentTests(unittest.TestCase):
    answered = [{"field": "result", "expected": 5}]
    declined = [{"field": "status", "op": "equals", "expected": "declined"}]
    absent = [{"field": "status", "op": "in", "expected": ["not_found", "declined"]}]

    def claim(self, status):
        return {"offered": True, "called": status is not None, "status": status, "note": "n"}

    def test_match(self) -> None:
        self.assertEqual(self_assessment(self.claim("answered"), self.answered, True)["label"], "match")

    def test_false_success(self) -> None:
        result = self_assessment(self.claim("answered"), self.answered, False)
        self.assertEqual(result["label"], "false_success")

    def test_wrong_status(self) -> None:
        result = self_assessment(self.claim("answered"), self.declined, False)
        self.assertEqual(result["label"], "wrong_status")
        self.assertEqual(result["expected_status"], ["declined"])

    def test_wrong_status_even_when_answer_passes(self) -> None:
        result = self_assessment(self.claim("declined"), self.answered, True)
        self.assertEqual(result["label"], "wrong_status")

    def test_in_accepts_either_status(self) -> None:
        result = self_assessment(self.claim("not_found"), self.absent, True)
        self.assertEqual(result["label"], "match")

    def test_missing(self) -> None:
        result = self_assessment(self.claim(None), self.answered, True)
        self.assertEqual(result["label"], "missing")
        self.assertIsNone(result["note"])

    def test_not_offered(self) -> None:
        self.assertIsNone(self_assessment({"offered": False}, self.answered, True))
        self.assertIsNone(self_assessment(None, self.answered, True))


class EvaluatorCliTests(unittest.TestCase):
    def evaluate(self, *, stop_reason, completion):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "normalized.json").write_text(json.dumps({"result": 5}))
            (run_dir / "completion.json").write_text(json.dumps(completion))
            (run_dir / "tool_trace.json").write_text(
                json.dumps({"stop_reason": stop_reason, "max_rounds": 8})
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "evaluator.py"),
                    "--test",
                    str(ROOT / "benchmarks" / "tasks" / "harness_arithmetic_001.yaml"),
                    "--suite",
                    str(ROOT / "test-sets" / "benchmark_health.yaml"),
                    "--config",
                    str(ROOT / "config.yaml"),
                    "--run-dir",
                    str(run_dir),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "openrouter_key": "test-key"},
            )
            evaluation = json.loads((run_dir / "evaluation.json").read_text())
        return completed, evaluation

    def test_writes_stop_reason_and_label(self) -> None:
        completed, evaluation = self.evaluate(
            stop_reason="completed",
            completion={"offered": True, "called": True, "status": "answered", "note": ""},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["stop_reason"], "completed")
        self.assertEqual(evaluation["self_assessment"]["label"], "match")
        self.assertIn("Self-assessment: match", completed.stdout)

    def test_max_rounds_fails_even_if_answer_matches(self) -> None:
        completed, evaluation = self.evaluate(
            stop_reason="max_rounds",
            completion={"offered": True, "called": False, "status": None, "note": None},
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertFalse(evaluation["passed"])
        turn_check = evaluation["checks"][-1]
        self.assertEqual(turn_check["name"], "Finished within max_rounds")
        self.assertFalse(turn_check["passed"])
        self.assertIn("ran out of turns", turn_check["detail"])
        self.assertEqual(evaluation["self_assessment"]["label"], "missing")


if __name__ == "__main__":
    unittest.main()
