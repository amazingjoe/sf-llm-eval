#!/usr/bin/env python3
"""Serve the Salesforce open-model workbench: runs, config, tests, and test-sets."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DEFAULT_RUNS = REPO_ROOT / "runs"
TESTS_DIR = REPO_ROOT / "tests"
SUITES_DIR = REPO_ROOT / "test-sets"
CONFIG_PATH = REPO_ROOT / "config.yaml"
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$")
MAX_BODY_BYTES = 2_000_000
FOLD_KEYS = {"prompt", "description", "notes", "instructions", "system_prompt"}

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import yaml
except ImportError:  # pragma: no cover - optional for display names
    yaml = None

try:
    from common import (
        ensure_test_does_not_define_owned_provider_fields,
        validate_config,
        validate_suite,
    )
except ImportError:
    validate_config = None
    validate_suite = None
    ensure_test_does_not_define_owned_provider_fields = None


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def safe_segment(value: str) -> str:
    cleaned = unquote(value).strip()
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"invalid path segment: {value!r}")
    return cleaned


def load_yaml(path: Path) -> Any | None:
    text = read_text(path)
    if text is None:
        return None
    if yaml is not None:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    return None


def suite_category_index(suite_yaml: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(suite_yaml, dict):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for category in suite_yaml.get("categories") or []:
        if not isinstance(category, dict) or not category.get("id"):
            continue
        index[str(category["id"])] = {
            "name": category.get("name"),
            "emoji": str(category.get("emoji") or "").strip() or None,
        }
    return index


def library_category_emoji(suite_id: str, category_id: str) -> str | None:
    if not suite_id or not category_id:
        return None
    for path in yaml_files(SUITES_DIR):
        loaded = load_yaml(path) or {}
        suite = loaded.get("suite") if isinstance(loaded.get("suite"), dict) else {}
        if str(suite.get("id") or "") != suite_id and path.stem != suite_id:
            continue
        meta = suite_category_index(loaded).get(category_id) or {}
        return meta.get("emoji")
    return None


def test_name_from_yaml(path: Path) -> str | None:
    loaded = load_yaml(path)
    if isinstance(loaded, dict):
        test = loaded.get("test")
        if isinstance(test, dict) and test.get("name"):
            return str(test["name"])
    text = read_text(path) or ""
    match = re.search(r"(?m)^\s*name:\s*(.+)$", text)
    if not match:
        return None
    name = match.group(1).strip().strip("\"'")
    return name or None


def test_meta_from_yaml(path: Path) -> dict[str, Any]:
    loaded = load_yaml(path)
    out: dict[str, Any] = {}
    if not isinstance(loaded, dict):
        name = test_name_from_yaml(path)
        if name:
            out["name"] = name
        return out
    test = loaded.get("test") if isinstance(loaded.get("test"), dict) else {}
    request = loaded.get("request") if isinstance(loaded.get("request"), dict) else {}
    pipeline = loaded.get("pipeline") if isinstance(loaded.get("pipeline"), dict) else {}
    metadata = test.get("metadata") if isinstance(test.get("metadata"), dict) else {}
    if test.get("id"):
        out["test_id"] = test.get("id")
    if test.get("name"):
        out["name"] = test.get("name")
    if test.get("prompt"):
        out["prompt"] = str(test.get("prompt")).strip()
    if metadata:
        out["metadata"] = metadata
    if request.get("system_prompt"):
        out["system_prompt"] = str(request.get("system_prompt")).strip()
    if request.get("parameters"):
        out["parameters"] = request.get("parameters")
    evaluator = pipeline.get("evaluator") if isinstance(pipeline.get("evaluator"), dict) else {}
    if evaluator.get("checks"):
        out["expected_checks"] = evaluator.get("checks")
    return out


def summarize_checks(evaluation: Any) -> list[dict[str, Any]]:
    if not isinstance(evaluation, dict):
        return []
    checks = evaluation.get("checks") or []
    summary = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        summary.append(
            {
                "name": check.get("name"),
                "field": check.get("field"),
                "passed": check.get("passed"),
            }
        )
    return summary


def extract_response_view(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    return {
        "id": data.get("id"),
        "model": data.get("model"),
        "provider": data.get("provider"),
        "finish_reason": choice.get("finish_reason"),
        "content": message.get("content"),
        "reasoning": message.get("reasoning"),
        "refusal": message.get("refusal"),
    }


def maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def advertised_tools_from_request(request: Any) -> tuple[list[str], list[str]]:
    server: list[str] = []
    client: list[str] = []
    if not isinstance(request, dict):
        return server, client
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        typ = str(tool.get("type") or "")
        if typ.startswith("openrouter:"):
            server.append(typ)
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = function.get("name") or tool.get("name") or typ or "function"
        client.append(str(name))
    return server, client


def display_tool_name(name: str) -> str:
    text = str(name or "").strip()
    if text.startswith("openrouter:"):
        return text.split(":", 1)[-1]
    return text or "tool"


def query_from_mapping(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("query", "search_query", "q"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("action", "web_search", "function"):
        nested = query_from_mapping(data.get(nested_key))
        if nested:
            return nested
    arguments = maybe_json(data.get("arguments"))
    if arguments is not data:
        return query_from_mapping(arguments)
    return None


def extract_search_query(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    choices = response.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = display_tool_name(function.get("name") or call.get("name") or call.get("type") or "")
        if not is_search_tool_name(name) and "search" not in str(call.get("type") or "").lower():
            continue
        found = query_from_mapping(call)
        if found:
            return found
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        if "search" not in typ.lower() and not typ.startswith("openrouter:"):
            continue
        found = query_from_mapping(item)
        if found:
            return found
    return None


def citation_sources(annotations: Any) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    if not isinstance(annotations, list):
        return sources
    for item in annotations:
        if not isinstance(item, dict):
            continue
        cite = item.get("url_citation") if isinstance(item.get("url_citation"), dict) else item
        if not isinstance(cite, dict):
            continue
        url = cite.get("url")
        if not url:
            continue
        source: dict[str, Any] = {"url": url}
        if cite.get("title"):
            source["title"] = cite.get("title")
        if cite.get("content"):
            source["content"] = cite.get("content")
        sources.append(source)
    return sources


def web_search_request_count(response: Any) -> int:
    if not isinstance(response, dict):
        return 0
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    details = usage.get("server_tool_use_details") or usage.get("server_tool_use") or {}
    if not isinstance(details, dict):
        return 0
    value = details.get("web_search_requests")
    return int(value) if isinstance(value, (int, float)) else 0


def is_search_tool_name(name: Any) -> bool:
    text = display_tool_name(str(name or "")).lower()
    return "search" in text


def advertised_search_tool_type(request: Any) -> str:
    server, _client = advertised_tools_from_request(request)
    for item in server:
        if "search" in str(item).lower():
            return str(item)
    return "web_search"


def extract_message_tool_calls(response: Any) -> tuple[list[dict[str, Any]], Any]:
    if not isinstance(response, dict):
        return [], None
    choices = response.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    finish_reason = choice.get("finish_reason")
    calls: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        typ = str(call.get("type") or "function")
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or typ)
        if ":" in typ and ":" not in name:
            name = typ
        arguments = maybe_json(function.get("arguments") or call.get("arguments"))
        if not isinstance(arguments, dict):
            arguments = {}
        query = query_from_mapping(call) or query_from_mapping(arguments)
        if query and "query" not in arguments:
            arguments = {**arguments, "query": query}
        calls.append(
            {
                "id": call.get("id"),
                "name": name,
                "arguments": arguments,
            }
        )
    return calls, finish_reason


def load_round_tools(round_dir: Path) -> dict[str, Any]:
    response = read_json(round_dir / "response.json")
    meta = read_json(round_dir / "response_meta.json")
    annotations = read_json(round_dir / "annotations.json")
    results = read_json(round_dir / "tool_results.json")
    messages = read_json(round_dir / "tool_messages.json")
    calls, finish_reason = extract_message_tool_calls(response)
    result_rows = results if isinstance(results, list) else []
    message_rows = messages if isinstance(messages, list) else []
    by_id: dict[str, Any] = {}
    for row in message_rows:
        if isinstance(row, dict) and row.get("tool_call_id"):
            by_id[str(row["tool_call_id"])] = maybe_json(row.get("content"))

    assembled: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        meta_row = result_rows[index] if index < len(result_rows) and isinstance(result_rows[index], dict) else {}
        payload = by_id.get(str(call.get("id") or ""))
        if payload is None and index < len(message_rows) and isinstance(message_rows[index], dict):
            payload = maybe_json(message_rows[index].get("content"))
        assembled.append(
            {
                "id": call.get("id"),
                "name": call.get("name"),
                "arguments": call.get("arguments"),
                "ok": meta_row.get("ok"),
                "latency_ms": meta_row.get("latency_ms"),
                "error": meta_row.get("error"),
                "returned": meta_row.get("returned"),
                "total_size": meta_row.get("total_size"),
                "truncated": meta_row.get("truncated"),
                "sobject": meta_row.get("sobject"),
                "field_count": meta_row.get("field_count"),
                "result": payload,
            }
        )

    sources = citation_sources(annotations)
    search_requests = web_search_request_count(response)
    search_query = extract_search_query(response)
    search_type = advertised_search_tool_type(read_json(round_dir / "request.json"))
    has_search = any(is_search_tool_name(call.get("name")) for call in assembled)
    if sources or search_requests:
        if has_search:
            for call in assembled:
                if not is_search_tool_name(call.get("name")):
                    continue
                if ":" not in str(call.get("name") or ""):
                    call["name"] = search_type
                if search_query:
                    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                    call["arguments"] = {**args, "query": args.get("query") or search_query}
                if call.get("result") is None and sources:
                    call["result"] = {
                        "sources": sources,
                        "search_requests": search_requests or 1,
                    }
                    call["returned"] = len(sources)
                    call["total_size"] = len(sources)
                    if call.get("ok") is None:
                        call["ok"] = True
        else:
            assembled.append(
                {
                    "id": f"web_search_{round_dir.name}",
                    "name": search_type,
                    "arguments": {"query": search_query} if search_query else {},
                    "ok": True,
                    "latency_ms": None,
                    "error": None,
                    "returned": len(sources),
                    "total_size": len(sources),
                    "truncated": None,
                    "sobject": None,
                    "field_count": None,
                    "result": {
                        "sources": sources,
                        "search_requests": search_requests or (1 if sources else 0),
                    },
                }
            )

    latency = None
    if isinstance(meta, dict) and isinstance(meta.get("latency_ms"), (int, float)):
        latency = meta.get("latency_ms")
    round_id = round_dir.name
    try:
        round_index = int(round_id)
    except ValueError:
        round_index = round_id
    return {
        "index": round_index,
        "id": round_id,
        "finish_reason": finish_reason,
        "latency_ms": latency,
        "calls": assembled,
        "annotations": annotations if isinstance(annotations, list) else [],
    }


def load_test_tools(test_dir: Path, request: Any = None) -> dict[str, Any]:
    trace = read_json(test_dir / "tool_trace.json")
    if not isinstance(trace, dict):
        trace = {}
    server_tools = list(trace.get("server_tools") or [])
    client_tools = list(trace.get("client_tools") or [])
    if not server_tools and not client_tools:
        server_tools, client_tools = advertised_tools_from_request(request)

    rounds: list[dict[str, Any]] = []
    rounds_dir = test_dir / "rounds"
    if rounds_dir.is_dir():
        for round_dir in sorted(path for path in rounds_dir.iterdir() if path.is_dir()):
            rounds.append(load_round_tools(round_dir))

    executions = trace.get("client_tool_executions")
    if executions is None:
        executions = sum(len(round.get("calls") or []) for round in rounds)
    return {
        "server_tools": server_tools,
        "client_tools": client_tools,
        "client_tool_executions": executions,
        "rounds_used": trace.get("rounds_used") or len(rounds),
        "rounds": rounds,
    }


def discover_runs(runs_dir: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if not runs_dir.is_dir():
        return runs
    for suite_dir in sorted(runs_dir.iterdir()):
        if not suite_dir.is_dir():
            continue
        for run_dir in sorted(suite_dir.iterdir(), reverse=True):
            summary_path = run_dir / "summary.json"
            if not summary_path.is_file():
                continue
            summary = read_json(summary_path)
            if not isinstance(summary, dict):
                continue
            runs.append(enrich_run(summary, run_dir))
    runs.sort(key=lambda item: item.get("started_at_utc") or item.get("run_id") or "", reverse=True)
    return runs


def _metric_number(blob: Any, key: str) -> float | None:
    if isinstance(blob, dict) and isinstance(blob.get(key), (int, float)):
        return float(blob[key])
    return None


def _sum_known(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return float(sum(present))


def _int_or_none(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _add_usage_bucket(
    bucket: dict[str, Any], prefix: str, cost: float | None, tokens: float | None
) -> None:
    if cost is not None:
        bucket[f"{prefix}_cost"] = bucket.get(f"{prefix}_cost", 0.0) + cost
        bucket[f"has_{prefix}_cost"] = True
    if tokens is not None:
        bucket[f"{prefix}_tokens"] = bucket.get(f"{prefix}_tokens", 0.0) + tokens
        bucket[f"has_{prefix}_tokens"] = True


def _bucket_pair(bucket: dict[str, Any], prefix: str) -> tuple[float | None, int | None]:
    cost = bucket.get(f"{prefix}_cost") if bucket.get(f"has_{prefix}_cost") else None
    tokens = int(bucket[f"{prefix}_tokens"]) if bucket.get(f"has_{prefix}_tokens") else None
    return cost, tokens


def enrich_run(summary: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    tests_out: list[dict[str, Any]] = []
    buckets: dict[str, Any] = {}
    total_latency = 0.0
    has_latency = False
    category_lookup = suite_category_index(load_yaml(run_dir / "suite.yaml"))
    suite_id = str(summary.get("suite_id") or run_dir.parent.name)

    for item in summary.get("tests") or []:
        if not isinstance(item, dict):
            continue
        test_id = str(item.get("test_id") or "")
        category_id = str(item.get("category_id") or "")
        test_dir = run_dir / category_id / test_id if category_id and test_id else None
        metrics = read_json(test_dir / "metrics.json") if test_dir else None
        normalizer_metrics = read_json(test_dir / "normalizer_metrics.json") if test_dir else None
        evaluator_metrics = read_json(test_dir / "evaluator_metrics.json") if test_dir else None
        evaluation = read_json(test_dir / "evaluation.json") if test_dir else None
        source = read_json(test_dir / "source.json") if test_dir else None
        name = None
        if test_dir:
            yaml_meta = test_meta_from_yaml(test_dir / "test.yaml")
            name = yaml_meta.get("name") or test_name_from_yaml(test_dir / "test.yaml")
        category_name = None
        category_emoji = None
        if isinstance(source, dict):
            category_name = source.get("category_name")
            category_emoji = source.get("category_emoji")
        cat_meta = category_lookup.get(category_id) or {}
        category_name = category_name or cat_meta.get("name")
        category_emoji = (
            str(category_emoji or "").strip()
            or cat_meta.get("emoji")
            or library_category_emoji(suite_id, category_id)
        )
        inference_cost = _metric_number(metrics, "cost_usd")
        inference_tokens = _metric_number(metrics, "total_tokens")
        normalizer_cost = _metric_number(normalizer_metrics, "cost_usd")
        normalizer_tokens = _metric_number(normalizer_metrics, "total_tokens")
        evaluator_cost = _metric_number(evaluator_metrics, "cost_usd")
        evaluator_tokens = _metric_number(evaluator_metrics, "total_tokens")
        pipeline_cost = _sum_known(normalizer_cost, evaluator_cost)
        pipeline_tokens = _sum_known(normalizer_tokens, evaluator_tokens)
        run_cost = _sum_known(inference_cost, pipeline_cost)
        run_tokens = _sum_known(inference_tokens, pipeline_tokens)
        _add_usage_bucket(buckets, "inference", inference_cost, inference_tokens)
        _add_usage_bucket(buckets, "normalizer", normalizer_cost, normalizer_tokens)
        _add_usage_bucket(buckets, "evaluator", evaluator_cost, evaluator_tokens)
        _add_usage_bucket(buckets, "pipeline", pipeline_cost, pipeline_tokens)
        _add_usage_bucket(buckets, "run", run_cost, run_tokens)
        normalizer_latency = _metric_number(normalizer_metrics, "latency_ms")
        started_at = None
        request_meta = read_json(test_dir / "request_meta.json") if test_dir else None
        if isinstance(request_meta, dict):
            started_at = request_meta.get("started_at_utc")
        client_tool_executions = None
        if isinstance(metrics, dict) and isinstance(
            metrics.get("client_tool_executions"), (int, float)
        ):
            client_tool_executions = int(metrics["client_tool_executions"])
        latency = item.get("latency_ms")
        if isinstance(latency, (int, float)):
            total_latency += float(latency)
            has_latency = True
        wall_ms = None
        if isinstance(latency, (int, float)) or isinstance(normalizer_latency, (int, float)):
            wall_ms = float(latency or 0) + float(normalizer_latency or 0)
        tests_out.append(
            {
                "test_id": test_id,
                "name": name,
                "category_id": category_id,
                "category_name": category_name,
                "category_emoji": category_emoji,
                "model": item.get("model"),
                "latency_ms": latency,
                "normalizer_latency_ms": normalizer_latency,
                "wall_ms": wall_ms,
                "started_at_utc": started_at,
                "status": item.get("status"),
                "score": item.get("score"),
                "error": item.get("error"),
                "cost_usd": inference_cost,
                "total_tokens": _int_or_none(inference_tokens),
                "normalizer_cost_usd": normalizer_cost,
                "normalizer_tokens": _int_or_none(normalizer_tokens),
                "evaluator_cost_usd": evaluator_cost,
                "evaluator_tokens": _int_or_none(evaluator_tokens),
                "pipeline_cost_usd": pipeline_cost,
                "pipeline_tokens": _int_or_none(pipeline_tokens),
                "run_cost_usd": run_cost,
                "run_tokens": _int_or_none(run_tokens),
                "total_cost_usd": run_cost,
                "client_tool_executions": client_tool_executions,
                "checks": summarize_checks(evaluation),
            }
        )

    inference_cost, inference_tokens = _bucket_pair(buckets, "inference")
    normalizer_cost, normalizer_tokens = _bucket_pair(buckets, "normalizer")
    evaluator_cost, evaluator_tokens = _bucket_pair(buckets, "evaluator")
    pipeline_cost, pipeline_tokens = _bucket_pair(buckets, "pipeline")
    run_cost, run_tokens = _bucket_pair(buckets, "run")
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    return {
        "suite_id": summary.get("suite_id") or run_dir.parent.name,
        "run_id": run_dir.name,
        "suite_name": summary.get("suite_name"),
        "description": (summary.get("description") or "").strip() or None,
        "model": summary.get("model"),
        "normalizer_model": summary.get("normalizer_model"),
        "evaluator_mode": summary.get("evaluator_mode"),
        "evaluator_model": summary.get("evaluator_model"),
        "started_at_utc": summary.get("started_at_utc"),
        "finished_at_utc": summary.get("finished_at_utc"),
        "no_pipeline": bool(summary.get("no_pipeline")),
        "counts": {
            "total": counts.get("total", len(tests_out)),
            "pass": counts.get("pass", 0),
            "fail": counts.get("fail", 0),
            "error": counts.get("error", 0),
            "completed": counts.get("completed", 0),
        },
        "totals": {
            "cost_usd": inference_cost,
            "total_tokens": inference_tokens,
            "latency_ms": total_latency if has_latency else None,
            "normalizer_cost_usd": normalizer_cost,
            "normalizer_tokens": normalizer_tokens,
            "evaluator_cost_usd": evaluator_cost,
            "evaluator_tokens": evaluator_tokens,
            "pipeline_cost_usd": pipeline_cost,
            "pipeline_tokens": pipeline_tokens,
            "run_cost_usd": run_cost,
            "run_tokens": run_tokens,
        },
        "tests": tests_out,
    }


def load_test_detail(run_dir: Path, category_id: str, test_id: str, listing: dict[str, Any] | None) -> dict[str, Any]:
    test_dir = run_dir / category_id / test_id
    evaluation = read_json(test_dir / "evaluation.json")
    metrics = read_json(test_dir / "metrics.json")
    normalizer_metrics = read_json(test_dir / "normalizer_metrics.json")
    evaluator_metrics = read_json(test_dir / "evaluator_metrics.json")
    normalized = read_json(test_dir / "normalized.json")
    request = read_json(test_dir / "request.json")
    source = read_json(test_dir / "source.json")
    response = read_json(test_dir / "response.json")
    response_view = extract_response_view(response)
    yaml_meta = test_meta_from_yaml(test_dir / "test.yaml")
    answer = read_text(test_dir / "answer.txt")
    error_text = read_text(test_dir / "error.txt")
    schema_errors = None
    if isinstance(evaluation, dict) and isinstance(evaluation.get("schema"), dict):
        schema_errors = evaluation["schema"].get("errors")

    detail = {
        "suite_id": run_dir.parent.name,
        "run_id": run_dir.name,
        "test_id": test_id,
        "category_id": category_id,
        "exists": test_dir.is_dir(),
        "name": yaml_meta.get("name") or (listing or {}).get("name"),
        "prompt": yaml_meta.get("prompt"),
        "system_prompt": yaml_meta.get("system_prompt"),
        "parameters": yaml_meta.get("parameters"),
        "metadata": yaml_meta.get("metadata"),
        "status": (listing or {}).get("status"),
        "score": (listing or {}).get("score"),
        "error": (listing or {}).get("error"),
        "error_trace": error_text,
        "answer": answer,
        "normalized": normalized,
        "evaluation": evaluation,
        "schema_errors": schema_errors,
        "metrics": metrics,
        "normalizer_metrics": normalizer_metrics,
        "evaluator_metrics": evaluator_metrics,
        "request": request,
        "response": response,
        "response_view": response_view,
        "source": source,
        "tools": load_test_tools(test_dir, request),
        "latency_ms": (listing or {}).get("latency_ms"),
        "normalizer_latency_ms": (listing or {}).get("normalizer_latency_ms"),
        "wall_ms": (listing or {}).get("wall_ms"),
        "started_at_utc": (listing or {}).get("started_at_utc"),
        "cost_usd": (listing or {}).get("cost_usd"),
        "total_tokens": (listing or {}).get("total_tokens"),
        "normalizer_cost_usd": (listing or {}).get("normalizer_cost_usd"),
        "normalizer_tokens": (listing or {}).get("normalizer_tokens"),
        "evaluator_cost_usd": (listing or {}).get("evaluator_cost_usd"),
        "evaluator_tokens": (listing or {}).get("evaluator_tokens"),
        "pipeline_cost_usd": (listing or {}).get("pipeline_cost_usd"),
        "pipeline_tokens": (listing or {}).get("pipeline_tokens"),
        "run_cost_usd": (listing or {}).get("run_cost_usd"),
        "run_tokens": (listing or {}).get("run_tokens"),
        "total_cost_usd": (listing or {}).get("total_cost_usd")
        or (listing or {}).get("run_cost_usd"),
        "checks": (listing or {}).get("checks"),
        "client_tool_executions": (
            int(metrics["client_tool_executions"])
            if isinstance(metrics, dict)
            and isinstance(metrics.get("client_tool_executions"), (int, float))
            else (listing or {}).get("client_tool_executions")
        ),
    }
    if isinstance(source, dict):
        detail["category_name"] = source.get("category_name")
        detail["category_emoji"] = source.get("category_emoji") or (listing or {}).get(
            "category_emoji"
        )
        detail["model"] = source.get("model")
        detail["normalizer_model"] = source.get("normalizer_model")
        detail["evaluator_mode"] = source.get("evaluator_mode")
        detail["evaluator_model"] = source.get("evaluator_model")
    return detail


def load_run_detail(runs_dir: Path, suite_id: str, run_id: str) -> dict[str, Any]:
    run_dir = (runs_dir / suite_id / run_id).resolve()
    runs_root = runs_dir.resolve()
    if runs_root not in run_dir.parents and run_dir != runs_root:
        raise FileNotFoundError("run not found")
    summary = read_json(run_dir / "summary.json")
    if not isinstance(summary, dict):
        raise FileNotFoundError("summary.json not found")
    listing = enrich_run(summary, run_dir)
    tests = []
    for item in listing["tests"]:
        tests.append(load_test_detail(run_dir, item["category_id"], item["test_id"], item))
    listing["tests"] = tests
    return listing


class FoldedStr(str):
    """Multiline string dumped with YAML folded style."""


class PrettyDumper(yaml.SafeDumper if yaml is not None else object):  # type: ignore[misc]
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


if yaml is not None:
    def _represent_folded(dumper: Any, data: FoldedStr) -> Any:
        text = str(data).rstrip("\n") + "\n"
        return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=">")

    def _represent_none(dumper: Any, _data: None) -> Any:
        return dumper.represent_scalar("tag:yaml.org,2002:null", "null")

    PrettyDumper.add_representer(FoldedStr, _represent_folded)
    PrettyDumper.add_representer(type(None), _represent_none)


def fold_walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in FOLD_KEYS and isinstance(value, str) and value.strip():
                out[key] = FoldedStr(value.strip())
            else:
                out[key] = fold_walk(value)
        return out
    if isinstance(obj, list):
        return [fold_walk(item) for item in obj]
    return obj


def dump_yaml(data: Any) -> str:
    if yaml is None:
        raise RuntimeError("PyYAML is required to save YAML files.")
    dumped = yaml.dump(
        fold_walk(data),
        Dumper=PrettyDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=88,
    )
    return dumped if dumped.endswith("\n") else dumped + "\n"


def parse_yaml_text(text: str) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to parse YAML files.")
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("YAML must contain a mapping at the top level.")
    return loaded


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def safe_filename(value: str) -> str:
    cleaned = safe_segment(value)
    if not FILENAME_RE.match(cleaned):
        raise ValueError(
            "File name must be a simple .yaml name, like account_lookup.yaml."
        )
    return cleaned


def yaml_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
        ],
        key=lambda path: path.name.lower(),
    )


def test_summary(path: Path) -> dict[str, Any]:
    loaded = load_yaml(path) or {}
    test = loaded.get("test") if isinstance(loaded.get("test"), dict) else {}
    metadata = test.get("metadata") if isinstance(test.get("metadata"), dict) else {}
    evaluator = (
        ((loaded.get("pipeline") or {}).get("evaluator") or {})
        if isinstance(loaded.get("pipeline"), dict)
        else {}
    )
    checks = evaluator.get("checks") if isinstance(evaluator.get("checks"), list) else []
    return {
        "filename": path.name,
        "path": f"tests/{path.name}",
        "test_id": test.get("id"),
        "name": test.get("name"),
        "category": metadata.get("category"),
        "difficulty": metadata.get("difficulty"),
        "benchmark_group": metadata.get("benchmark_group"),
        "check_count": len(checks),
    }


def suite_summary(path: Path) -> dict[str, Any]:
    loaded = load_yaml(path) or {}
    suite = loaded.get("suite") if isinstance(loaded.get("suite"), dict) else {}
    provider = loaded.get("provider") if isinstance(loaded.get("provider"), dict) else {}
    categories = loaded.get("categories") if isinstance(loaded.get("categories"), list) else []
    test_count = 0
    for category in categories:
        if isinstance(category, dict) and isinstance(category.get("tests"), list):
            test_count += len(category["tests"])
    return {
        "filename": path.name,
        "path": f"test-sets/{path.name}",
        "suite_id": suite.get("id"),
        "name": suite.get("name"),
        "description": str(suite.get("description") or "").strip() or None,
        "model": provider.get("model"),
        "category_count": len(categories),
        "test_count": test_count,
    }


def list_tests() -> list[dict[str, Any]]:
    suites = [suite_summary(path) for path in yaml_files(SUITES_DIR)]
    items = []
    for path in yaml_files(TESTS_DIR):
        item = test_summary(path)
        used_in = []
        for suite_path in yaml_files(SUITES_DIR):
            text = read_text(suite_path) or ""
            if item["path"] in text or path.name in text:
                meta = next((s for s in suites if s["filename"] == suite_path.name), None)
                used_in.append((meta or {}).get("name") or suite_path.name)
        item["used_in"] = used_in
        items.append(item)
    return items


def list_suites() -> list[dict[str, Any]]:
    return [suite_summary(path) for path in yaml_files(SUITES_DIR)]


def load_named_yaml(directory: Path, filename: str, kind: str) -> dict[str, Any]:
    path = (directory / safe_filename(filename)).resolve()
    if directory.resolve() not in path.parents and path.parent != directory.resolve():
        raise FileNotFoundError(f"{kind} not found")
    if not path.is_file():
        raise FileNotFoundError(f"{kind} not found")
    text = read_text(path)
    if text is None:
        raise FileNotFoundError(f"{kind} not found")
    data = parse_yaml_text(text)
    return {
        "filename": path.name,
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "text": text,
        "data": data,
    }


def validate_test_definition(raw: dict[str, Any], path: Path) -> None:
    test = raw.get("test")
    if not isinstance(test, dict) or not str(test.get("id") or "").strip():
        raise ValueError("test.id is required")
    if not str(test.get("name") or "").strip():
        raise ValueError("test.name is required")
    if not str(test.get("prompt") or "").strip():
        raise ValueError("test.prompt is required")
    if ensure_test_does_not_define_owned_provider_fields is not None:
        ensure_test_does_not_define_owned_provider_fields(raw, path)
    pipeline = raw.get("pipeline") if isinstance(raw.get("pipeline"), dict) else {}
    evaluator = pipeline.get("evaluator") if isinstance(pipeline.get("evaluator"), dict) else {}
    if evaluator.get("enabled", True):
        checks = evaluator.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("pipeline.evaluator.checks must list one or more checks")


def save_named_yaml(directory: Path, filename: str, data: dict[str, Any], kind: str) -> dict[str, Any]:
    path = directory / safe_filename(filename)
    if kind == "config":
        path = CONFIG_PATH
        if validate_config is not None:
            validate_config(data, path)
    elif kind == "suite":
        if validate_suite is not None:
            validate_suite(data, path)
    elif kind == "test":
        validate_test_definition(data, path)
    else:
        raise ValueError(f"Unknown document kind: {kind}")
    write_text(path, dump_yaml(data))
    return load_named_yaml(path.parent, path.name, kind) if kind != "config" else {
        "filename": path.name,
        "path": "config.yaml",
        "text": path.read_text(encoding="utf-8"),
        "data": parse_yaml_text(path.read_text(encoding="utf-8")),
    }


def workspace_snapshot(runs_dir: Path) -> dict[str, Any]:
    config = load_yaml(CONFIG_PATH) if CONFIG_PATH.is_file() else {}
    evaluator = (config or {}).get("evaluator") if isinstance(config, dict) else {}
    normalizer = (config or {}).get("normalizer") if isinstance(config, dict) else {}
    runs = discover_runs(runs_dir)
    return {
        "config_path": "config.yaml",
        "normalizer_model": (normalizer or {}).get("model"),
        "evaluator_mode": (evaluator or {}).get("mode"),
        "evaluator_model": (evaluator or {}).get("model"),
        "tests": list_tests(),
        "suites": list_suites(),
        "run_count": len(runs),
    }


def payload_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object.")
    if "text" in payload and payload["text"] is not None:
        return parse_yaml_text(str(payload["text"]))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Body must include a data object or YAML text.")
    return data


class VisualizerHandler(BaseHTTPRequestHandler):
    runs_dir: Path = DEFAULT_RUNS
    static_dir: Path = ROOT
    repo_root: Path = REPO_ROOT

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, cache: bool = True) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status, cache=False)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in {"/api/workspace", "/api/workspace/"}:
            self._send_json(workspace_snapshot(self.runs_dir))
            return

        if path in {"/api/config", "/api/config/"}:
            if not CONFIG_PATH.is_file():
                self._send_error_json(404, "config.yaml not found.")
                return
            try:
                self._send_json(load_named_yaml(CONFIG_PATH.parent, CONFIG_PATH.name, "config"))
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            return

        if path in {"/api/tests", "/api/tests/"}:
            self._send_json({"tests": list_tests()})
            return

        if path.startswith("/api/tests/"):
            filename = path.split("/", 3)[-1]
            try:
                self._send_json(load_named_yaml(TESTS_DIR, unquote(filename), "test"))
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            except FileNotFoundError:
                self._send_error_json(404, "Test file not found.")
            return

        if path in {"/api/suites", "/api/suites/"}:
            self._send_json({"suites": list_suites()})
            return

        if path.startswith("/api/suites/"):
            filename = path.split("/", 3)[-1]
            try:
                self._send_json(load_named_yaml(SUITES_DIR, unquote(filename), "suite"))
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            except FileNotFoundError:
                self._send_error_json(404, "Suite file not found.")
            return

        if path in {"/api/runs", "/api/runs/"}:
            self._send_json({"runs": discover_runs(self.runs_dir)})
            return

        if path.startswith("/api/run/"):
            parts = [p for p in path.split("/") if p]
            # api, run, suite_id, run_id
            if len(parts) != 4:
                self._send_error_json(404, "Run not found. Expected /api/run/<suite_id>/<run_id>.")
                return
            try:
                suite_id = safe_segment(parts[2])
                run_id = safe_segment(parts[3])
                self._send_json(load_run_detail(self.runs_dir, suite_id, run_id))
            except ValueError as exc:
                self._send_error_json(400, str(exc))
            except FileNotFoundError:
                self._send_error_json(404, "Run not found.")
            return

        if path.startswith("/api/"):
            self._send_error_json(404, "Unknown API path.")
            return

        self._serve_static(path)

    def _read_json_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ValueError("Missing JSON body.")
        if length > MAX_BODY_BYTES:
            raise ValueError("Payload is too large.")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Body must be JSON.") from exc

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            payload = self._read_json_body()
            data = payload_data(payload)
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return

        try:
            if path in {"/api/config", "/api/config/"}:
                saved = save_named_yaml(CONFIG_PATH.parent, CONFIG_PATH.name, data, "config")
                self._send_json(saved)
                return
            if path.startswith("/api/tests/"):
                filename = path.split("/", 3)[-1]
                saved = save_named_yaml(TESTS_DIR, unquote(filename), data, "test")
                self._send_json(saved)
                return
            if path.startswith("/api/suites/"):
                filename = path.split("/", 3)[-1]
                saved = save_named_yaml(SUITES_DIR, unquote(filename), data, "suite")
                self._send_json(saved)
                return
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        except RuntimeError as exc:
            self._send_error_json(500, str(exc))
            return

        self._send_error_json(404, "Unknown API path.")

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (self.static_dir / relative).resolve()
        if self.static_dir.resolve() not in target.parents and target != self.static_dir.resolve():
            self._send_error_json(403, "Forbidden.")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send_bytes(b"Not found.", "text/plain; charset=utf-8", status=404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type = f"{content_type}; charset=utf-8"
        self._send_bytes(target.read_bytes(), content_type)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the Salesforce open-model workbench."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Port. Default: 8765")
    parser.add_argument(
        "--runs",
        default=str(DEFAULT_RUNS),
        help="Path to the runs directory. Default: <repo>/runs",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runs_dir = Path(args.runs).resolve()
    VisualizerHandler.runs_dir = runs_dir
    VisualizerHandler.static_dir = ROOT
    VisualizerHandler.repo_root = REPO_ROOT
    server = ThreadingHTTPServer((args.host, args.port), VisualizerHandler)
    print(f"Workbench: http://{args.host}:{args.port}")
    print(f"Repo: {REPO_ROOT}")
    print(f"Runs: {runs_dir}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
