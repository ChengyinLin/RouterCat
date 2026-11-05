"""RouterCat - SQLite 存储层（WAL + 线程本地连接）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS ping (
    ts     INTEGER NOT NULL,
    target TEXT    NOT NULL,
    ok     INTEGER NOT NULL,
    rtt    REAL
);
CREATE INDEX IF NOT EXISTS idx_ping_target_ts ON ping(target, ts);
CREATE INDEX IF NOT EXISTS idx_ping_ts ON ping(ts);

CREATE TABLE IF NOT EXISTS sys (
    ts       INTEGER PRIMARY KEY,
    cpu      REAL,
    per_core TEXT,
    mem_pct  REAL,
    mem_used INTEGER,
    mem_total INTEGER,
    swap_pct REAL,
    load1    REAL
);

CREATE TABLE IF NOT EXISTS net (
    ts    INTEGER NOT NULL,
    iface TEXT NOT NULL,
    rx_bps REAL,
    tx_bps REAL
);
CREATE INDEX IF NOT EXISTS idx_net_ts ON net(ts);

CREATE TABLE IF NOT EXISTS disk (
    ts        INTEGER PRIMARY KEY,
    read_bps  REAL,
    write_bps REAL
);

CREATE TABLE IF NOT EXISTS checks (
    ts      INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    target  TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    latency REAL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks ON checks(kind, target, ts);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key      TEXT NOT NULL,
    level    TEXT NOT NULL,
    title    TEXT NOT NULL,
    detail   TEXT,
    ts_start INTEGER NOT NULL,
    ts_end   INTEGER,
    status   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(ts_start DESC);

CREATE TABLE IF NOT EXISTS devices (
    ip         TEXT PRIMARY KEY,
    mac        TEXT,
    iface      TEXT,
    hostname   TEXT,
    first_seen INTEGER,
    last_seen  INTEGER
);

CREATE TABLE IF NOT EXISTS speedtest (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             INTEGER NOT NULL,
    down_mbps      REAL,
    down_avg_mbps  REAL,
    up_mbps        REAL,
    latency_idle   REAL,
    latency_loaded REAL,
    server         TEXT,
    server_url     TEXT,
    bytes_down     INTEGER,
    bytes_up       INTEGER,
    duration       REAL
);
CREATE INDEX IF NOT EXISTS idx_speedtest_ts ON speedtest(ts DESC);
"""


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            self.conn().executescript(SCHEMA)
            self.conn().commit()
            self._migrate()

    def _migrate(self) -> None:
        """老库补列：事件的 code / params 用于前端多语言渲染。"""
        cols = {r["name"] for r in self.query("PRAGMA table_info(events)")}
        for col in ("code", "params"):
            if col not in cols:
                try:
                    self.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=8000")
            self._local.conn = c
        return c

    # ------------------------------------------------------------ 基础执行
    def execute(self, sql: str, args=()):
        c = self.conn()
        with c:
            return c.execute(sql, args)

    def query(self, sql: str, args=()):
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def query_one(self, sql: str, args=()):
        row = self.conn().execute(sql, args).fetchone()
        return dict(row) if row else None

    def executemany(self, sql: str, rows) -> None:
        c = self.conn()
        with c:
            c.executemany(sql, rows)

    # ------------------------------------------------------------ 写入
    def add_ping(self, ts: int, target: str, ok: bool, rtt):
        self.execute(
            "INSERT INTO ping(ts,target,ok,rtt) VALUES(?,?,?,?)",
            (ts, target, 1 if ok else 0, rtt),
        )

    def add_sys(self, ts: int, cpu: float, per_core, mem: dict, load1: float):
        import json

        self.execute(
            "INSERT OR REPLACE INTO sys(ts,cpu,per_core,mem_pct,mem_used,mem_total,swap_pct,load1)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                ts,
                cpu,
                json.dumps(per_core),
                mem.get("pct"),
                mem.get("used"),
                mem.get("total"),
                mem.get("swap_pct"),
                load1,
            ),
        )

    def add_net(self, ts: int, iface: str, rx_bps: float, tx_bps: float):
        self.execute(
            "INSERT INTO net(ts,iface,rx_bps,tx_bps) VALUES(?,?,?,?)",
            (ts, iface, rx_bps, tx_bps),
        )

    def add_disk_io(self, ts: int, read_bps: float, write_bps: float):
        self.execute(
            "INSERT OR REPLACE INTO disk(ts,read_bps,write_bps) VALUES(?,?,?)",
            (ts, read_bps, write_bps),
        )

    def add_check(self, ts: int, kind: str, target: str, ok: bool, latency, detail: str = ""):
        self.execute(
            "INSERT INTO checks(ts,kind,target,ok,latency,detail) VALUES(?,?,?,?,?,?)",
            (ts, kind, target, 1 if ok else 0, latency, detail),
        )

    def open_event(self, key: str, level: str, title: str, detail: str, ts: int,
                   params: dict | None = None) -> int:
        import json

        cur = self.execute(
            "INSERT INTO events(key,level,title,detail,ts_start,ts_end,status,code,params)"
            " VALUES(?,?,?,?,?,NULL,'open',?,?)",
            (key, level, title, detail, ts, key,
             json.dumps(params, ensure_ascii=False) if params else None),
        )
        return int(cur.lastrowid)

    def close_event(self, event_id: int, ts: int) -> None:
        self.execute(
            "UPDATE events SET status='resolved', ts_end=? WHERE id=? AND ts_end IS NULL",
            (ts, event_id),
        )

    def touch_device(self, ip: str, mac: str, iface: str, hostname=None) -> None:
        now = int(time.time())
        row = self.query_one("SELECT ip, first_seen FROM devices WHERE ip=?", (ip,))
        if row:
            self.execute(
                "UPDATE devices SET mac=?, iface=?, last_seen=?, hostname=COALESCE(?, hostname) WHERE ip=?",
                (mac, iface, now, hostname, ip),
            )
        else:
            self.execute(
                "INSERT INTO devices(ip,mac,iface,hostname,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                (ip, mac, iface, hostname, now, now),
            )

    # ------------------------------------------------------------ 测速
    def add_speedtest(self, r: dict) -> int:
        cur = self.execute(
            "INSERT INTO speedtest(ts,down_mbps,down_avg_mbps,up_mbps,latency_idle,latency_loaded,"
            "server,server_url,bytes_down,bytes_up,duration) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("ts") or int(time.time()),
                r.get("down_mbps"),
                r.get("down_avg_mbps"),
                r.get("up_mbps"),
                r.get("latency_idle"),
                r.get("latency_loaded"),
                r.get("server"),
                r.get("server_url"),
                r.get("bytes_down"),
                r.get("bytes_up"),
                r.get("duration"),
            ),
        )
        return int(cur.lastrowid)

    def speedtest_history(self, limit: int = 20):
        return self.query("SELECT * FROM speedtest ORDER BY ts DESC LIMIT ?", (int(limit),))

    # ------------------------------------------------------------ 聚合查询
    def ping_series(self, target: str, since: int, bucket: int):
        bucket = max(1, int(bucket))
        sql = """
        SELECT CAST(ts / ? AS INTEGER) * ? AS t,
               AVG(CASE WHEN ok = 1 THEN rtt END) AS avg,
               MAX(CASE WHEN ok = 1 THEN rtt END) AS max,
               MIN(CASE WHEN ok = 1 THEN rtt END) AS min,
               100.0 * (COUNT(*) - SUM(ok)) / COUNT(*) AS loss,
               COUNT(*) AS n
        FROM ping WHERE target = ? AND ts >= ?
        GROUP BY t ORDER BY t
        """
        return self.query(sql, (bucket, bucket, target, since))

    def ping_stats(self, target: str, since: int):
        return self.query_one(
            """
            SELECT COUNT(*) AS n,
                   SUM(ok) AS ok_n,
                   AVG(CASE WHEN ok=1 THEN rtt END) AS avg_rtt,
                   MIN(CASE WHEN ok=1 THEN rtt END) AS min_rtt,
                   MAX(CASE WHEN ok=1 THEN rtt END) AS max_rtt
            FROM ping WHERE target=? AND ts>=?
            """,
            (target, since),
        )

    def ping_latest(self, target: str):
        return self.query_one(
            "SELECT ts, ok, rtt FROM ping WHERE target=? ORDER BY ts DESC LIMIT 1", (target,)
        )

    def sys_series(self, field: str, since: int, bucket: int):
        allowed = {"cpu", "mem_pct", "swap_pct", "load1"}
        if field not in allowed:
            raise ValueError("bad field")
        bucket = max(1, int(bucket))
        sql = f"""
        SELECT CAST(ts / ? AS INTEGER) * ? AS t, AVG({field}) AS v
        FROM sys WHERE ts >= ? GROUP BY t ORDER BY t
        """
        return self.query(sql, (bucket, bucket, since))

    def net_series(self, iface: str, since: int, bucket: int):
        bucket = max(1, int(bucket))
        sql = """
        SELECT CAST(ts / ? AS INTEGER) * ? AS t, AVG(rx_bps) AS rx, AVG(tx_bps) AS tx
        FROM net WHERE iface = ? AND ts >= ? GROUP BY t ORDER BY t
        """
        return self.query(sql, (bucket, bucket, iface, since))

    def disk_series(self, since: int, bucket: int):
        bucket = max(1, int(bucket))
        sql = """
        SELECT CAST(ts / ? AS INTEGER) * ? AS t, AVG(read_bps) AS rd, AVG(write_bps) AS wr
        FROM disk WHERE ts >= ? GROUP BY t ORDER BY t
        """
        return self.query(sql, (bucket, bucket, since))

    def latest_checks(self):
        return self.query(
            """
            SELECT c.* FROM checks c
            JOIN (SELECT kind, target, MAX(ts) AS mts FROM checks GROUP BY kind, target) m
              ON c.kind = m.kind AND c.target = m.target AND c.ts = m.mts
            ORDER BY c.kind, c.target
            """
        )

    def recent_events(self, limit: int = 50):
        rows = self.query(
            "SELECT * FROM events ORDER BY ts_start DESC LIMIT ?", (int(limit),)
        )
        return [self._decode_event(r) for r in rows]

    def active_events(self):
        rows = self.query("SELECT * FROM events WHERE status='open' ORDER BY ts_start DESC")
        return [self._decode_event(r) for r in rows]

    @staticmethod
    def _decode_event(row: dict) -> dict:
        raw = row.get("params")
        if raw:
            try:
                row["params"] = json.loads(raw)
            except (TypeError, ValueError):
                row["params"] = None
        else:
            row["params"] = None
        return row

    def devices(self):
        return self.query("SELECT * FROM devices ORDER BY last_seen DESC")

    # ------------------------------------------------------------ 维护
    def cleanup(self, days: float) -> int:
        cutoff = int(time.time() - days * 86400)
        removed = 0
        for table in ("ping", "sys", "net", "disk", "checks", "speedtest"):
            cur = self.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            removed += cur.rowcount or 0
        # 已恢复且超过保留期的事件也清掉
        self.execute("DELETE FROM events WHERE ts_start < ? AND status='resolved'", (cutoff,))
        return removed

    def stats(self) -> dict:
        out = {}
        for table in ("ping", "sys", "net", "disk", "checks", "events", "devices", "speedtest"):
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = row["n"] if row else 0
        try:
            out["db_mb"] = round(os.path.getsize(self.path) / 1048576, 2)
        except OSError:
            out["db_mb"] = 0
        return out
