"""RouterCat - 本机系统指标采集（全部读取 /proc、/sys，无第三方依赖）。"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time

from . import hostinfo

SECTOR = 512
VIRTUAL_FS = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "ramfs", "proc", "sysfs", "cgroup",
    "cgroup2", "devpts", "fusectl", "mqueue", "debugfs", "tracefs", "securityfs",
    "pstore", "bpf", "autofs", "configfs", "hugetlbfs", "nsfs", "efivarfs",
}


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _run(cmd, timeout=4.0) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception:
        return ""


def _run_json(cmd, timeout=4.0):
    out = _run(cmd, timeout)
    try:
        return json.loads(out)
    except Exception:
        return None


def human_platform() -> dict:
    info = {"hostname": socket.gethostname(), "kernel": "", "os": "", "arch": ""}
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            info["os"] = line.split("=", 1)[1].strip().strip('"')
    info["kernel"] = _read("/proc/sys/kernel/osrelease").strip()
    info["arch"] = os.uname().machine
    cpu_model, cores = "", 0
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name") and not cpu_model:
            cpu_model = line.split(":", 1)[1].strip()
        elif line.startswith("processor"):
            cores += 1
    info["cpu_model"] = cpu_model or "Unknown CPU"
    info["cores"] = cores or os.cpu_count() or 1
    return info


class SystemCollector:
    """有状态的采集器：需要上一次的 CPU / 网卡 / 磁盘计数来算速率。"""

    def __init__(self) -> None:
        self._prev_cpu = self._cpu_times()
        self._prev_net = self._net_bytes()
        self._prev_disk = self._disk_sectors()
        self._prev_ts = time.time()
        self._procs = []
        self._procs_ts = 0.0
        self._host = human_platform()
        # 本机身份：发行版 / 桌面 / 机型 / 虚拟化，进程生命周期内不变，只识别一次
        self._system = hostinfo.detect_host()
        self._iface_cache = {}
        self._iface_ts = 0.0

    # -------------------------------------------------------------- CPU
    @staticmethod
    def _cpu_times():
        total, per_core = [], []
        for line in _read("/proc/stat").splitlines():
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            name = parts[0]
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            tot = sum(vals)
            if name == "cpu":
                total = [tot, idle]
            else:
                per_core.append([tot, idle])
        return {"total": total, "cores": per_core}

    def _cpu_pct(self, prev, cur):
        dt = cur[0] - prev[0]
        di = cur[1] - prev[1]
        if dt <= 0:
            return 0.0
        return max(0.0, min(100.0, (1 - di / dt) * 100))

    # -------------------------------------------------------------- 内存
    @staticmethod
    def _memory() -> dict:
        kv = {}
        for line in _read("/proc/meminfo").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = int(v.strip().split()[0]) * 1024
        total = kv.get("MemTotal", 0)
        avail = kv.get("MemAvailable", kv.get("MemFree", 0))
        used = max(0, total - avail)
        st, sf = kv.get("SwapTotal", 0), kv.get("SwapFree", 0)
        s_used = max(0, st - sf)
        return {
            "total": total,
            "used": used,
            "free": avail,
            "pct": round(used / total * 100, 1) if total else 0.0,
            "swap_total": st,
            "swap_used": s_used,
            "swap_pct": round(s_used / st * 100, 1) if st else 0.0,
            "cached": kv.get("Cached", 0),
        }

    # -------------------------------------------------------------- 网卡
    @staticmethod
    def _net_bytes() -> dict:
        out = {}
        for line in _read("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            f = rest.split()
            if len(f) < 16:
                continue
            out[name.strip()] = {
                "rx": int(f[0]), "tx": int(f[8]),
                "rx_err": int(f[2]), "tx_err": int(f[10]),
                "rx_drop": int(f[3]), "tx_drop": int(f[11]),
            }
        return out

    def _iface_meta(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._iface_ts and now - self._iface_ts < 10:
            return self._iface_cache
        meta = {}
        data = _run_json(["ip", "-j", "-4", "addr", "show"])
        if data:
            for item in data:
                name = item.get("ifname")
                addrs = item.get("addr_info") or []
                ip = addrs[0].get("local") if addrs else None
                prefix = addrs[0].get("prefixlen") if addrs else None
                meta[name] = {"ip": ip, "prefix": prefix, "mac": item.get("address")}
        for name in os.listdir("/sys/class/net"):
            m = meta.setdefault(name, {})
            m["operstate"] = _read(f"/sys/class/net/{name}/operstate").strip() or "unknown"
            m["mtu"] = _read(f"/sys/class/net/{name}/mtu").strip()
            sp = _read(f"/sys/class/net/{name}/speed").strip()
            m["speed"] = int(sp) if sp.isdigit() and int(sp) > 0 else None
            m["wireless"] = os.path.isdir(f"/sys/class/net/{name}/wireless")
        self._iface_cache = meta
        self._iface_ts = now
        return meta

    # -------------------------------------------------------------- 磁盘
    @staticmethod
    def _disk_sectors() -> dict:
        """返回 {盘名: {"read": 字节, "write": 字节}}，只统计整块物理盘。"""
        out = {}
        for line in _read("/proc/diskstats").splitlines():
            f = line.split()
            if len(f) < 14:
                continue
            name = f[2]
            if not os.path.exists(f"/sys/block/{name}"):
                continue  # 只看整块盘，跳过分区
            try:
                out[name] = {"read": int(f[5]) * SECTOR, "write": int(f[9]) * SECTOR}
            except ValueError:
                continue
        return out

    @staticmethod
    def _disk_usage() -> list:
        """按文件系统去重（消除 bind mount / overlay 造成的重复项）。"""
        best = {}
        for line in _read("/proc/mounts").splitlines():
            f = line.split()
            if len(f) < 3:
                continue
            dev, mnt, fstype = f[0], f[1], f[2]
            if fstype in VIRTUAL_FS or not dev.startswith("/dev/"):
                continue
            if mnt.startswith(("/snap", "/var/lib/docker", "/var/lib/snapd")):
                continue
            if not os.path.isdir(mnt):
                continue
            try:
                st = os.statvfs(mnt)
                st_dev = os.stat(mnt).st_dev
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total <= 0:
                continue
            used = total - free
            entry = {
                "mount": mnt,
                "device": dev,
                "fstype": fstype,
                "total": total,
                "used": used,
                "free": free,
                "pct": round(used / total * 100, 1),
            }
            prev = best.get(st_dev)
            if prev is None or len(mnt) < len(prev["mount"]):
                best[st_dev] = entry
        out = sorted(best.values(), key=lambda d: (-d["total"], d["mount"]))
        return out[:8]

    # -------------------------------------------------------------- 温度
    @staticmethod
    def _temperatures() -> list:
        out = []
        for hw in sorted(os.listdir("/sys/class/hwmon")) if os.path.isdir("/sys/class/hwmon") else []:
            base = f"/sys/class/hwmon/{hw}"
            name = _read(f"{base}/name").strip() or hw
            for fn in sorted(os.listdir(base)):
                if not (fn.startswith("temp") and fn.endswith("_input")):
                    continue
                raw = _read(f"{base}/{fn}").strip()
                if not raw.isdigit():
                    continue
                val = int(raw) / 1000.0
                if not (-20 < val < 150):
                    continue
                label = _read(f"{base}/{fn.replace('_input', '_label')}").strip()
                out.append({"name": f"{name} {label}".strip(), "value": round(val, 1)})
        if not out:
            for z in sorted(os.listdir("/sys/class/thermal")) if os.path.isdir("/sys/class/thermal") else []:
                if not z.startswith("thermal_zone"):
                    continue
                raw = _read(f"/sys/class/thermal/{z}/temp").strip()
                if not raw.isdigit():
                    continue
                val = int(raw) / 1000.0
                if -20 < val < 150:
                    out.append({"name": _read(f"/sys/class/thermal/{z}/type").strip() or z, "value": round(val, 1)})
        return out[:6]

    # -------------------------------------------------------------- 进程
    def _processes(self) -> list:
        now = time.time()
        if now - self._procs_ts < 4:
            return self._procs
        if not shutil.which("ps"):
            return self._procs
        out = _run(
            ["ps", "-eo", "pid,comm,%cpu,%mem,rss", "--sort=-%cpu", "--no-headers"], timeout=3
        )
        rows = []
        for line in out.splitlines()[:40]:
            f = line.split(None, 4)
            if len(f) < 5:
                continue
            try:
                rows.append(
                    {
                        "pid": int(f[0]),
                        "name": f[1][:32],
                        "cpu": float(f[2]),
                        "mem": float(f[3]),
                        "rss": int(f[4]) * 1024,
                    }
                )
            except ValueError:
                continue
        self._procs = rows[:10]
        self._procs_ts = now
        return self._procs

    # -------------------------------------------------------------- 汇总
    def snapshot(self) -> dict:
        now = time.time()
        cpu_cur = self._cpu_times()
        cpu_pct = self._cpu_pct(self._prev_cpu["total"], cpu_cur["total"])
        per_core = [
            round(self._cpu_pct(p, c), 1)
            for p, c in zip(self._prev_cpu["cores"], cpu_cur["cores"])
        ]
        self._prev_cpu = cpu_cur

        net_cur = self._net_bytes()
        dt = max(0.2, now - self._prev_ts)
        meta = self._iface_meta()
        interfaces = []
        for name, cur in net_cur.items():
            prev = self._prev_net.get(name, cur)
            rx_bps = max(0.0, (cur["rx"] - prev["rx"]) / dt)
            tx_bps = max(0.0, (cur["tx"] - prev["tx"]) / dt)
            m = meta.get(name, {})
            interfaces.append(
                {
                    "name": name,
                    "ip": m.get("ip"),
                    "prefix": m.get("prefix"),
                    "mac": m.get("mac"),
                    "state": m.get("operstate", "unknown"),
                    "wireless": bool(m.get("wireless")),
                    "speed": m.get("speed"),
                    "mtu": m.get("mtu"),
                    "rx_bps": rx_bps,
                    "tx_bps": tx_bps,
                    "rx_total": cur["rx"],
                    "tx_total": cur["tx"],
                    "rx_err": cur["rx_err"],
                    "tx_err": cur["tx_err"],
                    "rx_drop": cur["rx_drop"],
                    "tx_drop": cur["tx_drop"],
                }
            )
        interfaces.sort(key=lambda i: (i["name"] == "lo", i["name"]))
        self._prev_net = net_cur

        disk_cur = self._disk_sectors()
        rd_now = sum(d["read"] for d in disk_cur.values())
        wr_now = sum(d["write"] for d in disk_cur.values())
        rd_prev = sum(d["read"] for d in self._prev_disk.values())
        wr_prev = sum(d["write"] for d in self._prev_disk.values())
        read_bps = max(0.0, (rd_now - rd_prev) / dt)
        write_bps = max(0.0, (wr_now - wr_prev) / dt)
        self._prev_disk = disk_cur
        self._prev_ts = now

        mem = self._memory()
        load = _read("/proc/loadavg").split()
        up_s = float(_read("/proc/uptime").split()[0] or 0)

        return {
            "ts": int(now),
            "cpu": round(cpu_pct, 1),
            "per_core": per_core,
            "mem": mem,
            "load": [float(x) for x in load[:3]] if len(load) >= 3 else [0, 0, 0],
            "uptime": up_s,
            "boot_ts": int(now - up_s),
            "interfaces": interfaces,
            "disk_io": {"read_bps": read_bps, "write_bps": write_bps, "disks": len(disk_cur)},
            "disks": self._disk_usage(),
            "temps": self._temperatures(),
            "processes": self._processes(),
            "platform": self._host,
            "system": self._system,
        }
