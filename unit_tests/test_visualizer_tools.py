from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "visualizer"))

import serve as viz  # noqa: E402


class LoadTestToolsTests(unittest.TestCase):
    def test_assembles_query_call_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_dir = Path(tmp)
            round_dir = test_dir / "rounds" / "00"
            round_dir.mkdir(parents=True)
            (round_dir / "response.json").write_text(
                json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "salesforce_query",
                                                "arguments": json.dumps(
                                                    {
                                                        "query": "SELECT Name FROM Account LIMIT 1"
                                                    }
                                                ),
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (round_dir / "response_meta.json").write_text(
                json.dumps({"latency_ms": 12.5}), encoding="utf-8"
            )
            (round_dir / "tool_results.json").write_text(
                json.dumps(
                    [
                        {
                            "name": "salesforce_query",
                            "ok": True,
                            "latency_ms": 30,
                            "returned": 1,
                            "total_size": 1,
                            "truncated": False,
                            "cache_hit": True,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (round_dir / "tool_messages.json").write_text(
                json.dumps(
                    [
                        {
                            "role": "tool",
                            "tool_call_id": "call_1",
                            "content": json.dumps(
                                {
                                    "records": [{"Name": "Acme Incorporated"}],
                                    "returned": 1,
                                }
                            ),
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (test_dir / "request.json").write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "type": "function",
                                "function": {"name": "salesforce_query"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            tools = viz.load_test_tools(
                test_dir, json.loads((test_dir / "request.json").read_text())
            )

        self.assertEqual(tools["client_tools"], ["salesforce_query"])
        self.assertEqual(tools["client_tool_executions"], 1)
        call = tools["rounds"][0]["calls"][0]
        self.assertEqual(call["name"], "salesforce_query")
        self.assertEqual(call["arguments"]["query"], "SELECT Name FROM Account LIMIT 1")
        self.assertEqual(call["result"]["records"][0]["Name"], "Acme Incorporated")
        self.assertTrue(call["ok"])
        self.assertTrue(call["cache_hit"])

    def test_real_account_lookup_run_if_present(self) -> None:
        run = (
            ROOT
            / "runs"
            / "harness_smoke"
            / "20260911T202323.062455Z"
            / "salesforce_account_lookup"
            / "account_lookup_001"
        )
        if not run.is_dir():
            self.skipTest("sample run not present")
        tools = viz.load_test_tools(run)
        self.assertGreaterEqual(tools["client_tool_executions"], 1)
        query_rounds = [
            call
            for round in tools["rounds"]
            for call in round["calls"]
            if call["name"] == "salesforce_query"
        ]
        self.assertTrue(query_rounds)
        self.assertIn("Acme Incorporated", json.dumps(query_rounds[0]["arguments"]))
        self.assertIn("Acme Incorporated", json.dumps(query_rounds[0]["result"]))

    def test_web_search_uses_provider_query_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_dir = Path(tmp)
            round_dir = test_dir / "rounds" / "00"
            round_dir.mkdir(parents=True)
            (round_dir / "response.json").write_text(
                json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "search_1",
                                            "type": "openrouter:web_search",
                                            "function": {
                                                "name": "web_search",
                                                "arguments": '{"query": "current CEO of Salesforce"}',
                                            },
                                        }
                                    ],
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url_citation": {
                                                "url": "https://example.com/ceo",
                                                "title": "Salesforce CEO",
                                                "content": "Marc Benioff is CEO.",
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"server_tool_use_details": {"web_search_requests": 1}},
                    }
                ),
                encoding="utf-8",
            )
            (round_dir / "annotations.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "url_citation",
                            "url_citation": {
                                "url": "https://example.com/ceo",
                                "title": "Salesforce CEO",
                                "content": "Marc Benioff is CEO.",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            tools = viz.load_test_tools(test_dir)
        call = tools["rounds"][0]["calls"][0]
        self.assertEqual(call["name"], "openrouter:web_search")
        self.assertEqual(call["arguments"]["query"], "current CEO of Salesforce")
        self.assertEqual(call["result"]["sources"][0]["title"], "Salesforce CEO")

    def test_web_search_round_becomes_tool_card(self) -> None:
        run = (
            ROOT
            / "runs"
            / "harness_smoke"
            / "20260911T202323.062455Z"
            / "web_search"
            / "salesforce_ceo_001"
        )
        if not run.is_dir():
            self.skipTest("sample run not present")
        tools = viz.load_test_tools(run)
        search_calls = [
            call
            for round in tools["rounds"]
            for call in round["calls"]
            if "search" in str(call["name"])
        ]
        self.assertTrue(search_calls)
        call = search_calls[0]
        self.assertTrue(call["ok"])
        self.assertEqual(call["returned"], 1)
        self.assertEqual(
            call["result"]["sources"][0]["url"].startswith("https://investor.salesforce.com"),
            True,
        )


class EnrichRunCostTests(unittest.TestCase):
    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_splits_inference_from_pipeline_costs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "harness_smoke" / "run1"
            test_dir = run_dir / "lookup" / "account_001"
            self._write_json(
                test_dir / "metrics.json",
                {"cost_usd": 0.10, "total_tokens": 100, "client_tool_executions": 1},
            )
            self._write_json(
                test_dir / "normalizer_metrics.json",
                {"cost_usd": 0.03, "total_tokens": 40, "latency_ms": 12},
            )
            self._write_json(
                test_dir / "evaluator_metrics.json",
                {"cost_usd": 0.02, "total_tokens": 20},
            )
            summary = {
                "suite_id": "harness_smoke",
                "suite_name": "Smoke",
                "tests": [
                    {
                        "test_id": "account_001",
                        "category_id": "lookup",
                        "latency_ms": 50,
                        "status": "pass",
                        "score": 1.0,
                    }
                ],
                "counts": {"total": 1, "pass": 1, "fail": 0, "error": 0, "completed": 0},
            }
            listing = viz.enrich_run(summary, run_dir)
            test = listing["tests"][0]
            self.assertAlmostEqual(test["cost_usd"], 0.10)
            self.assertEqual(test["total_tokens"], 100)
            self.assertAlmostEqual(test["normalizer_cost_usd"], 0.03)
            self.assertEqual(test["normalizer_tokens"], 40)
            self.assertAlmostEqual(test["evaluator_cost_usd"], 0.02)
            self.assertEqual(test["evaluator_tokens"], 20)
            self.assertAlmostEqual(test["pipeline_cost_usd"], 0.05)
            self.assertEqual(test["pipeline_tokens"], 60)
            self.assertAlmostEqual(test["run_cost_usd"], 0.15)
            self.assertEqual(test["run_tokens"], 160)
            totals = listing["totals"]
            self.assertAlmostEqual(totals["cost_usd"], 0.10)
            self.assertEqual(totals["total_tokens"], 100)
            self.assertAlmostEqual(totals["pipeline_cost_usd"], 0.05)
            self.assertAlmostEqual(totals["run_cost_usd"], 0.15)
            self.assertEqual(totals["run_tokens"], 160)

    def test_sample_run_model_cost_excludes_normalizer(self) -> None:
        run_dir = (
            ROOT / "runs" / "harness_smoke" / "20260911T202323.062455Z"
        )
        summary_path = run_dir / "summary.json"
        if not summary_path.is_file():
            self.skipTest("sample run not present")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        listing = viz.enrich_run(summary, run_dir)
        inference = 0.0
        pipeline = 0.0
        for test in listing["tests"]:
            if test["pipeline_cost_usd"]:
                self.assertGreater(test["run_cost_usd"], test["cost_usd"])
            inference += test["cost_usd"] or 0
            pipeline += test["pipeline_cost_usd"] or 0
        self.assertAlmostEqual(listing["totals"]["cost_usd"], inference)
        self.assertAlmostEqual(listing["totals"]["pipeline_cost_usd"] or 0, pipeline)
        self.assertAlmostEqual(
            listing["totals"]["run_cost_usd"],
            inference + pipeline,
        )


class BenchmarkTaskLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.tasks = root / "benchmarks" / "tasks"
        self.fixtures = root / "benchmarks" / "fixtures"
        self.tasks.mkdir(parents=True)
        self.fixtures.mkdir(parents=True)
        for name, value in (
            ("REPO_ROOT", root),
            ("BENCHMARK_TASKS_DIR", self.tasks),
            ("BENCHMARK_FIXTURES_DIR", self.fixtures),
            ("TESTS_DIR", root / "tests"),
            ("SUITES_DIR", root / "test-sets"),
        ):
            patcher = patch.object(viz, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, **test_overrides):
        return {
            "version": 1,
            "test": {
                "id": "lookup_001",
                "name": "Lookup",
                "prompt": "Which account owns the largest renewal?",
                **test_overrides,
            },
            "request": {"system_prompt": "You are a test."},
            "pipeline": {
                "normalizer": {"schema": {"type": "object"}},
                "evaluator": {"enabled": True, "script": "evaluator.py"},
            },
        }

    def fixture(self, expected="Zephyrine Holdings"):
        return {
            "checks": [
                {"name": "Account", "field": "account_name", "op": "equals", "expected": expected}
            ]
        }

    def test_save_writes_task_and_fixture_separately(self) -> None:
        saved = viz.save_benchmark_task("lookup_001.yaml", self.task(), self.fixture())
        task_text = (self.tasks / "lookup_001.yaml").read_text(encoding="utf-8")
        fixture_text = (self.fixtures / "lookup_001.yaml").read_text(encoding="utf-8")
        self.assertNotIn("Zephyrine", task_text)
        self.assertIn("fixture: ../fixtures/lookup_001.yaml", task_text)
        self.assertIn("Zephyrine Holdings", fixture_text)
        self.assertIn("test_id: lookup_001", fixture_text)
        self.assertEqual(saved["fixture"]["path"], "benchmarks/fixtures/lookup_001.yaml")
        self.assertEqual(saved["fixture"]["data"]["checks"][0]["expected"], "Zephyrine Holdings")

    def test_listing_marks_benchmark_tasks(self) -> None:
        viz.save_benchmark_task("lookup_001.yaml", self.task(), self.fixture())
        items = viz.list_tests()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "benchmarks")
        self.assertEqual(items[0]["path"], "benchmarks/tasks/lookup_001.yaml")
        self.assertEqual(items[0]["check_count"], 1)

    def test_save_rejects_answer_in_success_criteria(self) -> None:
        data = self.task(success_criteria="Mention Zephyrine Holdings.")
        with self.assertRaisesRegex(ValueError, "fixture values"):
            viz.save_benchmark_task("lookup_001.yaml", data, self.fixture())
        self.assertFalse((self.tasks / "lookup_001.yaml").exists())
        self.assertFalse((self.fixtures / "lookup_001.yaml").exists())

    def test_save_rejects_inline_checks(self) -> None:
        data = self.task()
        data["pipeline"]["evaluator"]["checks"] = [{"field": "x", "expected": 1}]
        with self.assertRaisesRegex(ValueError, "must not contain expected values"):
            viz.save_benchmark_task("lookup_001.yaml", data, self.fixture())

    def test_save_rejects_fixture_outside_fixtures_dir(self) -> None:
        data = self.task(fixture="../../tests/lookup_001.yaml")
        with self.assertRaisesRegex(ValueError, "benchmarks/fixtures"):
            viz.save_benchmark_task("lookup_001.yaml", data, self.fixture())

    def test_save_requires_fixture_checks(self) -> None:
        with self.assertRaisesRegex(ValueError, "checks must be a non-empty list"):
            viz.save_benchmark_task("lookup_001.yaml", self.task(), {"checks": []})


if __name__ == "__main__":
    unittest.main()
