"""汇率版本发布与确认时快照。"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from .adapters import fx_adapter
from .audit import audit, immediate_tx, now_str
from .errors import InvalidState, PermissionDenied, RateUnavailable


def list_rates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.id,r.currency_code,c.name AS currency_name,r.rate_date,"
        "r.version,r.rate,r.effective_at,r.status,r.created_by"
        " FROM fx_rates r JOIN currencies c ON c.code=r.currency_code"
        " ORDER BY r.rate_date DESC, r.currency_code, r.version DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def publish_rate(
    conn: sqlite3.Connection,
    actor: str,
    actor_role: str,
    currency: str,
    rate: str | Decimal | None = None,
    rate_date: str | None = None,
) -> dict:
    """主管发布当日新版本；旧版本置为 superseded（历史不可改）。"""
    if actor_role != "supervisor":
        raise PermissionDenied("仅主管可发布汇率版本")
    day = rate_date or date.today().isoformat()
    rate = Decimal(str(rate if rate is not None
                       else fx_adapter.suggest_rate(currency)))
    with immediate_tx(conn):
        if not conn.execute(
            "SELECT 1 FROM currencies WHERE code=? AND active=1", (currency,)
        ).fetchone():
            raise InvalidState(f"币种 {currency} 不存在或已停用")
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM fx_rates"
            " WHERE currency_code=? AND rate_date=?",
            (currency, day),
        ).fetchone()
        version = row["v"] + 1
        conn.execute(
            "UPDATE fx_rates SET status='superseded'"
            " WHERE currency_code=? AND rate_date=? AND status='active'",
            (currency, day),
        )
        cur = conn.execute(
            "INSERT INTO fx_rates(currency_code,rate_date,version,rate,"
            "effective_at,status,created_by) VALUES (?,?,?,?,?,'active',?)",
            (currency, day, version, str(rate), now_str(), actor),
        )
        rate_id = cur.lastrowid
        audit(conn, actor, "fx_rate", "publish", f"{currency}-{day}-v{version}",
              {"rate_id": rate_id, "rate": str(rate)})
    return {"id": rate_id, "currency": currency, "rate_date": day,
            "version": version, "rate": str(rate)}


def active_rate(
    conn: sqlite3.Connection, currency: str, on_date: str | None = None
) -> sqlite3.Row:
    """取某日（默认今天）生效中的汇率；无当日牌价则跨日不可用。"""
    day = on_date or date.today().isoformat()
    row = conn.execute(
        "SELECT * FROM fx_rates WHERE currency_code=? AND rate_date=?"
        " AND status='active' ORDER BY version DESC LIMIT 1",
        (currency, day),
    ).fetchone()
    if row is None:
        raise RateUnavailable(f"{currency} 在 {day} 无有效汇率版本")
    return row
