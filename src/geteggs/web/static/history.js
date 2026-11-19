import {
  $,
  $$,
  REDUCED_MOTION,
  apiFetch,
  appState,
  escapeHtml,
  formatDateTime,
  formatTime,
  localTodayStr,
  providerDisplayName,
  showBanner,
  statusPill,
} from "./utils.js";
import { renderFullTrendChart } from "./charts.js";

const historyState = {
  offset: 0,
  limit: 100,
  selectedDate: null,
  loadedRecords: [],
  activeMonth: "",
  calendarItems: [],
  hasMore: false,
};

let activeAuditSubtab = "runs";
let openRunId = null;

function currentMonthLocal() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(monthStr, deltaMonths) {
  const [y, m] = (monthStr || currentMonthLocal()).split("-").map(Number);
  const dt = new Date(y, m - 1 + deltaMonths, 1);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}`;
}

function providerSupportsStreak(providerId) {
  const provider = appState.providers.find(
    (item) => item.provider_id === providerId
  );
  return provider ? provider.supports_streak !== false : true;
}

function streakText(record) {
  if (!providerSupportsStreak(record.provider_id)) return "不适用";
  return record.streak_days || "--";
}

function historyRowHtml(r) {
  return `
    <tr>
      <td>${escapeHtml(r.biz_date)}</td>
      <td>${escapeHtml(r.display_name)}</td>
      <td>${statusPill(r.status)}</td>
      <td>${r.delta > 0 ? `+${r.delta}` : "0"}</td>
      <td>${escapeHtml(streakText(r))}</td>
      <td>${escapeHtml(r.message)}</td>
    </tr>
  `;
}

function renderLedgerRows() {
  const tbody = $("#history-tbody");
  const chip = $("#history-date-filter-chip");
  const chipText = $("#history-date-filter-text");
  const countEl = $("#history-ledger-count");
  const statusEl = $("#history-loaded-status");
  const loadMoreBtn = $("#btn-load-more-history");
  if (!tbody) return;

  const filtered = historyState.selectedDate
    ? historyState.loadedRecords.filter((r) => r.biz_date === historyState.selectedDate)
    : historyState.loadedRecords;

  if (chip && chipText) {
    if (historyState.selectedDate) {
      chipText.textContent = `日期筛选：${historyState.selectedDate}`;
      chip.hidden = false;
    } else {
      chip.hidden = true;
    }
  }

  if (countEl) {
    countEl.textContent = `${filtered.length} 条`;
  }

  if (statusEl) {
    if (!historyState.loadedRecords.length) {
      statusEl.textContent = "";
    } else if (historyState.hasMore) {
      statusEl.textContent = `已加载 ${historyState.loadedRecords.length} 条`;
    } else {
      statusEl.textContent = `已显示本月全部 ${historyState.loadedRecords.length} 条记录`;
    }
  }
  if (loadMoreBtn) {
    loadMoreBtn.hidden = !historyState.hasMore;
  }

  const rows = filtered.map(historyRowHtml).join("");
  const emptyText = historyState.selectedDate
    ? `${historyState.selectedDate} 暂无签到流水`
    : "本月暂无签到记录";
  tbody.innerHTML = rows || `<tr><td colspan="6" class="muted">${escapeHtml(emptyText)}</td></tr>`;
}

function renderCalendar(monthStr, calItems) {
  const grid = $("#calendar-grid");
  if (!grid) return;
  const [y, m] = monthStr.split("-").map(Number);
  if (!y || !m) return;
  const byDate = Object.fromEntries(calItems.map((i) => [i.biz_date, i]));
  const firstDay = new Date(y, m - 1, 1).getDay();
  const daysInMonth = new Date(y, m, 0).getDate();
  const todayKey = localTodayStr();
  const weekdays = ["日", "一", "二", "三", "四", "五", "六"]
    .map((w) => `<div class="cal-weekday">周${w}</div>`)
    .join("");

  let cells = "";
  for (let i = 0; i < firstDay; i++) {
    cells += `<div class="cal-day empty"></div>`;
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const dateKey = `${monthStr}-${String(d).padStart(2, "0")}`;
    const item = byDate[dateKey];
    let cls = "";
    let badge = "";
    if (item) {
      cls = item.ok ? "ok" : "fail";
      badge = item.ok ? (item.delta > 0 ? `+${item.delta}` : "已签") : "异常";
    } else if (dateKey > todayKey) {
      cls = "future";
    }
    if (dateKey === todayKey) cls += " today";
    if (dateKey === historyState.selectedDate) cls += " selected-day";

    if (item) {
      cells += `
        <button type="button" class="cal-day ${cls.trim()}" data-cal-date="${escapeHtml(dateKey)}" title="${escapeHtml(dateKey)} ${escapeHtml(badge)}">
          <span class="cal-num">${d}</span>
          <span class="cal-delta">${escapeHtml(badge)}</span>
        </button>
      `;
    } else {
      cells += `
        <div class="cal-day ${cls.trim()}">
          <span class="cal-num">${d}</span>
          <span class="cal-delta"></span>
        </div>
      `;
    }
  }
  grid.innerHTML = weekdays + cells;
}

export async function loadHistory(reset = true) {
  const monthInput = $("#history-month");
  if (!monthInput) return;
  if (!monthInput.value) {
    monthInput.value = currentMonthLocal();
  }
  const month = monthInput.value;
  const provider = $("#history-provider")?.value || "";
  if (reset) {
    historyState.offset = 0;
    historyState.selectedDate = null;
    historyState.loadedRecords = [];
  }
  const params = new URLSearchParams({
    month,
    limit: String(historyState.limit),
    offset: String(historyState.offset),
  });
  if (provider) params.set("provider_id", provider);

  try {
    const data = await apiFetch(`/api/history?${params.toString()}`);
    const records = data.records || [];
    if (reset) {
      historyState.activeMonth = month;
      historyState.calendarItems = data.calendar || [];
      renderCalendar(month, historyState.calendarItems);
      historyState.loadedRecords = records;
    } else {
      historyState.loadedRecords = historyState.loadedRecords.concat(records);
    }
    historyState.offset += records.length;
    historyState.hasMore = records.length >= historyState.limit;
    renderLedgerRows();
  } catch (err) {
    appState.historyDirty = true;
    showBanner(err.message, true);
  }
}

const TRIGGER_LABELS = {
  scheduled: "定时任务",
  missed_catchup: "漏签补跑",
  manual_cli: "CLI 手动",
  manual_web: "Web 手动",
};

const RUN_STATUS_LABELS = {
  running: ["进行中", "pill-warn"],
  completed: ["已完成", "pill-ok"],
  error: ["异常", "pill-err"],
};

function formatRunDuration(startedAt, finishedAt) {
  if (!startedAt || !finishedAt) return "--";
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "--";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}

function formatRunSummary(r) {
  const total = Number(r.record_count) || 0;
  if (!total) return "--";
  if (r.dry_run) return `预检 ${total} 个工具`;
  const delta = Number(r.total_delta) || 0;
  return `${Number(r.ok_count) || 0}/${total} 成功${delta > 0 ? ` · +${delta}` : ""}`;
}

function closeOpenRunDetail() {
  $$("#runs-tbody tr.run-detail-row").forEach((tr) => tr.remove());
  $$("#runs-tbody [data-run-detail]").forEach((btn) => {
    btn.textContent = "详情";
    btn.setAttribute("aria-expanded", "false");
  });
  openRunId = null;
}

async function toggleRunDetail(runId, triggerBtn) {
  if (openRunId === runId) {
    closeOpenRunDetail();
    return;
  }
  closeOpenRunDetail();

  const row = triggerBtn.closest("tr");
  if (!row) return;
  openRunId = runId;
  triggerBtn.textContent = "收起";
  triggerBtn.setAttribute("aria-expanded", "true");

  const detailRow = document.createElement("tr");
  detailRow.className = "run-detail-row";
  detailRow.innerHTML = `<td colspan="6"><div class="muted">正在加载记录详情...</div></td>`;
  row.insertAdjacentElement("afterend", detailRow);

  try {
    const data = await apiFetch(`/api/runs/${encodeURIComponent(runId)}`);
    if (!detailRow.isConnected) return;
    const run = data.run || {};
    const records = data.records || [];
    const events = data.events || [];
    const [statusLabel, statusClass] =
      RUN_STATUS_LABELS[run.status] || [run.status, "pill-muted"];

    const recordRows =
      records
        .map(
          (r) => `
            <tr>
              <td>${escapeHtml(providerDisplayName(r.provider_id, r.display_name))}</td>
              <td>${statusPill(r.status)}</td>
              <td>${r.delta > 0 ? `+${r.delta}` : "0"}</td>
              <td>${escapeHtml(streakText(r))}</td>
              <td>${r.duration_ms ?? 0} ms</td>
              <td>${escapeHtml(r.message)}</td>
            </tr>
          `
        )
        .join("") || `<tr><td colspan="6" class="muted">本批次没有工具执行结果</td></tr>`;

    const logText =
      events
        .map((e) => {
          const prefix = e.provider_id ? `[${e.provider_id}] ` : "";
          return `[${formatTime(e.ts)}] ${String(e.level || "info").toUpperCase()} ${prefix}${e.message}`;
        })
        .join("\n") || "本批次没有持久化日志。";

    detailRow.innerHTML = `
      <td colspan="6">
        <div class="run-detail-inner">
          <div class="section-header compact">
            <div class="section-title-group">
              <h4 class="run-detail-heading">执行详情 <code>${escapeHtml(run.run_id || runId)}</code></h4>
              <span class="pill ${statusClass}">${escapeHtml(statusLabel)}</span>
              <span class="muted-meta">开始 ${escapeHtml(formatDateTime(run.started_at))} · 结束 ${escapeHtml(formatDateTime(run.finished_at))}</span>
            </div>
            <button type="button" class="btn btn-quiet btn-sm" data-close-run-detail>收起</button>
          </div>
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>工具</th>
                  <th>状态</th>
                  <th>变动</th>
                  <th>连签</th>
                  <th>耗时</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>${recordRows}</tbody>
            </table>
          </div>
          <p class="run-detail-log-title">执行日志</p>
          <div class="terminal-frame">
            <pre class="terminal">${escapeHtml(logText)}</pre>
          </div>
        </div>
      </td>
    `;
    detailRow.scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth", block: "nearest" });
  } catch (err) {
    if (detailRow.isConnected) {
      detailRow.innerHTML = `<td colspan="6">${escapeHtml(err.message)}</td>`;
    }
  }
}

async function loadRuns() {
  const tbody = $("#runs-tbody");
  if (!tbody) return;
  openRunId = null;
  try {
    const data = await apiFetch("/api/runs?limit=20");
    tbody.innerHTML =
      (data.runs || [])
        .map((r) => {
          const [statusLabel, statusClass] =
            RUN_STATUS_LABELS[r.status] || [r.status, "pill-muted"];
          return `
            <tr data-run-row="${escapeHtml(r.run_id)}">
              <td>${escapeHtml(formatDateTime(r.started_at))}</td>
              <td>${escapeHtml(TRIGGER_LABELS[r.trigger_type] || r.trigger_type)}</td>
              <td><span class="pill ${statusClass}">${escapeHtml(statusLabel)}</span></td>
              <td>${escapeHtml(formatRunSummary(r))}</td>
              <td>${escapeHtml(formatRunDuration(r.started_at, r.finished_at))}</td>
              <td><button type="button" class="btn btn-ghost btn-sm" aria-expanded="false" data-run-detail="${escapeHtml(r.run_id)}">详情</button></td>
            </tr>
          `;
        })
        .join("") || `<tr><td colspan="6" class="muted">暂无执行记录</td></tr>`;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6">${escapeHtml(err.message)}</td></tr>`;
  }
}

async function loadNotifications() {
  const tbody = $("#notifications-tbody");
  if (!tbody) return;
  try {
    const data = await apiFetch("/api/notifications?limit=50");
    const levelMap = {
      info: ["信息", "pill-muted"],
      warning: ["提醒", "pill-warn"],
      error: ["错误", "pill-err"],
    };
    tbody.innerHTML =
      (data.notifications || [])
        .map((n) => {
          const [levelLabel, levelClass] = levelMap[n.level] || [n.level, "pill-muted"];
          return `
            <tr>
              <td>${escapeHtml(formatDateTime(n.sent_at))}</td>
              <td>${escapeHtml(n.channel)}</td>
              <td><span class="pill ${levelClass}">${escapeHtml(levelLabel)}</span></td>
              <td>${escapeHtml(n.title)}</td>
              <td>${
                n.success
                  ? '<span class="pill pill-ok">成功</span>'
                  : '<span class="pill pill-err">失败</span>'
              }</td>
              <td>${escapeHtml(n.error_message || "--")}</td>
            </tr>
          `;
        })
        .join("") || `<tr><td colspan="6" class="muted">暂无通知记录</td></tr>`;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6">${escapeHtml(err.message)}</td></tr>`;
  }
}

export function loadActiveAuditTab() {
  if (activeAuditSubtab === "notifications") {
    return loadNotifications();
  }
  return loadRuns();
}

export function renderHistoryTrend(fromCache = true) {
  renderFullTrendChart(appState.trend30d, fromCache);
}

export function initHistory() {
  $("#calendar-grid")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-cal-date]");
    if (!btn) return;
    const clicked = btn.dataset.calDate;
    historyState.selectedDate = historyState.selectedDate === clicked ? null : clicked;
    renderCalendar(historyState.activeMonth, historyState.calendarItems);
    renderLedgerRows();
  });

  $("#runs-tbody")?.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-close-run-detail]")) {
      closeOpenRunDetail();
      return;
    }
    const detailBtn = ev.target.closest("[data-run-detail]");
    if (detailBtn) {
      toggleRunDetail(detailBtn.dataset.runDetail, detailBtn);
    }
  });

  $$("[data-history-subtab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.historySubtab;
      activeAuditSubtab = target;
      $$("[data-history-subtab]").forEach((b) => {
        const active = b.dataset.historySubtab === target;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", active ? "true" : "false");
        b.setAttribute("tabindex", active ? "0" : "-1");
      });
      $("#subtab-runs")?.classList.toggle("active", target === "runs");
      $("#subtab-notifications")?.classList.toggle("active", target === "notifications");
      loadActiveAuditTab();
    });
  });

  $("#btn-refresh-audit")?.addEventListener("click", loadActiveAuditTab);

  $("#btn-load-more-history")?.addEventListener("click", () => loadHistory(false));
  $("#history-provider")?.addEventListener("change", () => loadHistory(true));
  $("#history-month")?.addEventListener("change", () => loadHistory(true));
  $("#btn-prev-month")?.addEventListener("click", () => {
    const input = $("#history-month");
    if (!input) return;
    input.value = shiftMonth(input.value, -1);
    loadHistory(true);
  });
  $("#btn-next-month")?.addEventListener("click", () => {
    const input = $("#history-month");
    if (!input) return;
    input.value = shiftMonth(input.value, 1);
    loadHistory(true);
  });
  $("#btn-clear-date-filter")?.addEventListener("click", () => {
    historyState.selectedDate = null;
    renderCalendar(historyState.activeMonth, historyState.calendarItems);
    renderLedgerRows();
  });
}
