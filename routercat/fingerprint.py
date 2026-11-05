"""RouterCat - 路由器型号自动识别。

识别分四层，从便宜到贵，任何一层拿到结果就能收工：

1. **MAC OUI** —— 网关 MAC 前三字节查厂商。优先读系统 IEEE 库
   （/usr/share/ieee-data/oui.csv，装了 ieee-data 或 nmap 就有），
   没有就退回内置的常用路由厂商表，再不行可选联网补查。
2. **厂商私有接口** —— 华为 `/api/system/deviceinfo`、小米 `/cgi-bin/luci/...`
   这类免鉴权接口能直接给出精确型号，是最可靠的一层。
3. **UPnP / SSDP** —— M-SEARCH 拿 LOCATION，读设备描述 XML 里的 modelName。
   大部分消费级路由开着 UPnP 时最准，但华为默认关闭。
4. **登录页指纹** —— 抓首页 title / 关键字认品牌，再从文本里抠型号，兜底用。

结果带 `sources`（识别来源）和 `confidence`，前端会把来源显示出来；
认不出来也只影响显示名字，不影响监控。
"""
from __future__ import annotations

import csv
import gzip
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("routercat.fingerprint")

UA = "Mozilla/5.0 (X11; Linux x86_64) RouterCat/1.2"
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "upnp:rootdevice",
    "ssdp:all",
)
# 系统里可能存在的 IEEE OUI 库，按优先级排列
OUI_FILES = (
    "/usr/share/ieee-data/oui.csv",
    "/var/lib/ieee-data/oui.csv",
    "/usr/share/nmap/nmap-mac-prefixes",
    "/usr/share/arp-scan/ieee-oui.txt",
    "/usr/share/misc/oui.txt",
    "/usr/share/hwdata/oui.txt",
)

# ---------------------------------------------------------------- 内置 OUI 兜底
# 从 IEEE 数据库（/usr/share/ieee-data/oui.csv）里筛出常见路由/光猫厂商的前缀，
# 体积只有十几 KB，用于系统没装 ieee-data 时的兜底。
_OUI_BUILTIN = {
    "00000C": "Cisco Systems, Inc",
    "000142": "Cisco Systems, Inc",
    "000143": "Cisco Systems, Inc",
    "000163": "Cisco Systems, Inc",
    "000164": "Cisco Systems, Inc",
    "000196": "Cisco Systems, Inc",
    "000197": "Cisco Systems, Inc",
    "0001C7": "Cisco Systems, Inc",
    "00040E": "AVM GmbH",
    "00055D": "D-LINK SYSTEMS, INC.",
    "000726": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "00095B": "NETGEAR",
    "000AC2": "Wuhan FiberHome Digital Technology Co.,Ltd.",
    "000AEB": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "000C42": "Routerboard.com",
    "000C6E": "ASUSTek COMPUTER INC.",
    "000D88": "D-Link Corporation",
    "000E59": "Sagemcom Broadband SAS",
    "000E5E": "Raisecom Technology CO., LTD",
    "000E8F": "Sercomm Corporation.",
    "000EA6": "ASUSTek COMPUTER INC.",
    "000F3D": "D-Link Corporation",
    "000FB5": "NETGEAR",
    "000FE2": "Hangzhou H3C Technologies Co., Limited",
    "00112F": "ASUSTek COMPUTER INC.",
    "001195": "D-Link Corporation",
    "0011AD": "Shanghai Ruijie Technology",
    "0011D8": "ASUSTek COMPUTER INC.",
    "0012BF": "Arcadyan Corporation",
    "001346": "D-Link Corporation",
    "0013D4": "ASUSTek COMPUTER INC.",
    "00146C": "NETGEAR",
    "001478": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "00150C": "AVM GmbH",
    "001556": "Sagemcom Broadband SAS",
    "00156D": "Ubiquiti Inc",
    "0015E9": "D-Link Corporation",
    "0015EB": "zte corporation",
    "0015F2": "ASUSTek COMPUTER INC.",
    "001731": "ASUSTek COMPUTER INC.",
    "00179A": "D-Link Corporation",
    "00184D": "NETGEAR",
    "001882": "HUAWEI TECHNOLOGIES CO.,LTD",
    "0018F3": "ASUSTek COMPUTER INC.",
    "00194B": "Sagemcom Broadband SAS",
    "00195B": "D-Link Corporation",
    "0019C6": "zte corporation",
    "0019E0": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "001A2A": "Arcadyan Corporation",
    "001A4F": "AVM GmbH",
    "001B2F": "NETGEAR",
    "001BBF": "Sagemcom Broadband SAS",
    "001C4A": "AVM GmbH",
    "001D0F": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "001D19": "Arcadyan Corporation",
    "001E10": "HUAWEI TECHNOLOGIES CO.,LTD",
    "001E2A": "NETGEAR",
    "001E73": "zte corporation",
    "001E74": "Sagemcom Broadband SAS",
    "001F33": "NETGEAR",
    "001F3F": "AVM GmbH",
    "001F95": "Sagemcom Broadband SAS",
    "001FA4": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "002127": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "00223F": "NETGEAR",
    "002293": "zte corporation",
    "0022A1": "Huawei Symantec Technologies Co.,Ltd.",
    "002308": "Arcadyan Corporation",
    "002348": "Sagemcom Broadband SAS",
    "002389": "Hangzhou H3C Technologies Co., Limited",
    "0023CD": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "0024FE": "AVM GmbH",
    "002512": "zte corporation",
    "002568": "HUAWEI TECHNOLOGIES CO.,LTD",
    "002569": "Sagemcom Broadband SAS",
    "002586": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "00259E": "HUAWEI TECHNOLOGIES CO.,LTD",
    "00264D": "Arcadyan Corporation",
    "0026ED": "zte corporation",
    "002719": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "002722": "Ubiquiti Inc",
    "002EC7": "HUAWEI TECHNOLOGIES CO.,LTD",
    "002FD9": "Fiberhome Telecommunication Technologies Co.,LTD",
    "0034FE": "HUAWEI TECHNOLOGIES CO.,LTD",
    "003DE1": "Huawei Device Co., Ltd.",
    "004A77": "zte corporation",
    "004BF3": "SHENZHEN MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "0056F1": "zte corporation",
    "005CC2": "SHENZHEN MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "006762": "Fiberhome Telecommunication Technologies Co.,LTD",
    "00749C": "Ruijie Networks Co.,LTD",
    "007AA4": "FRITZ! Technology GmbH",
    "009EC8": "Xiaomi Communications Co Ltd",
    "00A307": "Honor Device Co., Ltd.",
    "00BE9E": "Fiberhome Telecommunication Technologies Co.,LTD",
    "00BED5": "New H3C Technologies Co., Ltd",
    "00C002": "Sercomm Corporation.",
    "00C30A": "Xiaomi Communications Co Ltd",
    "00DDB6": "New H3C Technologies Co., Ltd",
    "00E0FC": "HUAWEI TECHNOLOGIES CO.,LTD",
    "00EC0A": "Xiaomi Communications Co Ltd",
    "040986": "Arcadyan Corporation",
    "04106B": "Xiaomi Communications Co Ltd",
    "0418D6": "Ubiquiti Inc",
    "0422E7": "Fiberhome Telecommunication Technologies Co.,LTD",
    "0440A9": "New H3C Technologies Co., Ltd",
    "044BA5": "SHENZHEN MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "046761": "Beijing Xiaomi Mobile Software Co., Ltd",
    "047056": "Arcadyan Corporation",
    "047975": "Honor Device Co., Ltd.",
    "047A0B": "Beijing Xiaomi Electronics Co., Ltd.",
    "0495E6": "Tenda Technology Co.,Ltd.Dongguan branch",
    "04A222": "Arcadyan Corporation",
    "04A2F3": "Fiberhome Telecommunication Technologies Co.,LTD",
    "04A959": "New H3C Technologies Co., Ltd",
    "04AE47": "Beijing Xiaomi Mobile Software Co., Ltd",
    "04B167": "Xiaomi Communications Co Ltd",
    "04C1B9": "Fiberhome Telecommunication Technologies Co.,LTD",
    "04D4C4": "ASUSTek COMPUTER INC.",
    "04D7A5": "New H3C Technologies Co., Ltd",
    "04ECBB": "Fiberhome Telecommunication Technologies Co.,LTD",
    "04F41C": "Routerboard.com",
    "083A38": "New H3C Technologies Co., Ltd",
    "0840F3": "Tenda Technology Co.,Ltd.Dongguan branch",
    "085531": "Routerboard.com",
    "0884FB": "Honor Device Co., Ltd.",
    "0896D7": "AVM GmbH",
    "08E021": "Honor Device Co., Ltd.",
    "0C7329": "Sercomm Corporation.",
    "0C8509": "SHEN ZHEN TENDA TECHNOLOGY CO.,LTD",
    "0CB789": "Honor Device Co., Ltd.",
    "0CB983": "Honor Device Co., Ltd.",
    "0CEA14": "Ubiquiti Inc",
    "105072": "Sercomm Corporation.",
    "105F02": "Ruijie Networks Co.,LTD",
    "10634B": "SHENZHEN MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "10823D": "Ruijie Networks Co.,LTD",
    "14144B": "Ruijie Networks Co.,LTD",
    "142E5E": "Sercomm Corporation.",
    "1461A4": "Honor Device Co., Ltd.",
    "14CC20": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "18C501": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "18E829": "Ubiquiti Inc",
    "18FD74": "Routerboard.com",
    "1C0B8B": "Ubiquiti Inc",
    "1C60DE": "MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "1C61B4": "TP-Link Systems Inc",
    "1C6A1B": "Ubiquiti Inc",
    "1C872C": "ASUSTek COMPUTER INC.",
    "1CA532": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "1CC992": "Honor Device Co., Ltd.",
    "201F54": "Raisecom Technology CO., LTD",
    "245A4C": "Ubiquiti Inc",
    "24698E": "SHENZHEN MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "28D0F5": "Ruijie Networks Co.,LTD",
    "2C15E1": "Phicomm (Shanghai) Co., Ltd.",
    "2C56DC": "ASUSTek COMPUTER INC.",
    "2CAB25": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "2CB21A": "Phicomm (Shanghai) Co., Ltd.",
    "2CB6C8": "Raisecom Technology CO., LTD",
    "2CC81B": "Routerboard.com",
    "2CDFE6": "Raisecom Technology CO., LTD",
    "300D9E": "Ruijie Networks Co.,LTD",
    "30DE4B": "TP-Link Systems Inc",
    "30DF8D": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "34CE00": "XIAOMI Electronics,CO.,LTD",
    "3802DE": "Sercomm Corporation.",
    "38327A": "Routerboard.com",
    "386B1C": "SHENZHEN MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "38E595": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "3C62F0": "Sercomm Corporation.",
    "3C9872": "Sercomm Corporation.",
    "44F971": "SHENZHEN MERCURY COMMUNICATION TECHNOLOGIES CO.,LTD.",
    "4881D4": "Ruijie Networks Co.,LTD",
    "488F5A": "Routerboard.com",
    "48A98A": "Routerboard.com",
    "48AD08": "HUAWEI TECHNOLOGIES CO.,LTD",
    "4C09B4": "zte corporation",
    "4C5E0C": "Routerboard.com",
    "4CB911": "Raisecom Technology CO., LTD",
    "500FF5": "Tenda Technology Co.,Ltd.Dongguan branch",
    "502B73": "Tenda Technology Co.,Ltd.Dongguan branch",
    "50C7BF": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "50DA9E": "SHEN ZHEN TENDA TECHNOLOGY CO.,LTD",
    "50DB3F": "SHENZHEN GONGJIN ELECTRONICS CO.,LT",
    "5476B2": "Raisecom Technology CO., LTD",
    "58D9D5": "Tenda Technology Co.,Ltd.Dongguan branch",
    "60A4B7": "TP-Link Systems Inc",
    "640980": "Xiaomi Communications Co Ltd",
    "64F81C": "Huawei Technologies Co., Ltd.",
    "68DB54": "Phicomm (Shanghai) Co., Ltd.",
    "7090B7": "Huawei Device Co., Ltd.",
    "70F96D": "Hangzhou H3C Technologies Co., Limited",
    "747D24": "Phicomm (Shanghai) Co., Ltd.",
    "74D02B": "ASUSTek COMPUTER INC.",
    "7811DC": "XIAOMI Electronics,CO.,LTD",
    "7864C0": "Apple, Inc.",
    "7891E9": "Raisecom Technology CO., LTD",
    "809FE4": "SHEN ZHEN TENDA TECHNOLOGY CO.,LTD",
    "8468C8": "TOTOLINK TECHNOLOGY INT‘L LIMITED",
    "8C53C3": "Beijing Xiaomi Mobile Software Co., Ltd",
    "980074": "Raisecom Technology CO., LTD",
    "98BB99": "Phicomm (Sichuan) Co.,Ltd.",
    "98DAC4": "TP-LINK TECHNOLOGIES CO.,LTD.",
    "9C99A0": "Xiaomi Communications Co Ltd",
    "B0F963": "Hangzhou H3C Technologies Co., Limited",
    "CC81DA": "Phicomm (Shanghai) Co., Ltd.",
    "D8C8E9": "Phicomm (Shanghai) Co., Ltd.",
    "F0B429": "Xiaomi Communications Co Ltd",
    "FC7C02": "Phicomm (Shanghai) Co., Ltd.",
}

# 组织名 / 页面关键字 → 品牌
BRANDS = (
    (r"huawei|华为", "华为", "HUAWEI"),
    (r"honor|荣耀", "荣耀", "HONOR"),
    (r"xiaomi|miwifi|redmi|小米", "小米", "Xiaomi"),
    (r"tp-?link|tplogin|普联", "普联", "TP-Link"),
    (r"mercury|水星", "水星", "Mercury"),
    (r"new h3c|\bh3c\b", "新华三", "H3C"),
    (r"\bzte\b|中兴", "中兴", "ZTE"),
    (r"asustek|\basus\b|华硕|\brt-ax|\brt-ac", "华硕", "ASUS"),
    (r"tenda|腾达", "腾达", "Tenda"),
    (r"netgear|网件", "网件", "NETGEAR"),
    (r"d-link|\bdlink\b", "友讯", "D-Link"),
    (r"ruijie|锐捷", "锐捷", "Ruijie"),
    (r"ubiquiti|\bunifi\b", "优比快", "Ubiquiti"),
    (r"mikrotik|routerboard|routeros", "MikroTik", "MikroTik"),
    (r"totolink", "TOTOLINK", "TOTOLINK"),
    (r"phicomm|斐讯", "斐讯", "PHICOMM"),
    (r"fiberhome|烽火", "烽火", "FiberHome"),
    (r"raisecom|瑞斯康达", "瑞斯康达", "Raisecom"),
    (r"sagemcom", "Sagemcom", "Sagemcom"),
    (r"arcadyan", "Arcadyan", "Arcadyan"),
    (r"sercomm", "Sercomm", "Sercomm"),
    (r"avm gmbh|fritz", "AVM", "AVM"),
    (r"technicolor", "Technicolor", "Technicolor"),
    (r"gongjin", "共进电子", "Gongjin"),
    (r"openwrt|\bluci\b", "OpenWrt", "OpenWrt"),
    (r"\bcisco\b", "思科", "Cisco"),
)

# 型号里常见的噪声词，抠型号时要排掉
MODEL_NOISE = {
    "HTTP", "HTTPS", "HTML", "WIFI", "WLAN", "LAN", "WAN", "SSID", "IPV4", "IPV6",
    "USB", "WPS", "DNS", "DHCP", "PPPOE", "WEB", "APP", "LOGIN", "ADMIN", "ROUTER",
    "GATEWAY", "HOME", "INDEX", "ERROR", "PRO", "PLUS", "MAX", "MIN", "NEW", "OK",
}

STATE_RE = re.compile(r"^[\s\-–—_/|:：·•>]+|[\s\-–—_/|:：·•>]+$")


# ---------------------------------------------------------------- 小工具
def _text(body: bytes) -> str:
    """尽量按中文网页的编码解出来。"""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "replace")


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "N/A", "n/a", "unknown", "未知", "0"}:
        return None
    return text


def norm_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    hexonly = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(hexonly) < 12:
        return None
    return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2)).lower()


def oui_prefix(mac: str | None) -> str | None:
    if not mac:
        return None
    hexonly = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    return hexonly[:6] if len(hexonly) >= 6 else None


def brand_of(text: str | None) -> tuple[str | None, str | None]:
    """从任意文本（组织名、页面内容）里认出品牌，返回 (中文名, 英文名)。"""
    if not text:
        return None, None
    low = text.lower()
    for pattern, zh, en in BRANDS:
        if re.search(pattern, low):
            return zh, en
    return None, None


# ---------------------------------------------------------------- OUI
_oui_lock = threading.Lock()
_oui_map: dict[str, str] | None = None
_oui_online_cache: dict[str, str | None] = {}


def _read_oui_file(path: str) -> dict[str, str]:
    table: dict[str, str] = {}
    try:
        if path.endswith(".csv"):
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                for row in csv.reader(f):
                    if len(row) < 3 or not re.fullmatch(r"[0-9A-Fa-f]{6}", row[1].strip()):
                        continue
                    table[row[1].strip().upper()] = row[2].strip()
        elif "nmap" in path:
            # 00000C		Cisco Systems, Inc
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split(None, 1)
                    if len(parts) == 2 and re.fullmatch(r"[0-9A-Fa-f]{6}", parts[0]):
                        table[parts[0].upper()] = parts[1].strip()
        else:
            # 00-00-0C   (hex)		Cisco Systems, Inc
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})\s+\(hex\)\s+(.+)", line)
                    if m:
                        table[(m.group(1) + m.group(2) + m.group(3)).upper()] = m.group(4).strip()
    except OSError:
        return {}
    return table


def load_oui() -> dict[str, str]:
    """内置表 + 系统 IEEE 库（有就覆盖，权威性更高）。"""
    global _oui_map
    with _oui_lock:
        if _oui_map is not None:
            return _oui_map
        table = dict(_OUI_BUILTIN)
        for path in OUI_FILES:
            if not os.path.isfile(path):
                continue
            extra = _read_oui_file(path)
            if extra:
                table.update(extra)
                log.debug("OUI 库已加载 %s（%d 条）", path, len(extra))
                break
        _oui_map = table
        return table


def _oui_online(prefix: str, timeout: float = 3.0) -> str | None:
    if prefix in _oui_online_cache:
        return _oui_online_cache[prefix]
    vendor = None
    try:
        res = _fetch(f"https://api.macvendors.com/{prefix[:2]}-{prefix[2:4]}-{prefix[4:6]}", timeout=timeout)
        body = _text(res.get("body") or b"").strip()
        if res.get("ok") and body and len(body) < 80 and "not found" not in body.lower():
            vendor = body
    except Exception:  # noqa: BLE001
        vendor = None
    _oui_online_cache[prefix] = vendor
    return vendor


def vendor_from_mac(mac: str | None, online: bool = True) -> tuple[str | None, str]:
    prefix = oui_prefix(mac)
    if not prefix:
        return None, "none"
    vendor = load_oui().get(prefix)
    if vendor:
        return vendor, "local"
    if online:
        vendor = _oui_online(prefix)
        if vendor:
            return vendor, "online"
    return None, "none"


# ---------------------------------------------------------------- HTTP
def _fetch(url: str, timeout: float = 4.0, max_bytes: int = 96_000) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    body = gzip.decompress(body)
                except OSError:
                    pass
            return {
                "ok": 200 <= resp.status < 400,
                "status": resp.status,
                "headers": {k.lower(): v for k, v in resp.headers.items()},
                "body": body,
                "url": resp.geturl(),
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(max_bytes) or b""
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": False,
            "status": exc.code,
            "headers": {k.lower(): v for k, v in (exc.headers or {}).items()},
            "body": body,
            "url": url,
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "headers": {}, "body": b"", "url": url,
                "error": str(exc)[:120]}


# ---------------------------------------------------------------- 第 2 层：厂商接口
def _huawei_deviceinfo(base: str, timeout: float = 4.0) -> dict | None:
    """华为/荣耀路由的免鉴权设备信息接口，直接给中文型号。"""
    res = _fetch(base + "/api/system/deviceinfo", timeout=timeout)
    body = res.get("body") or b""
    if not body.lstrip().startswith(b"{"):
        return None
    try:
        data = json.loads(_text(body))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cust = data.get("custinfo") or {}
    model = _clean(cust.get("CustZHFriendlyName")) or _clean(data.get("FriendlyName"))
    model_en = _clean(cust.get("CustENFriendlyName")) or _clean(data.get("FriendlyName"))
    code = _clean(cust.get("CustDeviceName")) or _clean(data.get("NetECCode"))
    if not (model or model_en or code):
        return None
    display = model or model_en or code
    series = re.sub(r"^(华为路由|华为|荣耀路由|HUAWEI(\s+WiFi)?|HONOR)\s*", "", display, flags=re.I).strip()
    uptime = data.get("UpTime")
    try:
        uptime = int(uptime) if uptime not in (None, "") else None
    except (TypeError, ValueError):
        uptime = None
    return {
        "brand_zh": "华为" if "honor" not in json.dumps(cust, ensure_ascii=False).lower() else "荣耀",
        "brand_en": "HUAWEI",
        "model": display,
        "model_en": model_en or display,
        "code": code,
        "series": series or None,
        "hw_version": _clean(data.get("HardwareVersion")),
        "sw_version": _clean(data.get("SoftwareVersion")),
        "serial": _clean(data.get("SerialNumber")),
        "uptime": uptime,
        "confidence": "high",
        "source": {"key": "vendor-api", "label": "华为设备信息接口", "detail": "/api/system/deviceinfo"},
    }


def _xiaomi_deviceinfo(base: str, timeout: float = 3.5) -> dict | None:
    """小米路由：init_info 返回 hardware 代号，如 R4A / RA67。"""
    for path, kind in (("/cgi-bin/luci/api/xqsystem/init_info", "json"),
                       ("/cgi-bin/luci/web", "html")):
        res = _fetch(base + path, timeout=timeout, max_bytes=200_000)
        body = res.get("body") or b""
        if not body:
            continue
        text = _text(body)
        code = None
        if kind == "json":
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if isinstance(data, dict):
                code = _clean(data.get("hardware")) or _clean(data.get("model"))
        else:
            if "miwifi" not in text.lower():
                continue
            m = re.search(r"var\s+(?:model|hardware)\s*=\s*[\"']([^\"']{2,24})[\"']", text)
            code = m.group(1) if m else None
        if not code:
            continue
        return {
            "brand_zh": "小米", "brand_en": "Xiaomi",
            "model": f"小米路由器 {code}", "model_en": f"Xiaomi Router {code}",
            "code": code, "series": code,
            "hw_version": None, "sw_version": None, "serial": None, "uptime": None,
            "confidence": "high",
            "source": {"key": "vendor-api", "label": "小米路由接口", "detail": path},
        }
    return None


def _huawei_web_code(base: str, timeout: float = 3.5) -> dict | None:
    """华为新版 Web UI 的兜底：首页样式表带 ?v=xxx.YGJN-BE33-10 这种产品代号。"""
    res = _fetch(base + "/html/index.html", timeout=timeout, max_bytes=200_000)
    body = res.get("body") or b""
    if not body:
        return None
    text = _text(body)
    m = re.search(r"\?v=[\d.]+\.([A-Z0-9]{3,}(?:-[A-Z0-9]+){1,3})", text)
    if not m:
        return None
    code = m.group(1)
    model = None
    js = _fetch(base + "/js/multiversion.js", timeout=timeout, max_bytes=400_000)
    if js.get("body"):
        js_text = _text(js["body"])
        block = re.search(r"['\"]" + re.escape(code) + r"['\"]\s*:\s*\{(.*?)\n\s*\}", js_text, re.S)
        if block:
            tm = re.search(r"['\"]productTitle['\"]\s*:\s*['\"]([^'\"]+)['\"]", block.group(1))
            if tm:
                model = tm.group(1).strip()
    series = re.sub(r"^(华为路由|华为|HUAWEI(\s+WiFi)?)\s*", "", model or "", flags=re.I).strip()
    return {
        "brand_zh": "华为", "brand_en": "HUAWEI",
        "model": model or code, "model_en": model or code,
        "code": code, "series": series or None,
        "hw_version": None, "sw_version": None, "serial": None, "uptime": None,
        "confidence": "high" if model else "medium",
        "source": {"key": "webui", "label": "华为 Web UI 产品代号", "detail": code},
    }


# ---------------------------------------------------------------- 第 3 层：UPnP / SSDP
def _ssdp_search(st: str, timeout: float, attempts: int = 2) -> list[dict]:
    msg = "\r\n".join((
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
        'MAN: "ssdp:discover"',
        "MX: 2",
        f"ST: {st}",
        "", "",
    )).encode()
    found: list[dict] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.4)
        for _ in range(max(1, attempts)):
            try:
                sock.sendto(msg, SSDP_ADDR)
            except OSError:
                break
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65507)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            headers = {}
            for line in text.splitlines()[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            found.append({"ip": addr[0], "headers": headers})
    finally:
        sock.close()
    return found


def _xml_field(xml_text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml_text, re.S | re.I)
    if not m:
        return None
    value = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return value or None


def ssdp_discover(gateway: str, timeout: float = 2.4) -> dict | None:
    """找到网关对应的 UPnP 设备描述，返回厂商/型号。"""
    locations: list[str] = []
    servers: dict[str, str] = {}
    for st in SSDP_TARGETS:
        for item in _ssdp_search(st, timeout=max(0.8, timeout / len(SSDP_TARGETS))):
            loc = item["headers"].get("location")
            if not loc or loc in locations:
                continue
            locations.append(loc)
            if item["headers"].get("server"):
                servers[loc] = item["headers"]["server"]
        if locations:
            break
    if not locations:
        return None
    # 网关自己的描述文件优先
    locations.sort(key=lambda u: 0 if re.search(rf"//{re.escape(gateway)}[:/]", u) else 1)
    for loc in locations[:3]:
        res = _fetch(loc, timeout=3.5, max_bytes=200_000)
        if not res.get("body"):
            continue
        xml_text = _text(res["body"])
        model = _xml_field(xml_text, "modelName")
        number = _xml_field(xml_text, "modelNumber")
        maker = _xml_field(xml_text, "manufacturer")
        friendly = _xml_field(xml_text, "friendlyName")
        if not (model or number or maker):
            continue
        brand_zh, brand_en = brand_of(f"{maker or ''} {model or ''} {friendly or ''}")
        display = model or number
        if number and number.lower() not in (display or "").lower():
            display = f"{display} ({number})" if display else number
        return {
            "brand_zh": brand_zh, "brand_en": brand_en,
            "model": display, "model_en": display,
            "code": number, "series": None,
            "hw_version": None, "sw_version": None, "serial": None, "uptime": None,
            "vendor": _clean(maker),
            "friendly": _clean(friendly),
            "server": servers.get(loc),
            "confidence": "high" if (brand_zh and display) else "medium",
            "source": {"key": "upnp", "label": "UPnP 设备描述", "detail": loc},
        }
    return None


# ---------------------------------------------------------------- 第 4 层：网页指纹
def _guess_model(text: str) -> str | None:
    """从标题/页面里抠一个像型号的串，认不准就返回 None。"""
    if not text:
        return None
    # 显式标注优先
    m = re.search(r"(?:型号|产品型号|device\s*model|model(?:\s*name)?)\s*[:：=]\s*([A-Za-z0-9][\w\-+ ]{2,24})", text, re.I)
    if m:
        return m.group(1).strip()
    candidates: list[str] = []
    # 型号一般是 "RT-AX86U" / "HG8245H" / "R4A" / "BE3 Pro+" 这种：字母+数字，可带连字符或后缀词
    pattern = r"[A-Z]{1,5}[A-Z0-9]*(?:-[A-Z0-9]{2,8})*(?:\s(?:Pro|Plus|Max|Ultra|Lite|SE)\+?)?"
    for token in re.findall(pattern, text):
        token = token.strip()
        core = token.replace(" ", "")
        if not (3 <= len(core) <= 20):
            continue
        if not (re.search(r"\d", core) and re.search(r"[A-Z]", core)):
            continue
        if core.upper() in MODEL_NOISE:
            continue
        candidates.append(token)
    if not candidates:
        return None
    # 型号一般带数字且字母数字混排，取最长的那个
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def web_fingerprint(gateway: str, timeout: float = 3.5) -> dict | None:
    """抓登录页，用品牌关键字 + 标题抠型号。"""
    base = f"http://{gateway}"
    pages = []
    for path in ("/", "/html/index.html", "/login.html"):
        res = _fetch(base + path, timeout=timeout)
        if not res.get("status"):
            break  # 网关根本不响应 HTTP，换路径再试只是白等
        pages.append(res)
        if re.search(r"(?is)<title[^>]*>\s*\S", _text(res.get("body") or b"")):
            break  # 标题里有内容就够判断了
        if len(pages) >= 2:
            break
    if not pages:
        return None

    best: dict | None = None
    for res in pages:
        if not res.get("status"):
            # 连不上就别换路径再试了，省时间
            break
        text = _text(res.get("body") or b"")
        title = None
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        if m:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip() or None
        server = (res.get("headers") or {}).get("server")
        haystack = " ".join(filter(None, [title, server, text[:6000]]))
        brand_zh, brand_en = brand_of(haystack)
        model = _guess_model(title or "") or None
        if not model:
            meta = re.search(r'(?is)<meta[^>]+name=["\'](?:description|keywords)["\'][^>]+content=["\']([^"\']+)', text)
            if meta:
                model = _guess_model(meta.group(1))
        if not model:
            jm = re.search(r"(?:var\s+)?(?:productid|productId|model|modelName)\s*[:=]\s*[\"']([\w\-+]{3,20})[\"']", text)
            if jm:
                model = jm.group(1)
        if not (brand_zh or model):
            continue
        candidate = {
            "brand_zh": brand_zh, "brand_en": brand_en,
            "model": model, "model_en": model,
            "code": None, "series": None,
            "hw_version": None, "sw_version": None, "serial": None, "uptime": None,
            "title": title, "server": _clean(server),
            "confidence": "medium" if model else "low",
            "source": {"key": "webui", "label": "登录页指纹", "detail": title or _clean(server) or "页面关键字"},
        }
        if best is None or (candidate["model"] and not best["model"]):
            best = candidate
        if best["model"] and best["brand_zh"]:
            break
    return best


# ---------------------------------------------------------------- 汇总
def identify(gateway: str, mac: str | None = None, online_oui: bool = True,
             timeout: float = 4.0) -> dict:
    """跑完四层探测，返回合并后的识别结果。整个过程 1~6 秒，全部做了超时保护。"""
    started = time.perf_counter()
    base = f"http://{gateway}"
    mac = norm_mac(mac)
    result: dict = {
        "ok": False,
        "gateway": gateway,
        "mac": mac,
        "brand": None, "brand_en": None,
        "model": None, "model_en": None,
        "code": None, "series": None,
        "vendor": None, "vendor_source": "none",
        "hw_version": None, "sw_version": None, "serial": None,
        "uptime": None, "friendly": None,
        "sources": [], "confidence": "low",
        "title": None, "server": None,
        "detected_at": int(time.time()),
        "duration_ms": 0,
        "error": None,
    }

    # 第 1 层：MAC 厂商（纯本地，很快）
    vendor, vendor_src = vendor_from_mac(mac, online=online_oui)
    if vendor:
        result["vendor"] = vendor
        result["vendor_source"] = vendor_src
        result["sources"].append({
            "key": "mac", "label": "MAC 厂商" if vendor_src == "local" else "MAC 厂商（联网）",
            "detail": vendor,
        })
        zh, en = brand_of(vendor)
        if zh:
            result["brand"], result["brand_en"] = zh, en

    # 第 2~4 层并行跑，谁先回来算谁的
    layers: list[dict | None] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="identify") as pool:
        futures = [
            pool.submit(_huawei_deviceinfo, base, timeout),
            pool.submit(_xiaomi_deviceinfo, base, timeout),
            pool.submit(ssdp_discover, gateway, 1.8),
            pool.submit(web_fingerprint, gateway, timeout),
        ]
        for fut in futures:
            try:
                layers.append(fut.result(timeout=timeout + 3))
            except Exception:  # noqa: BLE001
                layers.append(None)

    huawei, xiaomi, upnp, web = (layers + [None, None, None, None])[:4]

    # 华为没走通、但页面本身能打开时，再试一下 Web UI 里的产品代号
    elapsed = time.perf_counter() - started
    if not huawei and web and elapsed < timeout * 2:
        try:
            huawei = _huawei_web_code(base, timeout)
        except Exception:  # noqa: BLE001
            huawei = None

    ordered = [d for d in (huawei, xiaomi, upnp, web) if d]
    ordered.sort(key=lambda d: {"high": 0, "medium": 1, "low": 2}.get(d.get("confidence"), 3))

    # 品牌来源的可信优先级：MAC 厂商 1 分，页面/接口报价 2 分，报价且带型号 3 分
    brand_score = 1 if result["brand"] else 0
    for layer in ordered:
        contributed = False
        if layer.get("model") and not result["model"]:
            result["model"] = layer["model"]
            result["model_en"] = layer.get("model_en") or layer["model"]
            result["confidence"] = layer.get("confidence") or "medium"
            contributed = True
        for field in ("code", "series", "hw_version", "sw_version", "serial"):
            if layer.get(field) and not result.get(field):
                result[field] = layer[field]
                contributed = True
        for field in ("uptime", "friendly", "title", "server"):
            if layer.get(field) and not result.get(field):
                result[field] = layer[field]
                contributed = True
        # 品牌：带型号的层 > 只报品牌的层 > MAC 厂商
        if layer.get("brand_zh"):
            score = 3 if layer.get("model") else 2
            if score > brand_score:
                result["brand"] = layer["brand_zh"]
                result["brand_en"] = layer.get("brand_en") or result["brand_en"]
                brand_score = score
                contributed = True
            elif layer["brand_zh"] == result["brand"] and layer.get("model"):
                brand_score = max(brand_score, 3)
        src = layer.get("source")
        # 只有真的补充了信息才记来源，避免"登录页指纹"这种空手而归也占一行
        if src and contributed and not any(s["key"] == src["key"] for s in result["sources"]):
            result["sources"].append(src)

    if not result["model"]:
        if result["brand"]:
            result["model"] = f"{result['brand']}路由器"
            result["confidence"] = "low"
        else:
            result["error"] = "未能识别型号"
    else:
        result["ok"] = True

    # 置信度：型号来自接口/UPnP 且品牌有交叉验证 → high
    if result["ok"]:
        if result["confidence"] == "high" and result["brand"]:
            result["confidence"] = "high"
        elif result["confidence"] == "high":
            result["confidence"] = "medium"

    result["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return result


# ---------------------------------------------------------------- 缓存与调度
class RouterIdentity:
    """识别结果的持有者：内存 + 磁盘缓存，异步刷新，网关变化时重认。"""

    def __init__(self, cfg, cache_path: str) -> None:
        self.cfg = cfg
        self.path = cache_path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._busy = False
        self._thread: threading.Thread | None = None
        self._last_attempt = 0.0
        self._load()

    # ------------------------------------------------------------ 缓存
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("gateway"):
                self._data = data
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.debug("识别结果缓存写入失败", exc_info=True)

    # ------------------------------------------------------------ 状态
    def view(self) -> dict:
        with self._lock:
            data = dict(self._data)
            busy = self._busy
        if not data:
            status = "detecting" if busy else "none"
        else:
            status = "detecting" if busy else ("ok" if data.get("model") else "failed")
        # 路由器运行时长按"识别那一刻 + 流逝时间"推算，不然缓存期内一直是旧值
        if data.get("uptime") and data.get("detected_at"):
            data["uptime"] = int(data["uptime"]) + max(0, int(time.time() - data["detected_at"]))
        data["status"] = status
        data["auto"] = bool(self.cfg.get("router.auto_detect", True))
        return data

    def cached_gateway(self) -> str | None:
        with self._lock:
            return self._data.get("gateway")

    def age(self) -> float:
        with self._lock:
            ts = self._data.get("detected_at") or 0
        return time.time() - ts if ts else 1e18

    # ------------------------------------------------------------ 识别
    def identify_async(self, gateway: str | None, mac: str | None, reason: str = "manual") -> bool:
        if not gateway:
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._thread = threading.Thread(
            target=self._run, args=(gateway, mac, reason), name="routercat-identify", daemon=True
        )
        self._thread.start()
        return True

    def _run(self, gateway: str, mac: str | None, reason: str) -> None:
        try:
            info = identify(
                gateway,
                mac,
                online_oui=bool(self.cfg.get("router.oui_online", True)),
                timeout=float(self.cfg.get("router.detect_timeout", 4) or 4),
            )
            with self._lock:
                self._data = info
                self._last_attempt = time.time()
            self._save()
            log.info(
                "识别路由器：%s（%s，耗时 %dms，来源 %s）",
                info.get("model") or "未识别",
                info.get("confidence"),
                info.get("duration_ms") or 0,
                "、".join(s["label"] for s in info.get("sources") or []) or "无",
            )
        except Exception:  # noqa: BLE001
            log.exception("路由器识别失败")
            with self._lock:
                self._last_attempt = time.time()
        finally:
            with self._lock:
                self._busy = False

    def maybe_refresh(self, gateway: str | None, mac: str | None) -> None:
        """网关换了、缓存过期、或者上次失败过，都重试一次。"""
        if not gateway or not self.cfg.get("router.auto_detect", True):
            return
        with self._lock:
            busy = self._busy
            data = dict(self._data)
        if busy:
            return
        ttl = float(self.cfg.get("router.detect_ttl", 21600) or 21600)
        changed = data.get("gateway") != gateway or (data.get("mac") or None) != (mac or None)
        failed = bool(data) and not data.get("model")
        expired = self.age() > (600 if failed else ttl)
        if changed or expired or not data:
            self.identify_async(gateway, mac, reason="refresh")
