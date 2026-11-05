"""RouterCat - 配置管理。

默认配置内置在代码里，用户修改写入 data/config.json，读取时做深度合并，
这样升级版本新增的配置项不会丢失。
"""
from __future__ import annotations

import copy
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ROUTERCAT_DATA", os.path.join(BASE_DIR, "data"))
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "routercat.db")

DEFAULTS = {
    "app": {
        "title": "RouterCat",
        "listen_host": "0.0.0.0",
        "listen_port": 8848,
        # auto = 跟随浏览器语言；zh / en = 固定语言
        "lang": "auto",
    },
    "router": {
        # auto = 自动从默认路由探测网关地址
        "gateway": "auto",
        # 留空 = 用自动识别出来的型号；填了就用你写的名字
        "name": "",
        "image": "/img/router.png",
        # 自动识别型号（MAC 厂商 / 厂商接口 / UPnP / 登录页指纹）
        "auto_detect": True,
        "detect_ttl": 21600,       # 识别结果缓存时长（秒），6 小时
        "detect_timeout": 4,       # 单个探测的超时（秒）
        "oui_online": True,        # 本地 MAC 厂商库查不到时，是否联网补查
    },
    "probe": {
        "gateway_interval": 2,
        "wan_interval": 5,
        "wan_targets": ["223.5.5.5", "119.29.29.29"],
        "dns_interval": 30,
        "dns_targets": ["www.baidu.com"],
        "tcp_interval": 15,
        "tcp_targets": [
            {"host": "223.5.5.5", "port": 53, "label": "阿里 DNS :53"},
            {"host": "www.baidu.com", "port": 443, "label": "百度 :443"},
        ],
        "http_interval": 60,
        "http_targets": [{"url": "https://www.baidu.com", "label": "百度首页"}],
        "ping_timeout": 1,
    },
    "sample": {
        "interval": 2,
    },
    "speedtest": {
        "duration": 8,
        "upload_size_mb": 10,
        # 下载测速会依次试这些地址，挑最快的用
        "download_urls": [
            "https://mirrors.aliyun.com/ubuntu/ls-lR.gz",
            "https://mirrors.cloud.tencent.com/ubuntu/ls-lR.gz",
            "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ls-lR.gz",
            "https://speed.cloudflare.com/__down?bytes=200000000",
        ],
        # 上传测速：会依次尝试，全部不可达时自动跳过上行测试
        "upload_urls": [
            "https://httpbin.org/post",
            "https://speed.cloudflare.com/__up",
        ],
        "upload_url": "",  # 手动指定则优先使用（留空表示用上面的列表）
        "latency_target": "",  # 留空则用第一个外网探测目标
    },
    "retention": {
        "days": 7,
    },
    "thresholds": {
        "loss_pct": 5.0,
        "latency_ms": 80.0,
        "jitter_ms": 40.0,
        "cpu_pct": 90.0,
        "mem_pct": 92.0,
        "disk_pct": 90.0,
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self.load()

    # ---------------------------------------------------------------- 读写
    def load(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                self._data = _deep_merge(DEFAULTS, user)
        except FileNotFoundError:
            self.save()
        except Exception:
            # 配置损坏时回退到默认值，不阻塞启动
            self._data = copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        with self._lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)

    # ---------------------------------------------------------------- 访问
    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, path: str, default=None):
        cur = self._data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return copy.deepcopy(cur)

    def patch(self, patch: dict) -> dict:
        """局部更新配置（深度合并）并落盘。"""
        with self._lock:
            self._data = _deep_merge(self._data, patch or {})
            self.save()
            return copy.deepcopy(self._data)

    def threshold(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(f"thresholds.{key}", default))
        except (TypeError, ValueError):
            return default
