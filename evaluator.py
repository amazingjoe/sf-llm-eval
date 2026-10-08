from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from common import (
    FINISH_FUNCTION,
    fixture_for_test,
    load_test_file,
    read_json,
    tool_limits,
    write_json,
)


def get_path(obj: Any, dotted_path: str) -> Any:
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(dotted_path)
    return current


def compare(actual: Any, expected: Any, op: str, tolerance: float | None = None) -> tuple[bool, str]:
    if op == "equals":
        passed = actual == expected
    elif op == "case_insensitive_equals":
        passed = str(actual).casefold() == str(expected).casefold()
    elif op == "numeric_equals":
        try:
            a = float(actual)
            e = float(expected)
            passed = math.isclose(a, e, abs_tol=tolerance or 0.0, rel_tol=0.0)
        except (TypeError, ValueError):
            passed = False
    elif op == "contains":
        passed = str(expected) in str(actual)
    elif op == "in":
        passed = actual in expected
    else:
        raise ValueError(f"Unsupported evaluation operator: {op}")

    detail = f"actual={actual!r}, expected={expected!r}, op={op}"
    return passed, detail


def evaluation_checks(
    cfg: dict[str, Any], test_file: Path
) -> tuple[list[dict[str, Any]], Path | None]:
    """Benchmark tasks take checks from their fixture; legacy tests keep inline checks."""
    loaded = fixture_for_test(cfg, test_file)
    if loaded is None:
        return list(cfg["pipeline"]["evaluator"].get("checks", [])), None
    fixture_path, fixture = loaded
    return list(fixture["checks"]), fixture_path


def expected_statuses(checks: list[dict[str, Any]]) -> list[str]:
    """Statuses the fixture accepts. Tasks without a status check expect `answered`."""
    for check in checks:
        if check.get("field") != "status":
            continue
        expected = check.get("expected")
        return [str(item) for item in expected] if isinstance(expected, list) else [str(expected)]
    return ["answered"]


def self_assessment(
    completion: dict[str, Any] | None,
    checks: list[dict[str, Any]],
    passed: bool,
) -> dict[str, Any] | None:
    """Compare the subject's finish claim with the external grade.

    match: claimed an accepted status and passed.
    false_success: claimed an accepted status but failed.
    wrong_status: claimed a status the fixture does not accept.
    missing: the finish tool was offered but never validly called.
    None when the finish tool was not offered at all.
    """
    if not completion or not completion.get("offered"):
        return None
    expected = expected_statuses(checks)
    claimed = completion.get("status") if completion.get("called") else None
    if claimed is None:
        label = "missing"
    elif claimed not in expected:
        label = "wrong_status"
    elif passed:
        label = "match"
    else:
        label = "false_success"
    return {
        "label": label,
        "claimed_status": claimed,
        "expected_status": expected,
        "note": completion.get("note") if completion.get("called") else None,
    }


def read_optional_json(path: Path) -> Any:
    return read_json(path) if path.is_file() else None


def grade_answer(
    normalized: Any,
    schema: dict[str, Any],
    configured_checks: list[dict[str, Any]],
    require_schema_valid: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Schema + field checks on one normalized answer. Returns (schema, checks, passed)."""
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(normalized),
        key=lambda e: list(e.path),
    )
    schema_result = {
        "passed": not schema_errors,
        "errors": [
            {
                "path": ".".join(str(p) for p in error.path),
                "message": error.message,
            }
            for error in schema_errors
        ],
    }

    checks = []
    for check in configured_checks:
        field = check["field"]
        op = check.get("op", "equals")
        expected = check.get("expected")
        tolerance = check.get("tolerance")

        try:
            actual = get_path(normalized, field)
            passed, detail = compare(actual, expected, op, tolerance)
        except KeyError:
            actual = None
            passed = False
            detail = f"field '{field}' was missing"

        checks.append(
            {
                "name": check.get("name", field),
                "field": field,
                "operator": op,
                "expected": expected,
                "actual": actual,
                "passed": passed,
                "detail": detail,
            }
        )

    passed = all(c["passed"] for c in checks) and (
        schema_result["passed"] or not require_schema_valid
    )
    return schema_result, checks, passed


def tool_call_counts(trace: dict[str, Any]) -> dict[str, int]:
    """Client tool calls the harness executed, by name. The finish tool is not counted."""
    counts: dict[str, int] = {}
    for round_trace in trace.get("rounds") or []:
        for call in round_trace.get("client_tool_results") or []:
            name = str(call.get("name") or "unknown")
            if name == FINISH_FUNCTION:
                continue
            counts[name] = counts.get(name, 0) + 1
    return counts


def tool_limit_checks(counts: dict[str, int], limits: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {
            "name": f"{name} called at most {limit} time{'s' if limit != 1 else ''}",
            "field": f"tool_calls.{name}",
            "operator": "max_calls",
            "expected": limit,
            "actual": counts.get(name, 0),
            "passed": counts.get(name, 0) <= limit,
            "detail": f"{counts.get(name, 0)} call(s), limit {limit}",
        }
        for name, limit in limits.items()
    ]


def initial_delivery(answer_passed: bool, run_dir: Path) -> dict[str, Any]:
    """delivered, not_found, or pending (the runner then runs the transcript pass)."""
    if answer_passed:
        return {"label": "delivered", "source": "answer"}
    transcript = run_dir / "transcript.txt"
    if not transcript.is_file() or not transcript.read_text(encoding="utf-8").strip():
        return {"label": "not_found", "source": "empty_transcript"}
    return {"label": "pending", "source": None}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate normalized benchmark output deterministically."
    )
    parser.add_argument("--test", required=True, help="Path to the YAML test file.")
    parser.add_argument(
        "--suite",
        help="Path to the parent test-set YAML. Required unless the test file already includes provider fields.",
    )
    parser.add_argument(
        "--config",
        help="Path to config.yaml (evaluator model/mode). Discovered automatically if omitted.",
    )
    parser.add_argument("--run-dir", required=True, help="Existing benchmark run directory.")
    parser.add_argument(
        "--delivery",
        action="store_true",
        help=(
            "Label a pending delivery from normalized_transcript.json. Never "
            "changes pass/fail. Run after normalizer.py --source transcript."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    test_file = Path(args.test).resolve()
    run_dir = Path(args.run_dir).resolve()
    _, cfg = load_test_file(
        test_file, suite_path=args.suite, config_path=args.config
    )

    normalizer_cfg = cfg["pipeline"]["normalizer"]
    evaluator_cfg = cfg["pipeline"]["evaluator"]
    evaluator_mode = str(evaluator_cfg.get("mode") or "deterministic")
    if evaluator_mode == "judge":
        raise SystemExit(
            "evaluator.mode=judge is reserved in config.yaml but not implemented yet. "
            "Set evaluator.mode to deterministic."
        )
    if evaluator_mode != "deterministic":
        raise SystemExit(f"Unknown evaluator.mode: {evaluator_mode}")

    schema = normalizer_cfg["schema"]
    require_schema_valid = evaluator_cfg.get("require_schema_valid", True)
    configured_checks, fixture_path = evaluation_checks(cfg, test_file)

    if args.delivery:
        label_delivery(run_dir, schema, configured_checks, require_schema_valid)
        return

    normalized_path = run_dir / "normalized.json"
    if not normalized_path.exists():
        raise SystemExit(f"Missing {normalized_path}. Run normalizer.py first.")
    normalized = read_json(normalized_path)

    schema_result, checks, answer_passed = grade_answer(
        normalized, schema, configured_checks, require_schema_valid
    )

    completion = read_optional_json(run_dir / "completion.json")
    trace = read_optional_json(run_dir / "tool_trace.json") or {}
    counts = tool_call_counts(trace)
    limits = tool_limits(cfg)
    checks.extend(tool_limit_checks(counts, limits))
    if trace.get("stop_reason") == "max_rounds":
        # Running out of turns means the user never got a final answer.
        checks.append(
            {
                "name": "Finished within max_rounds",
                "field": "stop_reason",
                "operator": "not_max_rounds",
                "expected": "a final answer before request.max_rounds",
                "actual": "max_rounds",
                "passed": False,
                "detail": (
                    f"ran out of turns: hit request.max_rounds="
                    f"{trace.get('max_rounds')} before a final answer"
                ),
            }
        )

    overall_pass = all(c["passed"] for c in checks) and (
        schema_result["passed"] or not require_schema_valid
    )

    score = (
        sum(1 for c in checks if c["passed"]) / len(checks)
        if checks
        else (1.0 if overall_pass else 0.0)
    )

    result = {
        "test_id": cfg["test"]["id"],
        "passed": overall_pass,
        "score": score,
        "schema": schema_result,
        "checks": checks,
        "fixture_file": str(fixture_path) if fixture_path else None,
        "stop_reason": trace.get("stop_reason"),
        "tool_calls": {"counts": counts, "limits": limits},
        "delivery": initial_delivery(answer_passed, run_dir),
        "self_assessment": self_assessment(completion, configured_checks, overall_pass),
        "evaluator": {
            "mode": evaluator_mode,
            "model": evaluator_cfg.get("model"),
        },
    }
    write_json(run_dir / "evaluation.json", result)

    assessment = result["self_assessment"]
    print(
        f"Evaluation: {'PASS' if overall_pass else 'FAIL'} "
        f"({sum(1 for c in checks if c['passed'])}/{len(checks)} checks)"
        + (f" | Self-assessment: {assessment['label']}" if assessment else "")
        + (f" | Tool calls: {format_counts(counts)}" if counts else "")
    )
    print(f"Result written to {run_dir / 'evaluation.json'}")

    raise SystemExit(0 if overall_pass else 2)


def format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))


def label_delivery(
    run_dir: Path,
    schema: dict[str, Any],
    configured_checks: list[dict[str, Any]],
    require_schema_valid: bool,
) -> None:
    """Fill in a pending delivery label from the transcript normalization."""
    evaluation_path = run_dir / "evaluation.json"
    if not evaluation_path.exists():
        raise SystemExit(f"Missing {evaluation_path}. Run evaluator.py first.")
    evaluation = read_json(evaluation_path)
    delivery = evaluation.get("delivery") or {}
    if delivery.get("label") != "pending":
        print(f"Delivery: {delivery.get('label')} (nothing to do)")
        return

    transcript_path = run_dir / "normalized_transcript.json"
    if not transcript_path.exists():
        delivery = {"label": "unavailable", "source": "transcript"}
    else:
        _, checks, found = grade_answer(
            read_json(transcript_path), schema, configured_checks, require_schema_valid
        )
        delivery = {
            "label": "found_not_delivered" if found else "not_found",
            "source": "transcript",
            "checks": checks,
        }
    evaluation["delivery"] = delivery
    write_json(evaluation_path, evaluation)
    print(f"Delivery: {delivery['label']}")


if __name__ == "__main__":
    main()
