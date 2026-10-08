from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from common import iter_suite_tests, load_suite_file, load_test_file
from evaluator import evaluation_checks


ROOT = Path(__file__).resolve().parents[1]

SECRET_ANSWER = "Zephyrine Holdings"

SUITE = {
    "version": 1,
    "suite": {"id": "loader_suite"},
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


def task(**test_overrides):
    test = {
        "id": "lookup_001",
        "name": "Lookup",
        "prompt": "Which account owns the largest renewal?",
        "success_criteria": "Name the account and nothing else.",
        "fixture": "../fixtures/lookup_001.yaml",
        **test_overrides,
    }
    return {
        "version": 1,
        "test": test,
        "request": {"system_prompt": "You are a test.", "parameters": {}},
        "pipeline": {
            "normalizer": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {"enum": ["answered", "not_found", "declined"]},
                        "account_name": {"type": ["string", "null"]},
                    },
                }
            },
            "evaluator": {"enabled": True, "script": "evaluator.py"},
        },
    }


def fixture(**overrides):
    return {
        "version": 1,
        "test_id": "lookup_001",
        "checks": [
            {"field": "status", "op": "equals", "expected": "answered"},
            {
                "field": "account_name",
                "op": "case_insensitive_equals",
                "expected": SECRET_ANSWER,
            },
        ],
        **overrides,
    }


class BenchmarkTaskFixture:
    """Writes a task, fixture, suite, and config into a temp project."""

    def __init__(self, task_data, fixture_data):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for rel, data in (
            ("tasks/lookup_001.yaml", task_data),
            ("fixtures/lookup_001.yaml", fixture_data),
            ("suite.yaml", SUITE),
            ("config.yaml", CONFIG),
        ):
            if data is None:
                continue
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.task_path = self.root / "tasks" / "lookup_001.yaml"

    def load(self):
        return load_test_file(
            self.task_path,
            suite_path=self.root / "suite.yaml",
            config_path=self.root / "config.yaml",
        )

    def cleanup(self):
        self._tmp.cleanup()


class BenchmarkLoaderTests(unittest.TestCase):
    def make(self, task_data=None, fixture_data=None):
        project = BenchmarkTaskFixture(
            task_data if task_data is not None else task(),
            fixture_data if fixture_data is not None else fixture(),
        )
        self.addCleanup(project.cleanup)
        return project

    def test_loaded_config_does_not_contain_fixture(self) -> None:
        raw, cfg = self.make().load()
        self.assertNotIn(SECRET_ANSWER, json.dumps(raw))
        self.assertNotIn(SECRET_ANSWER, json.dumps(cfg))
        self.assertEqual(cfg["test"]["fixture"], "../fixtures/lookup_001.yaml")

    def test_evaluator_reads_checks_from_fixture(self) -> None:
        project = self.make()
        _, cfg = project.load()
        checks, fixture_path = evaluation_checks(cfg, project.task_path)
        self.assertEqual(fixture_path, (project.root / "fixtures" / "lookup_001.yaml").resolve())
        self.assertEqual(checks[1]["expected"], SECRET_ANSWER)

    def test_rejects_inline_evaluator_checks(self) -> None:
        data = task()
        data["pipeline"]["evaluator"]["checks"] = [
            {"field": "account_name", "expected": SECRET_ANSWER}
        ]
        with self.assertRaisesRegex(ValueError, "pipeline.evaluator.checks"):
            self.make(task_data=data).load()

    def test_rejects_expected_key_anywhere(self) -> None:
        data = task(metadata={"expected_count": 3})
        with self.assertRaisesRegex(ValueError, r"test\.metadata\.expected_count"):
            self.make(task_data=data).load()

    def test_schema_property_named_expected_is_allowed(self) -> None:
        data = task()
        data["pipeline"]["normalizer"]["schema"]["properties"]["expected"] = {
            "type": "string"
        }
        self.make(task_data=data).load()

    def test_rejects_missing_fixture(self) -> None:
        project = BenchmarkTaskFixture(task(), None)
        self.addCleanup(project.cleanup)
        with self.assertRaises(FileNotFoundError):
            project.load()

    def test_rejects_fixture_for_other_test(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.make(fixture_data=fixture(test_id="other_001")).load()

    def test_rejects_fixture_without_expected(self) -> None:
        data = fixture(checks=[{"field": "account_name", "op": "equals"}])
        with self.assertRaisesRegex(ValueError, "must set expected"):
            self.make(fixture_data=data).load()

    def test_rejects_answer_in_success_criteria(self) -> None:
        data = task(success_criteria=f"The answer should mention {SECRET_ANSWER.upper()}.")
        with self.assertRaisesRegex(ValueError, "test.success_criteria contains"):
            self.make(task_data=data).load()

    def test_rejects_answer_in_system_prompt(self) -> None:
        data = task()
        data["request"]["system_prompt"] = f"Hint: {SECRET_ANSWER}"
        with self.assertRaisesRegex(ValueError, "request.system_prompt contains"):
            self.make(task_data=data).load()

    def test_schema_enum_values_are_not_leaks(self) -> None:
        data = task(success_criteria="If you found it, your status is answered.")
        self.make(task_data=data).load()

    def test_leak_check_matches_whole_words_only(self) -> None:
        data = fixture(
            checks=[{"field": "account_name", "op": "equals", "expected": "Ace"}]
        )
        project = self.make(
            task_data=task(prompt="Which account had the most space on its plan?"),
            fixture_data=data,
        )
        project.load()


class BenchmarkRequestTests(unittest.TestCase):
    def test_request_has_success_criteria_and_no_fixture(self) -> None:
        from runner import run_inference_loop

        project = BenchmarkTaskFixture(task(), fixture())
        self.addCleanup(project.cleanup)
        _, cfg = project.load()

        def fake_post_chat(**kwargs):
            body = {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Some account."},
                    }
                ],
                "usage": {},
            }
            round_dir = kwargs["round_dir"]
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / "request.json").write_text(
                json.dumps(kwargs["payload"]), encoding="utf-8"
            )
            (round_dir / "response.raw.txt").write_text(json.dumps(body), encoding="utf-8")
            return body, 1.0, 200

        with tempfile.TemporaryDirectory() as tmp, patch(
            "runner.post_chat", side_effect=fake_post_chat
        ):
            run_dir = Path(tmp)
            run_inference_loop(
                cfg=cfg,
                endpoint="https://example.invalid",
                headers={},
                run_dir=run_dir,
            )
            request_text = (run_dir / "request.json").read_text(encoding="utf-8")
            round_text = (run_dir / "rounds" / "00" / "request.json").read_text(
                encoding="utf-8"
            )

        for text in (request_text, round_text):
            self.assertNotIn(SECRET_ANSWER, text)
            self.assertNotIn("lookup_001.yaml", text)
        user = json.loads(request_text)["messages"][-1]["content"]
        self.assertEqual(
            user,
            "Which account owns the largest renewal?\n\n"
            "Success criteria:\nName the account and nothing else.",
        )


class CommittedBenchmarkTests(unittest.TestCase):
    def test_benchmark_health_suite_loads(self) -> None:
        suite_path = ROOT / "test-sets" / "benchmark_health.yaml"
        with patch.dict(os.environ, {"openrouter_key": "test-key"}):
            raw_suite, _ = load_suite_file(suite_path)
            entries = iter_suite_tests(raw_suite, suite_path)
            self.assertTrue(entries)
            for entry in entries:
                _, cfg = load_test_file(
                    entry["test_path"],
                    suite_path=suite_path,
                    config_path=ROOT / "config.yaml",
                )
                checks, fixture_path = evaluation_checks(cfg, entry["test_path"])
                self.assertTrue(checks)
                self.assertTrue(fixture_path.is_relative_to(ROOT / "benchmarks" / "fixtures"))


if __name__ == "__main__":
    unittest.main()
