/* RouterCat - 前端应用逻辑 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const t = (key, vars) => window.RCI18n.t(key, vars);
  const state = {
    view: "overview",
    range: "30m",
    data: null,
    config: null,
    devices: [],
    events: [],
    eventFilter: "all",
    charts: {},
    rings: null,
    iface: null,
    lastTs: 0,
    failCount: 0,
    langChoice: "auto",
  };

  // ------------------------------------------------------------------ 工具
  const pad2 = (n) => String(n).padStart(2, "0");

  const fmt = {
    ms(v, unit = true) {
      if (v == null || !isFinite(v)) return "--";
      const s = v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : Math.round(v).toString();
      return unit ? `${s} ms` : s;
    },
    pct(v, digits = 1) {
      if (v == null || !isFinite(v)) return "--";
      return `${v.toFixed(digits)}%`;
    },
    bytes(b, digits = 1) {
      if (b == null || !isFinite(b)) return "--";
      if (b < 1024) return `${Math.round(b)} B`;
      const units = ["KB", "MB", "GB", "TB", "PB"];
      let v = b / 1024;
      let i = 0;
      while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
      }
      return `${v.toFixed(v >= 100 ? 0 : digits)} ${units[i]}`;
    },
    bps(b) {
      if (b == null || !isFinite(b)) return "--";
      return `${fmt.bytes(b)}/s`;
    },
    num(v, digits = 0) {
      if (v == null || !isFinite(v)) return "--";
      return v.toFixed(digits);
    },
    dur(sec) {
      if (!sec || sec < 0) return "--";
      const d = Math.floor(sec / 86400);
      const h = Math.floor((sec % 86400) / 3600);
      const m = Math.floor((sec % 3600) / 60);
      if (d) return t("{d} 天 {h} 小时", { d, h });
      if (h) return t("{h} 小时 {m} 分", { h, m });
      if (m) return t("{n} 分", { n: m });
      return t("{n} 秒", { n: Math.round(sec) });
    },
    clock(ts) {
      if (!ts) return "--";
      const d = new Date(ts * 1000);
      return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    },
    datetime(ts) {
      if (!ts) return "--";
      const d = new Date(ts * 1000);
      return `${d.getMonth() + 1}/${d.getDate()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    },
    ago(ts) {
      if (!ts) return "--";
      const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
      if (diff < 10) return t("刚刚");
      if (diff < 60) return t("{n} 秒前", { n: diff });
      if (diff < 3600) return t("{n} 分钟前", { n: Math.floor(diff / 60) });
      if (diff < 86400) return t("{n} 小时前", { n: Math.floor(diff / 3600) });
      return t("{n} 天前", { n: Math.floor(diff / 86400) });
    },
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  let toastTimer = null;
  function toast(msg, isErr) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.className = "toast"), 2400);
  }

  function setText(id, value) {
    const el = $(id);
    if (el && el.textContent !== String(value)) el.textContent = value;
  }

  function statusInfo(status) {
    if (status === "online") return { cls: "ok", text: t("在线") };
    if (status === "degraded") return { cls: "warn", text: t("不稳定") };
    if (status === "offline") return { cls: "bad", text: t("离线") };
    return { cls: "", text: t("未知") };
  }

  function lossClass(loss) {
    if (loss == null) return "t-dim";
    if (loss <= 0.5) return "t-ok";
    if (loss < 5) return "t-warn";
    return "t-bad";
  }

  // ------------------------------------------------------------------ 环形图
  function buildRings() {
    const box = $("rings");
    const defs = [
      { key: "cpu", label: "CPU" },
      { key: "mem", label: t("内存") },
      { key: "disk", label: t("磁盘") },
    ];
    box.innerHTML = defs
      .map(
        (d) => `
      <div class="ring" data-key="${d.key}">
        <svg viewBox="0 0 100 100">
          <circle class="track" cx="50" cy="50" r="42"></circle>
          <circle class="bar" cx="50" cy="50" r="42" pathLength="100" stroke-dasharray="100" stroke-dashoffset="100"></circle>
        </svg>
        <div class="ring-center"><b>--</b><span>${d.label}</span></div>
      </div>`
      )
      .join("");
    state.rings = {};
    box.querySelectorAll(".ring").forEach((el) => {
      state.rings[el.dataset.key] = {
        bar: el.querySelector(".bar"),
        num: el.querySelector("b"),
      };
    });
  }

  function ringColor(pct) {
    if (pct >= 85) return "#ff5c66";
    if (pct >= 65) return "#ffb340";
    return "#2fd6a5";
  }

  function updateRing(key, pct) {
    const r = state.rings && state.rings[key];
    if (!r) return;
    const v = Math.max(0, Math.min(100, pct || 0));
    r.bar.style.strokeDashoffset = String(100 - v);
    r.bar.style.stroke = ringColor(v);
    const txt = `${Math.round(v)}%`;
    if (r.num.textContent !== txt) r.num.textContent = txt;
  }

  // ------------------------------------------------------------------ 导航
  const VIEW_META = {
    overview: ["总览", "路由器与链路实时状态"],
    router: ["路由器", "网关连通性、延迟与丢包"],
    network: ["网络与主机", "网卡流量、磁盘与资源占用"],
    devices: ["设备发现", "局域网内已发现的终端"],
    speedtest: ["测网速", "下行/上行带宽与负载延迟"],
    events: ["事件与告警", "链路异常与恢复记录"],
    settings: ["设置", "监控目标、阈值与数据保留"],
  };

  function viewMeta(view) {
    const meta = VIEW_META[view] || ["", ""];
    return [t(meta[0]), t(meta[1])];
  }

  function switchView(view, updateHash = true) {
    state.view = view;
    if (updateHash && location.hash !== `#${view}`) {
      history.replaceState(null, "", `#${view}`);
    }
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
    const meta = viewMeta(view);
    setText("page-title", meta[0]);
    setText("page-sub", meta[1]);
    $("range-picker").style.visibility = ["overview", "router", "network"].includes(view) ? "visible" : "hidden";
    if (view === "devices") loadDevices();
    if (view === "events") loadEvents();
    if (view === "settings") loadSettings();
    if (view === "speedtest") loadSpeedTest();
    else stopSpeedPolling();
    setTimeout(() => {
      Object.values(state.charts).forEach((c) => c && c.draw());
      loadCharts();
    }, 30);
  }

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  document.body.addEventListener("click", (e) => {
    const t = e.target.closest("[data-goto]");
    if (t) switchView(t.dataset.goto);
  });

  $("range-picker").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-range]");
    if (!btn) return;
    state.range = btn.dataset.range;
    document.querySelectorAll("#range-picker button").forEach((b) => b.classList.toggle("active", b === btn));
    loadCharts();
  });

  // ------------------------------------------------------------------ 总览渲染
  function renderOverview(d) {
    const st = statusInfo(d.status);
    const pill = $("router-pill");
    pill.className = `pill ${st.cls}`;
    pill.innerHTML = `<i></i>${st.text}`;

    setText("gw-ip", d.router.ip || t("未探测"));
    const m5 = d.router_stats.m5 || {};
    setText("gw-latency", m5.avg == null ? "--" : fmt.ms(m5.avg, false));
    setText("gw-loss", fmt.pct(m5.loss, m5.loss >= 10 ? 0 : 1));
    setText("gw-jitter", m5.jitter == null ? "--" : fmt.ms(m5.jitter, false));
    setText("gw-p95", fmt.ms(m5.p95, false));
    setText("gw-count", t("{n} 次", { n: m5.n || 0 }));

    // 环形
    const host = d.host || {};
    const mem = host.mem || {};
    const disks = host.disks || [];
    const worstDisk = disks.reduce((a, b) => (b.pct > (a ? a.pct : -1) ? b : a), null);
    updateRing("cpu", host.cpu);
    updateRing("mem", mem.pct);
    updateRing("disk", worstDisk ? worstDisk.pct : 0);

    setText("host-uptime", host.boot_ts ? t("已运行 {t}", { t: fmt.dur(host.uptime) }) : "--");
    const load = host.load || [];
    setText("host-load", load.length ? load.map((v) => v.toFixed(2)).join(" / ") : "--");
    const temps = host.temps || [];
    setText("host-temp", temps.length ? `${Math.max.apply(null, temps.map((t) => t.value)).toFixed(0)}°C` : "--");
    setText("host-proc", worstDisk ? fmt.pct(worstDisk.pct, 0) : "--");

    // 侧栏状态
    const dot = $("side-status").querySelector(".dot");
    dot.className = "dot " + (st.cls === "ok" ? "on" : st.cls === "warn" ? "warn" : "off");
    setText("side-status-text", st.cls === "ok" ? t("链路正常") : st.cls === "warn" ? t("链路不稳定") : t("链路中断"));
    setText("side-meta", `${d.router.local_ip || "-"} · ${t("{n} 次探测", { n: d.monitor.counters.ping })}`);
    const platform = host.platform || {};
    setText("brand-sub", platform.hostname || t("路由器状态监控"));

    // 告警角标
    const alerts = d.alerts || [];
    const badge = $("alert-badge");
    if (alerts.length) {
      badge.hidden = false;
      badge.textContent = alerts.length;
    } else {
      badge.hidden = true;
    }

    // 迷你走势
    if (d.spark && d.spark.length > 1) {
      window.RCChart.sparkline($("hero-spark"), d.spark, d.status === "offline" ? "#ff5c66" : "#4f8cff");
    }

    renderQuality(d);
    renderEventList($("overview-events"), (d.events || []).slice(0, 6), t("暂无事件记录"));
    if (state.view === "router") renderRouter(d);
    if (state.view === "network") renderNetwork(d);
  }

  function renderQuality(d) {
    const h24 = d.router_stats.h24 || {};
    const n = h24.n || 0;
    const loss = h24.loss || 0;
    const successRate = n ? Math.max(0, 100 - loss) : 0;
    const lostCount = n ? Math.round((loss / 100) * n) : 0;
    const rateText = n ? (successRate >= 99.95 ? "100%" : `${successRate.toFixed(1)}%`) : "--";
    const color = loss <= 0.5 ? "#2fd6a5" : loss < 5 ? "#ffb340" : "#ff5c66";
    $("quality").innerHTML = `
      <div class="quality-top">
        <div class="quality-ring">
          <svg viewBox="0 0 100 100">
            <circle class="track" cx="50" cy="50" r="41"></circle>
            <circle class="bar" cx="50" cy="50" r="41" pathLength="100"
              stroke-dasharray="100" stroke-dashoffset="${100 - successRate}" style="stroke:${color}"></circle>
          </svg>
          <div class="qc"><b>${rateText}</b><span>${t("连通率")}</span></div>
        </div>
        <div class="q-stats">
          <div><span>${t("平均延迟")}</span><b>${fmt.ms(h24.avg)}</b></div>
          <div><span>${t("P95 延迟")}</span><b>${fmt.ms(h24.p95)}</b></div>
          <div><span>${t("最低 / 最高")}</span><b>${h24.min != null ? fmt.ms(h24.min, false) + " / " + fmt.ms(h24.max, false) : "--"}</b></div>
          <div><span>${t("探测次数")}</span><b>${n}</b></div>
        </div>
      </div>
      <div class="q-note">${t("24 小时内共 {n} 次探测，丢失 {k} 次", { n, k: lostCount })}</div>`;
  }

  // ------------------------------------------------------------------ 事件
  const LEVEL_ICON = { critical: "!", warning: "!", info: "i" };

  // 事件标题/详情的文案由前端出，后端只给 key + 数值，切语言时历史事件也能跟着变
  const ALERT_TITLES = {
    gateway_offline: "路由器不可达",
    gateway_loss: "路由器链路丢包",
    gateway_latency: "局域网延迟偏高",
    gateway_jitter: "网络抖动明显",
    wan_offline: "外网连接中断",
    cpu_high: "本机 CPU 占用过高",
    mem_high: "本机内存占用过高",
    disk_high: "磁盘空间不足",
    link_down: "本机网络接口断开",
  };
  const ALERT_DETAILS = {
    gateway_offline: "网关近 5 分钟丢包 {loss}%（{n} 次探测）",
    gateway_loss: "近 5 分钟丢包 {loss}%，超过阈值 {thr}%",
    gateway_latency: "网关平均延迟 {ms} ms，超过阈值 {thr} ms",
    gateway_jitter: "网关抖动 {ms} ms，超过阈值 {thr} ms",
    wan_offline: "外网目标近 5 分钟全部无响应（{n} 次探测）",
    cpu_high: "CPU 使用率 {pct}%，阈值 {thr}%",
    mem_high: "内存使用率 {pct}%，阈值 {thr}%",
    disk_high: "{mount} 已用 {pct}%",
    link_down: "所有物理网卡均处于 down 状态",
  };

  function eventText(e) {
    const params = e.params || {};
    const title = ALERT_TITLES[e.code];
    if (!title) return { title: e.title || "", detail: e.detail || "" };
    const detail = ALERT_DETAILS[e.code];
    return {
      title: t(title),
      detail: detail ? t(detail, params) : (e.detail || ""),
    };
  }

  function renderEventList(box, events, emptyText) {
    if (!events.length) {
      box.innerHTML = `<div class="event-empty">
        <span class="ee-icon">
          <svg viewBox="0 0 24 24" width="20" height="20"><path d="M4.5 12.5l5 5 10-11"/></svg>
        </span>
        <div>${esc(emptyText)}</div>
        <em>${t("链路正常时这里会保持安静")}</em>
      </div>`;
      return;
    }
    box.innerHTML = events
      .map((e) => {
        const running = e.status === "open";
        const txt = eventText(e);
        return `<div class="event-item">
          <div class="event-icon ${e.level}">${LEVEL_ICON[e.level] || "i"}</div>
          <div class="event-body">
            <div class="event-title">${esc(txt.title)}${running ? ` <span class="pill bad" style="padding:1px 6px;font-size:10px">${t("进行中")}</span>` : ""}</div>
            <div class="event-desc">${esc(txt.detail)}</div>
          </div>
          <div class="event-time">${running ? fmt.ago(e.ts_start) : fmt.datetime(e.ts_start)}</div>
        </div>`;
      })
      .join("");
  }

  async function loadEvents() {
    try {
      const [res, all] = await Promise.all([api("/api/events?limit=200"), api("/api/overview")]);
      state.events = res.events || [];
      state.alerts = (all.alerts || []).slice();
      renderEventPage();
    } catch (err) {
      console.warn(err);
    }
  }

  function renderEventPage() {
    let list = state.events.slice();
    if (state.eventFilter === "open") list = list.filter((e) => e.status === "open");
    else if (state.eventFilter === "critical") list = list.filter((e) => e.level === "critical");
    else if (state.eventFilter === "warning") list = list.filter((e) => e.level === "warning");
    renderEventList($("event-list"), list, t("该条件下暂无事件"));
  }

  $("event-filter").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    state.eventFilter = btn.dataset.filter;
    $("event-filter").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    renderEventPage();
  });

  // ------------------------------------------------------------------ 路由器页
  const CONF_KEY = { high: "高", medium: "中", low: "低" };

  function fmtUptime(sec) {
    if (!sec || sec < 0) return "--";
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    if (d) return t("{n} 天", { n: d });
    if (h) return t("{n} 小时", { n: h });
    const m = Math.floor((sec % 3600) / 60);
    return m ? t("{n} 分", { n: m }) : t("刚刚");
  }

  // 顶部设备卡上的"识别结果"部分：型号来源、代号、固件、运行时长
  function renderIdentity(ui) {
    const id = (ui && ui.router) || {};
    const busy = id.status === "detecting";
    const auto = id.auto !== false;
    const btn = $("router-identify");
    if (btn) {
      btn.disabled = busy || !auto;
      btn.classList.toggle("busy", busy);
      const src = (id.sources || []).map((s) => `${t(s.label)}（${s.detail}）`).join("；");
      btn.title = src
        ? `${t("识别来源：{x}", { x: src })}\n${t("置信度：{x}", { x: t(CONF_KEY[id.confidence] || "未知") })}${
            id.duration_ms ? `\n${t("耗时：{n} ms", { n: id.duration_ms })}` : ""
          }`
        : t("重新识别路由器型号");
    }
    setText("router-identify-label", busy ? t("识别中…") : auto ? t("自动识别") : t("已关闭"));

    const bits = [];
    if (ui.router_name_manual) bits.push(`<b class="meta-tag">${t("手动命名")}</b>`);
    if (id.code) bits.push(`<span class="mono">${esc(id.code)}</span>`);
    if (id.hw_version) bits.push(t("硬件 {v}", { v: esc(id.hw_version) }));
    if (id.serial) bits.push(`SN ${esc(id.serial)}`);
    if (!bits.length) {
      if (!auto) bits.push(t("型号识别已关闭，可在设置里开启"));
      else if (busy) bits.push(t("正在读取路由器信息…"));
      else bits.push(t("未识别到型号，可点右上角重新识别"));
    }
    const srcLabels = (id.sources || []).map((s) => t(s.label)).join(" · ");
    if (srcLabels) bits.push(`<span class="meta-src">${t("来源 {x}", { x: esc(srcLabels) })}</span>`);
    $("router-meta").innerHTML = bits.join('<i class="dot">·</i>');

    setText("dev-uptime", fmtUptime(id.uptime));
  }

  // 探测详情：后端只给 code + 原始 detail，文案在前端拼
  function checkDetail(c) {
    const extra = c.detail ? ` · ${c.detail}` : "";
    switch (c.code) {
      case "dns_ok":
        return t("解析成功") + (c.detail ? ` · ${c.detail}` : "");
      case "dns_fail":
        return t("解析失败") + extra;
      case "tcp_ok":
        return t("握手成功") + (c.short ? ` · ${c.short}` : "");
      case "tcp_fail":
        return c.detail || t("异常");
      case "http_ok":
      case "http_fail":
        return c.detail || t("异常");
      default:
        return c.detail || "";
    }
  }

  function renderRouter(d) {
    const r = d.router || {};
    // 顶部主设备卡
    const ui = d.ui || {};
    const st = statusInfo(d.status);
    const pill = $("router-status-pill");
    pill.className = `pill ${st.cls}`;
    pill.innerHTML = `<i></i>${esc(st.text)}`;
    // 英文界面优先用厂商给的英文型号（如 HUAWEI WiFi BE3 Pro+），手动命名则不覆盖
    const id = ui.router || {};
    const autoName = window.RCI18n.lang === "en" && id.model_en ? id.model_en : ui.router_name;
    setText("router-name", autoName || t("路由器"));
    renderIdentity(ui);
    const img = $("router-img");
    const src = ui.router_image || "/img/router.png";
    if (img.getAttribute("src") !== src) img.src = src;
    setText("router-addr", [r.ip, r.mac, r.iface].filter(Boolean).join(" · ") || "--");
    const m5 = d.router_stats.m5 || {};
    setText("dev-lat", fmt.ms(m5.avg));
    setText("dev-loss", fmt.pct(m5.loss, (m5.loss || 0) >= 10 ? 0 : 1));
    setText("dev-jitter", fmt.ms(m5.jitter));
    const h24 = d.router_stats.h24 || {};
    setText("dev-avail", h24.n ? fmt.pct(100 - (h24.loss || 0), 2) : "--");

    const info = [
      ["网关地址", r.ip || t("未探测"), true],
      ["MAC 地址", r.mac || t("未知"), true],
      ["出口网卡", r.iface || t("未知")],
      ["本机地址", r.local_ip ? `${r.local_ip}/${r.prefix || ""}` : t("未知")],
      ["网关延迟", fmt.ms((d.router_stats.m5 || {}).avg)],
      ["探测间隔", t("{n} 秒", { n: (state.config && state.config.probe && state.config.probe.gateway_interval) || 2 })],
    ];
    $("gw-info").innerHTML = info
      .map(([k, v, mono]) => `<dt>${esc(t(k))}</dt><dd class="${mono ? "mono" : ""}">${esc(v)}</dd>`)
      .join("");
    setText("gw-mode", (state.config && state.config.router && state.config.router.gateway === "auto") ? t("自动探测") : t("手动指定"));

    // 探测表
    const rows = [];
    const stats = d.router_stats.m5 || {};
    rows.push({
      name: t("网关 {ip}", { ip: r.ip || "" }).trim(),
      kind: "ICMP",
      ok: (stats.loss || 0) < 100 && stats.n > 0,
      latency: stats.avg,
      loss: stats.loss,
      detail: t("探测 {n} 次", { n: stats.n || 0 }),
    });
    (d.wan.targets || []).forEach((tg) => {
      rows.push({
        name: t("外网 {ip}", { ip: tg.target }),
        kind: "ICMP",
        ok: (tg.loss || 0) < 100 && tg.n > 0,
        latency: tg.avg,
        loss: tg.loss,
        detail: t("探测 {n} 次", { n: tg.n || 0 }),
      });
    });
    (d.checks || []).forEach((c) => {
      rows.push({
        name: t(c.target),
        kind: c.kind.toUpperCase(),
        ok: c.ok,
        latency: c.latency,
        loss: null,
        detail: checkDetail(c),
      });
    });
    $("probe-table").querySelector("tbody").innerHTML = rows
      .map(
        (row) => `<tr>
        <td>${esc(row.name)}</td>
        <td class="t-dim">${esc(row.kind)}</td>
        <td><span class="pill ${row.ok ? "ok" : "bad"}" style="padding:2px 8px;font-size:10.5px"><i></i>${row.ok ? t("可达") : t("异常")}</span></td>
        <td class="num">${fmt.ms(row.latency)}</td>
        <td class="num ${lossClass(row.loss)}">${row.loss == null ? "--" : fmt.pct(row.loss, row.loss >= 10 ? 0 : 1)}</td>
        <td><span class="ellip" title="${esc(row.detail)}">${esc(row.detail)}</span></td>
      </tr>`
      )
      .join("");

    // 丢包对比
    const bars = [
      { name: t("网关 {ip}", { ip: r.ip || "" }).trim(), loss: stats.loss, avg: stats.avg },
      ...(d.wan.targets || []).map((tg) => ({ name: t("外网 {ip}", { ip: tg.target }), loss: tg.loss, avg: tg.avg })),
    ];
    $("loss-hint").textContent = t("{n} 个目标 · 近 5 分钟", { n: bars.length });
    $("loss-bars").innerHTML = bars
      .map((b) => {
        const l = b.loss || 0;
        const clean = l <= 0.001;
        const w = clean ? 100 : Math.min(100, Math.max(1.5, l * 4));
        const color = clean ? "rgba(47,214,165,0.18)" : l < 5 ? "#ffb340" : "#ff5c66";
        return `<div class="bar-row">
          <span class="bl">${esc(b.name)}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${color}"></div></div>
          <span class="bv ${lossClass(l)}">${fmt.pct(l, 1)} · ${fmt.ms(b.avg, false)}</span>
        </div>`;
      })
      .join("");
  }

  // ------------------------------------------------------------------ 网络页
  // 本机系统卡：徽标配色 / 机型 / 虚拟化。system 由后端 hostinfo 识别，取不到就走旧字段
  const OS_COLORS = {
    ubuntu: ["#ff8a4c", "#d63f14"],
    debian: ["#f2547d", "#a30a41"],
    fedora: ["#5b93e0", "#294172"],
    arch: ["#2eb6ec", "#0f6f9f"],
    manjaro: ["#3fd3a8", "#10796a"],
    "linuxmint": ["#a6d95b", "#5d9a24"],
    centos: ["#c04b8c", "#7b2059"],
    rhel: ["#ff5a52", "#b30000"],
    rocky: ["#3ecf9a", "#0f7a56"],
    alma: ["#8cc63f", "#4a6f17"],
    "opensuse-leap": ["#8ccf3a", "#4d7a11"],
    "opensuse-tumbleweed": ["#8ccf3a", "#4d7a11"],
    kali: ["#4aa8ff", "#12457f"],
    raspbian: ["#e0567f", "#8f123d"],
    alpine: ["#3f9fcc", "#12455f"],
    nixos: ["#7b9bf0", "#3f5aa8"],
    gentoo: ["#8f7fd6", "#4a3a86"],
    deepin: ["#5aa3ff", "#12489e"],
    kylin: ["#4f9bff", "#123f8e"],
    uos: ["#5a8cf5", "#1b3f92"],
    elementary: ["#9b78d6", "#5b3286"],
    pop: ["#6fd0dd", "#1f7d88"],
    default: ["#6d9dff", "#2b5fd9"],
  };

  const OS_GLYPH = {
    ubuntu: "U", debian: "D", fedora: "F", arch: "A", manjaro: "M",
    linuxmint: "LM", centos: "C", rhel: "R", rocky: "R", alma: "A",
    opensuse: "S", kali: "K", raspbian: "R", alpine: "A", nixos: "N",
    gentoo: "G", deepin: "D", kylin: "K", uos: "U", elementary: "e", pop: "P",
  };

  // 有官方标志的发行版放 logo（static/img/os/*.svg），其余用上面的首字母
  const OS_LOGO = {
    ubuntu: "/img/os/ubuntu.svg",
  };

  const VIRT_TEXT = { hardware: "物理机", vm: "虚拟机", container: "容器" };
  const VIRT_CLS = { hardware: "ok", vm: "warn", container: "" };
  const FORM_TEXT = {
    desktop: "台式机", laptop: "笔记本", aio: "一体机", server: "服务器",
    tablet: "平板", phone: "手机", handheld: "掌机",
  };

  function shortCpu(name) {
    if (!name) return "--";
    return String(name)
      .replace(/\s*\((?:R|TM|r|tm)\)/g, "")
      .replace(/\s+CPU\b/gi, "")
      .replace(/\s*@\s*[\d.]+\s*GHz/i, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  // 徽章内容：认得出的发行版放官方标志，其余用首字母（logo 加载失败也回退字母）
  function fillBadge(badge, osKey, name) {
    const letter = OS_GLYPH[osKey] || (name[0] || "L").toUpperCase();
    const src = OS_LOGO[osKey];
    if (!src) {
      badge.classList.remove("has-logo");
      badge.textContent = letter;
      return;
    }
    badge.classList.add("has-logo");
    badge.innerHTML = `<img src="${src}" alt="${esc(name)}" draggable="false">`;
    const img = badge.querySelector("img");
    img.addEventListener(
      "error",
      () => {
        badge.classList.remove("has-logo");
        badge.textContent = letter;
      },
      { once: true }
    );
  }

  function renderHostCard(d) {
    const host = (d && d.host) || {};
    const sys = host.system || {};
    const plat = host.platform || {};
    if (!sys.name && !plat.os && !plat.hostname) return;

    const name = sys.name || plat.os || "Linux";
    const pretty = sys.pretty || plat.os || name;
    const osKey = String(sys.logo || sys.id || "linux").toLowerCase();
    const badge = $("host-os-badge");
    if (badge) {
      const c = OS_COLORS[osKey] || OS_COLORS.default;
      badge.dataset.os = osKey;
      fillBadge(badge, osKey, name);
      badge.style.background = `linear-gradient(158deg, ${c[0]}, ${c[1]})`;
      badge.style.boxShadow = `0 16px 32px ${c[1]}55, inset 0 1px 0 rgba(255,255,255,.28)`;
    }

    setText("host-os-name", pretty);

    // 虚拟化状态
    const kind = sys.virt_kind || "hardware";
    const pill = $("host-virt-pill");
    if (pill) {
      const label = t(VIRT_TEXT[kind] || "物理机");
      const extra = kind !== "hardware" && sys.virt_name ? ` ${sys.virt_name}` : "";
      pill.className = `pill ${VIRT_CLS[kind] || ""}`.trim();
      pill.innerHTML = `<i></i>${esc(label)}${esc(extra)}`;
      pill.title = `${t("虚拟化")}: ${label}${extra}`;
    }

    // 机型 + 主机名
    const form = FORM_TEXT[sys.machine && sys.machine.form] || "";
    const kindBits = [];
    if (form) kindBits.push(t(form));
    if (sys.hostname || plat.hostname) kindBits.push(sys.hostname || plat.hostname);
    setText("host-os-kind", kindBits.join(" · "));

    // 内核行
    const kernel = sys.kernel || plat.kernel || "--";
    const arch = sys.arch || plat.arch || "";
    setText("host-kernel-line", `Linux ${kernel}${arch ? ` · ${arch}` : ""}`);

    // 标签行：机型 / 桌面 / 会话
    const hw = sys.machine || {};
    const meta = [];
    if (hw.model) meta.push(`<span class="meta-tag">${esc(hw.model)}</span>`);
    if (sys.desktop) {
      const session = sys.session ? ` · ${sys.session}` : "";
      meta.push(`${t("桌面环境")} <b class="t-soft">${esc(sys.desktop)}${esc(session)}</b>`);
    }
    const codename = sys.codename ? ` (${sys.codename})` : "";
    if (sys.version) meta.push(`${t("版本")} <b class="t-soft">${esc(sys.version)}${esc(codename)}</b>`);
    $("host-meta").innerHTML = meta.join('<span class="dot">·</span>') || `<span class="t-dim">${t("未识别")}</span>`;

    // 四个数字
    setText("host-uptime2", fmt.dur(host.uptime));
    setText("host-cpu-model", shortCpu(sys.cpu_model || plat.cpu_model));
    setText("host-cores", sys.cores || plat.cores || "--");
    const memTotal = sys.mem_total || (host.mem && host.mem.total) || null;
    setText("host-mem-total", memTotal ? fmt.bytes(memTotal) : "--");
  }

  function renderNetwork(d) {
    const host = d.host || {};
    renderHostCard(d);
    const ifaces = (host.interfaces || []).filter((i) => i.name !== "lo");
    setText("iface-count", t("{n} 个接口", { n: ifaces.length }));
    $("iface-table").querySelector("tbody").innerHTML = ifaces
      .map((i) => {
        const up = i.state === "up";
        return `<tr>
          <td><div class="name-cell"><span>${esc(i.name)}</span>${i.wireless ? '<span class="t-dim" style="font-size:10px">Wi-Fi</span>' : ""}</div></td>
          <td class="mono">${i.ip ? esc(i.ip) : `<span class="t-dim">${t("未分配")}</span>`}</td>
          <td class="${up ? "t-ok" : "t-dim"}">${up ? t("已连接") : i.state === "down" ? t("未连接") : esc(i.state)}</td>
          <td class="num">${fmt.bps(i.rx_bps)}</td>
          <td class="num">${fmt.bps(i.tx_bps)}</td>
          <td class="num">${fmt.bytes(i.rx_total)} / ${fmt.bytes(i.tx_total)}</td>
        </tr>`;
      })
      .join("");

    const disks = host.disks || [];
    $("disk-list").innerHTML = disks.length
      ? disks
          .map((dsk) => {
            const color = dsk.pct >= 90 ? "#ff5c66" : dsk.pct >= 75 ? "#ffb340" : "#4f8cff";
            return `<div class="disk-item">
              <div class="disk-top">
                <span class="mono">${esc(dsk.mount)}</span>
                <span>${fmt.bytes(dsk.used)} / ${fmt.bytes(dsk.total)}</span>
              </div>
              <div class="bar-track"><div class="bar-fill" style="width:${dsk.pct}%;background:${color}"></div></div>
            </div>`;
          })
          .join("")
      : `<div class="event-empty">${t("未发现可用磁盘")}</div>`;

    const procs = host.processes || [];
    $("proc-table").querySelector("tbody").innerHTML = procs.length
      ? procs
          .slice(0, 8)
          .map(
            (p) => `<tr>
        <td>${esc(p.name)}</td>
        <td class="num t-dim">${p.pid}</td>
        <td class="num ${p.cpu > 50 ? "t-warn" : ""}">${p.cpu.toFixed(1)}%</td>
        <td class="num">${fmt.bytes(p.rss)}</td>
      </tr>`
          )
          .join("")
      : `<tr class="empty"><td colspan="4">${t("暂无数据")}</td></tr>`;
  }

  // ------------------------------------------------------------------ 设备
  async function loadDevices() {
    try {
      const res = await api("/api/devices");
      state.devices = res.devices || [];
      renderDevices();
    } catch (err) {
      console.warn(err);
    }
  }

  function renderDevices() {
    const list = state.devices;
    setText("dev-count", list.length);
    const gateway = (state.data && state.data.router && state.data.router.ip) || null;
    $("dev-table").querySelector("tbody").innerHTML = list.length
      ? list
          .map(
            (d) => `<tr>
        <td><span class="pill ${d.online ? "ok" : ""}" style="padding:2px 8px;font-size:10.5px"><i></i>${d.online ? t("在线") : t("离线")}</span></td>
        <td class="mono">${esc(d.ip)}${d.ip === gateway ? ` <span class="t-dim" style="font-size:10px">${t("网关")}</span>` : ""}</td>
        <td class="mono t-dim">${esc(d.mac || "--")}</td>
        <td class="t-dim">${esc(d.iface || "--")}</td>
        <td>${esc(d.hostname || "-")}</td>
        <td class="num t-dim">${fmt.ago(d.last_seen)}</td>
      </tr>`
          )
          .join("")
      : `<tr class="empty"><td colspan="6">${t("尚未发现设备，点击「扫描网段」开始探测")}</td></tr>`;
    setText("dev-hint", t("{n} 台在线", { n: list.filter((d) => d.online).length }));
  }

  $("scan-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const old = btn.innerHTML;
    btn.textContent = t("扫描中…");
    try {
      const res = await api("/api/devices/scan", { method: "POST", body: "{}" });
      if (res.ok === false) toast(t("无法确定本机网段"), true);
      else toast(t("已扫描 {n} 个地址", { n: res.scanned || 0 }));
      await loadDevices();
    } catch (err) {
      toast(t("扫描失败: {e}", { e: err.message }), true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
      window.RCI18n.applyDom(btn);
    }
  });

  // ------------------------------------------------------------------ 图表
  function initCharts() {
    const C = window.RCChart;
    state.charts.latency = new C.LineChart($("chart-latency"), {
      yFormat: (v) => String(Math.round(v)),
      tipFormat: (v) => fmt.ms(v),
      legend: "#latency-legend",
      minRange: 10,
    });
    state.charts.bandwidth = new C.LineChart($("chart-bandwidth"), {
      yFormat: (v) => fmt.bytes(v),
      tipFormat: (v) => fmt.bps(v),
      legend: "#bw-legend",
      minRange: 1024,
    });
    state.charts.router = new C.LineChart($("chart-router-latency"), {
      yFormat: (v) => String(Math.round(v)),
      tipFormat: (v) => fmt.ms(v),
      legend: "#router-legend",
      minRange: 10,
    });
    state.charts.wan = new C.LineChart($("chart-wan-latency"), {
      yFormat: (v) => String(Math.round(v)),
      tipFormat: (v) => fmt.ms(v),
      legend: "#wan-legend",
      minRange: 20,
    });
    state.charts.net = new C.LineChart($("chart-net"), {
      yFormat: (v) => fmt.bytes(v),
      tipFormat: (v) => fmt.bps(v),
      legend: "#net-legend",
      minRange: 1024,
    });
    state.charts.disk = new C.LineChart($("chart-disk"), {
      yFormat: (v) => fmt.bytes(v),
      tipFormat: (v) => fmt.bps(v),
      legend: "#disk-legend",
      minRange: 1024,
    });
  }

  // 去掉全程为 0 的序列（例如未插网线的网卡），避免图表被压平
  function dropIdleSeries(series) {
    return (series || []).filter((s) => (s.points || []).some((p) => p[1] > 0));
  }

  // 图表序列名：后端只给 key/dir，图例文案在前端出，切语言时图例跟着变
  function seriesName(s, short) {
    if (s.key === "gateway") return t("网关");
    if (s.dir) return short ? t(s.dir === "rx" ? "下行" : "上行") : `${s.key} ${t(s.dir === "rx" ? "下行" : "上行")}`;
    if (s.key === "read") return t("读取");
    if (s.key === "write") return t("写入");
    if (s.key === "cpu") return "CPU";
    if (s.key === "mem_pct") return t("内存");
    if (/^\d+\.\d+\.\d+\.\d+$/.test(s.key || "")) return t("外网 {ip}", { ip: s.key });
    return s.name || s.key;
  }

  async function loadCharts() {
    const r = state.range;
    try {
      if (state.view === "overview") {
        const [ping, net] = await Promise.all([
          api(`/api/series?metric=ping&range=${r}`),
          api(`/api/series?metric=net&range=${r}&iface=${encodeURIComponent(state.iface || "")}`),
        ]);
        state.charts.latency.setData(
          (ping.series || []).map((s) => ({ name: seriesName(s), points: s.points.map((p) => [p[0], p[1]]) })),
          { domain: [ping.from, ping.to] }
        );
        state.charts.bandwidth.setData(
          dropIdleSeries(net.series || []).map((s) => ({
            name: seriesName(s, true),
            points: s.points.map((p) => [p[0], p[1]]),
          })),
          { domain: [net.from, net.to] }
        );
        if (state.data && state.data.host) {
          const host = state.data.host;
          const iface = (host.interfaces || []).find((i) => i.name === state.iface);
          setText("bw-hint", iface ? `${iface.name} · ↓${fmt.bps(iface.rx_bps)} ↑${fmt.bps(iface.tx_bps)}` : "");
        }
      } else if (state.view === "router") {
        const [gw, wan] = await Promise.all([
          api(`/api/series?metric=ping&range=${r}&target=gateway`),
          api(`/api/series?metric=ping&range=${r}`),
        ]);
        state.charts.router.setData(
          (gw.series || []).map((s) => ({ name: seriesName(s), points: s.points.map((p) => [p[0], p[1]]) })),
          { domain: [gw.from, gw.to] }
        );
        const wanSeries = (wan.series || []).filter((s) => s.key !== "gateway");
        state.charts.wan.setData(
          wanSeries.map((s) => ({ name: seriesName(s), points: s.points.map((p) => [p[0], p[1]]) })),
          { domain: [wan.from, wan.to] }
        );
      } else if (state.view === "network") {
        const [net, disk] = await Promise.all([
          api(`/api/series?metric=net&range=${r}`),
          api(`/api/series?metric=disk&range=${r}`),
        ]);
        state.charts.net.setData(
          dropIdleSeries(net.series || []).map((s) => ({ name: seriesName(s), points: s.points.map((p) => [p[0], p[1]]) })),
          { domain: [net.from, net.to] }
        );
        state.charts.disk.setData(
          (disk.series || []).map((s) => ({ name: seriesName(s), points: s.points.map((p) => [p[0], p[1]]) })),
          { domain: [disk.from, disk.to] }
        );
      }
    } catch (err) {
      console.warn(t("图表加载失败"), err);
    }
  }

  // ------------------------------------------------------------------ 设置
  async function loadSettings() {
    try {
      const [cfgRes, statsRes] = await Promise.all([api("/api/config"), api("/api/stats")]);
      state.config = cfgRes.config;
      state.statsCache = statsRes;
      fillForm($("settings-form"), cfgRes.config);
      fillForm($("threshold-form"), cfgRes.config);
      syncLangButtons();
      renderDbStats(statsRes, cfgRes.config, state.data);
    } catch (err) {
      toast(t("读取配置失败"), true);
    }
  }

  function getPath(obj, path) {
    return path.split(".").reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), obj);
  }

  function fillForm(form, cfg) {
    form.querySelectorAll("input[name], select[name]").forEach((input) => {
      let v = getPath(cfg, input.name);
      if (v === undefined) return;
      const scale = Number(input.dataset.scale || 0);
      if (scale && typeof v === "number") v = Number((v / scale).toFixed(2));
      input.value = Array.isArray(v) ? v.join(", ") : v;
    });
  }

  const ARRAY_FIELDS = new Set([
    "probe.wan_targets",
    "probe.dns_targets",
    "speedtest.download_urls",
    "speedtest.upload_urls",
  ]);

  function collectPatch(form) {
    const patch = {};
    form.querySelectorAll("input[name], select[name]").forEach((input) => {
      const path = input.name;
      let value = input.value.trim();
      if (input.dataset.type === "bool") value = value === "true";
      else if (input.type === "number") {
        value = Number(value);
        const scale = Number(input.dataset.scale || 0);
        if (scale) value *= scale;
      } else if (ARRAY_FIELDS.has(path)) {
        value = value.split(",").map((s) => s.trim()).filter(Boolean);
      }
      const parts = path.split(".");
      let cur = patch;
      for (let i = 0; i < parts.length - 1; i++) {
        cur[parts[i]] = cur[parts[i]] || {};
        cur = cur[parts[i]];
      }
      cur[parts[parts.length - 1]] = value;
    });
    return patch;
  }

  function bindSave(form, msgId) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const patch = collectPatch(form);
      try {
        const res = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
        state.config = res.config;
        $(msgId).textContent = t("已保存 ✓");
        toast(t("配置已保存，新的探测参数立即生效"));
        setTimeout(() => ($(msgId).textContent = ""), 2600);
        loadCharts();
      } catch (err) {
        toast(t("保存失败: {e}", { e: err.message }), true);
      }
    });
  }

  function renderDbStats(stats, cfg, data) {
    const db = (stats && stats.db) || {};
    const rows = [
      ["延迟样本", t("{n} 条", { n: db.ping || 0 })],
      ["系统样本", t("{n} 条", { n: db.sys || 0 })],
      ["探测记录", t("{n} 条", { n: db.checks || 0 })],
      ["事件", t("{n} 条", { n: db.events || 0 })],
      ["设备", t("{n} 台", { n: db.devices || 0 })],
      ["数据库大小", `${db.db_mb || 0} MB`],
    ];
    $("db-stats").innerHTML = rows.map(([k, v]) => `<dt>${esc(t(k))}</dt><dd>${esc(v)}</dd>`).join("");

    const host = (data && data.host) || {};
    const plat = host.platform || {};
    const sys = host.system || {};
    const hw = sys.machine || {};
    const mon = (data && data.monitor) || {};
    const kind = sys.virt_kind || "hardware";
    const virt = t(VIRT_TEXT[kind] || "物理机") + (kind !== "hardware" && sys.virt_name ? ` (${sys.virt_name})` : "");
    const desktop = sys.desktop ? `${sys.desktop}${sys.session ? ` · ${sys.session}` : ""}` : "-";
    const info = [
      ["主机名", sys.hostname || plat.hostname || "-"],
      ["操作系统", sys.pretty || plat.os || "-"],
      ["内核", sys.kernel || plat.kernel || "-"],
      ["架构", sys.arch || plat.arch || "-"],
      ["桌面环境", desktop],
      ["机型", hw.model || "-"],
      ["虚拟化", virt],
      ["CPU", shortCpu(sys.cpu_model || plat.cpu_model)],
      ["核心数", sys.cores || plat.cores || "-"],
      ["内存", host.mem ? fmt.bytes(host.mem.total) : "-"],
      ["监听地址", `${(cfg.app && cfg.app.listen_host) || "0.0.0.0"}:${(cfg.app && cfg.app.listen_port) || 8848}`],
      ["监控运行", mon.uptime ? fmt.dur(mon.uptime) : "-"],
      ["探测次数", t("{n} 次", { n: (mon.counters && mon.counters.ping) || 0 })],
      ["保留策略", t("{n} 天", { n: (cfg.retention && cfg.retention.days) || 7 })],
    ];
    $("about-info").innerHTML = info
      .map(([k, v]) => `<dt>${esc(t(k))}</dt><dd title="${esc(v)}">${esc(v)}</dd>`)
      .join("");
  }

  $("probe-now").addEventListener("click", async () => {
    try {
      await api("/api/probe/now", { method: "POST", body: "{}" });
      toast(t("已触发一轮探测"));
      setTimeout(refresh, 1200);
    } catch (err) {
      toast(t("触发失败: {e}", { e: err.message }), true);
    }
  });

  // ------------------------------------------------------------------ 测网速
  let speedTimer = null;

  function stopSpeedPolling() {
    if (speedTimer) {
      clearInterval(speedTimer);
      speedTimer = null;
    }
  }

  function startSpeedPolling() {
    stopSpeedPolling();
    speedTimer = setInterval(() => {
      if (state.view !== "speedtest") return stopSpeedPolling();
      loadSpeedTest();
    }, 900);
  }

  async function loadSpeedTest() {
    try {
      const d = await api("/api/speedtest");
      renderSpeed(d);
    } catch (err) {
      console.warn(t("测速状态读取失败"), err);
    }
  }

  function speedText(v, unit = true) {
    if (v == null || !isFinite(v)) return "--";
    const s = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
    return unit ? `${s} Mbps` : s;
  }

  // 测速阶段：后端给 phase_code，文案在前端出
  const PHASE_TEXT = {
    idle: "准备就绪",
    probe_servers: "检测测试服务器",
    probe_upload: "检测上传服务器",
    download: "测试下行速率",
    upload: "测试上行速率",
    summarize: "汇总结果",
    done: "测速完成",
    failed: "测速失败",
    timeout: "测速超时",
    servers_down: "测试服务器不可达",
    no_url: "未配置下载测速地址",
    aborted: "测速异常结束",
  };

  function phaseText(d) {
    const key = PHASE_TEXT[d.phase_code];
    if (key) return t(key);
    return d.phase || "";
  }

  function renderSpeed(d) {
    state.speed = d;
    const running = d.state === "running";
    const res = d.result || {};
    const card = document.querySelector(".speed-card");
    card.classList.toggle("running", running);

    const down = running ? d.live_down : res.down_mbps;
    const up = running ? d.live_up : res.up_mbps;
    setText("st-down", down == null || down <= 0 ? "--" : fmt.num(down, down >= 100 ? 0 : 1));
    setText("st-up", up == null || up <= 0 ? "--" : fmt.num(up, up >= 100 ? 0 : 1));

    const bar = $("st-bar");
    bar.style.width = `${Math.max(running ? 2 : 0, (d.progress || 0) * 100)}%`;

    const phase = $("st-phase");
    phase.className = "speed-phase" + (d.state === "error" ? " err" : "");
    if (d.state === "error") phase.textContent = d.error || phaseText(d);
    else if (running) phase.textContent = phaseText(d);
    else if (d.state === "done" && res.ts) phase.textContent = t("完成于 {t}", { t: fmt.ago(res.ts) });
    else phase.textContent = t("准备就绪");

    const btn = $("st-start");
    btn.disabled = running;
    btn.textContent = running ? t("测速中…") : t("开始测速");

    setText("st-hint", running ? t("测速会占满带宽，期间其他设备可能变慢") : t("点击开始，约需 15 秒"));
    setText("st-server", res.server || "--");

    // 延迟对比
    const idle = res.latency_idle;
    const loaded = res.latency_loaded;
    const maxLat = Math.max(idle || 0, loaded || 0, 1);
    const rows = [
      { name: t("空闲"), value: idle, color: "#4f8cff" },
      { name: t("负载"), value: loaded, color: loaded && idle && loaded > idle * 1.6 ? "#ffb340" : "#2fd6a5" },
    ];
    $("st-latency").innerHTML = rows
      .map(
        (r) => `<div class="lat-row">
          <span>${r.name}</span>
          <div class="lat-bar"><i style="width:${r.value ? Math.max(4, (r.value / maxLat) * 100) : 0}%;background:${r.color}"></i></div>
          <b>${r.value == null ? "--" : fmt.ms(r.value)}</b>
        </div>`
      )
      .join("");

    // 说明
    const note = $("st-note");
    if (running) {
      note.innerHTML = t("正在测试 {p}，进度 {n}%", {
        p: `<b>${esc(phaseText(d))}</b>`,
        n: Math.round((d.progress || 0) * 100),
      });
    } else if (d.state === "done" && idle && loaded) {
      if (loaded <= idle) {
        note.innerHTML = t("负载下延迟没有上升（{a} → {b} ms），链路缓存健康，满载时也不会明显卡顿。", {
          a: idle.toFixed(1),
          b: loaded.toFixed(1),
        });
      } else {
        const delta = loaded - idle;
        const ratio = idle > 0 ? loaded / idle : 1;
        const verdict =
          ratio < 1.3 ? t("基本无排队，链路缓存健康")
            : ratio < 2 ? t("有一定排队，高峰时会感到卡顿")
            : t("明显缓冲膨胀（bufferbloat），可开启路由器的 SQM/QoS");
        note.innerHTML = `${t("负载延迟比空闲高出 {d} ms（{r}×）。", { d: delta.toFixed(1), r: ratio.toFixed(1) })}<br>${verdict}`;
      }
    } else if (res.upload_error && d.state === "done") {
      note.innerHTML = `${t("上行未测：{e}", { e: esc(res.upload_error) })}<br>${t("可在设置里换一个接受 POST 的上传地址。")}`;
    } else {
      note.textContent = t("空闲延迟取自最近 1 分钟的探测记录");
    }

    renderSpeedHistory(d.history || []);
  }

  function renderSpeedHistory(rows) {
    const tbody = $("st-history").querySelector("tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr class="empty"><td colspan="5">${t("还没有测速记录")}</td></tr>`;
      return;
    }
    const maxDown = Math.max(...rows.map((r) => r.down_mbps || 0), 1);
    tbody.innerHTML = rows
      .slice(0, 10)
      .map((r) => {
        const w = Math.max(4, ((r.down_mbps || 0) / maxDown) * 100);
        return `<tr>
          <td>${fmt.datetime(r.ts)}</td>
          <td class="num"><span class="rate-cell"><span class="rate-track"><i style="width:${w}%"></i></span><span>${speedText(
          r.down_mbps
        )}</span></span></td>
          <td class="num">${r.up_mbps ? speedText(r.up_mbps) : "--"}</td>
          <td class="num">${r.latency_idle != null ? fmt.ms(r.latency_idle, false) : "--"} / ${
          r.latency_loaded != null ? fmt.ms(r.latency_loaded, false) : "--"
        }</td>
          <td class="ellip">${esc(r.server || "--")}</td>
        </tr>`;
      })
      .join("");
  }

  $("router-identify").addEventListener("click", async () => {
    const btn = $("router-identify");
    btn.disabled = true;
    setText("router-identify-label", t("识别中…"));
    try {
      await api("/api/router/identify", { method: "POST", body: "{}" });
      toast(t("正在识别…"));
    } catch (err) {
      toast(t("识别失败: {e}", { e: err.message }), true);
      btn.disabled = false;
      setText("router-identify-label", t("自动识别"));
    }
  });

  $("st-start").addEventListener("click", async () => {
    const btn = $("st-start");
    btn.disabled = true;
    btn.textContent = t("启动中…");
    try {
      const res = await api("/api/speedtest/start", { method: "POST", body: "{}" });
      if (!res.ok) toast(res.message ? t(res.message) : t("无法开始测速"), true);
      else toast(t("测速已开始"));
      await loadSpeedTest();
      startSpeedPolling();
    } catch (err) {
      toast(t("启动失败: {e}", { e: err.message }), true);
      btn.disabled = false;
      btn.textContent = t("开始测速");
    }
  });

  // ------------------------------------------------------------------ 轮询
  const live = $("live-pill");

  async function refresh() {
    try {
      const d = await api("/api/overview");
      state.data = d;
      state.lastTs = d.now;
      if (!state.iface) {
        state.iface = (d.router && d.router.iface) || null;
        loadCharts();
      }
      renderOverview(d);
      state.failCount = 0;
      live.className = "live";
      live.querySelector("span:last-child").textContent = `${t("实时")} · ${fmt.clock(d.now)}`;
    } catch (err) {
      state.failCount++;
      live.className = "live " + (state.failCount > 3 ? "down" : "stale");
      live.querySelector("span:last-child").textContent = t("连接中断");
    }
  }

  // ------------------------------------------------------------------ 语言
  // 选项在设置页：自动 / 中文 / English。auto = 跟随浏览器，不写 localStorage
  function browserLang() {
    const raw = String(navigator.language || navigator.userLanguage || "zh").toLowerCase();
    return raw.indexOf("en") === 0 ? "en" : "zh";
  }

  function syncLangButtons() {
    const choice = state.langChoice || "auto";
    document.querySelectorAll("#lang-switch button").forEach((b) => {
      const on = b.dataset.lang === choice;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setLanguage(choice, persist) {
    const want = ["auto", "zh", "en"].includes(choice) ? choice : "auto";
    state.langChoice = want;
    if (want === "auto") {
      try {
        localStorage.removeItem("rc.lang");
      } catch (err) {
        /* 隐私模式下忽略 */
      }
      window.RCI18n.setLang(browserLang(), { silent: true });
    } else {
      window.RCI18n.setLang(want);
    }
    syncLangButtons();
    if (persist) {
      api("/api/config", { method: "POST", body: JSON.stringify({ app: { lang: want } }) })
        .then((res) => {
          state.config = res.config;
        })
        .catch(() => toast(t("保存失败: {e}", { e: "lang" }), true));
    }
  }

  $("lang-switch").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-lang]");
    if (btn && btn.dataset.lang !== state.langChoice) setLanguage(btn.dataset.lang, true);
  });

  // 切语言后把当前页重新渲染一遍（文案都在 render 函数里）
  document.addEventListener("rc:langchange", () => {
    buildRings();
    syncLangButtons();
    if (state.data) {
      renderOverview(state.data);
      if (state.view === "router") renderRouter(state.data);
      if (state.view === "network") renderNetwork(state.data);
      if (state.view === "devices") renderDevices();
      if (state.view === "events") renderEventPage();
      if (state.view === "speedtest" && state.speed) renderSpeed(state.speed);
      if (state.view === "settings" && state.config) {
        renderDbStats(state.statsCache || {}, state.config, state.data);
      }
    }
    const meta = viewMeta(state.view);
    setText("page-title", meta[0]);
    setText("page-sub", meta[1]);
    loadCharts();
  });

  async function boot() {
    const pick = (v) => {
      const s = String(v || "").toLowerCase();
      if (s.indexOf("zh") === 0) return "zh";
      if (s.indexOf("en") === 0) return "en";
      return null;
    };
    // 语言优先级：?lang= → 本机记住的选择 → 服务端 app.lang（auto 时看浏览器）
    let urlLang = null;
    try {
      urlLang = pick(new URLSearchParams(location.search).get("lang"));
    } catch (err) {
      urlLang = null;
    }
    let saved = null;
    try {
      saved = pick(localStorage.getItem("rc.lang"));
    } catch (err) {
      saved = null;
    }

    state.langChoice = urlLang || saved || "auto";
    // 先把静态文案铺上，避免首屏闪中文
    window.RCI18n.init(state.langChoice === "auto" ? null : state.langChoice);
    syncLangButtons();

    buildRings();
    initCharts();
    try {
      const cfg = await api("/api/config");
      state.config = cfg.config;
      // 本机和 URL 都没指定过，才用服务端配置的默认语言
      const configured = pick(cfg.config.app && cfg.config.app.lang);
      if (!urlLang && !saved && configured && configured !== window.RCI18n.lang) {
        window.RCI18n.setLang(configured, { silent: true });
        state.langChoice = configured;
        syncLangButtons();
      }
    } catch (err) {
      console.warn(err);
    }
    await refresh();
    const initial = location.hash.replace("#", "");
    if (VIEW_META[initial]) switchView(initial, false);
    await loadCharts();

    setInterval(refresh, 2000);
    setInterval(() => {
      if (["overview", "router", "network"].includes(state.view)) loadCharts();
    }, 6000);
    setInterval(() => {
      if (state.view === "events") loadEvents();
      if (state.view === "devices") loadDevices();
    }, 10000);
  }

  window.addEventListener("hashchange", () => {
    const v = location.hash.replace("#", "");
    if (VIEW_META[v] && v !== state.view) switchView(v, false);
  });

  bindSave($("settings-form"), "form-msg");
  bindSave($("threshold-form"), "threshold-msg");
  window.addEventListener("resize", () => {
    Object.values(state.charts).forEach((c) => c && c.draw());
  });

  boot();
})();
