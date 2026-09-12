from __future__ import annotations

import json
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
