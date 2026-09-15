from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import load_yaml_object, redact_secrets, translate_tool, translate_tools
from salesforce import (
    SalesforceCliError,
    SalesforceCliProvider,
    SalesforceSession,
    bootstrap_salesforce_session,
    parse_sf_json,
    public_org_identity,
    run_sf,
    slim_describe,
)
from tool_runtime import ToolContext, execute_client_tool, test_needs_salesforce


DISPLAY_RESULT = {
    "status": 0,
    "result": {
        "id": "00Dxx0000000001",
        "alias": "harness-org",
        "username": "joe@example.com",
        "instanceUrl": "https://example.my.salesforce.com",
        "connectedStatus": "Connected",
        "isSandbox": True,
        "orgName": "Harness Sandbox",
        "accessToken": "SECRET_TOKEN",
        "sfdxAuthUrl": "force://secret",
    },
}

QUERY_RESULT = {
    "status": 0,
    "result": {
        "totalSize": 1,
        "done": True,
        "records": [
            {
                "attributes": {"type": "Account", "url": "/services/data/v60.0/sobjects/Account/001"},
                "Name": "Acme Incorporated",
                "NumberOfEmployees": 350,
                "Industry": "Manufacturing",
            }
        ],
    },
}

DESCRIBE_RESULT = {
    "status": 0,
    "result": {
        "name": "Account",
        "label": "Account",
        "keyPrefix": "001",
        "custom": False,
        "queryable": True,
        "fields": [
            {
                "name": "Name",
                "label": "Account Name",
                "type": "string",
                "nillable": False,
                "custom": False,
                "length": 255,
                "picklistValues": [],
            },
            {
                "name": "Industry",
                "label": "Industry",
                "type": "picklist",
                "nillable": True,
                "picklistValues": [
                    {"value": "Manufacturing", "active": True},
                    {"value": "Other", "active": False},
                ],
            },
        ],
    },
}


def _session() -> SalesforceSession:
    return SalesforceSession(
        alias="harness-org",
        sf_bin="/usr/bin/sf",
        org_id="00Dxx0000000001",
        instance_url="https://example.my.salesforce.com",
        username="joe@example.com",
        connected_status="Connected",
        is_sandbox=True,
        max_records=2,
    )


class TranslateToolTests(unittest.TestCase):
    def test_web_search_still_server(self) -> None:
        tool = translate_tool("web_search")
        self.assertEqual(tool["type"], "openrouter:web_search")

    def test_salesforce_query_shorthand(self) -> None:
        tool = translate_tool("salesforce.query")
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], "salesforce_query")

    def test_salesforce_describe_and_org_info(self) -> None:
        names = {
            translate_tool(key)["function"]["name"]
            for key in ("salesforce.describe", "salesforce.org_info", "salesforce_query")
        }
        self.assertEqual(
            names,
            {"salesforce_describe", "salesforce_org_info", "salesforce_query"},
        )

    def test_write_tools_are_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            translate_tool("salesforce.apex")
        self.assertIn("not implemented", str(ctx.exception).lower())

    def test_metadata_read_rejected(self) -> None:
        with self.assertRaises(ValueError):
            translate_tool("salesforce.metadata_read")

    def test_unknown_shorthand(self) -> None:
        with self.assertRaises(ValueError):
            translate_tool("not_a_real_tool")

    def test_dict_type_shorthand(self) -> None:
        tool = translate_tool({"type": "salesforce.query"})
        self.assertEqual(tool["function"]["name"], "salesforce_query")


class SuiteCategoryTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_record_operations_category_lists_discovery_test(self) -> None:
        suite = load_yaml_object(self.root / "test-sets" / "harness_smoke.yaml")
        category = next(
            item
            for item in suite["categories"]
            if item["id"] == "salesforce_record_operations"
        )
        self.assertEqual(category["name"], "Salesforce record operations")
        self.assertIn("tests/acme_lookup.yaml", category["tests"])
        self.assertIn("tests/acme_object_discovery.yaml", category["tests"])
        self.assertFalse(
            any(item["id"] == "salesforce_account_lookup" for item in suite["categories"])
        )

    def test_object_discovery_advertises_describe_and_query(self) -> None:
        raw = load_yaml_object(self.root / "tests" / "acme_object_discovery.yaml")
        tools = translate_tools(raw["request"]["tools"])
        names = [tool["function"]["name"] for tool in tools]
        self.assertEqual(names, ["salesforce_describe", "salesforce_query"])
        self.assertTrue(test_needs_salesforce(raw))


class RedactionTests(unittest.TestCase):
    def test_public_org_identity_drops_tokens(self) -> None:
        public = public_org_identity(DISPLAY_RESULT["result"])
        self.assertEqual(public["id"], "00Dxx0000000001")
        self.assertNotIn("accessToken", public)
        self.assertNotIn("sfdxAuthUrl", public)

    def test_redact_secrets_catches_token_keys(self) -> None:
        redacted = redact_secrets(DISPLAY_RESULT)
        self.assertEqual(redacted["result"]["accessToken"], "***REDACTED***")


class ParseAndSlimTests(unittest.TestCase):
    def test_parse_json_with_leading_noise(self) -> None:
        raw = "Warning: blah\n" + json.dumps({"status": 0, "result": {"ok": True}})
        self.assertEqual(parse_sf_json(raw)["result"]["ok"], True)

    def test_slim_describe_picklists(self) -> None:
        slim = slim_describe(DESCRIBE_RESULT["result"])
        self.assertEqual(slim["name"], "Account")
        self.assertEqual(slim["field_count"], 2)
        industry = slim["fields"][1]
        self.assertEqual(industry["picklistValues"], ["Manufacturing"])


class ProviderTests(unittest.TestCase):
    def test_query_strips_attributes_and_injects_target_org(self) -> None:
        provider = SalesforceCliProvider(_session())
        with patch("salesforce.run_sf") as mocked:
            mocked.return_value = type(
                "Cli",
                (),
                {
                    "argv": ["sf"],
                    "returncode": 0,
                    "stdout": json.dumps(QUERY_RESULT),
                    "stderr": "",
                    "data": QUERY_RESULT,
                    "latency_ms": 1,
                    "parse_error": None,
                },
            )()
            out = provider.query("SELECT Name FROM Account")
        self.assertEqual(out["records"][0]["Name"], "Acme Incorporated")
        self.assertNotIn("attributes", out["records"][0])
        argv = mocked.call_args[0][0]
        self.assertIn("--target-org", argv)
        self.assertEqual(argv[argv.index("--target-org") + 1], "harness-org")
        self.assertIn("--json", argv)
        self.assertNotIn("SECRET_TOKEN", json.dumps(out))

    def test_query_rejects_non_select(self) -> None:
        provider = SalesforceCliProvider(_session())
        self.assertIn("error", provider.query("UPDATE Account SET Name='x'"))
        self.assertIn("error", provider.query("SELECT Id FROM Account; DROP"))

    def test_query_rejects_org_flags_in_handler(self) -> None:
        context = ToolContext(salesforce=SalesforceCliProvider(_session()))
        message, trace = execute_client_tool(
            {
                "id": "call_1",
                "function": {
                    "name": "salesforce_query",
                    "arguments": json.dumps(
                        {"query": "SELECT Id FROM Account", "target_org": "prod"}
                    ),
                },
            },
            context,
        )
        payload = json.loads(message["content"])
        self.assertIn("error", payload)
        self.assertFalse(trace["ok"])

    def test_describe_via_handler(self) -> None:
        provider = SalesforceCliProvider(_session())
        with patch.object(provider, "run") as mocked:
            mocked.return_value = type(
                "Cli",
                (),
                {
                    "argv": ["sf"],
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                    "data": DESCRIBE_RESULT,
                    "latency_ms": 1,
                    "parse_error": None,
                },
            )()
            context = ToolContext(salesforce=provider)
            message, trace = execute_client_tool(
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "salesforce_describe",
                        "arguments": '{"sobject": "Account"}',
                    },
                },
                context,
            )
        payload = json.loads(message["content"])
        self.assertEqual(payload["name"], "Account")
        self.assertTrue(trace["ok"])
        self.assertEqual(payload["fields"][1]["picklistValues"], ["Manufacturing"])

    def test_unknown_tool_returns_error_not_exception(self) -> None:
        message, trace = execute_client_tool(
            {
                "id": "call_x",
                "function": {"name": "not_registered", "arguments": "{}"},
            },
            ToolContext(),
        )
        self.assertIn("Unknown tool", json.loads(message["content"])["error"])
        self.assertFalse(trace["ok"])

    def test_run_sf_does_not_use_shell(self) -> None:
        with patch("salesforce.subprocess.run") as mocked:
            mocked.return_value = type(
                "P",
                (),
                {"returncode": 0, "stdout": '{"status": 0}', "stderr": ""},
            )()
            run_sf(
                ["org", "display", "--json"],
                sf_bin="/usr/bin/sf",
                timeout_seconds=5,
                max_output_bytes=10_000,
            )
            kwargs = mocked.call_args.kwargs
            self.assertFalse(kwargs.get("shell"))
            self.assertEqual(mocked.call_args.args[0][0], "/usr/bin/sf")


class BootstrapTests(unittest.TestCase):
    def test_reuses_connected_alias_without_login(self) -> None:
        identity = public_org_identity(DISPLAY_RESULT["result"])
        login_calls = []

        def display_fn(**kwargs):
            return identity, None

        def login_fn(**kwargs):
            login_calls.append(kwargs)

        def confirm_fn(**kwargs):
            return "use"

        with tempfile.TemporaryDirectory() as tmp:
            session = bootstrap_salesforce_session(
                config_cfg={"salesforce": {"alias": "harness-org"}},
                suite_cfg={},
                run_dir=Path(tmp),
                assume_yes=True,
                sf_bin="/usr/bin/sf",
                display_fn=display_fn,
                login_fn=login_fn,
                confirm_fn=confirm_fn,
            )
            saved = json.loads((Path(tmp) / "salesforce_session.json").read_text())
        self.assertEqual(session.org_id, "00Dxx0000000001")
        self.assertEqual(login_calls, [])
        self.assertNotIn("accessToken", saved)
        self.assertTrue(saved["confirmed"])

    def test_expected_org_mismatch_aborts(self) -> None:
        identity = public_org_identity(DISPLAY_RESULT["result"])

        def display_fn(**kwargs):
            return identity, None

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SalesforceCliError) as ctx:
                bootstrap_salesforce_session(
                    config_cfg={
                        "salesforce": {
                            "alias": "harness-org",
                            "expected_org_id": "00DNOPE",
                        }
                    },
                    suite_cfg={},
                    run_dir=Path(tmp),
                    assume_yes=True,
                    sf_bin="/usr/bin/sf",
                    display_fn=display_fn,
                    login_fn=lambda **kwargs: None,
                    confirm_fn=lambda **kwargs: "use",
                )
        self.assertIn("expected_org_id", str(ctx.exception))


class TestNeedsSalesforceTests(unittest.TestCase):
    def test_detects_query_tool(self) -> None:
        cfg = {"request": {"tools": ["salesforce.query"]}}
        self.assertTrue(test_needs_salesforce(cfg))

    def test_web_search_only(self) -> None:
        cfg = {"request": {"tools": ["web_search"]}}
        self.assertFalse(test_needs_salesforce(cfg))


class InferenceLoopTests(unittest.TestCase):
    def test_executes_client_tool_and_continues(self) -> None:
        from runner import run_inference_loop

        provider = SalesforceCliProvider(_session())
        with patch.object(provider, "query", return_value={"records": [{"Name": "Acme Incorporated"}], "returned": 1, "total_size": 1, "done": True, "truncated": False}):
            responses = [
                (
                    {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "salesforce_query",
                                                "arguments": json.dumps(
                                                    {
                                                        "query": (
                                                            "SELECT Name, NumberOfEmployees, "
                                                            "Industry FROM Account "
                                                            "WHERE Name = 'Acme Incorporated'"
                                                        )
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    },
                    12.0,
                    200,
                ),
                (
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "Acme Incorporated, 350 employees, Manufacturing",
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
                    },
                    8.0,
                    200,
                ),
            ]

            def fake_post_chat(**kwargs):
                body, latency_ms, status_code = responses.pop(0)
                round_dir = kwargs["round_dir"]
                round_dir.mkdir(parents=True, exist_ok=True)
                (round_dir / "response.raw.txt").write_text(
                    json.dumps(body), encoding="utf-8"
                )
                return body, latency_ms, status_code

            cfg = {
                "request": {
                    "model": "test/model",
                    "system_prompt": "You are a test.",
                    "messages": [],
                    "parameters": {},
                    "tools": ["salesforce.query"],
                    "max_rounds": 4,
                    "timeout_seconds": 5,
                },
                "test": {"prompt": "Find Acme"},
            }
            with tempfile.TemporaryDirectory() as tmp, patch(
                "runner.post_chat", side_effect=fake_post_chat
            ):
                run_dir = Path(tmp)
                response, metrics, _payload = run_inference_loop(
                    cfg=cfg,
                    endpoint="https://example.invalid",
                    headers={"Authorization": "Bearer x"},
                    run_dir=run_dir,
                    tool_context=ToolContext(salesforce=provider),
                )
                trace = json.loads((run_dir / "tool_trace.json").read_text())
                tool_results = json.loads(
                    (run_dir / "rounds" / "00" / "tool_results.json").read_text()
                )

        self.assertIn("Acme Incorporated", response["choices"][0]["message"]["content"])
        self.assertEqual(metrics["rounds"], 2)
        self.assertEqual(metrics["client_tool_executions"], 1)
        self.assertEqual(trace["client_tools"], ["salesforce_query"])
        self.assertTrue(tool_results[0]["ok"])


if __name__ == "__main__":
    unittest.main()
