const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "doubao_work2api", "web", "app.js"), "utf8");

function extractFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const prefixStart = source.slice(Math.max(0, start - 6), start) === "async " ? start - 6 : start;
  const bodyStart = source.indexOf("{", start);
  let depth = 0;
  let quote = null;
  let escaped = false;
  let templateDepth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === '"' || character === "'" || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(prefixStart, index + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

function makeContext(extra = {}) {
  const context = {
    console,
    Intl,
    Number,
    String,
    Promise,
    Set,
    Map,
    Error,
    JSON,
    state: { accounts: [], selected: new Set() },
    ...extra,
  };
  vm.createContext(context);
  return context;
}

function loadFunctions(names, context) {
  const code = names.map(extractFunction).join("\n");
  vm.runInContext(code, context);
  return context;
}

function testFormatTime() {
  const context = loadFunctions(["timestampDate", "formatTime"], makeContext());
  assert.equal(context.formatTime("2026-11-13T02:03:04+00:00"), "10:03:04");
  assert.equal(context.formatTime(1763000000), "10:13:20");
  assert.equal(context.formatTime(1763000000000), "10:13:20");
  assert.equal(context.formatTime("1763000000"), "10:13:20");
  assert.equal(context.formatTime(""), "—");
  assert.equal(context.formatTime(null), "—");
  assert.equal(context.formatTime("not-a-date"), "—");
  assert.doesNotThrow(() => context.formatTime("2026-11-13T02:03:04Z"));
}

function testAccountImportDocuments() {
  const context = loadFunctions(["accountImportDocuments"], makeContext());
  const one = { id: "one", cookies: { sessionid: "secret" } };
  const two = { id: "two" };
  assert.deepEqual(JSON.parse(JSON.stringify(context.accountImportDocuments(one))), [one]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.accountImportDocuments([one, two]))), [one, two]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.accountImportDocuments({ accounts: [one, two] }))), [one, two]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.accountImportDocuments(JSON.stringify({ accounts: [one] })))), [one]);
  assert.throws(() => context.accountImportDocuments({ accounts: [] }), /没有提供账号/);
  assert.throws(() => context.accountImportDocuments({ accounts: "bad" }), /accounts 字段必须是账号数组/);
  assert.throws(() => context.accountImportDocuments(["bad"]), /账号 JSON 必须是账号对象/);
  assert.throws(() => context.accountImportDocuments("not json"), /账号 JSON 格式无效/);
}

function testAccountImportFile() {
  const context = loadFunctions(["parseAccountImportFile"], makeContext());
  const one = { id: "one" };
  assert.deepEqual(JSON.parse(JSON.stringify(context.parseAccountImportFile(JSON.stringify(one)))), [one]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.parseAccountImportFile(JSON.stringify([one])))), [one]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.parseAccountImportFile(JSON.stringify({ accounts: [one] })))), [one]);
  assert.throws(() => context.parseAccountImportFile("not json"), /不是有效的 JSON/);
  assert.throws(() => context.parseAccountImportFile("null"), /文件必须包含账号/);
}

function testMissingKeyAccounts() {
  const context = loadFunctions(["missingKeyAccounts"], makeContext({
    state: { accounts: [{ id: "live" }] },
  }));
  assert.deepEqual(JSON.parse(JSON.stringify(context.missingKeyAccounts({ accounts: ["live", "gone"] }))), ["gone"]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.missingKeyAccounts({ accounts: [] }))), []);
  assert.deepEqual(JSON.parse(JSON.stringify(context.missingKeyAccounts(null))), []);
}

function testMissingBindingSaveGuard() {
  const context = loadFunctions(["missingKeyAccounts", "selectedAccountsForSave"], makeContext({
    state: { accounts: [{ id: "live" }] },
  }));
  const key = { accounts: ["gone", "live"] };
  assert.throws(
    () => context.selectedAccountsForSave(key, ["gone", "live"]),
    /已删除.*不会自动转为允许全部账号/,
  );
  assert.deepEqual(JSON.parse(JSON.stringify(context.selectedAccountsForSave(key, ["live"]))), ["live"]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.selectedAccountsForSave(key, []))), []);
  assert.deepEqual(JSON.parse(JSON.stringify(context.selectedAccountsForSave(null, []))), []);
}

function testFilterAndSelection() {
  const elements = {
    "#accountSearch": { value: "" },
    "#accountStatus": { value: "" },
    "#accountErrorCode": { value: "" },
  };
  const context = loadFunctions(["escapeHtml", "updateAccountFilters", "filteredAccounts", "visibleAccounts", "renderSelection"], makeContext({
    state: {
      accounts: [
        { id: "one", name: "Alpha", state: "ready", error_code: 1 },
        { id: "two", name: "Beta", state: "cooling", error_code: 2 },
      ],
      selected: new Set(["one"]),
      accountPage: 0,
      pageSize: 1,
    },
    $: (selector) => elements[selector],
    $$: (selector) => selector === "[data-batch]" ? elements.batchButtons : [],
  }));
  elements["#accountErrorCode"] = { value: "", innerHTML: "" };
  elements["#selectionSummary"] = { textContent: "" };
  elements.batchButtons = [{ disabled: false }, { disabled: false }];
  context.updateAccountFilters();
  assert.match(elements["#accountErrorCode"].innerHTML, /value="v:1"/);
  assert.deepEqual(JSON.parse(JSON.stringify(context.filteredAccounts().map((account) => account.id))), ["one", "two"]);
  elements["#accountErrorCode"].value = "v:1";
  assert.deepEqual(JSON.parse(JSON.stringify(context.filteredAccounts().map((account) => account.id))), ["one"]);
  elements["#accountErrorCode"].value = "";
  elements["#accountSearch"].value = "beta";
  assert.deepEqual(JSON.parse(JSON.stringify(context.filteredAccounts().map((account) => account.id))), ["two"]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.visibleAccounts().map((account) => account.id))), ["two"]);
  context.renderSelection();
  assert.match(elements["#selectionSummary"].textContent, /筛选结果 1 个.*已选 1 个（筛选外 1 个）/);
  assert.equal(elements.batchButtons.every((button) => button.disabled === false), true);
  elements["#accountSearch"].value = "alpha";
  context.renderSelection();
  assert.match(elements["#selectionSummary"].textContent, /筛选结果 1 个.*已选 1 个（筛选外 0 个）/);
  elements["#accountSearch"].value = "";
  context.state.accounts.push({ id: "empty", state: "ready", error_code: "" });
  elements["#accountErrorCode"].value = "v:";
  assert.deepEqual(JSON.parse(JSON.stringify(context.filteredAccounts().map((account) => account.id))), ["empty"]);
  context.state.selected.clear();
  context.renderSelection();
  assert.equal(elements.batchButtons.every((button) => button.disabled === true), true);
}

function testCatalogFiltering() {
  const context = loadFunctions(["formatCatalogTokens", "catalogEffortLabel", "filterCatalogModels"], makeContext());
  const models = [
    { id: "person-kimi-k3-261111", name: "Kimi K3", price: "1.48x", image_input: true, context_window: 300000, reasoning_efforts: ["low", "high", "max"] },
    { id: "seed-lite-7b", name: "豆包 Lite", price: "免费", image_input: null, context_window: 256000, reasoning_efforts: ["low", "medium", "high"] },
  ];
  const ids = (query) => context.filterCatalogModels(models, query, models[0].id).map((model) => model.id).join(",");
  assert.equal(ids("  KIMI  "), models[0].id);
  assert.equal(ids("免费 256k"), models[1].id);
  assert.equal(ids("image 推理"), models[0].id);
  assert.equal(ids("默认"), models[0].id);
  assert.equal(ids("not-a-model"), "");
  assert.equal(ids("  "), models.map((model) => model.id).join(","));
  assert.equal(models.length, 2);
}

function testUsageExcludesRunningRequestsFromCompletedAverage() {
  const context = loadFunctions(["aggregateModelUsage"], makeContext());
  const rows = context.aggregateModelUsage([
    { model: "alpha", status: "success", duration_ms: 1000 },
    { model: "alpha", status: "error", duration_ms: 3000 },
    { model: "alpha", status: "running", duration_ms: 90000 },
    { model: "beta", status: "running", duration_ms: 80000 },
  ]);
  assert.equal(rows[0].model, "alpha");
  assert.equal(rows[0].total, 3);
  assert.equal(rows[0].finished, 2);
  assert.equal(rows[0].duration / rows[0].finished, 2000);
  assert.equal(rows[1].finished, 0);
  assert.equal(rows[1].duration, 0);
}

function testErrorDetailsEscapeUntrustedText() {
  const context = loadFunctions(["escapeHtml", "renderErrorDetails"], makeContext());
  assert.match(context.renderErrorDetails(""), /—/);
  const short = context.renderErrorDetails('<img src=x onerror="bad()">');
  assert.ok(!short.includes("<img"));
  assert.match(short, /&lt;img/);
  const long = context.renderErrorDetails('<script>bad()</script>' + "long error ".repeat(20));
  assert.match(long, /<details/);
  assert.ok(!long.includes("<script>"));
  assert.match(long, /&lt;script/);
}

function testRequestMetricsKeepUnknownAndEstimatedDistinct() {
  const context = loadFunctions(["escapeHtml", "renderRequestMetric"], makeContext());
  assert.match(context.renderRequestMetric(null), /—/);
  assert.match(context.renderRequestMetric(0), />0</);
  assert.match(context.renderRequestMetric(1234, true), /≈1,234/);
  assert.ok(!context.renderRequestMetric('<img src=x>').includes('<img'));
}

testRequestMetricsKeepUnknownAndEstimatedDistinct();
function testAutomationRecordsRespectDayAndRunningState() {
  const context = loadFunctions(['automationAccountRows'], makeContext());
  const accounts = ['today','old','pending','running','queued'].map((id) => ({id, available:true}));
  const benefits = {today:'2026-11-14', config:{account_ids:['today','pending']}, running:['running'], accounts:{
    today:{checkin:{day:'2026-11-14',status:'claimed',updated_at:110}},
    old:{checkin:{day:'2026-11-13',status:'done',updated_at:50}},
    pending:{checkin:{day:'2026-11-14',status:'pending',updated_at:110}},
  }};
  const rows = context.automationAccountRows(accounts, benefits, new Set(['today','pending','queued']), 100);
  assert.deepEqual(Array.from(rows, (row) => row.status), ['done','unknown','pending','running','queued']);
  assert.equal(rows[0].planned, true);
  assert.equal(rows[1].planned, false);
  assert.equal(rows[1].record.day, '2026-11-13');
  assert.equal(context.automationAccountRows(accounts, {...benefits,today:'2026-11-15'})[0].status, 'unknown');
}

function testAutomationScopeCannotBroadenAfterAccountDisappears() {
  const context = loadFunctions(['automationScopeIds'], makeContext());
  const accounts = [{id:'one',available:true},{id:'two',available:true},{id:'disabled',available:false}];
  assert.deepEqual(Array.from(context.automationScopeIds(accounts, '')), ['one','two']);
  assert.deepEqual(Array.from(context.automationScopeIds(accounts, 'one')), ['one']);
  assert.deepEqual(Array.from(context.automationScopeIds(accounts, '', new Set(['one']))), ['two']);
  assert.throws(() => context.automationScopeIds(accounts, 'gone'), /已删除/);
  assert.throws(() => context.automationScopeIds(accounts, 'disabled'), /停用/);
}

testAutomationRecordsRespectDayAndRunningState();
function testManualCheckinRequiresAttentionAndReadOnlyRecheck() {
  const context = loadFunctions(['automationAccountRows'], makeContext());
  const accounts = [{id:'manual',available:true}];
  const benefits = {today:'2026-11-14', accounts:{manual:{checkin:{
    day:'2026-11-14',status:'manual_required',code:'710010702',query_only:true,
  }}}};
  const row = context.automationAccountRows(accounts, benefits)[0];
  assert.equal(row.status, 'attention');
  assert.equal(row.label, '需官方签到');
  assert.equal(row.queryOnly, true);
  assert.equal(context.automationAccountRows(accounts, {...benefits,today:'2026-11-15'})[0].queryOnly, false);
}
testManualCheckinRequiresAttentionAndReadOnlyRecheck();
testAutomationScopeCannotBroadenAfterAccountDisappears();
testFormatTime();
testCatalogFiltering();
testUsageExcludesRunningRequestsFromCompletedAverage();
testErrorDetailsEscapeUntrustedText();
testAccountImportDocuments();
testAccountImportFile();
testMissingKeyAccounts();
testMissingBindingSaveGuard();
testFilterAndSelection();

async function testAutomationExecutionWaitsForFinalSnapshotAndReleasesBusyState() {
  let resolvePrior;
  let priorFinished = false;
  let cleared = 0;
  let refreshed = 0;
  const modelState = {sessionEpoch:1,accounts:[{id:'one',available:true}],benefits:{running:[]},automationRunningIds:new Set(),automationStartedAt:0};
  modelState.refreshPromise = new Promise((resolve) => { resolvePrior = resolve; });
  const context = loadFunctions(['checkinAccounts'], makeContext({
    state:modelState,
    renderAutomation() {},
    setInterval() { return 7; },
    clearInterval(id) { assert.equal(id,7); cleared += 1; },
    async api(path, options) {
      assert.equal(path,'/admin/checkin');
      assert.deepEqual(JSON.parse(options.body), {ids:['one']});
      setTimeout(() => { priorFinished = true; modelState.refreshPromise = null; resolvePrior(); },0);
      return {results:[{id:'one',status:'done'}]};
    },
    async refresh() { assert.ok(priorFinished); refreshed += 1; },
  }));
  const result = await context.checkinAccounts(['one'], false);
  assert.equal(result[0].status,'done');
  assert.equal(refreshed,1);
  assert.equal(modelState.automationRunningIds.size,0);
  assert.equal(modelState.automationPollTimer,null);
  assert.ok(cleared >= 1);
}

testAutomationExecutionWaitsForFinalSnapshotAndReleasesBusyState().then(() => {
  console.log("test_web.js: all tests passed");
}).catch((error) => { console.error(error); process.exitCode = 1; });
