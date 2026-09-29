"""审计与事务辅助。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


def audit(
    conn: sqlite3.Connection,
    actor: str,
    entity: str,
    action: str,
    ref_code: str | None = None,
    detail: dict | str | None = None,
) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    conn.execute(
        "INSERT INTO audit_events(ts,actor,entity,ref_code,action,detail)"
        " VALUES (?,?,?,?,?,?)",
        (now_str(), actor, entity, ref_code, action, detail),
    )


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE：拿写锁后再读库存，保证“检查-扣减”原子。

    SQLite 同一时刻只允许一个写事务，配合 busy_timeout，
    两名柜员并发占用同一券别时被天然串行化，后到者读到的是已提交数据。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
