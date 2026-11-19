export const $ = (sel) => document.querySelector(sel);
export const $$ = (sel) => Array.from(document.querySelectorAll(sel));

export const REDUCED_MOTION =
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// 跨模块共享的轻量运行态，避免页面模块之间循环引用。
export const appState = {
  providers: [],
  trend30d: null,
  activeRun: false,
  authBusy: false,
  historyDirty: true,
};

export function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function formatDateTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value).slice(11, 19);
  }
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function localTodayStr() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function providerInitial(name) {
  return (name || "?").trim().slice(0, 1).toUpperCase();
}

export function providerDisplayName(providerId, fallbackName) {
  if (fallbackName) return fallbackName;
  return appState.providers.find((p) => p.provider_id === providerId)?.display_name || providerId;
}

export function showBanner(msg, isError = false) {
  const el = $("#toast-banner");
  if (!el) return;
  const textEl = $("#toast-text");
  if (textEl) textEl.textContent = msg;
  else el.textContent = msg;
  el.setAttribute("role", isError ? "alert" : "status");
  el.classList.remove("toast-success", "toast-error");
  el.classList.add(isError ? "toast-error" : "toast-success", "show");
  clearTimeout(window.__bannerTimer);
  if (!isError) {
    window.__bannerTimer = setTimeout(() => el.classList.remove("show"), 3600);
  }
}

$("#toast-close")?.addEventListener("click", () => {
  clearTimeout(window.__bannerTimer);
  $("#toast-banner")?.classList.remove("show");
});

export async function copyText(text, okMessage = "已复制到剪贴板") {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showBanner(okMessage);
  } catch {
    showBanner("浏览器未授予剪贴板权限", true);
  }
}

export async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(payload.detail || `请求失败 (${resp.status})`);
  }
  return payload;
}

export function statusPill(status) {
  const map = {
    running: ["执行中", "pill-running"],
    success: ["今日已签", "pill-ok"],
    already_done: ["今日已签", "pill-ok"],
    auth_expired: ["登录态失效", "pill-err"],
    needs_manual: ["需手动处理", "pill-warn"],
    schema_changed: ["接口可能改版", "pill-err"],
    unavailable: ["未就绪", "pill-warn"],
    failed: ["执行异常", "pill-err"],
  };
  const [label, cls] = map[status] || ["今日待签", "pill-muted"];
  const dot = status === "running" ? `<span class="status-dot running"></span>` : "";
  return `<span class="pill ${cls}">${dot}${label}</span>`;
}

export function animateNumber(el, targetValue, prefix = "") {
  if (!el) return;
  const numericTarget = Number(targetValue);
  if (!Number.isFinite(numericTarget)) {
    el.textContent = String(targetValue ?? "--");
    delete el.dataset.numVal;
    return;
  }
  const prevRaw = el.dataset.numVal;
  const prev = prevRaw !== undefined ? Number(prevRaw) : null;
  el.dataset.numVal = String(numericTarget);

  if (REDUCED_MOTION || prev === null || !Number.isFinite(prev) || prev === numericTarget) {
    el.textContent = `${prefix}${numericTarget}`;
    return;
  }

  const duration = 320;
  const start = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = Math.round(prev + (numericTarget - prev) * eased);
    el.textContent = `${prefix}${current}`;
    if (progress < 1) {
      requestAnimationFrame(step);
    } else {
      el.textContent = `${prefix}${numericTarget}`;
    }
  };
  requestAnimationFrame(step);
}

/**
 * 自定义手账风格确认弹窗，取代原生 confirm()。
 * 兼容不支持 showModal 的环境并自动管理焦点回退。
 */
export function confirmAction({
  title = "确认操作",
  message = "",
  impacts = [],
  confirmText = "确认",
  danger = false,
}) {
  const dialog = $("#confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    const extra = impacts.length ? `\n\n影响：\n- ${impacts.join("\n- ")}` : "";
    return Promise.resolve(window.confirm(`${message}${extra}`));
  }

  const titleEl = $("#confirm-title");
  const msgEl = $("#confirm-message");
  const impactsEl = $("#confirm-impacts");
  const okBtn = $("#confirm-ok");
  const cancelBtn = $("#confirm-cancel");

  if (titleEl) titleEl.textContent = title;
  if (msgEl) msgEl.textContent = message;
  if (impactsEl) {
    if (impacts.length) {
      impactsEl.innerHTML = impacts.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
      impactsEl.hidden = false;
    } else {
      impactsEl.innerHTML = "";
      impactsEl.hidden = true;
    }
  }
  if (okBtn) {
    okBtn.textContent = confirmText;
    okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"} btn-sm`;
  }

  return new Promise((resolve) => {
    let settled = false;
    const finish = (confirmed) => {
      if (settled) return;
      settled = true;
      dialog.removeEventListener("close", onClose);
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("click", onBackdrop);
      okBtn?.removeEventListener("click", onConfirm);
      cancelBtn?.removeEventListener("click", onDismiss);
      if (dialog.open) dialog.close();
      resolve(confirmed);
    };
    const onConfirm = (ev) => {
      ev.preventDefault();
      finish(true);
    };
    const onDismiss = () => finish(false);
    const onCancel = (ev) => {
      ev.preventDefault();
      finish(false);
    };
    const onClose = () => finish(false);
    const onBackdrop = (ev) => {
      if (ev.target === dialog) finish(false);
    };

    okBtn?.addEventListener("click", onConfirm);
    cancelBtn?.addEventListener("click", onDismiss);
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onClose);
    dialog.addEventListener("click", onBackdrop);
    dialog.showModal();
    cancelBtn?.focus();
  });
}
