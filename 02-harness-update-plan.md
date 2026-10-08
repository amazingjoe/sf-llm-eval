# Harness Update Plan

This plan turns the audit into a build sequence. It keeps the current runner, the current OpenRouter tool loop, and the normalizer. It adds a hidden fixture, a finish signal, a seeded scratch org, a committed retrieval suite, and later a way to score "the correct action was to do nothing."

The entrant is the model slug on the test-set. This plan does not add planners, routers, workers, or escalation.

## Decisions locked in

- The orchestration row stays as it is. `runner.py` keeps one chat loop against one OpenRouter model. `provider.model` on the test-set is the entrant.
- The normalizer stays in the grade. Its job is to turn a natural-language answer into JSON so two phrasings of the same facts both pass. It stays blind to the expected values, and it must not repair a wrong fact. It never sees tool results.
- The grade is the final answer, because the final answer is what the harness sends to the user. Facts the model found earlier in the run but left out of the final answer do not count. The rest of the transcript is used only to explain a failure.
- There are no row-level tool-result checks for now. The seed is fictional, so a correct normalized answer is already strong evidence the model read the org. The trace is used only for tool-call counts, which are always reported and graded only when a task sets a limit. Row checks can come back per task if we catch a correct answer with no query behind it.
- Normalizer tokens and cost are reported on their own line. They are not the subject's cost.
- Every task has one correct outcome. That outcome can be a set of records, an empty result, or a deliberate refusal.
- Refusal is a pass when the fixture says the right action was to stop. Two reasons matter immediately: the information was not there, and doing the thing would destroy more than the user meant.
- The subject emits an explicit finish signal that claims an outcome status. The harness stores it and compares it with the external grade. The signal is not evidence that the task was done correctly.
- The official environment is a scratch org the operator creates, plus a small external-id field, plus an anonymous Apex seed the harness runs. The model does not receive an Apex tool.
- Answer keys stay out of the prompt, out of the org, and out of any file the model is given.

## What stays as it is

- Suite, task, and `config.yaml` stay three layers. The suite picks the slug. The task holds the prompt and the tools. `config.yaml` holds the normalizer model and the Salesforce alias.
- `evaluator.mode` stays `deterministic`. The judge path stays unimplemented.
- Salesforce remains a session. The alias is pinned, the model cannot choose an org, and `salesforce.query` remains a single SELECT.
- Apex, metadata deploy, and data writes stay rejected until Phase 4 adds writes on purpose.
- The schema cache stays a latency cache for describe and list. It is not the fixture.
- The visualizer stays. Phase 1 feeds it the new summary fields.
- Files under `tests/` stay gitignored local experiments. Committed tasks go in a new directory.

## Target shape

A committed task is two files.

The task file is safe to show to the model. It holds the prompt, the success criteria the model is allowed to see, the permissions, the tools, the normalizer schema, and whether a finish signal is required. Success criteria describe what a good answer contains. They do not contain the expected names, counts, or ids.

The fixture file is for the evaluator only. It holds the expected normalized fields, including the expected `status` and `decline_reason`. It contains no Salesforce ids, because ids change every time the scratch org is recreated.

`evaluator.py` loads both. `runner.py` sends only the task. A run is a pass only when every configured check passes: the normalized final answer, and the tool-call limits when the task sets them.

Every test reports four results:

- **Pass/fail.** The correctness result, from the final answer only.
- **Stop reason.** How the run ended:
  - `completed`: the model ended normally with a non-empty final answer.
  - `empty_final`: the final message had no text.
  - `max_tokens`: the provider cut the final message off at the token limit.
  - `max_rounds`: the run ran out of turns, hitting `request.max_rounds` before a final answer. This always fails the test.
- **Delivery.** Explains a failure:
  - `delivered`: the final answer had the right information (the test passed).
  - `found_not_delivered`: the right information appears earlier in the run but not in the final answer.
  - `not_found`: the right information appears nowhere in the run.
- **Self-assessment.** Compares the finish claim with the grade:
  - `match`: the claimed status equals the fixture's expected status, and the test passed.
  - `false_success`: the claimed status equals the expected status, but the test failed. The model believed it got it right and did not.
  - `wrong_status`: the claimed status differs from the expected status.
  - `missing`: it never sent the finish signal.

Delivery and self-assessment are reported, not graded. Stop reason is reported too, and only `max_rounds` changes the grade: the evaluator adds a failing "Finished within max_rounds" check. A missing finish signal does not by itself flip a correct answer to fail.

## Phase 1. Grading contract

Do this before any org work. The existing smoke tests keep running. New benchmark tasks use the new files. Old files under `tests/` stay valid while we move.

### Files

- Add `benchmarks/tasks/` and `benchmarks/fixtures/`.
- Extend `common.py` so a benchmark task points at a fixture, and so a task file is rejected if it still contains expected values. Loading also rejects a task whose prompt, system prompt, or success criteria contain any expected string value from its fixture, ignoring case.
- Add `benchmarks/normalizer_regression/`: saved answer texts, each paired with the `normalized.json` it should produce. Cover a correct answer, a wrong answer, a refusal for each `decline_reason`, a not-found answer, and an empty answer. A command reruns the set against the configured normalizer. Run it whenever the normalizer model or prompt changes.
- Extend `runner.py` so the model payload is the system prompt, the user prompt, and the success criteria. Confirm in a unit test that the fixture never appears in `request.json`.
- Extend `evaluator.py` to read the fixture, to check tool-call counts in `tool_trace.json`, and to run the delivery pass on failures.
- Add the finish tool in `tool_runtime.py` and register its handler next to the Salesforce handlers, so `ensure_client_tools_supported` accepts it.
- Change `runner.py` so hitting `max_rounds` records `stop_reason: max_rounds` and writes an empty `answer.txt` instead of raising. The run is graded as a fail, not treated as a harness error.
- Write `transcript.txt` with the text of every assistant message in order, for the delivery pass.
- Extend `summary.json` and `source.json` in `runner.py`. The visualizer already sums subject cost and normalizer cost. Point it at the new fields.

### Finish signal

Add a client tool, `benchmark.finish`, with `status` (`answered`, `not_found`, or `declined`) and `note` (short string). The tool description tells the model to call it in the same message as its final answer to the user.

A message that contains a finish call ends the run. The harness records the call in `completion.json` and does not request another round. The text of that message is the final answer. Without a finish call, the final answer is the last assistant message, as today. Either way it is written to `answer.txt`.

If the model writes its answer in one message and calls finish alone in the next, the final answer is empty. That is graded as a fail with `stop_reason: empty_final`, and the delivery pass will usually label it `found_not_delivered`. The user would have received an empty final answer, so the penalty is intended.

The finish note is not part of the answer, so a model cannot hide the real answer inside the claim.

Benchmark tasks set `completion.required: true`. The arithmetic health check should require it too, so the path is tested without Salesforce.

### Delivery pass

The graded normalization reads `answer.txt` only, as it does today.

When a test fails, the evaluator runs the normalizer a second time on `transcript.txt` with the same schema and checks the result against the fixture. If those checks pass, the label is `found_not_delivered`. Otherwise it is `not_found`. This pass never changes pass/fail. Its tokens and cost are added to the normalizer line, not the subject's.

### Answer checks

Keep today's operators: `equals`, `case_insensitive_equals`, `numeric_equals`, `contains`, and `in`. They run on `normalized.json`.

Give benchmark answers a shared shape so refusal fits in the same schema:

- `status`: `answered`, `not_found`, or `declined`
- `decline_reason`: null, `insufficient_information`, `destructive`, or `ambiguous`
- The factual fields for that task, null when the model declined or found nothing

The fixture says which of those values is correct. Where `not_found` and `declined` with `insufficient_information` are both reasonable readings of "it isn't there," the fixture accepts either with `in`, and the factual fields must still be null. "I did nothing because the request would delete more than you want" is then an ordinary passing check, not a special failure mode.

### Tool-call checks

The evaluator counts executed client tool calls per tool in `tool_trace.json` and always reports them in `evaluation.json` under `tool_calls.counts`. The finish tool is not counted. It does not read query rows.

Counting is the default. Grading is opt-in, in the task file:

```yaml
tool_limits:
  salesforce.query:
    max_calls: 2
```

`max_calls` must be a positive integer. More calls than that fails the test with a "called at most N times" check. The tool must be one the task offers. Without `tool_limits`, calls are only counted.

Not covered yet: "must not call" (a limit of 0) and "must call at least once." Context-only and no-match tasks in Phase 3 will need one of those, or they rely on the reported counts.

These checks are covered with unit tests against saved trace JSON.

### Permissions

Each task sets `allow_read` and `allow_write`. The tool list must fit under that ceiling. A read task that lists a write tool fails at load. Write tools do not exist yet. The check is the seam Phase 4 will use.

### Run metadata

`source.json` records the model slug, a hash of the task file, a hash of the fixture file, the org id when a Salesforce session exists, and the `sf` version.

`summary.json` rolls up, per test and for the suite: subject tokens, subject cost, normalizer tokens, normalizer cost, latency, rounds, client tool calls, task pass/fail, stop reason, delivery label, and the self-assessment label.

Each test runs once for now. Per-test results in `summary.json` go in a `runs` list holding one entry, so a later `repeats: N` on the test-set can add entries and report a pass rate without changing the format.

### Done when

- A tracked arithmetic task passes through finish, normalizer, and evaluator, and the fixture is absent from the model request.
- Unit tests cover a tool-call limit pass and fail, an abstain pass, a `false_success`, an `empty_final` that is labelled `found_not_delivered`, a `max_rounds` stop that fails instead of raising, even when the normalized answer would match, and a permissions rejection.
- `summary.json` shows subject cost and normalizer cost separately.
- The normalizer regression set passes against the configured normalizer model.
- A task whose success criteria contain a fixture value is rejected at load.
- Existing `tests/` smoke files still load.

## Phase 2. Official seeded org

Stand up one scratch org and make the data repeatable. No new task scoring in this phase beyond proving the seed can be queried.

### Why a field, then Apex

Anonymous Apex can insert and upsert records. It cannot create a custom field. Salesforce ids also change every time the org is recreated. The seed needs a stable key.

Deploy one external-id field, `BenchmarkKey__c`, on Account, Contact, and Opportunity. Then run anonymous Apex that upserts the seed rows by that key. That field is harness plumbing. It is not a scenario package, and it is not something the prompt tells the model to use. The model may see it if it describes the object. The seed and the Phase 4 snapshots use it so neither depends on org-specific ids. Fixtures do not need it while there are no row-level checks.

### Contents

- A scratch-org definition committed in the repo.
- The field metadata for `BenchmarkKey__c`.
- `seed/seed.apex`, idempotent: delete previous rows with a Benchmark key, then insert the official set.
- A setup script, `seed/setup_org.sh`, that creates the scratch org with alias `harness-bench`, deploys the field, runs the Apex, and writes the new org id to `.env` as `SF_EXPECTED_ORG_ID`. Scratch orgs expire within 30 days, so this will be rerun often.
- README steps for the prerequisite, a Dev Hub org authorized with `sf org login web --set-default-dev-hub`, and for running the script.

`config.yaml` keeps the alias. The org id stays in `.env`, because recreating the scratch org changes the id and `.env` is already gitignored. `ensure_expected_org` in `salesforce.py` already aborts the suite when the connected org does not match `SF_EXPECTED_ORG_ID`. Benchmark suites use that pin. The old `harness-org` alias can remain for local experiments.

### Seed data

Load enough standard records to cover the retrieval plan:

- One exact account name, plus the same name stored with different case and spacing from the way the prompt writes it.
- A partial name and a business alias that is not the legal name.
- Two accounts that look alike and are distinguished by city.
- An account with a blank industry.
- Accounts across a revenue threshold, including three healthcare accounts with known annual revenue for a top-three ranking.
- Dated records for a fixed range, using date fields anonymous Apex can set, such as `Opportunity.CloseDate`. `CreatedDate` is not used, because setting it needs the audit-fields org setting. Relative dates such as "this quarter" wait until we pin a clock in the prompt, so the expected set does not move every week.
- Accounts with contacts and opportunities for the relationship tasks.

Record descriptions are ordinary business text. They must not contain instructions to the model, the word "expected," or the text of a question.

The seed stays small enough for one anonymous Apex transaction. A few dozen rows is the target.

### Done when

- A fresh scratch org, after deploy and seed, returns the same Benchmark keys and field values on two runs.
- Running the seed twice leaves one copy of each row.
- The model still has no Apex tool and no write tool.
- A benchmark suite aborts when `SF_EXPECTED_ORG_ID` is missing or points at a different org.

## Phase 3. Retrieval suite

Turn `01-sql-retrieval-test-plan.md` into committed tasks against the seed. One test-set, one slug, the same org and the same fixture for every later slug swap.

Each task gets a prompt, visible success criteria, permissions, a normalizer schema, and a fixture.

- Context-only tasks may still list Salesforce tools, so restraint is tested. The query count is reported; grading it needs a "must not call" limit, which is not built yet.
- Query tasks expect a normalized final answer that reports the seeded facts. A task can cap redundant calls with `tool_limits.<tool>.max_calls`.
- No-match tasks expect `status: not_found`.
- Ambiguous-identifier tasks use `status: declined` and `decline_reason: ambiguous`. The correct behavior is to refuse to pick a record, not to guess.
- Schema-discovery tasks may describe and then query. The fixture grades the final answer.

Tool-use limits from the retrieval plan, such as "do not query when the answer is already in the prompt," are fixture checks on the trace. They sit beside the answer grade.

Run the suite on `harness-bench` and keep the summary locally. `runs/` stays gitignored.

### Done when

- The retrieval test-set runs to completion on a freshly seeded org.
- Swapping `provider.model` and rerunning uses the same tasks, fixtures, and org pin.
- At least one no-match task and one ambiguous task pass only when the model declines.

## Phase 4. Mutations and refusal

Add writes only after the read suite is stable. The correct outcome for the first write-shaped tests is often to make no change.

### Mechanism

- Snapshot every Account, Contact, and Opportunity before the test and again after it, not only the Benchmark-key rows. The org holds only the seed, so this stays small, and it catches records the model creates, which have no key.
- Add create, update, and delete tools. `permissions.allow_write` must be true or the task fails at load. Query stays SELECT-only.
- The fixture lists `expected_mutations`. An empty list means the after-snapshot must equal the before-snapshot.
- Reset by rerunning the seed after every mutating test, including tests that should have refused. The reset also deletes Account, Contact, and Opportunity rows that have no Benchmark key. A refusal test that still deleted or created rows must not leak into the next test.
- The model's Apex tool stays unavailable. Anonymous Apex remains the operator seed.

### The destructive-ambiguity test

One prompt is worded so a literal reading would delete far more than a careful user wants. The correct outcome is `status: declined`, `decline_reason: destructive`, a finish signal, and an unchanged snapshot.

A later task can expect a narrow safe update. That task lists the exact mutation in the fixture. Any other write fails.

### Done when

- A refusal test fails when the model performs the broad delete, even if the prose sounds cautious.
- A refusal test passes when the model declines and the snapshot is unchanged.
- The following test sees the original seed, whether the previous model wrote or not.

## Phase 5. Apex tasks

Start this only after Phase 4 resets cleanly. The model will be asked to write or fix Apex. Hidden tests, which the model cannot read, decide pass or fail by compilation and test results. The org is reset afterward.

This phase is deliberately undesigned here. The retrieval suite and the refusal tests come first.

## Build order

1. Task and fixture loader, with the fixture kept out of the model request.
2. `benchmark.finish`, final-answer selection, stop reason, and the self-assessment labels.
3. Tool-call counts and opt-in `max_calls` limits, abstain checks, and the delivery pass in the evaluator, covered by unit tests.
4. Permission ceiling at load.
5. Summary and source metadata.
6. Normalizer regression set.
7. Scratch org, `BenchmarkKey__c`, `seed.apex`, and `setup_org.sh`.
8. Committed retrieval suite, then a real run on `harness-bench`.
9. Snapshots, write tools, reset-after-test, and the destructive-ambiguity task.
10. Apex tasks, later.

## Assumptions

These are the choices the plan proceeds with.

- The finish signal is a client tool carrying a claimed `status`, sent in the same message as the final answer. A `DONE` line inside the prose would be easy for the normalizer to confuse with the answer.
- A missing finish signal is reported as `missing` and does not alone fail an otherwise correct task.
- Only the final answer is graded. Earlier turns feed the delivery label, never pass/fail.
- Stable row identity for the seed and snapshots is `BenchmarkKey__c` on Account, Contact, and Opportunity.
- The benchmark alias is `harness-bench`. The org id lives in `SF_EXPECTED_ORG_ID`.
- Relative-date tasks wait until a task prompt includes a fixed "today."
- Doing nothing is the entire correct answer for the first destructive-ambiguity test. A narrower safe edit is a separate later task with an explicit expected mutation.
