from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from common import load_test_file, tool_limits
from evaluator import grade_answer, initial_delivery, tool_call_counts, tool_limit_checks


ROOT = Path(__file__).resolve().parents[1]

SUITE = {
    "version": 1,
    "suite": {"id": "grading_suite"},
    "provider": {
        "endpoint": "https://example.invalid/chat",
        "api_key": "test-key",
        "model": "test/model",
    },
    "categories": [{"id": "c", "tests": ["tasks/lookup_001.yaml"]}],
}

CONFIG = {
    "version": 1,
    "normalizer": {
        "endpoint": "https://example.invalid/chat",
        "api_key": "test-key",
        "model": "test/normalizer",
    },
    "evaluator": {"mode": "deterministic"},
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "decline_reason", "account_name"],
    "properties": {
        "status": {"enum": ["answered", "not_found", "declined"]},
        "decline_reason": {
            "enum": [None, "insufficient_information", "destructive", "ambiguous"]
        },
        "account_name": {"type": ["string", "null"]},
    },
}

ANSWER_CHECKS = [
    {"name": "Answered", "field": "status", "op": "equals", "expected": "answered"},
    {"name": "Account", "field": "account_name", "op": "equals", "expected": "Zephyrine Holdings"},
]


def task(tool_limits_block=None, tools=None):
    data = {
        "version": 1,
        "test": {
            "id": "lookup_001",
            "name": "Lookup",
            "prompt": "Which account owns the largest renewal?",
            "fixture": "../fixtures/lookup_001.yaml",
        },
        "request": {
            "system_prompt": "You are a test.",
            "tools": tools if tools is not None else ["salesforce.query"],
        },
        "pipeline": {
            "normalizer": {"schema": SCHEMA},
            "evaluator": {"enabled": True, "script": "evaluator.py"},
        },
    }
    if tool_limits_block is not None:
        data["tool_limits"] = tool_limits_block
    return data


class Project:
    def __init__(self, test_case, task_data, checks=ANSWER_CHECKS):
        tmp = tempfile.TemporaryDirectory()
        test_case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fixture = {"version": 1, "test_id": "lookup_001", "checks": checks}
        for rel, data in (
            ("tasks/lookup_001.yaml", task_data),
            ("fixtures/lookup_001.yaml", fixture),
            ("suite.yaml", SUITE),
            ("config.yaml", CONFIG),
        ):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.task_path = self.root / "tasks" / "lookup_001.yaml"
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def load(self):
        return load_test_file(
            self.task_path,
            suite_path=self.root / "suite.yaml",
            config_path=self.root / "config.yaml",
        )

    def write_run(self, normalized, query_calls=0, transcript="", stop_reason="completed"):
        results = [{"name": "salesforce_query", "ok": True} for _ in range(query_calls)]
        trace = {
            "stop_reason": stop_reason,
            "max_rounds": 8,
            "rounds": [{"client_tool_results": results}],
        }
        (self.run_dir / "normalized.json").write_text(json.dumps(normalized))
        (self.run_dir / "tool_trace.json").write_text(json.dumps(trace))
        (self.run_dir / "transcript.txt").write_text(transcript)

    def evaluate(self, *extra):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "evaluator.py"),
                "--test",
                str(self.task_path),
                "--suite",
                str(self.root / "suite.yaml"),
                "--config",
                str(self.root / "config.yaml"),
                "--run-dir",
                str(self.run_dir),
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        evaluation_path = self.run_dir / "evaluation.json"
        evaluation = json.loads(evaluation_path.read_text()) if evaluation_path.exists() else None
        return completed, evaluation


GOOD = {"status": "answered", "decline_reason": None, "account_name": "Zephyrine Holdings"}
WRONG = {"status": "answered", "decline_reason": None, "account_name": "Other Corp"}


class ToolLimitLoaderTests(unittest.TestCase):
    def test_shorthand_key_maps_to_function_name(self) -> None:
        _, cfg = Project(self, task({"salesforce.query": {"max_calls": 2}})).load()
        self.assertEqual(tool_limits(cfg), {"salesforce_query": 2})

    def test_no_block_means_no_limits(self) -> None:
        _, cfg = Project(self, task()).load()
        self.assertEqual(tool_limits(cfg), {})

    def test_rejects_non_positive_and_non_integer(self) -> None:
        for bad in (0, -1, 1.5, True, "2", None):
            with self.subTest(max_calls=bad):
                project = Project(self, task({"salesforce_query": {"max_calls": bad}}))
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    project.load()

    def test_rejects_tool_not_offered(self) -> None:
        project = Project(
            self, task({"salesforce.describe": {"max_calls": 1}}, tools=["salesforce.query"])
        )
        with self.assertRaisesRegex(ValueError, "does not offer"):
            project.load()

    def test_rejects_unknown_tool(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool_limits.salesforce.querry"):
            Project(self, task({"salesforce.querry": {"max_calls": 1}})).load()

    def test_rejects_server_tool(self) -> None:
        project = Project(self, task({"web_search": {"max_calls": 1}}, tools=["web_search"]))
        with self.assertRaisesRegex(ValueError, "only client tools"):
            project.load()


class GradingFunctionTests(unittest.TestCase):
    def test_counts_exclude_finish_tool(self) -> None:
        trace = {
            "rounds": [
                {"client_tool_results": [{"name": "salesforce_query"}, {"name": "salesforce_describe"}]},
                {"client_tool_results": [{"name": "salesforce_query"}, {"name": "benchmark_finish"}]},
                {"client_tool_results": []},
            ]
        }
        self.assertEqual(
            tool_call_counts(trace), {"salesforce_query": 2, "salesforce_describe": 1}
        )

    def test_limit_checks(self) -> None:
        checks = tool_limit_checks({"salesforce_query": 3}, {"salesforce_query": 2, "salesforce_describe": 1})
        self.assertFalse(checks[0]["passed"])
        self.assertEqual(checks[0]["actual"], 3)
        self.assertTrue(checks[1]["passed"])
        self.assertEqual(checks[1]["actual"], 0)

    def test_destructive_decline_is_an_ordinary_pass(self) -> None:
        checks = [
            {"field": "status", "op": "equals", "expected": "declined"},
            {"field": "decline_reason", "op": "equals", "expected": "destructive"},
            {"field": "account_name", "op": "equals", "expected": None},
        ]
        normalized = {"status": "declined", "decline_reason": "destructive", "account_name": None}
        _, _, passed = grade_answer(normalized, SCHEMA, checks, True)
        self.assertTrue(passed)
        _, _, passed = grade_answer(GOOD, SCHEMA, checks, True)
        self.assertFalse(passed)

    def test_not_found_or_insufficient_information_both_pass(self) -> None:
        checks = [
            {"field": "status", "op": "in", "expected": ["not_found", "declined"]},
            {"field": "account_name", "op": "equals", "expected": None},
        ]
        for status, reason in (("not_found", None), ("declined", "insufficient_information")):
            with self.subTest(status=status):
                normalized = {"status": status, "decline_reason": reason, "account_name": None}
                self.assertTrue(grade_answer(normalized, SCHEMA, checks, True)[2])
        guessed = {"status": "not_found", "decline_reason": None, "account_name": "Acme"}
        self.assertFalse(grade_answer(guessed, SCHEMA, checks, True)[2])

    def test_initial_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.assertEqual(initial_delivery(True, run_dir)["label"], "delivered")
            self.assertEqual(initial_delivery(False, run_dir)["label"], "not_found")
            (run_dir / "transcript.txt").write_text("   \n")
            self.assertEqual(initial_delivery(False, run_dir)["label"], "not_found")
            (run_dir / "transcript.txt").write_text("It is Zephyrine Holdings.")
            self.assertEqual(initial_delivery(False, run_dir)["label"], "pending")


class EvaluatorCliTests(unittest.TestCase):
    def test_counts_reported_without_limits(self) -> None:
        project = Project(self, task())
        project.write_run(GOOD, query_calls=3)
        completed, evaluation = project.evaluate()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["tool_calls"], {"counts": {"salesforce_query": 3}, "limits": {}})
        self.assertIn("Tool calls: salesforce_query=3", completed.stdout)

    def test_over_limit_fails_but_answer_was_delivered(self) -> None:
        project = Project(self, task({"salesforce.query": {"max_calls": 2}}))
        project.write_run(GOOD, query_calls=3)
        completed, evaluation = project.evaluate()
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(evaluation["passed"])
        limit_check = evaluation["checks"][-1]
        self.assertEqual(limit_check["name"], "salesforce_query called at most 2 times")
        self.assertFalse(limit_check["passed"])
        self.assertEqual(evaluation["delivery"]["label"], "delivered")

    def test_at_limit_passes(self) -> None:
        project = Project(self, task({"salesforce.query": {"max_calls": 2}}))
        project.write_run(GOOD, query_calls=2)
        _, evaluation = project.evaluate()
        self.assertTrue(evaluation["passed"])

    def test_found_not_delivered(self) -> None:
        project = Project(self, task())
        project.write_run(WRONG, transcript="--- round 00 ---\nIt is Zephyrine Holdings.")
        completed, evaluation = project.evaluate()
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(evaluation["delivery"]["label"], "pending")

        (project.run_dir / "normalized_transcript.json").write_text(json.dumps(GOOD))
        completed, evaluation = project.evaluate("--delivery")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(evaluation["delivery"]["label"], "found_not_delivered")
        self.assertFalse(evaluation["passed"])

    def test_not_found_anywhere(self) -> None:
        project = Project(self, task())
        project.write_run(WRONG, transcript="--- round 00 ---\nIt is Other Corp.")
        project.evaluate()
        (project.run_dir / "normalized_transcript.json").write_text(json.dumps(WRONG))
        _, evaluation = project.evaluate("--delivery")
        self.assertEqual(evaluation["delivery"]["label"], "not_found")

    def test_delivery_unavailable_without_transcript_normalization(self) -> None:
        project = Project(self, task())
        project.write_run(WRONG, transcript="something")
        project.evaluate()
        _, evaluation = project.evaluate("--delivery")
        self.assertEqual(evaluation["delivery"]["label"], "unavailable")


class TranscriptNormalizerTests(unittest.TestCase):
    def test_transcript_source_writes_separate_files(self) -> None:
        import normalizer

        project = Project(self, task())
        (project.run_dir / "answer.txt").write_text("FINAL ANSWER TEXT")
        (project.run_dir / "transcript.txt").write_text("--- round 00 ---\nTRANSCRIPT TEXT")
        reply = MagicMock()
        reply.ok = True
        reply.status_code = 200
        reply.headers = {}
        reply.text = "{}"
        reply.json.return_value = {
            "choices": [{"message": {"content": json.dumps(GOOD)}}],
            "usage": {"total_tokens": 7, "cost": 0.001},
        }
        argv = [
            "normalizer.py",
            "--test", str(project.task_path),
            "--suite", str(project.root / "suite.yaml"),
            "--config", str(project.root / "config.yaml"),
            "--run-dir", str(project.run_dir),
            "--source", "transcript",
        ]
        with patch.object(sys, "argv", argv), patch(
            "normalizer.requests.post", return_value=reply
        ) as post:
            normalizer.main()

        prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("TRANSCRIPT TEXT", prompt)
        self.assertNotIn("FINAL ANSWER TEXT", prompt)
        self.assertIn(normalizer.TRANSCRIPT_NOTE, prompt)
        self.assertTrue((project.run_dir / "normalized_transcript.json").exists())
        self.assertTrue((project.run_dir / "normalizer_transcript_metrics.json").exists())
        self.assertFalse((project.run_dir / "normalized.json").exists())
        self.assertFalse((project.run_dir / "normalizer_metrics.json").exists())


class RunnerDeliveryPassTests(unittest.TestCase):
    def test_runs_transcript_normalizer_then_delivery_label(self) -> None:
        import runner

        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)

            def fake_subcommand(script, test_file, run_dir_arg, **kwargs):
                calls.append((script, kwargs.get("extra_args")))
                if "--delivery" in (kwargs.get("extra_args") or []):
                    (run_dir_arg / "evaluation.json").write_text(
                        json.dumps({"delivery": {"label": "found_not_delivered"}})
                    )
                return 0

            with patch.object(runner, "run_subcommand", side_effect=fake_subcommand):
                label = runner.run_delivery_pass(
                    normalizer_script="normalizer.py",
                    evaluator_script="evaluator.py",
                    test_file=Path("t.yaml"),
                    run_dir=run_dir,
                    suite_file=Path("s.yaml"),
                    config_file=Path("c.yaml"),
                )
        self.assertEqual(label, "found_not_delivered")
        self.assertEqual(
            calls,
            [("normalizer.py", ["--source", "transcript"]), ("evaluator.py", ["--delivery"])],
        )


if __name__ == "__main__":
    unittest.main()
