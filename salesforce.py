from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from common import write_json

DEFAULT_ALIAS = "harness-org"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_LOGIN_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_BYTES = 8_000_000
DEFAULT_MAX_RECORDS = 200
DEFAULT_METADATA_CACHE_DIR = ".salesforce_schema_cache"
DEFAULT_METADATA_CACHE_TTL_HOURS = 48
METADATA_CACHE_VERSION = 2

SOQL_SELECT = re.compile(r"^\s*select\b", re.IGNORECASE)
SOBJECT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SOBJECT_CATEGORIES = {"all", "standard", "custom"}

ORG_PUBLIC_FIELDS = (
    "id",
    "alias",
    "username",
    "instanceUrl",
    "loginUrl",
    "connectedStatus",
    "isSandbox",
    "instanceName",
    "orgName",
    "namespacePrefix",
    "isDevHub",
    "isScratch",
)

DESCRIBE_OBJECT_FIELDS = (
    "name",
    "label",
    "labelPlural",
    "keyPrefix",
    "custom",
    "queryable",
    "createable",
    "updateable",
    "deletable",
    "feedEnabled",
    "searchable",
)
DESCRIBE_FIELD_FIELDS = (
    "name",
    "label",
    "inlineHelpText",
    "type",
    "nillable",
    "custom",
    "length",
    "byteLength",
    "precision",
    "scale",
    "unique",
    "externalId",
    "calculated",
    "defaultValue",
    "defaultedOnCreate",
    "nameField",
    "filterable",
    "sortable",
    "groupable",
    "restrictedPicklist",
    "dependentPicklist",
    "controllerName",
    "relationshipName",
    "referenceTo",
    "picklistValues",
)
DESCRIBE_CHILD_RELATIONSHIP_FIELDS = (
    "childSObject",
    "field",
    "relationshipName",
)


class SalesforceCliError(RuntimeError):
    """Bootstrap or CLI failures that should stop the suite."""


@dataclass
class SalesforceSession:
    alias: str
    sf_bin: str
    org_id: str | None = None
    instance_url: str | None = None
    username: str | None = None
    connected_status: str | None = None
    is_sandbox: bool | None = None
    org_name: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_records: int = DEFAULT_MAX_RECORDS
    metadata_cache_dir: Path | None = None
    metadata_cache_ttl_hours: int = DEFAULT_METADATA_CACHE_TTL_HOURS
    metadata_cache_enabled: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "org_id": self.org_id,
            "instance_url": self.instance_url,
            "username": self.username,
            "connected_status": self.connected_status,
            "is_sandbox": self.is_sandbox,
            "org_name": self.org_name,
        }


@dataclass
class SfCliResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    data: Any | None
    latency_ms: float
    parse_error: str | None = None


def find_sf_bin() -> str:
    for name in ("sf", "sf.cmd", "sf.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise SalesforceCliError(
        "Salesforce CLI (`sf`) was not found on PATH. "
        "Install it from https://developer.salesforce.com/tools/salesforcecli"
    )


def resolve_salesforce_settings(
    config_cfg: dict[str, Any] | None = None,
    suite_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(config_cfg, dict):
        block = config_cfg.get("salesforce")
        if isinstance(block, dict):
            merged.update(block)
    if isinstance(suite_cfg, dict):
        block = suite_cfg.get("salesforce")
        if isinstance(block, dict):
            merged.update({k: v for k, v in block.items() if v is not None})

    alias = (
        os.environ.get("SF_ORG_ALIAS")
        or merged.get("alias")
        or DEFAULT_ALIAS
    )
    expected = os.environ.get("SF_EXPECTED_ORG_ID") or merged.get("expected_org_id")
    if expected is not None:
        expected = str(expected).strip() or None

    confirm = merged.get("confirm")
    if confirm is None:
        confirm = True

    return {
        "alias": str(alias).strip() or DEFAULT_ALIAS,
        "expected_org_id": expected,
        "confirm": bool(confirm),
        "timeout_seconds": int(merged.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        "login_timeout_seconds": int(
            merged.get("login_timeout_seconds") or DEFAULT_LOGIN_TIMEOUT_SECONDS
        ),
        "max_output_bytes": int(merged.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES),
        "max_records": int(merged.get("max_records") or DEFAULT_MAX_RECORDS),
        "metadata_cache_dir": str(
            merged.get("metadata_cache_dir") or DEFAULT_METADATA_CACHE_DIR
        ),
        "metadata_cache_ttl_hours": int(
            merged.get("metadata_cache_ttl_hours") or DEFAULT_METADATA_CACHE_TTL_HOURS
        ),
        "metadata_cache_enabled": bool(merged.get("metadata_cache_enabled", True)),
    }


def parse_sf_json(stdout: str) -> Any:
    text = (stdout or "").strip()
    if not text:
        raise json.JSONDecodeError("Empty CLI output", stdout or "", 0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def public_org_identity(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ORG_PUBLIC_FIELDS:
        if key in result:
            out[key] = result[key]
    return out


def is_connected_identity(identity: dict[str, Any]) -> bool:
    if not identity.get("id"):
        return False
    status = str(identity.get("connectedStatus") or "").strip().lower()
    return status not in {"disconnected", "authdecryptederror", "not found"}


def run_sf(
    args: list[str],
    *,
    sf_bin: str,
    timeout_seconds: int,
    max_output_bytes: int,
    capture: bool = True,
) -> SfCliResult:
    argv = [sf_bin, *args]
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            capture_output=capture,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SalesforceCliError(
            f"Salesforce CLI timed out after {timeout_seconds}s: {' '.join(argv)}"
        ) from exc
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if capture and len(stdout.encode("utf-8", errors="replace")) > max_output_bytes:
        return SfCliResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=stdout[:1000],
            stderr=stderr,
            data=None,
            latency_ms=latency_ms,
            parse_error=(
                f"CLI output exceeded max_output_bytes={max_output_bytes}. "
                "Narrow the SOQL query (add LIMIT / fewer fields)."
            ),
        )
    data: Any | None = None
    parse_error: str | None = None
    if capture:
        try:
            data = parse_sf_json(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            parse_error = str(exc)
    return SfCliResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        data=data,
        latency_ms=latency_ms,
        parse_error=parse_error,
    )


def display_org(
    *,
    sf_bin: str,
    alias: str,
    timeout_seconds: int,
    max_output_bytes: int,
) -> tuple[dict[str, Any] | None, SfCliResult]:
    result = run_sf(
        ["org", "display", "--target-org", alias, "--json"],
        sf_bin=sf_bin,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    if result.parse_error or not isinstance(result.data, dict):
        return None, result
    if result.data.get("status") not in (0, "0"):
        return None, result
    raw = result.data.get("result")
    if not isinstance(raw, dict):
        return None, result
    identity = public_org_identity(raw)
    if not identity.get("id"):
        return None, result
    return identity, result


def login_web(*, sf_bin: str, alias: str, timeout_seconds: int) -> None:
    print()
    print(f"Opening Salesforce login for alias '{alias}'...")
    print("Complete the browser login, then return here.")
    try:
        completed = subprocess.run(
            [sf_bin, "org", "login", "web", "--alias", alias],
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SalesforceCliError(
            f"Salesforce web login timed out after {timeout_seconds}s"
        ) from exc
    if completed.returncode != 0:
        raise SalesforceCliError(
            f"`sf org login web --alias {alias}` exited with code {completed.returncode}"
        )


def format_org_identity(identity: dict[str, Any]) -> str:
    sandbox = identity.get("isSandbox")
    if sandbox is True:
        sandbox_label = "yes"
    elif sandbox is False:
        sandbox_label = "no"
    else:
        sandbox_label = "unknown"
    lines = [
        "Salesforce connection",
        f"  Org:      {identity.get('id') or 'unknown'}",
        f"  Name:     {identity.get('orgName') or 'unknown'}",
        f"  Instance: {identity.get('instanceUrl') or 'unknown'}",
        f"  User:     {identity.get('username') or 'unknown'}",
        f"  Alias:    {identity.get('alias') or 'unknown'}",
        f"  Sandbox:  {sandbox_label}",
        f"  Status:   {identity.get('connectedStatus') or 'unknown'}",
    ]
    return "\n".join(lines)


def prompt_org_confirmation(*, assume_yes: bool, confirm: bool) -> str:
    if assume_yes or not confirm:
        print("Continuing with this org (--yes or salesforce.confirm=false).")
        return "use"
    if not sys.stdin.isatty():
        raise SalesforceCliError(
            "Salesforce org confirmation requires a TTY. "
            "Re-run with --yes after verifying the org, or authenticate the alias first."
        )
    print()
    print("Use this connection? [Y/n/reconnect]")
    raw = input().strip().lower()
    if raw in ("", "y", "yes"):
        return "use"
    if raw in ("r", "reconnect", "relogin", "login"):
        return "reconnect"
    return "abort"


def session_from_identity(
    identity: dict[str, Any],
    *,
    settings: dict[str, Any],
    sf_bin: str,
) -> SalesforceSession:
    return SalesforceSession(
        alias=str(identity.get("alias") or settings["alias"]),
        sf_bin=sf_bin,
        org_id=identity.get("id"),
        instance_url=identity.get("instanceUrl"),
        username=identity.get("username"),
        connected_status=identity.get("connectedStatus"),
        is_sandbox=identity.get("isSandbox"),
        org_name=identity.get("orgName"),
        timeout_seconds=int(settings["timeout_seconds"]),
        max_output_bytes=int(settings["max_output_bytes"]),
        max_records=int(settings["max_records"]),
        metadata_cache_dir=Path(str(settings["metadata_cache_dir"])),
        metadata_cache_ttl_hours=int(settings["metadata_cache_ttl_hours"]),
        metadata_cache_enabled=bool(settings["metadata_cache_enabled"]),
    )


def ensure_expected_org(identity: dict[str, Any], expected_org_id: str | None) -> None:
    if not expected_org_id:
        return
    actual = str(identity.get("id") or "")
    if actual != expected_org_id:
        raise SalesforceCliError(
            f"Connected org id {actual or '(none)'} does not match "
            f"expected_org_id {expected_org_id}."
        )


def bootstrap_salesforce_session(
    *,
    config_cfg: dict[str, Any] | None,
    suite_cfg: dict[str, Any] | None,
    run_dir: Path,
    assume_yes: bool = False,
    sf_bin: str | None = None,
    display_fn: Callable[..., tuple[dict[str, Any] | None, SfCliResult]] | None = None,
    login_fn: Callable[..., None] | None = None,
    confirm_fn: Callable[..., str] | None = None,
) -> SalesforceSession:
    settings = resolve_salesforce_settings(config_cfg, suite_cfg)
    sf_bin = sf_bin or find_sf_bin()
    display = display_fn or display_org
    login = login_fn or login_web
    confirm = confirm_fn or prompt_org_confirmation
    alias = settings["alias"]
    login_performed = False

    identity, _result = display(
        sf_bin=sf_bin,
        alias=alias,
        timeout_seconds=settings["timeout_seconds"],
        max_output_bytes=settings["max_output_bytes"],
    )
    if identity is None or not is_connected_identity(identity):
        if assume_yes and not sys.stdin.isatty():
            raise SalesforceCliError(
                f"Salesforce alias '{alias}' is not connected. "
                "Authenticate it interactively (omit --yes) or run "
                f"`sf org login web --alias {alias}` first."
            )
        login(
            sf_bin=sf_bin,
            alias=alias,
            timeout_seconds=settings["login_timeout_seconds"],
        )
        login_performed = True
        identity, _result = display(
            sf_bin=sf_bin,
            alias=alias,
            timeout_seconds=settings["timeout_seconds"],
            max_output_bytes=settings["max_output_bytes"],
        )
        if identity is None or not is_connected_identity(identity):
            raise SalesforceCliError(
                f"Salesforce alias '{alias}' is still not connected after login."
            )

    if not identity.get("alias"):
        identity["alias"] = alias

    while True:
        print()
        print(format_org_identity(identity))
        ensure_expected_org(identity, settings.get("expected_org_id"))
        action = confirm(assume_yes=assume_yes, confirm=settings["confirm"])
        if action == "use":
            break
        if action == "reconnect":
            login(
                sf_bin=sf_bin,
                alias=alias,
                timeout_seconds=settings["login_timeout_seconds"],
            )
            login_performed = True
            identity, _result = display(
                sf_bin=sf_bin,
                alias=alias,
                timeout_seconds=settings["timeout_seconds"],
                max_output_bytes=settings["max_output_bytes"],
            )
            if identity is None or not is_connected_identity(identity):
                raise SalesforceCliError(
                    f"Salesforce alias '{alias}' is not connected after reconnect."
                )
            if not identity.get("alias"):
                identity["alias"] = alias
            continue
        raise SalesforceCliError("Salesforce org confirmation declined.")

    session = session_from_identity(identity, settings=settings, sf_bin=sf_bin)
    write_json(
        Path(run_dir) / "salesforce_session.json",
        {
            **session.public_dict(),
            "confirmed": True,
            "confirmed_at_utc": datetime.now(timezone.utc).isoformat(),
            "login_performed": login_performed,
            "expected_org_id": settings.get("expected_org_id"),
        },
    )
    return session


def cli_error_payload(result: SfCliResult) -> dict[str, Any]:
    if result.parse_error:
        return {
            "error": result.parse_error,
            "returncode": result.returncode,
            "stderr": (result.stderr or "")[-2000:],
        }
    data = result.data if isinstance(result.data, dict) else {}
    message = None
    if isinstance(data, dict):
        message = data.get("message") or data.get("name")
        if not message and isinstance(data.get("error"), str):
            message = data.get("error")
    return {
        "error": message or "Salesforce CLI command failed",
        "name": data.get("name") if isinstance(data, dict) else None,
        "status": data.get("status") if isinstance(data, dict) else None,
        "returncode": result.returncode,
        "stderr": (result.stderr or "")[-2000:],
    }


def pick_fields(record: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: record[key] for key in keys if key in record}


def slim_describe(result: dict[str, Any]) -> dict[str, Any]:
    out = pick_fields(result, DESCRIBE_OBJECT_FIELDS)
    fields = result.get("fields")
    slim_fields: list[dict[str, Any]] = []
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            item = pick_fields(field, DESCRIBE_FIELD_FIELDS)
            values = field.get("picklistValues")
            if isinstance(values, list):
                names = []
                for choice in values:
                    if isinstance(choice, dict) and choice.get("active", True):
                        names.append(choice.get("value"))
                    elif isinstance(choice, str):
                        names.append(choice)
                item["picklistValues"] = names
            slim_fields.append(item)
    out["field_count"] = len(slim_fields)
    out["fields"] = slim_fields
    child_relationships = result.get("childRelationships")
    slim_child_relationships: list[dict[str, Any]] = []
    if isinstance(child_relationships, list):
        for relationship in child_relationships:
            if not isinstance(relationship, dict):
                continue
            item = pick_fields(relationship, DESCRIBE_CHILD_RELATIONSHIP_FIELDS)
            if item.get("relationshipName"):
                slim_child_relationships.append(item)
    out["child_relationship_count"] = len(slim_child_relationships)
    out["childRelationships"] = slim_child_relationships
    return out


def truncate_records(
    records: list[Any], max_records: int
) -> tuple[list[Any], bool]:
    if len(records) <= max_records:
        return records, False
    return records[:max_records], True


class SalesforceCliProvider:
    """CLI-backed Salesforce transport. Model-facing tools call this, not `sf` directly."""

    def __init__(self, session: SalesforceSession):
        self.session = session

    def run(self, args: list[str]) -> SfCliResult:
        if "--target-org" in args or "-o" in args:
            raise SalesforceCliError("Internal CLI args must not set --target-org")
        full = [*args, "--target-org", self.session.alias]
        if "--json" not in full:
            full.append("--json")
        return run_sf(
            full,
            sf_bin=self.session.sf_bin,
            timeout_seconds=self.session.timeout_seconds,
            max_output_bytes=self.session.max_output_bytes,
        )

    def _cache_path(self, kind: str, key: str) -> Path | None:
        if not self.session.metadata_cache_enabled:
            return None
        cache_dir = self.session.metadata_cache_dir
        if cache_dir is None:
            return None
        cache_identity = "\x00".join(
            (
                str(self.session.org_id or self.session.instance_url or self.session.alias),
                str(self.session.username or "unknown-user"),
                kind,
                key,
            )
        )
        digest = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()
        return cache_dir / f"{kind}-{digest}.json"

    def _read_metadata_cache(self, kind: str, key: str) -> tuple[dict[str, Any], str] | None:
        path = self._cache_path(kind, key)
        if path is None or not path.is_file():
            return None
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("version") != METADATA_CACHE_VERSION:
                return None
            cached_at = datetime.fromisoformat(str(cached["cached_at"]))
            payload = cached["payload"]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        if cached_at.tzinfo is None:
            return None
        expires_at = cached_at + timedelta(
            hours=self.session.metadata_cache_ttl_hours
        )
        if datetime.now(timezone.utc) >= expires_at or not isinstance(payload, dict):
            return None
        return payload, cached_at.isoformat()

    def _write_metadata_cache(self, kind: str, key: str, payload: dict[str, Any]) -> str | None:
        path = self._cache_path(kind, key)
        if path is None:
            return None
        cached_at = datetime.now(timezone.utc).isoformat()
        try:
            write_json(
                path,
                {
                    "version": METADATA_CACHE_VERSION,
                    "cached_at": cached_at,
                    "payload": payload,
                },
            )
        except OSError:
            return None
        return cached_at

    @staticmethod
    def _with_cache_metadata(
        payload: dict[str, Any], *, hit: bool, cached_at: str | None
    ) -> dict[str, Any]:
        return {
            **payload,
            "cache": {
                "hit": hit,
                "cached_at": cached_at,
            },
        }

    def org_info(self) -> dict[str, Any]:
        identity, result = display_org(
            sf_bin=self.session.sf_bin,
            alias=self.session.alias,
            timeout_seconds=self.session.timeout_seconds,
            max_output_bytes=self.session.max_output_bytes,
        )
        if identity is None:
            return cli_error_payload(result)
        return identity

    def query(self, soql: str) -> dict[str, Any]:
        query = (soql or "").strip()
        if not query:
            return {"error": "query is required"}
        if ";" in query:
            return {"error": "SOQL must be a single SELECT statement (no semicolons)."}
        if not SOQL_SELECT.match(query):
            return {"error": "Only SOQL SELECT queries are allowed."}
        result = self.run(["data", "query", "--query", query])
        if result.parse_error or result.returncode != 0:
            return cli_error_payload(result)
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("status") not in (0, "0", None):
            return cli_error_payload(result)
        payload = data.get("result") if isinstance(data.get("result"), dict) else data
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            records = []
        kept, truncated = truncate_records(records, self.session.max_records)
        cleaned = []
        for row in kept:
            if isinstance(row, dict):
                cleaned.append({k: v for k, v in row.items() if k != "attributes"})
            else:
                cleaned.append(row)
        total_size = payload.get("totalSize") if isinstance(payload, dict) else len(records)
        return {
            "records": cleaned,
            "returned": len(cleaned),
            "total_size": total_size,
            "done": payload.get("done") if isinstance(payload, dict) else True,
            "truncated": truncated,
        }

    def describe(self, sobject: str, *, refresh: bool = False) -> dict[str, Any]:
        name = (sobject or "").strip()
        if not name:
            return {"error": "sobject is required"}
        if not SOBJECT_NAME.match(name):
            return {"error": f"Invalid sObject name: {name}"}
        cache_key = name.lower()
        if not refresh:
            cached = self._read_metadata_cache("describe", cache_key)
            if cached is not None:
                payload, cached_at = cached
                return self._with_cache_metadata(
                    payload, hit=True, cached_at=cached_at
                )
        result = self.run(["sobject", "describe", "--sobject", name])
        if result.parse_error or result.returncode != 0:
            return cli_error_payload(result)
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("status") not in (0, "0", None):
            return cli_error_payload(result)
        raw = data.get("result") if isinstance(data.get("result"), dict) else data
        if not isinstance(raw, dict):
            return {"error": "Describe returned no object information"}
        payload = slim_describe(raw)
        cached_at = self._write_metadata_cache("describe", cache_key, payload)
        return self._with_cache_metadata(payload, hit=False, cached_at=cached_at)

    def list_sobjects(
        self, category: str = "all", *, refresh: bool = False
    ) -> dict[str, Any]:
        requested_category = (category or "all").strip().lower()
        if requested_category not in SOBJECT_CATEGORIES:
            return {
                "error": (
                    "category must be one of: all, standard, custom"
                )
            }
        if not refresh:
            cached = self._read_metadata_cache("list_sobjects", requested_category)
            if cached is not None:
                payload, cached_at = cached
                return self._with_cache_metadata(
                    payload, hit=True, cached_at=cached_at
                )
        result = self.run(
            ["sobject", "list", "--sobject", requested_category]
        )
        if result.parse_error or result.returncode != 0:
            return cli_error_payload(result)
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("status") not in (0, "0", None):
            return cli_error_payload(result)
        raw = data.get("result") if "result" in data else data
        if not isinstance(raw, list):
            return {"error": "Object list returned no sObjects"}
        sobjects = [item for item in raw if isinstance(item, str)]
        payload = {
            "category": requested_category,
            "sobjects": sobjects,
            "returned": len(sobjects),
        }
        cached_at = self._write_metadata_cache(
            "list_sobjects", requested_category, payload
        )
        return self._with_cache_metadata(payload, hit=False, cached_at=cached_at)


def _require_provider(context: Any) -> SalesforceCliProvider | dict[str, Any]:
    provider = getattr(context, "salesforce", None)
    if provider is None:
        return {"error": "Salesforce is not connected for this run."}
    if not isinstance(provider, SalesforceCliProvider):
        return {"error": "Salesforce provider is not available."}
    return provider


def handle_org_info(_args: dict[str, Any], context: Any) -> dict[str, Any]:
    provider = _require_provider(context)
    if isinstance(provider, dict):
        return provider
    return provider.org_info()


def handle_query(args: dict[str, Any], context: Any) -> dict[str, Any]:
    provider = _require_provider(context)
    if isinstance(provider, dict):
        return provider
    if not isinstance(args, dict):
        return {"error": "arguments must be an object"}
    forbidden = {"target_org", "target-org", "alias", "org", "username"}
    present = forbidden.intersection(args)
    if present:
        return {
            "error": (
                "Org targeting is owned by the harness session; "
                f"do not pass {sorted(present)}"
            )
        }
    return provider.query(str(args.get("query") or ""))


def handle_describe(args: dict[str, Any], context: Any) -> dict[str, Any]:
    provider = _require_provider(context)
    if isinstance(provider, dict):
        return provider
    if not isinstance(args, dict):
        return {"error": "arguments must be an object"}
    refresh = args.get("refresh", False)
    if not isinstance(refresh, bool):
        return {"error": "refresh must be a boolean"}
    return provider.describe(
        str(args.get("sobject") or args.get("object") or ""), refresh=refresh
    )


def handle_list_sobjects(args: dict[str, Any], context: Any) -> dict[str, Any]:
    provider = _require_provider(context)
    if isinstance(provider, dict):
        return provider
    if not isinstance(args, dict):
        return {"error": "arguments must be an object"}
    refresh = args.get("refresh", False)
    if not isinstance(refresh, bool):
        return {"error": "refresh must be a boolean"}
    return provider.list_sobjects(
        str(args.get("category") or "all"), refresh=refresh
    )


SALESFORCE_SCHEMAS: dict[str, dict[str, Any]] = {
    "salesforce_query": {
        "type": "function",
        "function": {
            "name": "salesforce_query",
            "description": (
                "Run a read-only SOQL SELECT against the Salesforce org connected "
                "to this harness session. Do not pass an org alias or credentials; "
                "the harness always targets the session org. Prefer selective "
                "field lists and LIMIT."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SOQL SELECT statement.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    "salesforce_org_info": {
        "type": "function",
        "function": {
            "name": "salesforce_org_info",
            "description": (
                "Return the connected Salesforce org identity for this harness "
                "session (org id, instance URL, username, alias). Does not include "
                "access tokens."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        },
    },
    "salesforce_describe": {
        "type": "function",
        "function": {
            "name": "salesforce_describe",
            "description": (
                "Describe a Salesforce sObject in the connected org, including "
                "field labels, inline help text, SOQL capabilities, and child "
                "relationships. Use this before writing SOQL if you need schema."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sobject": {
                        "type": "string",
                        "description": "API name of the sObject, e.g. Account or MyThing__c.",
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": (
                            "Bypass the metadata cache and refresh it from Salesforce."
                        ),
                    },
                },
                "required": ["sobject"],
            },
        },
    },
    "salesforce_list_sobjects": {
        "type": "function",
        "function": {
            "name": "salesforce_list_sobjects",
            "description": (
                "List Salesforce sObject API names available in the connected org. "
                "Use category custom to discover non-standard objects before "
                "calling salesforce_describe or writing SOQL."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["all", "standard", "custom"],
                        "description": (
                            "Object category to return. Defaults to all."
                        ),
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": (
                            "Bypass the metadata cache and refresh it from Salesforce."
                        ),
                    },
                },
            },
        },
    },
}

SALESFORCE_HANDLERS: dict[str, Callable[[dict[str, Any], Any], dict[str, Any]]] = {
    "salesforce_query": handle_query,
    "salesforce_org_info": handle_org_info,
    "salesforce_describe": handle_describe,
    "salesforce_list_sobjects": handle_list_sobjects,
}

SALESFORCE_SHORTHAND: dict[str, str] = {
    "salesforce.query": "salesforce_query",
    "salesforce_query": "salesforce_query",
    "salesforce.org_info": "salesforce_org_info",
    "salesforce_org_info": "salesforce_org_info",
    "salesforce.describe": "salesforce_describe",
    "salesforce_describe": "salesforce_describe",
    "salesforce.list_sobjects": "salesforce_list_sobjects",
    "salesforce_list_sobjects": "salesforce_list_sobjects",
}

NOT_IMPLEMENTED_TOOLS: dict[str, str] = {
    "salesforce.apex": "Apex execution is not enabled in this harness yet.",
    "salesforce_apex": "Apex execution is not enabled in this harness yet.",
    "salesforce.metadata_read": (
        "Metadata retrieve/read is not enabled in this harness yet. "
        "Use salesforce.describe for sObject schema."
    ),
    "salesforce_metadata_read": (
        "Metadata retrieve/read is not enabled in this harness yet. "
        "Use salesforce.describe for sObject schema."
    ),
    "salesforce.metadata_deploy": "Metadata deploy is not enabled in this harness yet.",
    "salesforce_metadata_deploy": "Metadata deploy is not enabled in this harness yet.",
    "salesforce.data_write": "Salesforce data writes are not enabled in this harness yet.",
    "salesforce_data_write": "Salesforce data writes are not enabled in this harness yet.",
    "salesforce.create": "Salesforce data writes are not enabled in this harness yet.",
    "salesforce.update": "Salesforce data writes are not enabled in this harness yet.",
    "salesforce.delete": "Salesforce data writes are not enabled in this harness yet.",
}


def not_implemented_message(key: str) -> str | None:
    return NOT_IMPLEMENTED_TOOLS.get(key.strip())


def schema_for_shorthand(key: str) -> dict[str, Any] | None:
    function_name = SALESFORCE_SHORTHAND.get(key.strip())
    if not function_name:
        return None
    schema = SALESFORCE_SCHEMAS.get(function_name)
    return deepcopy(schema) if schema else None


def is_salesforce_function(name: str) -> bool:
    return name in SALESFORCE_HANDLERS
