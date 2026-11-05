/* RouterCat - 中英文文案与语言切换
 *
 * 约定：以中文原文作为词典 key，英文模式下查表替换。
 *  - 静态文本：在 HTML 上写 data-i18n="原文"，切语言时由 applyDom() 统一替换
 *  - 输入框占位符：data-i18n-ph
 *  - title 属性：data-i18n-title
 *  - 动态文本：JS 里用 t("原文") 或 t("含 {n} 的模板", {n: 3})
 */
(function () {
  "use strict";

  const DICT = {
    // ---------------------------------------------------------------- 框架
    "路由器状态监控": "Router status monitor",
    "总览": "Overview",
    "路由器": "Router",
    "网络与主机": "Network & Host",
    "设备发现": "Devices",
    "测网速": "Speed Test",
    "事件与告警": "Events & Alerts",
    "设置": "Settings",
    "正在连接…": "Connecting…",
    "实时": "Live",
    "连接中断": "Disconnected",
    "5分": "5m",
    "30分": "30m",
    "1时": "1h",
    "6时": "6h",
    "24时": "24h",

    // ---------------------------------------------------------------- 页面标题/副标题
    "路由器与链路实时状态": "Live router and link status",
    "网关连通性、延迟与丢包": "Gateway reachability, latency and loss",
    "网卡流量、磁盘与资源占用": "Interface throughput, disks and resources",
    "局域网内已发现的终端": "Devices found on your LAN",
    "下行/上行带宽与负载延迟": "Down/up bandwidth and latency under load",
    "链路异常与恢复记录": "Incidents and recoveries",
    "监控目标、阈值与数据保留": "Targets, thresholds and retention",

    // ---------------------------------------------------------------- 总览
    "路由器网关": "Gateway",
    "检测中": "Checking",
    "网关延迟 · 近 5 分钟平均": "Gateway latency · 5 min average",
    "丢包": "Loss",
    "抖动": "Jitter",
    "探测": "Probes",
    "近 10 分钟": "Last 10 min",
    "本机负载": "Host load",
    "负载": "Load",
    "温度": "Temp",
    "磁盘": "Disk",
    "延迟趋势": "Latency trend",
    "链路质量": "Link quality",
    "24 小时": "24 hours",
    "带宽": "Bandwidth",
    "最近事件": "Recent events",
    "查看全部": "View all",
    "连通率": "Reachability",
    "平均延迟": "Avg latency",
    "P95 延迟": "P95 latency",
    "最低 / 最高": "Min / Max",
    "探测次数": "Probes",
    "24 小时内共 {n} 次探测，丢失 {k} 次": "{n} probes in 24h, {k} lost",
    "暂无事件记录": "No events yet",
    "该条件下暂无事件": "No events match this filter",
    "链路正常时这里会保持安静": "Stays quiet while the link is healthy",
    "进行中": "Ongoing",
    "严重": "Critical",
    "警告": "Warning",
    "提示": "Info",
    "在线": "Online",
    "不稳定": "Degraded",
    "离线": "Offline",
    "未知": "Unknown",
    "链路正常": "Link healthy",
    "链路不稳定": "Link degraded",
    "链路中断": "Link down",
    "已运行 {t}": "Up {t}",
    "{n} 次探测": "{n} probes",

    // ---------------------------------------------------------------- 路由器页
    "重新识别路由器型号": "Re-identify router model",
    "自动识别": "Auto-detect",
    "识别中…": "Detecting…",
    "已关闭": "Off",
    "手动命名": "Custom name",
    "硬件 {v}": "HW {v}",
    "型号识别已关闭，可在设置里开启": "Model detection is off — enable it in Settings",
    "正在读取路由器信息…": "Reading router info…",
    "未识别到型号，可点右上角重新识别": "Model not identified — click the button to retry",
    "来源 {x}": "via {x}",
    "识别来源：{x}": "Sources: {x}",
    "置信度：{x}": "Confidence: {x}",
    "耗时：{n} ms": "Took {n} ms",
    "正在识别…": "Detecting…",
    "识别失败: {e}": "Detection failed: {e}",
    "高": "High",
    "中": "Medium",
    "低": "Low",
    "24h 可用率": "24h uptime",
    "路由器运行": "Router uptime",
    "网关信息": "Gateway info",
    "自动探测": "Auto",
    "手动指定": "Manual",
    "网关地址": "Gateway IP",
    "MAC 地址": "MAC address",
    "出口网卡": "Interface",
    "本机地址": "Local IP",
    "网关延迟": "Gateway latency",
    "探测间隔": "Probe interval",
    "{n} 秒": "{n}s",
    "连通性探测": "Connectivity checks",
    "实时结果": "Live results",
    "目标": "Target",
    "类型": "Type",
    "状态": "Status",
    "延迟": "Latency",
    "详情": "Detail",
    "可达": "Reachable",
    "异常": "Unreachable",
    "网关延迟图": "Gateway latency",
    "外网延迟": "WAN latency",
    "丢包率对比": "Packet loss by target",
    "近 5 分钟": "Last 5 min",
    "{n} 个目标 · 近 5 分钟": "{n} targets · last 5 min",
    "网关 {ip}": "Gateway {ip}",
    "外网 {ip}": "WAN {ip}",
    // 默认探测目标的标签（用户自定义标签查不到就原样显示）
    "百度首页": "Baidu homepage",
    "百度 :443": "Baidu :443",
    "阿里 DNS :53": "AliDNS :53",
    // 型号识别来源
    "MAC 厂商": "MAC vendor",
    "MAC 厂商（联网）": "MAC vendor (online)",
    "华为设备信息接口": "Huawei device-info API",
    "小米路由接口": "Xiaomi router API",
    "华为 Web UI 产品代号": "Huawei Web UI product code",
    "UPnP 设备描述": "UPnP device description",
    "登录页指纹": "Login page fingerprint",
    "页面关键字": "page keywords",
    "探测 {n} 次": "{n} probes",
    "握手成功": "handshake ok",
    "解析失败": "resolve failed",
    "解析成功": "resolved",
    "无响应": "no response",
    "超时": "timeout",

    // ---------------------------------------------------------------- 网络页
    "网卡速率": "Interface throughput",
    "网卡列表": "Interfaces",
    "接口": "Interface",
    "地址": "Address",
    "下行": "Down",
    "上行": "Up",
    "累计": "Total",
    "磁盘读写": "Disk I/O",
    "磁盘空间": "Disk usage",
    "CPU 占用进程": "Top CPU processes",
    "前 8 位": "Top 8",
    "进程": "Process",
    "内存": "Memory",
    "读取": "Read",
    "写入": "Write",
    "{n} 个接口": "{n} interfaces",
    "未分配": "unassigned",
    "已连接": "Connected",
    "未连接": "Down",
    "未发现可用磁盘": "No usable disk found",
    "暂无数据": "No data",
    "Wi-Fi": "Wi-Fi",

    // ---------------------------------------------------------------- 设备
    "已发现设备": "Discovered devices",
    "扫描网段": "Scan subnet",
    "IP 地址": "IP address",
    "主机名": "Hostname",
    "操作系统": "Operating system",
    "{n} 次": "{n}",
    "最近在线": "Last seen",
    "网关": "Gateway",
    "尚未发现设备，点击「扫描网段」开始探测": "No devices yet — click “Scan subnet” to start",
    "{n} 台在线": "{n} online",
    "扫描中…": "Scanning…",
    "扫描完成": "Scan finished",
    "扫描失败: {e}": "Scan failed: {e}",
    "已扫描 {n} 个地址": "Scanned {n} addresses",
    "无法确定本机网段": "Cannot determine local subnet",

    // ---------------------------------------------------------------- 测速
    "网速测试": "Speed test",
    "点击开始，约需 15 秒": "Click start — takes about 15s",
    "测速会占满带宽，期间其他设备可能变慢": "The test saturates your link; other devices may slow down",
    "开始测速": "Start test",
    "测速中…": "Testing…",
    "启动中…": "Starting…",
    "准备就绪": "Ready",
    "测速已开始": "Speed test started",
    "无法开始测速": "Cannot start the test",
    "启动失败: {e}": "Start failed: {e}",
    "完成于 {t}": "Finished {t}",
    "延迟对比": "Latency comparison",
    "空闲": "Idle",
    "负载": "Loaded",
    "空闲延迟取自最近 1 分钟的探测记录": "Idle latency comes from the last minute of probes",
    "正在测试 {p}，进度 {n}%": "Testing {p}, {n}% done",
    "负载下延迟没有上升（{a} → {b} ms），链路缓存健康，满载时也不会明显卡顿。":
      "Latency did not rise under load ({a} → {b} ms) — buffering looks healthy.",
    "负载延迟比空闲高出 {d} ms（{r}×）。": "Loaded latency is {d} ms higher ({r}×).",
    "基本无排队，链路缓存健康": "Almost no queueing — healthy buffering",
    "有一定排队，高峰时会感到卡顿": "Some queueing — you may feel it at peak times",
    "明显缓冲膨胀（bufferbloat），可开启路由器的 SQM/QoS":
      "Clear bufferbloat — consider enabling SQM/QoS on the router",
    "上行未测：{e}": "Upload skipped: {e}",
    "可在设置里换一个接受 POST 的上传地址。": "Try another upload endpoint that accepts POST.",
    "历史记录": "History",
    "最近 10 次": "Last 10 runs",
    "时间": "Time",
    "测试服务器": "Test server",
    "还没有测速记录": "No speed tests yet",
    "检测测试服务器": "Checking test servers",
    "检测上传服务器": "Checking upload server",
    "测试下行速率": "Testing download",
    "测试上行速率": "Testing upload",
    "汇总结果": "Summarizing",
    "测速完成": "Test complete",
    "测速失败": "Test failed",
    "测速超时": "Test timed out",
    "测试服务器不可达": "Test servers unreachable",
    "未配置下载测速地址": "No download URL configured",
    "测速异常结束": "Test ended unexpectedly",
    "测速超过 120 秒，已自动中止": "Test exceeded 120s and was aborted",
    "所有下载测速地址都无法连接，请检查网络或更换地址":
      "None of the download URLs responded — check your network or URL",
    "请在设置里填写下载测速地址": "Please set a download URL in Settings",
    "上传测速服务器不可达，已跳过": "Upload server unreachable — skipped",
    "测速步骤超时，已中止": "A step timed out — aborted",
    "上传地址无效": "Invalid upload URL",

    // ---------------------------------------------------------------- 事件页
    "事件记录": "Event log",
    "全部": "All",
    "路由器不可达": "Router unreachable",
    "路由器链路丢包": "Packet loss to router",
    "局域网延迟偏高": "LAN latency high",
    "网络抖动明显": "High jitter",
    "外网连接中断": "WAN down",
    "本机 CPU 占用过高": "Host CPU high",
    "本机内存占用过高": "Host memory high",
    "磁盘空间不足": "Disk almost full",
    "本机网络接口断开": "Host interfaces down",
    "网关近 5 分钟丢包 {loss}%（{n} 次探测）": "Gateway loss {loss}% over 5 min ({n} probes)",
    "近 5 分钟丢包 {loss}%，超过阈值 {thr}%": "Loss {loss}% over 5 min, above the {thr}% threshold",
    "网关平均延迟 {ms} ms，超过阈值 {thr} ms": "Gateway latency {ms} ms, above the {thr} ms threshold",
    "网关延迟偏高": "Gateway latency high",
    "网关抖动 {ms} ms，超过阈值 {thr} ms": "Gateway jitter {ms} ms, above the {thr} ms threshold",
    "网关抖动偏高": "Gateway jitter high",
    "外网目标近 5 分钟全部无响应（{n} 次探测）": "All WAN targets silent for 5 min ({n} probes)",
    "CPU 使用率 {pct}%，阈值 {thr}%": "CPU usage {pct}%, threshold {thr}%",
    "内存使用率 {pct}%，阈值 {thr}%": "Memory usage {pct}%, threshold {thr}%",
    "{mount} 已用 {pct}%": "{mount} is {pct}% full",
    "所有物理网卡均处于 down 状态": "All physical interfaces are down",

    // ---------------------------------------------------------------- 设置
    "监控配置": "Monitoring",
    "保存后立即生效": "Takes effect immediately",
    "语言": "Language",
    "自动（跟随浏览器）": "Auto (follow browser)",
    "界面语言": "Interface language",
    "自动": "Auto",
    "选择只记在这台浏览器上；「自动」跟随浏览器语言": "Saved in this browser only; Auto follows your browser language",
    "中文": "中文",
    "English": "English",
    "路由器网关（设置）": "Gateway address",
    "auto 或 192.168.1.1": "auto or 192.168.1.1",
    "auto 表示跟随系统默认路由自动探测": "auto follows the system default route",
    "路由器名称": "Router name",
    "留空 = 自动识别型号": "Empty = detected model",
    "留空则显示自动识别到的型号；填了就以你写的为准":
      "Leave empty to show the detected model; a value always wins",
    "自动识别型号": "Detect router model",
    "开启": "On",
    "关闭": "Off",
    "识别缓存（小时）": "Detection cache (hours)",
    "网关探测间隔（秒）": "Gateway interval (s)",
    "外网探测间隔（秒）": "WAN interval (s)",
    "外网探测目标": "WAN targets",
    "多个 IP 用英文逗号分隔": "Separate multiple IPs with commas",
    "DNS 测试域名": "DNS test domain",
    "采样间隔（秒）": "Sample interval (s)",
    "数据保留（天）": "Retention (days)",
    "测速时长（秒）": "Test duration (s)",
    "上传测速大小（MB）": "Upload size (MB)",
    "下载测速地址": "Download URLs",
    "多个地址用英文逗号分隔，测速前会挑最快的那个":
      "Comma-separated; the fastest one is picked before each test",
    "上传测速地址": "Upload URLs",
    "留空则不测上行；地址需接受匿名 POST": "Empty skips upload; the URL must accept anonymous POST",
    "保存配置": "Save",
    "立即探测": "Probe now",
    "已保存 ✓": "Saved ✓",
    "配置已保存，新的探测参数立即生效": "Saved — new settings are live",
    "保存失败: {e}": "Save failed: {e}",
    "读取配置失败": "Failed to load settings",
    "已触发一轮探测": "Probe round triggered",
    "触发失败: {e}": "Failed: {e}",
    "告警阈值": "Alert thresholds",
    "连续命中才触发": "Needs consecutive hits",
    "丢包率（%）": "Packet loss (%)",
    "延迟（ms）": "Latency (ms)",
    "抖动（ms）": "Jitter (ms)",
    "CPU（%）": "CPU (%)",
    "内存（%）": "Memory (%)",
    "磁盘（%）": "Disk (%)",
    "保存阈值": "Save thresholds",
    "数据与导出": "Data & export",
    "导出网关 24h CSV": "Export gateway 24h CSV",
    "导出外网 24h CSV": "Export WAN 24h CSV",
    "运行信息": "Runtime info",
    "系统": "System",
    "发行版": "Distribution",
    "内核": "Kernel",
    "架构": "Architecture",
    "桌面环境": "Desktop",
    "机型": "Model",
    "虚拟化": "Virtualization",
    "开机时长": "Uptime",
    "核心数": "Cores",
    "监听地址": "Listen address",
    "监控运行": "Monitor uptime",
    "保留策略": "Retention",
    "延迟样本": "Latency samples",
    "系统样本": "System samples",
    "探测记录": "Check records",
    "事件": "Events",
    "设备": "Devices",
    "数据库大小": "Database size",
    "{n} 条": "{n}",
    "{n} 台": "{n}",
    "物理机": "Bare metal",
    "容器": "Container",
    "虚拟机": "Virtual machine",
    "本机系统": "Host system",
    "自动识别本机系统": "Auto-detect host system",
    "版本": "Version",
    "会话": "Session",
    "未识别": "Not identified",
    "未探测": "Not probed",
    "识别结果": "Detection result",
    "延迟 空闲/负载": "Latency idle/loaded",
    "台式机": "Desktop",
    "笔记本": "Laptop",
    "一体机": "All-in-one",
    "服务器": "Server",
    "平板": "Tablet",
    "手机": "Phone",
    "掌机": "Handheld",
    "宿主机": "Host machine",

    // ---------------------------------------------------------------- 时间
    "{n} 分": "{n}m",
    "{n} 小时": "{n}h",
    "{n} 天": "{n}d",
    "{d} 天 {h} 小时": "{d}d {h}h",
    "{h} 小时 {m} 分": "{h}h {m}m",
    "刚刚": "just now",
    "{n} 秒前": "{n}s ago",
    "{n} 分钟前": "{n} min ago",
    "{n} 小时前": "{n}h ago",
    "{n} 天前": "{n}d ago",
    "天": "d",
    "小时": "h",
    "分": "m",
    "秒": "s",

    // ---------------------------------------------------------------- 杂项
    "图表加载失败": "Failed to load charts",
    "测速状态读取失败": "Failed to read speed test status",
    "正在采集数据…": "Collecting data…",
  };

  const STORAGE_KEY = "rc.lang";
  const state = { lang: "zh", source: "auto" };

  function normalize(value) {
    const v = String(value || "").toLowerCase();
    if (v.indexOf("zh") === 0) return "zh";
    if (v.indexOf("en") === 0) return "en";
    return null;
  }

  function detect() {
    // URL 参数优先级最高：方便分享链接 / 值守大屏固定语言
    try {
      const fromUrl = normalize(new URLSearchParams(location.search).get("lang"));
      if (fromUrl) return fromUrl;
    } catch (err) {
      /* 忽略 */
    }
    const saved = normalize(localStorage.getItem(STORAGE_KEY));
    if (saved) return saved;
    return normalize(navigator.language || navigator.userLanguage) || "zh";
  }

  function t(key, vars) {
    let s = key == null ? "" : String(key);
    if (state.lang === "en") s = DICT[s] != null ? DICT[s] : s;
    if (vars) {
      s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? String(vars[k]) : m));
    }
    return s;
  }

  function applyDom(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.dataset.i18n);
    });
    scope.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      el.placeholder = t(el.dataset.i18nPh);
    });
    scope.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.title = t(el.dataset.i18nTitle);
    });
    document.documentElement.lang = state.lang === "en" ? "en" : "zh-CN";
    const title = state.lang === "en" ? "RouterCat · Router monitor" : "RouterCat · 路由器监控";
    if (document.title !== title) document.title = title;
  }

  function setLang(lang, opts) {
    const next = normalize(lang) || detect();
    state.lang = next;
    if (!opts || !opts.silent) localStorage.setItem(STORAGE_KEY, next);
    applyDom();
    document.dispatchEvent(new CustomEvent("rc:langchange", { detail: { lang: next } }));
    return next;
  }

  function init(preferred) {
    const chosen = normalize(preferred) || detect();
    state.lang = chosen;
    applyDom();
    return chosen;
  }

  window.RCI18n = {
    t,
    init,
    setLang,
    applyDom,
    get lang() {
      return state.lang;
    },
    get raw() {
      return DICT;
    },
  };
})();
