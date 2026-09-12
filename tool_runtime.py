from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from common import redact_secrets, tool_call_name, tool_kind

from salesforce import (
    SalesforceCliProvider,
    is_salesforce_function,
    not_implemented_message,
    schema_for_shorthand,
    SALESFORCE_HANDLERS,
)

DEFAULT_MAX_TOOL_RESULT_CHARS = 120_000


@dataclass
class ToolContext:
    salesforce: SalesforceCliProvider | None = None
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS


def translate_client_shorthand(key: str) -> dict[str, Any] | None:
    """Expand a YAML shorthand into an OpenAI function tool, or raise if reserved."""
    message = not_implemented_message(key)
    if message:
        raise ValueError(f"Tool '{key}' is not implemented. {message}")
    return schema_for_shorthand(key)


def handlers() -> dict[str, Callable[[dict[str, Any], ToolContext], dict[str, Any]]]:
    return SALESFORCE_HANDLERS


def function_name_from_tool(tool: dict[str, Any]) -> str | None:
    if tool_kind(tool) == "server":
        return None
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    name = function.get("name") or tool.get("name")
    return str(name) if name else None


def advertised_client_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        name = function_name_from_tool(tool)
        if name:
            names.append(name)
    return names


def ensure_client_tools_supported(tools: list[dict[str, Any]]) -> None:
    known = handlers()
    unknown = [
        name for name in advertised_client_tool_names(tools) if name not in known
    ]
    if unknown:
        raise ValueError(
            "No executor for function tool(s): "
            + ", ".join(unknown)
            + ". Use a registered shorthand such as salesforce.query."
        )


def test_needs_salesforce(cfg: dict[str, Any]) -> bool:
    from common import translate_tools

    tools = translate_tools((cfg.get("request") or {}).get("tools") or [])
    return any(is_salesforce_function(name) for name in advertised_client_tool_names(tools))


def parse_tool_arguments(call: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = function.get("arguments", call.get("arguments", "{}"))
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, "Tool arguments must be a JSON object string"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON arguments: {exc}"
    if not isinstance(parsed, dict):
        return None, "Tool arguments must be a JSON object"
    return parsed, None


def tool_message(call_id: str, payload: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    content = json.dumps(redact_secrets(payload), ensure_ascii=False, default=str)
    if len(content) > max_chars:
        truncated = {
            "error": "Tool result exceeded size limit and was truncated.",
            "truncated": True,
            "preview": content[: max(0, max_chars - 200)],
        }
        content = json.dumps(truncated, ensure_ascii=False)
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    }


def execute_client_tool(
    call: dict[str, Any],
    context: ToolContext | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = context or ToolContext()
    call_id = str(call.get("id") or "")
    name = tool_call_name(call)
    args, parse_error = parse_tool_arguments(call)
    if parse_error:
        payload = {"error": parse_error}
        trace = {
            "name": name,
            "ok": False,
            "error": parse_error,
            "latency_ms": 0,
        }
        return tool_message(call_id, payload, max_chars=context.max_tool_result_chars), trace

    handler = handlers().get(name)
    if handler is None:
        payload = {"error": f"Unknown tool '{name}'"}
        trace = {
            "name": name,
            "ok": False,
            "error": payload["error"],
            "latency_ms": 0,
        }
        return tool_message(call_id, payload, max_chars=context.max_tool_result_chars), trace

    start = time.perf_counter()
    try:
        payload = handler(args or {}, context)
        if not isinstance(payload, dict):
            payload = {"result": payload}
    except Exception as exc:
        payload = {"error": str(exc)}
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    ok = not (isinstance(payload, dict) and payload.get("error"))
    trace: dict[str, Any] = {
        "name": name,
        "ok": ok,
        "latency_ms": latency_ms,
    }
    if isinstance(payload, dict):
        if payload.get("error"):
            trace["error"] = payload.get("error")
        if "returned" in payload:
            trace["returned"] = payload.get("returned")
        if "total_size" in payload:
            trace["total_size"] = payload.get("total_size")
        if "truncated" in payload:
            trace["truncated"] = payload.get("truncated")
        if "name" in payload and name == "salesforce_describe":
            trace["sobject"] = payload.get("name")
        if "field_count" in payload:
            trace["field_count"] = payload.get("field_count")
    message = tool_message(call_id, payload, max_chars=context.max_tool_result_chars)
    return message, trace
