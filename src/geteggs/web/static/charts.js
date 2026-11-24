import { $, escapeHtml } from "./utils.js";

const signatures = new WeakMap();
const delegatedContainers = new WeakSet();

/**
 * 通用柱状趋势图渲染器，同时服务总览「近 14 天」与历史页「近 30 天」。
 */
function renderBarTrend({
  series,
  containerSelector,
  tooltipSelector,
  totalSelector,
  totalLabel,
  height,
  ariaLabel,
  fromCache = false,
}) {
  const container = $(containerSelector);
  const tooltip = $(tooltipSelector);
  const totalEl = $(totalSelector);
  if (!container) return;
  if (fromCache && !series) return;

  if (tooltip) {
    tooltip.classList.remove("visible");
    tooltip.setAttribute("aria-hidden", "true");
    tooltip.style.left = "0px";
    tooltip.style.top = "0px";
  }

  if (fromCache && container.clientWidth === 0) return;

  const sig = JSON.stringify(series);
  const unchanged = sig === signatures.get(container);
  signatures.set(container, sig);
  container.classList.toggle("no-anim", fromCache || unchanged);

  if (!series || !series.length) {
    if (totalEl) totalEl.textContent = `${totalLabel} +0`;
    container.innerHTML = `<p class="muted">暂无历史趋势数据。</p>`;
    return;
  }

  const totalSum = series.reduce((acc, s) => acc + (Number(s.delta) || 0), 0);
  if (totalEl) totalEl.textContent = `${totalLabel} +${totalSum}`;

  const w = Math.max(300, Math.round(container.clientWidth || 860));
  const h = height;
  const padX = 14;
  const padTop = 12;
  const padBottom = 22;
  const chartH = h - padTop - padBottom;
  const maxVal = Math.max(10, ...series.map((s) => s.delta || 0));
  const barW = (w - padX * 2) / series.length;
  const midY = padTop + chartH / 2;

  const bars = series
    .map((item, i) => {
      const val = Number(item.delta) || 0;
      const barH = val > 0 ? Math.max(5, (val / maxVal) * chartH) : 3;
      const rectW = Math.min(26, Math.max(5, barW - 5));
      const x = padX + i * barW + (barW - rectW) / 2;
      const y = padTop + chartH - barH;
      const isToday = i === series.length - 1;
      const fill = val > 0
        ? (isToday ? "var(--chart-bar-today, var(--accent))" : "var(--chart-bar, var(--accent-bar))")
        : "var(--chart-empty, var(--border))";
      const delay = Math.min(i * 6, 150);
      return `
        <rect
          class="trend-bar"
          tabindex="0"
          role="img"
          aria-label="${escapeHtml(item.date)}: +${val}"
          data-date="${escapeHtml(item.date)}"
          data-delta="${val}"
          x="${x.toFixed(1)}"
          y="${y.toFixed(1)}"
          width="${rectW.toFixed(1)}"
          height="${barH.toFixed(1)}"
          rx="4"
          fill="${fill}"
          style="--bar-delay:${delay}ms"
        />
      `;
    })
    .join("");

  const firstDate = escapeHtml(String(series[0]?.date ?? "").slice(5));
  const midDate = escapeHtml(String(series[Math.floor(series.length / 2)]?.date ?? "").slice(5));
  const lastDate = escapeHtml(String(series[series.length - 1]?.date ?? "").slice(5));

  container.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="group" aria-label="${escapeHtml(ariaLabel)}">
      <line x1="${padX}" y1="${padTop}" x2="${w - padX}" y2="${padTop}" stroke="var(--chart-grid, var(--border))" stroke-width="1" stroke-dasharray="3 4" />
      <line x1="${padX}" y1="${midY}" x2="${w - padX}" y2="${midY}" stroke="var(--chart-grid, var(--border))" stroke-width="1" stroke-dasharray="3 4" />
      <line x1="${padX}" y1="${padTop + chartH}" x2="${w - padX}" y2="${padTop + chartH}" stroke="var(--chart-axis-line, var(--border-strong))" stroke-width="1" />
      ${bars}
      <text x="${padX}" y="${h - 4}" fill="var(--chart-axis, var(--muted))" font-size="10.5" font-family="var(--mono)">${firstDate}</text>
      <text x="${w / 2}" y="${h - 4}" text-anchor="middle" fill="var(--chart-axis, var(--muted))" font-size="10.5" font-family="var(--mono)">${midDate}</text>
      <text x="${w - padX}" y="${h - 4}" text-anchor="end" fill="var(--chart-axis, var(--muted))" font-size="10.5" font-family="var(--mono)">${lastDate}</text>
    </svg>
  `;

  if (!tooltip || delegatedContainers.has(container)) return;
  delegatedContainers.add(container);
  const wrap = container.closest(".trend-chart-wrap");

  const showTip = (rect) => {
    if (!wrap) return;
    const wrapBox = wrap.getBoundingClientRect();
    const barBox = rect.getBoundingClientRect();
    const date = rect.dataset.date || "";
    const delta = Number(rect.dataset.delta) || 0;
    tooltip.textContent = `${date} · ${delta > 0 ? `+${delta} 积分` : "无新增"}`;
    const halfWidth = (tooltip.offsetWidth || 100) / 2;
    const rawLeft = barBox.left - wrapBox.left + barBox.width / 2;
    const minLeft = halfWidth + 8;
    const maxLeft = Math.max(minLeft, wrapBox.width - halfWidth - 8);
    const clampedLeft = Math.min(maxLeft, Math.max(minLeft, rawLeft));
    tooltip.style.left = `${clampedLeft}px`;
    tooltip.style.top = `${Math.max(4, barBox.top - wrapBox.top)}px`;
    tooltip.setAttribute("aria-hidden", "false");
    tooltip.classList.add("visible");
  };

  const hideTip = () => {
    tooltip.classList.remove("visible");
    tooltip.setAttribute("aria-hidden", "true");
    tooltip.style.left = "0px";
    tooltip.style.top = "0px";
  };

  container.addEventListener("pointerover", (ev) => {
    const bar = ev.target.closest?.(".trend-bar");
    if (bar) showTip(bar);
  });
  container.addEventListener("pointerout", (ev) => {
    const bar = ev.target.closest?.(".trend-bar");
    if (bar && !bar.contains(ev.relatedTarget)) hideTip();
  });
  container.addEventListener("focusin", (ev) => {
    const bar = ev.target.closest?.(".trend-bar");
    if (bar) showTip(bar);
  });
  container.addEventListener("focusout", (ev) => {
    const bar = ev.target.closest?.(".trend-bar");
    if (bar) hideTip();
  });
}

export function renderMiniTrendChart(series, fromCache = false) {
  const recent14 = Array.isArray(series) ? series.slice(-14) : series;
  renderBarTrend({
    series: recent14,
    containerSelector: "#trend-mini-chart",
    tooltipSelector: "#trend-mini-tooltip",
    totalSelector: "#trend-14d-total",
    totalLabel: "14 日合计",
    height: 92,
    ariaLabel: "近14天积分走势",
    fromCache,
  });
}

export function renderFullTrendChart(series, fromCache = false) {
  renderBarTrend({
    series,
    containerSelector: "#trend-chart",
    tooltipSelector: "#trend-tooltip",
    totalSelector: "#trend-total-pill",
    totalLabel: "30 日合计",
    height: 160,
    ariaLabel: "近30天积分趋势",
    fromCache,
  });

  const list = Array.isArray(series) ? series : [];
  const sum = list.reduce((acc, s) => acc + (Number(s.delta) || 0), 0);
  const activeDays = list.filter((s) => (Number(s.delta) || 0) > 0).length;
  const peak = list.reduce((best, s) => Math.max(best, Number(s.delta) || 0), 0);

  const heroEl = $("#trend-30d-hero");
  const activeEl = $("#trend-30d-active-days");
  const peakEl = $("#trend-30d-peak");
  if (heroEl) heroEl.textContent = `+${sum.toLocaleString("en-US")}`;
  if (activeEl) activeEl.textContent = `${activeDays} / ${list.length || 30}`;
  if (peakEl) peakEl.textContent = `+${peak}`;
}
