const $ = (id) => document.getElementById(id);
const DEFAULT_PROJECT = {
  url: "https://github.com/octocat/Hello-World.git",
  ref: "master",
  refresh: false,
};

const state = {
  user: null,
  project: { ...DEFAULT_PROJECT },
  conversations: [],
  conversationOffset: 0,
  moreConversations: false,
  activeConversation: null,
  turns: [],
  moreTurns: false,
  beforeSeq: null,
  submitting: false,
  eventSource: null,
  liveTurnId: null,
  selectedTurn: null,
  selectedResult: null,
  navigation: 0,
  toastTimer: null,
  skills: [],
  skillsPage: false,
};

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 3500);
}

function clock(value) {
  if (!value) return "";
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function shortTitle(value) {
  const text = (value || "").replace(/\s+/g, " ").trim();
  return text.length > 24 ? text.slice(0, 24) + "…" : text || "新对话";
}

async function errorText(response) {
  try {
    const data = await response.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map((item) => item.msg).join("；");
  } catch {}
  return "接口返回 HTTP " + response.status;
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) throw new Error(await errorText(response));
  return response.json();
}

async function checkApi() {
  try {
    const status = await api("/api/status");
    $("api-indicator").className = "footer-indicator " + (status.ready ? "online" : "offline");
    $("api-indicator").lastElementChild.textContent = status.ready ? "Runner 已就绪" : "Runner 未就绪";
  } catch {
    $("api-indicator").className = "footer-indicator offline";
    $("api-indicator").lastElementChild.textContent = "API 未连接";
  }
}

function activeProject() {
  return state.activeConversation?.repository || state.project;
}

function setProjectForm() {
  const project = activeProject();
  const path = new URL(project.url).pathname.replace(/\.git$/, "").split("/").filter(Boolean);
  $("project-label").textContent = path.at(-1) || "选择仓库";
  $("repository-url").value = project.url;
  $("repository-ref").value = project.ref;
  $("repository-refresh").checked = !!project.refresh;
  for (const id of ["repository-url", "repository-ref", "repository-refresh", "project-save"]) {
    $(id).disabled = !!state.activeConversation;
  }
  $("project-popover").querySelector(".popover-heading span").textContent = state.activeConversation
    ? "当前对话固定使用这个仓库；新建对话后可重新选择"
    : "新对话会在此仓库的独立副本中执行";
}

function validateProject() {
  const url = $("repository-url").value.trim();
  const ref = $("repository-ref").value.trim();
  let parsed;
  try { parsed = new URL(url); } catch { throw new Error("请输入完整的 Git HTTPS 地址。"); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || !parsed.hostname) {
    throw new Error("仓库地址必须是无需内嵌凭据的 HTTPS URL。");
  }
  if (!/^[A-Za-z0-9_./-]+$/.test(ref) || ref.includes("..") || ref.startsWith("-")) {
    throw new Error("分支或标签名称不符合接口要求。");
  }
  return { url, ref, refresh: $("repository-refresh").checked };
}

function closeProject() {
  $("project-popover").classList.add("hidden");
  $("project-button").setAttribute("aria-expanded", "false");
}

function renderConversations() {
  const list = $("history-list");
  list.replaceChildren();
  $("history-count").textContent = state.conversations.length;
  $("load-conversations").classList.toggle("hidden", !state.moreConversations);
  if (!state.conversations.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = "暂无对话。点击“新对话”开始。";
    list.append(empty);
    return;
  }
  for (const conversation of state.conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item" + (conversation.id === state.activeConversation?.id ? " active" : "");
    button.dataset.conversationId = conversation.id;
    const icon = document.createElement("span");
    icon.className = "history-icon";
    icon.textContent = "◫";
    const body = document.createElement("span");
    body.className = "history-body";
    const title = document.createElement("span");
    title.className = "history-title";
    title.textContent = shortTitle(conversation.title);
    const preview = document.createElement("span");
    preview.className = "history-preview";
    preview.textContent = conversation.last_message || "空白对话";
    body.append(title, preview);
    const dot = document.createElement("span");
    dot.className = "history-status " + (conversation.last_status === "failed" ? "failed" : "done");
    button.append(icon, body, dot);
    list.append(button);
  }
}

async function loadConversations(reset = true) {
  const offset = reset ? 0 : state.conversationOffset;
  const page = await api("/v1/conversations?limit=50&offset=" + offset);
  state.conversations = reset ? page.items : state.conversations.concat(page.items);
  state.conversationOffset = page.next_offset ?? state.conversations.length;
  state.moreConversations = page.has_more;
  renderConversations();
}

function closeStream() {
  state.eventSource?.close();
  state.eventSource = null;
  state.liveTurnId = null;
}

function showChatView() {
  state.skillsPage = false;
  $("skills-page").classList.add("hidden");
  $("chat-layout").classList.remove("hidden");
  $("nav-chat").classList.add("selected");
  $("nav-skills").classList.remove("selected");
  $("top-title").textContent = shortTitle(state.activeConversation?.title);
  $("top-subtitle").textContent = "同一窗口记住最近 5 轮问答";
  $("sidebar").classList.remove("open");
}

function renderSkills() {
  const grid = $("skills-grid");
  grid.replaceChildren();
  const installed = state.skills.filter((skill) => skill.installed).length;
  $("skills-count").textContent = `公共技能 ${state.skills.length} 项 · 已安装 ${installed} 项`;
  if (!state.skills.length) {
    grid.append(node("p", "history-empty", "公共目录暂无可用技能。"));
    return;
  }
  for (const skill of state.skills) {
    const card = node("article", "skill-card");
    const top = node("div", "skill-card-top");
    top.append(node("span", "skill-category", skill.category || "技能"));
    if (skill.installed) top.append(node("span", "skill-installed", "✓ 已安装"));
    card.append(top, node("h2", null, skill.name), node("p", null, skill.description));
    const actions = node("div", "skill-card-actions");
    const primary = node("button", "primary", skill.installed ? "用于提问" : "安装技能");
    primary.type = "button";
    primary.dataset.skillId = skill.id;
    primary.dataset.action = skill.installed ? "use" : "install";
    actions.append(primary);
    if (skill.installed) {
      const remove = node("button", "remove", "卸载");
      remove.type = "button";
      remove.dataset.skillId = skill.id;
      remove.dataset.action = "remove";
      actions.append(remove);
    }
    card.append(actions);
    grid.append(card);
  }
}

async function loadSkills() {
  const result = await api("/v1/skills/catalog");
  state.skills = result.items;
  renderSkills();
}

async function showSkillsView() {
  state.skillsPage = true;
  closeProject();
  $("chat-layout").classList.add("hidden");
  $("skills-page").classList.remove("hidden");
  $("nav-chat").classList.remove("selected");
  $("nav-skills").classList.add("selected");
  $("top-title").textContent = "技能广场";
  $("top-subtitle").textContent = "公共目录 · 用户独立安装";
  $("sidebar").classList.remove("open");
  $("skills-count").textContent = "正在加载技能…";
  try {
    await loadSkills();
  } catch (error) {
    $("skills-count").textContent = "技能加载失败";
    toast("技能加载失败：" + error.message);
  }
}

async function handleSkillAction(button) {
  const skill = state.skills.find((item) => item.id === button.dataset.skillId);
  if (!skill) return;
  if (button.dataset.action === "use") {
    showChatView();
    const input = $("prompt-input");
    input.value = `$${skill.id} ` + input.value;
    updatePromptCount();
    input.focus();
    return;
  }
  button.disabled = true;
  try {
    if (button.dataset.action === "install") {
      await api("/v1/skills/" + skill.id + "/install", { method: "POST" });
      toast("已安装“" + skill.name + "”，新任务可以使用。");
    } else {
      await api("/v1/skills/" + skill.id, { method: "DELETE" });
      toast("已从你的技能目录卸载“" + skill.name + "”。");
    }
    await loadSkills();
  } catch (error) {
    toast("操作失败：" + error.message);
    button.disabled = false;
  }
}

function newChat() {
  if (state.submitting) return toast("正在提交任务，请稍候。");
  showChatView();
  closeStream();
  state.navigation += 1;
  state.activeConversation = null;
  state.turns = [];
  state.moreTurns = false;
  state.beforeSeq = null;
  state.selectedTurn = null;
  state.selectedResult = null;
  location.hash = "";
  $("top-title").textContent = "新对话";
  $("conversation").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("detail-button").disabled = true;
  $("detail-panel").classList.add("hidden");
  $("prompt-input").value = "";
  updatePromptCount();
  setProjectForm();
  renderConversations();
  updateComposer();
  $("sidebar").classList.remove("open");
  $("prompt-input").focus();
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function describeEvent(event) {
  const type = event?.type || "unknown";
  const item = event.item || {};
  if (type === "system") return { title: event.title, desc: event.message || "", kind: "normal" };
  if (type === "thread.started") return { title: "Codex 已启动", desc: "开始处理当前任务", kind: "normal" };
  if (type === "turn.started") return { title: "正在分析问题", desc: "正在读取项目并规划操作", kind: "normal" };
  if (type === "turn.completed") {
    const usage = event.usage || {};
    return {
      title: "本轮执行完毕",
      desc: usage.input_tokens || usage.output_tokens
        ? "输入 " + (usage.input_tokens ?? "—") + " · 输出 " + (usage.output_tokens ?? "—") + " tokens"
        : "正在整理最终结果",
      kind: "message",
    };
  }
  if (type === "error" || type === "turn.failed") {
    return { title: "执行出错", desc: String(event.error?.message || event.message || type), kind: "error" };
  }
  if (type.startsWith("item.") && item.type === "command_execution") {
    return {
      title: type === "item.started" ? "正在执行命令" : "命令执行完毕",
      desc: item.command || "命令",
      output: item.aggregated_output || item.output || "",
      kind: "command",
    };
  }
  if (type.startsWith("item.") && item.type === "agent_message") {
    return { title: "Codex 回复", desc: item.text || "", kind: "message" };
  }
  if (type.startsWith("item.") && item.type === "file_change") {
    return { title: "文件发生变更", desc: (item.changes || []).map((change) => change.path).join("、"), kind: "command" };
  }
  return { title: "事件 · " + type, desc: item.type || event.message || "", kind: "normal" };
}

function renderEvent(item, parent) {
  const row = node("div", "event-item " + item.kind);
  const dot = node("span", "event-dot");
  const body = node("div");
  body.append(node("div", "event-title", item.title), node("div", "event-desc", item.desc));
  if (item.output) body.append(node("pre", "event-output", String(item.output).slice(0, 8000)));
  if (item.raw) {
    const details = node("details", "event-raw");
    details.append(node("summary", null, "查看原始事件"), node("pre", null, item.raw));
    body.append(details);
  }
  row.append(dot, body, node("span", "event-time", clock(item.time)));
  parent.append(row);
  parent.scrollTop = parent.scrollHeight;
}

function addEvent(turn, event, envelope = null) {
  const detail = describeEvent(event);
  const item = {
    ...detail, time: envelope?.timestamp || new Date().toISOString(),
    raw: envelope ? JSON.stringify(envelope, null, 2).slice(0, 12000) : "",
  };
  turn.events ||= [];
  turn.events.push(item);
  turn.events = turn.events.slice(-100);
  const list = document.querySelector('[data-turn-id="' + turn.id + '"] .event-list');
  if (!list && state.turns.includes(turn)) {
    turn.progressOpen = true;
    renderTurns(false);
  } else if (list) {
    renderEvent(item, list);
    const count = document.querySelector('[data-turn-id="' + turn.id + '"] .event-count');
    if (count) count.textContent = turn.events.length + " 条事件";
    const note = document.querySelector('[data-turn-id="' + turn.id + '"] .stream-note');
    if (note) note.classList.add("hidden");
  }
}

function renderProgress(turn) {
  const running = turn.status === "starting" || turn.status === "running";
  const expanded = turn.progressOpen ?? running;
  const card = node("div", "progress-card" + (running ? " is-running" : ""));
  const heading = node("button", "progress-heading");
  heading.type = "button";
  heading.setAttribute("aria-expanded", String(expanded));
  const left = node("span");
  left.append(node("i", "activity-dot"), node("strong", null, running ? "正在处理你的问题" : "执行过程"));
  left.append(node("small", "event-count", (turn.events?.length || 0) + " 条事件"));
  const chevron = node("span", "chevron", expanded ? "⌃" : "⌄");
  heading.append(left, chevron);
  const content = node("div", "progress-content" + (expanded ? "" : " hidden"));
  const eventList = node("div", "event-list");
  eventList.setAttribute("role", "log");
  eventList.setAttribute("aria-live", "polite");
  for (const event of turn.events || []) renderEvent(event, eventList);
  content.append(eventList, node("div", "stream-note" + (turn.events?.length ? " hidden" : ""), "任务事件会实时出现在这里。"));
  heading.addEventListener("click", () => {
    const isOpen = heading.getAttribute("aria-expanded") === "true";
    turn.progressOpen = !isOpen;
    heading.setAttribute("aria-expanded", String(!isOpen));
    content.classList.toggle("hidden", isOpen);
    chevron.textContent = isOpen ? "⌄" : "⌃";
  });
  card.append(heading, content);
  return card;
}

function renderTurn(turn) {
  const article = node("article", "turn");
  article.dataset.turnId = turn.id;
  const user = node("div", "message user-message");
  user.append(node("div", "message-content", turn.user_message));
  user.append(node("div", "message-time", clock(turn.created_at)));
  const assistant = node("div", "assistant-message");
  assistant.append(node("div", "assistant-avatar", "✳"));
  const body = node("div", "assistant-body");
  const name = node("div", "assistant-name", "Codex");
  const status = turn.status === "succeeded" ? "已完成"
    : turn.status === "timed_out" ? "执行超时"
    : turn.status === "failed" ? "执行失败" : "正在工作";
  name.append(node("span", "assistant-state " + (turn.status === "succeeded" ? "success" : ["failed", "timed_out"].includes(turn.status) ? "error" : ""), status));
  body.append(name);
  if (turn.status === "starting" || turn.status === "running") {
    body.append(renderProgress(turn));
  } else {
    if (turn.events?.length) body.append(renderProgress(turn));
    const answer = turn.assistant_message || (turn.status === "succeeded" ? "任务没有返回最终回答。"
      : turn.status === "timed_out" ? "任务执行超时，已自动停止。点击执行详情查看过程。"
      : "任务执行失败，点击执行详情查看结果。");
    body.append(node("div", "answer", answer));
    const actions = node("div", "answer-actions");
    const copy = node("button", null, "⧉ 复制回答");
    copy.type = "button";
    copy.dataset.action = "copy";
    copy.dataset.turnId = turn.id;
    const details = node("button", null, "▣ 查看代码变更");
    details.type = "button";
    details.dataset.action = "details";
    details.dataset.turnId = turn.id;
    actions.append(copy, details);
    if (!turn.events?.length) {
      const events = node("button", null, "◴ 查看执行过程");
      events.type = "button";
      events.dataset.action = "events";
      events.dataset.turnId = turn.id;
      actions.append(events);
    }
    body.append(actions);
  }
  assistant.append(body);
  article.append(user, assistant);
  return article;
}

function renderTurns(scrollBottom = true) {
  const hasTurns = state.turns.length > 0;
  $("welcome").classList.toggle("hidden", hasTurns);
  $("conversation").classList.toggle("hidden", !hasTurns);
  $("load-older").classList.toggle("hidden", !state.moreTurns);
  const list = $("turn-list");
  list.replaceChildren();
  for (const turn of state.turns) list.append(renderTurn(turn));
  if (!state.skillsPage) $("top-title").textContent = shortTitle(state.activeConversation?.title);
  $("detail-button").disabled = !state.selectedTurn;
  updateComposer();
  if (scrollBottom) $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
}

function updateComposer() {
  const active = state.turns.some((turn) => turn.status === "starting" || turn.status === "running");
  $("send-button").disabled = state.submitting || active;
  $("prompt-input").disabled = state.submitting || active;
}

async function openConversation(id) {
  if (state.submitting) return toast("正在提交任务，请稍候。");
  closeStream();
  const navigation = ++state.navigation;
  $("sidebar").classList.remove("open");
  try {
    const [conversation, page] = await Promise.all([
      api("/v1/conversations/" + id),
      api("/v1/conversations/" + id + "/turns?limit=20"),
    ]);
    if (navigation !== state.navigation) return;
    state.activeConversation = conversation;
    showChatView();
    state.turns = page.items;
    state.moreTurns = page.has_more;
    state.beforeSeq = page.next_before_seq;
    state.selectedTurn = null;
    state.selectedResult = null;
    $("detail-panel").classList.add("hidden");
    location.hash = conversation.id;
    setProjectForm();
    renderConversations();
    renderTurns();
    const active = state.turns.find((turn) => turn.task_id === conversation.active_task_id);
    if (active) connectEvents(active);
  } catch (error) {
    if (navigation === state.navigation) toast("打开对话失败：" + error.message);
  }
}

async function loadOlder() {
  if (!state.activeConversation || !state.moreTurns) return;
  const id = state.activeConversation.id;
  const before = state.beforeSeq;
  $("load-older").disabled = true;
  try {
    const page = await api("/v1/conversations/" + id + "/turns?limit=20&before_seq=" + before);
    if (id !== state.activeConversation?.id) return;
    const scroll = $("chat-scroll");
    const height = scroll.scrollHeight;
    state.turns = page.items.concat(state.turns);
    state.moreTurns = page.has_more;
    state.beforeSeq = page.next_before_seq;
    renderTurns(false);
    scroll.scrollTop += scroll.scrollHeight - height;
  } catch (error) {
    toast("读取历史失败：" + error.message);
  } finally {
    $("load-older").disabled = false;
  }
}

async function loadResult(taskId) {
  for (let attempt = 0; attempt < 8; attempt++) {
    const response = await fetch("/v1/tasks/" + taskId + "/result", { cache: "no-store" });
    if (response.status === 202) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      continue;
    }
    if (!response.ok) throw new Error(await errorText(response));
    return response.json();
  }
  throw new Error("任务已结束，但结果尚未就绪。");
}

async function finishLive(turn) {
  if (turn.finishing) return;
  turn.finishing = true;
  closeStream();
  try {
    const result = await loadResult(turn.task_id);
    turn.result = result;
    turn.status = result.status;
    turn.assistant_message = result.final_message || result.error || "";
    turn.progressOpen = true;
    if (state.turns.includes(turn)) renderTurns();
  } catch (error) {
    turn.status = "failed";
    turn.assistant_message = error.message;
    if (state.turns.includes(turn)) renderTurns();
  } finally {
    turn.finishing = false;
  }
  loadConversations().catch(() => {});
}

function connectEvents(turn) {
  closeStream();
  const source = new EventSource("/v1/tasks/" + turn.task_id + "/events");
  state.eventSource = source;
  state.liveTurnId = turn.id;
  const seen = new Set();
  source.addEventListener("codex.event", (message) => {
    try {
      const envelope = JSON.parse(message.data);
      if (seen.has(envelope.sequence)) return;
      seen.add(envelope.sequence);
      addEvent(turn, envelope.data, envelope);
    } catch {
      addEvent(turn, { type: "system", title: "事件解析失败", message: "收到无法解析的事件。" });
    }
  });
  for (const type of ["task.completed", "task.failed"]) {
    source.addEventListener(type, () => {
      if (state.liveTurnId !== turn.id) return;
      addEvent(turn, { type: "system", title: "执行结束", message: "正在读取最终结果。" });
      finishLive(turn);
    });
  }
  source.addEventListener("error", () => {
    if (state.liveTurnId !== turn.id) return;
    api("/v1/tasks/" + turn.task_id).then((task) => {
      if (["succeeded", "failed", "timed_out"].includes(task.status)) finishLive(turn);
    }).catch(() => {});
  });
}

async function sendPrompt() {
  const message = $("prompt-input").value.trim();
  if (!message || state.submitting || $("send-button").disabled) return;
  state.submitting = true;
  updateComposer();
  closeProject();
  try {
    if (!state.activeConversation) {
      const conversation = await api("/v1/conversations", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repository: state.project }),
      });
      state.activeConversation = conversation;
      location.hash = conversation.id;
      setProjectForm();
    }
    const conversationId = state.activeConversation.id;
    const task = await api("/v1/conversations/" + conversationId + "/turns", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, request_id: crypto.randomUUID() }),
    });
    const turn = {
      id: task.turn_id, conversation_id: conversationId,
      sequence: (state.turns.at(-1)?.sequence || 0) + 1,
      user_message: message, assistant_message: null, task_id: task.task_id,
      status: "starting", context_rounds_used: task.context_rounds_used,
      created_at: new Date().toISOString(), events: [],
    };
    state.turns.push(turn);
    $("prompt-input").value = "";
    updatePromptCount();
    renderTurns();
    addEvent(turn, { type: "system", title: "任务已创建", message: "正在启动独立 Runner。" });
    connectEvents(turn);
    loadConversations().catch(() => {});
  } catch (error) {
    toast("提交失败：" + error.message);
  } finally {
    state.submitting = false;
    updateComposer();
  }
}

function formatMs(value) {
  return value == null ? "—" : value < 1000 ? value + " ms" : (value / 1000).toFixed(2) + " s";
}

async function openDetails(turn) {
  try {
    const result = turn.result || await loadResult(turn.task_id);
    turn.result = result;
    state.selectedTurn = turn;
    state.selectedResult = result;
    const facts = [
      ["状态", result.status === "succeeded" ? "已完成" : result.status === "timed_out" ? "执行超时" : result.status || "失败"],
      ["任务 ID", turn.task_id],
      ["仓库", state.activeConversation.repository.url],
      ["缓存", result.repository?.cache_hit ? "已命中" : "首次创建"],
      ["总耗时", formatMs(result.timings_ms?.total)],
      ["Codex 执行", formatMs(result.timings_ms?.codex)],
    ];
    $("task-facts").replaceChildren();
    for (const [label, value] of facts) {
      const row = node("div", "fact");
      row.append(node("dt", null, label), node("dd", null, value));
      $("task-facts").append(row);
    }
    const gitStatus = result.git_status?.trim() || "";
    $("diff-output").textContent = result.diff || (gitStatus
      ? "Git 状态：\n" + gitStatus + "\n\n当前 Runner 的 diff 尚未包含新建的未跟踪文件。"
      : "本轮没有文件变更。");
    $("diff-count").textContent = result.diff ? "已生成 diff" : gitStatus ? "有未跟踪变更" : "无变更";
    const artifact = (result.artifacts || []).find((entry) => entry.name === "codex-events.jsonl");
    $("events-download").classList.toggle("hidden", !artifact);
    if (artifact) $("events-download").href = artifact.download_url;
    $("detail-panel").classList.remove("hidden");
    $("detail-button").disabled = false;
  } catch (error) {
    toast("读取执行详情失败：" + error.message);
  }
}

function loadEvents(turn) {
  if (turn.loadingEvents || turn.events?.length) return;
  turn.loadingEvents = true;
  const source = new EventSource("/v1/tasks/" + turn.task_id + "/events");
  const seen = new Set();
  source.addEventListener("codex.event", (message) => {
    try {
      const envelope = JSON.parse(message.data);
      if (seen.has(envelope.sequence)) return;
      seen.add(envelope.sequence);
      addEvent(turn, envelope.data, envelope);
    } catch { toast("有一条历史事件无法解析。"); }
  });
  const stop = () => {
    source.close();
    turn.loadingEvents = false;
    if (state.turns.includes(turn)) renderTurns(false);
  };
  source.addEventListener("task.completed", stop);
  source.addEventListener("task.failed", stop);
  source.addEventListener("error", stop);
}

function updatePromptCount() {
  $("prompt-count").textContent = $("prompt-input").value.length + " / 20000";
}

async function initialize() {
  checkApi();
  try {
    const identity = await api("/api/me");
    state.user = identity.user_id;
    $("account-button").textContent = identity.user_id;
    $("user-label").textContent = identity.user_id + " · 历史可查，模型只读最近 5 轮";
    $("login-overlay").classList.add("hidden");
    await loadConversations();
    const id = location.hash.slice(1);
    if (/^[0-9a-f]{32}$/.test(id)) await openConversation(id);
  } catch (error) {
    if (error.message.includes("API key") || error.message.includes("HTTP 401")) {
      $("login-overlay").classList.remove("hidden");
    } else {
      toast("加载工作台失败：" + error.message);
    }
  }
}

$("send-button").addEventListener("click", sendPrompt);
$("prompt-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendPrompt();
  }
});
$("prompt-input").addEventListener("input", updatePromptCount);
$("new-chat").addEventListener("click", newChat);
$("nav-chat").addEventListener("click", showChatView);
$("nav-skills").addEventListener("click", showSkillsView);
$("composer-skills").addEventListener("click", showSkillsView);
$("skills-back").addEventListener("click", showChatView);
$("skills-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-skill-id]");
  if (button) handleSkillAction(button);
});
$("history-list").addEventListener("click", (event) => {
  const id = event.target.closest("[data-conversation-id]")?.dataset.conversationId;
  if (id) openConversation(id);
});
$("load-conversations").addEventListener("click", () => loadConversations(false).catch((error) => toast(error.message)));
$("load-older").addEventListener("click", loadOlder);
$("project-button").addEventListener("click", () => {
  const willOpen = $("project-popover").classList.contains("hidden");
  closeProject();
  $("project-popover").classList.toggle("hidden", !willOpen);
  $("project-button").setAttribute("aria-expanded", String(willOpen));
});
$("project-save").addEventListener("click", () => {
  try {
    state.project = validateProject();
    setProjectForm();
    closeProject();
    toast("新对话的仓库设置已保存。");
  } catch (error) { toast(error.message); }
});
$("turn-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const turn = state.turns.find((item) => item.id === button.dataset.turnId);
  if (!turn) return;
  if (button.dataset.action === "details") return openDetails(turn);
  if (button.dataset.action === "events") return loadEvents(turn);
  try {
    await navigator.clipboard.writeText(turn.assistant_message || "");
    toast("回答已复制。");
  } catch { toast("复制失败，请手动选择文字。"); }
});
$("detail-button").addEventListener("click", () => {
  if (state.selectedTurn) openDetails(state.selectedTurn);
});
$("detail-close").addEventListener("click", () => $("detail-panel").classList.add("hidden"));
$("mobile-menu").addEventListener("click", () => $("sidebar").classList.add("open"));
$("sidebar-close").addEventListener("click", () => $("sidebar").classList.remove("open"));
$("account-button").addEventListener("click", async () => {
  if (state.user === "local-dev") return toast("当前是本地单用户实验模式。");
  await api("/api/logout", { method: "POST" });
  location.reload();
});
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  const key = $("login-key").value.trim();
  if (key === "admin" || key === "tester") {
    $("login-error").textContent = "这里需要填写 API Key，而不是用户名。请复制 .local-user-keys.json 中对应的长字符串。";
    return;
  }
  try {
    await api("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    $("login-key").value = "";
    await initialize();
  } catch (error) {
    $("login-error").textContent = error.message === "Invalid API key"
      ? "API Key 不正确。请复制 .local-user-keys.json 中对应的值，不要复制用户名或引号。"
      : error.message;
  }
});
for (const button of document.querySelectorAll(".future-feature")) {
  button.addEventListener("click", () => toast(button.dataset.feature + "入口已预留，当前版本尚未接入。"));
}
for (const suggestion of document.querySelectorAll(".suggestion")) {
  suggestion.addEventListener("click", () => {
    $("prompt-input").value = suggestion.dataset.prompt;
    updatePromptCount();
    $("prompt-input").focus();
  });
}
setProjectForm();
initialize();
