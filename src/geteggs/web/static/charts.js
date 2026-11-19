import { $, escapeHtml } from "./utils.js";

const signatures = new WeakMap();

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

  const sig = JSON.stringify(series);
  const unchanged = sig === signatures.get(container);
  signatures.set(container, sig);

  if (fromCache && container.clientWidth === 0) return;
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
      const fill = val > 0 ? (isToday ? "var(--accent-hover)" : "var(--accent)") : "var(--border)";
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

  const firstDate = escapeHtml(series[0].date.slice(5));
  const midDate = escapeHtml(series[Math.floor(series.length / 2)].date.slice(5));
  const lastDate = escapeHtml(series[series.length - 1].date.slice(5));

  container.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="${escapeHtml(ariaLabel)}">
      <line x1="${padX}" y1="${padTop}" x2="${w - padX}" y2="${padTop}" stroke="var(--border)" stroke-width="1" stroke-dasharray="3 4" />
      <line x1="${padX}" y1="${midY}" x2="${w - padX}" y2="${midY}" stroke="var(--border)" stroke-width="1" stroke-dasharray="3 4" />
      <line x1="${padX}" y1="${padTop + chartH}" x2="${w - padX}" y2="${padTop + chartH}" stroke="var(--border-strong)" stroke-width="1" />
      ${bars}
      <text x="${padX}" y="${h - 4}" fill="var(--muted)" font-size="11">${firstDate}</text>
      <text x="${w / 2}" y="${h - 4}" text-anchor="middle" fill="var(--muted)" font-size="11">${midDate}</text>
      <text x="${w - padX}" y="${h - 4}" text-anchor="end" fill="var(--muted)" font-size="11">${lastDate}</text>
    </svg>
  `;

  if (!tooltip) return;
  const wrap = container.closest(".trend-chart-wrap");

  const showTip = (rect) => {
    if (!wrap) return;
    const wrapBox = wrap.getBoundingClientRect();
    const barBox = rect.getBoundingClientRect();
    const date = rect.dataset.date;
    const delta = Number(rect.dataset.delta) || 0;
    tooltip.textContent = `${date} · ${delta > 0 ? `+${delta} 积分` : "无新增"}`;
    tooltip.style.left = `${barBox.left - wrapBox.left + barBox.width / 2}px`;
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

  container.querySelectorAll(".trend-bar").forEach((bar) => {
    bar.addEventListener("mouseenter", () => showTip(bar));
    bar.addEventListener("focus", () => showTip(bar));
    bar.addEventListener("mouseleave", hideTip);
    bar.addEventListener("blur", hideTip);
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
}
