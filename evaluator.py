from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from common import load_test_file, read_json, write_json


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


def main() -> None:
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
    args = parser.parse_args()

    test_file = Path(args.test).resolve()
    run_dir = Path(args.run_dir).resolve()
    _, cfg = load_test_file(
        test_file, suite_path=args.suite, config_path=args.config
    )

    normalized_path = run_dir / "normalized.json"
    if not normalized_path.exists():
        raise SystemExit(f"Missing {normalized_path}. Run normalizer.py first.")

    normalized = read_json(normalized_path)
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
    for check in evaluator_cfg.get("checks", []):
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

    require_schema_valid = evaluator_cfg.get("require_schema_valid", True)
    all_checks_passed = all(c["passed"] for c in checks)
    overall_pass = all_checks_passed and (
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
        "evaluator": {
            "mode": evaluator_mode,
            "model": evaluator_cfg.get("model"),
        },
    }
    write_json(run_dir / "evaluation.json", result)

    print(
        f"Evaluation: {'PASS' if overall_pass else 'FAIL'} "
        f"({sum(1 for c in checks if c['passed'])}/{len(checks)} checks)"
    )
    print(f"Result written to {run_dir / 'evaluation.json'}")

    raise SystemExit(0 if overall_pass else 2)


if __name__ == "__main__":
    main()
