# Salesforce Open-Model Benchmark Harness

A small Python harness for benchmarking OpenRouter-hosted models on Salesforce-oriented tasks.

Tests are model-agnostic. A **test-set** (suite) selects the tests to run and
supplies the subject model/provider for the whole suite. Pipeline models
(normalizer, and later an LLM judge) live in a global `config.yaml` so every
suite uses the same extractors. Per-test files keep their own prompts, expected
values, and generation parameters (temperature, max tokens).

## What it does

1. Loads global `config.yaml` (normalizer and evaluator models).
2. Loads a YAML test-set from `test-sets/`.
3. For each listed test, overlays the suite subject model and the global pipeline models.
4. Resolves `${name}` placeholders from `.env`.
5. Sends the benchmark prompt to OpenRouter.
6. Saves the exact request body and exact response body.
7. Records latency, token usage, and provider-reported cost when available.
8. Runs a separate normalizer CLI that maps the subject model answer to JSON.
9. Runs a separate deterministic evaluator CLI against expected values.
10. Writes a suite `summary.json` with pass/fail for every test.

The API key is used for the HTTP Authorization header but is redacted from log metadata.

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```

Edit `.env`:

```text
openrouter_key=YOUR_REAL_KEY
```

Then edit:

- `test-sets/harness_smoke.yaml` → `provider.model` (the model under test)
- `config.yaml` → `normalizer.model` (stable extractor) and, when you need it, `evaluator.model` / `evaluator.mode`

## Who owns which model

| Path | Owns |
| --- | --- |
| `config.yaml` | Normalizer model/provider. Evaluator mode (`deterministic` now, `judge` reserved) and judge model/provider for later. Salesforce CLI alias / optional org-id pin. |
| `test-sets/*.yaml` | Suite name/description, subject model/provider, which tests run (grouped by category) |
| `tests/*.yaml` | Prompt, expected answers, schema, temperature / max_tokens, tools |

The same test file can be listed in multiple test-sets to compare models without copying prompts.

## Commands

### Full suite run

Runs every test in the test-set: inference, normalization, and evaluation:

```bash
python runner.py --suite test-sets/harness_smoke.yaml
```

Subset of a suite:

```bash
python runner.py --suite test-sets/harness_smoke.yaml --category harness_health_check
python runner.py --suite test-sets/harness_smoke.yaml --category web_search
python runner.py --suite test-sets/harness_smoke.yaml --only harness_test_001
python runner.py --suite test-sets/harness_smoke.yaml --yes   # skip Salesforce org prompt when already connected
```

### Inference only

```bash
python runner.py --suite test-sets/harness_smoke.yaml --no-pipeline
```

### Normalizer only

Point it at a per-test run directory created by the runner:

```bash
python normalizer.py \
  --config config.yaml \
  --suite test-sets/harness_smoke.yaml \
  --test tests/acme_lookup.yaml \
  --run-dir runs/harness_smoke/<run-id>/salesforce_account_lookup/account_lookup_001
```

### Evaluator only

```bash
python evaluator.py \
  --config config.yaml \
  --suite test-sets/harness_smoke.yaml \
  --test tests/acme_lookup.yaml \
  --run-dir runs/harness_smoke/<run-id>/salesforce_account_lookup/account_lookup_001
```

### Unit tests

```bash
python -m unittest discover -s unit_tests -v
```

## Run artifacts

A suite run is stored under `runs/<suite-id>/<timestamp>/`:

```text
suite.yaml
config.yaml
resolved_suite_redacted.yaml
resolved_config_redacted.yaml
salesforce_session.json   # when a selected test uses Salesforce tools
summary.json
<category>/<test-id>/
  test.yaml
  merged_test.yaml
  resolved_test_redacted.yaml
  source.json
  request.json
  ...
```

Each per-test directory contains files such as:

```text
request.json
request_meta.json
response.raw.txt
response.json
response_meta.json
answer.txt
metrics.json
tool_trace.json
rounds/00/request.json
rounds/00/response.json
rounds/00/annotations.json   # when search citations are present
rounds/00/tool_results.json  # when client tools ran

normalizer_request.json
normalizer_response.raw.txt
normalizer_response.json
normalizer_metrics.json
normalized.json

evaluation.json
```

`request.json` is the actual JSON body sent to OpenRouter.
`response.raw.txt` is the exact HTTP response body returned by the provider.

## Important design choice

The subject model is allowed to answer naturally. The normalizer model is deliberately
blind to the expected answers and is told only to extract what the subject model actually
said. This avoids accidentally "repairing" an incorrect benchmark answer.

The evaluator is currently deterministic (`evaluator.mode: deterministic` in
`config.yaml`) and reads only `normalized.json` plus the expectations in the YAML
test definition. `evaluator.mode: judge` is reserved for a future LLM-as-judge
path that will use `evaluator.model` from the same file.

## Tools

`request.tools` supports two kinds of tools:

- **Server tools** (OpenRouter executes them): YAML shorthand such as `web_search`
  is translated to `{ "type": "openrouter:web_search" }`. The harness does not
  implement search. OpenRouter may search 0–N times inside a single HTTP request
  (`max_tool_calls`).
- **Client tools** (this repo executes them): YAML shorthand such as
  `salesforce.query` is expanded into an OpenAI `function` tool. The runner
  loops up to `request.max_rounds`, runs the function locally, and sends the
  result back to the model.

Every inference round is logged under `rounds/` (request, response, annotations,
usage, and `tool_results.json` when client tools ran). `request.json` /
`response.json` / `answer.txt` are the first request and the final assistant
answer so the normalizer and evaluator stay unchanged.

### Salesforce CLI tools

Salesforce access is a **session** concern, not a per-test concern. If any
selected test lists a Salesforce tool, the suite:

1. Looks up `sf` on `PATH`.
2. Runs `sf org display --target-org <alias> --json`.
3. If that alias is missing or disconnected, runs `sf org login web --alias <alias>`.
4. Prints org id / instance / user / alias and asks you to confirm (or reconnect).
5. Writes `salesforce_session.json` in the suite run directory (no access tokens).
6. Injects `--target-org <alias>` on every later CLI call. Tests and the model
   cannot choose the org.

Alias and optional org-id pin live in `config.yaml` under `salesforce:` (override
with `SF_ORG_ALIAS` / `SF_EXPECTED_ORG_ID`). Use `--yes` to skip the confirmation
prompt when the alias is already connected.

Implemented shorthands:

| YAML | Function | CLI |
| --- | --- | --- |
| `salesforce.query` | `salesforce_query` | `sf data query` |
| `salesforce.org_info` | `salesforce_org_info` | `sf org display` |
| `salesforce.describe` | `salesforce_describe` | `sf sobject describe` |

Not implemented (the test fails at load if you list them): Apex, metadata
read/deploy, and data writes.

Example:

```yaml
request:
  tools:
    - salesforce.query
```
