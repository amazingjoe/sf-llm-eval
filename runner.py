from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

from common import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_TOOL_CALLS,
    add_usage,
    assistant_message,
    client_tool_calls,
    discover_config_file,
    extract_assistant_text,
    extract_search_annotations,
    finish_reason,
    has_server_tools,
    iter_suite_tests,
    load_config_file,
    load_suite_file,
    load_test_file,
    read_json,
    redact_secrets,
    tool_call_name,
    tool_kind,
    translate_tools,
    usage_metrics,
    write_json,
)
from salesforce import SalesforceCliError, SalesforceCliProvider, bootstrap_salesforce_session
from tool_runtime import (
    ToolContext,
    advertised_client_tool_names,
    ensure_client_tools_supported,
    execute_client_tool,
    test_needs_salesforce,
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def initial_messages(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    request_cfg = cfg["request"]
    test_cfg = cfg["test"]
    messages: list[dict[str, Any]] = []
    if request_cfg.get("system_prompt"):
        messages.append({"role": "system", "content": request_cfg["system_prompt"]})
    for message in request_cfg.get("messages", []):
        messages.append(message)
    messages.append({"role": "user", "content": test_cfg["prompt"]})
    return messages


def build_payload(
    cfg: dict[str, Any],
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request_cfg = cfg["request"]
    payload = {
        "model": request_cfg["model"],
        "messages": messages if messages is not None else initial_messages(cfg),
    }

    payload.update(request_cfg.get("parameters", {}))

    tools = translate_tools(request_cfg.get("tools") or [])
    if tools:
        payload["tools"] = tools

    if request_cfg.get("tool_choice") is not None:
        payload["tool_choice"] = request_cfg["tool_choice"]

    # Escape hatch for provider-specific fields.
    payload.update(request_cfg.get("extra_payload", {}))

    if has_server_tools(payload.get("tools") or []) and "max_tool_calls" not in payload:
        payload["max_tool_calls"] = int(
            request_cfg.get("max_tool_calls") or DEFAULT_MAX_TOOL_CALLS
        )
    return payload


def post_chat(
    *,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
    round_dir: Path,
) -> tuple[dict[str, Any], float, int]:
    write_json(round_dir / "request.json", payload)
    start = time.perf_counter()
    response = requests.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=timeout_seconds,
    )
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    (round_dir / "response.raw.txt").write_text(response.text, encoding="utf-8")
    write_json(
        round_dir / "response_meta.json",
        {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "latency_ms": latency_ms,
        },
    )
    try:
        response_json = response.json()
    except ValueError:
        raise RuntimeError(
            f"Provider returned non-JSON response (HTTP {response.status_code}). "
            f"See {round_dir / 'response.raw.txt'}"
        )
    write_json(round_dir / "response.json", response_json)
    return response_json, latency_ms, response.status_code


def run_inference_loop(
    *,
    cfg: dict[str, Any],
    endpoint: str,
    headers: dict[str, str],
    run_dir: Path,
    tool_context: ToolContext | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    request_cfg = cfg["request"]
    timeout_seconds = request_cfg.get("timeout_seconds", 120)
    max_rounds = int(request_cfg.get("max_rounds") or DEFAULT_MAX_ROUNDS)
    if max_rounds < 1:
        raise ValueError("request.max_rounds must be >= 1")

    messages = initial_messages(cfg)
    first_payload: dict[str, Any] | None = None
    last_response: dict[str, Any] | None = None
    last_latency = 0.0
    totals: dict[str, Any] = {}
    trace_rounds: list[dict[str, Any]] = []
    tools = translate_tools(request_cfg.get("tools") or [])
    ensure_client_tools_supported(tools)
    client_tool_executions = 0

    for round_index in range(max_rounds):
        round_dir = run_dir / "rounds" / f"{round_index:02d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        payload = build_payload(cfg, messages)
        if first_payload is None:
            first_payload = payload
            write_json(run_dir / "request.json", payload)

        print(f"Inference round {round_index + 1}/{max_rounds}")
        response_json, latency_ms, status_code = post_chat(
            endpoint=endpoint,
            headers=headers,
            payload=payload,
            timeout_seconds=timeout_seconds,
            round_dir=round_dir,
        )
        last_response = response_json
        last_latency += latency_ms
        write_json(run_dir / "response.json", response_json)
        (run_dir / "response.raw.txt").write_text(
            (round_dir / "response.raw.txt").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        write_json(run_dir / "response_meta.json", {
            "status_code": status_code,
            "latency_ms": latency_ms,
            "round_index": round_index,
        })
        piece = usage_metrics(response_json)
        totals = add_usage(totals, piece)
        annotations = extract_search_annotations(response_json)
        if annotations:
            write_json(round_dir / "annotations.json", annotations)

        if status_code != 200:
            raise RuntimeError(
                f"Provider returned HTTP {status_code}. "
                f"See {round_dir / 'response.json'}"
            )

        message = assistant_message(response_json)
        pending = client_tool_calls(message)
        reason = finish_reason(response_json)
        round_trace: dict[str, Any] = {
            "index": round_index,
            "latency_ms": latency_ms,
            "finish_reason": reason,
            "usage": piece,
            "annotation_count": len(annotations),
            "client_tool_calls": [tool_call_name(call) for call in pending],
            "client_tool_results": [],
            "web_search_requests": (
                ((piece.get("raw_usage") or {}).get("server_tool_use") or {}).get(
                    "web_search_requests"
                )
                if isinstance(piece.get("raw_usage"), dict)
                else None
            ),
        }
        trace_rounds.append(round_trace)

        if not pending:
            break

        if round_index + 1 >= max_rounds:
            names = ", ".join(tool_call_name(call) for call in pending)
            raise RuntimeError(
                f"Hit request.max_rounds={max_rounds} with pending client "
                f"tool call(s): {names}"
            )

        messages.append(message)
        tool_messages: list[dict[str, Any]] = []
        executed: list[dict[str, Any]] = []
        for call in pending:
            name = tool_call_name(call)
            result_message, call_trace = execute_client_tool(call, tool_context)
            cache_hit = call_trace.get("cache_hit")
            cache_status = (
                " (cache: hit)"
                if cache_hit is True
                else " (cache: miss)"
                if cache_hit is False
                else ""
            )
            print(f"  client tool: {name}{cache_status}")
            tool_messages.append(result_message)
            executed.append(call_trace)
            client_tool_executions += 1
        messages.extend(tool_messages)
        round_trace["client_tool_results"] = executed
        write_json(round_dir / "tool_results.json", redact_secrets(executed))
        write_json(
            round_dir / "tool_messages.json",
            redact_secrets(tool_messages),
        )
    else:
        raise RuntimeError(
            f"Hit request.max_rounds={max_rounds} without a final assistant answer."
        )

    assert first_payload is not None and last_response is not None
    write_json(
        run_dir / "tool_trace.json",
        {
            "max_rounds": max_rounds,
            "rounds_used": len(trace_rounds),
            "server_tools": [
                tool.get("type") for tool in tools if tool_kind(tool) == "server"
            ],
            "client_tools": advertised_client_tool_names(tools),
            "client_tool_executions": client_tool_executions,
            "rounds": trace_rounds,
        },
    )
    metrics = {
        "latency_ms": round(last_latency, 2),
        "rounds": len(trace_rounds),
        "client_tool_executions": client_tool_executions,
        **totals,
    }
    return last_response, metrics, first_payload


def run_subcommand(
    script: str,
    test_file: Path,
    run_dir: Path,
    suite_file: Path | None = None,
    config_file: Path | None = None,
) -> int:
    script_path = Path(script)
    if not script_path.is_absolute():
        # Resolve scripts relative to the project/current working directory.
        script_path = Path.cwd() / script_path

    cmd = [
        sys.executable,
        str(script_path),
        "--test",
        str(test_file),
        "--run-dir",
        str(run_dir),
    ]
    if suite_file is not None:
        cmd.extend(["--suite", str(suite_file)])
    if config_file is not None:
        cmd.extend(["--config", str(config_file)])
    print(">", " ".join(cmd))
    completed = subprocess.run(cmd)
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a benchmark test-set: every test inherits the suite model "
            "and provider, then optionally runs its normalizer/evaluator."
        )
    )
    parser.add_argument(
        "--test",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--suite",
        help="Path to the YAML test-set file under test-sets/.",
    )
    parser.add_argument(
        "--config",
        help="Path to config.yaml (normalizer/evaluator models). Discovered automatically if omitted.",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Run only this category id. Repeatable.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Run only this test id. Repeatable.",
    )
    parser.add_argument(
        "--no-pipeline",
        action="store_true",
        help="Only run model inference; do not invoke normalizer/evaluator.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip Salesforce org confirmation when the configured alias is "
            "already connected."
        ),
    )
    parser.add_argument(
        "--run-dir",
        help="Optional explicit suite run directory. Otherwise one is created automatically.",
    )
    return parser.parse_args()


def write_yaml(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def run_one_test(
    *,
    test_file: Path,
    suite_file: Path,
    config_file: Path,
    raw_cfg: dict[str, Any],
    cfg: dict[str, Any],
    run_dir: Path,
    no_pipeline: bool,
    category_id: str,
    category_name: str,
    category_emoji: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    test_id = cfg["test"]["id"]
    request_cfg = cfg["request"]
    suite_meta = cfg.get("_suite") or {}
    pipeline_meta = cfg.get("_pipeline") or {}

    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "test.yaml").write_text(
        test_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    write_yaml(run_dir / "merged_test.yaml", raw_cfg)
    write_yaml(run_dir / "resolved_test_redacted.yaml", redact_secrets(cfg))
    write_json(
        run_dir / "source.json",
        {
            "suite_file": str(suite_file),
            "suite_id": suite_meta.get("id"),
            "suite_name": suite_meta.get("name"),
            "config_file": str(config_file),
            "test_file": str(test_file),
            "test_id": test_id,
            "category_id": category_id,
            "category_name": category_name,
            "category_emoji": category_emoji,
            "model": request_cfg.get("model"),
            "normalizer_model": pipeline_meta.get("normalizer_model"),
            "evaluator_mode": pipeline_meta.get("evaluator_mode"),
            "evaluator_model": pipeline_meta.get("evaluator_model"),
        },
    )

    endpoint = request_cfg["endpoint"]
    api_key = request_cfg["api_key"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **request_cfg.get("headers", {}),
    }
    write_json(
        run_dir / "request_meta.json",
        {
            "endpoint": endpoint,
            "headers": redact_secrets(headers),
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )

    print(f"Running test: {test_id}")
    print(f"Category: {category_id}")
    print(f"Model: {request_cfg['model']}")
    print(f"Run directory: {run_dir}")

    response_json, inference_metrics, _payload = run_inference_loop(
        cfg=cfg,
        endpoint=endpoint,
        headers=headers,
        run_dir=run_dir,
        tool_context=tool_context,
    )

    answer = extract_assistant_text(response_json)
    (run_dir / "answer.txt").write_text(answer, encoding="utf-8")

    latency_ms = inference_metrics.get("latency_ms")
    metrics = {
        "suite_id": suite_meta.get("id"),
        "test_id": test_id,
        "category_id": category_id,
        "model": request_cfg["model"],
        **inference_metrics,
    }
    write_json(run_dir / "metrics.json", metrics)

    print("Inference complete.")
    print(
        f"Tokens: {metrics.get('total_tokens')} | "
        f"Latency: {latency_ms} ms | "
        f"Cost: {metrics.get('cost_usd')}"
        + (
            f" | Searches: {metrics.get('web_search_requests')}"
            if metrics.get("web_search_requests")
            else ""
        )
        + (
            f" | Rounds: {metrics.get('rounds')}"
            if metrics.get("rounds")
            else ""
        )
        + (
            f" | Client tools: {metrics.get('client_tool_executions')}"
            if metrics.get("client_tool_executions")
            else ""
        )
    )

    result: dict[str, Any] = {
        "test_id": test_id,
        "category_id": category_id,
        "run_dir": str(run_dir),
        "model": request_cfg["model"],
        "latency_ms": latency_ms,
        "status": "completed",
        "score": None,
        "error": None,
    }

    if no_pipeline:
        return result

    pipeline = cfg.get("pipeline", {})

    normalizer = pipeline.get("normalizer", {})
    if normalizer.get("enabled", True) and normalizer.get("script"):
        code = run_subcommand(
            normalizer["script"],
            test_file,
            run_dir,
            suite_file=suite_file,
            config_file=config_file,
        )
        if code != 0:
            result["status"] = "error"
            result["error"] = f"normalizer.py exited with code {code}"
            return result

    evaluator = pipeline.get("evaluator", {})
    if evaluator.get("enabled", True) and evaluator.get("script"):
        code = run_subcommand(
            evaluator["script"],
            test_file,
            run_dir,
            suite_file=suite_file,
            config_file=config_file,
        )
        evaluation_path = run_dir / "evaluation.json"
        if evaluation_path.exists():
            try:
                evaluation = read_json(evaluation_path)
            except Exception:
                evaluation = None
            if isinstance(evaluation, dict):
                result["score"] = evaluation.get("score")
                if evaluation.get("passed") is True:
                    result["status"] = "pass"
                elif evaluation.get("passed") is False:
                    result["status"] = "fail"
        if code == 2:
            result["status"] = "fail"
        elif code != 0:
            result["status"] = "error"
            result["error"] = f"evaluator.py exited with code {code}"
        elif result["status"] == "completed":
            result["status"] = "pass"

    return result


def select_entries(
    entries: list[dict[str, Any]],
    suite_file: Path,
    config_file: Path,
    categories: list[str],
    only_ids: list[str],
) -> list[dict[str, Any]]:
    selected = entries
    if categories:
        wanted = set(categories)
        selected = [entry for entry in selected if entry["category_id"] in wanted]
        missing = wanted - {entry["category_id"] for entry in entries}
        if missing:
            raise SystemExit(
                f"Unknown category id(s) in {suite_file}: {', '.join(sorted(missing))}"
            )

    loaded: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in selected:
        raw_cfg, cfg = load_test_file(
            entry["test_path"],
            suite_path=suite_file,
            config_path=config_file,
        )
        test_id = str(cfg["test"]["id"])
        if test_id in seen_ids:
            raise SystemExit(
                f"Duplicate test id '{test_id}' in suite {suite_file}"
            )
        seen_ids.add(test_id)
        try:
            ensure_client_tools_supported(
                translate_tools((cfg.get("request") or {}).get("tools") or [])
            )
        except ValueError as exc:
            raise SystemExit(f"{entry['test_path']}: {exc}") from exc
        loaded.append({**entry, "test_id": test_id, "raw_cfg": raw_cfg, "cfg": cfg})

    if only_ids:
        wanted_ids = set(only_ids)
        loaded = [entry for entry in loaded if entry["test_id"] in wanted_ids]
        missing_ids = wanted_ids - {entry["test_id"] for entry in loaded}
        if missing_ids:
            raise SystemExit(
                f"Test id(s) not found in the selected suite/categories: "
                f"{', '.join(sorted(missing_ids))}"
            )

    if not loaded:
        raise SystemExit("No tests matched the given --suite/--category/--only filters.")
    return loaded


def main() -> None:
    args = parse_args()
    if not args.suite:
        raise SystemExit(
            "A test-set is required. Use --suite test-sets/<file>.yaml "
            "(optionally --only <test_id> or --category <id>). "
            "Individual tests no longer run on their own."
        )
    suite_file = Path(args.suite).resolve()
    if not suite_file.exists():
        raise SystemExit(f"Test-set not found: {suite_file}")

    config_file = (
        Path(args.config).resolve()
        if args.config
        else discover_config_file(suite_file)
    )
    if config_file is None or not config_file.exists():
        raise SystemExit(
            "Missing config.yaml. Create one at the project root with normalizer "
            "and evaluator models, or pass --config."
        )

    try:
        _, config_cfg = load_config_file(config_file)
        raw_suite, suite_cfg = load_suite_file(suite_file)
        entries = iter_suite_tests(raw_suite, suite_file)
        selected = select_entries(
            entries, suite_file, config_file, args.category, args.only
        )
    except (ValueError, FileNotFoundError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc

    suite_meta = suite_cfg.get("suite") or {}
    suite_id = suite_meta["id"]
    model = suite_cfg["provider"]["model"]
    output_root = Path(
        (suite_cfg.get("logging") or {}).get("output_dir")
        or (config_cfg.get("logging") or {}).get("output_dir")
        or "runs"
    )
    suite_run_dir = (
        Path(args.run_dir).resolve()
        if args.run_dir
        else (output_root / suite_id / utc_stamp()).resolve()
    )
    suite_run_dir.mkdir(parents=True, exist_ok=True)

    (suite_run_dir / "suite.yaml").write_text(
        suite_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (suite_run_dir / "config.yaml").write_text(
        config_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    write_yaml(suite_run_dir / "resolved_suite_redacted.yaml", redact_secrets(suite_cfg))
    write_yaml(suite_run_dir / "resolved_config_redacted.yaml", redact_secrets(config_cfg))

    print(f"Suite: {suite_id} ({suite_meta.get('name') or suite_id})")
    if suite_meta.get("description"):
        print(suite_meta["description"].strip())
    print(f"Subject model: {model}")
    print(f"Normalizer model: {(config_cfg.get('normalizer') or {}).get('model')}")
    evaluator_cfg = config_cfg.get("evaluator") or {}
    print(
        f"Evaluator: {evaluator_cfg.get('mode') or 'deterministic'}"
        + (
            f" ({evaluator_cfg.get('model')})"
            if evaluator_cfg.get("model")
            else ""
        )
    )
    print(f"Tests: {len(selected)}")
    print(f"Suite run directory: {suite_run_dir}")

    tool_context = ToolContext()
    if any(test_needs_salesforce(entry["cfg"]) for entry in selected):
        try:
            session = bootstrap_salesforce_session(
                config_cfg=config_cfg,
                suite_cfg=suite_cfg,
                run_dir=suite_run_dir,
                assume_yes=args.yes,
            )
        except SalesforceCliError as exc:
            raise SystemExit(str(exc)) from exc
        tool_context.salesforce = SalesforceCliProvider(session)
        print(
            f"Salesforce org: {session.org_id} ({session.username}) "
            f"via alias {session.alias}"
        )

    started_at = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, Any]] = []

    for index, entry in enumerate(selected, start=1):
        test_id = entry["test_id"]
        category_id = entry["category_id"]
        test_run_dir = suite_run_dir / category_id / test_id
        print()
        print(f"=== [{index}/{len(selected)}] {category_id} / {test_id} ===")
        try:
            result = run_one_test(
                test_file=entry["test_path"],
                suite_file=suite_file,
                config_file=config_file,
                raw_cfg=entry["raw_cfg"],
                cfg=entry["cfg"],
                run_dir=test_run_dir,
                no_pipeline=args.no_pipeline,
                category_id=category_id,
                category_name=entry["category_name"],
                category_emoji=entry.get("category_emoji"),
                tool_context=tool_context,
            )
        except Exception as exc:
            test_run_dir.mkdir(parents=True, exist_ok=True)
            (test_run_dir / "error.txt").write_text(
                "".join(traceback.format_exception(exc)), encoding="utf-8"
            )
            result = {
                "test_id": test_id,
                "category_id": category_id,
                "run_dir": str(test_run_dir),
                "model": model,
                "latency_ms": None,
                "status": "error",
                "score": None,
                "error": str(exc),
            }
            print(f"ERROR: {exc}")
        results.append(result)

    passed = sum(1 for item in results if item["status"] == "pass")
    failed = sum(1 for item in results if item["status"] == "fail")
    errors = sum(1 for item in results if item["status"] == "error")
    completed = sum(1 for item in results if item["status"] == "completed")

    summary = {
        "suite_id": suite_id,
        "suite_name": suite_meta.get("name"),
        "description": suite_meta.get("description"),
        "model": model,
        "normalizer_model": (config_cfg.get("normalizer") or {}).get("model"),
        "evaluator_mode": (config_cfg.get("evaluator") or {}).get("mode"),
        "evaluator_model": (config_cfg.get("evaluator") or {}).get("model"),
        "suite_file": str(suite_file),
        "config_file": str(config_file),
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "no_pipeline": args.no_pipeline,
        "tests": results,
        "counts": {
            "total": len(results),
            "pass": passed,
            "fail": failed,
            "error": errors,
            "completed": completed,
        },
    }
    write_json(suite_run_dir / "summary.json", summary)

    print()
    print(
        f"Suite summary: {passed} passed, {failed} failed, "
        f"{errors} errors, {completed} completed without evaluation"
    )
    print(f"Done. Artifacts are in: {suite_run_dir}")

    if errors:
        raise SystemExit(1)
    if failed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
