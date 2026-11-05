"""RouterCat - 告警引擎：连续命中才算触发，恢复后自动关闭事件。

规则只产出 `key` + `params`（数值），中英文文案分别由前端词典渲染，
这样历史事件在切换语言时也能跟着变；落到数据库的 title/detail 保留中文，
供 CSV 导出和第三方调用方使用。
"""
from __future__ import annotations

import time

LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}

# key -> (中文标题, 中文详情模板)
ALERT_TEXT = {
    "gateway_offline": ("路由器不可达", "网关近 5 分钟丢包 {loss}%（{n} 次探测）"),
    "gateway_loss": ("路由器链路丢包", "近 5 分钟丢包 {loss}%，超过阈值 {thr}%"),
    "gateway_latency": ("局域网延迟偏高", "网关平均延迟 {ms} ms，超过阈值 {thr} ms"),
    "gateway_jitter": ("网络抖动明显", "网关抖动 {ms} ms，超过阈值 {thr} ms"),
    "wan_offline": ("外网连接中断", "外网目标近 5 分钟全部无响应（{n} 次探测）"),
    "cpu_high": ("本机 CPU 占用过高", "CPU 使用率 {pct}%，阈值 {thr}%"),
    "mem_high": ("本机内存占用过高", "内存使用率 {pct}%，阈值 {thr}%"),
    "disk_high": ("磁盘空间不足", "{mount} 已用 {pct}%"),
    "link_down": ("本机网络接口断开", "所有物理网卡均处于 down 状态"),
}


def _render(key: str, params: dict) -> tuple[str, str]:
    title, tpl = ALERT_TEXT.get(key, (key, ""))
    try:
        detail = tpl.format(**params) if params else tpl
    except (KeyError, IndexError, ValueError):
        detail = tpl
    return title, detail


class AlertEngine:
    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg
        self._counters: dict[str, int] = {}
        self.active: dict[str, dict] = {}

    # ------------------------------------------------------------------ 内部
    def _rule(self, key: str, level: str, params: dict, triggered: bool, hold: int = 3) -> None:
        now = int(time.time())
        title, detail = _render(key, params)
        if triggered:
            self._counters[key] = self._counters.get(key, 0) + 1
            if self._counters[key] >= hold and key not in self.active:
                event_id = self.db.open_event(key, level, title, detail, now, params)
                self.active[key] = {
                    "key": key,
                    "code": key,
                    "level": level,
                    "title": title,
                    "detail": detail,
                    "params": params,
                    "since": now,
                    "event_id": event_id,
                }
            elif key in self.active:
                self.active[key]["detail"] = detail
                self.active[key]["params"] = params
        else:
            self._counters[key] = 0
            if key in self.active:
                info = self.active.pop(key)
                self.db.close_event(info["event_id"], now)

    # ------------------------------------------------------------------ 评估
    def evaluate(self, stats: dict) -> list:
        """stats 需要包含 gw5 / wan5 (ping 统计字典) 与 host (系统快照)。"""
        thr = self.cfg.get("thresholds", {}) or {}
        loss_thr = float(thr.get("loss_pct", 5))
        lat_thr = float(thr.get("latency_ms", 80))
        jit_thr = float(thr.get("jitter_ms", 40))

        gw = stats.get("gw5") or {}
        wan = stats.get("wan5") or {}
        host = stats.get("host") or {}

        # 1. 网关不可达
        gw_loss = gw.get("loss", 100.0)
        gw_avg = gw.get("avg")
        gw_samples = gw.get("n", 0)
        self._rule(
            "gateway_offline",
            "critical",
            {"loss": f"{gw_loss:.0f}", "n": gw_samples},
            bool(gw_samples >= 5 and gw_loss >= 99.9),
            hold=2,
        )
        # 2. 网关丢包
        self._rule(
            "gateway_loss",
            "warning",
            {"loss": f"{gw_loss:.1f}", "thr": f"{loss_thr:.0f}"},
            bool(gw_samples >= 10 and loss_thr < gw_loss < 99.9),
            hold=3,
        )
        # 3. 网关延迟过高
        self._rule(
            "gateway_latency",
            "warning",
            {"ms": f"{gw_avg:.1f}" if gw_avg else "-", "thr": f"{lat_thr:.0f}"},
            bool(gw_avg is not None and gw_avg > lat_thr),
            hold=3,
        )
        # 4. 网关抖动
        gw_jitter = gw.get("jitter")
        self._rule(
            "gateway_jitter",
            "warning",
            {"ms": f"{gw_jitter:.1f}" if gw_jitter else "-", "thr": f"{jit_thr:.0f}"},
            bool(gw_jitter is not None and gw_jitter > jit_thr),
            hold=3,
        )
        # 5. 外网中断
        wan_loss = wan.get("loss", 0.0)
        wan_n = wan.get("n", 0)
        self._rule(
            "wan_offline",
            "critical",
            {"n": wan_n},
            bool(wan_n >= 3 and wan_loss >= 99.9),
            hold=2,
        )
        # 6. 本机负载
        cpu = float(host.get("cpu") or 0)
        cpu_thr = float(thr.get("cpu_pct", 90))
        self._rule(
            "cpu_high",
            "warning",
            {"pct": f"{cpu:.0f}", "thr": f"{cpu_thr:.0f}"},
            cpu > cpu_thr,
            hold=5,
        )
        mem = (host.get("mem") or {}).get("pct", 0) or 0
        mem_thr = float(thr.get("mem_pct", 92))
        self._rule(
            "mem_high",
            "warning",
            {"pct": f"{mem:.0f}", "thr": f"{mem_thr:.0f}"},
            mem > mem_thr,
            hold=5,
        )
        disks = host.get("disks") or []
        worst = max(disks, key=lambda d: d.get("pct", 0), default=None)
        disk_thr = float(thr.get("disk_pct", 90))
        self._rule(
            "disk_high",
            "warning",
            {"mount": worst["mount"] if worst else "-", "pct": f"{worst['pct']:.0f}" if worst else "0"},
            bool(worst and worst.get("pct", 0) > disk_thr),
            hold=2,
        )
        # 7. 本机网络接口全部 down
        ifaces = [i for i in (host.get("interfaces") or []) if i["name"] != "lo"]
        self._rule(
            "link_down",
            "warning",
            {},
            bool(ifaces) and all(i.get("state") not in ("up", "unknown") for i in ifaces),
            hold=3,
        )

        return self.snapshot()

    def snapshot(self) -> list:
        return sorted(self.active.values(), key=lambda a: (LEVEL_ORDER.get(a["level"], 9), -a["since"]))

    def reset(self) -> None:
        self._counters.clear()
