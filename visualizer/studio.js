const CHECK_OPS = [
  "equals",
  "case_insensitive_equals",
  "numeric_equals",
  "contains",
  "in",
];

function fieldValue(id) {
  const el = document.getElementById(id);
  return el ? el.value : "";
}

function fieldChecked(id) {
  const el = document.getElementById(id);
  return Boolean(el && el.checked);
}

function parseJsonField(id, fallback) {
  const raw = fieldValue(id).trim();
  if (!raw) return fallback;
  return JSON.parse(raw);
}

function parseExpected(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return "";
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function expectedToInput(value) {
  if (value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function isBenchmarkEditor() {
  return Boolean(state.editor && state.editor.source === "benchmarks");
}

function blankFixture() {
  return {
    version: 1,
    test_id: "",
    checks: [{ name: "", field: "", op: "equals", expected: "" }],
  };
}

function blankTest(source = "tests") {
  const data = blankLocalTest();
  if (source === "benchmarks") delete data.pipeline.evaluator.checks;
  return data;
}

function blankLocalTest() {
  return {
    version: 1,
    test: {
      id: "",
      name: "",
      prompt: "",
      success_criteria: "",
      metadata: {
        category: "",
        difficulty: "easy",
        benchmark_group: "",
        notes: "",
      },
    },
    request: {
      system_prompt: "You are an assistant. Complete the user's task accurately and concisely.\n",
      parameters: { temperature: 0, max_tokens: 400, top_p: 1 },
      extra_payload: {},
      headers: { "X-Title": "Salesforce Open Model Benchmark" },
      timeout_seconds: 120,
      tools: [],
    },
    pipeline: {
      normalizer: {
        enabled: true,
        script: "normalizer.py",
        parameters: { temperature: 0, max_tokens: 400 },
        instructions:
          "Extract only facts explicitly present in the subject model's answer. Do not fix the answer, add outside knowledge, or infer missing values. If a requested value is not present, use null.",
        schema: {
          type: "object",
          additionalProperties: false,
          required: [],
          properties: {},
        },
        extra_payload: {},
        timeout_seconds: 120,
      },
      evaluator: {
        enabled: true,
        script: "evaluator.py",
        require_schema_valid: true,
        checks: [{ name: "", field: "", op: "equals", expected: "" }],
      },
    },
  };
}

function blankSuite() {
  return {
    version: 1,
    suite: { id: "", name: "", description: "" },
    provider: {
      endpoint: "https://openrouter.ai/api/v1/chat/completions",
      api_key: "${openrouter_key}",
      model: "",
      headers: { "X-Title": "Salesforce Open Model Benchmark" },
      timeout_seconds: 120,
      extra_payload: {},
    },
    categories: [{ id: "", emoji: "", name: "", tests: [] }],
    logging: { output_dir: "runs" },
  };
}

function schemaRows(schema) {
  const properties = (schema && schema.properties) || {};
  const required = new Set((schema && schema.required) || []);
  const names = Object.keys(properties);
  if (!names.length) return [{ name: "", types: "string, null", required: true }];
  return names.map((name) => {
    const spec = properties[name] || {};
    const types = Array.isArray(spec.type) ? spec.type.join(", ") : spec.type || "string";
    return { name, types, required: required.has(name) };
  });
}

function rowsToSchema(rows) {
  const properties = {};
  const required = [];
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) continue;
    const types = row.types
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
    properties[name] = { type: types.length === 1 ? types[0] : types };
    if (row.required) required.push(name);
  }
  return {
    type: "object",
    additionalProperties: false,
    required,
    properties,
  };
}

function jsonPretty(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function filterLibrary(items, fields) {
  const query = state.query.trim().toLowerCase();
  if (!query) return items;
  return items.filter((item) =>
    fields
      .map((field) => item[field])
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(query)
  );
}

async function ensureWorkspace() {
  if (!state.workspace) {
    state.workspace = await fetchJson("/api/workspace");
    state.tests = state.workspace.tests || [];
    state.suites = state.workspace.suites || [];
  }
  return state.workspace;
}

async function renderHub() {
  try {
    const ws = await ensureWorkspace();
    app.innerHTML = `
      <p class="status-line">Configure the pipeline, write tests, assemble suites, then inspect runs.</p>
      <div class="hub">
        <a class="hub-card" href="#runs">
          <span class="kicker">Inspect</span>
          <h2><span class="hub-emoji">📊</span> Runs</h2>
          <p>Assay suite results, expand tests, and read answers against expected fields.</p>
          <div class="count">${ws.run_count} suite run${ws.run_count === 1 ? "" : "s"}</div>
        </a>
        <a class="hub-card" href="#tests">
          <span class="kicker">Author</span>
          <h2><span class="hub-emoji">📝</span> Tests</h2>
          <p>Create and edit the YAML tests: prompts, schemas, and field checks.</p>
          <div class="count">${ws.tests.length} test file${ws.tests.length === 1 ? "" : "s"}</div>
        </a>
        <a class="hub-card" href="#suites">
          <span class="kicker">Assemble</span>
          <h2><span class="hub-emoji">🗂️</span> Suites</h2>
          <p>Choose the subject model and which tests run together in a test-set.</p>
          <div class="count">${ws.suites.length} suite${ws.suites.length === 1 ? "" : "s"}</div>
        </a>
        <a class="hub-card" href="#config">
          <span class="kicker">Pipeline</span>
          <h2><span class="hub-emoji">⚙️</span> Config</h2>
          <p>Global normalizer and evaluator settings used by every suite.</p>
          <div class="count">${escapeHtml(ws.normalizer_model || "no model")} · ${escapeHtml(
            ws.evaluator_mode || "deterministic"
          )}</div>
        </a>
      </div>`;
  } catch (err) {
    app.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

async function renderTests() {
  try {
    if (!state.tests.length) await ensureWorkspace();
    const tests = filterLibrary(state.tests, [
      "name",
      "test_id",
      "filename",
      "category",
      "difficulty",
      "benchmark_group",
    ]);
    const card = (test) => `
            <button type="button" class="library-card" data-open-file="${escapeHtml(
              test.source || "tests"
            )}/${escapeHtml(test.filename)}">
              <div>
                <strong>${escapeHtml(test.name || test.test_id || test.filename)}</strong>
                <div class="meta">${escapeHtml(test.path || test.filename)} · ${escapeHtml(test.test_id || "")}</div>
              </div>
              <div class="meta">${escapeHtml(test.difficulty || "")}${
                test.check_count ? ` · ${test.check_count} checks` : ""
              }${test.used_in?.length ? `<br>in ${escapeHtml(test.used_in.join(", "))}` : ""}</div>
            </button>`;
    const section = (title, blurb, href, label, items) => `
      <div class="list-toolbar">
        <p class="muted"><strong>${title}.</strong> ${blurb}</p>
        <a class="primary" href="${href}">${label}</a>
      </div>
      ${
        items.length
          ? `<div class="library-list">${items.map(card).join("")}</div>`
          : `<div class="empty">No ${title.toLowerCase()} match that filter.</div>`
      }`;
    app.innerHTML = `
      ${section(
        "Benchmark tasks",
        "Committed tasks in <code>benchmarks/tasks/</code>. Their answer keys live in <code>benchmarks/fixtures/</code>, which the model never sees.",
        "#benchmarks/new",
        "New benchmark task",
        tests.filter((test) => test.source === "benchmarks")
      )}
      ${section(
        "Local tests",
        "Files in <code>tests/</code> are gitignored local experiments. Suites pick which ones to run.",
        "#tests/new",
        "New local test",
        tests.filter((test) => test.source !== "benchmarks")
      )}`;
  } catch (err) {
    app.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

async function renderSuites() {
  try {
    if (!state.suites.length) await ensureWorkspace();
    const suites = filterLibrary(state.suites, ["name", "suite_id", "filename", "model", "description"]);
    app.innerHTML = `
      <div class="list-toolbar">
        <p class="muted">Test-sets in <code>test-sets/</code> choose the subject model and the tests to run.</p>
        <a class="primary" href="#suites/new">New suite</a>
      </div>
      ${
        suites.length
          ? `<div class="library-list">${suites
              .map(
                (suite) => `
            <button type="button" class="library-card" data-open-file="suites/${escapeHtml(suite.filename)}">
              <div>
                <strong>${escapeHtml(suite.name || suite.suite_id || suite.filename)}</strong>
                <div class="meta">${escapeHtml(suite.filename)} · ${escapeHtml(suite.model || "no model")}</div>
              </div>
              <div class="meta">${suite.test_count} tests · ${suite.category_count} categories</div>
            </button>`
              )
              .join("")}</div>`
          : `<div class="empty">No suites match that filter.</div>`
      }`;
  } catch (err) {
    app.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

function providerFields(prefix, block, showMode) {
  const headers = block.headers || {};
  return `
    <div class="form-grid">
      ${
        showMode
          ? `<div class="field-block">
              <label for="${prefix}-mode">Evaluator mode</label>
              <select id="${prefix}-mode">
                <option value="deterministic" ${
                  (block.mode || "deterministic") === "deterministic" ? "selected" : ""
                }>deterministic</option>
                <option value="judge" ${block.mode === "judge" ? "selected" : ""}>judge</option>
              </select>
            </div>`
          : ""
      }
      <div class="field-block ${showMode ? "" : "wide"}">
        <label for="${prefix}-model">Model</label>
        <input class="mono" id="${prefix}-model" type="text" value="${escapeHtml(block.model || "")}">
      </div>
      <div class="field-block wide">
        <label for="${prefix}-endpoint">Endpoint</label>
        <input class="mono" id="${prefix}-endpoint" type="text" value="${escapeHtml(block.endpoint || "")}">
      </div>
      <div class="field-block">
        <label for="${prefix}-key">API key</label>
        <input class="mono" id="${prefix}-key" type="text" value="${escapeHtml(block.api_key || "")}">
        <span class="hint">Keep the ${"${openrouter_key}"} placeholder unless you intend to paste a secret.</span>
      </div>
      <div class="field-block">
        <label for="${prefix}-timeout">Timeout seconds</label>
        <input id="${prefix}-timeout" type="number" value="${escapeHtml(block.timeout_seconds ?? 120)}">
      </div>
      <div class="field-block">
        <label for="${prefix}-title">X-Title</label>
        <input id="${prefix}-title" type="text" value="${escapeHtml(headers["X-Title"] || "")}">
      </div>
      <div class="field-block wide">
        <label for="${prefix}-extra">Extra payload (JSON)</label>
        <textarea class="mono" id="${prefix}-extra">${escapeHtml(jsonPretty(block.extra_payload || {}))}</textarea>
      </div>
    </div>`;
}

function collectProvider(prefix, existing) {
  const headers = { ...(existing.headers || {}) };
  const title = fieldValue(`${prefix}-title`).trim();
  if (title) headers["X-Title"] = title;
  else delete headers["X-Title"];
  const out = {
    ...existing,
    endpoint: fieldValue(`${prefix}-endpoint`).trim(),
    api_key: fieldValue(`${prefix}-key`).trim(),
    model: fieldValue(`${prefix}-model`).trim(),
    timeout_seconds: Number(fieldValue(`${prefix}-timeout`) || 120),
    headers,
    extra_payload: parseJsonField(`${prefix}-extra`, {}),
  };
  const modeEl = document.getElementById(`${prefix}-mode`);
  if (modeEl) out.mode = modeEl.value;
  return out;
}

async function renderConfig() {
  try {
    if (!state.editor || state.editor.kind !== "config") {
      const doc = await fetchJson("/api/config");
      state.editor = { kind: "config", data: doc.data };
    }
    const data = state.editor.data;
    app.innerHTML = `
      <p class="muted">These pipeline models apply to every test in every suite. The model under test lives on each suite.</p>
      <div class="editor">
        <article class="panel">
          <h3>Normalizer</h3>
          ${providerFields("norm", data.normalizer || {})}
        </article>
        <article class="panel">
          <h3>Evaluator</h3>
          ${providerFields("eval", data.evaluator || {}, true)}
        </article>
        <article class="panel">
          <h3>Logging</h3>
          <div class="form-grid">
            <div class="field-block">
              <label for="log-dir">Output directory</label>
              <input class="mono" id="log-dir" type="text" value="${escapeHtml(
                (data.logging || {}).output_dir || "runs"
              )}">
            </div>
          </div>
        </article>
      </div>`;
  } catch (err) {
    app.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

function collectConfig() {
  const data = state.editor.data;
  return {
    ...data,
    version: data.version || 1,
    normalizer: collectProvider("norm", data.normalizer || {}),
    evaluator: collectProvider("eval", data.evaluator || {}),
    logging: { ...(data.logging || {}), output_dir: fieldValue("log-dir").trim() || "runs" },
  };
}

function paintTestEditor() {
  const data = state.editor.data;
  const test = data.test || {};
  const meta = test.metadata || {};
  const request = data.request || {};
  const params = request.parameters || {};
  const normalizer = (data.pipeline || {}).normalizer || {};
  const evaluator = (data.pipeline || {}).evaluator || {};
  const nparams = normalizer.parameters || {};
  const rows = schemaRows(normalizer.schema);
  const benchmark = isBenchmarkEditor();
  const savedChecks = benchmark ? (state.editor.fixture || {}).checks : evaluator.checks;
  const checks = savedChecks && savedChecks.length ? savedChecks : [{ name: "", field: "", op: "equals", expected: "" }];
  const fixtureFile = state.editor.fixturePath || (filename ? `benchmarks/fixtures/${filename}` : "benchmarks/fixtures/");
  const filename = state.editor.isNew ? fieldValue("test-filename") || state.editor.filename : state.editor.filename;

  app.innerHTML = `
    <div class="editor">
      <article class="panel">
        <h3>Identity</h3>
        <div class="form-grid">
          <div class="field-block">
            <label for="test-filename">File name</label>
            <input class="mono" id="test-filename" type="text" ${
              state.editor.isNew ? "" : "readonly"
            } value="${escapeHtml(filename || "")}" placeholder="account_lookup.yaml">
          </div>
          <div class="field-block">
            <label for="test-id">Test id</label>
            <input class="mono" id="test-id" type="text" value="${escapeHtml(test.id || "")}">
          </div>
          <div class="field-block wide">
            <label for="test-name">Name</label>
            <input id="test-name" type="text" value="${escapeHtml(test.name || "")}">
          </div>
          <div class="field-block">
            <label for="test-category">Category</label>
            <input class="mono" id="test-category" type="text" value="${escapeHtml(meta.category || "")}">
          </div>
          <div class="field-block">
            <label for="test-difficulty">Difficulty</label>
            <input id="test-difficulty" type="text" value="${escapeHtml(meta.difficulty || "")}">
          </div>
          <div class="field-block wide">
            <label for="test-group">Benchmark group</label>
            <input class="mono" id="test-group" type="text" value="${escapeHtml(meta.benchmark_group || "")}">
          </div>
          <div class="field-block wide">
            <label for="test-notes">Notes</label>
            <textarea id="test-notes">${escapeHtml((meta.notes || "").trim())}</textarea>
          </div>
        </div>
      </article>
      <article class="panel">
        <h3>Prompts</h3>
        <div class="form-grid">
          <div class="field-block wide">
            <label for="test-prompt">User prompt</label>
            <textarea id="test-prompt">${escapeHtml((test.prompt || "").trim())}</textarea>
          </div>
          <div class="field-block wide">
            <label for="test-criteria">Success criteria</label>
            <textarea id="test-criteria">${escapeHtml((test.success_criteria || "").trim())}</textarea>
            <span class="hint">Sent to the model after the prompt. Describe what a good answer contains, never the answer itself.</span>
          </div>
          <div class="field-block wide">
            <label for="test-system">System prompt</label>
            <textarea id="test-system">${escapeHtml((request.system_prompt || "").trim())}</textarea>
          </div>
        </div>
      </article>
      <article class="panel">
        <h3>Request</h3>
        <div class="form-grid">
          <div class="field-block">
            <label for="req-temp">Temperature</label>
            <input id="req-temp" type="number" step="0.1" value="${escapeHtml(params.temperature ?? 0)}">
          </div>
          <div class="field-block">
            <label for="req-tokens">Max tokens</label>
            <input id="req-tokens" type="number" value="${escapeHtml(params.max_tokens ?? 400)}">
          </div>
          <div class="field-block">
            <label for="req-topp">Top p</label>
            <input id="req-topp" type="number" step="0.1" value="${escapeHtml(params.top_p ?? 1)}">
          </div>
          <div class="field-block">
            <label for="req-timeout">Timeout seconds</label>
            <input id="req-timeout" type="number" value="${escapeHtml(request.timeout_seconds ?? 120)}">
          </div>
          <div class="field-block">
            <label for="req-title">X-Title</label>
            <input id="req-title" type="text" value="${escapeHtml((request.headers || {})["X-Title"] || "")}">
          </div>
          <div class="field-block wide">
            <label for="req-tools">Tools (JSON)</label>
            <textarea class="mono" id="req-tools">${escapeHtml(jsonPretty(request.tools || []))}</textarea>
          </div>
        </div>
      </article>
      <article class="panel">
        <h3>Normalizer schema</h3>
        <div class="form-grid">
          <div class="field-block wide">
            <label class="inline-check"><input id="norm-enabled" type="checkbox" ${
              normalizer.enabled === false ? "" : "checked"
            }> Enabled</label>
          </div>
          <div class="field-block wide">
            <label for="norm-instructions">Instructions</label>
            <textarea id="norm-instructions">${escapeHtml((normalizer.instructions || "").trim())}</textarea>
          </div>
          <div class="field-block">
            <label for="norm-temp">Temperature</label>
            <input id="norm-temp" type="number" step="0.1" value="${escapeHtml(nparams.temperature ?? 0)}">
          </div>
          <div class="field-block">
            <label for="norm-tokens">Max tokens</label>
            <input id="norm-tokens" type="number" value="${escapeHtml(nparams.max_tokens ?? 400)}">
          </div>
        </div>
        <div class="schema-box">
          <div class="muted">Fields extracted from the model answer</div>
          ${rows
            .map(
              (row, index) => `
            <div class="schema-row">
              <input data-schema-name="${index}" type="text" class="mono" placeholder="field name" value="${escapeHtml(
                row.name
              )}">
              <input data-schema-types="${index}" type="text" class="mono" placeholder="string, null" value="${escapeHtml(
                row.types
              )}">
              <label class="inline-check"><input data-schema-required="${index}" type="checkbox" ${
                row.required ? "checked" : ""
              }> required</label>
              <button type="button" class="row-remove" data-remove-schema="${index}">Remove</button>
            </div>`
            )
            .join("")}
          <button type="button" class="add-row" data-add-schema>Add field</button>
        </div>
      </article>
      <article class="panel">
        <h3>${benchmark ? "Fixture checks" : "Evaluator checks"}</h3>
        ${
          benchmark
            ? `<p class="muted">The answer key. Saved to <code>${escapeHtml(
                fixtureFile
              )}</code> and read only by the evaluator; the model never sees it.</p>`
            : ""
        }
        <div class="checks-box">
          <label class="inline-check"><input id="eval-enabled" type="checkbox" ${
            evaluator.enabled === false ? "" : "checked"
          }> Enabled</label>
          <label class="inline-check"><input id="eval-schema-valid" type="checkbox" ${
            evaluator.require_schema_valid === false ? "" : "checked"
          }> Require schema valid</label>
          ${checks
            .map(
              (check, index) => `
            <div class="check-row">
              <input data-check-name="${index}" type="text" placeholder="Check name" value="${escapeHtml(
                check.name || ""
              )}">
              <input data-check-field="${index}" class="mono" type="text" placeholder="field" value="${escapeHtml(
                check.field || ""
              )}">
              <select data-check-op="${index}">
                ${CHECK_OPS.map(
                  (op) =>
                    `<option value="${op}" ${
                      (check.op || check.operator) === op ? "selected" : ""
                    }>${op}</option>`
                ).join("")}
              </select>
              <input data-check-expected="${index}" class="mono" type="text" placeholder="expected" value="${escapeHtml(
                expectedToInput(check.expected)
              )}">
              <button type="button" class="row-remove" data-remove-check="${index}">Remove</button>
            </div>`
            )
            .join("")}
          <button type="button" class="add-row" data-add-check>Add check</button>
        </div>
      </article>
    </div>`;
}

async function renderTestEditor() {
  try {
    if (!state.editor || state.editor.kind !== "test") {
      const source = state.route.source === "benchmarks" ? "benchmarks" : "tests";
      if (state.route.view === "test-new") {
        state.editor = {
          kind: "test",
          source,
          filename: "",
          isNew: true,
          data: blankTest(source),
          fixture: source === "benchmarks" ? blankFixture() : null,
        };
      } else {
        const doc = await fetchJson(`/api/${source}/${encodeURIComponent(state.route.filename)}`);
        state.editor = { kind: "test", source, filename: doc.filename, isNew: false, data: doc.data };
        if (source === "benchmarks") {
          state.editor.fixture = (doc.fixture && doc.fixture.data) || blankFixture();
          state.editor.fixturePath = doc.fixture && doc.fixture.path;
        }
      }
    }
    paintTestEditor();
  } catch (err) {
    app.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

function collectSchemaRows() {
  const names = [...document.querySelectorAll("[data-schema-name]")];
  return names.map((input) => {
    const index = input.getAttribute("data-schema-name");
    const required = document.querySelector(`[data-schema-required="${index}"]`);
    const types = document.querySelector(`[data-schema-types="${index}"]`);
    return {
      name: input.value,
      types: types ? types.value : "string",
      required: Boolean(required && required.checked),
    };
  });
}

function collectChecks() {
  return [...document.querySelectorAll("[data-check-name]")].map((input) => {
    const index = input.getAttribute("data-check-name");
    const field = document.querySelector(`[data-check-field="${index}"]`);
    const op = document.querySelector(`[data-check-op="${index}"]`);
    const expected = document.querySelector(`[data-check-expected="${index}"]`);
    return {
      name: input.value.trim(),
      field: field ? field.value.trim() : "",
      op: op ? op.value : "equals",
      expected: parseExpected(expected ? expected.value : ""),
    };
  }).filter((check) => check.name || check.field);
}

function collectTest() {
  const data = state.editor.data;
  const request = { ...(data.request || {}) };
  const headers = { ...(request.headers || {}) };
  const title = fieldValue("req-title").trim();
  if (title) headers["X-Title"] = title;
  else delete headers["X-Title"];
  request.system_prompt = fieldValue("test-system");
  request.parameters = {
    ...(request.parameters || {}),
    temperature: Number(fieldValue("req-temp") || 0),
    max_tokens: Number(fieldValue("req-tokens") || 400),
    top_p: Number(fieldValue("req-topp") || 1),
  };
  request.timeout_seconds = Number(fieldValue("req-timeout") || 120);
  request.headers = headers;
  request.tools = parseJsonField("req-tools", []);
  const pipeline = { ...(data.pipeline || {}) };
  const normalizer = { ...(pipeline.normalizer || {}) };
  normalizer.enabled = fieldChecked("norm-enabled");
  normalizer.script = normalizer.script || "normalizer.py";
  normalizer.instructions = fieldValue("norm-instructions");
  normalizer.parameters = {
    ...(normalizer.parameters || {}),
    temperature: Number(fieldValue("norm-temp") || 0),
    max_tokens: Number(fieldValue("norm-tokens") || 400),
  };
  normalizer.schema = rowsToSchema(collectSchemaRows());
  const evaluator = { ...(pipeline.evaluator || {}) };
  evaluator.enabled = fieldChecked("eval-enabled");
  evaluator.script = evaluator.script || "evaluator.py";
  evaluator.require_schema_valid = fieldChecked("eval-schema-valid");
  if (isBenchmarkEditor()) {
    delete evaluator.checks;
    state.editor.fixture = { ...(state.editor.fixture || blankFixture()), checks: collectChecks() };
  } else {
    evaluator.checks = collectChecks();
  }
  pipeline.normalizer = normalizer;
  pipeline.evaluator = evaluator;
  const testBlock = {
      ...(data.test || {}),
      id: fieldValue("test-id").trim(),
      name: fieldValue("test-name").trim(),
      prompt: fieldValue("test-prompt"),
      success_criteria: fieldValue("test-criteria"),
      metadata: {
        ...((data.test || {}).metadata || {}),
        category: fieldValue("test-category").trim(),
        difficulty: fieldValue("test-difficulty").trim(),
        benchmark_group: fieldValue("test-group").trim(),
        notes: fieldValue("test-notes"),
      },
    };
  delete testBlock.emoji;
  if (!testBlock.success_criteria.trim()) delete testBlock.success_criteria;
  return {
    ...data,
    version: data.version || 1,
    test: testBlock,
    request,
    pipeline,
  };
}

function paintSuiteEditor() {
  const data = state.editor.data;
  const suite = data.suite || {};
  const provider = data.provider || {};
  const categories = data.categories && data.categories.length ? data.categories : [{ id: "", name: "", tests: [] }];
  const filename = state.editor.isNew ? state.editor.filename : state.editor.filename;
  const testOptions = (state.tests || [])
    .map((test) => `<option value="${escapeHtml(test.path)}">${escapeHtml(test.path)} — ${escapeHtml(test.name || test.test_id || "")}</option>`)
    .join("");

  app.innerHTML = `
    <div class="editor">
      <article class="panel">
        <h3>Suite</h3>
        <div class="form-grid">
          <div class="field-block">
            <label for="suite-filename">File name</label>
            <input class="mono" id="suite-filename" type="text" ${
              state.editor.isNew ? "" : "readonly"
            } value="${escapeHtml(filename || "")}" placeholder="harness_smoke.yaml">
          </div>
          <div class="field-block">
            <label for="suite-id">Suite id</label>
            <input class="mono" id="suite-id" type="text" value="${escapeHtml(suite.id || "")}">
          </div>
          <div class="field-block wide">
            <label for="suite-name">Name</label>
            <input id="suite-name" type="text" value="${escapeHtml(suite.name || "")}">
          </div>
          <div class="field-block wide">
            <label for="suite-description">Description</label>
            <textarea id="suite-description">${escapeHtml((suite.description || "").trim())}</textarea>
          </div>
          <div class="field-block">
            <label for="suite-log">Output directory</label>
            <input class="mono" id="suite-log" type="text" value="${escapeHtml(
              (data.logging || {}).output_dir || "runs"
            )}">
          </div>
        </div>
      </article>
      <article class="panel">
        <h3>Subject model</h3>
        ${providerFields("suite", provider)}
      </article>
      <article class="panel">
        <h3>Categories and tests</h3>
        ${categories
          .map((category, index) => {
            const tests = category.tests || [];
            return `<div class="category-box">
              <div class="cat-head">
                <input data-cat-emoji="${index}" type="text" placeholder="🧪" value="${escapeHtml(
                  category.emoji || ""
                )}">
                <input data-cat-id="${index}" class="mono" type="text" placeholder="category id" value="${escapeHtml(
                  category.id || ""
                )}">
                <input data-cat-name="${index}" type="text" placeholder="Category name" value="${escapeHtml(
                  category.name || ""
                )}">
                <button type="button" class="row-remove" data-remove-category="${index}">Remove</button>
              </div>
              <div class="cat-tests">
                ${tests
                  .map(
                    (ref, tIndex) => `
                  <input class="mono" data-cat-test="${index}:${tIndex}" type="text" value="${escapeHtml(
                      typeof ref === "string" ? ref : ref.path || ""
                    )}">
                  <button type="button" class="row-remove" data-remove-suite-test="${index}:${tIndex}">Remove test</button>`
                  )
                  .join("")}
              </div>
              <label class="field-block">
                <span class="hint">Add a test file</span>
                <select data-add-suite-test="${index}">
                  <option value="">Select a test…</option>
                  ${testOptions}
                </select>
              </label>
            </div>`;
          })
          .join("")}
        <div class="checks-box">
          <button type="button" class="add-row" data-add-category>Add category</button>
        </div>
      </article>
    </div>`;
}

async function renderSuiteEditor() {
  try {
    await ensureWorkspace();
    if (!state.editor || state.editor.kind !== "suite") {
      if (state.route.view === "suite-new") {
        state.editor = { kind: "suite", filename: "", isNew: true, data: blankSuite() };
      } else {
        const doc = await fetchJson(`/api/suites/${encodeURIComponent(state.route.filename)}`);
        state.editor = { kind: "suite", filename: doc.filename, isNew: false, data: doc.data };
      }
    }
    paintSuiteEditor();
  } catch (err) {
    app.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

function collectSuite() {
  const data = state.editor.data;
  const categories = [...document.querySelectorAll("[data-cat-id]")].map((input) => {
    const index = input.getAttribute("data-cat-id");
    const nameEl = document.querySelector(`[data-cat-name="${index}"]`);
    const emojiEl = document.querySelector(`[data-cat-emoji="${index}"]`);
    const tests = [...document.querySelectorAll(`[data-cat-test^="${index}:"]`)].map((el) => el.value.trim()).filter(Boolean);
    return {
      id: input.value.trim(),
      emoji: emojiEl ? emojiEl.value.trim() : "",
      name: nameEl ? nameEl.value.trim() : "",
      tests,
    };
  });
  return {
    ...data,
    version: data.version || 1,
    suite: {
      ...(data.suite || {}),
      id: fieldValue("suite-id").trim(),
      name: fieldValue("suite-name").trim(),
      description: fieldValue("suite-description"),
    },
    provider: collectProvider("suite", data.provider || {}),
    categories,
    logging: { ...(data.logging || {}), output_dir: fieldValue("suite-log").trim() || "runs" },
  };
}

function suggestedFilename(id, fallback) {
  const slug = String(id || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "");
  if (slug) return `${slug}.yaml`;
  return fallback;
}

async function saveCurrent() {
  try {
    const view = state.route.view;
    if (view === "config") {
      const data = collectConfig();
      const saved = await putJson("/api/config", { data });
      state.editor = { kind: "config", data: saved.data };
      state.workspace = null;
      showToast("Saved config.yaml");
      return;
    }
    if (view === "test-edit" || view === "test-new") {
      const data = collectTest();
      let filename = fieldValue("test-filename").trim() || state.editor.filename;
      if (!filename) filename = suggestedFilename(data.test.id, "new_test.yaml");
      if (!filename.endsWith(".yaml") && !filename.endsWith(".yml")) filename += ".yaml";
      const source = state.editor.source || "tests";
      const body = source === "benchmarks" ? { data, fixture: state.editor.fixture } : { data };
      const saved = await putJson(`/api/${source}/${encodeURIComponent(filename)}`, body);
      state.workspace = null;
      state.tests = [];
      showToast(
        source === "benchmarks"
          ? `Saved ${saved.path} and ${saved.fixture ? saved.fixture.path : "its fixture"}`
          : `Saved tests/${filename}`
      );
      if (view === "test-new" || filename !== state.editor.filename) {
        state.editor = null;
        setHash({ view: "test-edit", filename: saved.filename, source });
      } else {
        state.editor.data = saved.data;
        if (source === "benchmarks" && saved.fixture) {
          state.editor.fixture = saved.fixture.data;
          state.editor.fixturePath = saved.fixture.path;
        }
      }
      return;
    }
    if (view === "suite-edit" || view === "suite-new") {
      const data = collectSuite();
      let filename = fieldValue("suite-filename").trim() || state.editor.filename;
      if (!filename) filename = suggestedFilename(data.suite.id, "new_suite.yaml");
      if (!filename.endsWith(".yaml") && !filename.endsWith(".yml")) filename += ".yaml";
      const saved = await putJson(`/api/suites/${encodeURIComponent(filename)}`, { data });
      state.workspace = null;
      state.suites = [];
      showToast(`Saved test-sets/${filename}`);
      if (view === "suite-new" || filename !== state.editor.filename) {
        state.editor = { kind: "suite", filename: saved.filename, isNew: false, data: saved.data };
        setHash({ view: "suite-edit", filename: saved.filename });
      } else {
        state.editor.data = saved.data;
      }
    }
  } catch (err) {
    showToast(err.message, true);
  }
}

function editorChecks() {
  if (isBenchmarkEditor()) return state.editor.fixture.checks;
  return state.editor.data.pipeline.evaluator.checks;
}

function handleStudioClick(event) {
  const open = event.target.closest("[data-open-file]");
  if (open) {
    const [kind, filename] = open.getAttribute("data-open-file").split("/");
    if (kind === "suites") setHash({ view: "suite-edit", filename });
    else setHash({ view: "test-edit", filename, source: kind });
    return true;
  }
  if (!state.editor) return false;

  if (event.target.closest("[data-add-schema]")) {
    if (state.editor.kind !== "test") return false;
    state.editor.data = collectTest();
    const schema = state.editor.data.pipeline.normalizer.schema;
    schema.properties = schema.properties || {};
    let name = "field";
    let n = 1;
    while (schema.properties[name]) name = `field_${n++}`;
    schema.properties[name] = { type: ["string", "null"] };
    schema.required = [...(schema.required || []), name];
    paintTestEditor();
    return true;
  }
  const removeSchema = event.target.closest("[data-remove-schema]");
  if (removeSchema) {
    state.editor.data = collectTest();
    const rows = schemaRows(state.editor.data.pipeline.normalizer.schema);
    rows.splice(Number(removeSchema.getAttribute("data-remove-schema")), 1);
    state.editor.data.pipeline.normalizer.schema = rowsToSchema(rows);
    paintTestEditor();
    return true;
  }
  if (event.target.closest("[data-add-check]")) {
    state.editor.data = collectTest();
    editorChecks().push({
      name: "",
      field: "",
      op: "equals",
      expected: "",
    });
    paintTestEditor();
    return true;
  }
  const removeCheck = event.target.closest("[data-remove-check]");
  if (removeCheck) {
    state.editor.data = collectTest();
    editorChecks().splice(
      Number(removeCheck.getAttribute("data-remove-check")),
      1
    );
    paintTestEditor();
    return true;
  }
  if (event.target.closest("[data-add-category]")) {
    state.editor.data = collectSuite();
    state.editor.data.categories.push({ id: "", emoji: "", name: "", tests: [] });
    paintSuiteEditor();
    return true;
  }
  const removeCategory = event.target.closest("[data-remove-category]");
  if (removeCategory) {
    state.editor.data = collectSuite();
    state.editor.data.categories.splice(Number(removeCategory.getAttribute("data-remove-category")), 1);
    paintSuiteEditor();
    return true;
  }
  const removeSuiteTest = event.target.closest("[data-remove-suite-test]");
  if (removeSuiteTest) {
    state.editor.data = collectSuite();
    const [cIndex, tIndex] = removeSuiteTest.getAttribute("data-remove-suite-test").split(":").map(Number);
    state.editor.data.categories[cIndex].tests.splice(tIndex, 1);
    paintSuiteEditor();
    return true;
  }
  return false;
}

app.addEventListener("change", (event) => {
  const select = event.target.closest("[data-add-suite-test]");
  if (!select || !select.value || !state.editor || state.editor.kind !== "suite") return;
  state.editor.data = collectSuite();
  const index = Number(select.getAttribute("data-add-suite-test"));
  const category = state.editor.data.categories[index];
  category.tests = category.tests || [];
  category.tests.push(select.value);
  paintSuiteEditor();
});

boot();
