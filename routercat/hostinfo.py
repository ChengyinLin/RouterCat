"""RouterCat - 本机系统自动识别。

读 /etc/os-release、DMI、systemd-detect-virt 等，拼出这台机器的"身份"：
发行版、版本、内核、架构、桌面环境、整机型号、是物理机 / 虚拟机 / 容器。
全部走标准库和系统文件，识别不出来就留空，不影响监控。
"""
from __future__ import annotations

import os
import platform
import re
import socket
import subprocess

DMI_DIR = "/sys/class/dmi/id"

# SMBIOS chassis_type 取值
CHASSIS = {
    3: "desktop", 4: "desktop", 5: "desktop", 6: "desktop", 7: "desktop",
    8: "laptop", 9: "laptop", 10: "laptop", 11: "handheld", 12: "laptop",
    13: "aio", 14: "laptop", 15: "handheld", 16: "desktop",
    17: "server", 18: "server", 19: "server", 20: "server", 21: "server",
    22: "server", 23: "server", 24: "server", 25: "server", 26: "server",
    27: "server", 28: "server", 29: "tablet", 30: "tablet", 31: "phone", 32: "tablet",
}

# 这些发行版名太长，界面上用短名
SHORT_NAMES = {
    "ubuntu": "Ubuntu",
    "debian": "Debian",
    "fedora": "Fedora",
    "arch": "Arch Linux",
    "manjaro": "Manjaro",
    "centos": "CentOS",
    "rhel": "RHEL",
    "rocky": "Rocky Linux",
    "alma": "AlmaLinux",
    "opensuse-leap": "openSUSE",
    "opensuse-tumbleweed": "openSUSE",
    "linuxmint": "Linux Mint",
    "kali": "Kali Linux",
    "raspbian": "Raspbian",
    "elementary": "elementary OS",
    "zorin": "Zorin OS",
    "deepin": "deepin",
    "kylin": "Kylin",
    "uos": "UOS",
    "alpine": "Alpine",
    "nixos": "NixOS",
    "gentoo": "Gentoo",
    "slackware": "Slackware",
    "amzn": "Amazon Linux",
    "ol": "Oracle Linux",
    "pop": "Pop!_OS",
}


def _read(path: str, limit: int = 65536) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().strip("\x00")
    if not text or text.lower() in {
        "to be filled by o.e.m.", "none", "default string", "not specified",
        "not available", "system product name", "oem", "unknown", "n/a", "-",
    }:
        return None
    return text


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def os_release() -> dict:
    text = _read("/etc/os-release") or _read("/usr/lib/os-release")
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("\"'")
    return data


def virtualization() -> tuple[str, str]:
    """返回 (kind, name)：kind 为 hardware / vm / container。"""
    if os.path.exists("/.dockerenv"):
        return "container", "docker"
    if os.path.exists("/run/.containerenv"):
        return "container", "podman"
    cgroup = _read("/proc/1/cgroup", 8192)
    for needle, name in (("kubepods", "kubernetes"), ("docker", "docker"),
                         ("lxc", "lxc"), ("podman", "podman")):
        if needle in cgroup:
            return "container", name
    if os.path.exists("/proc/vz") and not os.path.exists("/proc/bc"):
        return "container", "openvz"
    virt = _run(["systemd-detect-virt", "--vm"])
    if virt and virt != "none":
        return "vm", virt
    return "hardware", ""


def desktop_env() -> tuple[str | None, str | None]:
    desktop = (
        os.environ.get("XDG_CURRENT_DESKTOP")
        or os.environ.get("DESKTOP_SESSION")
        or None
    )
    if desktop:
        # "ubuntu:GNOME" → GNOME
        desktop = desktop.split(":")[-1].strip()
        mapping = {
            "gnome": "GNOME", "ubuntu": "GNOME", "kde": "KDE Plasma",
            "plasma": "KDE Plasma", "xfce": "XFCE", "x-cinnamon": "Cinnamon",
            "cinnamon": "Cinnamon", "mate": "MATE", "lxqt": "LXQt",
            "lxde": "LXDE", "budgie": "Budgie", "pantheon": "Pantheon",
            "deepin": "DDE", "sway": "Sway", "i3": "i3", "hyprland": "Hyprland",
        }
        desktop = mapping.get(desktop.lower(), desktop)
    session = os.environ.get("XDG_SESSION_TYPE") or None
    return desktop, session


def machine() -> dict:
    vendor = _clean(_read(f"{DMI_DIR}/sys_vendor"))
    product = _clean(_read(f"{DMI_DIR}/product_name"))
    version = _clean(_read(f"{DMI_DIR}/product_version"))
    board = _clean(_read(f"{DMI_DIR}/board_name"))
    chassis_raw = _read(f"{DMI_DIR}/chassis_type").strip()
    chassis = None
    try:
        chassis = CHASSIS.get(int(chassis_raw.split()[0]))
    except (ValueError, IndexError):
        chassis = None
    # 有的厂商 product_name 是内部料号（20N2A00TCD），product_version 才是商品名（ThinkPad T490）
    label = version if version and re.search(r"[A-Za-z]{3,}", version) else product
    brand = vendor.title() if vendor and vendor.isupper() else vendor
    model = " ".join(x for x in (brand, label) if x) or None
    return {
        "vendor": vendor,
        "product": product,
        "version": version,
        "board": board,
        "form": chassis,
        "model": model,
    }


def memory_total() -> int:
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) * 1024
            except (ValueError, IndexError):
                return 0
    return 0


def _cpu_model() -> str | None:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name") or line.startswith("Model"):
            value = line.split(":", 1)[-1].strip()
            if value:
                return re.sub(r"\s+", " ", value)
    return None


def detect_host() -> dict:
    """一次性识别本机系统，结果基本不变，进程内缓存即可。"""
    rel = os_release()
    os_id = (rel.get("ID") or "").lower()
    like = (rel.get("ID_LIKE") or "").lower()
    pretty = rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux"

    kind, virt_name = virtualization()
    desktop, session = desktop_env()
    hw = machine()
    uname = platform.uname()

    # 发行版识别：优先 ID，其次 ID_LIKE
    family = os_id
    if family not in SHORT_NAMES and like:
        for token in like.split():
            if token in SHORT_NAMES:
                family = token
                break

    name = SHORT_NAMES.get(os_id) or rel.get("NAME") or "Linux"
    version = rel.get("VERSION_ID") or None
    if not version:
        vm = re.search(r"\d+(?:\.\d+)*", pretty)
        version = vm.group(0) if vm else None
    codename = rel.get("VERSION_CODENAME") or rel.get("UBUNTU_CODENAME") or None

    return {
        "hostname": socket.gethostname(),
        "id": os_id or "linux",
        "family": family or "linux",
        "name": name,
        "pretty": pretty,
        "version": version,
        "codename": codename,
        "kernel": uname.release,
        "kernel_build": uname.version,
        "arch": uname.machine,
        "desktop": desktop,
        "session": session,
        "virt_kind": kind,
        "virt_name": virt_name,
        "machine": hw,
        "cpu_model": _cpu_model(),
        "cores": os.cpu_count() or 1,
        "mem_total": memory_total(),
        "logo": os_id or family or "linux",
    }
