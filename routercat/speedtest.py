"""RouterCat - 网速测试：下载 / 上传 / 负载延迟（仅用标准库）。"""
from __future__ import annotations

import http.client
import os
import threading
import time
import urllib.request
from collections import deque
from urllib.parse import urlsplit

from .netprobe import ping

CHUNK = 65536
USER_AGENT = "RouterCat/1.0 (+speedtest)"

# 阶段文案 -> 稳定 code（前端按 code 出中英文）
PHASE_CODES = {
    "准备就绪": "idle",
    "检测测试服务器": "probe_servers",
    "检测上传服务器": "probe_upload",
    "测试下行速率": "download",
    "测试上行速率": "upload",
    "汇总结果": "summarize",
    "测速完成": "done",
    "测速失败": "failed",
    "测速超时": "timeout",
    "测试服务器不可达": "servers_down",
    "未配置下载测速地址": "no_url",
    "测速异常结束": "aborted",
}


# ----------------------------------------------------------------- 基础测速
def _open(url: str, timeout: float):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Cache-Control": "no-cache"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def probe_server(url: str, window: float = 1.3, timeout: float = 6.0, max_bytes: int = 24_000_000) -> float:
    """在一小段时间内拉数据，用后段样本估稳态带宽（Mbps），失败返回 0。

    小样本会被 TCP 慢启动严重低估，所以按时间窗取后 60% 的斜率。
    """
    t0 = time.perf_counter()
    total = 0
    samples = []
    try:
        with _open(url, timeout) as resp:
            while True:
                elapsed = time.perf_counter() - t0
                if elapsed >= window or total >= max_bytes:
                    break
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                samples.append((elapsed, total))
    except Exception:  # noqa: BLE001
        return 0.0
    if total <= 0:
        return 0.0
    if len(samples) < 3:
        return total * 8 / max(0.05, window) / 1e6
    cut = window * 0.4
    start = next((s for s in samples if s[0] >= cut), samples[0])
    end = samples[-1]
    dt = max(0.02, end[0] - start[0])
    return max(0.0, (end[1] - start[1]) * 8 / dt / 1e6)


def download(url: str, duration: float, on_progress=None, idle_timeout: float = 5.0,
             max_bytes: int = 220_000_000, max_rounds: int = 6) -> dict:
    """在 duration 秒内持续下载（文件读完会重新连接续测），返回平均与稳态速率。"""
    t0 = time.perf_counter()
    total = 0
    samples = deque()
    steady_bytes = 0
    steady_start = None
    empty_rounds = 0
    rounds = 0
    error = None

    while time.perf_counter() - t0 < duration and total < max_bytes and rounds < max_rounds:
        rounds += 1
        got_any = False
        try:
            with _open(url, idle_timeout) as resp:
                while True:
                    now = time.perf_counter()
                    if now - t0 >= duration:
                        break
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    got_any = True
                    total += len(chunk)
                    if steady_start is None and now - t0 >= 1.0:
                        steady_start = now
                        steady_bytes = 0
                    if steady_start is not None:
                        steady_bytes += len(chunk)
                    samples.append((now, total))
                    while len(samples) > 2 and now - samples[0][0] > 1.0:
                        samples.popleft()
                    if on_progress:
                        inst = 0.0
                        if len(samples) >= 2:
                            dt = samples[-1][0] - samples[0][0]
                            if dt > 0:
                                inst = (samples[-1][1] - samples[0][1]) * 8 / dt / 1e6
                        on_progress(total, inst, min(1.0, (now - t0) / duration))
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:160]
            if total < 200_000:
                return {"ok": False, "error": error, "bytes": total, "mbps": 0.0}
            break
        if not got_any:
            empty_rounds += 1
            if empty_rounds >= 2:
                break

    elapsed = max(0.001, time.perf_counter() - t0)
    steady_elapsed = max(0.001, time.perf_counter() - steady_start) if steady_start else 0.0
    steady = steady_bytes * 8 / steady_elapsed / 1e6 if steady_start and steady_elapsed > 0.2 else 0.0
    avg = total * 8 / elapsed / 1e6
    return {
        "ok": total > 0,
        "bytes": total,
        "mbps": round(steady if steady else avg, 1),
        "avg_mbps": round(avg, 1),
        "steady_mbps": round(steady, 1),
        "seconds": round(elapsed, 1),
        "error": error,
    }


def upload(url: str, size_bytes: int, duration: float, on_progress=None) -> dict:
    """向支持的地址 POST 指定大小的数据，测上行带宽。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"ok": False, "error": "上传地址无效", "bytes": 0, "mbps": 0.0}
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=5)
    payload = os.urandom(CHUNK)
    sent = 0
    t0 = time.perf_counter()
    t_send = None
    last_send = None
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size_bytes))
        conn.putheader("User-Agent", USER_AGENT)
        conn.endheaders()
        while sent < size_bytes:
            if time.perf_counter() - t0 >= duration:
                break
            n = min(CHUNK, size_bytes - sent)
            conn.send(payload if n == CHUNK else payload[:n])
            sent += n
            now = time.perf_counter()
            if t_send is None:
                t_send = now
            last_send = now
            if on_progress:
                on_progress(sent, sent * 8 / max(0.001, now - (t_send or t0)) / 1e6,
                            min(1.0, sent / size_bytes))
        send_seconds = max(0.001, (last_send or time.perf_counter()) - (t_send or t0))
        resp = conn.getresponse()
        resp.read(2048)
    except Exception as exc:  # noqa: BLE001
        conn.close()
        if sent < 300_000:
            return {"ok": False, "error": str(exc)[:160], "bytes": sent, "mbps": 0.0}
        send_seconds = max(0.001, (last_send or time.perf_counter()) - (t_send or t0))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    mbps = round(sent * 8 / max(0.001, send_seconds) / 1e6, 1)
    return {
        "ok": sent > 0,
        "bytes": sent,
        "mbps": mbps,
        "seconds": round(send_seconds, 1),
        "error": None,
    }


# ----------------------------------------------------------------- 任务管理
class SpeedTestManager:
    """同一时间只跑一个测速任务，状态供前端轮询。"""

    def __init__(self, db, cfg, monitor) -> None:
        self.db = db
        self.cfg = cfg
        self.monitor = monitor
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = "idle"
        self.phase = "准备就绪"
        self.phase_code = "idle"
        self.progress = 0.0
        self.live_down = 0.0
        self.live_up = 0.0
        self.result: dict | None = None
        self.error: str | None = None
        self.started_at: int | None = None
        self.updated_at = int(time.time())
        self._samples: list[float] = []
        self._upload_ok: str | None = None  # 记住上次成功的上传地址，后续直接复用

    # -------------------------------------------------------------- 状态
    def _set(self, **kw) -> None:
        # 阶段再给一个稳定的 code，前端据此出中英文文案
        if "phase" in kw and "phase_code" not in kw:
            kw["phase_code"] = PHASE_CODES.get(kw["phase"], "unknown")
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.updated_at = int(time.time())

    def snapshot(self) -> dict:
        with self._lock:
            # 看门狗：任何异常卡死都不允许一直显示“测速中”
            if self.state == "running" and self.started_at and time.time() - self.started_at > 120:
                self.state = "error"
                self.phase = "测速超时"
                self.phase_code = "timeout"
                self.error = "测速超过 120 秒，已自动中止"
                self.progress = 0.0
            return {
                "state": self.state,
                "phase": self.phase,
                "phase_code": self.phase_code,
                "progress": round(self.progress, 3),
                "live_down": round(self.live_down, 1),
                "live_up": round(self.live_up, 1),
                "result": self.result,
                "error": self.error,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "config": {
                    "duration": float(self.cfg.get("speedtest.duration", 8) or 8),
                    "upload_size_mb": float(self.cfg.get("speedtest.upload_size_mb", 10) or 10),
                    "upload_enabled": bool(
                        self.cfg.get("speedtest.upload_url") or self.cfg.get("speedtest.upload_urls")
                    ),
                },
                "history": self.db.speedtest_history(20),
            }

    # -------------------------------------------------------------- 启动
    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.state == "running":
                return False, "正在测速中，请稍候"
            self.state = "running"
            self.phase = "检测测试服务器"
            self.phase_code = "probe_servers"
            self.progress = 0.01
            self.live_down = 0.0
            self.live_up = 0.0
            self.result = None
            self.error = None
            self.started_at = int(time.time())
            self.updated_at = int(time.time())
        self._thread = threading.Thread(target=self._run, name="routercat-speedtest", daemon=True)
        self._thread.start()
        return True, "已开始测速"

    # -------------------------------------------------------------- 执行
    def _run(self) -> None:
        try:
            self._do_run()
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", phase="测速失败", error=str(exc)[:200], progress=0.0)
        finally:
            if self.state == "running":
                self._set(state="error", phase="测速异常结束", error="未知错误")

    def _do_run(self) -> None:
        duration = max(3.0, min(20.0, float(self.cfg.get("speedtest.duration", 8) or 8)))
        urls = [u for u in (self.cfg.get("speedtest.download_urls") or []) if u]
        if not urls:
            self._set(state="error", phase="未配置下载测速地址", error="请在设置里填写下载测速地址")
            return

        # 1) 选服务器
        self._set(phase="检测测试服务器", progress=0.03)
        best_url, best_speed = None, -1.0
        for url in urls[:3]:
            speed = probe_server(url)
            if speed > best_speed:
                best_url, best_speed = url, speed
            if speed > 300:  # 千兆以上，没必要再比
                break
            if self.state != "running":
                return
        if not best_url or best_speed <= 0:
            self._set(state="error", phase="测试服务器不可达", error="所有下载测速地址都无法连接，请检查网络或更换地址")
            return
        server_name = urlsplit(best_url).hostname or best_url

        # 2) 先挑一个可用的上传目标（此时链路空闲，探活更准）
        candidates = []
        primary = self.cfg.get("speedtest.upload_url")
        if primary:
            candidates.append(primary)
        for u in self.cfg.get("speedtest.upload_urls") or []:
            if u and u not in candidates:
                candidates.append(u)
        upload_endpoint = None
        if candidates:
            self._set(phase="检测上传服务器", progress=0.05)
            if self._upload_ok and self._upload_ok in candidates:
                if upload(self._upload_ok, 65536, 4.0)["ok"]:
                    upload_endpoint = self._upload_ok
                else:
                    self._upload_ok = None
            if not upload_endpoint:
                outcomes: dict[str, bool] = {}

                def _probe(u: str) -> None:
                    outcomes[u] = upload(u, 65536, 3.5)["ok"]

                for u in candidates[:3]:
                    threading.Thread(target=_probe, args=(u,), daemon=True).start()
                deadline = time.time() + 7
                while time.time() < deadline and not any(outcomes.values()):
                    time.sleep(0.1)
                for u in candidates[:3]:
                    if outcomes.get(u):
                        upload_endpoint = u
                        break
            if upload_endpoint:
                self._upload_ok = upload_endpoint

        # 3) 空闲延迟基线
        idle = (self.monitor.window_stats("gateway", 30) or {}).get("avg")
        wan_target = self.cfg.get("speedtest.latency_target") or (
            (self.cfg.get("probe.wan_targets") or ["223.5.5.5"])[0]
        )
        wan_idle = (self.monitor.window_stats(wan_target, 60) or {}).get("avg")

        # 4) 下载 + 并行采样负载延迟
        self._set(phase="测试下行速率", progress=0.08)
        stop = threading.Event()
        samples: list[float] = []
        lat_thread = threading.Thread(target=self._latency_worker, args=(stop, samples, wan_target), daemon=True)
        lat_thread.start()

        down = download(best_url, duration, on_progress=self._on_down)
        self._set(live_down=down["mbps"], progress=0.62)

        # 5) 上传（目标已在前面挑好）
        up = {"mbps": 0.0, "ok": False, "error": "上传测速服务器不可达，已跳过"}
        if upload_endpoint:
            if time.time() - self.started_at > 90:
                self._set(state="error", phase="测速超时", error="测速步骤超时，已中止")
                return
            self._set(phase="测试上行速率", progress=0.68)
            size = int(float(self.cfg.get("speedtest.upload_size_mb", 10) or 10) * 1024 * 1024)
            up = upload(upload_endpoint, size, min(duration, 10.0), on_progress=self._on_up)
            if not up.get("ok"):
                self._upload_ok = None
            self._set(live_up=up["mbps"], progress=0.94)

        stop.set()
        lat_thread.join(timeout=3)

        # 6) 汇总
        self._set(phase="汇总结果", progress=0.97)
        loaded = round(sum(samples) / len(samples), 1) if samples else None
        result = {
            "ts": int(time.time()),
            "down_mbps": down["mbps"],
            "down_avg_mbps": down.get("avg_mbps"),
            "up_mbps": up["mbps"] if up.get("ok") else None,
            "server": server_name,
            "server_url": best_url,
            "bytes_down": down["bytes"],
            "bytes_up": up.get("bytes", 0),
            "duration": down.get("seconds"),
            "latency_idle": round(wan_idle, 1) if wan_idle else (round(idle, 1) if idle else None),
            "latency_loaded": loaded,
            "latency_target": wan_target,
            "upload_error": None if up.get("ok") else up.get("error"),
            "download_error": down.get("error"),
        }
        try:
            self.db.add_speedtest(result)
        except Exception:  # noqa: BLE001
            pass
        self._set(state="done", phase="测速完成", progress=1.0, result=result, live_up=result["up_mbps"] or 0.0)

    def _on_down(self, total: int, inst: float, frac: float) -> None:
        self._set(live_down=round(inst, 1), progress=0.08 + min(0.54, frac * 0.54))

    def _on_up(self, total: int, inst: float, frac: float) -> None:
        self._set(live_up=round(inst, 1), progress=0.66 + min(0.28, frac * 0.28))

    def _latency_worker(self, stop: threading.Event, samples: list, target: str) -> None:
        while not stop.is_set():
            res = ping(target, timeout=1)
            if res["ok"] and res["rtt"] is not None:
                samples.append(res["rtt"])
            stop.wait(0.5)
