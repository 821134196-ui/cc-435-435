"""SQLite 连接、建表与演示种子数据（全部虚构）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS currencies (
    code      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    country   TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS denominations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_code TEXT NOT NULL REFERENCES currencies(code),
    face_value    TEXT NOT NULL,              -- Decimal 以文本存储
    seq           INTEGER NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(currency_code, face_value)
);

CREATE TABLE IF NOT EXISTS branches (
    code    TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    address TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    branch_code    TEXT NOT NULL REFERENCES branches(code),
    denomination_id INTEGER NOT NULL REFERENCES denominations(id),
    total_qty      INTEGER NOT NULL DEFAULT 0,
    locked_qty     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (branch_code, denomination_id),
    CHECK (total_qty >= 0),
    CHECK (locked_qty >= 0),
    CHECK (locked_qty <= total_qty)
);

CREATE TABLE IF NOT EXISTS tellers (
    username TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    role     TEXT NOT NULL CHECK (role IN ('teller','supervisor'))
);

CREATE TABLE IF NOT EXISTS customers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    id_type TEXT NOT NULL,
    id_no   TEXT NOT NULL,
    phone   TEXT NOT NULL
);

-- 汇率版本：同一币种同一日期可发布多个版本，仅最新 active 生效；历史版本不可改
CREATE TABLE IF NOT EXISTS fx_rates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_code TEXT NOT NULL REFERENCES currencies(code),
    rate_date    TEXT NOT NULL,               -- YYYY-MM-DD（银行账务日）
    version      INTEGER NOT NULL,
    rate         TEXT NOT NULL,               -- 1 外币兑人民币
    effective_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','superseded')),
    created_by   TEXT NOT NULL,
    UNIQUE(currency_code, rate_date, version)
);

CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    currency_code TEXT NOT NULL REFERENCES currencies(code),
    branch_code   TEXT NOT NULL REFERENCES branches(code),
    status        TEXT NOT NULL CHECK (status IN (
                    'RESERVED','CONFIRMED','REPLACE_PENDING',
                    'REVIEWED','DELIVERED','CANCELLED','EXPIRED')),
    reserved_date  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    valid_until    TEXT NOT NULL,
    confirmed_date TEXT,
    confirmed_at   TEXT,
    fx_rate_id     INTEGER REFERENCES fx_rates(id),  -- 确认时快照
    amount_foreign TEXT,
    amount_cny     TEXT,
    plan_version   INTEGER NOT NULL DEFAULT 1,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS appointment_items (
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id),
    denomination_id INTEGER NOT NULL REFERENCES denominations(id),
    planned_qty     INTEGER NOT NULL CHECK (planned_qty > 0),
    plan_version    INTEGER NOT NULL,
    PRIMARY KEY (appointment_id, denomination_id)
);

CREATE TABLE IF NOT EXISTS appointment_item_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id  INTEGER NOT NULL,
    denomination_id INTEGER NOT NULL,
    qty             INTEGER NOT NULL,
    plan_version    INTEGER NOT NULL,
    change_kind     TEXT NOT NULL CHECK (change_kind IN
                        ('original','substitution','reconfirmed')),
    created_at      TEXT NOT NULL,
    actor           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    branch_code     TEXT NOT NULL,
    denomination_id INTEGER NOT NULL,
    delta_locked    INTEGER NOT NULL,
    delta_total     INTEGER NOT NULL DEFAULT 0,
    ref_code        TEXT,
    kind            TEXT NOT NULL CHECK (kind IN
                        ('lock','release','deliver','restock','adjust')),
    actor           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replacements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    old_plan       TEXT NOT NULL,              -- JSON [[denom_id,qty],...]
    new_plan       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN
                        ('pending','confirmed','rejected')),
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    decided_at     TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id  INTEGER NOT NULL UNIQUE REFERENCES appointments(id),
    teller          TEXT NOT NULL REFERENCES tellers(username),
    reviewer        TEXT REFERENCES tellers(username),
    status          TEXT NOT NULL CHECK (status IN
                        ('counting','pending_review','reviewed')),
    cash_session_id TEXT NOT NULL,
    counterfeit_qty INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT
);

CREATE TABLE IF NOT EXISTS delivery_lines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id     INTEGER NOT NULL REFERENCES deliveries(id),
    denomination_id INTEGER NOT NULL REFERENCES denominations(id),
    planned_qty     INTEGER NOT NULL,
    counted_qty     INTEGER NOT NULL,
    reviewed_qty    INTEGER,
    diff            INTEGER NOT NULL DEFAULT 0,
    UNIQUE(delivery_id, denomination_id)
);

CREATE TABLE IF NOT EXISTS diff_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id),
    delivery_id     INTEGER REFERENCES deliveries(id),
    denomination_id INTEGER REFERENCES denominations(id),
    planned_qty     INTEGER,
    counted_qty     INTEGER,
    reviewed_qty    INTEGER,
    kind            TEXT NOT NULL CHECK (kind IN
                        ('short','over','counterfeit','substitution')),
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','resolved')),
    detail          TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT,
    resolution      TEXT
);

CREATE TABLE IF NOT EXISTS fx_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    event_type     TEXT NOT NULL CHECK (event_type IN
                        ('confirm','cross_day','reconfirm')),
    fx_rate_id     INTEGER REFERENCES fx_rates(id),
    detail         TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cancellation_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    kind           TEXT NOT NULL CHECK (kind IN
                        ('customer','teller','expired')),
    reason         TEXT NOT NULL,
    actor          TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    entity    TEXT NOT NULL,
    ref_code  TEXT,
    action    TEXT NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS daily_seq (
    seq_date TEXT PRIMARY KEY,
    n        INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM currencies").fetchone()[0] == 0:
        seed(conn)


# ---------------------------------------------------------------- seed data
SEED_CURRENCIES = [
    ("USD", "美元", "美国"),
    ("EUR", "欧元", "欧元区"),
    ("JPY", "日元", "日本"),
    ("HKD", "港币", "中国香港"),
    ("GBP", "英镑", "英国"),
]

# (币种, [(面值, 展示顺序)])
SEED_DENOMS = {
    "USD": [(1, 1), (5, 2), (10, 3), (20, 4), (50, 5), (100, 6)],
    "EUR": [(5, 1), (10, 2), (20, 3), (50, 4), (100, 5), (200, 6)],
    "JPY": [(1000, 1), (5000, 2), (10000, 3)],
    "HKD": [(20, 1), (50, 2), (100, 3), (500, 4), (1000, 5)],
    "GBP": [(5, 1), (10, 2), (20, 3), (50, 4)],
}

SEED_BRANCHES = [
    ("BR001", "钴蓝营业部", "虚构市银白路 1 号"),
    ("BR002", "柠檬支行", "虚构市柠檬黄大街 88 号"),
]

SEED_TELLERS = [
    ("zhang_san", "张三", "teller"),
    ("li_si", "李四", "teller"),
    ("wang_wu", "王五", "supervisor"),
]

SEED_CUSTOMERS = [
    ("林一舟", "护照", "E12345678", "138-0000-0001"),
    ("沈知夏", "港澳通行证", "H87654321", "138-0000-0002"),
    ("顾南乔", "身份证", "110101199001011234", "138-0000-0003"),
    ("苏晚棠", "护照", "E98765432", "138-0000-0004"),
]

# 每个网点每个券别的初始库存（张）
SEED_STOCK = {
    "BR001": {
        "USD": [40, 60, 80, 120, 100, 60],
        "EUR": [50, 60, 90, 70, 50, 20],
        "JPY": [120, 60, 40],
        "HKD": [100, 100, 120, 60, 30],
        "GBP": [40, 50, 40, 20],
    },
    "BR002": {
        "USD": [20, 30, 40, 60, 50, 30],
        "EUR": [20, 30, 40, 30, 20, 8],
        "JPY": [60, 30, 15],
        "HKD": [50, 50, 60, 25, 10],
        "GBP": [15, 20, 15, 8],
    },
}

SEED_RATES = {
    # 币种: [(相对今天偏移天, 汇率)] —— 演示跨日确认
    "USD": [-1, 0],
    "EUR": [-1, 0],
    "JPY": [-1, 0],
    "HKD": [-1, 0],
    "GBP": [-1, 0],
}
RATE_VALUES = {
    "USD": ["7.1028", "7.1156"],
    "EUR": ["7.7642", "7.7805"],
    "JPY": ["0.0472", "0.0475"],
    "HKD": ["0.9128", "0.9156"],
    "GBP": ["9.1385", "9.1022"],
}


def seed(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """写入虚构种子数据。幂等：仅在空库时由 init_db 调用。"""
    now = now or datetime.now()
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO currencies(code,name,country,active) VALUES (?,?,?,1)",
        SEED_CURRENCIES,
    )
    denom_ids: dict[tuple[str, Decimal], int] = {}
    for code, denoms in SEED_DENOMS.items():
        for face, seq in denoms:
            cur.execute(
                "INSERT INTO denominations(currency_code,face_value,seq,active)"
                " VALUES (?,?,?,1)",
                (code, str(face), seq),
            )
            denom_ids[(code, Decimal(face))] = cur.lastrowid
    cur.executemany(
        "INSERT INTO branches(code,name,address) VALUES (?,?,?)", SEED_BRANCHES
    )
    cur.executemany(
        "INSERT INTO tellers(username,name,role) VALUES (?,?,?)", SEED_TELLERS
    )
    cur.executemany(
        "INSERT INTO customers(name,id_type,id_no,phone) VALUES (?,?,?,?)",
        SEED_CUSTOMERS,
    )
    # 库存
    for branch, by_ccy in SEED_STOCK.items():
        for ccy, qtys in by_ccy.items():
            for (face, _), qty in zip(SEED_DENOMS[ccy], qtys):
                did = denom_ids[(ccy, Decimal(face))]
                cur.execute(
                    "INSERT INTO inventory(branch_code,denomination_id,total_qty,"
                    "locked_qty) VALUES (?,?,?,0)",
                    (branch, did, qty),
                )
    # 汇率：昨天 v1（已 superseded），今天 v1 active
    for ccy, offsets in SEED_RATES.items():
        for idx, offset in enumerate(offsets):
            day = (now + timedelta(days=offset)).date().isoformat()
            eff = (now + timedelta(days=offset)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            status = "active" if offset == max(offsets) else "superseded"
            cur.execute(
                "INSERT INTO fx_rates(currency_code,rate_date,version,rate,"
                "effective_at,status,created_by) VALUES (?,?,?,?,?,?,?)",
                (ccy, day, 1, RATE_VALUES[ccy][idx],
                 eff.isoformat(timespec="seconds"), status, "wang_wu"),
            )
    cur.execute(
        "INSERT INTO audit_events(ts,actor,entity,ref_code,action,detail)"
        " VALUES (?,?,?,?,?,?)",
        (now.isoformat(timespec="seconds"), "system", "platform", "-",
         "seed", "写入虚构演示数据"),
    )
