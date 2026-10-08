from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

CONFIG_FILENAME = "config.yaml"

# Subject model/provider come from the parent test-set.
SUITE_OWNED_REQUEST_KEYS = ("endpoint", "api_key", "model")
# Pipeline models come from the global config.yaml, not tests or test-sets.
PIPELINE_PROVIDER_KEYS = ("endpoint", "api_key", "model")
VALID_EVALUATOR_MODES = ("deterministic", "judge")

# YAML shorthand -> OpenRouter server-tool type. OpenRouter executes these.
SERVER_TOOL_SHORTHAND = {
    "web_search": "openrouter:web_search",
    "search": "openrouter:web_search",
    "web_fetch": "openrouter:web_fetch",
    "datetime": "openrouter:datetime",
}
DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_MAX_ROUNDS = 8

# Finish signal. The subject calls this in the same message as its final answer.
FINISH_SHORTHAND = "benchmark.finish"
FINISH_FUNCTION = "benchmark_finish"
FINISH_STATUSES = ("answered", "not_found", "declined")
# Appended to the system prompt by the harness when completion.required is true.
# Identical for every task and model.
FINISH_INSTRUCTION = (
    "When you give your final answer to the user, call the benchmark_finish tool "
    "in that same message. Set status to answered, not_found, or declined. "
    "The text of that message is the answer the user receives."
)


def load_yaml_object(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return raw


def load_env_near(*paths: str | Path) -> None:
    seen: set[Path] = set()
    for path in paths:
        dotenv = find_dotenv_near(Path(path).resolve().parent)
        if dotenv is None or dotenv in seen:
            continue
        seen.add(dotenv)
        load_dotenv(dotenv_path=dotenv, override=False)
    load_dotenv(override=False)


def discover_config_file(*hints: str | Path | None) -> Path | None:
    """Find config.yaml by walking up from the given paths, cwd, then this package."""
    starts = [hint for hint in hints if hint]
    starts.extend([Path.cwd(), Path(__file__).resolve()])
    return find_named_file_near(CONFIG_FILENAME, *starts)


def load_config_file(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the global harness config.yaml and return (raw, env-resolved)."""
    path = Path(path).resolve()
    load_env_near(path)
    raw = load_yaml_object(path)
    validate_config(raw, path)
    return raw, resolve_env(raw)


def load_suite_file(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a test-set YAML and return (raw_config, env-resolved config)."""
    path = Path(path).resolve()
    load_env_near(path)
    raw = load_yaml_object(path)
    validate_suite(raw, path)
    return raw, resolve_env(raw)


def load_test_file(
    path: str | Path,
    suite_path: str | Path | None = None,
    suite: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a test YAML, overlay suite + global config, and resolve ${VAR}.

    Layering:
    - test-set: subject model/provider
    - config.yaml: normalizer and evaluator models/providers
    - test: prompts, schema, checks, temperature / max_tokens
    """
    path = Path(path).resolve()
    env_roots = [path]
    raw_suite = suite
    if suite_path is not None:
        suite_path = Path(suite_path).resolve()
        env_roots.append(suite_path)
        if raw_suite is None:
            raw_suite = load_yaml_object(suite_path)
            validate_suite(raw_suite, suite_path)

    raw_config = config
    resolved_config_path = Path(config_path).resolve() if config_path else None
    if resolved_config_path is not None:
        env_roots.append(resolved_config_path)
        if raw_config is None:
            raw_config = load_yaml_object(resolved_config_path)
            validate_config(raw_config, resolved_config_path)
    elif raw_config is None:
        discovered = discover_config_file(path, suite_path)
        if discovered is not None:
            resolved_config_path = discovered
            env_roots.append(discovered)
            raw_config = load_yaml_object(discovered)
            validate_config(raw_config, discovered)

    load_env_near(*env_roots)
    raw = load_yaml_object(path)
    if is_benchmark_task(raw):
        validate_benchmark_task(raw, path)

    if raw_suite is not None:
        ensure_test_does_not_define_owned_provider_fields(raw, path)
        raw = merge_suite_into_test(raw, raw_suite)
    else:
        ensure_provider_present(
            raw,
            path,
            hint=(
                "Individual tests no longer define model/provider. "
                "Run via a test-set: python runner.py --suite test-sets/<file>.yaml"
            ),
        )

    if raw_config is not None:
        raw = merge_config_into_test(raw, raw_config, config_path=resolved_config_path)
    else:
        ensure_pipeline_providers_present(
            raw,
            path,
            hint=f"Add {CONFIG_FILENAME} at the project root, or pass --config.",
        )

    raw = apply_completion_settings(raw)
    validate_tool_limits(raw, path)
    return raw, resolve_env(raw)


def tool_function_name(entry: Any) -> str | None:
    """Function name the model calls for a tool entry; None for server tools."""
    tool = translate_tool(entry)
    if tool_kind(tool) == "server":
        return None
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(function.get("name") or "") or None


def tool_limits(cfg: dict[str, Any]) -> dict[str, int]:
    """Graded per-tool call ceilings, keyed by function name. Empty means count only."""
    limits: dict[str, int] = {}
    for key, spec in (cfg.get("tool_limits") or {}).items():
        name = tool_function_name(str(key))
        if name:
            limits[name] = int(spec["max_calls"])
    return limits


def validate_tool_limits(raw: dict[str, Any], path: str | Path) -> None:
    """tool_limits: {<tool>: {max_calls: N}} with N a positive integer, for offered tools."""
    path = Path(path)
    limits = raw.get("tool_limits")
    if limits is None:
        return
    if not isinstance(limits, dict):
        raise ValueError(f"{path}: tool_limits must be a mapping of tool name to limits")
    offered = {
        tool_function_name(tool) for tool in (raw.get("request") or {}).get("tools") or []
    }
    for key, spec in limits.items():
        try:
            name = tool_function_name(str(key))
        except ValueError as exc:
            raise ValueError(f"{path}: tool_limits.{key}: {exc}") from exc
        if name is None:
            raise ValueError(f"{path}: tool_limits.{key}: only client tools can be limited")
        if name == FINISH_FUNCTION:
            raise ValueError(f"{path}: tool_limits.{key}: the finish tool cannot be limited")
        if name not in offered:
            raise ValueError(
                f"{path}: tool_limits.{key} names a tool this test does not offer"
            )
        max_calls = spec.get("max_calls") if isinstance(spec, dict) else None
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError(
                f"{path}: tool_limits.{key}.max_calls must be a positive integer. "
                "Omit tool_limits to only count calls."
            )


def completion_required(cfg: dict[str, Any]) -> bool:
    return bool((cfg.get("completion") or {}).get("required"))


def is_finish_tool_entry(entry: Any) -> bool:
    if isinstance(entry, str):
        return entry.strip() in (FINISH_SHORTHAND, FINISH_FUNCTION)
    if isinstance(entry, dict):
        function = entry.get("function") if isinstance(entry.get("function"), dict) else {}
        return entry.get("type") == FINISH_SHORTHAND or function.get("name") == FINISH_FUNCTION
    return False


def apply_completion_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Offer the finish tool and its fixed instruction when completion is required."""
    if not completion_required(raw):
        return raw
    merged = deepcopy(raw)
    request = merged.setdefault("request", {})
    tools = list(request.get("tools") or [])
    if not any(is_finish_tool_entry(tool) for tool in tools):
        tools.append(FINISH_SHORTHAND)
    request["tools"] = tools
    system_prompt = str(request.get("system_prompt") or "").rstrip()
    if FINISH_INSTRUCTION not in system_prompt:
        request["system_prompt"] = (
            f"{system_prompt}\n\n{FINISH_INSTRUCTION}" if system_prompt else FINISH_INSTRUCTION
        )
    return merged


def is_benchmark_task(raw: dict[str, Any]) -> bool:
    """A benchmark task keeps its answer key in a separate fixture file."""
    return bool((raw.get("test") or {}).get("fixture"))


def resolve_fixture_path(task_path: str | Path, ref: str | Path) -> Path:
    candidate = Path(ref)
    if candidate.is_absolute():
        return candidate
    return (Path(task_path).resolve().parent / candidate).resolve()


def load_fixture_file(path: str | Path) -> dict[str, Any]:
    """Load an evaluator-only fixture. Never pass its contents to the model."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Fixture not found: {path}")
    raw = load_yaml_object(path)
    validate_fixture(raw, path)
    return raw


def validate_fixture(raw: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    if not str(raw.get("test_id") or "").strip():
        raise ValueError(f"{path}: test_id is required")
    checks = raw.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"{path}: checks must be a non-empty list")
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not check.get("field"):
            raise ValueError(f"{path}: checks[{index}] must be a mapping with a field")
        if "expected" not in check:
            raise ValueError(f"{path}: checks[{index}] must set expected")


def fixture_for_test(
    raw_test: dict[str, Any], task_path: str | Path
) -> tuple[Path, dict[str, Any]] | None:
    """Return (path, fixture) for a benchmark task, or None for a legacy test."""
    ref = (raw_test.get("test") or {}).get("fixture")
    if not ref:
        return None
    fixture_path = resolve_fixture_path(task_path, str(ref))
    return fixture_path, load_fixture_file(fixture_path)


def find_expected_keys(value: Any, prefix: str = "") -> list[str]:
    """Dotted paths of answer-key fields: `expected`, `expected_*`, evaluator checks."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if dotted == "pipeline.normalizer.schema":
                # Schema property names describe the answer shape, not its values.
                continue
            key_text = str(key)
            if (
                key_text == "expected"
                or key_text.startswith("expected_")
                or dotted == "pipeline.evaluator.checks"
            ):
                found.append(dotted)
                continue
            found.extend(find_expected_keys(child, dotted))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_expected_keys(child, f"{prefix}[{index}]"))
    return found


def model_visible_texts(raw_test: dict[str, Any]) -> dict[str, str]:
    """Every task field runner.py sends to the subject model."""
    test = raw_test.get("test") or {}
    request = raw_test.get("request") or {}
    texts = {
        "test.prompt": test.get("prompt"),
        "test.success_criteria": test.get("success_criteria"),
        "request.system_prompt": request.get("system_prompt"),
    }
    for index, message in enumerate(request.get("messages") or []):
        if isinstance(message, dict):
            texts[f"request.messages[{index}].content"] = message.get("content")
    return {key: text for key, text in texts.items() if isinstance(text, str)}


def schema_vocabulary(schema: Any) -> set[str]:
    """Enum/const strings in the normalizer schema. These are public, not answers."""
    words: set[str] = set()
    if isinstance(schema, dict):
        for key, child in schema.items():
            if key == "enum" and isinstance(child, list):
                words.update(str(item).casefold() for item in child if isinstance(item, str))
            elif key == "const" and isinstance(child, str):
                words.add(child.casefold())
            else:
                words.update(schema_vocabulary(child))
    elif isinstance(schema, list):
        for child in schema:
            words.update(schema_vocabulary(child))
    return words


def fixture_string_values(fixture: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for check in fixture.get("checks") or []:
        expected = check.get("expected")
        items = expected if isinstance(expected, list) else [expected]
        values.extend(item for item in items if isinstance(item, str) and item.strip())
    return values


def find_fixture_leaks(
    raw_test: dict[str, Any], fixture: dict[str, Any]
) -> list[tuple[str, str]]:
    """(task field, fixture value) pairs where an answer appears in model-visible text."""
    schema = ((raw_test.get("pipeline") or {}).get("normalizer") or {}).get("schema")
    public = schema_vocabulary(schema)
    leaks: list[tuple[str, str]] = []
    texts = {key: text.casefold() for key, text in model_visible_texts(raw_test).items()}
    for value in fixture_string_values(fixture):
        needle = value.strip().casefold()
        if needle in public:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
        for key, text in texts.items():
            if pattern.search(text):
                leaks.append((key, value))
    return leaks


def validate_benchmark_task(raw: dict[str, Any], path: str | Path) -> None:
    """Reject a benchmark task that carries or leaks its own answer key."""
    path = Path(path).resolve()
    expected_keys = find_expected_keys(raw)
    if expected_keys:
        raise ValueError(
            f"{path}: benchmark tasks must not contain expected values "
            f"({', '.join(expected_keys)}). Move them to the fixture file."
        )

    loaded = fixture_for_test(raw, path)
    assert loaded is not None
    fixture_path, fixture = loaded
    check_task_against_fixture(raw, path, fixture, fixture_path)


def check_task_against_fixture(
    raw: dict[str, Any],
    path: str | Path,
    fixture: dict[str, Any],
    fixture_path: str | Path,
) -> None:
    """Fixture belongs to this task, and the task text does not reveal it."""
    path = Path(path)
    test_id = str((raw.get("test") or {}).get("id") or "")
    if str(fixture.get("test_id")) != test_id:
        raise ValueError(
            f"{fixture_path}: test_id '{fixture.get('test_id')}' does not match "
            f"task test.id '{test_id}' in {path}"
        )

    leaks = find_fixture_leaks(raw, fixture)
    if leaks:
        details = ", ".join(f"{key} contains {value!r}" for key, value in leaks)
        raise ValueError(
            f"{path}: model-visible text contains fixture values ({details}). "
            "Reword the task so it does not reveal the answer."
        )


def validate_config(raw: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    normalizer = raw.get("normalizer")
    if not isinstance(normalizer, dict):
        raise ValueError(f"{path}: normalizer must be a mapping")
    for key in PIPELINE_PROVIDER_KEYS:
        if not normalizer.get(key):
            raise ValueError(f"{path}: normalizer.{key} is required")

    evaluator = raw.get("evaluator")
    if not isinstance(evaluator, dict):
        raise ValueError(f"{path}: evaluator must be a mapping")
    mode = str(evaluator.get("mode") or "deterministic")
    if mode not in VALID_EVALUATOR_MODES:
        raise ValueError(
            f"{path}: evaluator.mode must be one of {', '.join(VALID_EVALUATOR_MODES)}"
        )
    if mode == "judge":
        for key in PIPELINE_PROVIDER_KEYS:
            if not evaluator.get(key):
                raise ValueError(
                    f"{path}: evaluator.{key} is required when evaluator.mode is 'judge'"
                )


def validate_suite(raw: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    suite = raw.get("suite")
    if not isinstance(suite, dict) or not str(suite.get("id") or "").strip():
        raise ValueError(f"{path}: suite.id is required")

    provider = raw.get("provider")
    if not isinstance(provider, dict):
        raise ValueError(f"{path}: provider must be a mapping")
    for key in SUITE_OWNED_REQUEST_KEYS:
        if not provider.get(key):
            raise ValueError(f"{path}: provider.{key} is required")

    misplaced = [key for key in ("normalizer", "evaluator") if key in raw]
    if misplaced:
        names = ", ".join(misplaced)
        verb = "belongs" if len(misplaced) == 1 else "belong"
        raise ValueError(
            f"{path}: {names} {verb} in {CONFIG_FILENAME}, not in a test-set. "
            "Test-sets only choose the subject model and tests."
        )

    categories = raw.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError(f"{path}: categories must be a non-empty list")

    seen_category_ids: set[str] = set()
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise ValueError(f"{path}: categories[{index}] must be a mapping")
        category_id = str(category.get("id") or "").strip()
        if not category_id:
            raise ValueError(f"{path}: categories[{index}].id is required")
        if category_id in seen_category_ids:
            raise ValueError(f"{path}: duplicate category id '{category_id}'")
        seen_category_ids.add(category_id)
        tests = category.get("tests")
        if not isinstance(tests, list) or not tests:
            raise ValueError(
                f"{path}: categories[{index}] ('{category_id}') must list one or more tests"
            )


def ensure_test_does_not_define_owned_provider_fields(
    raw: dict[str, Any], path: str | Path
) -> None:
    path = Path(path)
    request = raw.get("request") or {}
    owned = [key for key in SUITE_OWNED_REQUEST_KEYS if key in request]
    if owned:
        raise ValueError(
            f"{path}: tests must not set request.{', request.'.join(owned)}. "
            "Those fields come from the parent test-set."
        )

    pipeline = raw.get("pipeline") or {}
    for role in ("normalizer", "evaluator"):
        block = pipeline.get(role) or {}
        owned_keys = [key for key in PIPELINE_PROVIDER_KEYS if key in block]
        if role == "evaluator" and "mode" in block:
            owned_keys.append("mode")
        if owned_keys:
            joined = ", ".join(f"pipeline.{role}.{key}" for key in owned_keys)
            raise ValueError(
                f"{path}: tests must not set {joined}. "
                f"Those fields come from {CONFIG_FILENAME}."
            )


def ensure_provider_present(
    raw: dict[str, Any],
    path: str | Path,
    hint: str | None = None,
) -> None:
    request = raw.get("request") or {}
    missing = [key for key in SUITE_OWNED_REQUEST_KEYS if not request.get(key)]
    if not missing:
        return
    message = f"{path} is missing request.{', request.'.join(missing)}."
    if hint:
        message = f"{message} {hint}"
    raise ValueError(message)


def overlay_provider_block(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Copy model/provider identity from source onto target; keep target extras."""
    for key in PIPELINE_PROVIDER_KEYS:
        if key in source:
            target[key] = deepcopy(source[key])
    if "headers" in source:
        target["headers"] = {
            **deepcopy(source.get("headers") or {}),
            **deepcopy(target.get("headers") or {}),
        }
    if "timeout_seconds" not in target and "timeout_seconds" in source:
        target["timeout_seconds"] = source["timeout_seconds"]
    source_extra = deepcopy(source.get("extra_payload") or {})
    target_extra = deepcopy(target.get("extra_payload") or {})
    if source_extra or target_extra:
        target["extra_payload"] = {**source_extra, **target_extra}


def merge_suite_into_test(
    test_cfg: dict[str, Any], suite_cfg: dict[str, Any]
) -> dict[str, Any]:
    """Copy suite-owned subject model/provider fields onto a test definition."""
    merged = deepcopy(test_cfg)
    provider = suite_cfg.get("provider") or {}
    request = merged.setdefault("request", {})
    overlay_provider_block(request, provider)

    if "logging" not in merged and "logging" in suite_cfg:
        merged["logging"] = deepcopy(suite_cfg["logging"])

    suite_meta = suite_cfg.get("suite") or {}
    merged["_suite"] = {
        "id": suite_meta.get("id"),
        "name": suite_meta.get("name"),
        "description": suite_meta.get("description"),
        "model": provider.get("model"),
    }
    return merged


def merge_config_into_test(
    test_cfg: dict[str, Any],
    config_cfg: dict[str, Any],
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Copy global normalizer/evaluator models onto a test definition."""
    merged = deepcopy(test_cfg)
    pipeline = merged.setdefault("pipeline", {})

    config_normalizer = config_cfg.get("normalizer") or {}
    normalizer = pipeline.setdefault("normalizer", {})
    overlay_provider_block(normalizer, config_normalizer)

    config_evaluator = config_cfg.get("evaluator") or {}
    evaluator = pipeline.setdefault("evaluator", {})
    overlay_provider_block(evaluator, config_evaluator)
    evaluator["mode"] = str(config_evaluator.get("mode") or "deterministic")

    if "logging" not in merged and "logging" in config_cfg:
        merged["logging"] = deepcopy(config_cfg["logging"])

    merged["_pipeline"] = {
        "config_file": str(Path(config_path).resolve()) if config_path else None,
        "normalizer_model": config_normalizer.get("model"),
        "evaluator_mode": evaluator.get("mode"),
        "evaluator_model": config_evaluator.get("model"),
    }
    return merged


def ensure_pipeline_providers_present(
    raw: dict[str, Any],
    path: str | Path,
    hint: str | None = None,
) -> None:
    normalizer = (raw.get("pipeline") or {}).get("normalizer") or {}
    if normalizer.get("enabled", True):
        missing = [key for key in PIPELINE_PROVIDER_KEYS if not normalizer.get(key)]
        if missing:
            message = (
                f"{path} is missing pipeline.normalizer."
                f"{', pipeline.normalizer.'.join(missing)}."
            )
            if hint:
                message = f"{message} {hint}"
            raise ValueError(message)


def resolve_test_path(ref: str | Path, suite_path: str | Path) -> Path:
    candidate = Path(ref)
    if candidate.is_absolute():
        return candidate
    from_suite = (Path(suite_path).resolve().parent / candidate)
    if from_suite.exists():
        return from_suite.resolve()
    from_cwd = Path.cwd() / candidate
    return from_cwd.resolve()


def iter_suite_tests(
    suite_cfg: dict[str, Any], suite_path: str | Path
) -> list[dict[str, Any]]:
    """Flatten suite categories into an ordered list of test entries."""
    suite_path = Path(suite_path).resolve()
    entries: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()

    for category in suite_cfg.get("categories") or []:
        category_id = str(category.get("id"))
        category_name = str(category.get("name") or category_id)
        category_emoji = str(category.get("emoji") or "").strip() or None
        for ref in category.get("tests") or []:
            if isinstance(ref, dict):
                ref = ref.get("path") or ref.get("test")
            if not ref:
                raise ValueError(
                    f"{suite_path}: category '{category_id}' has an empty test reference"
                )
            test_path = resolve_test_path(str(ref), suite_path)
            if not test_path.exists():
                raise FileNotFoundError(
                    f"{suite_path}: test '{ref}' in category '{category_id}' "
                    f"was not found (resolved to {test_path})"
                )
            if test_path in seen_paths:
                raise ValueError(
                    f"{suite_path}: test {test_path} is listed more than once"
                )
            seen_paths.add(test_path)
            entries.append(
                {
                    "category_id": category_id,
                    "category_name": category_name,
                    "category_emoji": category_emoji,
                    "test_path": test_path,
                    "test_ref": str(ref),
                }
            )
    return entries


def find_named_file_near(filename: str, *starts: str | Path) -> Path | None:
    seen: set[Path] = set()
    for start in starts:
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
        for candidate_dir in [current, *current.parents]:
            if candidate_dir in seen:
                continue
            seen.add(candidate_dir)
            candidate = candidate_dir / filename
            if candidate.is_file():
                return candidate
    return None


def find_dotenv_near(start: Path) -> Path | None:
    return find_named_file_near(".env", start)


def resolve_env(value: Any) -> Any:
    """Recursively replace ${VAR} using environment/.env values."""
    if isinstance(value, dict):
        return {k: resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env(v) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in os.environ:
                raise KeyError(
                    f"Environment variable '{key}' was referenced but not found. "
                    f"Add it to .env or the process environment."
                )
            return os.environ[key]
        return ENV_PATTERN.sub(repl, value)
    return value


def redact_secrets(value: Any) -> Any:
    """Recursively redact likely secret-bearing fields before logging."""
    secret_fragments = ("api_key", "apikey", "authorization", "token", "secret", "password")

    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key_lower = str(k).lower()
            if any(fragment in key_lower for fragment in secret_fragments):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


def write_json(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_assistant_text(response_json: dict[str, Any]) -> str:
    try:
        content = response_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            "Could not find choices[0].message.content in the model response."
        ) from exc

    if content is None:
        return ""

    if isinstance(content, str):
        return content

    # Some providers may return content blocks.
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)

    return str(content)


def translate_tool(entry: Any) -> dict[str, Any]:
    """Turn a YAML tool entry into an OpenRouter/OpenAI tools[] item.

    Server tools (web_search, openrouter:*) are executed by OpenRouter.
    Function tools are client-side and must be run by this harness.
    """
    if isinstance(entry, str):
        key = entry.strip()
        if not key:
            raise ValueError("Empty tool shorthand")
        if key.startswith("openrouter:"):
            return {"type": key}
        if key in SERVER_TOOL_SHORTHAND:
            return {"type": SERVER_TOOL_SHORTHAND[key]}
        from tool_runtime import translate_client_shorthand

        client = translate_client_shorthand(key)
        if client is not None:
            return client
        raise ValueError(
            f"Unknown tool shorthand '{entry}'. "
            f"Use one of {', '.join(sorted(SERVER_TOOL_SHORTHAND))}, "
            "a Salesforce shorthand such as salesforce.query, "
            "or an OpenAI function tool object."
        )

    if not isinstance(entry, dict):
        raise ValueError(f"Tool entry must be a string or mapping, got {type(entry).__name__}")

    typ = entry.get("type")
    if typ in SERVER_TOOL_SHORTHAND:
        params = {k: v for k, v in entry.items() if k != "type"}
        translated: dict[str, Any] = {"type": SERVER_TOOL_SHORTHAND[str(typ)]}
        if params:
            translated["parameters"] = params
        return translated

    if isinstance(typ, str) and typ.startswith("openrouter:"):
        return deepcopy(entry)

    if isinstance(typ, str) and typ != "function":
        from tool_runtime import translate_client_shorthand

        client = translate_client_shorthand(typ)
        if client is not None:
            return client

    return deepcopy(entry)


def translate_tools(tools: Any) -> list[dict[str, Any]]:
    if not tools:
        return []
    if not isinstance(tools, list):
        raise ValueError("request.tools must be a list")
    return [translate_tool(item) for item in tools]


def tool_kind(tool: dict[str, Any]) -> str:
    typ = str(tool.get("type") or "")
    if typ.startswith("openrouter:"):
        return "server"
    return "client"


def has_server_tools(tools: list[dict[str, Any]] | None) -> bool:
    return any(tool_kind(tool) == "server" for tool in tools or [])


def has_client_tools(tools: list[dict[str, Any]] | None) -> bool:
    return any(tool_kind(tool) == "client" for tool in tools or [])


def assistant_message(response_json: dict[str, Any]) -> dict[str, Any]:
    try:
        message = response_json["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            "Could not find choices[0].message in the model response."
        ) from exc
    if not isinstance(message, dict):
        raise ValueError("choices[0].message must be an object.")
    return message


def finish_reason(response_json: dict[str, Any]) -> str | None:
    try:
        return response_json["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


def client_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool calls the harness must execute. Server-tool calls are excluded."""
    calls = message.get("tool_calls") or []
    client: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        typ = str(call.get("type") or "function")
        if typ.startswith("openrouter:"):
            continue
        client.append(call)
    return client


def tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(function.get("name") or call.get("name") or call.get("type") or "unknown")


def extract_search_annotations(response_json: dict[str, Any]) -> list[Any]:
    annotations: list[Any] = []
    try:
        message = response_json["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return annotations
    if not isinstance(message, dict):
        return annotations
    raw = message.get("annotations")
    if isinstance(raw, list):
        annotations.extend(raw)
    return annotations


def add_usage(total: dict[str, Any], piece: dict[str, Any]) -> dict[str, Any]:
    """Sum numeric usage fields across rounds; keep last raw_usage."""
    out = dict(total)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        a = out.get(key)
        b = piece.get(key)
        if isinstance(a, (int, float)) or isinstance(b, (int, float)):
            out[key] = (a or 0) + (b or 0)
    a_cost = out.get("cost_usd")
    b_cost = piece.get("cost_usd")
    if isinstance(a_cost, (int, float)) or isinstance(b_cost, (int, float)):
        out["cost_usd"] = (a_cost or 0) + (b_cost or 0)
    new_searches = 0
    raw = piece.get("raw_usage")
    if isinstance(raw, dict):
        server = raw.get("server_tool_use") or {}
        if isinstance(server, dict) and isinstance(server.get("web_search_requests"), (int, float)):
            new_searches = int(server["web_search_requests"])
    out["web_search_requests"] = int(out.get("web_search_requests") or 0) + new_searches
    if piece.get("raw_usage") is not None:
        out["raw_usage"] = piece.get("raw_usage")
    return out


def usage_metrics(response_json: dict[str, Any]) -> dict[str, Any]:
    """Preserve the provider usage object while also exposing common fields."""
    usage = response_json.get("usage") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": usage.get("cost"),
        "raw_usage": usage,
    }
