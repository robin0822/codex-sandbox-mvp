const $ = (id) => document.getElementById(id);

const state = {
  user: null,
  workspacePath: "",
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
  installedSkills: [],
  selectedSkillIds: new Set(),
  skillsPage: false,
  mcps: [],
  installedMcps: [],
  selectedMcpIds: new Set(),
  capabilityTab: "skill",
  mcpPage: false,
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

function setProjectForm() {
  const workspace = state.activeConversation?.workspace;
  $("project-label").textContent = workspace?.type === "repository_snapshot" ? "旧仓库会话" : "空白工作区";
  $("project-save").disabled = !state.activeConversation || workspace?.type !== "conversation_workspace";
  $("workspace-download").classList.toggle("hidden", !state.activeConversation || workspace?.type !== "conversation_workspace");
  if (state.activeConversation && workspace?.type === "conversation_workspace") {
    $("workspace-download").href = `/v1/conversations/${state.activeConversation.id}/workspace`;
  }
  if (!state.activeConversation) {
    $("workspace-files").innerHTML = "<p>发送第一条消息后创建独立空白工作区。</p>";
  }
}

function formatBytes(value) {
  if (value == null) return "";
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  return (value / 1024 / 1024).toFixed(1) + " MB";
}

async function loadWorkspaceFiles(path = "") {
  if (!state.activeConversation || state.activeConversation.workspace?.type !== "conversation_workspace") return;
  const result = await api(`/v1/conversations/${state.activeConversation.id}/files?path=${encodeURIComponent(path)}`);
  state.workspacePath = path;
  const list = $("workspace-files");
  list.replaceChildren();
  if (path) {
    const up = node("button", "workspace-file", "↩ 返回上级");
    up.type = "button";
    up.dataset.workspacePath = path.split("/").slice(0, -1).join("/");
    list.append(up);
  }
  if (!result.items.length) list.append(node("p", null, path ? "这个目录是空的。" : "工作区目前是空的。"));
  for (const item of result.items) {
    if (item.type === "directory") {
      const entry = node("button", "workspace-file", `▸ ${item.name}`);
      entry.type = "button";
      entry.dataset.workspacePath = item.path;
      list.append(entry);
    } else {
      const entry = node("a", "workspace-file", `▤ ${item.name}`);
      entry.href = `/v1/conversations/${state.activeConversation.id}/files/content?path=${encodeURIComponent(item.path)}`;
      entry.download = item.name;
      if (item.size_bytes != null) entry.append(node("small", null, formatBytes(item.size_bytes)));
      list.append(entry);
    }
  }
}

function closeProject() {
  $("project-popover").classList.add("hidden");
  $("project-button").setAttribute("aria-expanded", "false");
}

function closeCapabilityPicker() {
  $("capability-picker").classList.add("hidden");
}

async function loadInstalledCapabilities() {
  const [skills, mcps] = await Promise.all([api("/v1/skills"), api("/v1/mcp")]);
  state.installedSkills = skills.items;
  state.installedMcps = mcps.items;
  const skillIds = new Set(state.installedSkills.map((item) => item.id));
  const mcpIds = new Set(state.installedMcps.map((item) => item.id));
  state.selectedSkillIds = new Set([...state.selectedSkillIds].filter((id) => skillIds.has(id)));
  state.selectedMcpIds = new Set([...state.selectedMcpIds].filter((id) => mcpIds.has(id)));
}

function capabilityItems() {
  return state.capabilityTab === "skill" ? state.installedSkills : state.installedMcps;
}

function selectedCapabilityIds() {
  return state.capabilityTab === "skill" ? state.selectedSkillIds : state.selectedMcpIds;
}

function renderCapabilitySelection() {
  const skillCount = state.selectedSkillIds.size;
  const mcpCount = state.selectedMcpIds.size;
  $("selected-skill-count").textContent = skillCount;
  $("selected-mcp-count").textContent = mcpCount;
  $("composer-skill-count").textContent = skillCount;
  $("composer-mcp-count").textContent = mcpCount;
  $("composer-skill-count").classList.toggle("hidden", !skillCount);
  $("composer-mcp-count").classList.toggle("hidden", !mcpCount);

  const chips = $("capability-chips");
  chips.replaceChildren();
  const entries = [
    ...state.installedSkills.filter((item) => state.selectedSkillIds.has(item.id)).map((item) => ["skill", item]),
    ...state.installedMcps.filter((item) => state.selectedMcpIds.has(item.id)).map((item) => ["mcp", item]),
  ];
  for (const [kind, item] of entries) {
    const chip = node("span", "capability-chip " + (kind === "mcp" ? "mcp" : ""));
    chip.append(node("span", null, `${kind === "mcp" ? "⌘" : "✦"} ${item.name}`));
    const remove = node("button", null, "×");
    remove.type = "button";
    remove.dataset.capabilityKind = kind;
    remove.dataset.capabilityId = item.id;
    remove.setAttribute("aria-label", `移除 ${item.name}`);
    chip.append(remove);
    chips.append(chip);
  }
  chips.classList.toggle("hidden", !entries.length);
}

function renderCapabilityOptions() {
  const options = $("capability-options");
  options.replaceChildren();
  const query = $("capability-search").value.trim().toLowerCase();
  const items = capabilityItems().filter((item) =>
    `${item.name} ${item.description} ${item.id}`.toLowerCase().includes(query));
  if (!items.length) {
    options.append(node("p", "capability-empty", capabilityItems().length
      ? "没有匹配的项目。" : `尚未安装${state.capabilityTab === "skill" ? "技能" : " MCP"}，请先前往广场安装。`));
    return;
  }
  const selected = selectedCapabilityIds();
  for (const item of items) {
    const button = node("button", `capability-option ${state.capabilityTab === "mcp" ? "mcp " : ""}${selected.has(item.id) ? "selected" : ""}`);
    button.type = "button";
    button.dataset.capabilityId = item.id;
    const icon = node("span", "capability-option-icon", state.capabilityTab === "mcp" ? "⌘" : "✦");
    const copy = node("span", "capability-option-copy");
    copy.append(node("strong", null, item.name), node("span", null, item.description));
    button.append(icon, copy, node("span", "capability-check", "✓"));
    options.append(button);
  }
}

function setCapabilityTab(tab) {
  state.capabilityTab = tab;
  $("capability-skill-tab").classList.toggle("active", tab === "skill");
  $("capability-mcp-tab").classList.toggle("active", tab === "mcp");
  $("capability-search").value = "";
  $("capability-search").placeholder = `搜索已安装的${tab === "skill" ? "技能" : " MCP"}…`;
  $("capability-market").textContent = tab === "skill" ? "前往技能广场管理 ↗" : "前往 MCP 广场管理 ↗";
  renderCapabilitySelection();
  renderCapabilityOptions();
}

async function openCapabilityPicker(tab) {
  closeProject();
  try {
    await loadInstalledCapabilities();
    setCapabilityTab(tab);
    $("capability-picker").classList.remove("hidden");
    $("capability-search").focus();
  } catch (error) {
    toast("能力列表加载失败：" + error.message);
  }
}

function clearCapabilitySelection() {
  state.selectedSkillIds.clear();
  state.selectedMcpIds.clear();
  renderCapabilitySelection();
  closeCapabilityPicker();
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
  state.mcpPage = false;
  $("skills-page").classList.add("hidden");
  $("mcp-page").classList.add("hidden");
  $("chat-layout").classList.remove("hidden");
  $("nav-chat").classList.add("selected");
  $("nav-skills").classList.remove("selected");
  $("nav-mcp").classList.remove("selected");
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
  state.mcpPage = false;
  closeProject();
  closeCapabilityPicker();
  $("chat-layout").classList.add("hidden");
  $("mcp-page").classList.add("hidden");
  $("skills-page").classList.remove("hidden");
  $("nav-chat").classList.remove("selected");
  $("nav-skills").classList.add("selected");
  $("nav-mcp").classList.remove("selected");
  $("top-title").textContent = "技能广场";
  $("top-subtitle").textContent = "公共目录 · 用户独立安装";
  $("sidebar").classList.remove("open");
  $("skills-count").textContent = "正在加载技能…";
  try {
    await loadSkills();
    await loadInstalledCapabilities();
    renderCapabilitySelection();
  } catch (error) {
    $("skills-count").textContent = "技能加载失败";
    toast("技能加载失败：" + error.message);
  }
}

function renderMcps() {
  const grid = $("mcp-grid");
  grid.replaceChildren();
  const installed = state.mcps.filter((mcp) => mcp.installed).length;
  $("mcp-count").textContent = `公共 MCP ${state.mcps.length} 项 · 已安装 ${installed} 项`;
  if (!state.mcps.length) {
    grid.append(node("p", "history-empty", "当前没有已审核的 MCP。"));
    return;
  }
  for (const mcp of state.mcps) {
    const card = node("article", "skill-card mcp-card");
    const top = node("div", "skill-card-top");
    top.append(node("span", "skill-category", mcp.category || "MCP"));
    if (mcp.installed) top.append(node("span", "skill-installed", "✓ 已安装"));
    const toolText = `${mcp.tools.length} 个工具 · ${mcp.transport}`;
    card.append(top, node("h2", null, mcp.name), node("p", null, mcp.description),
      node("div", "mcp-meta", toolText));
    const actions = node("div", "skill-card-actions");
    const action = node("button", mcp.installed ? "remove" : "primary", mcp.installed ? "卸载" : "安装 MCP");
    action.type = "button";
    action.dataset.mcpId = mcp.id;
    action.dataset.action = mcp.installed ? "remove" : "install";
    actions.append(action);
    if (mcp.repository_url) {
      const source = node("a", "mcp-source", "查看源码 ↗");
      source.href = mcp.repository_url;
      source.target = "_blank";
      source.rel = "noreferrer";
      actions.append(source);
    }
    card.append(actions);
    grid.append(card);
  }
}

async function loadMcps() {
  const result = await api("/v1/mcp/catalog");
  state.mcps = result.items;
  renderMcps();
}

async function showMcpView() {
  state.skillsPage = false;
  state.mcpPage = true;
  closeProject();
  closeCapabilityPicker();
  $("chat-layout").classList.add("hidden");
  $("skills-page").classList.add("hidden");
  $("mcp-page").classList.remove("hidden");
  $("nav-chat").classList.remove("selected");
  $("nav-skills").classList.remove("selected");
  $("nav-mcp").classList.add("selected");
  $("top-title").textContent = "MCP 广场";
  $("top-subtitle").textContent = "Codex 原生协议 · 用户独立安装";
  $("sidebar").classList.remove("open");
  $("mcp-count").textContent = "正在加载 MCP…";
  try {
    await loadMcps();
    await loadInstalledCapabilities();
    renderCapabilitySelection();
  } catch (error) {
    $("mcp-count").textContent = "MCP 加载失败";
    toast("MCP 加载失败：" + error.message);
  }
}

async function handleMcpAction(button) {
  const mcp = state.mcps.find((item) => item.id === button.dataset.mcpId);
  if (!mcp) return;
  button.disabled = true;
  try {
    if (button.dataset.action === "install") {
      await api("/v1/mcp/" + mcp.id + "/install", { method: "POST" });
      toast(`已安装“${mcp.name}”，新任务将由 Codex 原生加载。`);
    } else {
      await api("/v1/mcp/" + mcp.id, { method: "DELETE" });
      toast(`已卸载“${mcp.name}”。`);
    }
    await loadMcps();
    await loadInstalledCapabilities();
    renderCapabilitySelection();
  } catch (error) {
    toast("操作失败：" + error.message);
    button.disabled = false;
  }
}

async function handleSkillAction(button) {
  const skill = state.skills.find((item) => item.id === button.dataset.skillId);
  if (!skill) return;
  if (button.dataset.action === "use") {
    showChatView();
    state.selectedSkillIds.add(skill.id);
    await loadInstalledCapabilities();
    renderCapabilitySelection();
    $("prompt-input").focus();
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
    await loadInstalledCapabilities();
    renderCapabilitySelection();
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
  state.workspacePath = "";
  location.hash = "";
  $("top-title").textContent = "新对话";
  $("conversation").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("detail-button").disabled = true;
  $("detail-panel").classList.add("hidden");
  $("prompt-input").value = "";
  clearCapabilitySelection();
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

const markdown = typeof window.markdownit === "function"
  ? window.markdownit({ html: false, linkify: true, breaks: true }) : null;
let mermaidSequence = 0;
let mermaidLoader = null;

function loadMermaid() {
  if (window.mermaid) return Promise.resolve(window.mermaid);
  if (mermaidLoader) return mermaidLoader;
  mermaidLoader = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/vendor/mermaid/mermaid.min.js";
    script.onload = () => {
      window.mermaid.initialize({
        startOnLoad: false, securityLevel: "strict", theme: "neutral",
        flowchart: { htmlLabels: false, curve: "basis" },
      });
      resolve(window.mermaid);
    };
    script.onerror = () => reject(new Error("Mermaid 组件加载失败"));
    document.head.append(script);
  });
  return mermaidLoader;
}

async function renderMermaidBlocks(element) {
  const blocks = [...element.querySelectorAll("pre > code.language-mermaid")];
  if (!blocks.length) return;
  let mermaid;
  try {
    mermaid = await loadMermaid();
  } catch (error) {
    for (const code of blocks) {
      const fallback = node("pre", "mermaid-artifact is-error", code.textContent);
      code.parentElement.replaceWith(fallback);
    }
    return;
  }
  for (const code of blocks) {
    const shell = node("div", "mermaid-artifact is-loading");
    shell.append(node("div", "artifact-skeleton"), node("p", null, "正在渲染架构图…"));
    code.parentElement.replaceWith(shell);
    try {
      const result = await mermaid.render(`mermaid-artifact-${++mermaidSequence}`, code.textContent);
      shell.classList.remove("is-loading");
      // Mermaid runs in strict mode, which encodes diagram HTML and disables
      // interactive links before producing this SVG.
      shell.innerHTML = result.svg;
    } catch (error) {
      shell.classList.remove("is-loading");
      shell.classList.add("is-error");
      shell.textContent = "架构图渲染失败：" + error.message;
    }
  }
}

function markdownNode(text, className) {
  const element = node("div", className + " markdown-body");
  if (!markdown || !window.DOMPurify) {
    element.textContent = text;
    return element;
  }
  element.innerHTML = window.DOMPurify.sanitize(markdown.render(text), { USE_PROFILES: { html: true } });
  for (const link of element.querySelectorAll("a[href]")) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  requestAnimationFrame(() => renderMermaidBlocks(element));
  return element;
}

function hiddenRuntimeNotice(event) {
  const message = event?.item?.message || event?.message || "";
  return message.includes("Model metadata for `xopglm53` not found");
}

function hasChinese(text) {
  return /[\u3400-\u9fff]/.test(text || "");
}

function commandActivity(item, running) {
  const command = item.command || "";
  const failed = item.status === "failed" || Number(item.exit_code) > 0;
  if (/SKILL\.md|\.agents\/skills|\.codex\/skills/.test(command)) {
    return { title: running ? "正在加载任务技能" : failed ? "技能读取失败" : "已读取技能说明",
      desc: running ? "正在准备完成任务需要的专业能力" : failed ? "技能文件未能成功读取" : "任务所需技能已经就绪",
      kind: failed ? "error" : "skill", stageKey: "skill-read" };
  }
  if (/\bcurl\b|\bwget\b|https?:\/\//i.test(command)) {
    return { title: running ? "正在获取外部资料" : failed ? "外部资料获取失败" : "已获取外部资料",
      desc: failed ? "当前来源未能成功访问，正在调整处理方式"
        : running ? "正在读取与问题相关的公开信息" : "相关公开信息已经读取完成",
      kind: failed ? "error" : "search", stageKey: "external-fetch" };
  }
  if (/\b(pwd|ls|find|rg|git status|git diff)\b/.test(command)) {
    return { title: running ? "正在检查工作区" : failed ? "工作区检查失败" : "已检查当前工作区",
      desc: running ? "正在确认当前窗口中的文件和项目状态" : "当前窗口的文件和项目状态已经确认",
      kind: failed ? "error" : "workspace", stageKey: "workspace-check" };
  }
  if (/apply_patch|cat\s+>|tee\s+|sed\s+-i|mkdir|cp\s+|mv\s+/.test(command)) {
    return { title: running ? "正在处理工作区文件" : failed ? "文件处理失败" : "已完成文件处理",
      desc: running ? "正在按照要求写入或更新文件" : "要求的文件操作已经完成",
      kind: failed ? "error" : "file", stageKey: "file-work" };
  }
  return { title: running ? "正在执行任务步骤" : failed ? "任务步骤执行失败" : "已完成任务步骤",
    desc: running ? "Codex 正在调用本地工具继续处理" : "本地工具步骤已经结束",
    kind: failed ? "error" : "normal", stageKey: `command-${item.id || "general"}` };
}

function describeEvent(event) {
  const type = event?.type || "unknown";
  const item = event.item || {};
  if (type === "system") return { title: event.title, desc: event.message || "", kind: "normal", stageKey: event.title };
  if (type === "thread.started") return { title: "已启动任务环境", desc: "Codex 已开始处理当前问题", kind: "normal", stageKey: "startup" };
  if (type === "turn.started") return { title: "正在理解你的问题", desc: "正在结合对话上下文确定处理步骤", kind: "active", stageKey: "understand" };
  if (type === "turn.completed") {
    const usage = event.usage || {};
    return {
      title: "已完成内容整理",
      desc: usage.input_tokens || usage.output_tokens
        ? "输入 " + (usage.input_tokens ?? "—") + " · 输出 " + (usage.output_tokens ?? "—")
          + " · 推理 " + (usage.reasoning_output_tokens ?? "—") + " tokens"
        : "最终结果已经生成",
      kind: "done", stageKey: "complete",
    };
  }
  if (type === "error" || type === "turn.failed") {
    return { title: "执行遇到问题", desc: String(event.error?.message || event.message || type), kind: "error", stageKey: "task-error" };
  }
  if (type === "skill.loaded") {
    return { title: "已加载专业技能", desc: `$${event.skill_id} 已加入本轮任务`,
      kind: "skill", skillId: event.skill_id, stageKey: `skill-${event.skill_id}` };
  }
  if (type === "skill.read") {
    return { title: "已读取技能说明", desc: `$${event.skill_id} 已准备完成任务所需的处理规则`,
      kind: "skill", skillId: event.skill_id, stageKey: `skill-${event.skill_id}` };
  }
  if (type.startsWith("item.") && item.type === "reasoning") return null;
  if (type.startsWith("item.") && item.type === "command_execution") {
    return commandActivity(item, type === "item.started" || item.status === "in_progress");
  }
  if (type.startsWith("item.") && item.type === "agent_message") {
    const text = (item.text || "").trim();
    const conciseChinese = hasChinese(text) && text.length <= 220;
    return { title: /架构|流程|可视化|示意图/.test(text) ? "正在生成可视化结果" : "正在整理输出内容",
      desc: conciseChinese ? text : "正在根据已获得的信息组织最终回答",
      kind: "active", stageKey: /架构|流程|可视化|示意图/.test(text) ? "visualize" : "compose",
      visualizing: /架构|流程|可视化|示意图/.test(text) };
  }
  if (type.startsWith("item.") && item.type === "file_change") {
    return { title: "已更新工作区文件", desc: (item.changes || []).map((change) => change.path).join("、"),
      kind: "file", stageKey: "file-change" };
  }
  if (type.startsWith("item.") && item.type === "error") {
    const message = item.message || "";
    if (hiddenRuntimeNotice(event)) return null;
    return { title: "运行提示", desc: message, kind: "error", stageKey: `error-${item.id || message.slice(0, 40)}` };
  }
  if (type.startsWith("item.") && item.type === "mcp_tool_call") {
    const running = type === "item.started" || item.status === "in_progress";
    const failed = item.status === "failed" || item.error;
    const tool = item.tool || "";
    const searching = /search/i.test(tool);
    const fetching = /fetch|read|get_article|get_summary/i.test(tool);
    return {
      title: failed ? "联网工具调用失败" : running
        ? searching ? "正在并行检索补充信息" : fetching ? "正在阅读相关资料" : "正在调用专业工具"
        : searching ? "已完成联网检索" : fetching ? "已读取相关资料" : "专业工具调用完成",
      desc: failed ? (item.error?.message || String(item.error))
        : running ? `${item.server || "MCP"} 正在提供本轮任务需要的信息`
          : `${item.server || "MCP"} 已提供本轮任务需要的信息`,
      kind: failed ? "error" : searching || fetching ? "search" : "mcp",
      stageKey: searching ? "mcp-search" : fetching ? "mcp-fetch" : `mcp-${item.server || "tool"}-${tool}`,
    };
  }
  if (type.startsWith("item.") && item.type === "web_search") {
    return { title: "正在检索公开资料", desc: "正在搜索与问题相关的最新信息", kind: "search", stageKey: "web-search" };
  }
  if (type.startsWith("item.") && item.type === "todo_list") {
    return { title: "已明确处理步骤", desc: (item.items || []).map((step) => `${step.completed ? "✓" : "○"} ${step.text}`).join("\n"),
      kind: "normal", stageKey: "plan" };
  }
  return null;
}

function renderEvent(item, parent) {
  const row = node("div", "event-item " + item.kind);
  const dot = node("span", "event-dot");
  const body = node("div");
  body.append(node("div", "event-title", item.title), item.kind === "reasoning"
    ? markdownNode(item.desc, "event-desc") : node("div", "event-desc", item.desc));
  if (item.output) body.append(node("pre", "event-output", String(item.output).slice(0, 8000)));
  row.append(dot, body, node("span", "event-time", clock(item.time)));
  parent.append(row);
  parent.scrollTop = parent.scrollHeight;
}

function followLiveOutput(turn) {
  const running = ["starting", "running", "cancelling"].includes(turn.status);
  if (!running || state.liveTurnId !== turn.id) return;
  requestAnimationFrame(() => {
    const article = document.querySelector('[data-turn-id="' + turn.id + '"]');
    const eventList = article?.querySelector(".event-list");
    if (eventList) eventList.scrollTop = eventList.scrollHeight;
    const scroll = $("chat-scroll");
    scroll.scrollTop = scroll.scrollHeight;
  });
}

function addEvent(turn, event, envelope = null) {
  if (hiddenRuntimeNotice(event)) return;
  turn.rawEvents ||= [];
  if (envelope) {
    turn.rawEvents.push(JSON.stringify(envelope, null, 2).slice(0, 6000));
    turn.rawEvents = turn.rawEvents.slice(-30);
  }
  const detail = describeEvent(event);
  if (!detail) return;
  const item = {
    ...detail, time: envelope?.timestamp || new Date().toISOString(),
    itemId: event.item?.id || null,
  };
  turn.events ||= [];
  const existing = item.stageKey
    ? turn.events.findIndex((entry) => entry.stageKey === item.stageKey)
    : item.itemId ? turn.events.findIndex((entry) => entry.itemId === item.itemId) : -1;
  if (existing >= 0) turn.events[existing] = item;
  else turn.events.push(item);
  turn.events = turn.events.slice(-100);
  const article = document.querySelector('[data-turn-id="' + turn.id + '"]');
  const card = article?.querySelector(".progress-card");
  if (!card && state.turns.includes(turn)) {
    turn.progressOpen = true;
    renderTurns(false);
  } else if (card) card.replaceWith(renderProgress(turn));
  followLiveOutput(turn);
}

function elapsedText(turn) {
  const start = new Date(turn.created_at).getTime();
  const end = turn.completed_at ? new Date(turn.completed_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  return seconds < 60 ? `已处理 ${seconds} 秒` : `已处理 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function renderProgress(turn) {
  const running = ["starting", "running", "cancelling"].includes(turn.status);
  const expanded = turn.progressOpen ?? running;
  const card = node("div", "progress-card" + (running ? " is-running" : ""));
  const heading = node("button", "progress-heading");
  heading.type = "button";
  heading.setAttribute("aria-expanded", String(expanded));
  const left = node("span");
  left.append(node("i", "activity-dot"), node("strong", null, running ? "正在处理你的问题" : "处理过程"));
  const elapsed = node("small", "elapsed-time", elapsedText(turn));
  elapsed.dataset.startedAt = turn.created_at;
  if (turn.completed_at) elapsed.dataset.completedAt = turn.completed_at;
  left.append(elapsed);
  const chevron = node("span", "chevron", expanded ? "⌃" : "⌄");
  heading.append(left, chevron);
  const content = node("div", "progress-content" + (expanded ? "" : " hidden"));
  content.append(node("div", "progress-section-label", "执行进度"));
  const eventList = node("div", "event-list");
  eventList.setAttribute("role", "log");
  eventList.setAttribute("aria-live", "polite");
  for (const event of turn.events || []) renderEvent(event, eventList);
  content.append(eventList, node("div", "stream-note" + (turn.events?.length ? " hidden" : ""), "任务事件会实时出现在这里。"));
  if (running && (turn.events || []).some((event) => event.visualizing)) {
    const visual = node("div", "artifact-preview is-loading");
    visual.append(node("div", "artifact-skeleton"), node("p", null, "正在生成可视化结果…"));
    content.append(visual);
  }
  if (turn.rawEvents?.length) {
    const technical = node("details", "technical-events");
    technical.append(node("summary", null, "查看技术详情"),
      node("pre", null, turn.rawEvents.join("\n\n")));
    content.append(technical);
  }
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

function workspaceChanges(turn) {
  return turn.result?.workspace_changes || turn.workspace_changes || { created: [], modified: [], deleted: [] };
}

function fileIcon(type) {
  return type === "image" ? "▧" : type === "pdf" ? "PDF" : type === "json" ? "{}" : type === "markdown" ? "M↓" : "</>";
}

function renderFileCards(turn) {
  const changes = workspaceChanges(turn);
  const visible = [
    ...(changes.created || []).map((item) => ({ ...item, changeLabel: "新建" })),
    ...(changes.modified || []).map((item) => ({ ...item, changeLabel: "已修改" })),
  ];
  if (!visible.length && !(changes.deleted || []).length) return null;
  const section = node("section", "turn-files");
  const heading = node("div", "turn-files-heading");
  const count = visible.length + (changes.deleted || []).length;
  heading.append(node("strong", null, `本轮文件 ${count}`));
  if (changes.download_all_url) {
    const downloadAll = node("a", null, "下载本轮文件");
    downloadAll.href = changes.download_all_url;
    downloadAll.download = `turn-${turn.task_id}-files.tar.gz`;
    heading.append(downloadAll);
  }
  section.append(heading);
  const grid = node("div", "turn-file-grid");
  for (const item of visible) {
    const card = node("article", "turn-file-card");
    const icon = node("span", "turn-file-icon " + (item.preview_type || "file"), fileIcon(item.preview_type));
    const copy = node("div", "turn-file-copy");
    copy.append(node("strong", null, item.name), node("span", null, `${item.changeLabel} · ${formatBytes(item.size_bytes)}`));
    const actions = node("div", "turn-file-actions");
    if (item.preview_url) {
      const preview = node("button", null, "预览");
      preview.type = "button";
      preview.dataset.action = "file-preview";
      preview.dataset.turnId = turn.id;
      preview.dataset.name = item.name;
      preview.dataset.previewType = item.preview_type;
      preview.dataset.size = item.size_bytes;
      preview.dataset.previewUrl = item.preview_url;
      preview.dataset.downloadUrl = item.download_url;
      actions.append(preview);
    }
    if (item.download_url) {
      const download = node("a", null, "下载");
      download.href = item.download_url;
      download.download = item.name;
      actions.append(download);
    } else {
      actions.append(node("span", "turn-file-unavailable", "历史版本未保存"));
    }
    card.append(icon, copy, actions);
    grid.append(card);
  }
  section.append(grid);
  if ((changes.deleted || []).length) {
    section.append(node("p", "turn-files-deleted", "已删除：" + changes.deleted.map((item) => item.path).join("、")));
  }
  return section;
}

function closeFilePreview() {
  $("file-preview-modal").classList.add("hidden");
  $("file-preview-body").replaceChildren();
}

async function openFilePreview(button) {
  const { name, previewType, previewUrl, downloadUrl } = button.dataset;
  $("file-preview-title").textContent = name;
  $("file-preview-meta").textContent = `${previewType.toUpperCase()} · ${formatBytes(Number(button.dataset.size))}`;
  $("file-preview-download").href = downloadUrl;
  $("file-preview-download").download = name;
  const body = $("file-preview-body");
  body.replaceChildren(node("div", "file-preview-loading", "正在加载预览…"));
  $("file-preview-modal").classList.remove("hidden");
  try {
    if (previewType === "image") {
      const image = node("img", "file-preview-image");
      image.src = previewUrl;
      image.alt = name;
      body.replaceChildren(image);
      return;
    }
    if (previewType === "pdf") {
      const frame = node("iframe", "file-preview-pdf");
      frame.src = previewUrl;
      frame.title = name;
      body.replaceChildren(frame);
      return;
    }
    const response = await fetch(previewUrl, { cache: "no-store" });
    if (!response.ok) throw new Error(await errorText(response));
    let text = await response.text();
    if (previewType === "markdown") body.replaceChildren(markdownNode(text, "file-preview-markdown"));
    else {
      if (previewType === "json") {
        try { text = JSON.stringify(JSON.parse(text), null, 2); } catch {}
      }
      body.replaceChildren(node("pre", "file-preview-code", text));
    }
  } catch (error) {
    body.replaceChildren(node("div", "file-preview-error", "预览失败：" + error.message));
  }
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
    : turn.status === "cancelled" ? "已停止"
    : turn.status === "cancelling" ? "正在停止"
    : turn.status === "failed" ? "执行失败" : "正在工作";
  name.append(node("span", "assistant-state " + (turn.status === "succeeded" ? "success" : ["failed", "timed_out", "cancelled"].includes(turn.status) ? "error" : ""), status));
  body.append(name);
  const loaded = turn.result?.loaded_skills?.length ? turn.result.loaded_skills
    : (turn.events || []).filter((event) => event.kind === "skill").map((event) => event.skillId);
  if (loaded.length) body.append(node("div", "skill-usage", "已验证加载 " + loaded.map((id) => "$" + id).join("、")));
  if (["starting", "running", "cancelling"].includes(turn.status)) {
    body.append(renderProgress(turn));
  } else {
    if (turn.events?.length) body.append(renderProgress(turn));
    const answer = turn.assistant_message || (turn.status === "succeeded" ? "任务没有返回最终回答。"
      : turn.status === "timed_out" ? "任务执行超时，已自动停止。点击执行详情查看过程。"
      : turn.status === "cancelled" ? "任务已由用户停止。点击执行详情查看已产生的过程。"
      : "任务执行失败，点击执行详情查看结果。");
    body.append(markdownNode(answer, "answer"));
    const files = renderFileCards(turn);
    if (files) body.append(files);
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
  if (!state.skillsPage && !state.mcpPage) $("top-title").textContent = shortTitle(state.activeConversation?.title);
  $("detail-button").disabled = !state.selectedTurn;
  updateComposer();
  if (scrollBottom) $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
}

function updateComposer() {
  const active = state.turns.find((turn) => ["starting", "running", "cancelling"].includes(turn.status));
  const button = $("send-button");
  button.disabled = state.submitting || active?.status === "cancelling";
  button.classList.toggle("is-stop", Boolean(active));
  button.textContent = active ? "■" : "➤";
  button.title = active ? "停止执行" : "发送（Enter）";
  button.setAttribute("aria-label", active ? "停止执行" : "发送问题");
  $("prompt-input").disabled = state.submitting || active;
}

async function cancelActiveTask() {
  const turn = state.turns.find((item) => ["starting", "running"].includes(item.status));
  if (!turn || state.submitting) return;
  turn.status = "cancelling";
  addEvent(turn, { type: "system", title: "正在停止任务", message: "正在请求 Runner 停止当前 Codex 轮次。" });
  renderTurns(false);
  try {
    await api("/v1/tasks/" + turn.task_id + "/cancel", { method: "POST" });
  } catch (error) {
    turn.status = "running";
    toast("停止失败：" + error.message);
    renderTurns(false);
  }
}

async function openConversation(id) {
  if (state.submitting) return toast("正在提交任务，请稍候。");
  closeStream();
  clearCapabilitySelection();
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
    state.workspacePath = "";
    $("detail-panel").classList.add("hidden");
    location.hash = conversation.id;
    setProjectForm();
    renderConversations();
    renderTurns();
    const active = state.turns.find((turn) => turn.task_id === conversation.active_task_id);
    if (active) connectEvents(active);
    else if (state.turns.at(-1)?.task_id) loadEvents(state.turns.at(-1));
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
    turn.completed_at = result.finished_at || new Date().toISOString();
    turn.progressOpen = true;
    if (state.turns.includes(turn)) renderTurns();
  } catch (error) {
    turn.status = "failed";
    turn.assistant_message = error.message;
    if (state.turns.includes(turn)) renderTurns();
  } finally {
    turn.finishing = false;
  }
  if (!$("project-popover").classList.contains("hidden")) {
    loadWorkspaceFiles(state.workspacePath).catch(() => {});
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
  for (const type of ["task.completed", "task.failed", "task.cancelled"]) {
    source.addEventListener(type, () => {
      if (state.liveTurnId !== turn.id) return;
      addEvent(turn, { type: "system", title: "执行结束", message: "正在读取最终结果。" });
      finishLive(turn);
    });
  }
  source.addEventListener("error", () => {
    if (state.liveTurnId !== turn.id) return;
    api("/v1/tasks/" + turn.task_id).then((task) => {
      if (["succeeded", "failed", "timed_out", "cancelled"].includes(task.status)) finishLive(turn);
    }).catch(() => {});
  });
}

async function sendPrompt() {
  const message = $("prompt-input").value.trim();
  if (!message || state.submitting || $("send-button").disabled) return;
  state.submitting = true;
  updateComposer();
  closeProject();
  closeCapabilityPicker();
  try {
    if (!state.activeConversation) {
      const conversation = await api("/v1/conversations", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      state.activeConversation = conversation;
      location.hash = conversation.id;
      setProjectForm();
    }
    const conversationId = state.activeConversation.id;
    const skillIds = [...state.selectedSkillIds];
    const mcpIds = [...state.selectedMcpIds];
    const task = await api("/v1/conversations/" + conversationId + "/turns", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, request_id: crypto.randomUUID(), skill_ids: skillIds, mcp_ids: mcpIds }),
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
    clearCapabilitySelection();
    updatePromptCount();
    renderTurns();
    addEvent(turn, { type: "system", title: "正在准备运行环境", message: "正在启动本轮独立任务环境" });
    if (skillIds.length) addEvent(turn, { type: "system", title: "正在准备专业技能", message: skillIds.map((id) => `$${id}`).join("、") });
    if (mcpIds.length) addEvent(turn, { type: "system", title: "已准备外部工具", message: mcpIds.join("、") });
    if (!mcpIds.length && task.auto_mcps?.length) {
      addEvent(turn, { type: "system", title: "已根据问题准备联网能力", message: task.auto_mcps.join("、") });
    }
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
      ["状态", result.status === "succeeded" ? "已完成" : result.status === "timed_out" ? "执行超时" : result.status === "cancelled" ? "已停止" : result.status || "失败"],
      ["任务 ID", turn.task_id],
      ["工作区", state.activeConversation.workspace?.type === "conversation_workspace" ? "窗口持久工作区" : "旧仓库快照"],
      ["本轮可用技能", result.skills?.length ? result.skills.join("、") : "无"],
      ["本轮显式指定", result.requested_skills?.length ? result.requested_skills.join("、") : "无"],
      ["Codex 原生加载", result.loaded_skills?.length ? result.loaded_skills.join("、") : "无记录"],
      ["命令读取技能文件", result.read_skills?.length ? result.read_skills.join("、") : "无记录"],
      ["本轮原生 MCP", result.mcps?.length ? result.mcps.join("、") : "无"],
      ["本轮版本", result.workspace?.turn_sequence ?? "—"],
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
    const workspaceDownload = state.activeConversation.workspace?.type === "conversation_workspace";
    $("detail-workspace-download").classList.toggle("hidden", !workspaceDownload);
    if (workspaceDownload) $("detail-workspace-download").href = `/v1/conversations/${state.activeConversation.id}/workspace`;
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
  source.addEventListener("task.cancelled", stop);
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
    await Promise.all([loadConversations(), loadInstalledCapabilities()]);
    renderCapabilitySelection();
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

$("send-button").addEventListener("click", () => {
  const active = state.turns.some((turn) => ["starting", "running", "cancelling"].includes(turn.status));
  if (active) cancelActiveTask();
  else sendPrompt();
});
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
$("composer-skills").addEventListener("click", () => openCapabilityPicker("skill"));
$("nav-mcp").addEventListener("click", showMcpView);
$("composer-mcp").addEventListener("click", () => openCapabilityPicker("mcp"));
$("capability-close").addEventListener("click", closeCapabilityPicker);
$("capability-skill-tab").addEventListener("click", () => setCapabilityTab("skill"));
$("capability-mcp-tab").addEventListener("click", () => setCapabilityTab("mcp"));
$("capability-search").addEventListener("input", renderCapabilityOptions);
$("capability-options").addEventListener("click", (event) => {
  const id = event.target.closest("[data-capability-id]")?.dataset.capabilityId;
  if (!id) return;
  const selected = selectedCapabilityIds();
  if (selected.has(id)) selected.delete(id); else selected.add(id);
  renderCapabilitySelection();
  closeCapabilityPicker();
});
$("capability-chips").addEventListener("click", (event) => {
  const button = event.target.closest("[data-capability-id]");
  if (!button) return;
  const selected = button.dataset.capabilityKind === "mcp" ? state.selectedMcpIds : state.selectedSkillIds;
  selected.delete(button.dataset.capabilityId);
  renderCapabilitySelection();
  if (!$("capability-picker").classList.contains("hidden")) renderCapabilityOptions();
});
$("capability-market").addEventListener("click", () => {
  if (state.capabilityTab === "skill") showSkillsView(); else showMcpView();
});
$("skills-back").addEventListener("click", showChatView);
$("mcp-back").addEventListener("click", showChatView);
$("skills-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-skill-id]");
  if (button) handleSkillAction(button);
});
$("mcp-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-mcp-id]");
  if (button) handleMcpAction(button);
});
$("history-list").addEventListener("click", (event) => {
  const id = event.target.closest("[data-conversation-id]")?.dataset.conversationId;
  if (id) openConversation(id);
});
$("load-conversations").addEventListener("click", () => loadConversations(false).catch((error) => toast(error.message)));
$("load-older").addEventListener("click", loadOlder);
$("project-button").addEventListener("click", () => {
  const willOpen = $("project-popover").classList.contains("hidden");
  closeCapabilityPicker();
  closeProject();
  $("project-popover").classList.toggle("hidden", !willOpen);
  $("project-button").setAttribute("aria-expanded", String(willOpen));
  if (willOpen) loadWorkspaceFiles(state.workspacePath).catch((error) => toast("工作区加载失败：" + error.message));
});
$("project-save").addEventListener("click", () => loadWorkspaceFiles(state.workspacePath)
  .catch((error) => toast("工作区加载失败：" + error.message)));
$("workspace-files").addEventListener("click", (event) => {
  const entry = event.target.closest("[data-workspace-path]");
  if (entry) loadWorkspaceFiles(entry.dataset.workspacePath).catch((error) => toast("工作区加载失败：" + error.message));
});
$("turn-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const turn = state.turns.find((item) => item.id === button.dataset.turnId);
  if (!turn) return;
  if (button.dataset.action === "file-preview") return openFilePreview(button);
  if (button.dataset.action === "details") return openDetails(turn);
  if (button.dataset.action === "events") return loadEvents(turn);
  try {
    await navigator.clipboard.writeText(turn.assistant_message || "");
    toast("回答已复制。");
  } catch { toast("复制失败，请手动选择文字。"); }
});
$("file-preview-close").addEventListener("click", closeFilePreview);
$("file-preview-done").addEventListener("click", closeFilePreview);
$("file-preview-modal").addEventListener("click", (event) => {
  if (event.target === $("file-preview-modal")) closeFilePreview();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("file-preview-modal").classList.contains("hidden")) closeFilePreview();
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
setInterval(() => {
  for (const element of document.querySelectorAll(".elapsed-time:not([data-completed-at])")) {
    const seconds = Math.max(0, Math.round((Date.now() - new Date(element.dataset.startedAt).getTime()) / 1000));
    element.textContent = seconds < 60 ? `已处理 ${seconds} 秒`
      : `已处理 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
}, 1000);
setProjectForm();
initialize();
