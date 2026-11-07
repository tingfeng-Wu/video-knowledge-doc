"use strict";

const STATUS_LABELS = {
  ready: "待开始",
  queued: "排队中",
  running: "正在生成",
  cancelling: "正在停止",
  completed: "已完成",
  failed: "需处理",
  cancelled: "已取消",
};

const STAGES = [
  ["preflight", "环境检查"],
  ["task_init", "初始化"],
  ["transcript", "语音转写"],
  ["audio", "提取音频"],
  ["asr", "语音识别"],
  ["timeline", "文本时间轴"],
  ["frames", "抽取画面"],
  ["page_changes", "页面变化"],
  ["ocr", "画面文字"],
  ["timeline_v2", "多源对齐"],
  ["chapter_plan_v2", "章节规划"],
  ["chapter_frames", "章节配图"],
  ["document_content_v2", "正文生成"],
  ["export_v2", "文档导出"],
  ["completed", "处理完成"],
];

const state = {
  view: "dashboard",
  tasks: [],
  overview: null,
  filter: "all",
  search: "",
  showArchived: false,
  currentTaskId: null,
  currentTask: null,
  documentData: null,
  selectedVersion: "machine",
  editing: false,
  draft: null,
  editingDirty: false,
  workspaceTimer: null,
  createMode: "upload",
  file: null,
  creating: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function attr(value) {
  return esc(value).replaceAll("`", "&#096;");
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "大小未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / Math.pow(1024, index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function formatDuration(seconds) {
  const value = Math.max(0, Math.round(Number(seconds || 0)));
  if (!value) return "时长未知";
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分 ${secs} 秒`;
}

function relativeTime(iso) {
  if (!iso) return "刚刚";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "时间未知";
  const diff = Date.now() - date.getTime();
  if (diff < 45_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.max(1, Math.floor(diff / 60_000))} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

function statusLabel(status) {
  return STATUS_LABELS[status] || "未知状态";
}

function taskTitle(task) {
  return task.document?.title || task.document_title || task.display_name || task.metadata?.file_name || task.task_id;
}

function isActive(task) {
  return ["queued", "running", "cancelling"].includes(task?.status);
}

async function api(path, options = {}) {
  const request = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  };
  const response = await fetch(path, request);
  let payload = null;
  const contentType = response.headers.get("Content-Type") || "";
  if (contentType.includes("application/json")) {
    payload = await response.json().catch(() => null);
  }
  if (!response.ok) {
    const error = new Error(payload?.error?.message || `请求失败（${response.status}）`);
    error.code = payload?.error?.code || "REQUEST_FAILED";
    throw error;
  }
  return payload;
}

function toast(title, message = "", type = "success", timeout = 4200) {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.innerHTML = `<div><strong>${esc(title)}</strong><span>${esc(message)}</span></div>`;
  $("#toastStack").append(node);
  window.setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(8px)";
    node.style.transition = ".2s ease";
    window.setTimeout(() => node.remove(), 220);
  }, timeout);
}

function showView(view, updateHistory = true) {
  if (state.view === "workspace" && view !== "workspace" && state.editingDirty) {
    if (!window.confirm("当前编辑还未保存，离开会放弃这些修改。确认离开吗？")) return;
    state.editing = false;
    state.draft = null;
    state.editingDirty = false;
  }
  const target = view === "tasks" ? "tasks" : view === "workspace" ? "workspace" : "dashboard";
  state.view = target;
  $$(".view").forEach((element) => element.classList.toggle("active", element.id === `${target}View`));
  $$(".nav-item").forEach((element) => {
    const navTarget = element.dataset.view === "tasks" && target === "workspace" ? "tasks" : target;
    element.classList.toggle("active", element.dataset.view === navTarget);
  });
  $("#sidebar").classList.remove("open");
  if (target !== "workspace") stopWorkspacePolling();
  if (updateHistory && target !== "workspace") {
    history.replaceState(null, "", target === "tasks" ? "#tasks" : "#dashboard");
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
  updateBreadcrumb(target);
}

function updateBreadcrumb(view, task = null) {
  let html = "";
  if (view === "tasks") {
    html = `<span>任务中心</span><svg viewBox="0 0 16 16" fill="none"><path d="m6 3 5 5-5 5"/></svg><strong>全部任务</strong>`;
  } else if (view === "workspace") {
    html = `<span>任务中心</span><svg viewBox="0 0 16 16" fill="none"><path d="m6 3 5 5-5 5"/></svg><strong>${esc(taskTitle(task || state.currentTask || {}))}</strong>`;
  } else {
    html = `<span>工作台</span><svg viewBox="0 0 16 16" fill="none"><path d="m6 3 5 5-5 5"/></svg><strong>概览</strong>`;
  }
  $("#breadcrumb").innerHTML = html;
}

function showDashboard() {
  showView("dashboard");
  refreshDashboard();
}

async function refreshDashboard() {
  try {
    const overview = await api("/api/overview");
    state.overview = overview;
    const counts = overview.counts || {};
    $("#metricTotal").textContent = counts.total ?? 0;
    $("#metricActive").textContent = counts.active ?? 0;
    $("#metricCompleted").textContent = counts.completed ?? 0;
    $("#metricAttention").textContent = counts.needs_attention ?? 0;
    $("#activeTaskCount").textContent = counts.active ?? 0;
    renderRecentTasks(overview.recent || []);
    renderEnvironment(overview.system || {});
  } catch (error) {
    toast("工作台加载失败", error.message, "error");
  }
}

function renderRecentTasks(tasks) {
  const root = $("#recentTaskList");
  if (!tasks.length) {
    root.innerHTML = `
      <div class="empty-state">
        <div><div class="empty-illustration"></div><strong>还没有视频任务</strong>
        <p>导入第一段视频，系统会在本机转写语音、整理章节，并生成可编辑的图文讲义。</p>
        <button class="button button-primary button-small" data-open-create>导入视频</button></div>
      </div>`;
    return;
  }
  root.innerHTML = tasks.map((task) => `
    <article class="recent-item" data-open-task="${attr(task.task_id)}">
      <span class="recent-icon">
        <svg viewBox="0 0 24 24" fill="none"><path d="M7 3.5h7l4 4v13H7z"/><path d="M14 3.5v4h4M10 12h5M10 16h4"/></svg>
      </span>
      <div class="recent-main">
        <strong>${esc(taskTitle(task))}</strong>
        <small>${esc(task.task_id)} · ${esc(task.document?.chapter_count ? `${task.document.chapter_count} 章` : task.stage_message || "等待处理")}</small>
      </div>
      <div class="recent-side">
        <span class="status-pill ${attr(task.status)}">${esc(statusLabel(task.status))}</span>
        <time>${esc(relativeTime(task.updated_at))}</time>
      </div>
    </article>`).join("");
}

function renderEnvironment(system) {
  const ffmpegOk = Boolean(system.ffmpeg && system.ffprobe);
  const stateDot = $(".state-dot");
  stateDot.classList.toggle("warn", !ffmpegOk);
  $("#systemStateText").textContent = ffmpegOk ? "FFmpeg 环境正常" : "缺少媒体工具";
  $("#environmentSummary").textContent = ffmpegOk ? "视频处理依赖已就绪" : "请安装 FFmpeg 后重新启动";
  $("#environmentTags").innerHTML = [
    `<span class="environment-tag ${ffmpegOk ? "ok" : "warn"}">${ffmpegOk ? "FFmpeg 就绪" : "FFmpeg 缺失"}</span>`,
    `<span class="environment-tag ok">Python ${esc(system.python || "")}</span>`,
    `<span class="environment-tag ${system.model_config ? "ok" : "warn"}">${system.model_config ? "模型角色已配置" : "模型配置缺失"}</span>`,
    `<span class="environment-tag ${system.llm_key_ready ? "ok" : "warn"}">${system.llm_key_ready ? "模型密钥已配置" : "模型密钥缺失"}</span>`,
    `<span class="environment-tag">输出目录 ${esc(system.output_root || "outputs")}</span>`,
  ].join("");
}

async function showTasks(focusSearch = false) {
  showView("tasks");
  await loadTasks();
  if (focusSearch) $("#taskSearch").focus();
}

async function loadTasks() {
  try {
    const query = new URLSearchParams({
      status: "all",
      search: state.search,
      archived: state.showArchived ? "true" : "false",
    });
    const payload = await api(`/api/tasks?${query}`);
    state.tasks = payload.tasks || [];
    renderTaskList();
    const active = state.tasks.filter(isActive).length;
    $("#activeTaskCount").textContent = active;
    $("#activeFilterCount").textContent = active;
    $("#allTaskCount").textContent = state.tasks.length;
  } catch (error) {
    $("#taskTable").innerHTML = emptyState("任务加载失败", error.message, "重试", "data-retry-tasks");
  }
}

function filteredTasks() {
  if (state.filter === "all") return state.tasks;
  if (state.filter === "active") return state.tasks.filter(isActive);
  if (state.filter === "failed") return state.tasks.filter((task) => ["failed", "cancelled"].includes(task.status));
  return state.tasks.filter((task) => task.status === state.filter);
}

function renderTaskList() {
  const tasks = filteredTasks();
  const root = $("#taskTable");
  if (!tasks.length) {
    root.innerHTML = emptyState(
      state.search ? "没有匹配的任务" : "这里还没有任务",
      state.search ? "换一个关键词，或清除当前筛选后重试。" : "导入一段知识视频后，任务会出现在这里。",
      state.search ? "清除搜索" : "新建任务",
      state.search ? "data-clear-task-search" : "data-open-create"
    );
    return;
  }
  root.innerHTML = tasks.map(taskRowHTML).join("");
}

function taskRowHTML(task) {
  const progress = Math.max(0, Math.min(100, Number(task.progress || (task.status === "completed" ? 100 : 0))));
  const showProgress = isActive(task) || task.status === "failed";
  const fileMeta = task.metadata || {};
  const size = fileMeta.file_size_bytes ? formatBytes(fileMeta.file_size_bytes) : "文件信息待读取";
  const duration = fileMeta.duration_seconds ? formatDuration(fileMeta.duration_seconds) : (task.document?.chapter_count ? `${task.document.chapter_count} 章` : "—");
  const action = isActive(task)
    ? `<button class="row-action" data-cancel-task="${attr(task.task_id)}">取消</button>`
    : `<button class="row-action primary" data-open-task="${attr(task.task_id)}">${task.status === "completed" ? "打开文档" : "查看"}</button>`;
  return `
    <article class="task-row" data-open-task="${attr(task.task_id)}">
      <div class="task-cell-main">
        <span class="task-file-icon"><svg viewBox="0 0 24 24" fill="none"><path d="M7 3.5h7l4 4v13H7z"/><path d="M14 3.5v4h4M10 12h5M10 16h4"/></svg></span>
        <div class="task-title">
          <strong>${esc(taskTitle(task))}</strong>
          <small>${esc(task.task_id)} · ${esc(task.display_name || fileMeta.file_name || "视频任务")}</small>
          ${showProgress ? `<div class="task-progress-mini"><span><i style="width:${progress}%"></i></span></div>` : ""}
        </div>
      </div>
      <div class="task-status-cell"><span class="status-pill ${attr(task.status)}">${esc(statusLabel(task.status))}</span></div>
      <div class="task-meta task-meta-cell"><strong>${esc(duration)}</strong>${esc(size)}</div>
      <div class="task-meta task-updated"><strong>${esc(relativeTime(task.updated_at))}</strong>${esc(task.stage_message || "")}</div>
      <div class="task-actions">
        <button class="row-action" data-archive-task="${attr(task.task_id)}">${task.archived ? "恢复" : "归档"}</button>
        ${action}
      </div>
    </article>`;
}

function emptyState(title, text, actionText, actionAttr) {
  return `
    <div class="empty-state">
      <div>
        <div class="empty-illustration"></div>
        <strong>${esc(title)}</strong>
        <p>${esc(text)}</p>
        <button class="button button-primary button-small" ${attr(actionAttr)}>${esc(actionText)}</button>
      </div>
    </div>`;
}

async function openTask(taskId) {
  if (state.view === "workspace" && state.currentTaskId !== taskId && state.editingDirty) {
    if (!window.confirm("当前编辑还未保存，打开其他任务会放弃这些修改。确认继续吗？")) return;
    state.editingDirty = false;
  }
  state.currentTaskId = taskId;
  state.documentData = null;
  state.editing = false;
  state.draft = null;
  state.editingDirty = false;
  showView("workspace", false);
  history.replaceState(null, "", `#task=${encodeURIComponent(taskId)}`);
  $("#workspaceLoading").hidden = false;
  $("#workspaceContent").hidden = true;
  updateBreadcrumb("workspace");
  try {
    const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    state.currentTask = task;
    updateBreadcrumb("workspace", task);
    if (task.status === "completed" && task.document?.has_content) {
      await loadDocument(taskId);
    } else {
      renderTaskWorkspace(task);
      if (isActive(task)) startWorkspacePolling(taskId);
    }
  } catch (error) {
    $("#workspaceLoading").innerHTML = `<div class="empty-state"><div><div class="empty-illustration"></div><strong>任务无法打开</strong><p>${esc(error.message)}</p><button class="button button-secondary button-small" data-view="tasks">返回任务中心</button></div></div>`;
    toast("打开任务失败", error.message, "error");
  }
}

async function loadDocument(taskId, version = null) {
  try {
    const query = version ? `?version=${encodeURIComponent(version)}` : "";
    const payload = await api(`/api/tasks/${encodeURIComponent(taskId)}/document${query}`);
    state.documentData = payload;
    state.currentTask = payload.task;
    state.selectedVersion = payload.selected.version_id;
    state.editing = false;
    state.draft = null;
    state.editingDirty = false;
    renderDocumentWorkspace(payload);
    updateBreadcrumb("workspace", payload.task);
  } catch (error) {
    toast("文档读取失败", error.message, "error");
    const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`).catch(() => state.currentTask);
    if (task) renderTaskWorkspace({ ...task, status: "failed", error: { code: "CONTENT_INVALID", message: error.message, detail: "" } });
  }
}

function workspaceHeader(task, options = {}) {
  const completed = task.status === "completed" || Boolean(state.documentData);
  const active = isActive(task);
  const versions = state.documentData?.versions || task.edits || [];
  const versionOptions = completed && state.documentData && !state.editing ? [
    `<option value="machine" ${state.selectedVersion === "machine" ? "selected" : ""}>机器生成原稿</option>`,
    ...versions.map((version) => `<option value="${attr(version.edit_id)}" ${state.selectedVersion === version.edit_id ? "selected" : ""}>${esc(version.version_name)}</option>`),
  ].join("") : "";
  const runAction = active
    ? `<button class="button button-danger button-small" data-cancel-task="${attr(task.task_id)}">停止生成</button>`
    : completed
      ? `<button class="button button-secondary button-small" data-run-task="${attr(task.task_id)}">重新生成</button>`
      : `<button class="button button-primary button-small" data-run-task="${attr(task.task_id)}">${task.status === "failed" ? "重新运行" : "开始生成"}</button>`;
  const download = completed && state.documentData && !options.progress
    ? `<div class="menu-wrap">
        <button class="button button-primary button-small" type="button" data-toggle-download>下载文档
          <svg viewBox="0 0 20 20" fill="none"><path d="m6 8 4 4 4-4" stroke-linecap="round"/></svg>
        </button>
        <div class="download-menu" id="downloadMenu">
          <button type="button" data-export-format="docx">Word 文档 <small>.docx</small></button>
          <button type="button" data-export-format="md">Markdown <small>.md</small></button>
          <button type="button" data-export-format="json">结构化 JSON <small>.json</small></button>
        </div>
      </div>` : "";
  const edit = completed && state.documentData && !options.progress && !state.editing
    ? `<button class="button button-secondary button-small" type="button" data-start-edit>编辑内容</button>` : "";
  return `
    <header class="workspace-head">
      <div class="workspace-heading">
        <button class="back-button" type="button" data-view="tasks"><svg viewBox="0 0 16 16" fill="none"><path d="m10 3-5 5 5 5"/></svg>返回任务中心</button>
        <div class="workspace-title-row">
          <h1 id="workspaceTitle">${esc(taskTitle(task))}</h1>
          <span class="status-pill ${attr(task.status)}">${esc(statusLabel(task.status))}</span>
        </div>
        <div class="workspace-subtitle">
          <span class="workspace-id">${esc(task.task_id)}</span>
          <span><svg viewBox="0 0 20 20" fill="none"><path d="M4 5.5h12v11H4zM7 3.5v4M13 3.5v4M4 9h12"/></svg>${esc(relativeTime(task.updated_at))}</span>
          <span>${esc(task.stage_message || "")}</span>
          ${task.document?.chapter_count ? `<span>${task.document.chapter_count} 章</span>` : ""}
        </div>
      </div>
      <div class="workspace-actions">
        ${versionOptions ? `<select class="version-select" id="versionSelect" aria-label="选择文档版本">${versionOptions}</select>` : ""}
        ${edit}${runAction}${download}
      </div>
    </header>`;
}

function renderTaskWorkspace(task) {
  $("#workspaceLoading").hidden = true;
  const content = $("#workspaceContent");
  content.hidden = false;
  const currentStageIndex = STAGES.findIndex(([id]) => id === task.stage);
  const running = isActive(task);
  const progress = task.status === "completed" ? 100 : Math.max(1, Number(task.progress || 0));
  const stages = STAGES.map(([id, label], index) => {
    const done = task.status === "completed" || (currentStageIndex >= 0 && index < currentStageIndex);
    const active = running && index === currentStageIndex;
    const stateClass = done ? "done" : active ? "active" : "";
    return `<div class="stage-item ${stateClass}"><strong>${esc(label)}</strong><small>${done ? "已完成" : active ? "处理中" : "等待"}</small></div>`;
  }).join("");
  const errorBlock = task.error ? `
    <div class="task-error">
      <svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5M12 17h.01"/><path d="M10.3 4.5 3.2 17a2 2 0 0 0 1.7 3h14.2a2 2 0 0 0 1.7-3L13.7 4.5a2 2 0 0 0-3.4 0Z"/></svg>
      <div><strong>${esc(task.error.message || "任务处理失败")}</strong><p>失败阶段：${esc(stageLabel(task.error.failed_stage || task.stage))}。缓存和已发布结果已保留，可以直接重新运行。</p><code>${esc(task.error.code)} ${esc(task.error.detail || "")}</code>
      <div class="error-actions">
        <button class="button button-secondary button-small" data-run-task="${attr(task.task_id)}">重试任务</button>
        ${task.error.code === "LLM_APPROVAL_REQUIRED" ? `<button class="button button-primary button-small" data-run-with-llm="${attr(task.task_id)}">授权模型调用并重试</button>` : ""}
      </div></div>
    </div>` : "";
  const existingDocumentAction = task.document?.has_content && !running
    ? `<div class="error-actions" style="margin-top:16px"><button class="button button-secondary button-small" data-open-existing-document>打开已有文档结果</button></div>`
    : "";
  const activeMessage = running
    ? `正在${esc(stageLabel(task.stage))}`
    : task.status === "ready" ? "任务已创建，等待开始生成" : task.status === "completed" ? "处理已完成" : "任务当前未运行";
  content.innerHTML = `
    ${workspaceHeader(task, { progress: true })}
    <section class="panel workspace-progress">
      <div class="progress-summary">
        ${running ? `<span class="loading-ring"></span>` : `<span class="metric-icon ${task.status === "failed" ? "rose" : "blue"}"><svg viewBox="0 0 24 24" fill="none"><path d="M7 3.5h7l4 4v13H7z"/><path d="M14 3.5v4h4M10 12h5M10 16h4"/></svg></span>`}
        <div><strong>${activeMessage}</strong><small>${esc(task.stage_message || "可查看任务配置与处理记录")}</small></div>
      </div>
      <div class="progress-bar-row"><div class="progress-bar"><span style="width:${progress}%"></span></div><b>${progress}%</b></div>
      <div class="stage-list">${stages}</div>
      ${errorBlock}
      ${existingDocumentAction}
    </section>
    <div class="task-overview-grid">
      <section class="overview-card">
        <h3>任务信息</h3>
        <dl class="detail-list">
          <div><dt>任务 ID</dt><dd>${esc(task.task_id)}</dd></div>
          <div><dt>视频文件</dt><dd title="${attr(task.source_video || "")}">${esc(task.metadata?.file_name || task.source_video || "未登记")}</dd></div>
          <div><dt>文件大小</dt><dd>${esc(task.metadata?.file_size_bytes ? formatBytes(task.metadata.file_size_bytes) : "待读取")}</dd></div>
          <div><dt>视频时长</dt><dd>${esc(task.metadata?.duration_seconds ? formatDuration(task.metadata.duration_seconds) : "待读取")}</dd></div>
          <div><dt>OCR 模式</dt><dd>${esc(task.options?.ocr_mode || "auto")}</dd></div>
          <div><dt>模型调用</dt><dd>${task.options?.allow_llm ? "已授权" : "缓存优先 / 未授权"}</dd></div>
        </dl>
      </section>
      <section class="side-card">
        <h3>处理记录</h3>
        ${renderEventList(task.events || [])}
      </section>
    </div>`;
}

function renderEventList(events) {
  if (!events.length) return `<div class="editor-empty">暂无处理记录</div>`;
  return `<ul class="event-list">${events.slice(-8).reverse().map((event) => `
    <li class="${attr(event.kind)}"><span>${esc(event.message)}</span><time>${esc(relativeTime(event.timestamp))}</time></li>`).join("")}</ul>`;
}

function stageLabel(stage) {
  return STAGES.find(([id]) => id === stage)?.[1] || stage || "准备阶段";
}

function renderDocumentWorkspace(payload) {
  $("#workspaceLoading").hidden = true;
  const content = $("#workspaceContent");
  content.hidden = false;
  const selected = state.editing ? { ...payload.selected, editable: state.draft } : payload.selected;
  const document = selected.editable;
  const version = payload.versions.find((item) => item.edit_id === selected.version_id);
  content.innerHTML = `
    ${workspaceHeader(payload.task)}
    <div class="document-layout ${state.editing ? "with-editor" : ""}">
      ${renderToc(document)}
      <article class="article-card" id="articleCard">
        ${renderArticle(document, selected, version)}
      </article>
      ${state.editing ? renderEditor(state.draft, selected) : ""}
    </div>`;
  if (!$("#versionSelect")) return;
}

function renderToc(document) {
  return `
    <aside class="doc-toc">
      <strong>文档目录</strong>
      <div class="toc-items">
        <button class="toc-button" type="button" data-scroll-section="overview"><b>00</b><span>视频概述</span></button>
        ${document.chapters.map((chapter) => `<button class="toc-button" type="button" data-scroll-section="${attr(chapter.chapter_id)}"><b>${String(chapter.index).padStart(2, "0")}</b><span>${esc(chapter.title)}</span></button>`).join("")}
        ${document.overall_conclusion.length ? `<button class="toc-button" type="button" data-scroll-section="conclusion"><b>END</b><span>核心结论</span></button>` : ""}
      </div>
      <div class="toc-divider"></div>
      <div class="source-meta">
        <span>内容来源 <span>${esc(state.documentData?.selected?.source_content_id || "机器正文")}</span></span>
        <span>版本类型 <span>${state.selectedVersion === "machine" ? "MACHINE" : "EDITED"}</span></span>
      </div>
    </aside>`;
}

function renderArticle(document, selected, version) {
  const meta = selected.manifest || {};
  const kind = selected.kind === "machine" ? "机器生成原稿" : "独立编辑版本";
  return `
    <div class="article-meta">
      <span class="article-kind">${selected.kind === "machine" ? "原始结果 · 只读" : `编辑版本 · ${esc(version?.version_name || selected.version_id)}`}</span>
      <span>${selected.stats.chapter_count} 章 · ${selected.stats.frame_count} 张配图 · ${esc(kind)}</span>
    </div>
    <h1>${esc(document.title)}</h1>
    <section class="article-overview" id="section-overview">
      ${renderPreviewBlocks(document.overview)}
    </section>
    ${document.chapters.map((chapter) => renderChapter(chapter)).join("")}
    ${document.overall_conclusion.length ? `
      <section class="conclusion-section" id="section-conclusion">
        <h2>核心结论</h2>
        ${renderPreviewBlocks(document.overall_conclusion)}
      </section>` : ""}
    <footer class="article-footer">
      <span>来源任务 <strong>${esc(state.currentTaskId)}</strong></span>
      <span>${meta.created_at ? `版本保存于 ${esc(new Date(meta.created_at).toLocaleString("zh-CN"))}` : "由现有处理链生成"}</span>
    </footer>`;
}

function renderChapter(chapter) {
  const visibleFrames = chapter.selected_frame_ids
    ? chapter.frames.filter((frame) => chapter.selected_frame_ids.includes(frame.frame_id))
    : chapter.frames;
  return `
    <section class="article-section" id="section-${attr(chapter.chapter_id)}">
      <div class="chapter-kicker">CHAPTER ${String(chapter.index).padStart(2, "0")}</div>
      <h2>${esc(chapter.title)}</h2>
      <div class="chapter-time">视频位置 ${esc(chapter.start_text)} — ${esc(chapter.end_text)}</div>
      ${renderPreviewBlocks(chapter.blocks)}
      ${visibleFrames.length ? `<div class="frame-grid">${visibleFrames.map(renderFrame).join("")}</div>` : ""}
    </section>`;
}

function renderFrame(frame) {
  return `
    <figure class="frame-card">
      <img src="${attr(frame.image_url)}" alt="${attr(frame.caption_text || `视频画面 ${frame.frame_id}`)}" loading="lazy" onerror="this.style.display='none';this.parentElement.classList.add('image-missing')">
      <figcaption><strong>${esc(frame.decoded_text || "")}</strong><p>${esc(frame.caption_text || "章节代表画面")}</p></figcaption>
    </figure>`;
}

function renderPreviewBlocks(blocks) {
  return (blocks || []).map((block) => {
    if (block.type === "list") {
      const tag = block.list_kind === "numbered" ? "ol" : "ul";
      return `<${tag} class="block-list">${(block.items || []).map((item) => `<li>${esc(item.text)}</li>`).join("")}</${tag}>`;
    }
    if (block.type === "comparison") {
      return `<table class="comparison-table"><thead><tr><th>维度</th>${(block.columns || []).map((column) => `<th>${esc(column)}</th>`).join("")}</tr></thead>
        <tbody>${(block.rows || []).map((row) => `<tr><td><strong>${esc(row.dimension)}</strong></td>${(row.cells || []).map((cell) => `<td>${esc(cell.text)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }
    if (block.type === "callout") return `<div class="callout-block">${esc(block.text)}</div>`;
    return `<p class="block-text">${esc(block.text)}</p>`;
  }).join("");
}

function renderEditor(document, selected) {
  return `
    <aside class="editor-panel" id="editorPanel">
      <div class="editor-head">
        <div><span class="eyebrow">DOCUMENT EDITOR</span><h3>编辑文档内容</h3></div>
        <button class="icon-button editor-close" type="button" data-cancel-edit aria-label="关闭编辑">
          <svg viewBox="0 0 24 24" fill="none"><path d="m6 6 12 12M18 6 6 18" stroke-linecap="round"/></svg>
        </button>
      </div>
      <div class="editor-body">
        <div class="editor-section">
          <div class="editor-section-title"><strong>文档信息</strong><small>${esc(selected.kind === "machine" ? "从机器原稿创建新版本" : "继续编辑当前版本")}</small></div>
          <label class="field editor-field"><span>文档标题</span><input type="text" data-edit-field="title" value="${attr(document.title)}"></label>
        </div>
        <div class="editor-section">
          <div class="editor-section-title"><strong>全文概述</strong><small>${document.overview.length} 个段落</small></div>
          ${renderEditableBlocks(document.overview, "overview", -1)}
        </div>
        <div class="editor-section">
          <div class="editor-section-title"><strong>章节内容</strong><small>标题、正文与配图</small></div>
          ${document.chapters.map((chapter, chapterIndex) => `
            <details class="editor-chapter" ${chapterIndex === 0 ? "open" : ""}>
              <summary><b>${String(chapter.index).padStart(2, "0")}</b><span>${esc(chapter.title)}</span><svg viewBox="0 0 16 16" fill="none"><path d="m3 6 5 5 5-5"/></svg></summary>
              <div class="editor-chapter-body">
                <label class="field editor-field"><span>章节标题</span><input type="text" data-edit-field="chapter-title" data-chapter="${chapterIndex}" value="${attr(chapter.title)}"></label>
                ${renderEditableBlocks(chapter.blocks, "chapter", chapterIndex)}
                <div class="editor-section-title" style="margin-top:16px"><strong>章节配图</strong><small>取消勾选即从版本中删除</small></div>
                ${chapter.frames.length ? `<div class="editor-frame-list">${chapter.frames.map((frame, frameIndex) => `
                  <div class="editor-frame" data-frame-wrap="${chapterIndex}-${frameIndex}">
                    <img src="${attr(frame.image_url)}" alt="" onerror="this.style.display='none';this.parentElement.classList.add('image-missing')">
                    <label title="保留配图"><input type="checkbox" checked data-edit-frame data-chapter="${chapterIndex}" data-frame-index="${frameIndex}"><svg viewBox="0 0 16 16" fill="none"><path d="m3 8 3 3 7-7" stroke-linecap="round"/></svg></label>
                  </div>`).join("")}</div>` : `<div class="editor-empty">本章暂无配图</div>`}
              </div>
            </details>`).join("")}
        </div>
        ${document.overall_conclusion.length ? `<div class="editor-section">
          <div class="editor-section-title"><strong>全文结论</strong></div>
          ${renderEditableBlocks(document.overall_conclusion, "conclusion", -1)}
        </div>` : ""}
      </div>
      <div class="editor-footer">
        <label class="field editor-field"><span>版本备注</span><input type="text" id="versionName" maxlength="80" placeholder="例如：修正转写错词与章节标题"></label>
        <div class="editor-footer-actions">
          <button class="button button-secondary" type="button" data-cancel-edit>取消</button>
          <button class="button button-primary" type="button" id="saveEditButton" data-save-edit>保存为新版本</button>
        </div>
      </div>
    </aside>`;
}

function renderEditableBlocks(blocks, scope, chapterIndex) {
  if (!blocks?.length) return `<div class="editor-empty">暂无正文</div>`;
  return blocks.map((block, blockIndex) => {
    const base = `data-chapter="${chapterIndex}" data-block="${blockIndex}"`;
    if (block.type === "list") {
      return `<div class="editor-field"><label class="field"><span>列表 · ${esc(block.role || "正文")}</span></label>
        ${(block.items || []).map((item, itemIndex) => `<div class="editor-list-item"><span>${String(itemIndex + 1).padStart(2, "0")}</span><textarea class="field" data-edit-field="list-item" ${base} data-item="${itemIndex}" style="min-height:54px">${esc(item.text)}</textarea></div>`).join("")}</div>`;
    }
    if (block.type === "comparison") {
      return `<div class="editor-field"><label class="field"><span>对比表</span></label>
        ${(block.rows || []).map((row, rowIndex) => `<div style="padding:9px 0;border-top:1px solid #edf0f5">
          <input class="field" data-edit-field="comparison-dimension" ${base} data-row="${rowIndex}" value="${attr(row.dimension)}" style="width:100%;min-height:34px;padding:7px 9px;border:1px solid #d9e0ea;border-radius:7px;margin-bottom:5px">
          ${(row.cells || []).map((cell, cellIndex) => `<input data-edit-field="comparison-cell" ${base} data-row="${rowIndex}" data-cell="${cellIndex}" value="${attr(cell.text)}" style="width:100%;min-height:34px;padding:7px 9px;border:1px solid #d9e0ea;border-radius:7px;margin-bottom:5px">`).join("")}
        </div>`).join("")}</div>`;
    }
    return `<label class="field editor-field"><span>${esc(block.type === "callout" ? "强调段落" : "正文段落")}</span><textarea data-edit-field="block-text" ${base} style="min-height:${Math.max(72, Math.min(180, String(block.text || "").length / 1.2))}px">${esc(block.text)}</textarea></label>`;
  }).join("");
}

function startEditing() {
  const selected = state.documentData?.selected;
  if (!selected) return;
  state.draft = clone(selected.editable);
  state.draft.chapters.forEach((chapter) => {
    chapter.selected_frame_ids = chapter.frames.map((frame) => frame.frame_id);
  });
  state.editing = true;
  state.editingDirty = false;
  renderDocumentWorkspace(state.documentData);
  $("#editorPanel")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function cancelEditing(force = false) {
  if (!force && state.editingDirty && !window.confirm("放弃尚未保存的修改？")) return;
  state.editing = false;
  state.draft = null;
  state.editingDirty = false;
  renderDocumentWorkspace(state.documentData);
}

async function saveEdit() {
  if (!state.draft) return;
  const button = $("#saveEditButton");
  button.disabled = true;
  button.innerHTML = `<span class="loading-ring"></span> 正在保存`;
  try {
    const saved = await api(`/api/tasks/${encodeURIComponent(state.currentTaskId)}/edits`, {
      method: "POST",
      body: JSON.stringify({
        base_version: state.documentData.selected.version_id,
        version_name: $("#versionName")?.value || "",
        editable: state.draft,
      }),
    });
    state.editing = false;
    state.draft = null;
    state.editingDirty = false;
    toast("编辑版本已保存", "机器原稿保持不变，导出将跟随新版本。");
    await loadDocument(state.currentTaskId, saved.version_id);
  } catch (error) {
    button.disabled = false;
    button.textContent = "保存为新版本";
    toast("保存失败", error.message, "error");
  }
}

function handleEditorInput(target) {
  if (!state.draft || !("editField" in target.dataset)) return;
  const field = target.dataset.editField;
  const chapterIndex = Number(target.dataset.chapter);
  const blockIndex = Number(target.dataset.block);
  const blocks = field.startsWith("chapter") || field.startsWith("block") || field.startsWith("list") || field.startsWith("comparison")
    ? state.draft.chapters[chapterIndex]?.blocks
    : field.startsWith("conclusion") ? state.draft.overall_conclusion : state.draft.overview;
  if (field === "title") state.draft.title = target.value;
  else if (field === "chapter-title") state.draft.chapters[chapterIndex].title = target.value;
  else if (field === "block-text") blocks[blockIndex].text = target.value;
  else if (field === "list-item") blocks[blockIndex].items[Number(target.dataset.item)].text = target.value;
  else if (field === "comparison-dimension") blocks[blockIndex].rows[Number(target.dataset.row)].dimension = target.value;
  else if (field === "comparison-cell") blocks[blockIndex].rows[Number(target.dataset.row)].cells[Number(target.dataset.cell)].text = target.value;
  state.editingDirty = true;
  updateEditorPreview();
}

function updateEditorPreview() {
  if (!state.editing || !state.draft) return;
  $(".doc-toc")?.replaceWith(renderMarkdownElement(renderToc(state.draft)));
  const article = $("#articleCard");
  if (article) {
    const temp = document.createElement("div");
    temp.innerHTML = renderArticle(state.draft, { ...state.documentData.selected, stats: {
      chapter_count: state.draft.chapters.length,
      frame_count: state.draft.chapters.reduce((sum, chapter) => sum + chapter.selected_frame_ids.length, 0),
    }}, state.documentData.versions.find((item) => item.edit_id === state.documentData.selected.version_id));
    article.innerHTML = temp.innerHTML;
  }
}

function renderMarkdownElement(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

async function startTask(taskId, overrides = {}) {
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/run`, {
      method: "POST",
      body: JSON.stringify({
        allow_llm: false,
        ...overrides,
      }),
    });
    toast("任务已开始", "可以留在当前页面查看实时阶段进度。");
    state.currentTaskId = taskId;
    await openTask(taskId);
    refreshDashboard();
  } catch (error) {
    toast("无法启动任务", error.message, "error");
  }
}

async function cancelTask(taskId) {
  if (!window.confirm("停止当前生成吗？已发布的阶段缓存会保留。")) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST", body: "{}" });
    toast("正在停止任务", "生成进程结束后会保留可复用的已有结果。");
    if (state.currentTaskId === taskId) startWorkspacePolling(taskId);
    await loadTasks();
  } catch (error) {
    toast("无法取消任务", error.message, "error");
  }
}

async function toggleArchive(taskId) {
  const task = state.tasks.find((item) => item.task_id === taskId) || state.currentTask;
  if (!task) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/${task.archived ? "restore" : "archive"}`, {
      method: "POST",
      body: "{}",
    });
    toast(task.archived ? "任务已恢复" : "任务已归档", "归档不会删除任何任务或生成结果。");
    await loadTasks();
    if (state.currentTaskId === taskId && task.archived) await openTask(taskId);
  } catch (error) {
    toast("操作失败", error.message, "error");
  }
}

function startWorkspacePolling(taskId) {
  stopWorkspacePolling();
  const poll = async () => {
    if (state.view !== "workspace" || state.currentTaskId !== taskId) return;
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
      state.currentTask = task;
      if (state.editing) {
        state.workspaceTimer = window.setTimeout(poll, 2500);
        return;
      }
      if (task.status === "completed" && task.document?.has_content) {
        await loadDocument(taskId);
        refreshDashboard();
        return;
      }
      renderTaskWorkspace(task);
      if (isActive(task)) state.workspaceTimer = window.setTimeout(poll, 2200);
      else refreshDashboard();
    } catch (error) {
      state.workspaceTimer = window.setTimeout(poll, 3500);
    }
  };
  state.workspaceTimer = window.setTimeout(poll, 1200);
}

function stopWorkspacePolling() {
  if (state.workspaceTimer) window.clearTimeout(state.workspaceTimer);
  state.workspaceTimer = null;
}

async function downloadDocument(format) {
  const version = state.selectedVersion || "machine";
  const formatLabel = { docx: "Word", md: "Markdown", json: "JSON" }[format];
  toast(`正在准备${formatLabel}`, "导出过程不会修改机器原稿。", "success", 2500);
  try {
    await api(`/api/tasks/${encodeURIComponent(state.currentTaskId)}/exports`, {
      method: "POST",
      body: JSON.stringify({ version_id: version }),
    });
    const link = document.createElement("a");
    link.href = `/api/tasks/${encodeURIComponent(state.currentTaskId)}/download/${encodeURIComponent(version)}/${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    $("#downloadMenu")?.classList.remove("open");
  } catch (error) {
    toast("导出失败", error.message, "error");
  }
}

function openCreateDialog() {
  state.file = null;
  state.createMode = "upload";
  $("#createForm").reset();
  $("#boundedContent").checked = true;
  $("#allowLlm").checked = false;
  $("#startAfterCreate").checked = true;
  $("#selectedFile").hidden = true;
  $$(".source-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.sourceTab === "upload"));
  $$(".source-panel").forEach((panel) => panel.classList.toggle("active", panel.id === "uploadSourcePanel"));
  $("#createDialog").showModal();
  window.setTimeout(() => $("#videoFile").focus(), 80);
}

function closeDialog(id) {
  const dialog = document.getElementById(id);
  if (dialog?.open) dialog.close();
}

function setSourceMode(mode) {
  state.createMode = mode;
  $$(".source-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.sourceTab === mode));
  $$(".source-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${mode}SourcePanel`));
}

function chooseFile(file) {
  if (!file) return;
  state.file = file;
  const selected = $("#selectedFile");
  selected.hidden = false;
  selected.textContent = `${file.name} · ${formatBytes(file.size)}`;
  if (!$("#displayName").value) {
    $("#displayName").value = file.name.replace(/\.[^.]+$/, "");
  }
}

function createOptions() {
  return {
    ocr_mode: $("#ocrMode").value,
    bounded_content: $("#boundedContent").checked,
    allow_llm: $("#allowLlm").checked,
    ffmpeg_dir: $("#ffmpegDir").value.trim(),
  };
}

function uploadTask(taskId, file, displayName, startAfter) {
  return new Promise((resolve, reject) => {
    const params = new URLSearchParams({
      task_id: taskId,
      filename: file.name,
      display_name: displayName,
      ocr_mode: $("#ocrMode").value,
      bounded_content: String($("#boundedContent").checked),
      allow_llm: String($("#allowLlm").checked),
      ffmpeg_dir: $("#ffmpegDir").value.trim(),
    });
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/tasks/upload?${params}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      $("#createSubmit").innerHTML = `<span class="loading-ring"></span> 上传 ${percent}%`;
    };
    xhr.onerror = () => reject(new Error("网络连接中断，视频上传失败。"));
    xhr.onload = () => {
      let payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch { /* handled below */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new Error(payload?.error?.message || `上传失败（${xhr.status}）`));
    };
    state.uploadRequest = xhr;
    xhr.send(file);
  });
}

async function submitCreate(event) {
  event.preventDefault();
  if (state.creating) return;
  const taskId = $("#taskId").value.trim();
  const displayName = $("#displayName").value.trim();
  const startAfter = $("#startAfterCreate").checked;
  const options = createOptions();
  const submit = $("#createSubmit");
  const progress = $("#uploadProgress");
  if (state.createMode === "upload" && !state.file) {
    toast("请选择视频", "点击上传区域选择本地视频后再创建任务。", "error");
    return;
  }
  if (state.createMode === "path" && !$("#sourcePath").value.trim()) {
    toast("请输入视频路径", "需要填写当前电脑上可访问的视频文件路径。", "error");
    $("#sourcePath").focus();
    return;
  }
  state.creating = true;
  submit.disabled = true;
  submit.innerHTML = `<span class="loading-ring"></span> 正在创建`;
  progress.hidden = false;
  progress.firstElementChild.style.width = state.createMode === "upload" ? "2%" : "58%";
  try {
    let task;
    if (state.createMode === "upload") {
      task = await uploadTask(taskId, state.file, displayName, startAfter);
      progress.firstElementChild.style.width = "100%";
    } else {
      task = await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          task_id: taskId,
          source_video: $("#sourcePath").value.trim(),
          display_name: displayName,
          options,
        }),
      });
    }
    closeDialog("createDialog");
    toast("任务已创建", `${task.task_id} 已保存到本机任务目录。`);
    if (startAfter) {
      await api(`/api/tasks/${encodeURIComponent(task.task_id)}/run`, {
        method: "POST",
        body: JSON.stringify({ allow_llm: options.allow_llm }),
      });
      toast("开始生成", "页面将展示实时处理阶段。");
    }
    refreshDashboard();
    if (state.view === "tasks") await loadTasks();
    await openTask(task.task_id);
  } catch (error) {
    toast("创建任务失败", error.message, "error", 6000);
  } finally {
    state.creating = false;
    submit.disabled = false;
    submit.textContent = "创建任务";
    progress.hidden = true;
    progress.firstElementChild.style.width = "0%";
  }
}

function bindEvents() {
  document.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-view]");
    if (nav) {
      const target = nav.dataset.view;
      if (target === "tasks") showTasks();
      else if (target === "dashboard") showDashboard();
      return;
    }
    const create = event.target.closest("[data-open-create]");
    if (create) { openCreateDialog(); return; }
    const run = event.target.closest("[data-run-task]");
    if (run) { event.stopPropagation(); startTask(run.dataset.runTask); return; }
    const runWithLlm = event.target.closest("[data-run-with-llm]");
    if (runWithLlm) {
      event.stopPropagation();
      if (window.confirm("授权本次模型调用可能产生 API 费用，确认继续吗？")) {
        startTask(runWithLlm.dataset.runWithLlm, { allow_llm: true });
      }
      return;
    }
    const cancel = event.target.closest("[data-cancel-task]");
    if (cancel) { event.stopPropagation(); cancelTask(cancel.dataset.cancelTask); return; }
    const archive = event.target.closest("[data-archive-task]");
    if (archive) { event.stopPropagation(); toggleArchive(archive.dataset.archiveTask); return; }
    const open = event.target.closest("[data-open-task]");
    if (open) {
      openTask(open.dataset.openTask);
      return;
    }
    const close = event.target.closest("[data-close-dialog]");
    if (close) { closeDialog(close.dataset.closeDialog); return; }
    if (event.target.matches("dialog")) { event.target.close(); return; }
    const tab = event.target.closest("[data-source-tab]");
    if (tab) { setSourceMode(tab.dataset.sourceTab); return; }
    const edit = event.target.closest("[data-start-edit]");
    if (edit) { startEditing(); return; }
    const existingDocument = event.target.closest("[data-open-existing-document]");
    if (existingDocument) { loadDocument(state.currentTaskId); return; }
    const cancelEdit = event.target.closest("[data-cancel-edit]");
    if (cancelEdit) { cancelEditing(); return; }
    const save = event.target.closest("[data-save-edit]");
    if (save) { saveEdit(); return; }
    const toggleDownload = event.target.closest("[data-toggle-download]");
    if (toggleDownload) { event.stopPropagation(); $("#downloadMenu")?.classList.toggle("open"); return; }
    const exportFormat = event.target.closest("[data-export-format]");
    if (exportFormat) { downloadDocument(exportFormat.dataset.exportFormat); return; }
    const toc = event.target.closest("[data-scroll-section]");
    if (toc) {
      document.getElementById(`section-${toc.dataset.scrollSection}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (event.target.closest("[data-retry-tasks]")) { loadTasks(); return; }
    if (event.target.closest("[data-clear-task-search]")) {
      state.search = "";
      $("#taskSearch").value = "";
      loadTasks();
      return;
    }
    if (!event.target.closest(".download-menu") && !event.target.closest("[data-toggle-download]")) {
      $("#downloadMenu")?.classList.remove("open");
    }
  });

  $$("[data-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      $$("[data-filter]").forEach((item) => item.classList.toggle("active", item === button));
      renderTaskList();
    });
  });

  $("#taskSearch").addEventListener("input", (event) => {
    state.search = event.target.value.trim();
    window.clearTimeout(window.__taskSearchTimer);
    window.__taskSearchTimer = window.setTimeout(loadTasks, 220);
  });
  $("#showArchived").addEventListener("change", (event) => {
    state.showArchived = event.target.checked;
    loadTasks();
  });
  $("#globalSearch").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    state.search = event.target.value.trim();
    $("#taskSearch").value = state.search;
    showTasks();
  });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("#globalSearch").focus();
    }
    if (event.key === "Escape") {
      $("#downloadMenu")?.classList.remove("open");
      $("#sidebar").classList.remove("open");
    }
  });

  $("#topCreateButton").addEventListener("click", openCreateDialog);
  $("#mobileMenu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#helpButton").addEventListener("click", () => $("#helpDialog").showModal());
  $("#systemState").addEventListener("click", () => {
    const system = state.overview?.system;
    if (!system) return refreshDashboard();
    toast("本机运行环境", `Python ${system.python} · ${system.ffmpeg ? "FFmpeg 已就绪" : "FFmpeg 缺失"} · ${system.output_root}`, system.ffmpeg ? "success" : "error", 6000);
  });

  $("#createForm").addEventListener("submit", submitCreate);
  $("#videoFile").addEventListener("change", (event) => chooseFile(event.target.files[0]));
  const dropZone = $("#dropZone");
  ["dragenter", "dragover"].forEach((type) => dropZone.addEventListener(type, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((type) => dropZone.addEventListener(type, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  }));
  dropZone.addEventListener("drop", (event) => chooseFile(event.dataTransfer.files[0]));

  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-edit-field]")) handleEditorInput(event.target);
    if (event.target.id === "versionName") state.editingDirty = true;
  });
  document.addEventListener("change", (event) => {
    if (event.target.id === "versionSelect") {
      loadDocument(state.currentTaskId, event.target.value);
      return;
    }
    if (event.target.matches("[data-edit-frame]") && state.draft) {
      const chapter = state.draft.chapters[Number(event.target.dataset.chapter)];
      const frame = chapter.frames[Number(event.target.dataset.frameIndex)];
      const current = new Set(chapter.selected_frame_ids || []);
      if (event.target.checked) current.add(frame.frame_id);
      else current.delete(frame.frame_id);
      chapter.selected_frame_ids = chapter.frames.map((item) => item.frame_id).filter((id) => current.has(id));
      event.target.closest(".editor-frame")?.classList.toggle("removed", !event.target.checked);
      state.editingDirty = true;
      updateEditorPreview();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.editingDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function init() {
  bindEvents();
  const hash = location.hash;
  if (hash.startsWith("#task=")) {
    const taskId = decodeURIComponent(hash.slice(6));
    openTask(taskId);
  } else if (hash === "#tasks") {
    showTasks();
  } else {
    showDashboard();
  }
  window.setInterval(() => {
    if (document.hidden) return;
    if (state.view === "dashboard" || state.view === "tasks") {
      refreshDashboard();
      if (state.view === "tasks") loadTasks();
    }
  }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
