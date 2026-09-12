from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from common import (
    extract_assistant_text,
    load_test_file,
    redact_secrets,
    usage_metrics,
    write_json,
)


def extract_json_object(text: str) -> Any:
    """Parse plain JSON or JSON inside a fenced code block."""
    stripped = text.strip()

    # First try the entire response.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Then try a ```json ... ``` fence.
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.S | re.I)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    # Finally, take the largest likely object/array span.
    starts = [i for i in (stripped.find("{"), stripped.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end_obj = stripped.rfind("}")
        end_arr = stripped.rfind("]")
        end = max(end_obj, end_arr)
        if end > start:
            return json.loads(stripped[start : end + 1])

    raise ValueError("Normalizer did not return parseable JSON.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize a raw benchmark answer into the configured JSON schema."
    )
    parser.add_argument("--test", required=True, help="Path to the YAML test file.")
    parser.add_argument(
        "--suite",
        help="Path to the parent test-set YAML. Required unless the test file already includes provider fields.",
    )
    parser.add_argument(
        "--config",
        help="Path to config.yaml (normalizer model). Discovered automatically if omitted.",
    )
    parser.add_argument("--run-dir", required=True, help="Existing benchmark run directory.")
    args = parser.parse_args()

    test_file = Path(args.test).resolve()
    run_dir = Path(args.run_dir).resolve()

    _, cfg = load_test_file(
        test_file, suite_path=args.suite, config_path=args.config
    )
    normalizer_cfg = cfg["pipeline"]["normalizer"]

    answer_path = run_dir / "answer.txt"
    if not answer_path.exists():
        raise SystemExit(f"Missing {answer_path}. Run runner.py first.")

    answer = answer_path.read_text(encoding="utf-8")
    schema = normalizer_cfg["schema"]

    instructions = normalizer_cfg.get(
        "instructions",
        (
            "Extract only information that is explicitly present in the subject "
            "model's answer. Do not correct mistakes, infer missing facts, or use "
            "outside knowledge. Return JSON only."
        ),
    )

    normalizer_prompt = f"""You are a benchmark normalization layer.

{instructions}

JSON schema:
{json.dumps(schema, indent=2)}

SUBJECT MODEL ANSWER:
---BEGIN ANSWER---
{answer}
---END ANSWER---

Return only the normalized JSON object.
"""

    payload = {
        "model": normalizer_cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You normalize benchmark outputs. You do not judge correctness "
                    "and you never repair the subject model's factual errors."
                ),
            },
            {"role": "user", "content": normalizer_prompt},
        ],
        **normalizer_cfg.get("parameters", {}),
    }

    # Optional OpenRouter/provider-specific fields, e.g. response_format.
    payload.update(normalizer_cfg.get("extra_payload", {}))

    endpoint = normalizer_cfg["endpoint"]
    api_key = normalizer_cfg["api_key"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **normalizer_cfg.get("headers", {}),
    }

    write_json(run_dir / "normalizer_request.json", payload)
    write_json(
        run_dir / "normalizer_request_meta.json",
        {
            "endpoint": endpoint,
            "headers": redact_secrets(headers),
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )

    start = time.perf_counter()
    response = requests.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=normalizer_cfg.get("timeout_seconds", 120),
    )
    latency_ms = round((time.perf_counter() - start) * 1000, 2)

    (run_dir / "normalizer_response.raw.txt").write_text(
        response.text, encoding="utf-8"
    )
    write_json(
        run_dir / "normalizer_response_meta.json",
        {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "latency_ms": latency_ms,
        },
    )

    try:
        response_json = response.json()
    except ValueError:
        raise SystemExit(
            f"Normalizer returned non-JSON HTTP response. "
            f"See {run_dir / 'normalizer_response.raw.txt'}"
        )

    write_json(run_dir / "normalizer_response.json", response_json)

    if not response.ok:
        raise SystemExit(
            f"Normalizer returned HTTP {response.status_code}. "
            f"See {run_dir / 'normalizer_response.json'}"
        )

    text = extract_assistant_text(response_json)
    try:
        normalized = extract_json_object(text)
    except Exception as exc:
        (run_dir / "normalized_parse_error.txt").write_text(
            str(exc) + "\n\n" + text, encoding="utf-8"
        )
        raise SystemExit(
            f"Could not parse normalizer output as JSON. "
            f"See {run_dir / 'normalized_parse_error.txt'}"
        )

    write_json(run_dir / "normalized.json", normalized)
    write_json(
        run_dir / "normalizer_metrics.json",
        {
            "model": normalizer_cfg["model"],
            "latency_ms": latency_ms,
            **usage_metrics(response_json),
        },
    )

    print(f"Normalized output written to {run_dir / 'normalized.json'}")


if __name__ == "__main__":
    main()
