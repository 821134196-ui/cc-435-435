"""SQLite 持久层：建库、连接与种子数据（虚构数据）。"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

# 金额列固定 4 位小数（本币按 2 位展示）
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS currencies (
    code        TEXT PRIMARY KEY,          -- ISO 币种代码，如 USD
    name        TEXT NOT NULL,             -- 虚构展示名
    symbol      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS denominations (
    id          INTEGER PRIMARY KEY,
    currency    TEXT NOT NULL REFERENCES currencies(code),
    face_value  REAL NOT NULL,             -- 券别面额
    seq         INTEGER NOT NULL,          -- 展示排序
    UNIQUE(currency, face_value)
);

CREATE TABLE IF NOT EXISTS branches (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

-- 网点 × 券别 的现钞库存（可用/已锁定均以“张数”计）
CREATE TABLE IF NOT EXISTS inventory (
    branch_code     TEXT NOT NULL REFERENCES branches(code),
    denom_id        INTEGER NOT NULL REFERENCES denominations(id),
    total_qty       INTEGER NOT NULL DEFAULT 0,   -- 实物总量
    locked_qty      INTEGER NOT NULL DEFAULT 0,   -- 被有效预约锁定
    PRIMARY KEY (branch_code, denom_id),
    CHECK (locked_qty >= 0),
    CHECK (locked_qty <= total_qty)
);

-- 汇率版本：本币 CNY / 外币；rate = 1 外币兑多少本币
CREATE TABLE IF NOT EXISTS rate_versions (
    id          INTEGER PRIMARY KEY,
    currency    TEXT NOT NULL REFERENCES currencies(code),
    rate        REAL NOT NULL,              -- 外币->本币
    eff_date    TEXT NOT NULL,              -- 生效日 YYYY-MM-DD
    published_at TEXT NOT NULL,
    note        TEXT DEFAULT '',
    UNIQUE(currency, eff_date)
);

CREATE TABLE IF NOT EXISTS tellers (
    id          TEXT PRIMARY KEY,           -- T01
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('cashier','supervisor'))
);

-- 预约
CREATE TABLE IF NOT EXISTS reservations (
    id              TEXT PRIMARY KEY,       -- RV-yyyyMMdd-xxxx
    customer_name   TEXT NOT NULL,          -- 虚构客户
    id_doc          TEXT NOT NULL,          -- 虚构证件号
    currency        TEXT NOT NULL,
    branch_code     TEXT NOT NULL,
    cny_amount      REAL NOT NULL,          -- 确认时按快照汇率折算的本币金额
    fx_rate         REAL,                   -- 确认时锁定的汇率快照
    rate_version_id INTEGER,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','held','confirmed','replacement_offered',
                                      'replacement_confirmed','fulfilled','cancelled','expired')),
    expires_at      TEXT NOT NULL,          -- 取钞有效期（ISO）
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    fulfilled_at    TEXT,
    cancel_reason   TEXT DEFAULT '',
    created_by      TEXT REFERENCES tellers(id)
);

-- 预约行：每个券别数量；替换通过 replacement_of 指向原行
CREATE TABLE IF NOT EXISTS reservation_lines (
    id              INTEGER PRIMARY KEY,
    reservation_id  TEXT NOT NULL REFERENCES reservations(id),
    denom_id        INTEGER NOT NULL REFERENCES denominations(id),
    qty             INTEGER NOT NULL CHECK (qty > 0),
    kind            TEXT NOT NULL DEFAULT 'original'
                    CHECK (kind IN ('original','replacement')),
    replacement_of  INTEGER REFERENCES reservation_lines(id),
    customer_accepted INTEGER DEFAULT 0     -- 替换行是否已被客户重新确认
);

-- 锁库记录（幂等键：同一预约同一券别只锁一次；取消/过期/交付时释放）
CREATE TABLE IF NOT EXISTS inventory_locks (
    id              INTEGER PRIMARY KEY,
    reservation_id  TEXT NOT NULL REFERENCES reservations(id),
    branch_code     TEXT NOT NULL,
    denom_id        INTEGER NOT NULL,
    qty             INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'held'
                    CHECK (state IN ('held','released','delivered')),
    created_at      TEXT NOT NULL,
    released_at     TEXT,
    UNIQUE(reservation_id, denom_id)
);

-- 柜面现钞交付：按券别记录预约/实点/差额
CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY,
    reservation_id  TEXT NOT NULL REFERENCES reservations(id),
    denom_id        INTEGER NOT NULL,
    booked_qty      INTEGER NOT NULL,       -- 预约数量
    counted_qty     INTEGER NOT NULL,       -- 实点数量（模拟点验适配器）
    diff_qty        INTEGER NOT NULL,       -- 差额 = 实点 - 预约
    suspicious      INTEGER NOT NULL DEFAULT 0,  -- 假钞疑点
    idem_group      TEXT,                       -- 交付幂等键（同一次交付的各券别行共享）
    delivered_by    TEXT NOT NULL REFERENCES tellers(id),
    reviewed_by     TEXT REFERENCES tellers(id),  -- 复核柜员
    review_note     TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE(reservation_id, denom_id, idem_group)
);

-- 业务事件流水（跨日汇率、券别替换、库存不足、取消、差额、假钞等）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    type        TEXT NOT NULL,
    reservation_id TEXT,
    actor       TEXT,
    detail      TEXT NOT NULL
);
"""


def _new_conn(path: str | os.PathLike) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.executescript(SCHEMA)
    return conn


def _is_seeded(conn) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name='currencies'"
    ).fetchone()
    if not row["c"]:
        return False
    return conn.execute("SELECT COUNT(*) c FROM currencies").fetchone()["c"] > 0


class Database:
    """每线程一个 SQLite 连接；线程安全地共享同一个库文件。"""

    def __init__(self, db_path: str | os.PathLike = "fx.db"):
        self.path = str(db_path)
        self._local = threading.local()
        init = _new_conn(self.path)
        if not _is_seeded(init):
            seed(init)
        init.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            self._local.conn = _new_conn(self.path)
        return self._local.conn


def connect(db_path: str | os.PathLike = "fx.db") -> sqlite3.Connection:
    """单线程场景的便捷连接（建库并写种子）。"""
    conn = _new_conn(db_path)
    if not _is_seeded(conn):
        seed(conn)
    return conn


def seed(conn: sqlite3.Connection) -> None:
    """写入虚构基础数据：币种、券别、网点、库存、柜员、汇率版本。"""
    now = datetime(2026, 9, 27, 9, 0, 0)
    today = "2026-09-27"
    tomorrow = "2026-09-28"

    conn.executemany(
        "INSERT INTO currencies(code,name,symbol) VALUES(?,?,?)",
        [
            ("USD", "虚构美元", "$"),
            ("EUR", "虚构欧元", "€"),
            ("JPY", "虚构日元", "¥"),
            ("HKD", "虚构港币", "HK$"),
        ],
    )

    denoms = {
        "USD": [1, 5, 10, 20, 50, 100],
        "EUR": [5, 10, 20, 50, 100, 200],
        "JPY": [1000, 5000, 10000],
        "HKD": [20, 50, 100, 500, 1000],
    }
    rows = []
    for cur, vals in denoms.items():
        for i, v in enumerate(vals):
            rows.append((cur, float(v), i))
    conn.executemany(
        "INSERT INTO denominations(currency,face_value,seq) VALUES(?,?,?)", rows
    )

    conn.executemany(
        "INSERT INTO branches(code,name) VALUES(?,?)",
        [
            ("B01", "虚构·中山路支行"),
            ("B02", "虚构·滨江支行"),
        ],
    )

    conn.executemany(
        "INSERT INTO tellers(id,name,role) VALUES(?,?,?)",
        [
            ("T01", "柜员甲", "cashier"),
            ("T02", "柜员乙", "cashier"),
            ("T03", "主管丙", "supervisor"),
        ],
    )

    # 库存（张数）——刻意把部分券别设紧，便于触发库存不足/替换
    stock = {
        "B01": {
            ("USD", 100): 40, ("USD", 50): 30, ("USD", 20): 60, ("USD", 10): 50,
            ("USD", 5): 40, ("USD", 1): 100,
            ("EUR", 100): 25, ("EUR", 50): 20, ("EUR", 20): 30, ("EUR", 10): 20,
            ("EUR", 5): 30, ("EUR", 200): 6,
            ("JPY", 10000): 15, ("JPY", 5000): 10, ("JPY", 1000): 40,
            ("HKD", 1000): 20, ("HKD", 500): 25, ("HKD", 100): 60,
            ("HKD", 50): 50, ("HKD", 20): 80,
        },
        "B02": {
            ("USD", 100): 18, ("USD", 50): 12, ("USD", 20): 30, ("USD", 10): 0,
            ("USD", 5): 20, ("USD", 1): 60,
            ("EUR", 100): 10, ("EUR", 50): 8, ("EUR", 20): 15, ("EUR", 10): 0,
            ("EUR", 5): 20, ("EUR", 200): 2,
            ("JPY", 10000): 8, ("JPY", 5000): 0, ("JPY", 1000): 25,
            ("HKD", 1000): 10, ("HKD", 500): 12, ("HKD", 100): 30,
            ("HKD", 50): 25, ("HKD", 20): 40,
        },
    }
    inv_rows = []
    for branch, m in stock.items():
        for (cur, face), qty in m.items():
            denom_id = conn.execute(
                "SELECT id FROM denominations WHERE currency=? AND face_value=?",
                (cur, float(face)),
            ).fetchone()["id"]
            inv_rows.append((branch, denom_id, qty))
    conn.executemany(
        "INSERT INTO inventory(branch_code,denom_id,total_qty,locked_qty) VALUES(?,?,?,0)",
        inv_rows,
    )
    # 汇率版本：今天与昨天各一版（用于跨日快照演示）
    rates = {
        "USD": (7.1820, 7.1765),
        "EUR": (7.8450, 7.8310),
        "JPY": (0.04862, 0.04851),
        "HKD": (0.92150, 0.92080),
    }
    for cur, (r_today, r_yest) in rates.items():
        conn.execute(
            "INSERT INTO rate_versions(currency,rate,eff_date,published_at,note) VALUES(?,?,?,?,?)",
            (cur, r_yest, "2026-09-26", "2026-09-26T08:30:00", "上日牌价"),
        )
        conn.execute(
            "INSERT INTO rate_versions(currency,rate,eff_date,published_at,note) VALUES(?,?,?,?,?)",
            (cur, r_today, today, f"{today}T08:30:00", "当日牌价"),
        )
    conn.execute(
        "INSERT INTO events(ts,type,actor,detail) VALUES(?,?,?,?)",
        (now.isoformat(timespec="seconds"), "seed", "system", "初始化虚构基础数据"),
    )
