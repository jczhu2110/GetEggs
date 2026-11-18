const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDateTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value).slice(11, 19);
  }
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function showBanner(msg, isError = false) {
  const el = $("#toast-banner");
  el.textContent = msg;
  el.style.borderColor = isError ? "#f43f5e" : "#10b981";
  el.classList.add("show");
  clearTimeout(window.__bannerTimer);
  window.__bannerTimer = setTimeout(() => el.classList.remove("show"), 3600);
}

async function apiFetch(url, options = {}) {
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

function statusPill(status) {
  const map = {
    success: ["今日已签", "pill-ok"],
    already_done: ["今日已签", "pill-ok"],
    auth_expired: ["登录态失效", "pill-err"],
    needs_manual: ["需手动处理", "pill-warn"],
    schema_changed: ["接口可能改版", "pill-err"],
    unavailable: ["未就绪", "pill-warn"],
    failed: ["执行异常", "pill-err"],
  };
  const [label, cls] = map[status] || ["今日待签", "pill-muted"];
  return `<span class="pill ${cls}">${label}</span>`;
}

function switchTab(tabName) {
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tabName}`));
}

$$(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    switchTab(tab);
    if (tab === "history") {
      loadHistory(true);
      loadRuns();
    }
    if (tab === "settings") {
      loadSettings();
      loadNotifications();
    }
  });
});

function renderTrendChart(series) {
  const container = $("#trend-chart");
  if (!series || !series.length) {
    container.innerHTML = `<p class="muted">暂无历史趋势数据。</p>`;
    return;
  }
  const w = 840;
  const h = 150;
  const padX = 16;
  const padY = 18;
  const maxVal = Math.max(10, ...series.map((s) => s.delta || 0));
  const barW = (w - padX * 2) / series.length;

  const bars = series
    .map((item, i) => {
      const val = item.delta || 0;
      const barH = Math.max(2, ((val / maxVal) * (h - padY * 2)));
      const x = padX + i * barW + 2;
      const y = h - padY - barH;
      const fill = val > 0 ? "var(--accent)" : "var(--border)";
      return `<g>
        <rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(4, barW - 4).toFixed(1)}" height="${barH.toFixed(1)}" rx="3" fill="${fill}">
          <title>${escapeHtml(item.date)}: +${val}</title>
        </rect>
      </g>`;
    })
    .join("");

  const firstDate = escapeHtml(series[0].date.slice(5));
  const lastDate = escapeHtml(series[series.length - 1].date.slice(5));
  container.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="150" role="img" aria-label="30天积分趋势">
      <line x1="${padX}" y1="${h - padY}" x2="${w - padX}" y2="${h - padY}" stroke="var(--border)" stroke-width="1" />
      ${bars}
      <text x="${padX}" y="${h - 4}" fill="var(--muted)" font-size="11">${firstDate}</text>
      <text x="${w - padX}" y="${h - 4}" text-anchor="end" fill="var(--muted)" font-size="11">${lastDate}</text>
    </svg>
  `;
}

async function loadOverview(silent = false) {
  try {
    const data = await apiFetch("/api/overview");
    $("#biz-date-pill").textContent = `今日 ${data.biz_date}`;
    $("#metric-balance").textContent = data.total_balance ?? 0;
    $("#metric-today-delta").textContent = `+${data.today_delta ?? 0}`;
    $("#metric-schedule").textContent = data.schedule?.installed
      ? `已启用 (${data.schedule.state || "Ready"})`
      : "未安装";

    providersState = data.providers || [];
    const availableIds = new Set(
      providersState.map((provider) => provider.provider_id)
    );
    for (const providerId of Array.from(selectedProviders)) {
      if (!availableIds.has(providerId)) selectedProviders.delete(providerId);
    }
    if (!selectionInitialized && providersState.length) {
      providersState.forEach((provider) => {
        if (provider.enabled) selectedProviders.add(provider.provider_id);
      });
      selectionInitialized = true;
    }

    const grid = $("#providers-grid");
    grid.innerHTML = data.providers
      .map((p) => {
        const todayRec = p.today_record;
        const rec = p.latest_record;
        const bal = todayRec?.balance ?? rec?.balance ?? "--";
        const streak = todayRec?.streak_days ?? rec?.streak_days ?? "--";
        const delta = todayRec?.delta ? `+${todayRec.delta}` : "0";
        const msg = todayRec?.message || (rec ? `最近记录(${rec.biz_date})：${rec.message}` : "今日尚未执行签到");
        const lastStatus = todayRec?.last_status || todayRec?.status;
        const needsClient = ["auth_expired", "needs_manual", "schema_changed", "unavailable"].includes(lastStatus);
        const expiring = todayRec?.expiring_soon ?? rec?.expiring_soon;
        const expiringAt = todayRec?.expiring_at ?? rec?.expiring_at;
        const expiryParts = [];
        if (expiring !== null && expiring !== undefined) expiryParts.push(String(expiring));
        if (expiringAt) expiryParts.push(formatDateTime(expiringAt));
        const expiryHtml = expiryParts.length
          ? `<div class="stat-item"><span>即将过期</span><strong>${escapeHtml(expiryParts.join(" / "))}</strong></div>`
          : "";

        return `
          <article class="provider-card">
            <div class="provider-head">
              <label class="provider-select" title="选择此工具">
                <input
                  type="checkbox"
                  data-provider-select="${escapeHtml(p.provider_id)}"
                  aria-label="选择 ${escapeHtml(p.display_name)}"
                  ${selectedProviders.has(p.provider_id) ? "checked" : ""}
                />
              </label>
              <div class="provider-head-copy">
                <h3 class="provider-title">${escapeHtml(p.display_name)}</h3>
                <p class="provider-desc">${escapeHtml(p.description)}</p>
              </div>
              ${statusPill(todayRec?.status)}
            </div>

            <div class="provider-stats">
              <div class="stat-item"><span>当前余额</span><strong>${escapeHtml(bal)}</strong></div>
              <div class="stat-item"><span>今日收益</span><strong>${escapeHtml(delta)}</strong></div>
              <div class="stat-item"><span>连续天数</span><strong>${escapeHtml(streak)}</strong></div>
              ${expiryHtml}
            </div>

            <p class="provider-msg">${escapeHtml(msg)}</p>

            <div class="provider-actions">
              <button class="btn btn-primary" data-run-provider="${escapeHtml(p.provider_id)}">立即签到</button>
              <button class="btn btn-ghost" data-dry-provider="${escapeHtml(p.provider_id)}">预检</button>
              ${
                p.supports_browser_auth
                  ? `<button class="btn btn-ghost" data-auth-browser="${escapeHtml(p.provider_id)}">浏览器授权</button>`
                  : ""
              }
              ${
                needsClient || p.client_installed
                  ? `<button class="btn btn-ghost" data-launch-provider="${escapeHtml(p.provider_id)}">打开客户端</button>`
                  : ""
              }
            </div>
          </article>
        `;
      })
      .join("");

    $$("[data-run-provider]").forEach((btn) =>
      btn.addEventListener("click", () => triggerRun(btn.dataset.runProvider, false, btn))
    );
    $$("[data-dry-provider]").forEach((btn) =>
      btn.addEventListener("click", () => triggerRun(btn.dataset.dryProvider, true, btn))
    );
    $$("[data-auth-browser]").forEach((btn) =>
      btn.addEventListener("click", () => authBrowser(btn.dataset.authBrowser, btn))
    );
    $$("[data-launch-provider]").forEach((btn) =>
      btn.addEventListener("click", () => launchClient(btn.dataset.launchProvider))
    );
    $$("[data-provider-select]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          selectedProviders.add(checkbox.dataset.providerSelect);
        } else {
          selectedProviders.delete(checkbox.dataset.providerSelect);
        }
        updateSelectionSummary();
      });
    });
    syncProviderSelection();

    const providerSelect = $("#history-provider");
    if (providerSelect) {
      const current = providerSelect.value;
      providerSelect.innerHTML =
        `<option value="">全部工具</option>` +
        data.providers
          .map(
            (p) =>
              `<option value="${escapeHtml(p.provider_id)}">${escapeHtml(p.display_name)}</option>`
          )
          .join("");
      providerSelect.value = current;
    }

    renderTrendChart(data.trend_30d);
  } catch (err) {
    if (!silent) {
      showBanner(err.message, true);
    }
  }
}

function appendLogLine(level, text) {
  const term = $("#sse-terminal");
  if (term.textContent === "等待执行任务...") {
    term.textContent = "";
  }
  const span = document.createElement("span");
  span.className = `log-${level}`;
  span.textContent = `${text}\n`;
  term.appendChild(span);
  term.scrollTop = term.scrollHeight;
}

let activeRun = false;
let authBusy = false;
let scheduleBusy = false;
let settingsLoaded = false;
let providersState = [];
let selectionInitialized = false;
const selectedProviders = new Set();
const PLANNED_TOOLS = [
  {
    provider_id: "trae",
    display_name: "Trae",
    description: "AI 编程工具接入位，适配器尚未实现。",
  },
];

function providerInitial(name) {
  return (name || "?").trim().slice(0, 1).toUpperCase();
}

function updateSelectionSummary() {
  const count = selectedProviders.size;
  const total = providersState.length;
  const countLabel = $("#selected-provider-count");
  if (countLabel) countLabel.textContent = `已选 ${count} / ${total}`;
  const selectAll = $("#select-all-providers");
  if (selectAll) {
    selectAll.checked = total > 0 && count === total;
    selectAll.indeterminate = count > 0 && count < total;
  }
  $$("[data-provider-select]").forEach((checkbox) => {
    checkbox
      .closest(".provider-card")
      ?.classList.toggle("selected", checkbox.checked);
  });
}

function syncProviderSelection() {
  $$("[data-provider-select]").forEach((checkbox) => {
    checkbox.checked = selectedProviders.has(checkbox.dataset.providerSelect);
  });
  updateSelectionSummary();
}

function getSelectedProviderIds() {
  return providersState
    .filter((provider) => selectedProviders.has(provider.provider_id))
    .map((provider) => provider.provider_id);
}

function renderToolCatalog(providers) {
  const grid = $("#tool-catalog-grid");
  if (!grid) return;

  const available = (providers || [])
    .map(
      (provider) => `
        <label class="tool-card ${provider.enabled ? "enabled" : ""}">
          <input type="checkbox" data-tool-toggle="${escapeHtml(provider.provider_id)}" ${provider.enabled ? "checked" : ""} />
          <span class="tool-icon">${escapeHtml(providerInitial(provider.display_name))}</span>
          <span class="tool-copy">
            <strong>${escapeHtml(provider.display_name)}</strong>
            <span>${escapeHtml(provider.description)}</span>
          </span>
          <span class="pill ${provider.enabled ? "pill-ok" : "pill-muted"}">${provider.enabled ? "已启用" : "已停用"}</span>
        </label>
      `
    )
    .join("");

  const planned = PLANNED_TOOLS.map(
    (tool) => `
      <div class="tool-card disabled">
        <input type="checkbox" disabled />
        <span class="tool-icon muted-icon">${escapeHtml(providerInitial(tool.display_name))}</span>
        <span class="tool-copy">
          <strong>${escapeHtml(tool.display_name)}</strong>
          <span>${escapeHtml(tool.description)}</span>
        </span>
        <span class="pill pill-muted">即将支持</span>
      </div>
    `
  ).join("");

  grid.innerHTML =
    available + planned || `<p class="muted">暂无可用工具。</p>`;

  $$("[data-tool-toggle]").forEach((checkbox) => {
    checkbox.addEventListener("change", async () => {
      const providerId = checkbox.dataset.toolToggle;
      const enabled = checkbox.checked;
      const card = checkbox.closest(".tool-card");
      const name =
        card?.querySelector(".tool-copy strong")?.textContent || providerId;
      checkbox.disabled = true;
      try {
        await apiFetch(`/api/providers/${encodeURIComponent(providerId)}/config`, {
          method: "PUT",
          body: JSON.stringify({ enabled }),
        });
        const pill = card?.querySelector(".pill");
        if (pill) {
          pill.textContent = enabled ? "已启用" : "已停用";
          pill.className = `pill ${enabled ? "pill-ok" : "pill-muted"}`;
        }
        card?.classList.toggle("enabled", enabled);
        if (enabled) {
          selectedProviders.add(providerId);
        } else {
          selectedProviders.delete(providerId);
        }
        showBanner(`已${enabled ? "启用" : "停用"} ${name}`);
        loadOverview(true);
      } catch (err) {
        checkbox.checked = !enabled;
        showBanner(err.message, true);
      } finally {
        checkbox.disabled = false;
      }
    });
  });
}

function setSettingsButtonsEnabled(enabled) {
  [
    "#btn-install-schedule",
    "#btn-uninstall-schedule",
    "#btn-run-maintenance",
    "#form-global-settings button[type='submit']",
    "#form-workbuddy-settings button[type='submit']",
    "#form-qoder-settings button[type='submit']",
    "#btn-save-qoder-pat",
    "#btn-clear-qoder-pat",
  ].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = !enabled;
  });
}

function setRunButtonsBusy(busy) {
  [
    "#btn-run-all",
    "#btn-dry-run",
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

function finishRun(triggerButton, originalText) {
  activeRun = false;
  setRunButtonsBusy(false);
  if (triggerButton && triggerButton.isConnected) {
    triggerButton.textContent = originalText;
  }
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
  if (activeRun) {
    showBanner("已有签到任务正在执行，请稍候。", true);
    return;
  }

  activeRun = true;
  const originalText = triggerButton?.textContent;
  if (triggerButton) {
    triggerButton.textContent = dryRun ? "预检中..." : "签到中...";
  }
  setRunButtonsBusy(true);

  let es = null;
  try {
    switchTab("doctor");
    const targetLabel = ids ? `（${ids.length} 个工具）` : "（全部工具）";
    appendLogLine(
      "info",
      `[${new Date().toLocaleTimeString()}] 开始任务${targetLabel}${dryRun ? " (Dry-Run)" : ""}...`
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
      } catch (err) {
        appendLogLine("error", "[日志解析失败] 收到无法解析的实时日志。");
      }
    });
    es.addEventListener("done", () => {
      es.close();
      showBanner(dryRun ? "预检完成" : "签到流程执行完毕");
      finishRun(triggerButton, originalText);
      loadOverview();
      loadRuns();
    });
    es.addEventListener("error", (ev) => {
      let serverMessage = null;
      if (ev.data) {
        try {
          serverMessage = JSON.parse(ev.data).message;
        } catch (err) {
          serverMessage = null;
        }
      }
      if (serverMessage) {
        appendLogLine("error", `[错误] ${serverMessage}`);
        showBanner(serverMessage, true);
      } else {
        appendLogLine("error", "[连接中断] 实时日志连接已断开，请刷新页面或检查本地 Web 服务。");
        showBanner("实时日志连接中断", true);
      }
      es.close();
      finishRun(triggerButton, originalText);
      loadOverview();
    });
  } catch (err) {
    showBanner(err.message, true);
    finishRun(triggerButton, originalText);
  }
}

async function launchClient(providerId) {
  try {
    const res = await apiFetch(`/api/providers/${encodeURIComponent(providerId)}/launch-client`, {
      method: "POST",
    });
    showBanner(res.message);
  } catch (err) {
    showBanner(err.message, true);
  }
}

async function authBrowser(providerId, triggerButton = null) {
  if (authBusy) {
    showBanner("浏览器授权正在进行中，请先完成当前登录。", true);
    return;
  }

  authBusy = true;
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
    authBusy = false;
    buttons.forEach((button) => {
      button.disabled = false;
    });
    if (triggerButton && triggerButton.isConnected) {
      triggerButton.textContent = originalText;
    }
  }
}

async function runDoctor() {
  const button = $("#btn-run-doctor");
  if (button.disabled) return;
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "体检中...";

  const container = $("#doctor-results");
  container.innerHTML = `<div class="panel muted">正在检查本机客户端与会话凭证...</div>`;
  try {
    const { results } = await apiFetch("/api/doctor", { method: "POST" });
    container.innerHTML = results
      .map((r) => `
        <article class="doctor-card">
          <div class="provider-head">
            <div>
              <h3 class="provider-title">${escapeHtml(r.display_name)}</h3>
              <p class="provider-desc">账号标识: ${escapeHtml(r.account_label || "未识别")}</p>
            </div>
            <span class="pill ${r.healthy ? "pill-ok" : "pill-err"}">${r.healthy ? "健康就绪" : "需要关注"}</span>
          </div>
          <p class="provider-msg" style="margin-top:12px;">${escapeHtml(r.message)}</p>
          <div class="info-box">
            <div>客户端：${r.client_installed ? escapeHtml(r.client_exe_path) : "未检测到可执行文件"}</div>
            <div>登录态：${r.auth_file_found ? escapeHtml(r.auth_file_path) : "未找到 .info 文件"}</div>
            <div>令牌过期时间：${escapeHtml(formatDateTime(r.token_expires_at))}</div>
          </div>
        </article>
      `)
      .join("");
  } catch (err) {
    container.innerHTML = `<div class="panel">${escapeHtml(err.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

const historyState = { offset: 0, limit: 50 };

function currentMonthLocal() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function historyRowHtml(r) {
  return `
    <tr>
      <td>${escapeHtml(r.biz_date)}</td>
      <td>${escapeHtml(r.display_name)}</td>
      <td>${statusPill(r.status)}</td>
      <td>${r.delta > 0 ? `+${r.delta}` : "0"}</td>
      <td>${r.balance ?? "--"}</td>
      <td>${r.streak_days ?? "--"}</td>
      <td>${escapeHtml(r.message)}</td>
    </tr>
  `;
}

async function loadHistory(reset = true) {
  const monthInput = $("#history-month");
  if (!monthInput.value) {
    monthInput.value = currentMonthLocal();
  }
  const month = monthInput.value;
  const provider = $("#history-provider").value;
  if (reset) {
    historyState.offset = 0;
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
      renderCalendar(month, data.calendar || []);
      $("#history-tbody").innerHTML = "";
    }
    const rows = records.map(historyRowHtml).join("");
    if (reset) {
      $("#history-tbody").innerHTML =
        rows || `<tr><td colspan="7" class="muted">本月暂无签到记录</td></tr>`;
    } else if (rows) {
      $("#history-tbody").insertAdjacentHTML("beforeend", rows);
    }
    historyState.offset += records.length;
    $("#btn-load-more-history").disabled = records.length < historyState.limit;
  } catch (err) {
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

async function loadRuns() {
  const tbody = $("#runs-tbody");
  try {
    const data = await apiFetch("/api/runs?limit=20");
    tbody.innerHTML =
      (data.runs || [])
        .map((r) => {
          const [statusLabel, statusClass] =
            RUN_STATUS_LABELS[r.status] || [r.status, "pill-muted"];
          return `
            <tr>
              <td>${escapeHtml(formatDateTime(r.started_at))}</td>
              <td>${escapeHtml(TRIGGER_LABELS[r.trigger_type] || r.trigger_type)}</td>
              <td><span class="pill ${statusClass}">${escapeHtml(statusLabel)}</span></td>
              <td>${escapeHtml(formatDateTime(r.finished_at))}</td>
              <td><code>${escapeHtml(r.run_id)}</code></td>
              <td><button class="btn btn-ghost" data-run-detail="${escapeHtml(r.run_id)}">详情</button></td>
            </tr>
          `;
        })
        .join("") || `<tr><td colspan="6" class="muted">暂无执行批次</td></tr>`;

    $$("[data-run-detail]").forEach((button) => {
      button.addEventListener("click", () => loadRunDetail(button.dataset.runDetail));
    });
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6">${escapeHtml(err.message)}</td></tr>`;
  }
}

async function loadRunDetail(runId) {
  const panel = $("#run-detail");
  panel.style.display = "block";
  panel.innerHTML = `<div class="muted">正在加载批次详情...</div>`;
  try {
    const data = await apiFetch(`/api/runs/${encodeURIComponent(runId)}`);
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
              <td>${escapeHtml(r.provider_id)}</td>
              <td>${statusPill(r.status)}</td>
              <td>${r.delta > 0 ? `+${r.delta}` : "0"}</td>
              <td>${r.balance ?? "--"}</td>
              <td>${r.streak_days ?? "--"}</td>
              <td>${r.duration_ms ?? 0} ms</td>
              <td>${escapeHtml(r.message)}</td>
            </tr>
          `
        )
        .join("") || `<tr><td colspan="7" class="muted">本批次没有 Provider 结果</td></tr>`;

    const logText =
      events
        .map((e) => {
          const prefix = e.provider_id ? `[${e.provider_id}] ` : "";
          return `[${formatTime(e.ts)}] ${String(e.level || "info").toUpperCase()} ${prefix}${e.message}`;
        })
        .join("\n") || "本批次没有持久化日志。";

    panel.innerHTML = `
      <div class="section-header">
        <h3 style="margin:0;font-size:15px;">批次详情 <code>${escapeHtml(run.run_id || runId)}</code></h3>
        <button class="btn btn-ghost" id="btn-close-run-detail">关闭</button>
      </div>
      <div class="info-box">
        <div>触发方式：${escapeHtml(TRIGGER_LABELS[run.trigger_type] || run.trigger_type || "--")}</div>
        <div>批次状态：<span class="pill ${statusClass}">${escapeHtml(statusLabel)}</span></div>
        <div>开始时间：${escapeHtml(formatDateTime(run.started_at))}</div>
        <div>结束时间：${escapeHtml(formatDateTime(run.finished_at))}</div>
      </div>
      <h4>Provider 结果</h4>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>状态</th>
              <th>变动</th>
              <th>余额</th>
              <th>连签</th>
              <th>耗时</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>${recordRows}</tbody>
        </table>
      </div>
      <h4 style="margin-top:16px;">执行日志</h4>
      <pre class="terminal" style="min-height:120px; max-height:260px;">${escapeHtml(logText)}</pre>
    `;
    $("#btn-close-run-detail").addEventListener("click", () => {
      panel.style.display = "none";
      panel.innerHTML = "";
    });
  } catch (err) {
    panel.innerHTML = `<div>${escapeHtml(err.message)}</div>`;
  }
}

async function loadNotifications() {
  const tbody = $("#notifications-tbody");
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

function renderCalendar(monthStr, calItems) {
  const grid = $("#calendar-grid");
  const [y, m] = monthStr.split("-").map(Number);
  if (!y || !m) return;
  const byDate = Object.fromEntries(calItems.map((i) => [i.biz_date, i]));
  const firstDay = new Date(y, m - 1, 1).getDay();
  const daysInMonth = new Date(y, m, 0).getDate();
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
    }
    cells += `
      <div class="cal-day ${cls}">
        <span class="cal-num">${d}</span>
        <span class="cal-delta">${escapeHtml(badge)}</span>
      </div>
    `;
  }
  grid.innerHTML = weekdays + cells;
}

async function loadSettings() {
  settingsLoaded = false;
  setSettingsButtonsEnabled(false);
  try {
    const data = await apiFetch("/api/settings");
    const g = data.global;
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

    renderScheduleBox(data.schedule);

    providersState = data.providers || [];
    renderToolCatalog(providersState);

    const wb = (data.providers || []).find((p) => p.provider_id === "workbuddy");
    if (wb) {
      $("#wb-auth-path").value = wb.extra?.auth_file_path || "";
      $("#wb-exe-path").value = wb.extra?.client_exe_path || "";
      $("#wb-browser-state").value = wb.extra?.browser_state_path || "";
      $("#wb-base-url").value = wb.extra?.base_url || "";
      $("#wb-earliest-time").value = wb.earliest_time || "00:00";
      $("#wb-jitter-minutes").value = wb.extra?.jitter_override
        ? String(wb.jitter_minutes ?? 0)
        : "";
    }

    const qoder = (data.providers || []).find((p) => p.provider_id === "qoder");
    if (qoder) {
      $("#qoder-pat-file").value = qoder.extra?.auth_file_path || "";
      $("#qoder-exe-path").value = qoder.extra?.client_exe_path || "";
      $("#qoder-earliest-time").value = qoder.earliest_time || "00:00";
      $("#qoder-jitter-minutes").value = qoder.extra?.jitter_override
        ? String(qoder.jitter_minutes ?? 0)
        : "";
    }
    await loadQoderPatStatus();
    settingsLoaded = true;
    setSettingsButtonsEnabled(true);
    loadMaintenanceInfo();
  } catch (err) {
    showBanner(err.message, true);
  }
}

async function loadMaintenanceInfo() {
  const box = $("#maintenance-info");
  try {
    const info = await apiFetch("/api/maintenance");
    const counts = info.counts || {};
    box.innerHTML = [
      `数据库：${escapeHtml(formatBytes(info.db_size))}`,
      `签到流水：${counts.checkin_records ?? 0} 条；批次：${counts.checkin_runs ?? 0} 条；日志：${counts.run_events ?? 0} 条；通知：${counts.notifications ?? 0} 条`,
      `最近清理：${escapeHtml(formatDateTime(info.last_cleanup_at))}；最近 VACUUM：${escapeHtml(formatDateTime(info.last_vacuum_at))}；备份：${info.backup_count ?? 0} 份`,
    ].join("<br/>");
  } catch (err) {
    box.textContent = err.message;
  }
}

function renderScheduleBox(sch) {
  const box = $("#schedule-detail-box");
  if (!sch) return;
  if (!sch.supported) {
    box.textContent = sch.message;
    return;
  }
  box.innerHTML = sch.installed
    ? `状态：<strong>已安装 (${escapeHtml(sch.state || "Ready")})</strong><br/>上次运行：${escapeHtml(formatDateTime(sch.last_run_time))}<br/>下次计划：${escapeHtml(formatDateTime(sch.next_run_time))}`
    : `状态：<strong>未安装</strong>（点击下方按钮自动注册 Windows 计划任务）`;
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
    schedule_time: scheduleTime,
    jitter_minutes: jitterMinutes,
    timezone: "Asia/Shanghai",
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

$("#form-global-settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const payload = collectGlobalSettings();
    await apiFetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    showBanner("全局设置已保存；安装/更新计划任务会使用当前表单时间。");
  } catch (err) {
    showBanner(err.message, true);
  }
});

$("#form-workbuddy-settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!settingsLoaded) {
    showBanner("设置尚未加载完成，请稍候再操作。", true);
    return;
  }
  try {
    const payload = {
      auth_file_path: $("#wb-auth-path").value,
      client_exe_path: $("#wb-exe-path").value,
      browser_state_path: $("#wb-browser-state").value,
      base_url: $("#wb-base-url").value,
    };
    const earliest = $("#wb-earliest-time").value;
    if (earliest) payload.earliest_time = earliest;
    const jitterValue = $("#wb-jitter-minutes").value;
    if (jitterValue !== "") {
      payload.jitter_minutes = Number(jitterValue);
    } else {
      payload.jitter_override = false;
    }

    await apiFetch("/api/providers/workbuddy/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    showBanner("WorkBuddy 配置已保存");
    loadOverview();
  } catch (err) {
    showBanner(err.message, true);
  }
});

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
      el.textContent = "尚未配置 PAT。";
    }
  } catch (err) {
    el.textContent = `PAT 状态读取失败：${err.message}`;
  }
}

$("#btn-save-qoder-pat").addEventListener("click", async () => {
  const input = $("#qoder-pat-token");
  const pat = input.value.trim();
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
    loadOverview(true);
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#btn-clear-qoder-pat").addEventListener("click", async () => {
  const button = $("#btn-clear-qoder-pat");
  button.disabled = true;
  try {
    const data = await apiFetch("/api/providers/qoder/pat", {
      method: "DELETE",
    });
    showBanner(
      data.configured
        ? "已清除网页保存的 PAT，但仍有其他凭证来源生效。"
        : "已清除网页保存的 Qoder PAT"
    );
    await loadQoderPatStatus();
    loadOverview(true);
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#form-qoder-settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!settingsLoaded) {
    showBanner("设置尚未加载完成，请稍候再操作。", true);
    return;
  }
  try {
    const payload = {
      auth_file_path: $("#qoder-pat-file").value,
      client_exe_path: $("#qoder-exe-path").value,
    };
    const earliest = $("#qoder-earliest-time").value;
    if (earliest) payload.earliest_time = earliest;
    const jitterValue = $("#qoder-jitter-minutes").value;
    if (jitterValue !== "") {
      payload.jitter_minutes = Number(jitterValue);
      payload.jitter_override = true;
    } else {
      payload.jitter_override = false;
    }

    await apiFetch("/api/providers/qoder/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    showBanner("Qoder 配置已保存");
    loadOverview(true);
  } catch (err) {
    showBanner(err.message, true);
  }
});

$("#btn-run-all").addEventListener("click", (event) => triggerRun(null, false, event.currentTarget));
$("#btn-dry-run").addEventListener("click", (event) => triggerRun(null, true, event.currentTarget));
$("#btn-run-selected").addEventListener("click", (event) => {
  const ids = getSelectedProviderIds();
  if (!ids.length) {
    showBanner("请先选择至少一个工具。", true);
    return;
  }
  triggerRun(ids, false, event.currentTarget);
});
$("#btn-dry-run-selected").addEventListener("click", (event) => {
  const ids = getSelectedProviderIds();
  if (!ids.length) {
    showBanner("请先选择至少一个工具。", true);
    return;
  }
  triggerRun(ids, true, event.currentTarget);
});
$("#select-all-providers").addEventListener("change", (event) => {
  selectedProviders.clear();
  if (event.currentTarget.checked) {
    providersState.forEach((provider) =>
      selectedProviders.add(provider.provider_id)
    );
  }
  syncProviderSelection();
});
$("#btn-refresh-overview").addEventListener("click", () => loadOverview());
$("#btn-run-doctor").addEventListener("click", runDoctor);
$("#btn-clear-logs").addEventListener("click", () => {
  $("#sse-terminal").textContent = "等待执行任务...";
});
$("#btn-load-history").addEventListener("click", () => loadHistory(true));
$("#btn-load-more-history").addEventListener("click", () => loadHistory(false));
$("#history-provider").addEventListener("change", () => loadHistory(true));
$("#btn-refresh-runs").addEventListener("click", loadRuns);
$("#btn-refresh-notifications").addEventListener("click", loadNotifications);

$("#btn-test-toast").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (button.disabled) return;
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "发送中...";
  try {
    const res = await apiFetch("/api/notifiers/test", { method: "POST" });
    showBanner(res.message);
    loadNotifications();
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});

$("#btn-run-maintenance").addEventListener("click", async (event) => {
  if (!confirm("确定立即清理过期数据？会先自动备份数据库。")) {
    return;
  }
  const button = event.currentTarget;
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
    loadMaintenanceInfo();
    loadRuns();
    loadNotifications();
    loadOverview();
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});

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
    await apiFetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
    const res = await apiFetch("/api/schedule/install", { method: "POST" });
    renderScheduleBox(res.schedule);
    showBanner(res.message);
    loadOverview();
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
  if (!confirm("确定卸载 Windows 每日签到计划任务？卸载后将不再自动执行签到。")) {
    return;
  }
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
    loadOverview();
  } catch (err) {
    showBanner(err.message, true);
  } finally {
    scheduleBusy = false;
    installButton.disabled = false;
    uninstallButton.disabled = false;
    uninstallButton.textContent = originalText;
  }
}

$("#btn-install-schedule").addEventListener("click", installSchedule);
$("#btn-uninstall-schedule").addEventListener("click", uninstallSchedule);

$("#btn-shutdown-web").addEventListener("click", async (event) => {
  if (!confirm("确定关闭本地 Web 服务？已安装的 Windows 计划任务不受影响。")) {
    return;
  }
  const button = event.currentTarget;
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

setSettingsButtonsEnabled(false);
loadOverview();

setInterval(() => {
  if (!activeRun && !authBusy && document.visibilityState === "visible") {
    loadOverview(true);
  }
}, 30000);
