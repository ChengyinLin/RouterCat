"""RouterCat - 采集调度中心：系统采样线程 + 网络探测线程。"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .alerts import AlertEngine
from .config import DATA_DIR, Config
from .fingerprint import RouterIdentity
from .netprobe import (
    arp_table,
    detect_gateway,
    dns_check,
    http_check,
    ping,
    resolve_hostname,
    tcp_check,
)
from .speedtest import SpeedTestManager
from .sysinfo import SystemCollector

log = logging.getLogger("routercat.monitor")
GW_KEY = "gateway"


def _pct(values: list, q: float):
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 2)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[idx], 2)


class Monitor:
    def __init__(self, cfg: Config, db) -> None:
        self.cfg = cfg
        self.db = db
        self.sys = SystemCollector()
        self.alerts = AlertEngine(db, cfg)
        self.host: dict = {}
        self.gateway: dict = {"ip": None, "iface": None, "local_ip": None, "mac": None, "prefix": None}
        self.latest_ping: dict[str, dict] = {}
        self.latest_checks: dict[str, dict] = {}
        self.history: dict[str, deque] = {}
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._due: dict[str, float] = {}
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="probe")
        self._threads: list[threading.Thread] = []
        self._next_gw_check = 0.0
        self._next_cleanup = 0.0
        self._next_arp = 0.0
        self.started_at = time.time()
        self.counters = {"ping": 0, "checks": 0, "errors": 0}
        self.speedtest = SpeedTestManager(db, cfg, self)
        self.identity = RouterIdentity(cfg, os.path.join(DATA_DIR, "router_identity.json"))

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.gateway = detect_gateway() or self.gateway
        self.host = self.sys.snapshot()
        for name, fn in (("sampler", self._sample_loop), ("prober", self._probe_loop)):
            t = threading.Thread(target=fn, name=f"routercat-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        # 识别型号要发 HTTP/SSDP，放到后台跑，不挡启动
        if self.cfg.get("router.auto_detect", True):
            self.identity.identify_async(self.gateway.get("ip"), self.gateway.get("mac"), "startup")
        log.info("监控已启动，网关 %s", self.gateway.get("ip"))

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)

    # ------------------------------------------------------------------ 采样线程
    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self._sample_once()
            except Exception:  # noqa: BLE001
                log.exception("采样失败")
            interval = max(1, int(self.cfg.get("sample.interval", 2)))
            self._stop.wait(max(0.2, interval - (time.time() - started)))

    def _sample_once(self) -> None:
        snap = self.sys.snapshot()
        self.host = snap
        ts = snap["ts"]
        self.db.add_sys(ts, snap["cpu"], snap["per_core"], snap["mem"], snap["load"][0])
        for iface in snap["interfaces"]:
            if iface["name"] == "lo":
                continue
            self.db.add_net(ts, iface["name"], iface["rx_bps"], iface["tx_bps"])
        self.db.add_disk_io(ts, snap["disk_io"]["read_bps"], snap["disk_io"]["write_bps"])
        self._evaluate_alerts()

        now = time.time()
        if now >= self._next_cleanup:
            self._next_cleanup = now + 1800
            days = float(self.cfg.get("retention.days", 7) or 7)
            try:
                removed = self.db.cleanup(days)
                if removed:
                    log.info("清理 %s 条过期样本（保留 %s 天）", removed, days)
            except Exception:  # noqa: BLE001
                log.exception("清理过期数据失败")
        if now >= self._next_arp:
            self._next_arp = now + 30
            try:
                self._refresh_devices()
            except Exception:  # noqa: BLE001
                log.exception("刷新设备列表失败")

    def _evaluate_alerts(self) -> None:
        gw5 = self.window_stats(GW_KEY, 300)
        wan5 = self.aggregate_wan(300)
        self.alerts.evaluate({"gw5": gw5, "wan5": wan5, "host": self.host})

    # ------------------------------------------------------------------ 探测线程
    def _probe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._probe_tick()
            except Exception:  # noqa: BLE001
                log.exception("探测调度失败")
            self._stop.wait(0.4)

    def _due_now(self, key: str, interval: float, now: float) -> bool:
        if now < self._due.get(key, 0):
            return False
        self._due[key] = now + max(0.5, interval)
        return True

    def _dispatch(self, key: str, fn, *args) -> None:
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)

        def runner():
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                self.counters["errors"] += 1
                log.debug("探测 %s 失败", key, exc_info=True)
            finally:
                with self._lock:
                    self._inflight.discard(key)

        self._pool.submit(runner)

    def _probe_tick(self) -> None:
        now = time.time()
        probe = self.cfg.get("probe", {}) or {}
        timeout = int(probe.get("ping_timeout", 1) or 1)

        if now >= self._next_gw_check:
            self._next_gw_check = now + 60
            gw = detect_gateway()
            if gw.get("ip"):
                if gw.get("ip") != self.gateway.get("ip"):
                    log.info("网关地址更新为 %s", gw["ip"])
                self.gateway = gw
            # 网关换了 / 缓存过期 → 重新识别型号（内部有节流，不会反复打）
            try:
                self.identity.maybe_refresh(self.gateway.get("ip"), self.gateway.get("mac"))
            except Exception:  # noqa: BLE001
                log.debug("型号识别调度失败", exc_info=True)

        gw_ip = self.gateway.get("ip")
        if gw_ip and self._due_now("ping:gw", float(probe.get("gateway_interval", 2)), now):
            self._dispatch("ping:gw", self._do_ping, GW_KEY, gw_ip, timeout)

        for target in probe.get("wan_targets", []) or []:
            if self._due_now(f"ping:{target}", float(probe.get("wan_interval", 5)), now):
                self._dispatch(f"ping:{target}", self._do_ping, target, target, timeout)

        for host in probe.get("dns_targets", []) or []:
            if self._due_now(f"dns:{host}", float(probe.get("dns_interval", 30)), now):
                self._dispatch(f"dns:{host}", self._do_dns, host)

        for item in probe.get("tcp_targets", []) or []:
            label = item.get("label") or f"{item.get('host')}:{item.get('port')}"
            if self._due_now(f"tcp:{label}", float(probe.get("tcp_interval", 15)), now):
                self._dispatch(f"tcp:{label}", self._do_tcp, item, label)

        for item in probe.get("http_targets", []) or []:
            label = item.get("label") or item.get("url")
            if self._due_now(f"http:{label}", float(probe.get("http_interval", 60)), now):
                self._dispatch(f"http:{label}", self._do_http, item, label)

    def _do_ping(self, key: str, host: str, timeout: int) -> None:
        res = ping(host, timeout=timeout)
        ts = int(time.time())
        self.db.add_ping(ts, key, res["ok"], res["rtt"])
        self.latest_ping[key] = {"ts": ts, "host": host, **res}
        self.counters["ping"] += 1
        hist = self.history.setdefault(key, deque(maxlen=600))
        hist.append((ts, res["rtt"] if res["ok"] else None))

    def _do_dns(self, host: str) -> None:
        res = dns_check(host)
        ts = int(time.time())
        self.db.add_check(ts, "dns", host, res["ok"], res["latency"], res["detail"])
        self.latest_checks[f"dns:{host}"] = {
            "ts": ts, "kind": "dns", "target": host, **res
        }
        self.counters["checks"] += 1

    def _do_tcp(self, item: dict, label: str) -> None:
        res = tcp_check(item.get("host"), int(item.get("port", 80)))
        ts = int(time.time())
        self.db.add_check(ts, "tcp", label, res["ok"], res["latency"], res["detail"])
        self.latest_checks[f"tcp:{label}"] = {
            "ts": ts, "kind": "tcp", "target": label, **res
        }
        self.counters["checks"] += 1

    def _do_http(self, item: dict, label: str) -> None:
        res = http_check(item.get("url"))
        ts = int(time.time())
        self.db.add_check(ts, "http", label, res["ok"], res["latency"], res["detail"])
        self.latest_checks[f"http:{label}"] = {
            "ts": ts, "kind": "http", "target": label, **res
        }
        self.counters["checks"] += 1

    # ------------------------------------------------------------------ 设备
    def _refresh_devices(self) -> None:
        for entry in arp_table():
            known = False
            hostname = None
            row = self.db.query_one("SELECT ip, hostname FROM devices WHERE ip=?", (entry["ip"],))
            if row and row.get("hostname"):
                hostname = row["hostname"]
                known = True
            if not known:
                hostname = resolve_hostname(entry["ip"])
            self.db.touch_device(entry["ip"], entry["mac"], entry["iface"], hostname)

    def scan_devices(self) -> dict:
        gw = self.gateway or {}
        local, prefix = gw.get("local_ip"), gw.get("prefix")
        if not local or prefix is None:
            return {"ok": False, "message": "无法确定本机网段", "scanned": 0}
        from .netprobe import scan_subnet

        n = scan_subnet(local, int(prefix))
        self._refresh_devices()
        return {"ok": True, "message": f"已扫描 {n} 个地址", "scanned": n}

    # ------------------------------------------------------------------ 统计
    def window_stats(self, key: str, seconds: int) -> dict:
        since = int(time.time() - seconds)
        rows = self.db.query(
            "SELECT ts, ok, rtt FROM ping WHERE target=? AND ts>=? ORDER BY ts", (key, since)
        )
        n = len(rows)
        if not n:
            latest = self.db.ping_latest(key)
            return {
                "n": 0,
                "loss": 0.0,
                "avg": None,
                "min": None,
                "max": None,
                "jitter": None,
                "p95": None,
                "last_ok": bool(latest and latest["ok"]),
                "last_rtt": latest["rtt"] if latest else None,
                "last_ts": latest["ts"] if latest else None,
            }
        rtts = [r["rtt"] for r in rows if r["ok"] and r["rtt"] is not None]
        lost = sum(1 for r in rows if not r["ok"])
        diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
        return {
            "n": n,
            "loss": round(lost / n * 100, 2),
            "avg": round(sum(rtts) / len(rtts), 2) if rtts else None,
            "min": round(min(rtts), 2) if rtts else None,
            "max": round(max(rtts), 2) if rtts else None,
            "jitter": round(sum(diffs) / len(diffs), 2) if diffs else 0.0,
            "p95": _pct(rtts, 0.95),
            "last_ok": bool(rows[-1]["ok"]),
            "last_rtt": rows[-1]["rtt"],
            "last_ts": rows[-1]["ts"],
        }

    def aggregate_wan(self, seconds: int) -> dict:
        targets = self.cfg.get("probe.wan_targets", []) or []
        if not targets:
            return {"n": 0, "loss": 0.0, "avg": None, "jitter": None}
        since = int(time.time() - seconds)
        placeholders = ",".join("?" * len(targets))
        rows = self.db.query(
            f"SELECT target, ts, ok, rtt FROM ping WHERE target IN ({placeholders}) AND ts>=? ORDER BY ts",
            (*targets, since),
        )
        if not rows:
            return {"n": 0, "loss": 0.0, "avg": None, "jitter": None, "p95": None}
        rtts = [r["rtt"] for r in rows if r["ok"] and r["rtt"] is not None]
        lost = sum(1 for r in rows if not r["ok"])
        return {
            "n": len(rows),
            "loss": round(lost / len(rows) * 100, 2),
            "avg": round(sum(rtts) / len(rtts), 2) if rtts else None,
            "jitter": None,
            "p95": _pct(rtts, 0.95),
            "min": round(min(rtts), 2) if rtts else None,
            "max": round(max(rtts), 2) if rtts else None,
        }

    def overall_status(self) -> str:
        gw = self.window_stats(GW_KEY, 60)
        wan = self.aggregate_wan(60)
        if gw["n"] >= 3 and gw["loss"] >= 99.9:
            return "offline"
        if wan["n"] >= 2 and wan["loss"] >= 99.9:
            return "offline"
        if self.alerts.snapshot():
            return "degraded"
        if (gw["loss"] or 0) > 0 or (gw["avg"] or 0) > 40:
            return "degraded"
        return "online"

    def snapshot(self) -> dict:
        now = int(time.time())
        gw = dict(self.gateway or {})
        gw5 = self.window_stats(GW_KEY, 300)
        gw1h = self.window_stats(GW_KEY, 3600)
        gw24h = self.window_stats(GW_KEY, 86400)
        wan_targets = self.cfg.get("probe.wan_targets", []) or []
        wan_detail = [
            {"target": t, "name": f"外网 {t}", **self.window_stats(t, 300)} for t in wan_targets
        ]
        latest_gw = self.latest_ping.get(GW_KEY) or {}
        gw["latest"] = {
            "ok": latest_gw.get("ok"),
            "rtt": latest_gw.get("rtt"),
            "ts": latest_gw.get("ts"),
        }
        host = self.host
        boot_ts = host.get("boot_ts")
        spark = [
            [r["t"], r["avg"]]
            for r in self.db.ping_series(GW_KEY, now - 600, 5)
            if r["avg"] is not None
        ]
        identity = self.identity.view()
        override = (self.cfg.get("router.name") or "").strip()
        return {
            "now": now,
            "status": self.overall_status(),
            "router": gw,
            "ui": {
                "router_name": override or identity.get("model") or "网关路由器",
                "router_name_manual": bool(override),
                "router_image": self.cfg.get("router.image") or "/img/router.png",
                "router": identity,
            },
            "spark": spark,
            "router_stats": {"m5": gw5, "h1": gw1h, "h24": gw24h},
            "wan": {"targets": wan_detail, "m5": self.aggregate_wan(300), "h24": self.aggregate_wan(86400)},
            "host": host,
            "checks": sorted(self.latest_checks.values(), key=lambda c: (c["kind"], c["target"])),
            "alerts": self.alerts.snapshot(),
            "events": self.db.recent_events(8),
            "devices_count": len(self.db.devices()),
            "monitor": {
                "started_at": int(self.started_at),
                "uptime": int(time.time() - self.started_at),
                "counters": dict(self.counters),
                "boot_ts": boot_ts,
            },
        }
