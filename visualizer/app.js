const app = document.getElementById("app");
const filterInput = document.getElementById("filter");
const refreshBtn = document.getElementById("refresh");
const saveBtn = document.getElementById("save");
const searchWrap = document.getElementById("search-wrap");
const pageTitle = document.getElementById("page-title");
const pageKicker = document.getElementById("page-kicker");
const toastEl = document.getElementById("toast");

const state = {
  runs: [],
  runsLoaded: false,
  details: {},
  workspace: null,
  tests: [],
  suites: [],
  editor: null,
  statusFilter: "all",
  query: "",
  openRuns: new Set(),
  route: parseHash(),
  error: null,
  loading: true,
};

function parseHash() {
  const raw = (location.hash || "").replace(/^#/, "").replace(/^\/+/, "");
  const parts = raw.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
  if (!parts.length) return { view: "hub" };
  if (parts[0] === "runs") return { view: "board" };
  if (parts[0] === "run" && parts.length >= 3) {
    return {
      view: "detail",
      suiteId: parts[1],
      runId: parts[2],
      categoryId: parts[3] || null,
      testId: parts[4] || null,
    };
  }
  if (parts[0] === "config") return { view: "config" };
  if (parts[0] === "tests" && parts[1] === "new") return { view: "test-new" };
  if (parts[0] === "tests" && parts[1]) return { view: "test-edit", filename: parts[1] };
  if (parts[0] === "tests") return { view: "tests" };
  if (parts[0] === "suites" && parts[1] === "new") return { view: "suite-new" };
  if (parts[0] === "suites" && parts[1]) return { view: "suite-edit", filename: parts[1] };
  if (parts[0] === "suites") return { view: "suites" };
  return { view: "hub" };
}

function setHash(route) {
  const view = route.view;
  if (view === "hub") location.hash = "";
  else if (view === "board") location.hash = "#runs";
  else if (view === "config") location.hash = "#config";
  else if (view === "tests") location.hash = "#tests";
  else if (view === "test-new") location.hash = "#tests/new";
  else if (view === "test-edit") location.hash = `#tests/${encodeURIComponent(route.filename)}`;
  else if (view === "suites") location.hash = "#suites";
  else if (view === "suite-new") location.hash = "#suites/new";
  else if (view === "suite-edit") location.hash = `#suites/${encodeURIComponent(route.filename)}`;
  else if (view === "detail") {
    let hash = `#run/${encodeURIComponent(route.suiteId)}/${encodeURIComponent(route.runId)}`;
    if (route.categoryId && route.testId) {
      hash += `/${encodeURIComponent(route.categoryId)}/${encodeURIComponent(route.testId)}`;
    }
    location.hash = hash;
  }
}

function navKey(view) {
  if (view === "board" || view === "detail") return "runs";
  if (view === "test-edit" || view === "test-new" || view === "tests") return "tests";
  if (view === "suite-edit" || view === "suite-new" || view === "suites") return "suites";
  if (view === "config") return "config";
  return "";
}

function updateChrome() {
  const view = state.route.view;
  const titles = {
    hub: "Workbench",
    board: "Runs",
    detail: "Runs",
    config: "Config",
    tests: "Tests",
    "test-edit": "Tests",
    "test-new": "Tests",
    suites: "Suites",
    "suite-edit": "Suites",
    "suite-new": "Suites",
  };
  const title = titles[view] || "Workbench";
  pageTitle.textContent = title;
  pageKicker.textContent = view === "hub" ? "Salesforce open-model harness" : "Workbench";
  document.title = `${title} · Workbench`;
  document.querySelectorAll(".nav a").forEach((link) => {
    link.classList.toggle("is-active", link.dataset.nav === navKey(view));
  });
  const showSearch = view === "board" || view === "tests" || view === "suites";
  searchWrap.hidden = !showSearch;
  filterInput.placeholder =
    view === "tests" ? "Filter tests" : view === "suites" ? "Filter suites" : "Filter suite, model, or test";
  const showSave = ["config", "test-edit", "test-new", "suite-edit", "suite-new"].includes(view);
  saveBtn.hidden = !showSave;
}

function showToast(message, isError = false) {
  toastEl.hidden = false;
  toastEl.textContent = message;
  toastEl.classList.toggle("is-error", Boolean(isError));
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toastEl.hidden = true;
  }, 3200);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatDate(iso) {
  if (!iso) return "Unknown time";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    hour12: false,
  }).format(date) + " UTC";
}

function formatDuration(start, end) {
  const a = Date.parse(start);
  const b = Date.parse(end);
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return null;
  const ms = b - a;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function formatMs(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return "—";
  const n = Number(ms);
  if (n < 1000) return `${Math.round(n)} ms`;
  return `${(n / 1000).toFixed(2)} s`;
}

function formatCost(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(6)}`;
  return `$${n.toFixed(4)}`;
}

function formatScore(score) {
  if (score == null || Number.isNaN(Number(score))) return "—";
  return Number(score).toFixed(2);
}

function statusClass(status) {
  if (status === "pass" || status === "fail" || status === "error" || status === "completed") {
    return status;
  }
  return "completed";
}

function displayName(test) {
  return test.name || test.test_id || "Untitled test";
}

function costPerTask(cost, passed) {
  const n = Number(passed);
  const c = Number(cost);
  if (!n || !Number.isFinite(c)) return "—";
  return formatCost(c / n);
}

function matchesQuery(run, query) {
  if (!query) return true;
  const hay = [
    run.suite_id,
    run.suite_name,
    run.model,
    run.run_id,
    ...(run.tests || []).flatMap((t) => [t.test_id, t.name, t.category_id, t.category_name]),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return hay.includes(query);
}

function runPassesStatus(run, status) {
  if (status === "all") return true;
  return (run.tests || []).some((t) => t.status === status);
}

function groupedRuns(runs) {
  const query = state.query.trim().toLowerCase();
  const filtered = runs.filter(
    (run) => matchesQuery(run, query) && runPassesStatus(run, state.statusFilter)
  );
  const groups = new Map();
  for (const run of filtered) {
    const key = run.suite_id || "unknown";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(run);
  }
  return [...groups.entries()];
}

function assayButtons(run) {
  return (run.tests || [])
    .map((test) => {
      const title = `${displayName(test)} · ${test.status || "unknown"}`;
      return `<button type="button" class="${statusClass(test.status)}" data-open-test="${escapeHtml(
        run.suite_id
      )}/${escapeHtml(run.run_id)}/${escapeHtml(test.category_id)}/${escapeHtml(
        test.test_id
      )}" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}"></button>`;
    })
    .join("");
}

function overallStatus(run) {
  const tests = run.tests || [];
  if (tests.some((test) => test.status === "error")) return "error";
  if (tests.some((test) => test.status === "fail")) return "fail";
  if (tests.length && tests.every((test) => test.status === "pass")) return "pass";
  return "completed";
}

function checkDots(test) {
  const checks = test.checks || [];
  if (!checks.length) return "";
  return `<span class="check-dots" aria-hidden="true">${checks
    .map((c) => `<i class="${c.passed ? "pass" : "fail"}"></i>`)
    .join("")}</span>`;
}

function numericField(test, key) {
  const direct = Number(test?.[key]);
  if (Number.isFinite(direct)) return direct;
  const metrics = test?.metrics;
  if (key === "latency_ms" && Number.isFinite(Number(metrics?.latency_ms))) {
    return Number(metrics.latency_ms);
  }
  if (key === "cost_usd" && Number.isFinite(Number(metrics?.cost_usd))) {
    return Number(metrics.cost_usd);
  }
  if (key === "total_cost_usd") {
    const model = Number(metrics?.cost_usd);
    const norm = Number(test?.normalizer_metrics?.cost_usd);
    if (Number.isFinite(model) || Number.isFinite(norm)) {
      return (Number.isFinite(model) ? model : 0) + (Number.isFinite(norm) ? norm : 0);
    }
  }
  return null;
}

function sumNumbers(tests, key) {
  let total = 0;
  let any = false;
  for (const test of tests) {
    const value = numericField(test, key);
    if (Number.isFinite(value)) {
      total += value;
      any = true;
    }
  }
  return any ? total : null;
}

function categoryWallMs(tests) {
  const starts = tests
    .map((test) => Date.parse(test.started_at_utc))
    .filter((ms) => !Number.isNaN(ms));
  if (starts.length === tests.length && tests.length) {
    const first = Math.min(...starts);
    let lastEnd = 0;
    for (const test of tests) {
      const start = Date.parse(test.started_at_utc);
      const duration = Number(test.wall_ms);
      const end = start + (Number.isFinite(duration) ? duration : 0);
      if (end > lastEnd) lastEnd = end;
    }
    if (lastEnd > first) return lastEnd - first;
  }
  return sumNumbers(tests, "wall_ms");
}

function categoryStats(tests) {
  const total = tests.length;
  const passed = tests.filter((test) => test.status === "pass").length;
  const failed = tests.filter((test) => test.status === "fail").length;
  const errors = tests.filter((test) => test.status === "error").length;
  return {
    total,
    passed,
    failed,
    errors,
    modelMs: sumNumbers(tests, "latency_ms"),
    wallMs: categoryWallMs(tests),
    cost: sumNumbers(tests, "total_cost_usd") ?? sumNumbers(tests, "cost_usd"),
    emoji: tests.find((test) => test.category_emoji)?.category_emoji || "",
    name: tests[0]?.category_name || tests[0]?.category_id || "Ungrouped",
  };
}

function testsByCategory(tests) {
  const byCategory = new Map();
  for (const test of tests || []) {
    const key = test.category_id || test.category_name || "Ungrouped";
    if (!byCategory.has(key)) byCategory.set(key, []);
    byCategory.get(key).push(test);
  }
  return byCategory;
}

function testRows(run) {
  let html = "";
  for (const tests of testsByCategory(run.tests).values()) {
    const stats = categoryStats(tests);
    html += `<div class="category-head">
      <div class="category-label">${
        stats.emoji ? `<span class="category-emoji">${escapeHtml(stats.emoji)}</span>` : ""
      }${escapeHtml(stats.name)}</div>
      <div class="category-stats">
        <span class="stat pass"><b>${stats.passed}</b>/${stats.total} passed</span>
        <span class="stat"><b>${escapeHtml(formatMs(stats.modelMs))}</b> model</span>
        <span class="stat"><b>${escapeHtml(formatMs(stats.wallMs))}</b> wall</span>
        <span class="stat"><b>${escapeHtml(formatCost(stats.cost))}</b> cost</span>
      </div>
    </div>`;
    for (const test of tests) {
      html += `
        <button type="button" class="test-row" data-open-test="${escapeHtml(run.suite_id)}/${escapeHtml(
          run.run_id
        )}/${escapeHtml(test.category_id)}/${escapeHtml(test.test_id)}">
          <span class="pip ${statusClass(test.status)}"></span>
          <span class="name">
            <strong>${escapeHtml(displayName(test))}</strong>
            <span>${escapeHtml(test.test_id)}</span>
          </span>
          ${checkDots(test)}
          <span class="score">${escapeHtml(formatScore(test.score))} · ${escapeHtml(
            formatMs(test.latency_ms)
          )}</span>
        </button>`;
    }
  }
  return html;
}

function renderBoard() {
  if (state.loading) {
    app.innerHTML = `<p class="status-line">Reading suite runs…</p>`;
    return;
  }
  if (state.error) {
    app.innerHTML = `<div class="error-banner">${escapeHtml(state.error)}</div>`;
    return;
  }
  if (!state.runs.length) {
    app.innerHTML = `<div class="empty">No suite runs in <code>runs/</code> yet. Run a test-set, then refresh.</div>`;
    return;
  }

  const groups = groupedRuns(state.runs);
  const chips = ["all", "pass", "fail", "error"]
    .map((id) => {
      const label = id === "all" ? "All runs" : id === "pass" ? "Passed" : id === "fail" ? "Failures" : "Errors";
      return `<button type="button" class="chip" data-status="${id}" aria-pressed="${
        state.statusFilter === id
      }">${label}</button>`;
    })
    .join("");

  if (!groups.length) {
    app.innerHTML = `
      <div class="filters">${chips}</div>
      <div class="empty">No runs match that filter.</div>`;
    return;
  }

  let html = `<div class="filters">${chips}</div>`;
  for (const [suiteId, runs] of groups) {
    const name = runs[0].suite_name || suiteId;
    html += `<section class="suite-block">
      <div class="suite-head">
        <h2>${escapeHtml(name)}</h2>
        <p>${runs.length} run${runs.length === 1 ? "" : "s"} · <code>${escapeHtml(suiteId)}</code></p>
      </div>`;
    for (const run of runs) {
      const key = `${run.suite_id}/${run.run_id}`;
      const open = state.openRuns.has(key);
      const counts = run.counts || {};
      const totals = run.totals || {};
      const wall = formatDuration(run.started_at_utc, run.finished_at_utc);
      html += `
        <article class="run-card${open ? " is-open" : ""}" data-run="${escapeHtml(key)}">
          <div class="run-meta">
            <div>
              <div class="model">${escapeHtml(run.model || "unknown model")}</div>
              ${run.description ? `<p class="description">${escapeHtml(run.description)}</p>` : ""}
            </div>
            <div class="when">
              ${escapeHtml(formatDate(run.started_at_utc))}
              ${run.no_pipeline ? `<div><span class="badge">inference only</span></div>` : ""}
            </div>
          </div>
          <div class="assay" role="list">${assayButtons(run)}</div>
          <div class="stats">
            <div class="stat pass"><b>${counts.pass ?? 0}</b> passed</div>
            <div class="stat fail"><b>${counts.fail ?? 0}</b> failed</div>
            <div class="stat error"><b>${counts.error ?? 0}</b> errors</div>
            <div class="stat"><b>${counts.total ?? run.tests.length}</b> tests</div>
            <div class="stat"><b>${escapeHtml(formatMs(totals.latency_ms))}</b> model time</div>
            ${wall ? `<div class="stat"><b>${escapeHtml(wall)}</b> wall</div>` : ""}
            <div class="stat"><b>${escapeHtml(formatCost(totals.cost_usd))}</b> cost</div>
            <div class="stat"><b>${escapeHtml(costPerTask(totals.cost_usd, counts.pass))}</b> per task</div>
          </div>
          <div class="expand-row">
            <div class="expand-actions">
              <button type="button" class="ghost" data-open-run="${escapeHtml(run.suite_id)}/${escapeHtml(
                run.run_id
              )}">
                Open run
              </button>
              <button type="button" class="expand" data-toggle-run="${escapeHtml(key)}" aria-expanded="${open}">
                ${open ? "Hide tests" : "Show tests"}
              </button>
            </div>
            <span class="muted">${escapeHtml(run.evaluator_mode || "unevaluated")}${
              run.normalizer_model ? ` · ${escapeHtml(run.normalizer_model)}` : ""
            }</span>
          </div>
          <div class="categories">${testRows(run)}</div>
        </article>`;
    }
    html += `</section>`;
  }
  app.innerHTML = html;
}

function highlightJson(value) {
  const json = JSON.stringify(value, null, 2);
  return escapeHtml(json)
    .replaceAll(
      /&quot;([^&]+)&quot;(?=:)/g,
      '<span class="json-key">&quot;$1&quot;</span>'
    )
    .replaceAll(
      /&quot;((?:[^&]|&(?!quot;))*)&quot;/g,
      '<span class="json-str">&quot;$1&quot;</span>'
    )
    .replaceAll(/\b(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/g, '<span class="json-num">$1</span>')
    .replaceAll(/\bnull\b/g, '<span class="json-null">null</span>');
}

function isSearchCall(call) {
  return /search/i.test(String(call?.name || ""));
}

function titleCaseWords(value) {
  return String(value)
    .replaceAll("_", " ")
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function prettyToolName(name) {
  const text = String(name || "tool");
  const split = text.indexOf(":");
  if (split > 0) {
    const provider = titleCaseWords(text.slice(0, split));
    const tool = titleCaseWords(text.slice(split + 1).replace(/^web[_\s]+/i, ""));
    return `${provider} ${tool}`.trim();
  }
  return text;
}

function toolArgumentPreview(call) {
  const args = call?.arguments;
  if (!args || typeof args !== "object" || Array.isArray(args)) return null;
  if (typeof args.query === "string" && args.query.trim()) {
    return { label: isSearchCall(call) ? "Search" : "SOQL", text: args.query };
  }
  if (typeof args.sobject === "string" && args.sobject.trim()) {
    return { label: "sObject", text: args.sobject };
  }
  return null;
}

function citationItems(annotations) {
  return (annotations || [])
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const cite = item.url_citation && typeof item.url_citation === "object" ? item.url_citation : item;
      const url = cite.url || item.url;
      if (!url) return null;
      return { url, title: cite.title || url, content: cite.content };
    })
    .filter(Boolean);
}

function searchSources(call) {
  const result = call?.result;
  if (Array.isArray(result?.sources)) return result.sources;
  return citationItems(result);
}

function searchResultHtml(call) {
  const sources = searchSources(call);
  if (!sources.length) {
    return `<p class="muted tool-missing">No search sources were returned.</p>`;
  }
  return `<ul class="citations search-sources">${sources
    .map((source) => {
      const url = source.url || "";
      const title = source.title || url;
      const snippet = source.content
        ? `<div class="search-snippet">${escapeHtml(source.content)}</div>`
        : "";
      return `<li><a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(
        title
      )}</a>${snippet}</li>`;
    })
    .join("")}</ul>`;
}

function toolCallCard(call) {
  const failed = call.ok === false || Boolean(call.error);
  const search = isSearchCall(call);
  const preview = toolArgumentPreview(call);
  const stats = [];
  if (call.latency_ms != null) stats.push(formatMs(call.latency_ms));
  if (call.returned != null) {
    const unit = search ? "source" : "record";
    const total = call.total_size != null ? ` of ${call.total_size}` : "";
    stats.push(`${call.returned} ${unit}${call.returned === 1 ? "" : "s"}${total}`);
  }
  if (call.field_count != null) stats.push(`${call.field_count} fields`);
  if (call.truncated) stats.push("truncated");
  if (call.error) stats.push(call.error);
  const hasArgsDump =
    call.arguments != null &&
    !preview &&
    typeof call.arguments === "object" &&
    Object.keys(call.arguments).length > 0;
  const hasResult = call.result != null;
  const resultBody = search
    ? searchResultHtml(call)
    : `<pre>${highlightJson(call.result)}</pre>`;
  return `<article class="tool-call ${failed ? "is-fail" : "is-ok"}">
    <div class="tool-head">
      <span class="pip ${failed ? "fail" : "pass"}"></span>
      <strong>${escapeHtml(prettyToolName(call.name))}</strong>
      <span class="tool-status">${failed ? "error" : "ok"}</span>
      <span class="tool-stats">${escapeHtml(stats.join(" · ") || "")}</span>
    </div>
    ${
      preview
        ? `<pre class="tool-soql"><span class="tool-soql-label">${escapeHtml(
            preview.label
          )}</span>${escapeHtml(preview.text)}</pre>`
        : ""
    }
    ${
      hasArgsDump
        ? `<details class="tool-fold"><summary>Arguments</summary><pre>${highlightJson(
            call.arguments
          )}</pre></details>`
        : ""
    }
    ${
      hasResult || search
        ? `<details class="tool-fold"><summary>Result</summary>${resultBody}</details>`
        : `<p class="muted tool-missing">No result was recorded for this call.</p>`
    }
  </article>`;
}

function toolsPanel(test) {
  const tools = test.tools;
  if (!tools) return "";
  const rounds = (tools.rounds || []).filter(
    (round) => (round.calls || []).length || (round.annotations || []).length
  );
  const advertised = [...(tools.client_tools || []), ...(tools.server_tools || [])];
  if (!rounds.length && !advertised.length) return "";

  const n = Number(tools.client_tool_executions) || rounds.reduce((sum, round) => sum + (round.calls || []).length, 0);

  const roundHtml = rounds
    .map((round) => {
      return `<div class="tool-round">
        <div class="tool-round-label">Round ${escapeHtml(String(round.id ?? round.index))} · ${escapeHtml(
          round.finish_reason || "response"
        )}${round.latency_ms != null ? ` · ${escapeHtml(formatMs(round.latency_ms))}` : ""}</div>
        ${(round.calls || []).map(toolCallCard).join("")}
      </div>`;
    })
    .join("");

  const unused =
    !rounds.length && advertised.length
      ? `<p class="muted">Advertised ${advertised
          .map((name) => escapeHtml(String(name).replace(/^openrouter:/, "")))
          .join(", ")} · not called.</p>`
      : "";

  const callCount = rounds.reduce((sum, round) => sum + (round.calls || []).length, 0);
  const citeCount = rounds.reduce((sum, round) => sum + citationItems(round.annotations).length, 0);
  const titleCount = callCount || n || citeCount;

  return `<details class="panel fold tools-panel">
    <summary>
      Tool calls
      <span class="fold-meta"><span class="fold-count">${escapeHtml(String(titleCount))}</span></span>
    </summary>
    <div class="tools-body">
      ${unused}
      ${roundHtml}
    </div>
  </details>`;
}

function fieldCards(test) {
  const checks = test.evaluation?.checks || [];
  if (!checks.length) {
    return "";
  }
  const passed = checks.filter((check) => check.passed).length;
  const failed = checks.length - passed;
  return `<details class="panel fold fields-panel">
    <summary>
      Fields
      <span class="fold-meta">
        <span class="fold-count">${checks.length}</span>
        <span class="fold-tally"><span class="pass">${passed} pass</span><span class="fail">${failed} fail</span></span>
      </span>
    </summary>
    <div class="fields">${checks
    .map((check) => {
      const actual = check.actual === undefined ? "—" : JSON.stringify(check.actual);
      const expected = check.expected === undefined ? "—" : JSON.stringify(check.expected);
      const klass = check.passed ? "pass" : "fail";
      return `<article class="field ${klass}">
        <div class="label">
          <span>${escapeHtml(check.field || check.name || "field")}</span>
          <span>${check.passed ? "pass" : "fail"}</span>
        </div>
        <div class="actual">${escapeHtml(String(check.actual ?? "—"))}</div>
        <div class="expected">${escapeHtml(check.name || "")}<br>expected <code>${escapeHtml(
          expected
        )}</code> · actual <code>${escapeHtml(actual)}</code></div>
      </article>`;
    })
    .join("")}</div>
  </details>`;
}

function renderCategoryRail(run, current) {
  const tests = run.tests || [];
  const runActive = !current;
  const overview = `<button type="button" class="test-row run-home${
    runActive ? " is-active" : ""
  }" data-open-run="${escapeHtml(run.suite_id)}/${escapeHtml(run.run_id)}">
    <span class="pip ${overallStatus(run)}"></span>
    <span class="name">
      <strong>Run overview</strong>
      <span>${escapeHtml(run.run_id)}</span>
    </span>
  </button>`;
  const cats = [...testsByCategory(tests).values()]
    .map((group) => {
      const stats = categoryStats(group);
      const selectedHere = Boolean(
        current &&
          group.some(
            (test) => test.test_id === current.test_id && test.category_id === current.category_id
          )
      );
      const rows = group
        .map((test) => {
          const active =
            Boolean(current) &&
            test.test_id === current.test_id &&
            test.category_id === current.category_id;
          return `<button type="button" class="test-row${active ? " is-active" : ""}" data-open-test="${escapeHtml(
            run.suite_id
          )}/${escapeHtml(run.run_id)}/${escapeHtml(test.category_id)}/${escapeHtml(test.test_id)}">
            <span class="pip ${statusClass(test.status)}"></span>
            <span class="name">
              <strong>${escapeHtml(displayName(test))}</strong>
              <span>${escapeHtml(test.test_id)}</span>
            </span>
            ${checkDots(test)}
            ${
              test.client_tool_executions
                ? `<span class="score">${escapeHtml(String(test.client_tool_executions))} tool${
                    test.client_tool_executions === 1 ? "" : "s"
                  }</span>`
                : ""
            }
          </button>`;
        })
        .join("");
      return `<details class="rail-category"${selectedHere ? " open" : ""}>
        <summary>
          <span class="category-label">${
            stats.emoji ? `<span class="category-emoji">${escapeHtml(stats.emoji)}</span>` : ""
          }${escapeHtml(stats.name)}</span>
          <span class="fold-meta">
            <span class="fold-count">${stats.total}</span>
            <span class="fold-tally">
              <span class="pass">${stats.passed} pass</span>
              <span class="fail">${stats.failed} fail</span>
              ${stats.errors ? `<span class="error">${stats.errors} error</span>` : ""}
            </span>
          </span>
        </summary>
        <div class="rail-tests">${rows}</div>
      </details>`;
    })
    .join("");
  return `${overview}${cats}`;
}

function renderRunOverview(run) {
  const counts = run.counts || {};
  const totals = run.totals || {};
  const stamp = overallStatus(run);
  const wall = formatDuration(run.started_at_utc, run.finished_at_utc);
  const categories = [...testsByCategory(run.tests || []).values()]
    .map((group) => {
      const stats = categoryStats(group);
      return `<article class="run-cat-card">
        <div class="category-label">${
          stats.emoji ? `<span class="category-emoji">${escapeHtml(stats.emoji)}</span>` : ""
        }${escapeHtml(stats.name)}</div>
        <div class="fold-tally">
          <span class="fold-count">${stats.total}</span>
          <span class="pass">${stats.passed} pass</span>
          <span class="fail">${stats.failed} fail</span>
          ${stats.errors ? `<span class="error">${stats.errors} error</span>` : ""}
        </div>
        <p class="muted">${escapeHtml(formatMs(stats.modelMs))} model · ${escapeHtml(
          formatCost(stats.cost)
        )} cost</p>
      </article>`;
    })
    .join("");

  app.innerHTML = `
    <div class="stage" role="dialog" aria-modal="true" aria-label="${escapeHtml(
      run.suite_name || run.suite_id || "Run"
    )}">
      <div class="stage-top">
        <button type="button" class="ghost" id="back">Back to runs</button>
        <div>
          <div class="kicker">${escapeHtml(run.suite_name || run.suite_id)} · ${escapeHtml(
            formatDate(run.started_at_utc)
          )}</div>
          <strong>${escapeHtml(run.model || "")}</strong>
        </div>
      </div>
      <div class="stage-grid">
        <aside class="rail">${renderCategoryRail(run, null)}</aside>
        <section class="detail">
          <div class="verdict">
            <div>
              <div class="kicker">${escapeHtml(run.suite_id || "")}</div>
              <h2>${escapeHtml(run.suite_name || run.suite_id || "Run")}</h2>
              <p class="muted"><code>${escapeHtml(run.run_id)}</code>${
                run.no_pipeline ? ` · inference only` : ""
              }${run.evaluator_mode ? ` · ${escapeHtml(run.evaluator_mode)}` : ""}</p>
            </div>
            <div class="stamp ${stamp}">${escapeHtml(stamp === "completed" ? "done" : stamp)}</div>
          </div>
          ${
            run.description
              ? `<p class="description run-lead">${escapeHtml(run.description)}</p>`
              : ""
          }
          <div class="assay run-assay" role="list">${assayButtons(run)}</div>
          <article class="panel usage-lead">
            <h3>Suite totals</h3>
            <div class="kv">
              <div><span>Passed</span><b class="pass">${counts.pass ?? 0}</b></div>
              <div><span>Failed</span><b class="fail">${counts.fail ?? 0}</b></div>
              <div><span>Errors</span><b class="error">${counts.error ?? 0}</b></div>
              <div><span>Tests</span><b>${counts.total ?? (run.tests || []).length}</b></div>
              <div><span>Model time</span><b>${escapeHtml(formatMs(totals.latency_ms))}</b></div>
              <div><span>Wall</span><b>${escapeHtml(wall || "—")}</b></div>
              <div><span>Cost</span><b>${escapeHtml(formatCost(totals.cost_usd))}</b></div>
              <div><span>Per task</span><b>${escapeHtml(
                costPerTask(totals.cost_usd, counts.pass)
              )}</b></div>
            </div>
          </article>
          ${
            categories
              ? `<div class="run-cats">${categories}</div>`
              : `<p class="muted">No tests were recorded in this run.</p>`
          }
        </section>
      </div>
    </div>`;
}

function renderDetail(run, selected) {
  const current =
    selected ||
    (run.tests || [])[0] || {
      test_id: "unknown",
      category_id: "",
      status: "completed",
      name: "No tests",
    };
  const stamp = statusClass(current.status);
  const rail = renderCategoryRail(run, current);

  const metrics = current.metrics || {};
  const nmetrics = current.normalizer_metrics || {};
  const schemaErrors = current.schema_errors || current.evaluation?.schema?.errors || [];

  app.innerHTML = `
    <div class="stage" role="dialog" aria-modal="true" aria-label="${escapeHtml(displayName(current))}">
      <div class="stage-top">
        <button type="button" class="ghost" id="back-run">Back to run</button>
        <div>
          <div class="kicker">${escapeHtml(run.suite_name || run.suite_id)} · ${escapeHtml(
            formatDate(run.started_at_utc)
          )}</div>
          <strong>${escapeHtml(run.model || "")}</strong>
        </div>
      </div>
      <div class="stage-grid">
        <aside class="rail">${rail}</aside>
        <section class="detail">
          <div class="verdict">
            <div>
              <div class="kicker">${escapeHtml(
                `${current.category_emoji ? `${current.category_emoji} ` : ""}${current.category_name || current.category_id || ""}`
              )}</div>
              <h2>${escapeHtml(displayName(current))}</h2>
              <p class="muted"><code>${escapeHtml(current.test_id)}</code> · score ${escapeHtml(
                formatScore(current.score)
              )} · ${escapeHtml(formatMs(current.latency_ms ?? metrics.latency_ms))}</p>
            </div>
            <div class="stamp ${stamp}">${escapeHtml(current.status || "done")}</div>
          </div>
          <article class="panel usage-lead">
              <h3>Usage</h3>
              <div class="kv">
                <div><span>Latency</span><b>${escapeHtml(formatMs(metrics.latency_ms))}</b></div>
                <div><span>Tokens</span><b>${escapeHtml(metrics.total_tokens ?? "—")}</b></div>
                <div><span>Cost</span><b>${escapeHtml(formatCost(metrics.cost_usd))}</b></div>
                <div><span>Normalizer</span><b>${escapeHtml(formatMs(nmetrics.latency_ms))}</b></div>
                <div><span>Norm. tokens</span><b>${escapeHtml(nmetrics.total_tokens ?? "—")}</b></div>
                <div><span>Norm. cost</span><b>${escapeHtml(formatCost(nmetrics.cost_usd))}</b></div>
                <div><span>Rounds</span><b>${escapeHtml(metrics.rounds ?? current.tools?.rounds_used ?? "—")}</b></div>
                <div><span>Tool calls</span><b>${escapeHtml(
                  metrics.client_tool_executions ?? current.tools?.client_tool_executions ?? "—"
                )}</b></div>
              </div>
            </article>
          ${current.error ? `<div class="error-banner">${escapeHtml(current.error)}</div>` : ""}
          ${fieldCards(current)}
          ${toolsPanel(current)}
          ${
            schemaErrors.length
              ? `<div class="panel"><h3>Schema</h3><pre>${escapeHtml(
                  schemaErrors.map((e) => JSON.stringify(e)).join("\n")
                )}</pre></div>`
              : ""
          }
          <div class="panels">
            <article class="panel">
              <h3>Model answer</h3>
              <div class="body prose">${escapeHtml(current.answer || "No answer.txt")}</div>
            </article>
            ${
              current.response_view?.reasoning
                ? `<article class="panel"><h3>Reasoning</h3><div class="body prose">${escapeHtml(
                    current.response_view.reasoning
                  )}</div></article>`
                : ""
            }
            <article class="panel">
              <h3>Normalized fields</h3>
              <pre>${
                current.normalized == null
                  ? "No normalized.json"
                  : highlightJson(current.normalized)
              }</pre>
            </article>
            <article class="panel">
              <h3>Prompt</h3>
              <div class="body prose">${escapeHtml(current.prompt || "No prompt recorded.")}</div>
            </article>
            ${
              current.system_prompt
                ? `<article class="panel"><h3>System prompt</h3><div class="body prose">${escapeHtml(
                    current.system_prompt
                  )}</div></article>`
                : ""
            }
            <details class="panel fold">
              <summary>Request</summary>
              <pre>${current.request ? highlightJson(current.request) : "No request.json"}</pre>
            </details>
            <details class="panel fold">
              <summary>Response</summary>
              <pre>${current.response ? highlightJson(current.response) : "No response.json"}</pre>
            </details>
            ${
              current.error_trace
                ? `<article class="panel"><h3>Error trace</h3><pre>${escapeHtml(
                    current.error_trace
                  )}</pre></article>`
                : ""
            }
          </div>
        </section>
      </div>
    </div>`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const text = await response.text();
  let data = null;
  try {
    data = JSON.parse(text);
  } catch {
    data = null;
  }
  if (!response.ok) {
    const message =
      (data && data.error) ||
      `Could not load ${url} (${response.status}). Serve this workbench with python visualizer/serve.py.`;
    throw new Error(message);
  }
  return data;
}

async function putJson(url, body) {
  return fetchJson(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function loadRuns() {
  state.loading = true;
  state.error = null;
  if (state.route.view === "board") renderBoard();
  try {
    const data = await fetchJson("/api/runs");
    state.runs = data.runs || [];
    state.runsLoaded = true;
    state.loading = false;
    if (state.route.view === "board") renderBoard();
    else if (state.route.view === "detail") render();
  } catch (err) {
    state.loading = false;
    state.runsLoaded = true;
    state.error = err.message;
    if (state.route.view === "board") renderBoard();
  }
}

async function ensureRunDetail(suiteId, runId) {
  const key = `${suiteId}/${runId}`;
  if (state.details[key]) return state.details[key];
  const detail = await fetchJson(`/api/run/${encodeURIComponent(suiteId)}/${encodeURIComponent(runId)}`);
  state.details[key] = detail;
  return detail;
}

function findTest(run, categoryId, testId) {
  if (!run || !categoryId || !testId) return null;
  return (
    (run.tests || []).find((t) => t.category_id === categoryId && t.test_id === testId) || null
  );
}

async function render() {
  updateChrome();
  const route = state.route;
  if (route.view === "hub") {
    await renderHub();
    return;
  }
  if (route.view === "board") {
    if (!state.runsLoaded) {
      renderBoard();
      loadRuns();
      return;
    }
    renderBoard();
    return;
  }
  if (route.view === "config") {
    await renderConfig();
    return;
  }
  if (route.view === "tests") {
    await renderTests();
    return;
  }
  if (route.view === "test-edit" || route.view === "test-new") {
    await renderTestEditor();
    return;
  }
  if (route.view === "suites") {
    await renderSuites();
    return;
  }
  if (route.view === "suite-edit" || route.view === "suite-new") {
    await renderSuiteEditor();
    return;
  }
  if (route.view !== "detail") {
    renderBoard();
    return;
  }
  try {
    const run = await ensureRunDetail(route.suiteId, route.runId);
    const selected = findTest(run, route.categoryId, route.testId);
    const alreadyInStage = Boolean(document.querySelector(".stage"));
    if (!route.testId) {
      renderRunOverview(run);
      if (!alreadyInStage) document.getElementById("back")?.focus();
      return;
    }
    renderDetail(run, selected);
    if (!alreadyInStage) document.getElementById("back-run")?.focus();
  } catch (err) {
    state.error = err.message;
    setHash({ view: "board" });
  }
}

app.addEventListener("click", (event) => {
  const statusBtn = event.target.closest("[data-status]");
  if (statusBtn) {
    state.statusFilter = statusBtn.getAttribute("data-status");
    renderBoard();
    return;
  }
  const toggle = event.target.closest("[data-toggle-run]");
  if (toggle) {
    const key = toggle.getAttribute("data-toggle-run");
    if (state.openRuns.has(key)) state.openRuns.delete(key);
    else state.openRuns.add(key);
    renderBoard();
    return;
  }
  const openRun = event.target.closest("[data-open-run]");
  if (openRun) {
    const [suiteId, runId] = openRun.getAttribute("data-open-run").split("/");
    state.openRuns.add(`${suiteId}/${runId}`);
    setHash({ view: "detail", suiteId, runId });
    return;
  }
  const open = event.target.closest("[data-open-test]");
  if (open) {
    const [suiteId, runId, categoryId, testId] = open.getAttribute("data-open-test").split("/");
    state.openRuns.add(`${suiteId}/${runId}`);
    setHash({ view: "detail", suiteId, runId, categoryId, testId });
    return;
  }
  if (event.target.closest("#back-run")) {
    const route = state.route;
    setHash({ view: "detail", suiteId: route.suiteId, runId: route.runId });
    return;
  }
  if (event.target.closest("#back")) {
    setHash({ view: "board" });
    return;
  }
  if (typeof handleStudioClick === "function" && handleStudioClick(event)) {
    return;
  }
});

filterInput.addEventListener("input", () => {
  state.query = filterInput.value;
  const view = state.route.view;
  if (view === "board") renderBoard();
  else if (view === "tests") renderTests();
  else if (view === "suites") renderSuites();
});

refreshBtn.addEventListener("click", () => {
  state.details = {};
  state.workspace = null;
  state.editor = null;
  state.runsLoaded = false;
  if (state.route.view === "board" || state.route.view === "detail") loadRuns();
  else render();
});

saveBtn.addEventListener("click", () => {
  if (typeof saveCurrent === "function") saveCurrent();
});

window.addEventListener("hashchange", () => {
  state.route = parseHash();
  state.editor = null;
  render();
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.route.view === "detail") {
    if (state.route.testId) {
      setHash({ view: "detail", suiteId: state.route.suiteId, runId: state.route.runId });
    } else {
      setHash({ view: "board" });
    }
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
    if (!saveBtn.hidden) {
      event.preventDefault();
      if (typeof saveCurrent === "function") saveCurrent();
    }
  }
});

function boot() {
  render();
}
