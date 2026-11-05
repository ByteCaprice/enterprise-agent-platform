// Same-origin, dependency-free control plane. No storage, telemetry or model transport.
const $ = (selector) => document.querySelector(selector);
const state = { token: "", authMode: "key", project: "", session: null, epoch: 0, page: "tasks", selection: 0, timer: null, requests: new Set() };
const encode = encodeURIComponent;
const projectPath = (tail = "") => `/v2/projects/${encode(state.project)}${tail}`;
const isCurrent = (epoch) => epoch === state.epoch && Boolean(state.token);
const pretty = (value) => JSON.stringify(value, null, 2);
const time = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const statuses = { queued: "排队中", running: "执行中", succeeded: "已完成", failed: "失败", timed_out: "超时", cancelled: "已取消", pending: "待处理", leased: "投递中", delivered: "已送达", dead: "投递终止", skipped: "已跳过", published: "已发布", draft: "草稿", enabled: "已启用", disabled: "已停用", active: "有效", expired: "已过期", revoked: "已撤销" };
const kinds = { workflow_node: "流程节点", agent_execution: "Agent", agent_step: "Agent Step", model_call: "模型调用", tool_call: "工具调用" };

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key === "value") node.value = value;
    else if (key === "text") node.textContent = value;
    else if (typeof value === "boolean") node[key] = value;
    else if (value != null) node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
const button = (label, action, style = "") => el("button", { type: "button", class: `button ${style}`, onclick: action }, label);
const badge = (status) => el("span", { class: `badge ${Object.hasOwn(statuses, status) ? status : ""}` }, statuses[status] || status);
const panel = (title, description = "", ...content) => el("section", { class: "card" }, el("h2", {}, title), description ? el("p", {}, description) : null, content);
const field = (label, node) => el("label", {}, label, node);
const input = (placeholder = "", value = "") => el("input", { placeholder, value, required: true, autocomplete: "off" });
const editor = (value, compact = false) => el("textarea", { value: pretty(value), spellcheck: "false", class: compact ? "compact" : "", required: true });
const json = (node) => { try { return JSON.parse(node.value); } catch { throw new Error("JSON 格式无效，请检查后再提交。"); } };
const note = (text) => el("div", { class: "form-note" }, text);
const metaLine = (...pieces) => el("div", { class: "item-meta" }, ...pieces.map((piece) => el("span", {}, piece)));
const message = (error) => error?.message || "操作失败，请检查权限、配置与网络。";
function notice(text, error = false) { const node = $("#notice"); node.textContent = text; node.className = error ? "error" : ""; node.hidden = !text; }
function report(error) { if (error?.name !== "AbortError") notice(message(error), true); }
function form(action, ...children) {
  const node = el("form", { class: "form-stack" }, children);
  node.addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = node.querySelector('[type="submit"]'); if (submit?.disabled || node.dataset.pending === "true") return;
    node.dataset.pending = "true";
    if (submit) submit.disabled = true;
    try { await action(); } catch (error) { report(error); } finally { node.dataset.pending = "false"; if (submit) submit.disabled = node.dataset.locked === "true"; }
  });
  return node;
}
const submit = (label) => el("button", { type: "submit", class: "button primary" }, label);

async function api(path, options = {}) {
  if (!state.token || !path.startsWith("/v2/")) throw new Error("请先连接工作空间。");
  const epoch = state.epoch;
  const controller = new AbortController(); state.requests.add(controller);
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 20000);
  try {
    const response = await fetch(path, { method: options.method || "GET", body: options.body === undefined ? undefined : JSON.stringify(options.body),
      headers: { ...(state.authMode === "oidc" ? { Authorization: `Bearer ${state.token}` } : { "x-api-key": state.token }), ...options.headers, ...(options.body === undefined ? {} : { "Content-Type": "application/json" }) },
      signal: controller.signal, cache: "no-store", redirect: "error", credentials: "omit" });
    if (!isCurrent(epoch)) throw new DOMException("Connection changed", "AbortError");
    if (response.status === 401) { disconnect(); throw new Error("凭据已失效或身份已停用，请重新连接。"); }
    const data = response.status === 204 ? null : await response.json();
    if (!isCurrent(epoch)) throw new DOMException("Connection changed", "AbortError");
    if (!response.ok) {
      const id = response.headers.get("X-Request-ID") || data?.error?.request_id;
      const error = new Error(`${response.status} · ${data?.error?.message || data?.detail || "请求未被平台接受"}${id ? ` · 请求编号 ${id}` : ""}`);
      error.status = response.status; throw error;
    }
    return data;
  } catch (error) {
    if (timedOut && isCurrent(epoch)) {
      const timeoutError = new Error("请求超时，操作可能已经生效。请先查询状态；保留原请求 ID，不要盲目重试。");
      timeoutError.uncertain = true; throw timeoutError;
    }
    if (options.method && options.method !== "GET" && (!error.status || error.status >= 500)) error.uncertain = true;
    throw error;
  } finally { clearTimeout(timeout); state.requests.delete(controller); }
}

function disconnect() {
  state.epoch++; state.selection++; state.token = ""; state.project = ""; state.session = null;
  clearTimeout(state.timer); state.timer = null;
  for (const controller of state.requests) controller.abort(); state.requests.clear();
  $("#view").replaceChildren(panel("已断开连接", "页面凭据、执行内容和一次性 Key 已清除。重新连接以继续。"));
  $("#token-input").value = ""; $("#project-label").textContent = "未连接";
  $("#session-label").textContent = "密钥仅在当前页面内存中"; $("#disconnect").hidden = true; $("#connection").hidden = false;
  notice("");
}
$("#disconnect").addEventListener("click", disconnect);
$("#connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("#token-input").value.trim(), project = $("#project-input").value.trim();
  $("#token-input").value = "";
  if (!window.isSecureContext && !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) { notice("远程工作空间必须使用 HTTPS，避免明文传输 Key。", true); return; }
  disconnect(); state.project = project; state.token = token; state.authMode = $("#auth-mode").value; const epoch = state.epoch;
  try {
    const session = await api("/v2/session");
    if (!isCurrent(epoch)) return;
    if (!session.is_admin && session.project_key !== project) throw new Error("该服务身份不属于指定项目。");
    state.session = session; $("#project-label").textContent = project;
    $("#session-label").textContent = `${session.is_admin ? "管理员" : "服务身份"} · ${session.name}`;
    $("#disconnect").hidden = false; $("#connection").hidden = true;
    await navigate(state.page);
  } catch (error) { if (isCurrent(epoch)) disconnect(); report(error); }
});
window.addEventListener("pagehide", disconnect);
document.querySelectorAll("[data-page]").forEach((node) => node.addEventListener("click", () => navigate(node.dataset.page).catch(report)));

async function navigate(page) {
  clearTimeout(state.timer); state.selection++; state.page = page; notice("");
  document.querySelectorAll("[data-page]").forEach((node) => node.classList.toggle("active", node.dataset.page === page));
  const headings = { tasks: ["让执行过程清晰可见。", "从一个有界任务开始，连接能力、Agent 与真实执行证据。"], capabilities: ["把能力做成可靠的积木。", "Agent、Workflow、Prompt 与策略统一发布，版本和依赖显式固定。"], resources: ["资源有边界，执行有依据。", "管理员审核模型目录与只读工具绑定；凭据只保存环境引用。"], governance: ["最小权限，清楚的归属。", "管理项目、服务身份与独立资源授权。OIDC 只接受管理员明确绑定，不信任 token 角色。"], operations: ["让运营形成闭环。", "显式配额与预算、独立回调交付、合成评测、人工质检和受控记忆。"] };
  $("#page-title").textContent = headings[page][0]; $("#page-description").textContent = headings[page][1];
  $("#view").replaceChildren();
  if (!state.session) { $("#view").append(panel("请先连接项目", "控制台没有离线假数据，也不会自动启动 Worker。")); return; }
  if (page === "tasks") await tasksPage();
  else if (page === "capabilities") await capabilitiesPage();
  else if (page === "resources") await resourcesPage();
  else if (page === "operations") await operationsPage();
  else await governancePage();
}

async function tasksPage() {
  const epoch = state.epoch, selection = state.selection;
  const stats = el("div", { class: "stats" });
  const list = el("div", { class: "item-list" });
  const detail = el("div", { class: "stack" }, panel("选择一个任务", "这里将显示真实 Attempt、执行树与持久事件。"));
  const taskKey = input("capability-key"), version = input("1"), idempotency = input("请求唯一标识", crypto.randomUUID());
  const values = editor({});
  const create = form(async () => {
    const body = { capability_key: taskKey.value.trim(), version: version.value.trim(), input: json(values) };
    if (!confirm(`提交 ${body.capability_key}@${body.version}？\n已安装的 Worker 可能调用已配置模型。超时后请保留相同请求 ID 查询，不要盲目重试。`)) return;
    const record = await api(projectPath("/tasks"), { method: "POST", body, headers: { "Idempotency-Key": idempotency.value.trim() } });
    if (state.page !== "tasks") return;
    notice(`任务 ${record.id} 已接入，状态 ${statuses[record.status]}。queued/claimed 不表示成功。`);
    await loadList(); await selectTask(record, detail);
  }, el("div", { class: "form-grid" }, field("能力 Key", taskKey), field("固定版本", version)), field("输入 JSON", values),
    field("Idempotency-Key", idempotency), el("div", { class: "actions" }, submit("提交有界任务 →"), button("新请求 ID", () => { idempotency.value = crypto.randomUUID(); }, "ghost")),
    note("身份、权限与输入契约由服务端校验。请求 ID 不会因失败自动更换。"));
  const queue = panel("任务列表", "仅统计最近 100 条可见任务，不代表全项目总量。",
    el("div", { class: "actions" }, button("刷新列表", () => loadList().catch(report), "small")), el("div", { class: "divider" }), list);
  $("#view").append(stats, el("div", { class: "columns" }, el("div", { class: "stack" }, queue, panel("新任务", "明确指定能力与版本，不做隐式意图路由。", create)), detail));
  async function loadList() {
    const records = await api(projectPath("/tasks?limit=100"));
    if (!isCurrent(epoch) || state.page !== "tasks" || selection > state.selection) return;
    list.replaceChildren(...records.map((record) => el("button", { type: "button", class: "list-item", onclick: () => selectTask(record, detail).catch(report) },
      el("div", { class: "item-line" }, el("span", { class: "item-title" }, record.capability_key), badge(record.status)),
      metaLine(`v${record.version} · ${record.capability_kind}`, time(record.create_time)), el("div", { class: "task-id" }, record.id))));
    if (!records.length) list.append(note("还没有可见任务。提交一个有界请求开始。"));
    const groups = [["可见任务", records.length], ["等待与执行", records.filter((r) => ["queued", "running"].includes(r.status)).length], ["已完成", records.filter((r) => r.status === "succeeded").length], ["需关注", records.filter((r) => ["failed", "timed_out"].includes(r.status)).length]];
    stats.replaceChildren(...groups.map(([label, count]) => el("div", { class: "stat" }, el("div", { class: "stat-label" }, label), el("div", { class: "stat-value" }, count), el("div", { class: "stat-caption" }, "RECENT VISIBLE TASKS"))));
  }
  try { await loadList(); } catch (error) { list.append(el("p", { class: "error-text" }, message(error))); }
}

async function selectTask(initial, container) {
  const selection = ++state.selection, epoch = state.epoch, taskPath = projectPath(`/tasks/${encode(initial.id)}`);
  clearTimeout(state.timer);
  const current = () => isCurrent(epoch) && state.selection === selection && state.page === "tasks";
  let record = initial, attempts = [], attemptId = null, treeCursor = null, nextAfter = 0, eventRows = [], treeRecords = new Map(), treeGeneration = 0;
  const title = el("div"), meta = el("dl", { class: "metadata" }), cancellation = el("div", { class: "help" });
  const attemptSelect = el("select", { onchange: () => { attemptId = attemptSelect.value; loadTree(true).catch(report); } });
  const tree = el("div"), events = el("div", { class: "timeline" }), eventNote = note(""), result = el("div");
  const moreTree = button("更多证据", () => loadTree(false).catch(report), "small"); moreTree.hidden = true;
  const moreEvents = button("继续读取事件", () => loadEvents().catch(report), "small"); moreEvents.hidden = true;
  const cancel = button("请求取消", async () => {
    if (!confirm("请求取消当前任务？\n202 表示意图已保存，只有 Worker 停止确认后才是 cancelled；不保证远端回滚。")) return;
    cancel.disabled = true;
    try { const receipt = await api(`${taskPath}/cancel`, { method: "POST" }); if (!current()) return; notice(`取消回执：${receipt.cancellation.state}。`); await refreshMetadata(); await loadEvents(); }
    catch (error) { report(error); } finally { cancel.disabled = false; }
  }, "danger small");
  container.replaceChildren(panel("任务详情", "Task 决定最终状态；子 Agent 成功不等于任务成功。",
    el("div", { class: "panel-head" }, title, cancel), el("div", { class: "task-id" }, initial.id), meta, cancellation),
    panel("执行树", "真实父级与互斥调用 owner。下方是元数据快照，不含参数或消息正文。", field("TaskAttempt", attemptSelect),
      el("div", { class: "actions" }, button("刷新证据快照", () => loadTree(true).catch(report), "small ghost")), tree, moreTree),
    panel("持久时间线", "按 Task 序号续读；状态与事件同事务提交，不把日志正文当事件。", events, eventNote, moreEvents),
    observationPanel(`${taskPath}/requests`, "接入请求", "每次已受理请求独立留证；幂等复用不会再次执行 Task。关联标识不是权限凭证。", current,
      (row) => el("div", { class: "list-item" }, el("div", { class: "item-line" }, el("span", { class: "item-title" }, row.operation), row.task_created ? "创建 Task" : "复用 Task"),
        metaLine(row.id, time(row.create_time)), metaLine(`交付偏好 ${row.delivery_mode}`, `关联标识 ${row.correlation_id || "—"}`))),
    observationPanel(`${taskPath}/deliveries`, "Callback 交付", "Task 成功不等于通知送达。投递独立进行；终止或结果未知不代表 Task 失败，也不应据此重跑 Task。", current,
      (row) => el("div", { class: "list-item" }, el("div", { class: "item-line" }, el("span", { class: "item-title" }, row.endpoint_key), badge(row.state)),
        metaLine(row.id, `事件 #${row.event_sequence}`, `已尝试 ${row.attempts} 次`),
        metaLine(row.http_status ? `最近 HTTP ${row.http_status}` : "暂无 HTTP 确认", row.error_code || "—"),
        ["pending", "leased"].includes(row.state) ? note(`调度时间 ${time(row.next_attempt_at)}；实际执行取决于 Worker、租约和端点启用状态。`) : null)),
    panel("最终结果", "按需读取，不自动展示客户结果正文。失败或尚未完成时 result 为 null。",
      button("读取当前结果", async () => { try { const data = await api(`${taskPath}/result`); if (current()) result.replaceChildren(el("pre", {}, pretty(data))); } catch (error) { report(error); } }, "small"), result));
  function renderTree() {
    const all = [...treeRecords.values()]; const seen = new Set(); tree.replaceChildren();
    const key = (item) => `${item.kind}:${item.record.id}`;
    function visit(item, depth) {
      if (seen.has(key(item))) return; seen.add(key(item));
      const row = item.record; const label = row.node_key || row.agent_key || row.tool_alias || row.resource_key || row.policy_key || row.tool_key || row.id;
      tree.append(el("div", { class: `tree-row depth-${Math.min(depth, 5)}` }, el("div", { class: "item-line" }, el("span", { class: "item-title" }, `${kinds[item.kind]} · ${label}`), badge(row.status)),
        metaLine(row.kind === "model_turn" || row.kind === "tool_call" ? `turn ${row.turn} / slot ${row.slot}` : row.id.slice(0, 16), row.error_code || time(row.finish_time || row.start_time || row.create_time))));
      all.filter((child) => `${child.parent.kind}:${child.parent.id}` === key(item))
        .sort((a, b) => (a.record.turn || 0) - (b.record.turn || 0) || (a.record.kind === "model_turn" ? 0 : 1) - (b.record.kind === "model_turn" ? 0 : 1) || (a.record.slot || 0) - (b.record.slot || 0))
        .forEach((child) => visit(child, depth + 1));
    }
    all.filter((item) => item.parent.kind === "attempt").forEach((item) => visit(item, 0));
    all.filter((item) => !seen.has(key(item))).forEach((item) => visit(item, 0));
    if (!all.length) tree.append(note("此 Attempt 尚无从属执行证据。"));
    moreTree.hidden = !treeCursor;
  }
  async function loadTree(reset) {
    const generation = reset ? ++treeGeneration : treeGeneration;
    if (!attemptId) { tree.replaceChildren(note("尚未创建 Attempt；请检查是否有匹配的 Worker。")); return; }
    const requestedAttempt = attemptId;
    const data = await api(`${taskPath}/attempts/${encode(attemptId)}/execution-tree?limit=200${!reset && treeCursor ? `&cursor=${encode(treeCursor)}` : ""}`);
    if (!current() || requestedAttempt !== attemptId || generation !== treeGeneration) return;
    if (reset) treeRecords = new Map();
    data.items.forEach((item) => treeRecords.set(`${item.kind}:${item.record.id}`, item)); treeCursor = data.next_cursor; renderTree();
  }
  async function loadEvents() {
    const after = nextAfter;
    const data = await api(`${taskPath}/events?limit=200&after_sequence=${after}`);
    if (!current() || after !== nextAfter) return;
    nextAfter = data.next_after; eventRows = [...eventRows, ...data.items].slice(-500);
    events.replaceChildren(...eventRows.map((event) => el("div", { class: "event" }, el("div", { class: "event-seq" }, String(event.sequence).padStart(3, "0")),
      el("div", {}, el("div", { class: "event-title" }, event.event_type.replaceAll("_", " ")), el("div", { class: "event-meta" }, `${time(event.occurred_at)} · ${event.entity_kind} · ${event.entity_id.slice(0, 16)}`)))));
    eventNote.textContent = `已读至 ${nextAfter} / ${data.head_sequence}；页面最多保留最近 500 条。${data.history_complete ? "" : " 此任务创建于事件流启用前，早期历史没有伪造回填。"}`;
    moreEvents.hidden = !data.has_more;
  }
  async function refreshMetadata() {
    const [task, receipt, list] = await Promise.all([api(taskPath), api(`${taskPath}/cancellation`), api(`${taskPath}/attempts`)]);
    if (!current()) return;
    record = task; attempts = list; title.replaceChildren(el("span", { class: "item-title" }, `${task.capability_key}@${task.version}`), " ", badge(task.status));
    cancel.hidden = !["queued", "running"].includes(task.status);
    meta.replaceChildren(...[["能力类型", task.capability_kind], ["提交主体", task.submitted_by], ["创建时间", time(task.create_time)], ["完成时间", time(task.finish_time)]].map(([key, value]) => el("div", {}, el("dt", {}, key), el("dd", {}, value))));
    cancellation.textContent = receipt.cancellation ? `取消状态：${receipt.cancellation.state} · 请求 ${receipt.cancellation.request_id}。只有 acknowledged 才确认本地停止；不承诺远端回滚。` : "没有取消意图。执行授权和租约保护始终由服务端检查。";
    const previous = attemptId;
    if (!attempts.some((attempt) => attempt.id === attemptId)) attemptId = attempts.at(-1)?.id || null;
    attemptSelect.replaceChildren(...(attempts.length ? attempts.map((attempt) => el("option", { value: attempt.id }, `#${attempt.number} · ${statuses[attempt.status]} · ${attempt.id.slice(0, 12)}`)) : [el("option", { value: "" }, "尚无 Attempt")]));
    attemptSelect.value = attemptId || "";
    if (attemptId !== previous) await loadTree(true);
  }
  async function poll() {
    if (!current()) return;
    try { if (document.visibilityState === "visible") { await refreshMetadata(); await loadEvents(); } }
    catch (error) { if (current()) report(error); }
    if (current() && ["queued", "running"].includes(record.status)) state.timer = setTimeout(poll, 2500);
  }
  try { await refreshMetadata(); await loadEvents(); if (current() && ["queued", "running"].includes(record.status)) state.timer = setTimeout(poll, 2500); }
  catch (error) { if (current()) report(error); }
}

function observationPanel(path, title, description, current, render) {
  let cursor = null, busy = false;
  const rows = el("div", { class: "item-list" }), summary = note("按需读取元数据；每页重新授权。"), errorNote = el("p", { class: "error-text" });
  const refresh = button("读取 / 刷新", () => load(true), "small ghost");
  const more = button("下一页", () => load(false), "small"); more.hidden = true;
  async function load(reset) {
    if (busy || !current() || !reset && !cursor) return;
    busy = true; refresh.disabled = true; more.disabled = true; errorNote.textContent = "";
    try {
      const data = await api(`${path}?limit=50${!reset && cursor ? `&cursor=${encode(cursor)}` : ""}`);
      if (!current()) return;
      cursor = data.next_cursor;
      rows.replaceChildren(...data.items.map(render));
      if (!data.items.length) rows.append(note("当前未观察到记录；不代表已交付，也不证明必须存在回调端点。"));
      summary.textContent = `本页 ${data.items.length} 条 · 观测时间 ${time(data.read_time)}。分页不是跨请求快照；刷新可查看已更新的投递状态。`;
      more.hidden = !cursor;
    } catch (error) { if (current() && error?.name !== "AbortError") errorNote.textContent = message(error); }
    finally { busy = false; refresh.disabled = false; more.disabled = false; }
  }
  return panel(title, description, el("div", { class: "actions" }, refresh, more), summary, errorNote, rows);
}

const definitions = {
  prompt: { template: "请根据输入完成审核，仅返回约定的 JSON。" },
  model_policy: { interaction: "agent_turn", provider_key: "provider", model: "catalog/model", max_tokens: 1024 },
  tool: { binding_key: "select-fields", operation_type: "read_only", timeout_seconds: 30 },
  agent: { definition_version: "agent.v1", runtime: "pi", instruction_prompt: { key: "instructions", version: "1" }, model_policy: { key: "agent-policy", version: "1" }, tools: [], limits: { max_turns: 4, max_tool_calls: 0, timeout_seconds: 60 } },
  workflow: { nodes: [{ key: "review", kind: "agent", capability: { key: "review-agent", version: "1" }, input_mapping: { name: { source: "input", pointer: "/name" } } }], output_mapping: { name: { source: "node", node: "review", pointer: "/name" } } },
};

async function capabilitiesPage() {
  const epoch = state.epoch, pageSelection = state.selection;
  const list = el("div", { class: "item-list" }), studio = el("div", { class: "stack" }, panel("选择能力", "查看固定版本、创建新草稿；发布版本不可覆盖。"));
  const key = input("review-agent"), name = input("审核 Agent"), kind = el("select", {}, ...Object.keys(definitions).map((value) => el("option", { value }, value)));
  const create = form(async () => { await api(projectPath("/capabilities"), { method: "POST", body: { key: key.value.trim(), name: name.value.trim(), kind: kind.value } }); notice("能力身份已创建，下一步创建版本草稿。"); await load(); },
    field("Key", key), field("名称", name), field("类型", kind), submit("创建能力身份"));
  $("#view").append(el("div", { class: "columns" }, el("div", { class: "stack" }, panel("能力目录", "包括 Agent、Workflow、Prompt、模型策略与工具定义。", button("刷新目录", () => load().catch(report), "small"), el("div", { class: "divider" }), list), panel("新能力", "资源身份与不可变版本分开管理。", create)), studio));
  async function load() {
    const records = await api(projectPath("/capabilities?limit=200"));
    if (!isCurrent(epoch) || state.page !== "capabilities") return;
    list.replaceChildren(...records.map((record) => el("button", { class: "list-item", type: "button", onclick: () => open(record).catch(report) }, el("div", { class: "item-line" }, el("span", { class: "item-title" }, record.name), el("span", { class: "badge" }, record.kind)), metaLine(record.key, `default ${record.default_version || "—"}`))));
    if (!records.length) list.append(note("没有可见能力。创建一个资源身份开始。"));
  }
  let opened = 0;
  async function open(capability) {
    const currentOpen = ++opened, base = projectPath(`/capabilities/${encode(capability.key)}`);
    const versions = await api(`${base}/versions`);
    if (!isCurrent(epoch) || state.page !== "capabilities" || currentOpen !== opened || pageSelection > state.selection) return;
    const version = input("例如 1.0.0"), definition = editor(definitions[capability.kind] || {}), inputSchema = editor({ type: "object" }, true), outputSchema = editor({ type: "object" }, true), dependencies = editor([], true);
    const inspection = el("div"), versionsList = el("div", { class: "item-list" });
    const draft = form(async () => {
      await api(`${base}/versions`, { method: "POST", body: { version: version.value.trim(), definition: json(definition), input_schema: json(inputSchema), output_schema: json(outputSchema), dependencies: json(dependencies) } });
      notice("新草稿已保存。发布会再次校验固定依赖与 Schema。"); await open(capability);
    }, field("新版本（不能覆盖）", version), field("定义 JSON", definition), el("div", { class: "form-grid" }, field("Input schema", inputSchema), field("Output schema", outputSchema)), field("固定依赖 [{key,version}]", dependencies), submit("保存新版本草稿"));
    for (const item of versions) {
      const path = `${base}/versions/${encode(item.version)}`;
      const actions = el("div", { class: "actions" }, button("查看 / 基于此编辑", async () => {
        try { const data = await api(path); if (currentOpen !== opened || state.page !== "capabilities") return; definition.value = pretty(data.definition); inputSchema.value = pretty(data.input_schema); outputSchema.value = pretty(data.output_schema); dependencies.value = pretty(data.dependencies); version.value = ""; inspection.replaceChildren(el("pre", {}, pretty(data))); } catch (error) { report(error); }
      }, "small"));
      if (item.status === "draft") actions.append(button("发布", async () => { if (!confirm(`发布 ${capability.key}@${item.version}？版本及依赖将不可变。`)) return; try { await api(`${path}/publish`, { method: "POST" }); notice("固定版本已发布。"); await open(capability); } catch (error) { report(error); } }, "primary small"));
      if (item.status === "published") actions.append(button("设为默认", async () => { try { await api(`${path}/set-default`, { method: "POST" }); notice("默认版本已切换；已有任务不受影响。"); await load(); } catch (error) { report(error); } }, "small ghost"));
      versionsList.append(el("div", { class: "list-item" }, el("div", { class: "item-line" }, item.version, badge(item.status)), actions));
    }
    studio.replaceChildren(panel(`${capability.name} / ${capability.key}`, "查看不是修改已发布内容；基于已有版本编辑后仍须指定新版本。", versionsList, inspection), panel("版本工作室", "Agent 与 Workflow 使用显式固定依赖。模板仅作编辑起点，不自动安装资源。", draft));
    if (capability.kind === "prompt") {
      const promptVersion = input("已发布版本"), variables = editor({}, true), output = el("div");
      studio.append(panel("Prompt Bank · 渲染", "只渲染固定模板，不调用模型。", form(async () => { const data = await api(projectPath(`/prompts/${encode(capability.key)}/versions/${encode(promptVersion.value)}/render`), { method: "POST", body: { variables: json(variables) } }); output.replaceChildren(el("pre", {}, pretty(data))); }, field("版本", promptVersion), field("变量 JSON", variables), submit("渲染 Prompt")), output));
    }
  }
  try { await load(); } catch (error) { list.append(el("p", { class: "error-text" }, message(error))); }
}

async function resourcesPage() {
  const columns = el("div", { class: "columns" }); $("#view").append(columns);
  if (!state.session.is_admin) { columns.append(panel("资源目录由管理员管理", "服务身份按独立 Grant 使用已发布资源，不能自行修改 Provider 或部署绑定。")); return; }
  await Promise.all([
    resourceCollection(columns, "Model Provider / Catalog", "/model-providers", { key: "provider", name: "Approved provider", base_url: "https://api.example.com/v1", credential_ref: "provider-key", models: ["catalog/model"], max_tokens: 4096 }, "只接受公共 HTTPS。这里保存环境凭据引用，不输入或保存供应商 Key。"),
    resourceCollection(columns, "只读工具绑定", "/tool-bindings", { key: "select-fields", name: "JSON projection", handler_key: "json.project", handler_version: "1", operation_type: "read_only", max_timeout_seconds: 30 }, "只能绑定已在 Worker 安装的审核实现；JSON 不能注册任意 Python 或业务端点。"),
  ]);
}
async function resourceCollection(parent, title, tail, sample, help) {
  const epoch = state.epoch, page = state.page, list = el("div", { class: "item-list" }), spec = editor(sample);
  const current = () => isCurrent(epoch) && state.page === page;
  async function load() {
    const records = await api(projectPath(tail)); if (!current()) return;
    list.replaceChildren(...records.map((record) => el("div", { class: "list-item" }, el("div", { class: "item-line" }, record.name, badge(record.enabled ? "enabled" : "disabled")), el("pre", {}, pretty(record)), button(record.enabled ? "停用" : "启用", async () => {
      if (!confirm(`${record.enabled ? "停用" : "启用"} ${record.key}？此操作会影响后续调用授权。`)) return;
      try { await api(projectPath(`${tail}/${encode(record.key)}`), { method: "PATCH", body: { enabled: !record.enabled } }); await load(); } catch (error) { report(error); }
    }, "small ghost"))));
    if (!records.length) list.append(note("还没有配置资源。"));
  }
  parent.append(panel(title, help, button("刷新目录", () => load().catch(report), "small"), el("div", { class: "divider" }), list, el("div", { class: "divider" }), form(async () => { await api(projectPath(tail), { method: "POST", body: json(spec) }); notice("资源配置已创建；没有执行外部调用。"); await load(); }, field("新资源配置 JSON", spec), submit("创建审核资源"))));
  try { await load(); } catch (error) { list.append(el("p", { class: "error-text" }, message(error))); }
}

async function governancePage() {
  if (!state.session.is_admin) { $("#view").append(panel("当前服务身份", "身份、Key 与 Grant 管理由项目管理员负责；当前页面不提供提权入口。", el("pre", {}, pretty(state.session)))); return; }
  const epoch = state.epoch, identities = el("div", { class: "item-list" }), details = el("div", { class: "stack" });
  const projectSpec = editor({ key: "new-project", name: "New project", description: "" }, true), identitySpec = editor({ key: "business-service", name: "Business service" }, true);
  const projectList = el("div", { class: "pill-row" });
  $("#view").append(el("div", { class: "columns" }, el("div", { class: "stack" },
    panel("项目", "创建后需主动断开并连接新项目，不自动授权或复制配置。", projectList, form(async () => { await api("/v2/projects", { method: "POST", body: json(projectSpec) }); notice("项目已创建；身份与资源需显式配置。"); await loadProjects(); }, field("新项目 JSON", projectSpec), submit("创建项目"))),
    panel("服务身份", "任务归属服务身份，而非提交时的 bearer Key。", identities, el("div", { class: "divider" }), form(async () => { await api(projectPath("/service-identities"), { method: "POST", body: json(identitySpec) }); notice("身份已创建，尚未获得任何资源权限。"); await loadIdentities(); }, field("新身份 JSON", identitySpec), submit("创建服务身份")))), details));
  const valid = () => isCurrent(epoch) && state.page === "governance";
  async function loadProjects() {
    const projects = await api("/v2/projects");
    if (!valid()) return;
    projectList.replaceChildren(...projects.map((project) => el("div", { class: "pill" }, project.key, " ", button(project.enabled ? "停用" : "启用", async () => {
      if (!confirm(`更改项目 ${project.key} 的状态？停用会阻止后续业务执行，不承诺撤销已发生的远端操作。`)) return;
      try { await api(`/v2/projects/${encode(project.key)}`, { method: "PATCH", body: { enabled: !project.enabled } }); await loadProjects(); } catch (error) { report(error); }
    }, "small ghost"))));
  }
  async function loadIdentities() {
    const records = await api(projectPath("/service-identities")); if (!valid()) return;
    identities.replaceChildren(...records.map((record) => el("button", { class: "list-item", type: "button", onclick: () => identityDetail(record).catch(report) }, el("div", { class: "item-line" }, record.name, badge(record.enabled ? "enabled" : "disabled")), el("div", { class: "task-id" }, record.id))));
  }
  let selected = null;
  async function identityDetail(identity) {
    selected = identity.id;
    const base = projectPath(`/service-identities/${encode(identity.id)}`);
    const [grants, keys] = await Promise.all([api(`${base}/grants`), api(`${base}/keys`)]);
    if (!valid() || selected !== identity.id) return;
    const grantSpec = editor({ action: "agent:invoke", resource_key: "review-agent" }, true), keyName = input("Key 名称", "service-key"), oneTime = el("div");
    const grantList = el("div", { class: "item-list" }, ...grants.map((grant) => el("div", { class: "list-item" }, el("div", { class: "item-line" }, `${grant.action} · ${grant.resource_key}`, button("撤销", async () => { if (!confirm("撤销该资源授权？正在执行的后续调用会重新检查。")) return; try { await api(`${base}/grants/${encode(grant.id)}`, { method: "DELETE" }); await identityDetail(identity); } catch (error) { report(error); } }, "small danger")))));
    const keyList = el("div", { class: "item-list" }, ...keys.map((key) => el("div", { class: "list-item" }, el("div", { class: "item-line" }, key.name, badge(key.revoked ? "revoked" : key.expires_at && Date.parse(key.expires_at) <= Date.now() ? "expired" : "active")), note(`到期 ${time(key.expires_at)} · ${key.id}`), key.revoked ? null : button("撤销 Key", async () => { if (!confirm("撤销此 Key？其 HTTP 访问将立即失效，已有任务仍归属服务身份。")) return; try { await api(`${base}/keys/${encode(key.id)}/revoke`, { method: "POST" }); await identityDetail(identity); } catch (error) { report(error); } }, "small danger"))));
    details.replaceChildren(panel(identity.name, identity.id, button(identity.enabled ? "停用身份" : "启用身份", async () => {
      if (!confirm("更改身份状态会影响该身份的执行权限，是否继续？")) return;
      try { await api(base, { method: "PATCH", body: { enabled: !identity.enabled } }); await loadIdentities(); details.replaceChildren(); } catch (error) { report(error); }
    }, "small danger")), panel("独立资源授权", "task:submit/read/cancel、agent:invoke、model:invoke、prompt:render、tool:invoke 互不隐含授权。", grantList, form(async () => { await api(`${base}/grants`, { method: "POST", body: json(grantSpec) }); await identityDetail(identity); }, field("新 Grant JSON", grantSpec), submit("授予明确权限"))),
      panel("服务 Key", "一次性明文只在当前视图展示；离开或断开即清除，不写入存储。", keyList, form(async () => {
        if (!confirm("为该身份签发新 Key？只展示一次，请通过安全的凭据渠道保管。")) return;
        const issued = await api(`${base}/keys`, { method: "POST", body: { name: keyName.value.trim() } });
        if (!valid() || selected !== identity.id) return;
        oneTime.replaceChildren(el("div", { class: "one-time-key" }, el("h3", {}, "一次性 Key · 请安全保管"), el("pre", {}, issued.token), button("清除明文", () => oneTime.replaceChildren(), "small")));
      }, field("Key 名称", keyName), submit("签发一次性 Key")), oneTime));
  }
  try {
    await loadProjects();
    await loadIdentities();
  } catch (error) { if (valid()) report(error); }
}

async function operationsPage() {
  const epoch = state.epoch, selection = state.selection;
  const current = () => isCurrent(epoch) && state.page === "operations" && state.selection === selection;
  const columns = el("div", { class: "columns" });
  const left = el("div", { class: "stack" }), right = el("div", { class: "stack" });
  columns.append(left, right);
  $("#view").append(el("div", { class: "help" }, "V1：配置均显式提交。预算不是供应商硬停额度；回调至少一次。列表可续页，不是快照报表。成功后修改表单才可再提交；结果未知时先查询，再人工解锁。"), el("div", { class: "divider" }), columns);
  const pagePath = (collection, params = {}) => `${projectPath(`/operations/pages/${collection}`)}?${new URLSearchParams({ limit: "50", ...params })}`;

  function operation(parent, title, description, sample, execute, reads = []) {
    const content = editor(sample, true), output = el("div");
    let uncertain = false;
    async function show(action) {
      const data = await action();
      if (current()) output.replaceChildren(el("pre", {}, pretty(data)));
    }
    async function read(path, cursor = null) {
      const url = new URL(path, location.origin);
      if (cursor) url.searchParams.set("cursor", cursor);
      const data = await api(url.pathname + url.search);
      if (!current()) return;
      output.replaceChildren(el("pre", {}, pretty(data)));
      if (data?.next_cursor) output.append(button("下一页 →", () => read(path, data.next_cursor).catch(report), "small"));
    }
    const readButtons = el("div", { class: "actions" }, ...reads.map(([label, path]) => button(label, () => read(typeof path === "function" ? path(json(content)) : path).catch(report), "small")));
    const controls = execute ? form(async () => {
      const spec = json(content);
      if (!confirm(`${title}：确认提交？\n涉及评测时会排队真实任务，可能触发已配置的模型计费；超时先查询，不盲目重复提交。`)) return;
      try {
        await show(() => execute(spec));
        controls.dataset.locked = "true";
        if (current()) output.append(note("操作已提交。请保留返回编号；修改表单内容后才能再次提交。"));
      } catch (error) {
        if (error.uncertain) {
          uncertain = true; controls.dataset.locked = "true";
          if (current()) output.replaceChildren(note("结果未知，已锁定重复提交。先查询；必要时联系管理员并提供请求编号。"));
        }
        throw error;
      }
    }, field("显式配置 JSON", content), submit("提交配置 / 操作")) : null;
    content.addEventListener("input", () => {
      if (controls && !uncertain) { controls.dataset.locked = "false"; controls.querySelector('[type="submit"]').disabled = controls.dataset.pending === "true"; }
    });
    const unlock = execute ? button("查询确认后解锁", () => {
      if (controls.dataset.pending === "true") return;
      if (!confirm("确认已查询操作结果？解锁不代表上次失败；幂等操作仍须保留原 request_key。")) return;
      uncertain = false; controls.dataset.locked = "false"; controls.querySelector('[type="submit"]').disabled = false;
    }, "small") : null;
    parent.append(panel(title, description, readButtons, el("div", { class: "divider" }), controls, unlock, output));
  }
  const post = (tail, body) => api(projectPath(tail), { method: "POST", body });
  if (state.session.is_admin) {
    operation(left, "运行健康与待处理告警", "独立于 API readyz：查看 Worker 心跳、队列等待、失租、取消未确认、未知费用及死信。心跳只是观察，不替代执行租约。",
      {}, null, [["刷新健康状态", projectPath("/operations/health")]]);
    operation(left, "项目配额与活动预算", "null 表示未启用该限制；0 暂停新准入/领取。PUT 整体替换，预算周期须先创建。",
      { max_queued: null, max_running: null, active_budget_key: null }, (spec) => api(projectPath("/operations/limits"), { method: "PUT", body: spec }), [["读取配额 / 当前任务数", projectPath("/operations/limits")]]);
    operation(left, "创建不可变预算周期", "金额单位为指定货币的百万分之一。请填写真实窗口、单价与预留；此处不提供预设价格或默认预算。",
      { key: "review-budget", currency: "", limit_microunits: null, reserve_per_call_microunits: null, starts_at: "", ends_at: "", rates: [{ provider_key: "", model: "", input_microunits_per_million: null, output_microunits_per_million: null }] },
      (spec) => post("/operations/budgets", spec), [["预算与已用/预留", pagePath("budgets")], ["调用费用记录", pagePath("charges")]]);
    operation(left, "未知费用对账", "只允许一次明确核对 unknown 记录；填写外部凭据引用，不上传凭据正文。",
      { call_id: "", amount_microunits: null, reference: "" }, ({ call_id, ...body }) => post(`/operations/charges/${encode(call_id)}/reconcile`, body));
    operation(left, "审核回调端点", "仅发送终态元数据，不发送输入/输出。需要单独运行 callback-worker；凭据填环境引用，接收方按 delivery_id 去重。",
      { key: "review-callback", url: "https://example.com/notify", credential_ref: "callback-signing", max_attempts: 3, retry_delay_seconds: 60, timeout_seconds: 10 },
      (spec) => post("/operations/callbacks", spec), [["端点目录", pagePath("callbacks")], ["交付记录", pagePath("deliveries")]]);
    operation(left, "启停回调端点", "停用后暂停新交付；已经发往远端的请求不能撤回。",
      { key: "review-callback", enabled: false }, ({ key, ...body }) => api(projectPath(`/operations/callbacks/${encode(key)}`), { method: "PATCH", body }));
    operation(right, "不可变合成评测集", "管理员发布的评测集可被项目内获 quality:run 的身份用于测试。不要放生产客户数据；每版最多 100 例。",
      { key: "review-dataset", version: "1", cases: [{ key: "one", input: {}, expected: {}, matcher: "exact" }] },
      (spec) => post("/quality/datasets", spec), [["查看项目评测集", pagePath("datasets")]]);
    operation(right, "安装 OIDC 公钥 / 轮换", "固定 RS256、issuer、audience。仅管理员安装公开 JWKS；不接收私钥，不自动发现或创建用户。",
      { key: "oidc-provider", issuer: "", audience: "", jwks: { keys: [] } },
      (spec) => api("/v2/oidc/providers", { method: "PUT", body: spec }), [["已安装 Provider", "/v2/oidc/providers"]]);
    operation(right, "启停 OIDC Provider", "停用阻止该 Provider 后续 HTTP 身份认证；任务仍归属服务身份，停止执行需停用身份或取消任务。",
      { key: "oidc-provider", enabled: false }, ({ key, ...body }) => api(`/v2/oidc/providers/${encode(key)}`, { method: "PATCH", body }));
    operation(right, "明确绑定身份主体", "一个 issuer/sub 只能映射一个现有服务身份。token 的角色不授予管理员权限。",
      { provider_key: "oidc-provider", subject: "", identity_id: "" }, (spec) => post("/oidc/bindings", spec), [["本项目绑定", pagePath("oidc-bindings")]]);
    operation(right, "撤销 OIDC 主体绑定", "按已安装的 provider_key + subject 撤销，不自动删除服务身份或已受理任务。",
      { provider_key: "oidc-provider", subject: "", identity_id: "" }, (spec) => post("/oidc/unbind", spec));
  }
  operation(right, "受理评测任务", "需要 quality:run 与 task:submit；评分另需 task:read。request_key 保持不变可安全查询同一批，样例不是自动重试。",
    { request_key: crypto.randomUUID(), dataset_key: "review-dataset", dataset_version: "1", capability_key: "", capability_version: "1" },
    (spec) => post("/quality/runs", spec), [["我的评测批次", pagePath("evaluations")]]);
  operation(right, "汇总确定性评分", "exact 为全 JSON 相等；contains 为顶层字段精确包含；schema 为本地 JSON Schema。运行中的任务不算失败。",
    { run_id: "" }, ({ run_id }) => post(`/quality/runs/${encode(run_id)}/score`, undefined));
  operation(left, "受控客户记忆", "仅本人/当前服务身份 + capability 范围。expected_revision 乐观并发；state=deleted、value=null 写删除标记。过期/删除隐藏当前值，历史按显式保留政策处理。",
    { capability_key: "", subject_key: "synthetic-customer", fact_key: "preference", expected_revision: 0, value: {}, state: "active", source_task_id: null, expires_at: null },
    (spec) => post("/memory", spec), [["读取此主体当前事实", (spec) => pagePath("memory", { capability: spec.capability_key, subject: spec.subject_key })]]);
  operation(right, "追加人工质检", "需要该 Task 的 task:read 与 quality:review。评论只追加，不改 Task 的最终结果。",
    { task_id: "", verdict: "needs_review", comment: "" }, ({ task_id, ...body }) => post(`/tasks/${encode(task_id)}/reviews`, body),
    [["查看此任务质检", (spec) => pagePath("reviews", { task_id: spec.task_id })]]);
  operation(left, "安全重提纯规则任务", "仅失败/超时/已取消的纯规则 Workflow。创建带 origin_task_id 的新 Task，从头计算；不复用旧 Attempt，不恢复 Agent 会话，不重放外部调用。",
    { task_id: "", request_key: crypto.randomUUID() }, ({ task_id, ...body }) => post(`/tasks/${encode(task_id)}/recover`, body));
}
