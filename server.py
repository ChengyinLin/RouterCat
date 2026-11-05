"""RouterCat - Web 服务与 HTTP API。

启动：python3 server.py [--port 8848] [--host 0.0.0.0]
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routercat import __version__  # noqa: E402
from routercat.config import BASE_DIR, DB_PATH, Config  # noqa: E402
from routercat.db import DB  # noqa: E402
from routercat.monitor import GW_KEY, Monitor  # noqa: E402

STATIC_DIR = os.path.join(BASE_DIR, "static")
RANGES = {
    "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800,
    "6h": 21600, "12h": 43200, "24h": 86400, "7d": 604800,
}

log = logging.getLogger("routercat.http")


def parse_range(value: str) -> int:
    if value in RANGES:
        return RANGES[value]
    if value.endswith("s") and value[:-1].isdigit():
        return int(value[:-1])
    return 1800


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = DB(DB_PATH)
        self.monitor = Monitor(cfg, self.db)

    # ------------------------------------------------------------- 数据接口
    def series(self, params: dict) -> dict:
        metric = params.get("metric", "ping")
        rng = params.get("range", "30m")
        seconds = parse_range(rng)
        points = max(30, min(720, int(float(params.get("points", 180)))))
        bucket = max(1, seconds // points)
        since = int(time.time()) - seconds
        out = {"metric": metric, "range": rng, "bucket": bucket, "from": since, "to": int(time.time())}

        if metric == "ping":
            target = params.get("target", "all")
            if target == "all":
                keys = [(GW_KEY, "网关")]
                keys += [(t, f"外网 {t}") for t in (self.cfg.get("probe.wan_targets", []) or [])]
            else:
                keys = [(target, "网关" if target == GW_KEY else target)]
            series = []
            for key, name in keys:
                if key != GW_KEY and not self.db.query_one(
                    "SELECT 1 FROM ping WHERE target=? LIMIT 1", (key,)
                ):
                    continue
                rows = self.db.ping_series(key, since, bucket)
                series.append({
                    "key": key,
                    "name": name,
                    "points": [[r["t"], r["avg"], r["loss"]] for r in rows],
                })
            out["series"] = series
            return out

        if metric == "sys":
            series = []
            for field, name in (("cpu", "CPU"), ("mem_pct", "内存")):
                rows = self.db.sys_series(field, since, bucket)
                series.append({"key": field, "name": name, "points": [[r["t"], r["v"]] for r in rows]})
            out["series"] = series
            return out

        if metric == "net":
            iface = params.get("iface")
            ifaces = [i["name"] for i in (self.monitor.host.get("interfaces") or []) if i["name"] != "lo"]
            if iface and iface != "all":
                ifaces = [iface]
            series = []
            for name in ifaces:
                rows = self.db.net_series(name, since, bucket)
                series.append({
                    "key": name,
                    "dir": "rx",
                    "name": f"{name} 下行",
                    "points": [[r["t"], r["rx"]] for r in rows],
                })
                series.append({
                    "key": name,
                    "dir": "tx",
                    "name": f"{name} 上行",
                    "points": [[r["t"], r["tx"]] for r in rows],
                })
            out["series"] = series
            return out

        if metric == "disk":
            rows = self.db.disk_series(since, bucket)
            out["series"] = [
                {"key": "read", "name": "读取", "points": [[r["t"], r["rd"]] for r in rows]},
                {"key": "write", "name": "写入", "points": [[r["t"], r["wr"]] for r in rows]},
            ]
            return out

        raise ValueError(f"未知指标: {metric}")

    def export_csv(self, params: dict) -> str:
        target = params.get("target", GW_KEY)
        seconds = parse_range(params.get("range", "24h"))
        since = int(time.time()) - seconds
        rows = self.db.query(
            "SELECT ts, ok, rtt FROM ping WHERE target=? AND ts>=? ORDER BY ts", (target, since)
        )
        lines = ["timestamp,datetime,target,ok,rtt_ms"]
        for r in rows:
            dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            lines.append(f"{r['ts']},{dt},{target},{r['ok']},{r['rtt'] if r['rtt'] is not None else ''}")
        return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    server_version = "RouterCat/1.0"
    protocol_version = "HTTP/1.1"
    app: App = None  # 由 serve() 注入
    quiet = True

    # ---------------------------------------------------------------- 工具
    def log_message(self, fmt, *args):  # 静音访问日志
        if not self.quiet:
            log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str):
        self._json({"ok": False, "error": message}, code)

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ---------------------------------------------------------------- 路由
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                return self._api_get(path, params)
            return self._static(path)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s 失败", path)
            return self._error(500, str(exc))

    do_HEAD = do_GET

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/config":
                patch = self._body_json()
                self.app.cfg.patch(patch)
                self.app.monitor.alerts.reset()
                return self._json({"ok": True, "config": self.app.cfg.all()})
            if path == "/api/devices/scan":
                return self._json({"ok": True, **self.app.monitor.scan_devices()})
            if path == "/api/probe/now":
                self.app.monitor._probe_tick()
                return self._json({"ok": True})
            if path == "/api/router/identify":
                gw = self.app.monitor.gateway or {}
                started = self.app.monitor.identity.identify_async(gw.get("ip"), gw.get("mac"), "manual")
                return self._json({
                    "ok": True,
                    "started": started,
                    "message": "正在识别…" if started else "识别已在进行中",
                })
            if path == "/api/speedtest/start":
                ok, msg = self.app.monitor.speedtest.start()
                return self._json({"ok": ok, "message": msg})
            return self._error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s 失败", path)
            return self._error(500, str(exc))

    def _api_get(self, path: str, params: dict):
        app = self.app
        if path == "/api/overview":
            return self._json(app.monitor.snapshot())
        if path == "/api/series":
            return self._json(app.series(params))
        if path == "/api/devices":
            now = int(time.time())
            devices = app.db.devices()
            for d in devices:
                d["online"] = bool(d.get("last_seen") and now - d["last_seen"] < 300)
            return self._json({"devices": devices, "gateway": app.monitor.gateway})
        if path == "/api/events":
            limit = int(params.get("limit", 100))
            rows = app.db.recent_events(limit)
            status = params.get("status")
            if status in ("open", "resolved"):
                rows = [r for r in rows if r["status"] == status]
            return self._json({"events": rows})
        if path == "/api/config":
            return self._json({"config": app.cfg.all()})
        if path == "/api/processes":
            return self._json({"processes": app.monitor.host.get("processes", [])})
        if path == "/api/stats":
            return self._json({"db": app.db.stats(), "monitor": app.monitor.counters})
        if path == "/api/health":
            return self._json({"ok": True, "version": __version__, "ts": int(time.time()),
                               "status": app.monitor.overall_status()})
        if path == "/api/speedtest":
            return self._json(app.monitor.speedtest.snapshot())
        if path == "/api/router/identify":
            return self._json({
                "ok": True,
                "gateway": app.monitor.gateway,
                "router": app.monitor.identity.view(),
            })
        if path == "/api/export":
            csv = app.export_csv(params)
            name = f"routercat-{params.get('target', 'gateway')}-{int(time.time())}.csv"
            self._send(
                200,
                csv.encode("utf-8-sig"),
                "text/csv; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{name}"'},
            )
            return
        return self._error(404, "not found")

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        if ".." in rel or rel.startswith("."):
            return self._error(403, "forbidden")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            # 单页应用回退
            full = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(full):
                return self._error(404, "not found")
        ctype, _ = mimetypes.guess_type(full)
        if full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif full.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, ctype or "application/octet-stream")


def local_ips() -> list:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def serve(host: str | None = None, port: int | None = None, quiet: bool = True) -> None:
    logging.basicConfig(
        level=logging.INFO if not quiet else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config()
    host = host or cfg.get("app.listen_host", "0.0.0.0")
    port = int(port or cfg.get("app.listen_port", 8848))

    app = App(cfg)
    app.monitor.start()

    Handler.app = app
    Handler.quiet = quiet
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    print(f"\n  RouterCat 已启动  v{__version__}")
    print(f"  本机访问   http://127.0.0.1:{port}")
    for ip in local_ips():
        print(f"  局域网     http://{ip}:{port}")
    print(f"  网关       {app.monitor.gateway.get('ip') or '未探测到'}  ({app.monitor.gateway.get('iface') or '-'})")
    print("  Ctrl+C 停止\n", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在停止…")
    finally:
        app.monitor.stop()
        httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="RouterCat - 路由器状态监控面板")
    ap.add_argument("--host", default=None, help="监听地址，默认读取配置（0.0.0.0）")
    ap.add_argument("--port", type=int, default=None, help="监听端口，默认 8848")
    ap.add_argument("--verbose", action="store_true", help="输出访问日志")
    args = ap.parse_args()
    try:
        serve(args.host, args.port, quiet=not args.verbose)
    except OSError as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
