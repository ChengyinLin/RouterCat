/* RouterCat - 轻量 Canvas 图表库（无第三方依赖）
 * 支持：多序列面积/折线、网格、时间轴、悬停十字线与 tooltip。
 */
(function () {
  const DPR = () => Math.min(window.devicePixelRatio || 1, 2);

  function niceStep(raw) {
    if (!isFinite(raw) || raw <= 0) return 1;
    const exp = Math.floor(Math.log10(raw));
    const base = Math.pow(10, exp);
    const n = raw / base;
    let mult;
    if (n <= 1) mult = 1;
    else if (n <= 2) mult = 2;
    else if (n <= 2.5) mult = 2.5;
    else if (n <= 5) mult = 5;
    else mult = 10;
    return mult * base;
  }

  const PALETTE = ["#4f8cff", "#2fd6a5", "#ffb340", "#a78bfa", "#ff5c66", "#38bdf8"];

  class LineChart {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.opts = Object.assign(
        {
          padding: { top: 14, right: 12, bottom: 22, left: 46 },
          yFormat: (v) => String(Math.round(v)),
          timeFormat: null,
          minRange: 10,
          fill: true,
          smooth: true,
          grid: 4,
          xTicks: 5,
          unit: "",
          legend: null,
        },
        opts
      );
      this.series = [];
      this.hoverX = null;
      this._bind();
    }

    _bind() {
      const c = this.canvas;
      c.addEventListener("mousemove", (e) => {
        const rect = c.getBoundingClientRect();
        this.hoverX = e.clientX - rect.left;
        this.draw();
      });
      c.addEventListener("mouseleave", () => {
        this.hoverX = null;
        this.draw();
        this._hideTip();
      });
      if (window.ResizeObserver) {
        this._ro = new ResizeObserver(() => this.draw());
        this._ro.observe(c.parentElement);
      } else {
        window.addEventListener("resize", () => this.draw());
      }
    }

    setData(series, opts = {}) {
      this.series = (series || []).map((s, i) => ({
        name: s.name || `系列 ${i + 1}`,
        color: s.color || PALETTE[i % PALETTE.length],
        points: (s.points || []).filter((p) => p && isFinite(p[0])),
        fill: s.fill !== undefined ? s.fill : this.opts.fill,
        dashed: !!s.dashed,
        scale: s.scale || 1,
      }));
      Object.assign(this.opts, opts);
      this._renderLegend();
      this.draw();
    }
    _renderLegend() {
      if (!this.opts.legend) return;
      const el = typeof this.opts.legend === "string" ? document.querySelector(this.opts.legend) : this.opts.legend;
      if (!el) return;
      el.innerHTML = this.series
        .map(
          (s) =>
            `<span class="lg"><i style="background:${s.color}"></i>${escapeHtml(s.name)}</span>`
        )
        .join("");
    }

    _tip() {
      if (!this._tipEl) {
        const div = document.createElement("div");
        div.className = "chart-tip";
        this.canvas.parentElement.appendChild(div);
        this._tipEl = div;
      }
      return this._tipEl;
    }

    _hideTip() {
      if (this._tipEl) this._tipEl.style.opacity = 0;
    }

    draw() {
      const canvas = this.canvas;
      const parent = canvas.parentElement;
      if (!parent) return;
      const w = Math.max(120, parent.clientWidth);
      const h = Math.max(80, canvas.clientHeight || parent.clientHeight || 180);
      const dpr = DPR();
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = w + "px";
      canvas.style.height = h + "px";
      const ctx = this.ctx;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const basePad = this.opts.padding;
      const all = [];
      this.series.forEach((s) => s.points.forEach((p) => all.push(p[0])));

      ctx.font = "11px ui-sans-serif, -apple-system, 'PingFang SC', sans-serif";

      let vmax = 0;
      this.series.forEach((s) =>
        s.points.forEach((p) => {
          if (p[1] != null && isFinite(p[1])) vmax = Math.max(vmax, p[1] * s.scale);
        })
      );
      const rawMax = Math.max(vmax, this.opts.minRange);
      const step = niceStep(rawMax / this.opts.grid);
      const top = step * this.opts.grid;

      // 依据 Y 轴标签实际宽度动态留白，避免 "12 MB/s" 被裁切
      const yLabels = [];
      for (let i = 0; i <= this.opts.grid; i++) yLabels.push(this.opts.yFormat(step * i));
      const maxLabelW = yLabels.reduce((m, s) => Math.max(m, ctx.measureText(s).width), 0);
      const pad = Object.assign({}, basePad, { left: Math.max(30, Math.ceil(maxLabelW) + 13) });

      const iw = w - pad.left - pad.right;
      const ih = h - pad.top - pad.bottom;
      if (iw < 20 || ih < 20) return;

      if (!all.length) {
        this._empty(ctx, w, h);
        return;
      }
      const dataMin = Math.min(...all);
      const dataMax = Math.max(...all);
      const dom = this.opts.domain;
      const t0 = dom ? Math.max(dom[0], dataMin) : dataMin;
      const t1 = Math.max(dom ? dom[1] : dataMax, dataMax);
      const span = Math.max(1, t1 - t0);

      const X = (t) => pad.left + ((t - t0) / span) * iw;
      const Y = (v) => pad.top + ih - (Math.max(0, Math.min(v, top)) / top) * ih;

      // 网格 + Y 轴
      ctx.textBaseline = "middle";
      for (let i = 0; i <= this.opts.grid; i++) {
        const v = step * i;
        const y = Y(v);
        ctx.beginPath();
        ctx.strokeStyle = "rgba(255,255,255,0.055)";
        ctx.lineWidth = 1;
        ctx.moveTo(pad.left, y + 0.5);
        ctx.lineTo(w - pad.right, y + 0.5);
        ctx.stroke();
        ctx.fillStyle = "rgba(148,163,184,0.62)";
        ctx.textAlign = "right";
        ctx.fillText(yLabels[i], pad.left - 8, y);
      }

      // X 轴标签（相同文案只画一次）
      const tf = this.opts.timeFormat || defaultTimeFmt(span);
      ctx.textBaseline = "top";
      ctx.fillStyle = "rgba(148,163,184,0.55)";
      let lastLabel = "";
      for (let i = 0; i < this.opts.xTicks; i++) {
        const t = t0 + (span * i) / (this.opts.xTicks - 1);
        const x = X(t);
        if (x < pad.left - 2 || x > w - pad.right + 2) continue;
        const label = tf(t);
        if (label === lastLabel) continue;
        lastLabel = label;
        ctx.textAlign = i === 0 ? "left" : i === this.opts.xTicks - 1 ? "right" : "center";
        ctx.fillText(label, x, h - pad.bottom + 7);
      }

      // 序列
      this.series.forEach((s) => {
        const pts = s.points
          .filter((p) => p[1] != null && isFinite(p[1]))
          .map((p) => ({ x: X(p[0]), y: Y(p[1] * s.scale), t: p[0], v: p[1] }));
        if (!pts.length) return;
        if (s.fill) {
          const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + ih);
          grad.addColorStop(0, hexA(s.color, 0.28));
          grad.addColorStop(1, hexA(s.color, 0.01));
          ctx.beginPath();
          path(ctx, pts, this.opts.smooth, true);
          ctx.lineTo(pts[pts.length - 1].x, pad.top + ih);
          ctx.lineTo(pts[0].x, pad.top + ih);
          ctx.closePath();
          ctx.fillStyle = grad;
          ctx.fill();
        }
        ctx.beginPath();
        path(ctx, pts, this.opts.smooth, false);
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 1.9;
        ctx.lineJoin = "round";
        if (s.dashed) ctx.setLineDash([5, 4]);
        ctx.stroke();
        ctx.setLineDash([]);
        s._pts = pts;
      });

      // 悬停
      if (this.hoverX != null) this._drawHover(ctx, pad, w, h, X, Y, t0, t1);

      // 零数据提示
      if (!this.series.some((s) => s._pts && s._pts.length)) this._empty(ctx, w, h);
    }

    _empty(ctx, w, h) {
      ctx.fillStyle = "rgba(148,163,184,0.35)";
      ctx.font = "12px ui-sans-serif, -apple-system, 'PingFang SC', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("正在采集数据…", w / 2, h / 2);
    }

    _drawHover(ctx, pad, w, h, X, Y, t0, t1) {
      const x = this.hoverX;
      if (x < pad.left || x > w - pad.right) return this._hideTip();
      const t = t0 + ((x - pad.left) / (w - pad.left - pad.right)) * (t1 - t0);
      let nearest = null;
      let bestDist = Infinity;
      const rows = [];
      this.series.forEach((s) => {
        if (!s._pts || !s._pts.length) return;
        let p = s._pts[0];
        for (const q of s._pts) if (Math.abs(q.t - t) < Math.abs(p.t - t)) p = q;
        if (Math.abs(p.t - t) < Math.abs(bestDist)) {
          bestDist = p.t - t;
          nearest = p;
        }
        rows.push({ s, p });
      });
      if (!nearest) return this._hideTip();

      ctx.beginPath();
      ctx.strokeStyle = "rgba(255,255,255,0.18)";
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.moveTo(nearest.x, pad.top);
      ctx.lineTo(nearest.x, h - pad.bottom);
      ctx.stroke();
      ctx.setLineDash([]);

      rows.forEach(({ s, p }) => {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3.2, 0, Math.PI * 2);
        ctx.fillStyle = s.color;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
        ctx.fillStyle = hexA(s.color, 0.18);
        ctx.fill();
      });

      const tip = this._tip();
      const dt = new Date(nearest.t * 1000);
      const hh = String(dt.getHours()).padStart(2, "0");
      const mm = String(dt.getMinutes()).padStart(2, "0");
      const ss = String(dt.getSeconds()).padStart(2, "0");
      tip.innerHTML =
        `<div class="tip-time">${hh}:${mm}:${ss}</div>` +
        rows
          .map(
            ({ s, p }) =>
              `<div class="tip-row"><i style="background:${s.color}"></i>${escapeHtml(s.name)}<b>${(
                this.opts.tipFormat || this.opts.yFormat
              )(p.v)}</b></div>`
          )
          .join("");
      tip.style.opacity = 1;
      const tw = tip.offsetWidth || 140;
      let left = nearest.x + 14;
      if (left + tw > w - 6) left = nearest.x - tw - 14;
      tip.style.left = Math.max(6, left) + "px";
      tip.style.top = Math.max(4, nearest.y - 20) + "px";
    }
  }

  function path(ctx, pts, smooth, moveTo) {
    if (!pts.length) return;
    if (!smooth || pts.length < 3) {
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      return;
    }
    if (moveTo) ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) {
      const prev = pts[i - 1];
      const cur = pts[i];
      const cx = (prev.x + cur.x) / 2;
      const cy = (prev.y + cur.y) / 2;
      ctx.quadraticCurveTo(prev.x, prev.y, cx, cy);
    }
    const last = pts[pts.length - 1];
    ctx.lineTo(last.x, last.y);
  }

  function defaultTimeFmt(span) {
    return (ts) => {
      const d = new Date(ts * 1000);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      if (span <= 900) return `${hh}:${mm}:${ss}`;
      if (span > 86400) return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`;
      return `${hh}:${mm}`;
    };
  }

  function hexA(hex, a) {
    if (!hex.startsWith("#")) return hex;
    let h = hex.slice(1);
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // 迷你走势线：只画线 + 渐变，无坐标轴
  function sparkline(canvas, points, color) {
    const parent = canvas.parentElement;
    if (!parent) return;
    const w = Math.max(40, parent.clientWidth);
    const h = Math.max(30, parent.clientHeight);
    const dpr = DPR();
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const vals = points.filter((p) => p[1] != null);
    if (vals.length < 2) return;
    const vmax = Math.max(...vals.map((p) => p[1]), 1);
    const vmin = Math.min(...vals.map((p) => p[1]), 0);
    const pad = 4;
    const X = (i) => (i / (vals.length - 1)) * (w - pad * 2) + pad;
    const Y = (v) => h - pad - ((v - vmin) / Math.max(1, vmax - vmin)) * (h - pad * 2);
    const pts = vals.map((p, i) => ({ x: X(i), y: Y(p[1]) }));
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, hexA(color, 0.3));
    grad.addColorStop(1, hexA(color, 0));
    ctx.beginPath();
    path(ctx, pts, true, true);
    ctx.lineTo(pts[pts.length - 1].x, h);
    ctx.lineTo(pts[0].x, h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    path(ctx, pts, true, false);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.stroke();
    // 末端光点
    const last = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(last.x, last.y, 2.6, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }

  window.RCChart = { LineChart, sparkline, PALETTE };
})();
