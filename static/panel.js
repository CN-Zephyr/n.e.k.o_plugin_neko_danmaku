"use strict";

const PLUGIN_ID = "neko_danmaku";
const RUNS_URL = "/runs";
const ENTRY_POLL_MS = 280;
const DEFAULT_ENTRY_TIMEOUT_MS = 9000;
const STATUS_REFRESH_MS = 4000;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

let state = {
  overlay: {},
  windows: [],
  processes: [],
  running: false,
  status: {},
};
let curPreset = "clean";
let refreshPromise = null;
let formDirty = false;
let styleLoaded = false;
let lastStatusAt = 0;
const entryDiagnostics = [];

const PAGE_META = {
  overview: ["弹幕总览", "宿主聊天正在这里变成画面上流动的陪伴。"],
  target: ["弹幕目标", "选择弹幕要出现的显示器、游戏或窗口。"],
  appearance: ["外观与排版", "用实时预览调整字体、颜色、节奏和屏幕布局。"],
  behavior: ["聊天来源", "控制弹幕读取哪一条会话通道，以及进入画面的节奏。"],
  diagnostics: ["诊断与工具", "检查浮层链路、目标状态并运行真实自检。"],
};

const FONTS = [
  "Microsoft YaHei UI",
  "微软雅黑",
  "Segoe UI",
  "SimHei",
  "黑体",
  "SimSun",
  "宋体",
  "KaiTi",
  "楷体",
  "LiSu",
  "隶书",
  "DengXian",
  "等线",
  "Arial",
];

const PRESETS = {
  clean: { label: "干净", color: "#ffffff", swatch: "#ffffff" },
  master: { label: "主人", color: "#fff6e5", swatch: "#fff6e5" },
  neko: { label: "YUI", color: "#9adcff", swatch: "#9adcff" },
  champagne: { label: "香槟", color: "#ffd78c", swatch: "#ffd78c" },
  softbar: { label: "柔和底", color: "#ffffff", swatch: "#a9bad2" },
  pink: { label: "粉色", color: "#ff9ad5", swatch: "#ff9ad5" },
  mix: {
    label: "彩色",
    color: "#ff9ad5",
    swatch: "linear-gradient(135deg,#9adcff,#ff9ad5,#ffd78c)",
  },
};

const RANGE_IDS = [
  "font_size",
  "stroke_width",
  "shadow_blur",
  "shadow_dy",
  "shadow_alpha",
  "glow_blur",
  "bg_alpha",
  "bg_radius",
  "opacity",
  "speed",
  "area_offset",
  "max_items",
  "fade_in",
  "max_lines",
  "line_spacing",
  "letter_spacing",
  "sdkpoll",
];

const STYLE_INPUT_IDS = [
  "s-font",
  "p-font_size",
  "p-bold",
  "p-text_color",
  "p-stroke_color",
  "p-stroke_width",
  "p-grad_on",
  "p-grad_top",
  "p-grad_bot",
  "p-shadow_on",
  "p-shadow_blur",
  "p-shadow_dy",
  "p-shadow_alpha",
  "p-shadow_color",
  "p-glow_on",
  "p-glow_blur",
  "p-glow_color",
  "p-bg",
  "p-bg_color",
  "p-bg_alpha",
  "p-bg_radius",
  "p-area",
  "p-area_offset",
  "p-max_items",
  "p-fade_in",
  "p-wrap",
  "p-max_lines",
  "p-auto_scale",
  "p-line_spacing",
  "p-letter_spacing",
  "p-opacity",
  "p-speed",
  "prevlabel",
];

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function showToast(message, isError = false) {
  const box = $("#toastBox");
  if (!box) return;
  const toast = document.createElement("div");
  toast.className = `toast${isError ? " is-error" : ""}`;
  toast.innerHTML = `<span class="toast-mark">${isError ? "!" : "✓"}</span><span class="toast-text">${escapeHtml(message)}</span>`;
  box.appendChild(toast);
  requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add("is-show")));
  setTimeout(() => {
    toast.classList.remove("is-show");
    setTimeout(() => toast.remove(), 240);
  }, 2800);
}

function setDiagnostic(message) {
  const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  entryDiagnostics.push(`[${stamp}] ${message}`);
  if (entryDiagnostics.length > 18) entryDiagnostics.shift();
  const raw = $("#diag");
  if (raw) raw.textContent = entryDiagnostics.join("\n");
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = Boolean(busy);
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

function applyTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  $$("[data-theme-choice]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.themeChoice === next ? "true" : "false");
  });
  try {
    localStorage.setItem("neko-danmaku-theme", next);
  } catch (_) {}
  drawPreview();
}

function initTheme() {
  const forced = new URLSearchParams(location.search).get("theme");
  applyTheme(forced || document.documentElement.dataset.theme || "light");
  $$("[data-theme-choice]").forEach((button) => {
    button.addEventListener("click", () => applyTheme(button.dataset.themeChoice));
  });
}

function goPage(name, updateHash = true) {
  if (!PAGE_META[name]) name = "overview";
  $$(".nav-item[data-page]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.page === name);
    button.setAttribute("aria-current", button.dataset.page === name ? "page" : "false");
  });
  $$("[data-page-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.pagePanel === name);
  });
  const meta = PAGE_META[name];
  $("#pageTitle").textContent = meta[0];
  $("#pageSubtitle").textContent = meta[1];
  if (updateHash && location.hash !== `#${name}`) {
    history.replaceState(null, "", `#${name}`);
  }
  if (name === "appearance") {
    requestAnimationFrame(drawPreview);
  }
}

function initNavigation() {
  $$(".nav-item[data-page]").forEach((button) => {
    button.addEventListener("click", () => goPage(button.dataset.page));
  });
  $$("[data-jump]").forEach((button) => {
    button.addEventListener("click", () => goPage(button.dataset.jump));
  });
  const initial = location.hash.replace(/^#/, "");
  goPage(PAGE_META[initial] ? initial : "overview", false);
  window.addEventListener("hashchange", () => {
    const name = location.hash.replace(/^#/, "");
    goPage(PAGE_META[name] ? name : "overview", false);
  });
}

async function fetchJson(url, options = {}, timeoutMs = DEFAULT_ENTRY_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function callEntry(entryId, args = {}, timeoutMs = DEFAULT_ENTRY_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  try {
    setDiagnostic(`调用 ${entryId}`);
    const run = await fetchJson(RUNS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plugin_id: PLUGIN_ID, entry_id: entryId, args }),
    }, Math.min(5000, timeoutMs));
    if (!run.run_id) throw new Error("宿主未返回 run_id");

    while (Date.now() < deadline) {
      await delay(ENTRY_POLL_MS);
      const record = await fetchJson(`${RUNS_URL}/${run.run_id}`, {}, 3500);
      if (record.status === "succeeded") {
        const exported = await fetchJson(`${RUNS_URL}/${run.run_id}/export`, {}, 5000);
        const items = exported.items || [];
        const trigger = items.find((item) => item.label === "trigger_response")
          || items.find((item) => item.type === "json" && item.json)
          || items[0]
          || {};
        const jsonData = trigger.json || {};
        const pluginResponse = jsonData.plugin_response || jsonData;
        if (pluginResponse && typeof pluginResponse === "object" && pluginResponse.success !== undefined) {
          let data = pluginResponse.data;
          while (data && typeof data === "object" && !Array.isArray(data)
            && Object.keys(data).length === 1 && data.value !== undefined) {
            data = data.value;
          }
          return data || {};
        }
        return pluginResponse || {};
      }
      if (record.status === "failed" || record.status === "cancelled") {
        const message = record.error?.message || record.message || record.status;
        throw new Error(message);
      }
    }
    throw new Error("请求超时");
  } catch (error) {
    const message = error?.name === "AbortError" ? "请求超时" : String(error?.message || error);
    setDiagnostic(`${entryId} 失败：${message}`);
    return { ok: false, error: message };
  }
}

function setConnection(connected, status = {}) {
  const dot = $("#dot");
  dot.className = `status-dot ${connected ? "on" : "off"}`;
  const livePill = $("#livePill");
  livePill.classList.toggle("is-on", connected);
  livePill.classList.toggle("is-off", !connected);
  $("b", livePill).textContent = connected ? "浮层在线" : "浮层离线";

  const source = status.effective_source || status.data_source || "—";
  const target = state.overlay?.target;
  const targetText = target ? findTargetLabel(target).title : "未绑定目标";
  $("#connectionTitle").textContent = connected ? "弹幕浮层已连接" : "弹幕浮层未连接";
  $("#statustext").textContent = connected
    ? `${source} · ${targetText}`
    : (status.error || "请确认插件和浮层已经启动");
  $("#statustext").title = $("#statustext").textContent;

  const notice = $("#inlineNotice");
  notice.hidden = connected;
  if (!connected) {
    $("#noticeTitle").textContent = "浮层暂未连接";
    $("#noticeText").textContent = status.error || "插件可能仍在启动，请稍候重试。";
  }

  $("#livePill b").textContent = connected ? "浮层在线" : "浮层离线";
  $("#sidebarOverlay").textContent = connected ? "在线" : "离线";
  $("#sidebarSource").textContent = source;
}

function normalizeStatus(status) {
  const overlay = status?.overlay && typeof status.overlay === "object" ? status.overlay : {};
  return {
    status: status || {},
    running: Boolean(status?.running),
    windows: Array.isArray(status?.windows) ? status.windows : [],
    processes: Array.isArray(status?.processes) && status.processes.length
      ? status.processes
      : (Array.isArray(status?.windows) ? status.windows : []),
    overlay,
  };
}

async function refresh({ forceStyle = false } = {}) {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const status = await callEntry("get_panel_state", {}, 4200);
    if (!status || status.connected === false || status.ok === false) {
      state.running = false;
      state.overlay = {};
      state.windows = [];
      state.processes = [];
      state.status = status || {};
      setConnection(false, status || {});
      renderState();
      setDiagnostic(`状态读取失败：${status?.error || "无有效数据"}`);
      return status;
    }
    Object.assign(state, normalizeStatus(status));
    lastStatusAt = Date.now();
    setConnection(true, status);
    renderState();
    if (!formDirty && (!styleLoaded || forceStyle) && state.overlay.style) {
      loadStyleToUI(state.overlay.style);
      styleLoaded = true;
    }
    setDiagnostic(`状态正常：source=${status.effective_source || status.data_source || "?"} windows=${state.processes.length}`);
    return status;
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

function setTone(element, tone) {
  if (!element) return;
  element.classList.remove("is-good", "is-warn", "is-bad");
  if (tone) element.classList.add(`is-${tone}`);
}

function renderState() {
  const status = state.status || {};
  const overlay = state.overlay || {};
  const source = status.effective_source || status.data_source || "—";
  const target = overlay.target || null;
  const targetLabel = findTargetLabel(target);
  const displayCount = (overlay.displays || []).length;
  const windowsCount = state.processes.length;

  $("#statOverlay").textContent = state.running ? "运行中" : "未连接";
  setTone($("#statOverlayTone"), state.running ? "good" : "bad");
  $("#statSource").textContent = source === "sdk" ? "SDK 完整聊天" : source;
  setTone($("#statSourceTone"), source === "sdk" ? "good" : "warn");
  $("#statTarget").textContent = target ? targetLabel.title : "未绑定";
  setTone($("#statTargetTone"), target ? (overlay.obscured ? "warn" : "good") : "warn");
  $("#statWindows").textContent = String(windowsCount);
  setTone($("#statWindowsTone"), windowsCount ? "good" : "warn");

  $("#overviewTargetIcon").textContent = targetLabel.icon;
  $("#overviewTargetTitle").textContent = targetLabel.title;
  $("#overviewTargetSub").textContent = targetLabel.sub;
  $("#overviewSource").textContent = source;
  $("#overviewInterval").textContent = status.sdk_interval_s ? `${Number(status.sdk_interval_s).toFixed(2)}s` : "—";
  $("#overviewObscured").textContent = !target ? "无目标" : overlay.obscured ? "被遮挡" : "正常";
  $("#navTargetBadge").textContent = target ? targetLabel.title : "未绑定";
  $("#navTargetBadge").title = targetLabel.title;

  const sourceControl = $("#g-source");
  if (sourceControl && document.activeElement !== sourceControl && status.data_source) {
    sourceControl.value = status.data_source;
  }
  const intervalControl = $("#g-sdkpoll");
  if (intervalControl && document.activeElement !== intervalControl && Number.isFinite(Number(status.sdk_interval_s))) {
    intervalControl.value = Math.round(Number(status.sdk_interval_s) * 100);
    $("#v-sdkpoll").textContent = `${Number(status.sdk_interval_s).toFixed(2)}s`;
  }
  const roles = status.role_preset || {};
  for (const role of ["human", "ai"]) {
    const select = $(`#r-${role}`);
    if (select && document.activeElement !== select && roles[role]) {
      select.value = roles[role];
    }
  }

  $("#diagOverlay").textContent = state.running ? "在线" : "离线";
  $("#diagOverlay").className = state.running ? "is-good" : "is-bad";
  $("#diagSource").textContent = `${status.data_source || "—"} → ${source}`;
  $("#diagTarget").textContent = targetLabel.title;
  $("#diagItems").textContent = overlay.items == null ? "—" : `${overlay.entered || 0} / ${overlay.items}`;
  $("#diagDropped").textContent = overlay.dropped == null ? "0" : String(overlay.dropped);

  renderTargetBar(target, overlay);
  renderDisplays();
  renderWindows();
}

function findTargetLabel(target) {
  if (!target) {
    return {
      title: "还没有选择弹幕目标",
      sub: "选择一个显示器或游戏窗口后开始投送。",
      icon: "◎",
    };
  }
  const all = state.processes.length ? state.processes : state.windows;
  if (target.kind === "display") {
    const display = (state.overlay.displays || []).find((item) => item.id === target.id);
    return {
      title: display?.name || `显示器 ${Number(target.id) + 1}`,
      sub: display ? `${display.w}×${display.h} · 铺满该显示器` : "铺满所选显示器",
      icon: "▣",
    };
  }
  if (target.kind === "screen") {
    return { title: "主显示器", sub: "浮层覆盖整个主屏", icon: "▣" };
  }
  if (target.kind === "process") {
    const proc = all.find((item) => item.pid === target.pid);
    const size = proc?.client ? `${proc.client[2]}×${proc.client[3]}` : proc ? `${proc.w}×${proc.h}` : "";
    return {
      title: proc?.title || `进程 ${target.pid}`,
      sub: `${proc?.exe || "未知进程"}${size ? ` · ${size}` : ""} · 自动跟随窗口`,
      icon: proc?.fullscreen ? "▣" : proc?.kind === "game" ? "◇" : "□",
    };
  }
  if (target.kind === "window") {
    const proc = all.find((item) => item.hwnd === target.hwnd);
    const size = proc?.client ? `${proc.client[2]}×${proc.client[3]}` : proc ? `${proc.w}×${proc.h}` : "";
    return {
      title: proc?.title || `窗口 ${target.hwnd}`,
      sub: `${proc?.exe || "未知窗口"}${size ? ` · ${size}` : ""} · 跟随窗口`,
      icon: "□",
    };
  }
  return { title: "已选目标", sub: "", icon: "◎" };
}

function renderTargetBar(target, overlay) {
  const info = findTargetLabel(target);
  $("#tbIcon").textContent = info.icon;
  $("#tbTitle").textContent = info.title;
  $("#tbSub").textContent = overlay.obscured && target ? "目标被其它窗口遮挡，弹幕已暂停。" : info.sub;
  $("#targetBar").classList.toggle("is-empty", !target);
}

function renderDisplays() {
  const container = $("#displays");
  const displays = state.overlay.displays || [];
  $("#displayBadge").textContent = `${displays.length} 个`;
  if (!displays.length) {
    container.innerHTML = '<div class="empty-state">暂无显示器信息<br>确认弹幕浮层已经启动</div>';
    return;
  }
  const target = state.overlay.target;
  container.innerHTML = displays.map((display) => {
    const selected = target?.kind === "display" && target.id === display.id;
    const name = display.name || `显示器 ${Number(display.id) + 1}`;
    return `<button class="display-card${selected ? " is-selected" : ""}" type="button" data-display-id="${Number(display.id)}">
      <span class="display-visual">${display.primary ? "P" : "▣"}</span>
      <div><strong>${escapeHtml(name)}</strong><span>${Number(display.w)} × ${Number(display.h)}</span></div>
    </button>`;
  }).join("");
}

function renderWindows() {
  const container = $("#windows");
  const processes = state.processes || [];
  const query = ($("#windowSearch")?.value || "").trim().toLowerCase();
  $("#winbadge").textContent = `${processes.length} 个`;
  if (!processes.length) {
    container.innerHTML = '<div class="empty-state">未发现窗口或进程<br>浮层可能仍在启动</div>';
    return;
  }
  const target = state.overlay.target;
  const groups = [
    ["fullscreen", "全屏游戏"],
    ["game", "游戏窗口"],
    ["normal", "其它窗口"],
  ];
  const html = [];
  for (const [kind, label] of groups) {
    const items = processes.filter((proc) => {
      if (proc.kind !== kind) return false;
      if (!query) return true;
      return `${proc.title || ""} ${proc.exe || ""} ${proc.pid || ""}`.toLowerCase().includes(query);
    });
    if (!items.length) continue;
    html.push(`<div class="window-group-label">${label} · ${items.length}</div>`);
    for (const proc of items) {
      const selected = (target?.kind === "process" && target.pid === proc.pid)
        || (target?.kind === "window" && target.hwnd === proc.hwnd);
      const width = proc.client?.[2] ?? proc.w;
      const height = proc.client?.[3] ?? proc.h;
      const icon = proc.fullscreen ? "▣" : proc.kind === "game" ? "◇" : "□";
      html.push(`<button class="window-item${selected ? " is-selected" : ""}" type="button" data-process-id="${Number(proc.pid)}">
        <span class="window-icon">${icon}</span>
        <span class="window-copy"><strong>${escapeHtml(proc.title || "未命名窗口")}</strong><small>${escapeHtml(proc.exe || "未知进程")} · ${Number(width) || 0}×${Number(height) || 0}</small></span>
        ${proc.foreground ? '<span class="window-tag">当前</span>' : ""}
      </button>`);
    }
  }
  container.innerHTML = html.length ? html.join("") : '<div class="empty-state">没有匹配的窗口</div>';
}

function markDirty() {
  formDirty = true;
  const hint = $("#dirtyHint");
  hint.textContent = "外观已修改，点击「保存外观」同步到浮层";
  hint.classList.add("is-dirty");
}

function clearDirty() {
  formDirty = false;
  const hint = $("#dirtyHint");
  hint.textContent = "外观已与浮层同步";
  hint.classList.remove("is-dirty");
}

function syncToggles() {
  $("#grad-box").hidden = !$("#p-grad_on").checked;
  $("#shadow-box").hidden = !$("#p-shadow_on").checked;
  $("#glow-box").hidden = !$("#p-glow_on").checked;
  $("#bg-box").hidden = $("#p-bg").value === "none";
}

function rangeLabel(id, value) {
  if (id === "opacity") return `${value}%`;
  if (id === "fade_in") return `${value}ms`;
  if (id === "max_lines") return `${value} 行`;
  if (id === "line_spacing") return (Number(value) / 100).toFixed(2);
  if (id === "sdkpoll") return `${(Number(value) / 100).toFixed(2)}s`;
  return String(value);
}

function setRange(id, value) {
  const input = $(`#p-${id}`) || $(`#g-${id}`);
  if (!input) return;
  input.value = value;
  const label = $(`#v-${id}`);
  if (label) label.textContent = rangeLabel(id, value);
}

function bindControls() {
  const font = $("#s-font");
  for (const family of FONTS) {
    const option = document.createElement("option");
    option.value = family;
    option.textContent = family;
    font.appendChild(option);
  }

  for (const id of STYLE_INPUT_IDS) {
    const element = document.getElementById(id);
    if (!element) continue;
    const eventName = element.type === "range" || element.type === "color" ? "input" : "change";
    element.addEventListener(eventName, () => {
      markDirty();
      if (element.type === "range") {
        const key = id.replace(/^p-/, "").replace(/^g-/, "");
        const label = $(`#v-${key}`);
        if (label) label.textContent = rangeLabel(key, element.value);
      }
      syncToggles();
      schedulePreview();
    });
    if (element.tagName === "INPUT" && element.type !== "range" && element.type !== "color") {
      element.addEventListener("input", () => {
        markDirty();
        schedulePreview();
      });
    }
  }

  $("#prevlabel").addEventListener("input", schedulePreview);
  $("#btn-apply").addEventListener("click", () => saveAppearance($("#btn-apply")));
  $("#btn-reset").addEventListener("click", resetAppearance);
  $("#btn-reset-diag").addEventListener("click", resetAppearance);
  $("#applyBehavior").addEventListener("click", () => saveBehavior($("#applyBehavior")));
  $("#selftestBehavior").addEventListener("click", () => runSelfTest($("#selftestBehavior")));
  $("#g-source").addEventListener("change", () => applySource());
  $("#g-sdkpoll").addEventListener("change", () => applySource());
  $("#r-human").addEventListener("change", () => applyRoleStyle());
  $("#r-ai").addEventListener("change", () => applyRoleStyle());
}

function collectStyle() {
  const style = {
    preset: curPreset,
    font_family: $("#s-font").value,
    font_size: Number($("#p-font_size").value),
    bold: $("#p-bold").checked,
    text_color: $("#p-text_color").value,
    stroke_color: $("#p-stroke_color").value,
    stroke_width: Number($("#p-stroke_width").value),
    bg: $("#p-bg").value,
    opacity: Number($("#p-opacity").value) / 100,
    speed: Number($("#p-speed").value),
    area: $("#p-area").value,
    area_offset: Number($("#p-area_offset").value),
    max_items: Number($("#p-max_items").value),
    fade_in_ms: Number($("#p-fade_in").value),
    wrap: $("#p-wrap").checked,
    max_lines: Number($("#p-max_lines").value),
    auto_scale: $("#p-auto_scale").checked,
    line_spacing: Number($("#p-line_spacing").value) / 100,
    letter_spacing: Number($("#p-letter_spacing").value),
  };
  style.fill_gradient = $("#p-grad_on").checked
    ? [$("#p-grad_top").value, $("#p-grad_bot").value]
    : null;
  style.shadow = $("#p-shadow_on").checked;
  if (style.shadow) {
    style.shadow_blur = Number($("#p-shadow_blur").value);
    style.shadow_dy = Number($("#p-shadow_dy").value);
    style.shadow_alpha = Number($("#p-shadow_alpha").value);
    style.shadow_color = $("#p-shadow_color").value;
  }
  style.glow = $("#p-glow_on").checked;
  if (style.glow) {
    style.glow_blur = Number($("#p-glow_blur").value);
    style.glow_color = $("#p-glow_color").value;
  }
  if (style.bg !== "none") {
    style.bg_color = $("#p-bg_color").value;
    style.bg_alpha = Number($("#p-bg_alpha").value);
    style.bg_radius = Number($("#p-bg_radius").value);
  }
  return style;
}

function fillFromPreset(name) {
  const next = PRESETS[name] ? name : "clean";
  curPreset = next;
  renderPresets();
  const map = {
    clean: { color: "#ffffff", stroke: "#101018", width: 3, bg: "none", size: 34, shadow: false },
    master: { color: "#fff6e5", stroke: "#20242c", width: 3, bg: "none", size: 34, shadow: true },
    neko: { color: "#9adcff", stroke: "#08243a", width: 3, bg: "light", size: 34, shadow: true },
    champagne: { color: "#ffd78c", stroke: "#4a2a08", width: 3, bg: "none", size: 36, shadow: true, glow: true },
    softbar: { color: "#ffffff", stroke: "#101018", width: 2.5, bg: "soft", size: 34, shadow: false },
    pink: { color: "#ff9ad5", stroke: "#3a0a22", width: 3, bg: "none", size: 34, shadow: true },
    mix: { color: "#ffffff", stroke: "#101018", width: 3, bg: "light", size: 34, shadow: true },
  };
  const preset = map[next];
  $("#p-text_color").value = preset.color;
  $("#p-stroke_color").value = preset.stroke;
  setRange("stroke_width", preset.width);
  setRange("font_size", preset.size);
  $("#p-bg").value = preset.bg;
  $("#p-bold").checked = true;
  $("#p-shadow_on").checked = preset.shadow;
  $("#p-glow_on").checked = Boolean(preset.glow);
  syncToggles();
  markDirty();
  drawPreview();
}

function loadStyleToUI(style) {
  if (!style) return;
  if (style.font_size != null) setRange("font_size", style.font_size);
  if (style.font_family) {
    const font = $("#s-font");
    if (Array.from(font.options).some((option) => option.value === style.font_family)) {
      font.value = style.font_family;
    }
  }
  if (style.bold != null) $("#p-bold").checked = Boolean(style.bold);
  if (style.text_color) $("#p-text_color").value = style.text_color;
  if (style.stroke_color) $("#p-stroke_color").value = style.stroke_color;
  if (style.stroke_width != null) setRange("stroke_width", style.stroke_width);
  if (style.bg) $("#p-bg").value = style.bg;
  $("#p-grad_on").checked = Boolean(style.fill_gradient);
  if (Array.isArray(style.fill_gradient) && style.fill_gradient.length >= 2) {
    $("#p-grad_top").value = style.fill_gradient[0];
    $("#p-grad_bot").value = style.fill_gradient[1];
  }
  $("#p-shadow_on").checked = Boolean(style.shadow);
  for (const key of ["shadow_blur", "shadow_dy", "shadow_alpha"]) {
    if (style[key] != null) setRange(key, style[key]);
  }
  if (style.shadow_color) $("#p-shadow_color").value = style.shadow_color;
  $("#p-glow_on").checked = Boolean(style.glow);
  if (style.glow_blur != null) setRange("glow_blur", style.glow_blur);
  if (style.glow_color) $("#p-glow_color").value = style.glow_color;
  if (style.bg_color) $("#p-bg_color").value = style.bg_color;
  if (style.bg_alpha != null) setRange("bg_alpha", style.bg_alpha);
  if (style.bg_radius != null) setRange("bg_radius", style.bg_radius);
  if (style.opacity != null) setRange("opacity", Math.round(Number(style.opacity) * 100));
  if (style.speed != null) setRange("speed", style.speed);
  if (style.max_items != null) setRange("max_items", style.max_items);
  if (style.fade_in_ms != null) setRange("fade_in", style.fade_in_ms);
  if (style.max_lines != null) setRange("max_lines", style.max_lines);
  if (style.line_spacing != null) setRange("line_spacing", Math.round(Number(style.line_spacing) * 100));
  if (style.letter_spacing != null) setRange("letter_spacing", style.letter_spacing);
  if (style.wrap != null) $("#p-wrap").checked = Boolean(style.wrap);
  if (style.auto_scale != null) $("#p-auto_scale").checked = Boolean(style.auto_scale);
  if (style.area) $("#p-area").value = style.area;
  if (style.area_offset != null) setRange("area_offset", style.area_offset);
  syncToggles();
  clearDirty();
  schedulePreview();
}

function renderPresets() {
  const container = $("#presets");
  container.innerHTML = Object.entries(PRESETS).map(([key, preset]) => `
    <button class="preset-card${key === curPreset ? " is-active" : ""}" type="button" data-preset="${key}">
      <i class="preset-swatch" style="background:${preset.swatch}"></i>
      <span>${preset.label}</span>
    </button>
  `).join("");
  $$("[data-preset]", container).forEach((button) => {
    button.addEventListener("click", () => fillFromPreset(button.dataset.preset));
  });
}

let previewFrame = 0;
function schedulePreview() {
  cancelAnimationFrame(previewFrame);
  previewFrame = requestAnimationFrame(drawPreview);
}

function canvasContext(canvas, height) {
  if (!canvas) return null;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width || canvas.clientWidth || 640);
  const actualHeight = Math.max(1, height || rect.height || canvas.clientHeight || 210);
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(actualHeight * dpr);
  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, actualHeight);
  return { context, width, height: actualHeight };
}

function hexAlpha(hex, alpha) {
  const clean = /^#[0-9a-f]{6}$/i.test(String(hex)) ? hex : "#000000";
  const red = parseInt(clean.slice(1, 3), 16);
  const green = parseInt(clean.slice(3, 5), 16);
  const blue = parseInt(clean.slice(5, 7), 16);
  return `rgba(${red},${green},${blue},${Math.max(0, Math.min(1, alpha))})`;
}

function wrapPreviewLines(context, text, limit, maxLines) {
  const lines = [];
  let buffer = "";
  for (const char of text) {
    if (buffer && context.measureText(buffer + char).width > limit) {
      lines.push(buffer);
      buffer = char;
    } else {
      buffer += char;
    }
  }
  if (buffer) lines.push(buffer);
  if (lines.length > maxLines) {
    const clipped = lines.slice(0, maxLines);
    let tail = clipped[clipped.length - 1];
    while (tail && context.measureText(`${tail}…`).width > limit) tail = tail.slice(0, -1);
    clipped[clipped.length - 1] = `${tail}…`;
    return clipped;
  }
  return lines.length ? lines : [text];
}

function drawPreviewCanvas(canvas, compact = false) {
  const render = canvasContext(canvas);
  if (!render) return;
  const { context, width, height } = render;
  const text = ($("#prevlabel")?.value || "会吐弹幕的蓝色大肥鱼喵～").trim();
  const fontSize = Math.max(14, Number($("#p-font_size").value) * (compact ? 0.82 : 1));
  const bold = $("#p-bold").checked;
  const textColor = $("#p-text_color").value;
  const strokeColor = $("#p-stroke_color").value;
  const strokeWidth = Number($("#p-stroke_width").value);
  const gradientOn = $("#p-grad_on").checked;
  const shadowOn = $("#p-shadow_on").checked;
  const glowOn = $("#p-glow_on").checked;
  const opacity = Number($("#p-opacity").value) / 100;
  const wrapOn = $("#p-wrap").checked;
  const maxLines = Number($("#p-max_lines").value);
  const spacing = Number($("#p-line_spacing").value) / 100;

  context.save();
  context.font = `${bold ? 700 : 400} ${fontSize}px "${$("#s-font").value}", "Microsoft YaHei UI", sans-serif`;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.globalAlpha = Math.max(0.05, Math.min(1, opacity));

  const limit = width * (compact ? 0.72 : 0.78);
  const lines = wrapOn ? wrapPreviewLines(context, text, limit, maxLines) : [text];
  const lineHeight = fontSize * spacing * 1.18;
  const blockHeight = lineHeight * lines.length;
  const startY = height / 2 - blockHeight / 2 + fontSize * 0.82;
  const widths = lines.map((line) => context.measureText(line).width);
  const blockWidth = Math.max(...widths, fontSize * 2);
  const startX = compact ? 24 : Math.max(22, width * 0.12);
  const bg = $("#p-bg").value;

  const drawLines = (callback) => lines.forEach((line, index) => {
    callback(line, startX, startY + index * lineHeight);
  });

  if (bg !== "none") {
    const alpha = bg === "light" ? 46 : bg === "soft" ? 100 : Number($("#p-bg_alpha").value);
    const radius = Number($("#p-bg_radius").value);
    const boxX = startX - 16;
    const boxY = startY - fontSize * 0.95;
    const boxW = blockWidth + 32;
    const boxH = blockHeight + 22;
    context.fillStyle = hexAlpha($("#p-bg_color").value, alpha / 255);
    context.beginPath();
    if (context.roundRect) context.roundRect(boxX, boxY, boxW, boxH, radius);
    else context.rect(boxX, boxY, boxW, boxH);
    context.fill();
  }

  if (glowOn) {
    context.strokeStyle = $("#p-glow_color").value;
    context.lineWidth = Math.max(4, strokeWidth * 2.3);
    context.shadowColor = $("#p-glow_color").value;
    context.shadowBlur = Number($("#p-glow_blur").value);
    drawLines((line, x, y) => context.strokeText(line, x, y));
    context.shadowBlur = 0;
  }

  if (shadowOn) {
    context.shadowColor = hexAlpha($("#p-shadow_color").value, Number($("#p-shadow_alpha").value) / 255);
    context.shadowBlur = Number($("#p-shadow_blur").value);
    context.shadowOffsetY = Number($("#p-shadow_dy").value);
    context.fillStyle = textColor;
    drawLines((line, x, y) => context.fillText(line, x, y));
    context.shadowColor = "transparent";
    context.shadowBlur = 0;
    context.shadowOffsetY = 0;
  }

  if (strokeWidth > 0) {
    context.strokeStyle = strokeColor;
    context.lineWidth = strokeWidth * 2;
    drawLines((line, x, y) => context.strokeText(line, x, y));
  }

  if (gradientOn) {
    const gradient = context.createLinearGradient(startX, startY - fontSize, startX, startY + blockHeight);
    gradient.addColorStop(0, $("#p-grad_top").value);
    gradient.addColorStop(1, $("#p-grad_bot").value);
    context.fillStyle = gradient;
  } else {
    context.fillStyle = textColor;
  }
  drawLines((line, x, y) => context.fillText(line, x, y));
  context.restore();
}

function drawPreview() {
  drawPreviewCanvas($("#prev"));
  drawPreviewCanvas($("#heroPrev"), true);
}

async function saveAppearance(button) {
  setBusy(button, true);
  try {
    const result = await callEntry("apply_style", collectStyle(), 9000);
    if (result?.ok === false) throw new Error(result.error || "宿主拒绝保存");
    clearDirty();
    showToast("外观已保存并同步到浮层");
    await refresh({ forceStyle: true });
  } catch (error) {
    showToast(`保存失败：${error.message || error}`, true);
  } finally {
    setBusy(button, false);
  }
}

async function saveBehavior(button) {
  setBusy(button, true);
  try {
    const body = {
      opacity: Number($("#p-opacity").value) / 100,
      speed: Number($("#p-speed").value),
    };
    const result = await callEntry("apply_style", body, 7000);
    if (result?.ok === false) throw new Error(result.error || "宿主拒绝保存");
    showToast("节奏设置已应用");
    await refresh({ forceStyle: true });
  } catch (error) {
    showToast(`应用失败：${error.message || error}`, true);
  } finally {
    setBusy(button, false);
  }
}

async function applySource() {
  const source = $("#g-source").value;
  const interval = Number($("#g-sdkpoll").value) / 100;
  $("#v-sdkpoll").textContent = `${interval.toFixed(2)}s`;
  const result = await callEntry("set_data_source", {
    data_source: source,
    sdk_poll_interval_s: interval,
  }, 7000);
  if (result?.ok === false) {
    showToast(`数据源切换失败：${result.error || "未知错误"}`, true);
    return;
  }
  const effective = result.effective_source && result.effective_source !== source
    ? `，实际回退为 ${result.effective_source}`
    : "";
  showToast(`数据源已切换到 ${source}${effective}`);
  await refresh();
}

async function applyRoleStyle() {
  const human = $("#r-human").value;
  const ai = $("#r-ai").value;
  const result = await callEntry("set_role_style", { human, ai }, 7000);
  if (result?.ok === false) {
    showToast(`角色样式更新失败：${result.error || "未知错误"}`, true);
    return;
  }
  showToast(`角色样式已更新：主人 ${human} / 猫娘 ${ai}`);
  drawPreview();
}

async function pickDisplay(displayId) {
  const display = (state.overlay.displays || []).find((item) => item.id === displayId);
  showToast(`正在绑定 ${display?.name || `显示器 ${displayId + 1}`}…`);
  const result = await callEntry("apply_target", { kind: "display", id: displayId }, 8000);
  if (result?.ok === false) {
    showToast(`绑定失败：${result.error || "未知错误"}`, true);
    return;
  }
  showToast(`已绑定 ${display?.name || `显示器 ${displayId + 1}`}`);
  await refresh();
}

async function pickProcess(pid) {
  const process = state.processes.find((item) => item.pid === pid);
  const name = process?.title || process?.exe || `进程 ${pid}`;
  showToast(`正在绑定「${name}」…`);
  const result = await callEntry("apply_target", { kind: "process", pid, mode: "client" }, 8000);
  if (result?.ok === false) {
    showToast(`绑定失败：${result.error || "未知错误"}`, true);
    return;
  }
  showToast(`已绑定「${name}」`);
  await refresh();
}

async function pickScreen(button) {
  setBusy(button, true);
  try {
    const result = await callEntry("apply_target", { kind: "screen" }, 8000);
    if (result?.ok === false) throw new Error(result.error || "未知错误");
    showToast("浮层已覆盖主显示器");
    await refresh();
  } catch (error) {
    showToast(`操作失败：${error.message || error}`, true);
  } finally {
    setBusy(button, false);
  }
}

async function runSelfTest(button) {
  setBusy(button, true);
  try {
    showToast("正在通过真实链路推送测试弹幕…");
    const result = await callEntry("self_test", {}, 10000);
    if (result?.ok === false) throw new Error(result.error || "测试失败");
    const target = result.target ? "目标已绑定" : "未绑定目标";
    const warning = result.obscured ? "目标被遮挡" : result.pending_target ? "等待窗口就绪" : "";
    const overlay = result.overlay_running === false ? "浮层未运行" : "";
    showToast([target, warning, overlay].filter(Boolean).join(" · "));
    await refresh();
  } catch (error) {
    showToast(`自检失败：${error.message || error}`, true);
  } finally {
    setBusy(button, false);
  }
}

function resetAppearance() {
  fillFromPreset("clean");
  $("#prevlabel").value = "会吐弹幕的蓝色大肥鱼喵～";
  showToast("已恢复干净样式，点击「保存外观」同步到浮层");
}

function initDelegates() {
  $("#displays").addEventListener("click", (event) => {
    const card = event.target.closest("[data-display-id]");
    if (card) pickDisplay(Number(card.dataset.displayId));
  });
  $("#windows").addEventListener("click", (event) => {
    const card = event.target.closest("[data-process-id]");
    if (card) pickProcess(Number(card.dataset.processId));
  });
  $("#windowSearch").addEventListener("input", renderWindows);
  $("#btn-refresh").addEventListener("click", () => refresh());
  $("#btn-refresh-diag").addEventListener("click", () => refresh());
  $("#noticeRetry").addEventListener("click", () => refresh());
  $("#btn-screen").addEventListener("click", () => pickScreen($("#btn-screen")));
  $("#overviewScreen").addEventListener("click", () => pickScreen($("#overviewScreen")));
  $("#tbClear").addEventListener("click", () => pickScreen($("#tbClear")));
  $("#quickTest").addEventListener("click", () => runSelfTest($("#quickTest")));
  $("#overviewTest").addEventListener("click", () => runSelfTest($("#overviewTest")));
  $("#btn-selftest").addEventListener("click", () => runSelfTest($("#btn-selftest")));
}

function initLifecycle() {
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - lastStatusAt > STATUS_REFRESH_MS) refresh();
  });
  window.addEventListener("resize", schedulePreview, { passive: true });
  window.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (formDirty) saveAppearance($("#btn-apply"));
    }
  });
  setInterval(() => {
    if (!document.hidden) refresh();
  }, STATUS_REFRESH_MS);
}

function init() {
  initTheme();
  initNavigation();
  bindControls();
  renderPresets();
  syncToggles();
  initDelegates();
  initLifecycle();
  schedulePreview();
  refresh();
}

document.addEventListener("DOMContentLoaded", init);
