import { $, $$, appState } from "./utils.js";
import { renderFullTrendChart, renderMiniTrendChart } from "./charts.js";
import { authBrowser, initOverview, launchClient, loadOverview } from "./overview.js";
import {
  initHistory,
  loadActiveAuditTab,
  loadHistory,
  renderHistoryTrend,
} from "./history.js";
import {
  focusSection,
  focusTool,
  hasUnsavedSettings,
  initSettings,
  isSettingsFetched,
  loadMaintenanceInfo,
  loadSettings,
} from "./settings.js";

let activeTab = "overview";

function positionNavIndicator() {
  const nav = $(".nav");
  const indicator = $(".nav-indicator");
  const active = $(".nav-btn.active");
  if (!nav || !indicator || !active) return;
  if (active.offsetWidth === 0) return;
  indicator.style.width = `${active.offsetWidth}px`;
  indicator.style.transform = `translateX(${active.offsetLeft}px)`;
  indicator.classList.add("ready");
}

function closeMoreMenus() {
  $$(".more-menu[open]").forEach((m) => m.removeAttribute("open"));
}

function switchTab(tabName) {
  activeTab = tabName;
  document.body.dataset.tab = tabName;
  closeMoreMenus();

  $$(".nav-btn").forEach((b) => {
    const isActive = b.dataset.tab === tabName;
    b.classList.toggle("active", isActive);
    b.setAttribute("aria-selected", isActive ? "true" : "false");
    b.setAttribute("tabindex", isActive ? "0" : "-1");
  });
  $$(".tab-panel").forEach((p) => {
    p.classList.toggle("active", p.id === `tab-${tabName}`);
  });
  requestAnimationFrame(positionNavIndicator);

  if (tabName === "overview") {
    requestAnimationFrame(() => renderMiniTrendChart(appState.trend30d, true));
  }
  if (tabName === "history") {
    requestAnimationFrame(() => renderHistoryTrend(true));
  }
}

function activateTab(tab) {
  switchTab(tab);
  if (tab === "history") {
    if (appState.historyDirty) {
      appState.historyDirty = false;
      loadHistory(true);
    }
    loadActiveAuditTab();
  }
  if (tab === "settings") {
    if (!isSettingsFetched()) {
      loadSettings({ autoDoctor: true });
    } else {
      loadMaintenanceInfo();
    }
  }
}

/* ---- Tablist Keyboard Accessibility (WAI-ARIA Arrow Keys) ---- */

function wireTablistKeyboard(containerSelector, buttonSelector, onSelect) {
  const container = $(containerSelector);
  if (!container) return;
  container.addEventListener("keydown", (ev) => {
    const buttons = $$(buttonSelector);
    const idx = buttons.indexOf(document.activeElement);
    if (idx < 0) return;
    let nextIdx = -1;
    if (ev.key === "ArrowRight") nextIdx = (idx + 1) % buttons.length;
    else if (ev.key === "ArrowLeft") nextIdx = (idx - 1 + buttons.length) % buttons.length;
    else if (ev.key === "Home") nextIdx = 0;
    else if (ev.key === "End") nextIdx = buttons.length - 1;
    if (nextIdx < 0) return;

    ev.preventDefault();
    const target = buttons[nextIdx];
    target.focus();
    onSelect(target);
  });
}

$$(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

wireTablistKeyboard(".nav", ".nav-btn", (btn) => activateTab(btn.dataset.tab));
wireTablistKeyboard(".subtab-group", ".subtab-btn", (btn) => btn.click());

$$("[data-goto-tab]").forEach((el) => {
  const go = () => {
    const tab = el.dataset.gotoTab;
    const section = el.dataset.focusSection;
    activateTab(tab);
    if (tab === "settings" && section) {
      focusSection(section);
    }
  };
  el.addEventListener("click", go);
  el.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      go();
    }
  });
});

document.addEventListener("click", (ev) => {
  $$(".more-menu[open]").forEach((menu) => {
    if (!menu.contains(ev.target) || ev.target.closest(".more-menu-item")) {
      menu.removeAttribute("open");
    }
  });
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeMoreMenus();
});

window.addEventListener("resize", () => {
  positionNavIndicator();
  clearTimeout(window.__trendResizeTimer);
  window.__trendResizeTimer = setTimeout(() => {
    if (activeTab === "overview") renderMiniTrendChart(appState.trend30d, true);
    if (activeTab === "history") renderFullTrendChart(appState.trend30d, true);
  }, 150);
});

window.addEventListener("beforeunload", (ev) => {
  if (hasUnsavedSettings()) {
    ev.preventDefault();
    ev.returnValue = "";
  }
});

/* ---- Wire Modules & Boot ---- */

initOverview({
  onRunComplete: () => {
    if (activeTab === "history") {
      appState.historyDirty = false;
      loadHistory(true);
      loadActiveAuditTab();
    }
  },
  onOpenToolConfig: (providerId) => {
    activateTab("settings");
    focusTool(providerId);
  },
  onOpenHistory: () => {
    activateTab("history");
  },
});

initHistory();

initSettings({
  onOverviewRefresh: (silent = false) => loadOverview(silent),
  onHistoryRefresh: () => {
    if (activeTab === "history") {
      loadActiveAuditTab();
    }
  },
  onAuthBrowser: (providerId, btn) => authBrowser(providerId, btn),
  onLaunchClient: (providerId) => launchClient(providerId),
});

document.body.dataset.tab = "overview";
requestAnimationFrame(positionNavIndicator);
loadOverview();

setInterval(() => {
  if (!appState.activeRun && !appState.authBusy && document.visibilityState === "visible") {
    loadOverview(true);
  }
}, 30000);
