"""RouterCat - 网络探测：ICMP / DNS / TCP / HTTP / ARP 邻居发现。"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RTT_RE = re.compile(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)")
LOSS_RE = re.compile(r"([\d.]+)% packet loss")


def _run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or "", p.stderr or "", p.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 124
    except Exception as exc:  # noqa: BLE001
        return "", str(exc), 1


def ping(host: str, timeout: int = 1, count: int = 1) -> dict:
    """单次 ICMP 探测，返回 ok / rtt / jitter。"""
    t0 = time.perf_counter()
    out, err, rc = _run(
        ["ping", "-n", "-c", str(count), "-W", str(max(1, int(timeout))), host],
        timeout=count * (timeout + 1.5) + 2,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    text = out + err
    m = RTT_RE.search(text)
    lm = LOSS_RE.search(text)
    loss = float(lm.group(1)) if lm else (0.0 if rc == 0 else 100.0)
    if m:
        return {
            "ok": True,
            "rtt": round(float(m.group(2)), 2),
            "jitter": round(float(m.group(4)), 2),
            "min": round(float(m.group(1)), 2),
            "max": round(float(m.group(3)), 2),
            "loss": loss,
            "elapsed": round(elapsed, 1),
            "error": None,
        }
    return {
        "ok": False,
        "rtt": None,
        "jitter": None,
        "min": None,
        "max": None,
        "loss": loss if loss else 100.0,
        "elapsed": round(elapsed, 1),
        "error": (err.strip().splitlines()[-1] if err.strip() else "无响应"),
    }


def detect_gateway() -> dict:
    """从默认路由里取网关地址、出口网卡和本机地址。"""
    info = {"ip": None, "iface": None, "local_ip": None, "prefix": None, "mac": None}
    out, _, _ = _run(["ip", "-j", "route", "show", "default"], 4)
    try:
        routes = json.loads(out)
    except Exception:
        routes = []
    for r in routes or []:
        if r.get("gateway"):
            info["ip"] = r["gateway"]
            info["iface"] = r.get("dev")
            info["local_ip"] = r.get("prefsrc")
            break
    if not info["ip"]:
        text, _, _ = _run(["ip", "route", "show", "default"], 4)
        m = re.search(r"default via (\S+) dev (\S+)", text)
        if m:
            info["ip"], info["iface"] = m.group(1), m.group(2)
    # 本机地址 + 前缀长度
    data, _, _ = _run(["ip", "-j", "-4", "addr", "show"], 4)
    try:
        addrs = json.loads(data)
    except Exception:
        addrs = []
    for item in addrs or []:
        if item.get("ifname") == info["iface"]:
            a = (item.get("addr_info") or [{}])[0]
            info["local_ip"] = a.get("local") or info["local_ip"]
            info["prefix"] = a.get("prefixlen")
    if info["ip"]:
        for entry in arp_table():
            if entry["ip"] == info["ip"]:
                info["mac"] = entry["mac"]
                break
    return info


def dns_check(host: str, timeout: float = 3.0) -> dict:
    t0 = time.perf_counter()
    try:
        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(host, None)
        ips = sorted({i[4][0] for i in infos})
        return {
            "ok": True,
            "code": "dns_ok",
            "latency": round((time.perf_counter() - t0) * 1000, 1),
            "detail": ", ".join(ips[:3]),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "dns_fail", "latency": None, "detail": str(exc)[:120]}


def tcp_check(host: str, port: int, timeout: float = 3.0) -> dict:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "ok": True,
                "code": "tcp_ok",
                "short": f"{host}:{port}",
                "latency": round((time.perf_counter() - t0) * 1000, 1),
                "detail": "",
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "tcp_fail", "latency": None, "detail": str(exc)[:120]}


def http_check(url: str, timeout: float = 6.0) -> dict:
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers={"User-Agent": "RouterCat/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(2048)
            latency = round((time.perf_counter() - t0) * 1000, 1)
            ok = 200 <= resp.status < 400
            return {"ok": ok, "code": "http_ok" if ok else "http_fail",
                    "latency": latency, "detail": f"HTTP {resp.status}"}
    except urllib.error.HTTPError as exc:
        return {
            "ok": exc.code < 500,
            "code": "http_ok" if exc.code < 500 else "http_fail",
            "latency": round((time.perf_counter() - t0) * 1000, 1),
            "detail": f"HTTP {exc.code}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "http_fail", "latency": None, "detail": str(exc)[:120]}


def arp_table() -> list:
    """读取 /proc/net/arp（flags 0x2 表示条目有效）。"""
    out = []
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) < 6:
            continue
        try:
            valid = int(f[2], 16) & 0x2
        except ValueError:
            valid = 0
        if not valid:
            continue
        mac = f[3].lower()
        if mac == "00:00:00:00:00:00":
            continue
        out.append({"ip": f[0], "mac": mac, "iface": f[5]})
    return out


def resolve_hostname(ip: str, timeout: float = 0.8) -> str | None:
    try:
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            return socket.gethostbyaddr(ip)[0]
        finally:
            socket.setdefaulttimeout(old)
    except Exception:  # noqa: BLE001
        return None


def subnet_hosts(local_ip: str, prefix: int, limit: int = 254) -> list:
    try:
        net = ipaddress.ip_network(f"{local_ip}/{prefix}", strict=False)
    except ValueError:
        return []
    hosts = [str(h) for h in list(net.hosts())[:limit]]
    return hosts


def scan_subnet(local_ip: str, prefix: int, workers: int = 64, timeout: int = 1) -> int:
    """并发 ping 整个网段，刷新内核 ARP 表后再由调用方读取邻居。"""
    hosts = subnet_hosts(local_ip, prefix)
    if not hosts:
        return 0

    def one(ip: str) -> bool:
        try:
            subprocess.run(
                ["ping", "-n", "-c", "1", "-W", str(timeout), ip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout + 1.5,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, hosts))
    return len(hosts)
