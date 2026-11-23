import {
  $,
  $$,
  REDUCED_MOTION,
  apiFetch,
  appState,
  confirmAction,
  copyText,
  escapeHtml,
  formatBytes,
  formatDateTime,
  providerInitial,
  showBanner,
} from "./utils.js";

const PLANNED_TOOLS = [];

const RETENTION_PRESETS = {
  compact: {
    run_events_retention_days: 14,
    notification_retention_days: 30,
    run_retention_days: 30,
    ledger_retention_days: 90,
    vacuum_interval_days: 14,
  },
  balanced: {
    run_events_retention_days: 30,
    notification_retention_days: 90,
    run_retention_days: 180,
    ledger_retention_days: 365,
    vacuum_interval_days: 30,
  },
  long: {
    run_events_retention_days: 90,
    notification_retention_days: 180,
    run_retention_days: 365,
    ledger_retention_days: 1095,
    vacuum_interval_days: 30,
  },
};

const DEFAULT_CHECKIN_CREDITS = 100;
const MAX_CHECKIN_CREDITS = 1000000;

const PROVIDER_EXTRA_FIELDS = {
  workbuddy: [
    {
      name: "auth_file_path",
      label: "自定义登录态文件 (workbuddy-desktop.info)",
      placeholder: "留空自动探测",
    },
    {
      name: "client_exe_path",
      label: "自定义客户端 exe 路径",
      placeholder: "留空自动探测，如 D:\\WorkBuddy\\WorkBuddy.exe",
    },
    {
      name: "base_url",
      label: "自定义 API Base URL",
      placeholder: "留空自动推断，如 https://www.codebuddy.cn",
    },
    {
      name: "browser_state_path",
      label: "自定义浏览器会话文件 (storageState JSON)",
      placeholder: "留空使用默认加密会话文件",
    },
  ],
  qoder: [
    {
      name: "auth_file_path",
      label: "只读 PAT 文件路径（可选备选源）",
      placeholder: "留空不读取文件，如 C:\\Users\\...\\qoder_pat.txt",
    },
    {
      name: "client_exe_path",
      label: "自定义客户端 exe 路径",
      placeholder: "留空自动探测，如 C:\\Users\\...\\Qoder.exe",
    },
  ],
  trae: [
    {
      name: "auth_file_path",
      label: "自定义 Trae storage.json 路径",
      placeholder: "留空自动探测，如 C:\\Users\\...\\Trae CN\\User\\globalStorage\\storage.json",
    },
    {
      name: "client_exe_path",
      label: "自定义客户端 exe 路径",
      placeholder: "留空自动探测，如 D:\\Trae\\Trae CN\\Trae CN.exe",
    },
  ],
};

const DEFAULT_EXTRA_FIELDS = [
  {
    name: "client_exe_path",
    label: "自定义客户端 exe 路径",
    placeholder: "留空自动探测",
  },
];

function getProviderExtraFields(providerId) {
  return PROVIDER_EXTRA_FIELDS[providerId] || DEFAULT_EXTRA_FIELDS;
}

const RETENTION_INPUT_IDS = new Set([
  "set-events-retention",
  "set-notification-retention",
  "set-run-retention",
  "set-ledger-retention",
  "set-vacuum-interval",
]);

const GLOBAL_COMPARE_KEYS = [
  "schedule_time",
  "jitter_minutes",
  "notify_toast_enabled",
  "notify_on_already_done",
  "auto_catchup_on_logon",
  "auto_cleanup_enabled",
  "run_events_retention_days",
  "notification_retention_days",
  "run_retention_days",
  "ledger_retention_days",
  "vacuum_interval_days",
];

let settingsLoaded = false;
let settingsFetched = false;
let settingsLoadPromise = null;
let doctorFetched = false;
let scheduleBusy = false;
let globalSaveBusy = false;
let lastSavedGlobal = null;
let lastScheduleInfo = null;
let settingsProviders = [];
const doctorByProvider = new Map();
const doctorChecking = new Set();
const expandedTools = new Set();
const dirtyProviderForms = new Set();

let callbacks = {
  onOverviewRefresh: () => {},
  onHistoryRefresh: () => {},
  onAuthBrowser: () => {},
  onLaunchClient: () => {},
};

export function hasUnsavedSettings() {
  return isGlobalDirty() || dirtyProviderForms.size > 0;
}

export function isSettingsFetched() {
  return settingsFetched;
}

function setSettingsButtonsEnabled(enabled) {
  [
    "#btn-install-schedule",
    "#btn-uninstall-schedule",
    "#btn-run-maintenance",
  ].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = !enabled;
  });
  $$("[data-provider-form] button[type='submit']").forEach((btn) => {
    btn.disabled = !enabled;
  });
}

/* ---- Global Settings & Sticky Save Bar ---- */

function syncScheduleHeroPreview() {
  const clock = $("#schedule-hero-clock");
  const jitter = $("#schedule-hero-jitter");
  const catchup = $("#schedule-hero-catchup");
  if (clock) {
    clock.textContent = $("#set-schedule-time")?.value || "10:15";
  }
  if (jitter) {
    const raw = $("#set-jitter-minutes")?.value;
    const mins = raw === "" || raw === undefined ? 15 : Number(raw);
    jitter.textContent = Number.isFinite(mins) ? `±${mins} MIN` : "-- MIN";
  }
  if (catchup) {
    catchup.textContent = $("#set-catchup-logon")?.checked ? "ON LOGON" : "DISABLED";
  }
}

function populateGlobalForm(g) {
  if (!g) return;
  $("#set-schedule-time").value = g.schedule_time || "10:15";
  $("#set-jitter-minutes").value = g.jitter_minutes ?? 15;
  $("#set-toast-enabled").checked = Boolean(g.notify_toast_enabled);
  $("#set-notify-already").checked = Boolean(g.notify_on_already_done);
  $("#set-catchup-logon").checked = Boolean(g.auto_catchup_on_logon);
  $("#set-auto-cleanup").checked = Boolean(g.auto_cleanup_enabled);
  $("#set-events-retention").value = g.run_events_retention_days ?? 30;
  $("#set-notification-retention").value = g.notification_retention_days ?? 90;
  $("#set-run-retention").value = g.run_retention_days ?? 180;
  $("#set-ledger-retention").value = g.ledger_retention_days ?? 365;
  $("#set-vacuum-interval").value = g.vacuum_interval_days ?? 30;
  syncScheduleHeroPreview();
  syncPresetHighlight();
  updateSaveBarVisibility();
}

function collectGlobalSettings() {
  if (!settingsLoaded) {
    throw new Error("设置尚未加载完成，请稍候再操作。");
  }
  const scheduleTime = $("#set-schedule-time").value || "10:15";
  const jitterRaw = $("#set-jitter-minutes").value;
  const jitterMinutes = jitterRaw === "" ? 15 : Number(jitterRaw);
  if (!Number.isFinite(jitterMinutes) || jitterMinutes < 0 || jitterMinutes > 120) {
    throw new Error("随机抖动窗口必须在 0-120 分钟之间。");
  }
  const retentionValue = (selector, fallback) => {
    const raw = $(selector).value;
    const value = raw === "" ? fallback : Number(raw);
    if (!Number.isInteger(value) || value < 1 || value > 3650) {
      throw new Error("数据保留天数必须在 1-3650 天之间。");
    }
    return value;
  };
  return {
    ...(lastSavedGlobal || {}),
    schedule_time: scheduleTime,
    jitter_minutes: jitterMinutes,
    timezone: lastSavedGlobal?.timezone || "Asia/Shanghai",
    notify_toast_enabled: $("#set-toast-enabled").checked,
    notify_on_already_done: $("#set-notify-already").checked,
    auto_catchup_on_logon: $("#set-catchup-logon").checked,
    auto_cleanup_enabled: $("#set-auto-cleanup").checked,
    run_events_retention_days: retentionValue("#set-events-retention", 30),
    notification_retention_days: retentionValue("#set-notification-retention", 90),
    run_retention_days: retentionValue("#set-run-retention", 180),
    ledger_retention_days: retentionValue("#set-ledger-retention", 365),
    vacuum_interval_days: retentionValue("#set-vacuum-interval", 30),
  };
}

function isGlobalDirty() {
  if (!settingsLoaded || !lastSavedGlobal) return false;
  try {
    const current = collectGlobalSettings();
    return GLOBAL_COMPARE_KEYS.some((key) => current[key] !== lastSavedGlobal[key]);
  } catch {
    return true;
  }
}

function updateSaveBarVisibility() {
  const bar = $("#settings-save-bar");
  if (!bar) return;
  bar.hidden = !isGlobalDirty();
}

function detectRetentionPreset() {
  const vals = {
    run_events_retention_days: Number($("#set-events-retention")?.value),
    notification_retention_days: Number($("#set-notification-retention")?.value),
    run_retention_days: Number($("#set-run-retention")?.value),
    ledger_retention_days: Number($("#set-ledger-retention")?.value),
    vacuum_interval_days: Number($("#set-vacuum-interval")?.value),
  };
  for (const [name, preset] of Object.entries(RETENTION_PRESETS)) {
    if (Object.entries(preset).every(([k, v]) => vals[k] === v)) {
      return name;
    }
  }
  return "custom";
}

function syncPresetHighlight(forced = null) {
  const active = forced || detectRetentionPreset();
  $$("[data-retention-preset]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.retentionPreset === active);
  });
}

async function saveGlobalSettings(successMessage) {
  if (globalSaveBusy) return false;
  globalSaveBusy = true;
  const saveButton = $("#btn-save-global-settings");
  if (saveButton) saveButton.disabled = true;
  try {
    const previous = lastSavedGlobal;
    const payload = collectGlobalSettings();
    const saved = await apiFetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    lastSavedGlobal = saved.global || payload;
    updateSaveBarVisibility();

    const scheduleChanged = Boolean(
      previous &&
        (previous.schedule_time !== payload.schedule_time ||
          previous.auto_catchup_on_logon !== payload.auto_catchup_on_logon ||
          previous.jitter_minutes !== payload.jitter_minutes)
    );
    if (scheduleChanged && canAutoSyncSchedule()) {
      try {
        await syncScheduleIfInstalled();
        showBanner("全局设置已保存，计划任务已按新配置同步。");
        return true;
      } catch (err) {
        showBanner(`设置已保存，但计划任务同步失败：${err.message}`, true);
        return false;
      }
    }
    if (scheduleChanged && lastScheduleInfo?.installed) {
      showBanner(
        "设置已保存；当前没有管理员权限，计划任务未自动同步，请点击「安装 / 更新计划任务」。"
      );
      return true;
    }
    if (scheduleChanged && lastScheduleInfo?.query_failed) {
      showBanner(
        "设置已保存；计划任务状态查询失败，请稍后手动点击「安装 / 更新计划任务」同步。",
        true
      );
      return true;
    }

    showBanner(successMessage);
    return true;
  } catch (err) {
    showBanner(err.message, true);
    return false;
  } finally {
    globalSaveBusy = false;
    if (saveButton) saveButton.disabled = false;
  }
}

/* ---- Unified Tool Cards (Config + Credentials + Doctor) ---- */

function providerDescription(provider) {
  return (
    provider.description ??
    appState.providers.find((p) => p.provider_id === provider.provider_id)?.description ??
    ""
  );
}

function buildProviderAdvancedForm(provider) {
  const pid = provider.provider_id;
  const pidEsc = escapeHtml(pid);
  const extra = provider.extra || {};
  const earliest = escapeHtml(provider.earliest_time || "00:00");
  const fixedBusinessStart = provider.fixed_business_day_start || "";
  const businessStart = escapeHtml(
    fixedBusinessStart || provider.business_day_start || "00:00"
  );
  const businessStartHint = fixedBusinessStart
    ? `固定为 ${escapeHtml(fixedBusinessStart)}，由活动周期决定`
    : "跨过该时间才计入新一天";
  const jitterVal = extra.jitter_override ? escapeHtml(String(provider.jitter_minutes ?? 0)) : "";
  const creditsVal = escapeHtml(
    String(provider.checkin_credits ?? DEFAULT_CHECKIN_CREDITS)
  );
  const creditsHint =
    pid === "qoder"
      ? "Qoder 以活动返回金额为准，此项仅作兜底"
      : pid === "trae"
        ? "Trae 以接口返回积分为准，此项仅作兜底"
        : "接口未返回奖励时按此计入";

  const extraInputs = getProviderExtraFields(pid)
    .map(
      (field) => `
      <label class="field">
        <span>${escapeHtml(field.label)}</span>
        <input name="${escapeHtml(field.name)}" type="text" class="input" value="${escapeHtml(extra[field.name] || "")}" placeholder="${escapeHtml(field.placeholder)}" />
      </label>
    `
    )
    .join("");

  return `
    <form class="subgrid-form" data-provider-form="${pidEsc}">
      ${extraInputs}
      <label class="field">
        <span>签到积分（${creditsHint}，默认 ${DEFAULT_CHECKIN_CREDITS}）</span>
        <input name="checkin_credits" type="number" min="0" max="${MAX_CHECKIN_CREDITS}" step="1" class="input" value="${creditsVal}" />
      </label>
      <label class="field">
        <span>业务日起始时间（${businessStartHint}）</span>
        <input name="business_day_start" type="time" class="input" value="${businessStart}" ${fixedBusinessStart ? "disabled" : ""} />
      </label>
      <div class="two-col-fields">
        <label class="field">
          <span>最早可执行时间（补跑生效）</span>
          <input name="earliest_time" type="time" class="input" value="${earliest}" />
        </label>
        <label class="field">
          <span>独立抖动（分钟，留空继承全局）</span>
          <input name="jitter_minutes" type="number" min="0" max="120" class="input" value="${jitterVal}" placeholder="继承全局" />
        </label>
      </div>
      <div class="form-actions">
        <button type="button" class="btn btn-quiet btn-sm" data-reset-provider-form="${pidEsc}" hidden>还原</button>
        <button type="submit" class="btn btn-secondary btn-sm">保存配置</button>
      </div>
    </form>
  `;
}

function buildCredentialBlock(provider) {
  const pid = provider.provider_id;
  if (pid === "qoder") {
    return `
      <div class="tool-cred-box">
        <h4>Qoder CN PAT 凭据</h4>
        <p class="muted" id="qoder-pat-status">PAT 状态读取中...</p>
        <div class="cred-inline">
          <input id="qoder-pat-token" type="password" class="input" autocomplete="off" spellcheck="false" placeholder="粘贴 pt- / jt- 开头的 Qoder CN PAT（本地 DPAPI 加密存储）" />
          <button id="btn-save-qoder-pat" type="button" class="btn btn-primary btn-sm">保存 PAT</button>
          <button id="btn-clear-qoder-pat" type="button" class="btn btn-ghost btn-sm">清除</button>
        </div>
      </div>
    `;
  }
  if (provider.supports_browser_auth) {
    return `
      <div class="tool-cred-box">
        <h4>桌面会话与授权</h4>
        <p class="muted">若提示登录态过期或凭证加密，可通过 Edge 浏览器完成一次授权，或打开客户端刷新会话。</p>
        <div class="inline-actions">
          <button type="button" class="btn btn-secondary btn-sm" data-auth-browser="${escapeHtml(pid)}">浏览器授权</button>
          <button type="button" class="btn btn-ghost btn-sm" data-launch-provider="${escapeHtml(pid)}">打开客户端</button>
        </div>
      </div>
    `;
  }
  if (pid === "trae") {
    return `
      <div class="tool-cred-box">
        <h4>Trae 国内版本地登录态</h4>
        <p class="muted">GetEggs 只读读取 Trae CN 客户端的加密登录态，签到按真实设备每日一次。登录态接近过期时会按需续期，续期可能使 Trae 客户端需要重新登录。</p>
        <div class="inline-actions">
          <button type="button" class="btn btn-secondary btn-sm" data-launch-provider="${escapeHtml(pid)}">打开 Trae 客户端</button>
          <button type="button" class="btn btn-ghost btn-sm" data-doctor-provider="${escapeHtml(pid)}">重新体检</button>
        </div>
      </div>
    `;
  }
  return "";
}

function renderToolCards() {
  const list = $("#tool-cards-list");
  if (!list) return;

  if (!settingsProviders.length) {
    list.innerHTML = `<p class="muted">暂无可用工具。</p>`;
  } else {
    list.innerHTML = settingsProviders
      .map((provider) => {
        const pid = provider.provider_id;
        const pidEsc = escapeHtml(pid);
        const expanded = expandedTools.has(pid);
        const desc = providerDescription(provider);

        return `
          <article class="unified-tool-card ${provider.enabled ? "is-enabled" : ""} ${expanded ? "is-expanded" : ""}" data-tool-card="${pidEsc}">
            <div class="tool-card-head">
              <div class="tool-card-id">
                <span class="provider-monogram" aria-hidden="true">${escapeHtml(providerInitial(provider.display_name))}</span>
                <div>
                  <div class="tool-card-title-row">
                    <strong>${escapeHtml(provider.display_name)}</strong>
                    <span class="pill pill-muted" data-tool-health="${pidEsc}">未检测</span>
                  </div>
                  <p class="tool-card-desc">${escapeHtml(desc)}</p>
                </div>
              </div>

              <div class="tool-card-controls">
                <label class="switch-field" title="开启或停用每日自动签到">
                  <input type="checkbox" class="switch-input" data-tool-toggle="${pidEsc}" ${provider.enabled ? "checked" : ""} />
                  <span>自动调度</span>
                </label>
                <button type="button" class="btn btn-quiet btn-sm" data-tool-expand="${pidEsc}" aria-expanded="${expanded ? "true" : "false"}">
                  ${expanded ? "收起 ▴" : "配置与体检 ▾"}
                </button>
              </div>
            </div>

            <div class="tool-card-summary" data-tool-summary="${pidEsc}"></div>

            <div class="tool-card-body" data-tool-body="${pidEsc}" ${expanded ? "" : "hidden"}>
              <div class="tool-diag-grid" data-tool-diag="${pidEsc}">
                <span class="muted-meta">等待执行环境检测...</span>
              </div>
              ${buildCredentialBlock(provider)}
              <details class="sub-accordion" data-tool-adv="${pidEsc}">
                <summary class="sub-accordion-summary">高级：路径覆盖与执行窗口</summary>
                <div class="sub-accordion-body">
                  ${buildProviderAdvancedForm(provider)}
                </div>
              </details>
            </div>
          </article>
        `;
      })
      .join("");
  }

  const plannedEl = $("#planned-tools-note");
  if (plannedEl) {
    if (PLANNED_TOOLS.length) {
      plannedEl.textContent = `即将支持：${PLANNED_TOOLS.map((t) => `${t.display_name}（${t.description}）`).join("、")}`;
      plannedEl.hidden = false;
    } else {
      plannedEl.hidden = true;
    }
  }

  settingsProviders.forEach((p) => updateToolCardDoctorUi(p.provider_id));
  dirtyProviderForms.forEach((pid) => markProviderFormDirty(pid, true));
}

function updateToolCardDoctorUi(providerId) {
  const card = document.querySelector(`[data-tool-card="${CSS.escape(providerId)}"]`);
  if (!card) return;
  const pill = card.querySelector(`[data-tool-health="${CSS.escape(providerId)}"]`);
  const summary = card.querySelector(`[data-tool-summary="${CSS.escape(providerId)}"]`);
  const diag = card.querySelector(`[data-tool-diag="${CSS.escape(providerId)}"]`);
  const provider = settingsProviders.find((p) => p.provider_id === providerId);
  const report = doctorByProvider.get(providerId);
  const checking = doctorChecking.has(providerId);

  if (checking) {
    card.classList.remove("is-unhealthy");
    if (pill) {
      pill.className = "pill pill-running";
      pill.innerHTML = `<span class="status-dot running"></span>体检中`;
    }
    if (summary) {
      summary.innerHTML = `<span class="tool-card-summary-text muted">正在只读检测客户端路径与凭证状态...</span>`;
    }
    if (diag) {
      diag.innerHTML = `
        <div class="skeleton-line" style="width: 60%;"></div>
        <div class="skeleton-line" style="width: 85%;"></div>
      `;
    }
    return;
  }

  if (!report) {
    card.classList.remove("is-unhealthy");
    if (pill) {
      pill.className = "pill pill-muted";
      pill.textContent = "未检测";
    }
    if (summary) {
      summary.innerHTML = `<span class="tool-card-summary-text muted">尚未检测本机环境与凭证状态</span>`;
    }
    if (diag) {
      diag.innerHTML = `<div class="tool-diag-foot"><span class="muted-meta">点击右侧按钮执行只读检测</span><button type="button" class="btn btn-ghost btn-sm" data-doctor-provider="${escapeHtml(providerId)}">立即体检</button></div>`;
    }
    return;
  }

  card.classList.toggle("is-unhealthy", !report.healthy);
  if (pill) {
    pill.className = `pill ${report.healthy ? "pill-ok" : "pill-err"}`;
    pill.textContent = report.healthy ? "健康就绪" : "需要关注";
  }

  if (summary) {
    let quickBtn = "";
    if (!report.healthy) {
      if (provider?.supports_browser_auth) {
        quickBtn = `<button type="button" class="btn btn-secondary btn-sm" data-auth-browser="${escapeHtml(providerId)}">浏览器授权</button>`;
      } else if (providerId === "qoder") {
        quickBtn = `<button type="button" class="btn btn-secondary btn-sm" data-focus-pat="qoder">配置 PAT</button>`;
      } else if (providerId === "trae") {
        quickBtn = `<button type="button" class="btn btn-secondary btn-sm" data-launch-provider="${escapeHtml(providerId)}">打开客户端</button>`;
      } else {
        quickBtn = `<button type="button" class="btn btn-secondary btn-sm" data-open-tool-adv="${escapeHtml(providerId)}">检查路径</button>`;
      }
    }
    summary.innerHTML = `
      <span class="tool-card-summary-text">${escapeHtml(report.message)}</span>
      ${quickBtn}
    `;
  }

  if (diag) {
    const exeVal = report.client_installed ? report.client_exe_path : "未检测到可执行文件";
    const authVal = report.auth_file_found ? report.auth_file_path : "未找到凭证或登录态文件";
    const expVal = formatDateTime(report.token_expires_at);
    diag.innerHTML = `
      <div class="doctor-meta-row">
        <span class="doctor-meta-label">账号标识</span>
        <span class="doctor-meta-value">${escapeHtml(report.account_label || "未识别")}</span>
      </div>
      <div class="doctor-meta-row">
        <span class="doctor-meta-label">客户端路径</span>
        <span class="doctor-meta-value">
          <span class="doctor-meta-text" title="${escapeHtml(exeVal)}">${escapeHtml(exeVal)}</span>
          ${report.client_installed && report.client_exe_path ? `<button type="button" class="btn btn-quiet btn-sm" data-copy-text="${escapeHtml(report.client_exe_path)}">复制</button>` : ""}
        </span>
      </div>
      <div class="doctor-meta-row">
        <span class="doctor-meta-label">凭证来源</span>
        <span class="doctor-meta-value">
          <span class="doctor-meta-text" title="${escapeHtml(authVal)}">${escapeHtml(authVal)}</span>
          ${report.auth_file_found && report.auth_file_path ? `<button type="button" class="btn btn-quiet btn-sm" data-copy-text="${escapeHtml(report.auth_file_path)}">复制</button>` : ""}
        </span>
      </div>
      <div class="doctor-meta-row">
        <span class="doctor-meta-label">令牌有效期</span>
        <span class="doctor-meta-value">${escapeHtml(expVal)}</span>
      </div>
      <div class="tool-diag-foot">
        <span class="muted-meta">只读体检，不回传明文凭证</span>
        <button type="button" class="btn btn-ghost btn-sm" data-doctor-provider="${escapeHtml(providerId)}">重新体检</button>
      </div>
    `;
  }
}

async function runDoctor({ providerId = null, silent = false } = {}) {
  const allBtn = $("#btn-run-doctor");
  const targets = providerId
    ? [providerId]
    : settingsProviders.map((p) => p.provider_id);

  if (!targets.length) return;
  if (targets.some((id) => doctorChecking.has(id))) return;

  targets.forEach((id) => {
    doctorChecking.add(id);
    updateToolCardDoctorUi(id);
  });

  const originalHtml = allBtn?.innerHTML;
  if (!providerId && allBtn) {
    allBtn.disabled = true;
    allBtn.textContent = "体检中...";
  }

  try {
    const query = providerId ? `?provider_id=${encodeURIComponent(providerId)}` : "";
    const { results } = await apiFetch(`/api/doctor${query}`, { method: "POST" });
    doctorFetched = true;
    (results || []).forEach((r) => {
      doctorByProvider.set(r.provider_id, r);
      doctorChecking.delete(r.provider_id);
      updateToolCardDoctorUi(r.provider_id);
    });
    const stamp = $("#doctor-last-checked");
    if (stamp) {
      stamp.textContent = `最近体检：${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 全程只读`;
    }
    if (!silent) {
      const unhealthy = (results || []).filter((r) => !r.healthy).length;
      showBanner(unhealthy ? `体检完成，${unhealthy} 个工具需要关注` : "全部工具环境健康就绪");
    }
  } catch (err) {
    targets.forEach((id) => {
      doctorChecking.delete(id);
      updateToolCardDoctorUi(id);
    });
    if (!silent) showBanner(err.message, true);
  } finally {
    if (!providerId && allBtn) {
      allBtn.disabled = false;
      if (originalHtml !== undefined) allBtn.innerHTML = originalHtml;
    }
  }
}

function setToolExpanded(providerId, expand) {
  if (expand) expandedTools.add(providerId);
  else expandedTools.delete(providerId);
  const card = document.querySelector(`[data-tool-card="${CSS.escape(providerId)}"]`);
  if (!card) return;
  card.classList.toggle("is-expanded", expand);
  const body = card.querySelector(`[data-tool-body="${CSS.escape(providerId)}"]`);
  const btn = card.querySelector(`[data-tool-expand="${CSS.escape(providerId)}"]`);
  if (body) body.hidden = !expand;
  if (btn) {
    btn.setAttribute("aria-expanded", expand ? "true" : "false");
    btn.textContent = expand ? "收起 ▴" : "配置与体检 ▾";
  }
}

function markProviderFormDirty(providerId, dirty) {
  if (dirty) dirtyProviderForms.add(providerId);
  else dirtyProviderForms.delete(providerId);
  const form = document.querySelector(`[data-provider-form="${CSS.escape(providerId)}"]`);
  if (!form) return;
  form.querySelector("button[type='submit']")?.classList.toggle("has-changes", dirty);
  const resetBtn = form.querySelector(`[data-reset-provider-form="${CSS.escape(providerId)}"]`);
  if (resetBtn) resetBtn.hidden = !dirty;
}

function resetProviderForm(providerId) {
  const provider = settingsProviders.find((p) => p.provider_id === providerId);
  const form = document.querySelector(`[data-provider-form="${CSS.escape(providerId)}"]`);
  if (!provider || !form) return;
  const extra = provider.extra || {};

  for (const field of getProviderExtraFields(providerId)) {
    const input = form.querySelector(`[name="${CSS.escape(field.name)}"]`);
    if (input) input.value = extra[field.name] || "";
  }
  const earliestInput = form.querySelector("[name='earliest_time']");
  const businessStartInput = form.querySelector("[name='business_day_start']");
  const jitterInput = form.querySelector("[name='jitter_minutes']");
  const creditsInput = form.querySelector("[name='checkin_credits']");
  if (earliestInput) earliestInput.value = provider.earliest_time || "00:00";
  if (businessStartInput) {
    businessStartInput.value =
      provider.fixed_business_day_start || provider.business_day_start || "00:00";
  }
  if (jitterInput) {
    jitterInput.value = extra.jitter_override ? String(provider.jitter_minutes ?? 0) : "";
  }
  if (creditsInput) {
    creditsInput.value = String(provider.checkin_credits ?? DEFAULT_CHECKIN_CREDITS);
  }
  markProviderFormDirty(providerId, false);
}

async function submitProviderForm(form) {
  if (!settingsLoaded) {
    showBanner("设置尚未加载完成，请稍候再操作。", true);
    return;
  }
  const pid = form.dataset.providerForm;
  if (!pid) return;

  try {
    const payload = {};
    for (const field of getProviderExtraFields(pid)) {
      payload[field.name] = form.querySelector(`[name="${CSS.escape(field.name)}"]`)?.value ?? "";
    }
    const earliest = form.querySelector("[name='earliest_time']")?.value;
    if (earliest) payload.earliest_time = earliest;
    const businessStart = form.querySelector("[name='business_day_start']")?.value;
    if (businessStart) payload.business_day_start = businessStart;
    const creditsValue = (form.querySelector("[name='checkin_credits']")?.value ?? "").trim();
    const checkinCredits =
      creditsValue === "" ? DEFAULT_CHECKIN_CREDITS : Number(creditsValue);
    if (!Number.isInteger(checkinCredits) || checkinCredits < 0 || checkinCredits > MAX_CHECKIN_CREDITS) {
      showBanner(`签到积分必须是 0-${MAX_CHECKIN_CREDITS} 之间的整数。`, true);
      return;
    }
    payload.checkin_credits = checkinCredits;
    const jitterValue = form.querySelector("[name='jitter_minutes']")?.value ?? "";
    if (jitterValue !== "") {
      payload.jitter_minutes = Number(jitterValue);
      payload.jitter_override = true;
    } else {
      payload.jitter_override = false;
    }

    const previous = settingsProviders.find((p) => p.provider_id === pid);
    const beforeJitter = previous?.extra?.jitter_override
      ? Number(previous.jitter_minutes || 0)
      : Number(lastSavedGlobal?.jitter_minutes ?? 15);

    const res = await apiFetch(`/api/providers/${encodeURIComponent(pid)}/config`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    const idx = settingsProviders.findIndex((p) => p.provider_id === pid);
    if (idx >= 0 && res.provider) {
      settingsProviders[idx] = { ...settingsProviders[idx], ...res.provider };
    }
    markProviderFormDirty(pid, false);
    const name = settingsProviders[idx]?.display_name || pid;
    const afterJitter = res.provider?.extra?.jitter_override
      ? Number(res.provider.jitter_minutes || 0)
      : Number(lastSavedGlobal?.jitter_minutes ?? 15);
    if (beforeJitter !== afterJitter && canAutoSyncSchedule()) {
      try {
        await syncScheduleIfInstalled();
        showBanner(`${name} 配置已保存，计划任务执行时限已同步`);
      } catch (err) {
        showBanner(`${name} 配置已保存，但计划任务时限同步失败：${err.message}`, true);
      }
    } else if (beforeJitter !== afterJitter && lastScheduleInfo?.installed) {
      showBanner(
        `${name} 配置已保存；当前没有管理员权限，计划任务执行时限未同步，请点击「安装 / 更新计划任务」。`
      );
    } else {
      showBanner(`${name} 配置已保存`);
    }
    callbacks.onOverviewRefresh(true);
  } catch (err) {
    showBanner(err.message, true);
  }
}

async function loadQoderPatStatus() {
  const el = $("#qoder-pat-status");
  if (!el) return;
  try {
    const data = await apiFetch("/api/providers/qoder/pat/status");
    if (data.configured) {
      const suffix =
        data.format_ok === false ? "（格式不是 pt- / jt- 开头，请确认）" : "";
      el.textContent = `PAT 已配置，来源：${data.source || "未知"}${suffix}`;
    } else {
      el.textContent = "尚未配置 PAT（留空时也可读取环境变量或 PAT 文件）。";
    }
  } catch (err) {
    el.textContent = `PAT 状态读取失败：${err.message}`;
  }
}

function formatTaskResult(code) {
  if (code === null || code === undefined || code === "") return "--";
  const value = Number(code);
  if (value === 0) return "成功";
  if (value === 267011) return "尚未运行";
  if (value === 267009) return "正在运行";
  return `异常（代码 ${code}）`;
}

async function syncScheduleIfInstalled() {
  if (!lastScheduleInfo?.installed) return false;
  const res = await apiFetch("/api/schedule/install", { method: "POST" });
  renderScheduleBox(res.schedule);
  callbacks.onOverviewRefresh();
  return true;
}

function canAutoSyncSchedule() {
  return Boolean(lastScheduleInfo?.installed && lastScheduleInfo?.elevated);
}

function renderScheduleBox(sch) {
  lastScheduleInfo = sch || null;
  const box = $("#schedule-detail-box");
  const pill = $("#schedule-status-pill");
  if (pill && sch) {
    if (!sch.supported) {
      pill.textContent = "不支持";
      pill.className = "pill pill-muted";
    } else if (sch.query_failed) {
      pill.textContent = "查询失败";
      pill.className = "pill pill-err";
    } else if (sch.installed) {
      const state = String(sch.state || "Ready");
      const healthy = ["ready", "running", "queued"].includes(
        state.toLowerCase()
      );
      pill.textContent = `已安装 (${state})`;
      pill.className = `pill ${healthy ? "pill-ok" : "pill-warn"}`;
    } else {
      pill.textContent = "未安装";
      pill.className = "pill pill-warn";
    }
  }
  if (!box || !sch) return;
  if (!sch.supported || sch.query_failed) {
    box.textContent = sch.message || "计划任务状态查询失败，请稍后重试。";
    return;
  }
  box.innerHTML = sch.installed
    ? `上次运行：${escapeHtml(formatDateTime(sch.last_run_time))}<br/>下次计划：${escapeHtml(formatDateTime(sch.next_run_time))}<br/>上次结果：${escapeHtml(formatTaskResult(sch.last_task_result))}`
    : `尚未注册系统计划任务，点击下方按钮即可按左侧时间自动注册（含登录后补漏）。`;
}

export async function loadMaintenanceInfo() {
  const box = $("#maintenance-info");
  try {
    const info = await apiFetch("/api/maintenance");
    const counts = info.counts || {};
    const sizeText = formatBytes(info.db_size);
    if (box) {
      box.innerHTML = [
        `数据库大小：<strong>${escapeHtml(sizeText)}</strong>（备份 ${info.backup_count ?? 0} 份）`,
        `流水 ${counts.checkin_records ?? 0} 条 · 批次 ${counts.checkin_runs ?? 0} 条 · 日志 ${counts.run_events ?? 0} 条 · 通知 ${counts.notifications ?? 0} 条`,
        `最近清理：${escapeHtml(formatDateTime(info.last_cleanup_at))} · 最近 VACUUM：${escapeHtml(formatDateTime(info.last_vacuum_at))}`,
      ].join("<br/>");
    }
  } catch (err) {
    if (box) box.textContent = err.message;
  }
}

export async function loadSettings({ autoDoctor = true } = {}) {
  if (settingsLoadPromise) return settingsLoadPromise;
  settingsLoadPromise = (async () => {
    settingsLoaded = false;
    setSettingsButtonsEnabled(false);
    try {
      const data = await apiFetch("/api/settings");
      lastSavedGlobal = data.global;
      populateGlobalForm(lastSavedGlobal);
      renderScheduleBox(data.schedule);

      settingsProviders = data.providers || [];
      dirtyProviderForms.clear();
      renderToolCards();

      await loadQoderPatStatus();
      settingsLoaded = true;
      settingsFetched = true;
      setSettingsButtonsEnabled(true);
      updateSaveBarVisibility();
      loadMaintenanceInfo();

      if (autoDoctor && !doctorFetched) {
        runDoctor({ silent: true });
      }
    } catch (err) {
      showBanner(err.message, true);
    }
  })();
  try {
    await settingsLoadPromise;
  } finally {
    settingsLoadPromise = null;
  }
}

export async function focusTool(providerId, { openAdvanced = false } = {}) {
  if (!settingsFetched) {
    await loadSettings({ autoDoctor: true });
  }
  setToolExpanded(providerId, true);
  const card = document.querySelector(`[data-tool-card="${CSS.escape(providerId)}"]`);
  if (!card) return;
  const behavior = REDUCED_MOTION ? "auto" : "smooth";

  if (providerId === "qoder" && !openAdvanced) {
    const patInput = $("#qoder-pat-token");
    patInput?.scrollIntoView({ behavior, block: "center" });
    patInput?.focus({ preventScroll: true });
    return;
  }

  if (openAdvanced) {
    const adv = card.querySelector(`[data-tool-adv="${CSS.escape(providerId)}"]`);
    if (adv) adv.open = true;
  }
  card.scrollIntoView({ behavior, block: "nearest" });
}

export async function focusSection(sectionId) {
  if (!settingsFetched) {
    await loadSettings({ autoDoctor: true });
  }
  const el = document.getElementById(sectionId);
  el?.scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth", block: "start" });
}

async function installSchedule() {
  if (scheduleBusy) return;
  scheduleBusy = true;
  const installButton = $("#btn-install-schedule");
  const uninstallButton = $("#btn-uninstall-schedule");
  const originalText = installButton.textContent;
  installButton.disabled = true;
  uninstallButton.disabled = true;
  installButton.textContent = "安装中...";
  try {
    const settings = collectGlobalSettings();
    const saved = await apiFetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
    lastSavedGlobal = saved.global || settings;
    updateSaveBarVisibility();

    const res = await apiFetch("/api/schedule/install", { method: "POST" });
    renderScheduleBox(res.schedule);
    showBanner(res.message);
    callbacks.onOverviewRefresh();
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    scheduleBusy = false;
    installButton.disabled = false;
    uninstallButton.disabled = false;
    installButton.textContent = originalText;
  }
}

async function uninstallSchedule() {
  if (scheduleBusy) return;
  const confirmed = await confirmAction({
    title: "卸载 Windows 计划任务",
    message: "确定要移除每日自动签到计划任务吗？",
    impacts: [
      "停止每日定时静默签到与登录自动补漏",
      "已保存的工具配置、凭据和历史账本不会被删除",
      "可随时点击「安装 / 更新计划任务」重新启用",
    ],
    confirmText: "确认卸载",
    danger: true,
  });
  if (!confirmed) return;

  scheduleBusy = true;
  const installButton = $("#btn-install-schedule");
  const uninstallButton = $("#btn-uninstall-schedule");
  const originalText = uninstallButton.textContent;
  installButton.disabled = true;
  uninstallButton.disabled = true;
  uninstallButton.textContent = "卸载中...";
  try {
    const res = await apiFetch("/api/schedule", { method: "DELETE" });
    renderScheduleBox(res.schedule);
    showBanner(res.message);
    callbacks.onOverviewRefresh();
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    scheduleBusy = false;
    installButton.disabled = false;
    uninstallButton.disabled = false;
    uninstallButton.textContent = originalText;
  }
}

export function initSettings(userCallbacks = {}) {
  callbacks = { ...callbacks, ...userCallbacks };
  setSettingsButtonsEnabled(false);

  $("#btn-run-doctor")?.addEventListener("click", () => runDoctor({ silent: false }));

  // 监听全局设置输入变动，动态更新吸底保存栏
  const globalFieldIds = new Set([
    "set-schedule-time",
    "set-jitter-minutes",
    "set-catchup-logon",
    "set-toast-enabled",
    "set-notify-already",
    "set-auto-cleanup",
    "set-events-retention",
    "set-notification-retention",
    "set-run-retention",
    "set-ledger-retention",
    "set-vacuum-interval",
  ]);

  const onGlobalChange = (ev) => {
    if (!settingsLoaded) return;
    if (!globalFieldIds.has(ev.target?.id)) return;
    if (RETENTION_INPUT_IDS.has(ev.target.id)) {
      syncPresetHighlight();
    }
    syncScheduleHeroPreview();
    updateSaveBarVisibility();
  };
  $("#tab-settings")?.addEventListener("input", onGlobalChange);
  $("#tab-settings")?.addEventListener("change", onGlobalChange);

  $("#btn-save-global-settings")?.addEventListener("click", async () => {
    await saveGlobalSettings("全局设置已保存");
  });
  $("#btn-discard-global-settings")?.addEventListener("click", () => {
    if (!lastSavedGlobal) return;
    populateGlobalForm(lastSavedGlobal);
    showBanner("已还原未保存的全局设置");
  });

  // 保留策略预设点击
  $$("[data-retention-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.retentionPreset;
      if (name === "custom") {
        const details = $("#retention-custom-details");
        if (details) details.open = true;
        syncPresetHighlight("custom");
        return;
      }
      const preset = RETENTION_PRESETS[name];
      if (!preset) return;
      $("#set-events-retention").value = preset.run_events_retention_days;
      $("#set-notification-retention").value = preset.notification_retention_days;
      $("#set-run-retention").value = preset.run_retention_days;
      $("#set-ledger-retention").value = preset.ledger_retention_days;
      $("#set-vacuum-interval").value = preset.vacuum_interval_days;
      syncPresetHighlight(name);
      updateSaveBarVisibility();
    });
  });

  // 统一工具卡片区事件委托
  const toolsList = $("#tool-cards-list");
  toolsList?.addEventListener("click", async (ev) => {
    const expandBtn = ev.target.closest("[data-tool-expand]");
    if (expandBtn) {
      const pid = expandBtn.dataset.toolExpand;
      setToolExpanded(pid, !expandedTools.has(pid));
      return;
    }
    const doctorBtn = ev.target.closest("[data-doctor-provider]");
    if (doctorBtn) {
      runDoctor({ providerId: doctorBtn.dataset.doctorProvider, silent: false });
      return;
    }
    const copyBtn = ev.target.closest("[data-copy-text]");
    if (copyBtn) {
      copyText(copyBtn.dataset.copyText, "已复制路径");
      return;
    }
    const authBtn = ev.target.closest("[data-auth-browser]");
    if (authBtn) {
      callbacks.onAuthBrowser(authBtn.dataset.authBrowser, authBtn);
      return;
    }
    const launchBtn = ev.target.closest("[data-launch-provider]");
    if (launchBtn) {
      callbacks.onLaunchClient(launchBtn.dataset.launchProvider);
      return;
    }
    const focusPatBtn = ev.target.closest("[data-focus-pat]");
    if (focusPatBtn) {
      focusTool(focusPatBtn.dataset.focusPat, { openAdvanced: false });
      return;
    }
    const openAdvBtn = ev.target.closest("[data-open-tool-adv]");
    if (openAdvBtn) {
      focusTool(openAdvBtn.dataset.openToolAdv, { openAdvanced: true });
      return;
    }
    const resetBtn = ev.target.closest("[data-reset-provider-form]");
    if (resetBtn) {
      resetProviderForm(resetBtn.dataset.resetProviderForm);
      return;
    }
    if (ev.target.closest("#btn-save-qoder-pat")) {
      const input = $("#qoder-pat-token");
      const pat = input?.value.trim() || "";
      if (!pat) {
        showBanner("请先输入 Qoder CN PAT。", true);
        return;
      }
      const button = $("#btn-save-qoder-pat");
      button.disabled = true;
      try {
        const data = await apiFetch("/api/providers/qoder/pat", {
          method: "PUT",
          body: JSON.stringify({ pat }),
        });
        input.value = "";
        showBanner(
          data.format_ok === false
            ? "PAT 已加密保存，但格式不是 pt- / jt- 开头，请确认。"
            : "Qoder PAT 已加密保存"
        );
        await loadQoderPatStatus();
        runDoctor({ providerId: "qoder", silent: true });
        callbacks.onOverviewRefresh(true);
      } catch (err) {
        showBanner(err.message, true);
      } finally {
        button.disabled = false;
      }
      return;
    }
    if (ev.target.closest("#btn-clear-qoder-pat")) {
      const button = $("#btn-clear-qoder-pat");
      button.disabled = true;
      try {
        const data = await apiFetch("/api/providers/qoder/pat", { method: "DELETE" });
        showBanner(
          data.configured
            ? "已清除网页保存的 PAT，但仍有其他凭证来源生效。"
            : "已清除网页保存的 Qoder PAT"
        );
        await loadQoderPatStatus();
        runDoctor({ providerId: "qoder", silent: true });
        callbacks.onOverviewRefresh(true);
      } catch (err) {
        showBanner(err.message, true);
      } finally {
        button.disabled = false;
      }
    }
  });

  toolsList?.addEventListener("change", async (ev) => {
    const toggle = ev.target.closest("[data-tool-toggle]");
    if (toggle) {
      const providerId = toggle.dataset.toolToggle;
      const enabled = toggle.checked;
      const card = toggle.closest(".unified-tool-card");
      const name = settingsProviders.find((p) => p.provider_id === providerId)?.display_name || providerId;
      toggle.disabled = true;
      try {
        await apiFetch(`/api/providers/${encodeURIComponent(providerId)}/config`, {
          method: "PUT",
          body: JSON.stringify({ enabled }),
        });
        const idx = settingsProviders.findIndex((p) => p.provider_id === providerId);
        if (idx >= 0) settingsProviders[idx].enabled = enabled;
        card?.classList.toggle("is-enabled", enabled);
        if (lastScheduleInfo?.installed && canAutoSyncSchedule()) {
          try {
            await syncScheduleIfInstalled();
            showBanner(
              `已${enabled ? "启用" : "停用"} ${name} 自动调度，计划任务执行时限已同步`
            );
          } catch (err) {
            showBanner(
              `已${enabled ? "启用" : "停用"} ${name}，但计划任务时限同步失败：${err.message}`,
              true
            );
          }
        } else if (lastScheduleInfo?.installed) {
          showBanner(
            `已${enabled ? "启用" : "停用"} ${name} 自动调度；当前没有管理员权限，计划任务执行时限未同步，请点击「安装 / 更新计划任务」。`
          );
        } else {
          showBanner(`已${enabled ? "启用" : "停用"} ${name} 自动调度`);
        }
        callbacks.onOverviewRefresh(true);
      } catch (err) {
        toggle.checked = !enabled;
        showBanner(err.message, true);
      } finally {
        toggle.disabled = false;
      }
      return;
    }
    const form = ev.target.closest("[data-provider-form]");
    if (form && settingsLoaded) {
      markProviderFormDirty(form.dataset.providerForm, true);
    }
  });

  toolsList?.addEventListener("input", (ev) => {
    const form = ev.target.closest("[data-provider-form]");
    if (form && settingsLoaded) {
      markProviderFormDirty(form.dataset.providerForm, true);
    }
  });

  toolsList?.addEventListener("submit", (ev) => {
    const form = ev.target.closest("[data-provider-form]");
    if (!form) return;
    ev.preventDefault();
    submitProviderForm(form);
  });

  $("#btn-install-schedule")?.addEventListener("click", installSchedule);
  $("#btn-uninstall-schedule")?.addEventListener("click", uninstallSchedule);

  $("#btn-test-toast")?.addEventListener("click", async (ev) => {
    const button = ev.currentTarget;
    if (button.disabled) return;
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "发送中...";
    try {
      const res = await apiFetch("/api/notifiers/test", { method: "POST" });
      showBanner(res.message);
      callbacks.onHistoryRefresh();
    } catch (err) {
      showBanner(err.message, true);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  });

  $("#btn-run-maintenance")?.addEventListener("click", async (ev) => {
    const button = ev.currentTarget;
    const confirmed = await confirmAction({
      title: "清理过期历史数据",
      message: "将按当前保留策略清理超出保留天数的记录：",
      impacts: [
        "清理前会自动为当前 SQLite 数据库创建一份完整备份",
        "仅删除超出保留期的日志、通知、批次与旧流水",
        "不会影响当前工具配置、凭据及最近状态",
      ],
      confirmText: "立即清理",
      danger: false,
    });
    if (!confirmed) return;

    if (button.disabled) return;
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "清理中...";
    try {
      const res = await apiFetch("/api/maintenance/cleanup", {
        method: "POST",
        body: JSON.stringify({ vacuum: false }),
      });
      const deleted = res.deleted || {};
      const changed = Object.entries(deleted)
        .filter(([, value]) => value)
        .map(([key, value]) => `${key}=${value}`)
        .join(", ");
      showBanner(changed ? `数据清理完成：${changed}` : "数据清理完成，无过期数据");
      appState.historyDirty = true;
      loadMaintenanceInfo();
      callbacks.onHistoryRefresh();
      callbacks.onOverviewRefresh();
    } catch (err) {
      showBanner(err.message, true);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  });

  $("#btn-shutdown-web")?.addEventListener("click", async (ev) => {
    const button = ev.currentTarget;
    const confirmed = await confirmAction({
      title: "关闭本地 Web 服务",
      message: "确定关闭当前本地控制台进程吗？",
      impacts: [
        "本网页与本地 API 将立即停止响应，如需使用可再次运行 `geteggs ui`",
        "已安装的 Windows 计划任务不受影响，仍会定时静默执行",
      ],
      confirmText: "确认关闭",
      danger: true,
    });
    if (!confirmed) return;

    if (button.disabled) return;
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "正在关闭...";
    try {
      const res = await apiFetch("/api/system/shutdown", { method: "POST" });
      showBanner(res.message);
    } catch (err) {
      showBanner(err.message, true);
      button.disabled = false;
      button.textContent = originalText;
    }
  });
}
