import {
  $,
  $$,
  animateNumber,
  apiFetch,
  appState,
  copyText,
  escapeHtml,
  formatDateTime,
  formatTime,
  providerInitial,
  showBanner,
  sleep,
  statusPill,
} from "./utils.js";
import { renderFullTrendChart, renderMiniTrendChart } from "./charts.js";

const selectedProviders = new Set();
const runningProviderIds = new Set();
let selectionInitialized = false;
let selectionMode = false;
let callbacks = {
  onRunComplete: () => {},
  onOpenToolConfig: () => {},
  onOpenHistory: () => {},
};

const ACTION_NEEDED_STATUSES = new Set([
  "auth_expired",
  "needs_manual",
  "schema_changed",
  "unavailable",
  "failed",
]);

function renderAlertStrip(providers) {
  const strip = $("#overview-alert-strip");
  if (!strip) return;

  const issues = providers
    .map((p) => {
      const todayRec = p.today_record;
      const status = todayRec?.last_status || todayRec?.status;
      if (!status || !ACTION_NEEDED_STATUSES.has(status)) return null;
      return { provider: p, status, message: todayRec?.message || "需要处理" };
    })
    .filter(Boolean);

  if (!issues.length) {
    strip.innerHTML = "";
    strip.hidden = true;
    return;
  }

  strip.hidden = false;
  strip.innerHTML = issues
    .map(({ provider: p, status, message }) => {
      const pid = escapeHtml(p.provider_id);
      const isErr = status === "auth_expired" || status === "schema_changed" || status === "failed";
      let actionHtml = `<button type="button" class="btn btn-secondary btn-sm" data-alert-config="${pid}">前往检查</button>`;
      if (["auth_expired", "needs_manual"].includes(status) && p.supports_browser_auth) {
        actionHtml = `<button type="button" class="btn btn-primary btn-sm" data-auth-browser="${pid}">去浏览器授权</button>`;
      } else if (["auth_expired", "needs_manual", "unavailable"].includes(status)) {
        actionHtml = `<button type="button" class="btn btn-primary btn-sm" data-alert-config="${pid}">去配置凭据</button>`;
      } else if (status === "failed") {
        actionHtml = `<button type="button" class="btn btn-secondary btn-sm" data-run-provider="${pid}">重试</button>`;
      }
      return `
        <div class="alert-item${isErr ? " is-error" : ""}">
          <div class="alert-item-copy">
            <span class="alert-item-title">${escapeHtml(p.display_name)}</span>
            <span class="alert-item-msg" title="${escapeHtml(message)}">${escapeHtml(message)}</span>
          </div>
          <div class="inline-actions">${actionHtml}</div>
        </div>
      `;
    })
    .join("");
}

function updateSelectionUi() {
  const providers = appState.providers;
  const toggleBtn = $("#btn-toggle-select-mode");
  const batchBar = $("#provider-batch-bar");
  const grid = $("#providers-grid");
  const multiTool = providers.length > 1;

  if (toggleBtn) {
    toggleBtn.hidden = !multiTool;
    toggleBtn.setAttribute("aria-pressed", selectionMode ? "true" : "false");
  }
  if (!multiTool) selectionMode = false;
  if (batchBar) batchBar.hidden = !selectionMode;
  if (grid) grid.classList.toggle("is-selecting", selectionMode);

  const count = selectedProviders.size;
  const total = providers.length;
  const countLabel = $("#selected-provider-count");
  if (countLabel) countLabel.textContent = `已选 ${count} / ${total}`;

  const selectAll = $("#select-all-providers");
  if (selectAll) {
    const enabled = providers.filter((p) => p.enabled);
    const baseline = enabled.length ? enabled : providers;
    const baselineSelected = baseline.filter((p) => selectedProviders.has(p.provider_id)).length;
    selectAll.checked = baseline.length > 0 && baselineSelected === baseline.length;
    selectAll.indeterminate = baselineSelected > 0 && baselineSelected < baseline.length;
  }

  const runSelBtn = $("#btn-run-selected");
  const drySelBtn = $("#btn-dry-run-selected");
  if (runSelBtn && !appState.activeRun) {
    runSelBtn.textContent = count > 0 ? `运行已选 (${count})` : "运行已选";
  }
  if (drySelBtn && !appState.activeRun) {
    drySelBtn.textContent = count > 0 ? `预检已选 (${count})` : "预检已选";
  }

  $$("[data-provider-select]").forEach((checkbox) => {
    const checked = selectedProviders.has(checkbox.dataset.providerSelect);
    checkbox.checked = checked;
    checkbox
      .closest(".provider-card")
      ?.classList.toggle("selected", selectionMode && checked);
  });
}

function getSelectedProviderIds() {
  return appState.providers
    .filter((provider) => selectedProviders.has(provider.provider_id))
    .map((provider) => provider.provider_id);
}

function setRunButtonsBusy(busy) {
  [
    "#btn-run-all",
    "#btn-dry-run-all",
    "#btn-run-selected",
    "#btn-dry-run-selected",
    "#btn-refresh-overview",
    "#select-all-providers",
  ].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = busy;
  });
  $$("[data-run-provider], [data-dry-provider], [data-provider-select]").forEach(
    (button) => {
      button.disabled = busy;
    }
  );
}

function finishRun(triggerButton, originalHtml) {
  appState.activeRun = false;
  appState.historyDirty = true;
  runningProviderIds.clear();
  setRunButtonsBusy(false);
  if (triggerButton && triggerButton.isConnected && originalHtml !== undefined) {
    triggerButton.innerHTML = originalHtml;
  }
  updateSelectionUi();
}

let scheduleMetricRequest = null;
let latestScheduleSnapshot = null;

function setHeroStepState(el, state) {
  if (!el) return;
  el.classList.remove("is-done", "is-pending", "is-running");
  el.classList.add(`is-${state}`);
  const mark = el.querySelector(".step-mark");
  if (mark) {
    mark.textContent = state === "done" ? "✓" : state === "running" ? "◌" : "○";
  }
}

function updateAuxiaTelemetry(providers, schedule, todayDelta) {
  const list = providers || [];
  const enabled = list.filter((p) => p.enabled);
  const doneCount = enabled.filter((p) =>
    ["success", "already_done"].includes(p.today_record?.status)
  ).length;
  const maxStreak = list.reduce((best, p) => {
    const val = Number(p.today_record?.streak_days ?? p.latest_record?.streak_days ?? 0);
    return Number.isFinite(val) ? Math.max(best, val) : best;
  }, 0);

  const wfRun = $("#hero-wf-run");
  const wfLedger = $("#hero-wf-ledger");
  if (appState.activeRun) {
    setHeroStepState(wfRun, "running");
    setHeroStepState(wfLedger, "pending");
  } else if (enabled.length > 0 && doneCount === enabled.length) {
    setHeroStepState(wfRun, "done");
    setHeroStepState(wfLedger, "done");
  } else if (doneCount > 0) {
    setHeroStepState(wfRun, "running");
    setHeroStepState(wfLedger, "done");
  } else {
    setHeroStepState(wfRun, "pending");
    setHeroStepState(wfLedger, "pending");
  }

  const toolsEl = $("#hero-decision-tools");
  if (toolsEl) {
    toolsEl.textContent = `TOOLS · ${enabled.length}/${list.length} ENABLED`;
  }
  const schEl = $("#hero-decision-schedule");
  if (schEl) {
    schEl.textContent = schedule
      ? schedule.query_failed
        ? "SCHEDULER · CHECK NEEDED"
        : schedule.installed
          ? `SCHEDULER · ${String(schedule.state || "READY").toUpperCase()}`
          : "SCHEDULER · MANUAL MODE"
      : "SCHEDULER · SYNCING";
  }

  const vTitle = $("#hero-variant-title");
  const vSub = $("#hero-variant-sub");
  if (vTitle && vSub) {
    const deltaVal = Number(todayDelta) || 0;
    if (enabled.length > 0 && doneCount === enabled.length) {
      vTitle.textContent = `今日全部就绪 (+${deltaVal})`;
      vSub.textContent = maxStreak > 0
        ? `已完成 ${doneCount}/${enabled.length} 项工具 · 最高连签 ${maxStreak} 天`
        : `已完成 ${doneCount}/${enabled.length} 项工具 · 账本已同步`;
    } else if (enabled.length > 0) {
      vTitle.textContent = `${doneCount} / ${enabled.length} 已完成`;
      vSub.textContent = deltaVal > 0
        ? `今日累计 +${deltaVal} 积分 · 仍有工具待签`
        : "点击左侧发送或下方预检，自动核验会话并执行。";
    } else {
      vTitle.textContent = "等待启用工具";
      vSub.textContent = "前往「工具与设置」开启每日自动调度。";
    }
  }

  const matrix = $("#aux-decision-matrix");
  if (matrix && list.length) {
    const rows = list.slice(0, 4).map((p) => {
      const status = runningProviderIds.has(p.provider_id)
        ? "running"
        : p.today_record?.status || "pending";
      const code = escapeHtml(String(p.provider_id || "").toUpperCase());
      let decision = "✦ QUEUED FOR CHECK-IN";
      let pillClass = "";
      if (status === "running") {
        decision = "✦ EXECUTING AGENT";
        pillClass = " is-active";
      } else if (status === "success" || status === "already_done") {
        const d = Number(p.today_record?.delta) || 0;
        decision = d > 0 ? `✓ CREDITED +${d}` : "✓ IDEMPOTENT SKIP";
        pillClass = " is-ok";
      } else if (ACTION_NEEDED_STATUSES.has(status)) {
        decision = "⚠ ACTION REQUIRED";
        pillClass = " is-warn";
      } else if (!p.enabled) {
        decision = "○ SCHEDULE PAUSED";
        pillClass = " is-muted";
      }
      return `
        <div class="aux-matrix-row">
          <span class="aux-matrix-event">${code} · ${p.enabled ? "AUTO-SCHEDULE" : "MANUAL ONLY"}</span>
          <span class="aux-matrix-line" aria-hidden="true"></span>
          <span class="aux-matrix-pill${pillClass}">${escapeHtml(decision)}</span>
        </div>
      `;
    });
    rows.push(`
      <div class="aux-matrix-row">
        <span class="aux-matrix-event">SQLITE WAL · DAILY SNAPSHOT</span>
        <span class="aux-matrix-line" aria-hidden="true"></span>
        <span class="aux-matrix-pill is-active">✦ LEDGER PROTECTED</span>
      </div>
    `);
    matrix.innerHTML = rows.join("");
  }
}

function applyScheduleMetrics(sch) {
  latestScheduleSnapshot = sch;
  const failed = Boolean(sch?.query_failed);
  const state = String(sch?.state || "Ready");
  const healthy = ["ready", "running", "queued"].includes(state.toLowerCase());
  const schVal = $("#metric-schedule");
  if (schVal) {
    if (failed) schVal.textContent = "查询失败";
    else schVal.textContent = sch?.installed
      ? `已启用 (${state})`
      : "未安装";
  }
  const schChip = $("#metric-schedule-chip");
  if (schChip) {
    if (failed) schChip.textContent = "待检查";
    else schChip.textContent = sch?.installed
      ? healthy
        ? "运行中"
        : "需关注"
      : "待配置";
  }
  const schSub = $("#metric-schedule-sub");
  if (schSub) {
    if (failed) {
      schSub.textContent = "计划任务状态查询失败，请到设置页重试 →";
    } else {
      schSub.textContent =
        sch?.installed && sch.next_run_time
          ? `下次运行：${formatDateTime(sch.next_run_time)}`
          : "点击前往设置计划任务 →";
    }
  }
  const schDecision = $("#hero-decision-schedule");
  if (schDecision) {
    schDecision.textContent = failed
      ? "SCHEDULER · CHECK NEEDED"
      : sch?.installed
        ? `SCHEDULER · ${state.toUpperCase()}`
        : "SCHEDULER · MANUAL MODE";
  }
}

function loadScheduleMetric() {
  if (scheduleMetricRequest) return;
  scheduleMetricRequest = apiFetch("/api/schedule")
    .then((sch) => applyScheduleMetrics(sch))
    .catch(() => {
      const schVal = $("#metric-schedule");
      if (schVal) schVal.textContent = "读取失败";
      const schChip = $("#metric-schedule-chip");
      if (schChip) schChip.textContent = "待配置";
    })
    .finally(() => {
      scheduleMetricRequest = null;
    });
}

export async function loadOverview(silent = false) {
  try {
    const data = await apiFetch("/api/overview");
    if (
      silent &&
      document.body.dataset.tab === "overview" &&
      (document.querySelector(".more-menu[open]") || selectionMode)
    ) {
      return;
    }

    animateNumber($("#metric-today-delta"), data.today_delta ?? 0, "+");

    const providers = data.providers || [];
    appState.providers = providers;
    appState.trend30d = data.trend_30d || [];

    renderAlertStrip(providers);

    const enabledProviders = providers.filter((p) => p.enabled);
    const doneCount = enabledProviders.filter((p) =>
      ["success", "already_done"].includes(p.today_record?.status)
    ).length;
    const compChip = $("#metric-completion-chip");
    if (compChip) {
      compChip.textContent = enabledProviders.length
        ? `${doneCount} / ${enabledProviders.length} 已完成`
        : "暂无启用工具";
    }

    if (data.schedule) {
      applyScheduleMetrics(data.schedule);
      if (data.schedule_stale) loadScheduleMetric();
    } else {
      const schVal = $("#metric-schedule");
      if (schVal) schVal.textContent = "查询中...";
      const schChip = $("#metric-schedule-chip");
      if (schChip) schChip.textContent = "同步中";
      loadScheduleMetric();
    }

    const updatedLabel = $("#overview-updated-at");
    if (updatedLabel) {
      updatedLabel.textContent = `同步于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    }

    const availableIds = new Set(providers.map((p) => p.provider_id));
    for (const providerId of Array.from(selectedProviders)) {
      if (!availableIds.has(providerId)) selectedProviders.delete(providerId);
    }
    if (!selectionInitialized && providers.length) {
      providers.forEach((provider) => {
        if (provider.enabled) selectedProviders.add(provider.provider_id);
      });
      selectionInitialized = true;
    }

    const grid = $("#providers-grid");
    if (grid) {
      grid.innerHTML = providers
        .map((p) => {
          const todayRec = p.today_record;
          const rec = p.latest_record;
          const streak = todayRec?.streak_days ?? rec?.streak_days ?? "--";
          const delta = todayRec?.delta ? `+${todayRec.delta}` : "0";
          const msg =
            todayRec?.message ||
            (rec ? `最近记录 (${rec.biz_date})：${rec.message}` : "今日尚未执行签到");
          const lastStatus = todayRec?.last_status || todayRec?.status;
          const needsClient = [
            "auth_expired",
            "needs_manual",
            "schema_changed",
            "unavailable",
          ].includes(lastStatus);
          const expiring = todayRec?.expiring_soon ?? rec?.expiring_soon;
          const expiringAt = todayRec?.expiring_at ?? rec?.expiring_at;
          const expiryParts = [];
          if (expiring !== null && expiring !== undefined) expiryParts.push(`${expiring} 积分`);
          if (expiringAt) expiryParts.push(`${formatDateTime(expiringAt)} 到期`);
          const expiryHtml = expiryParts.length
            ? `<div class="provider-expiry-note">即将过期：${escapeHtml(expiryParts.join(" · "))}</div>`
            : "";

          const displayStatus = runningProviderIds.has(p.provider_id)
            ? "running"
            : todayRec?.status;
          const disabledTag = p.enabled
            ? ""
            : `<span class="provider-subtag">自动调度已停用</span>`;
          const pid = escapeHtml(p.provider_id);
          const isDone = ["success", "already_done"].includes(todayRec?.status);
          const authPrimary = needsClient && p.supports_browser_auth;
          const configPrimary = needsClient && !p.supports_browser_auth;
          const runPrimary = !isDone && !authPrimary && !configPrimary;

          return `
            <article class="provider-card ${p.enabled ? "" : "disabled-card"}" data-provider-card="${pid}">
              <div class="provider-head">
                <label class="provider-select" title="选择此工具">
                  <input
                    type="checkbox"
                    data-provider-select="${pid}"
                    aria-label="选择 ${escapeHtml(p.display_name)}"
                    ${selectedProviders.has(p.provider_id) ? "checked" : ""}
                  />
                </label>
                <span class="provider-monogram" aria-hidden="true">${escapeHtml(providerInitial(p.display_name))}</span>
                <div class="provider-head-copy">
                  <div class="provider-title-line">
                    <h3 class="provider-title">${escapeHtml(p.display_name)}</h3>
                    <span class="provider-slug">${escapeHtml(String(p.provider_id || "").toUpperCase())}</span>
                  </div>
                  ${disabledTag}
                </div>
                ${statusPill(displayStatus)}
              </div>

              <div class="provider-stats">
                <div class="stat-item"><span>今日收益</span><strong>${escapeHtml(delta)}</strong></div>
                <div class="stat-item"><span>连续天数</span><strong>${escapeHtml(streak)}</strong></div>
              </div>
              ${expiryHtml}

              <p class="provider-msg">${escapeHtml(msg)}</p>

              <div class="provider-actions">
                ${
                  authPrimary
                    ? `<button type="button" class="btn btn-primary btn-sm" data-auth-browser="${pid}">浏览器授权</button>`
                    : ""
                }
                ${
                  configPrimary
                    ? `<button type="button" class="btn btn-primary btn-sm" data-open-config="${pid}">去配置凭据</button>`
                    : ""
                }
                <button type="button" class="btn ${runPrimary ? "btn-primary" : "btn-ghost"} btn-sm" data-run-provider="${pid}">立即签到</button>
                <details class="more-menu">
                  <summary class="btn btn-ghost btn-sm more-trigger" aria-label="更多操作" title="更多操作">⋯</summary>
                  <div class="more-menu-list">
                    <button type="button" class="more-menu-item" data-dry-provider="${pid}">预检（不提交签到）</button>
                    ${
                      p.supports_browser_auth && !authPrimary
                        ? `<button type="button" class="more-menu-item" data-auth-browser="${pid}">浏览器授权</button>`
                        : ""
                    }
                    ${
                      needsClient || p.client_installed
                        ? `<button type="button" class="more-menu-item" data-launch-provider="${pid}">打开客户端</button>`
                        : ""
                    }
                    <button type="button" class="more-menu-item" data-open-config="${pid}">工具配置与体检</button>
                  </div>
                </details>
              </div>
            </article>
          `;
        })
        .join("");
    }

    updateSelectionUi();
    if (appState.activeRun) setRunButtonsBusy(true);

    const providerSelect = $("#history-provider");
    if (providerSelect) {
      const current = providerSelect.value;
      providerSelect.innerHTML =
        `<option value="">全部工具</option>` +
        providers
          .map(
            (p) =>
              `<option value="${escapeHtml(p.provider_id)}">${escapeHtml(p.display_name)}</option>`
          )
          .join("");
      providerSelect.value = current;
    }

    renderMiniTrendChart(appState.trend30d);
    if (document.body.dataset.tab === "history") {
      renderFullTrendChart(appState.trend30d);
    }
    updateAuxiaTelemetry(providers, data.schedule || latestScheduleSnapshot, data.today_delta ?? 0);
  } catch (err) {
    if (!silent) {
      showBanner(err.message, true);
    }
  }
}

/* ---- Live Execution Drawer & SSE ---- */

function openLiveDrawer(title) {
  const drawer = $("#live-drawer");
  const logBox = $("#live-drawer-log");
  const dot = $("#live-drawer-dot");
  const heading = $("#live-drawer-heading");
  if (!drawer || !logBox) return;

  logBox.textContent = "";
  if (heading) heading.textContent = title;
  if (dot) dot.className = "status-dot running";
  drawer.classList.remove("collapsed");
  const toggleBtn = $("#btn-drawer-toggle");
  if (toggleBtn) toggleBtn.textContent = "–";
  drawer.hidden = false;
  requestAnimationFrame(() => drawer.classList.add("open"));
}

function finalizeLiveDrawer(success, title) {
  const dot = $("#live-drawer-dot");
  const heading = $("#live-drawer-heading");
  if (dot) dot.className = `status-dot ${success ? "ok" : "err"}`;
  if (heading) heading.textContent = title;
}

function closeLiveDrawer() {
  const drawer = $("#live-drawer");
  if (!drawer) return;
  drawer.classList.remove("open");
  setTimeout(() => {
    if (!drawer.classList.contains("open")) drawer.hidden = true;
  }, 240);
}

function appendLogLine(level, text) {
  const container = $("#live-drawer-log");
  if (!container) return;
  const span = document.createElement("span");
  span.className = `log-line log-${level}`;
  span.textContent = `${text}\n`;
  container.appendChild(span);
  container.scrollTop = container.scrollHeight;
}

async function triggerRun(providerIds = null, dryRun = false, triggerButton = null) {
  const ids =
    providerIds == null
      ? null
      : Array.isArray(providerIds)
        ? providerIds
        : [providerIds];
  if (ids && ids.length === 0) {
    showBanner("请先选择至少一个工具。", true);
    return;
  }
  if (appState.activeRun) {
    showBanner("已有签到任务正在执行，请稍候。", true);
    return;
  }

  appState.activeRun = true;
  const originalHtml = triggerButton?.innerHTML;
  if (triggerButton) {
    triggerButton.textContent = dryRun ? "预检中..." : "签到中...";
  }
  const activeTargets = ids
    ? ids
    : appState.providers.filter((p) => p.enabled).map((p) => p.provider_id);
  activeTargets.forEach((id) => runningProviderIds.add(id));
  setRunButtonsBusy(true);
  updateAuxiaTelemetry(appState.providers, latestScheduleSnapshot, $("#metric-today-delta")?.dataset?.numVal ?? 0);

  const targetLabel = ids ? `${ids.length} 个工具` : "全部已启用工具";
  openLiveDrawer(dryRun ? `正在预检（${targetLabel}）` : `正在执行签到（${targetLabel}）`);

  let es = null;
  try {
    appendLogLine(
      "info",
      `[${new Date().toLocaleTimeString("zh-CN", { hour12: false })}] 开始任务（${targetLabel}）${dryRun ? " [Dry-Run]" : ""}...`
    );
    const { run_id } = await apiFetch("/api/runs", {
      method: "POST",
      body: JSON.stringify({ provider_ids: ids, dry_run: dryRun }),
    });

    es = new EventSource(`/api/runs/${encodeURIComponent(run_id)}/stream`, {
      withCredentials: true,
    });
    es.addEventListener("log", (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        const time = formatTime(payload.timestamp);
        const prefix = payload.provider_id ? `[${payload.provider_id}] ` : "";
        appendLogLine(payload.level || "info", `[${time}] ${prefix}${payload.message}`);
      } catch {
        appendLogLine("error", "[日志解析失败] 收到无法解析的实时日志。");
      }
    });
    es.addEventListener("done", () => {
      es.close();
      const doneMsg = dryRun ? "预检完成" : "签到流程执行完毕";
      showBanner(doneMsg);
      finalizeLiveDrawer(true, doneMsg);
      finishRun(triggerButton, originalHtml);
      loadOverview();
      callbacks.onRunComplete();
    });
    es.addEventListener("error", (ev) => {
      let serverMessage = null;
      if (ev.data) {
        try {
          serverMessage = JSON.parse(ev.data).message;
        } catch {
          serverMessage = null;
        }
      }
      if (serverMessage) {
        appendLogLine("error", `[错误] ${serverMessage}`);
        showBanner(serverMessage, true);
        finalizeLiveDrawer(false, "执行出错");
      } else {
        appendLogLine("error", "[连接中断] 实时日志连接已断开，请刷新页面或检查本地 Web 服务。");
        showBanner("实时日志连接中断", true);
        finalizeLiveDrawer(false, "连接中断");
      }
      es.close();
      finishRun(triggerButton, originalHtml);
      loadOverview();
    });
  } catch (err) {
    showBanner(err.message, true);
    finalizeLiveDrawer(false, "任务启动失败");
    finishRun(triggerButton, originalHtml);
  }
}

export async function launchClient(providerId) {
  try {
    const res = await apiFetch(`/api/providers/${encodeURIComponent(providerId)}/launch-client`, {
      method: "POST",
    });
    showBanner(res.message);
  } catch (err) {
    showBanner(err.message, true);
  }
}

export async function authBrowser(providerId, triggerButton = null) {
  if (appState.authBusy) {
    showBanner("浏览器授权正在进行中，请先完成当前登录。", true);
    return;
  }

  appState.authBusy = true;
  const buttons = $$("[data-auth-browser]");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  const originalText = triggerButton?.textContent;
  if (triggerButton) {
    triggerButton.textContent = "等待登录...";
  }
  showBanner("已打开 Microsoft Edge，请完成登录，最长等待约 3 分钟...");
  try {
    const start = await apiFetch(`/api/providers/${encodeURIComponent(providerId)}/auth-browser`, {
      method: "POST",
    });
    if (start.status === "success") {
      showBanner(start.message);
      loadOverview();
      return;
    }

    const deadline = Date.now() + 200000;
    while (Date.now() < deadline) {
      await sleep(2000);
      const status = await apiFetch(
        `/api/providers/${encodeURIComponent(providerId)}/auth-browser/status`
      );
      if (status.status === "success") {
        showBanner(status.message);
        loadOverview();
        return;
      }
      if (status.status === "error" || status.status === "idle") {
        throw new Error(status.message || "浏览器授权失败，请重试。");
      }
    }
    throw new Error("浏览器授权等待超时，请重试。");
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    appState.authBusy = false;
    buttons.forEach((button) => {
      button.disabled = false;
    });
    if (triggerButton && triggerButton.isConnected) {
      triggerButton.textContent = originalText;
    }
  }
}

export function initOverview(userCallbacks = {}) {
  callbacks = { ...callbacks, ...userCallbacks };

  // 使用事件委托处理卡片与待办条点击，避免每次轮询重复绑定监听器。
  $("#tab-overview")?.addEventListener("click", (ev) => {
    const runBtn = ev.target.closest("[data-run-provider]");
    if (runBtn) {
      triggerRun(runBtn.dataset.runProvider, false, runBtn);
      return;
    }
    const dryBtn = ev.target.closest("[data-dry-provider]");
    if (dryBtn) {
      triggerRun(dryBtn.dataset.dryProvider, true, dryBtn);
      return;
    }
    const authBtn = ev.target.closest("[data-auth-browser]");
    if (authBtn) {
      authBrowser(authBtn.dataset.authBrowser, authBtn);
      return;
    }
    const launchBtn = ev.target.closest("[data-launch-provider]");
    if (launchBtn) {
      launchClient(launchBtn.dataset.launchProvider);
      return;
    }
    const cfgBtn = ev.target.closest("[data-open-config], [data-alert-config]");
    if (cfgBtn) {
      const pid = cfgBtn.dataset.openConfig || cfgBtn.dataset.alertConfig;
      callbacks.onOpenToolConfig(pid);
    }
  });

  $("#providers-grid")?.addEventListener("change", (ev) => {
    const checkbox = ev.target.closest("[data-provider-select]");
    if (!checkbox) return;
    if (checkbox.checked) {
      selectedProviders.add(checkbox.dataset.providerSelect);
    } else {
      selectedProviders.delete(checkbox.dataset.providerSelect);
    }
    updateSelectionUi();
  });

  $("#btn-run-all")?.addEventListener("click", (ev) =>
    triggerRun(null, false, ev.currentTarget)
  );
  $("#btn-dry-run-all")?.addEventListener("click", (ev) =>
    triggerRun(null, true, ev.currentTarget)
  );
  $("#btn-refresh-overview")?.addEventListener("click", () => loadOverview());
  $("#btn-goto-history-trend")?.addEventListener("click", () => callbacks.onOpenHistory());

  $("#btn-toggle-select-mode")?.addEventListener("click", () => {
    selectionMode = !selectionMode;
    updateSelectionUi();
  });
  $("#btn-exit-select-mode")?.addEventListener("click", () => {
    selectionMode = false;
    updateSelectionUi();
  });
  $("#select-all-providers")?.addEventListener("change", (ev) => {
    selectedProviders.clear();
    if (ev.currentTarget.checked) {
      const enabled = appState.providers.filter((p) => p.enabled);
      const pool = enabled.length ? enabled : appState.providers;
      pool.forEach((p) => selectedProviders.add(p.provider_id));
    }
    updateSelectionUi();
  });
  const runSelected = (dryRun, triggerButton) => {
    const ids = getSelectedProviderIds();
    if (!ids.length) {
      showBanner("请先选择至少一个工具。", true);
      return;
    }
    triggerRun(ids, dryRun, triggerButton);
  };
  $("#btn-run-selected")?.addEventListener("click", (ev) => runSelected(false, ev.currentTarget));
  $("#btn-dry-run-selected")?.addEventListener("click", (ev) => runSelected(true, ev.currentTarget));

  // Live drawer controls
  $("#btn-drawer-toggle")?.addEventListener("click", () => {
    const drawer = $("#live-drawer");
    const btn = $("#btn-drawer-toggle");
    if (!drawer) return;
    const collapsed = drawer.classList.toggle("collapsed");
    if (btn) btn.textContent = collapsed ? "+" : "–";
  });
  $("#btn-drawer-close")?.addEventListener("click", closeLiveDrawer);
  $("#btn-drawer-expand")?.addEventListener("click", () => {
    const drawer = $("#live-drawer");
    const btn = $("#btn-drawer-expand");
    if (!drawer || !btn) return;
    drawer.classList.remove("collapsed");
    const toggle = $("#btn-drawer-toggle");
    if (toggle) toggle.textContent = "–";
    const expanded = drawer.classList.toggle("expanded");
    btn.textContent = expanded ? "收起" : "展开";
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
    const logBox = $("#live-drawer-log");
    if (logBox) logBox.scrollTop = logBox.scrollHeight;
  });
  $("#btn-drawer-copy")?.addEventListener("click", () => {
    copyText($("#live-drawer-log")?.textContent || "", "已复制执行日志");
  });
}
