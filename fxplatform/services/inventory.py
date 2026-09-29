"""网点库存：矩阵查询、锁定、释放与出库。"""
from __future__ import annotations

import sqlite3

from .audit import audit, immediate_tx, now_str
from .errors import ConcurrentLock, InventoryShortage


def matrix(conn: sqlite3.Connection) -> list[dict]:
    """币种 × 券别 × 网点 的紧凑矩阵数据。"""
    ccy_rows = conn.execute(
        "SELECT code,name,country FROM currencies WHERE active=1 ORDER BY code"
    ).fetchall()
    branches = conn.execute(
        "SELECT code,name FROM branches ORDER BY code"
    ).fetchall()
    out = []
    for ccy in ccy_rows:
        denoms = conn.execute(
            "SELECT id,face_value,seq FROM denominations WHERE currency_code=?"
            " AND active=1 ORDER BY seq",
            (ccy["code"],),
        ).fetchall()
        rows = []
        for d in denoms:
            cell = {"denomination_id": d["id"], "face_value": d["face_value"]}
            for b in branches:
                inv = conn.execute(
                    "SELECT total_qty,locked_qty FROM inventory"
                    " WHERE branch_code=? AND denomination_id=?",
                    (b["code"], d["id"]),
                ).fetchone()
                total = inv["total_qty"] if inv else 0
                locked = inv["locked_qty"] if inv else 0
                cell[b["code"]] = {
                    "total": total,
                    "locked": locked,
                    "available": total - locked,
                }
            rows.append(cell)
        out.append({
            "code": ccy["code"], "name": ccy["name"],
            "country": ccy["country"], "denominations": rows,
        })
    return {"currencies": out, "branches": [dict(b) for b in branches]}


def _denom_info(conn: sqlite3.Connection, denom_ids) -> dict[int, sqlite3.Row]:
    if not denom_ids:
        return {}
    qmarks = ",".join("?" * len(denom_ids))
    return {
        r["id"]: r
        for r in conn.execute(
            f"SELECT d.id,d.currency_code,d.face_value,c.name AS ccy_name"
            f" FROM denominations d JOIN currencies c ON c.code=d.currency_code"
            f" WHERE d.id IN ({qmarks})",
            tuple(denom_ids),
        )
    }


def check_availability(
    conn: sqlite3.Connection, branch_code: str, items: dict[int, int]
) -> None:
    """预检：可用库存不足直接抛 InventoryShortage（不持锁）。"""
    shortages = []
    info = _denom_info(conn, list(items))
    for did, need in items.items():
        inv = conn.execute(
            "SELECT total_qty,locked_qty FROM inventory"
            " WHERE branch_code=? AND denomination_id=?",
            (branch_code, did),
        ).fetchone()
        total = inv["total_qty"] if inv else 0
        available = total - (inv["locked_qty"] if inv else 0)
        if available < need:
            d = info.get(did)
            shortages.append({
                "denomination_id": did,
                "currency_code": d["currency_code"] if d else "?",
                "face_value": d["face_value"] if d else "?",
                "need": need, "available": available,
            })
    if shortages:
        raise InventoryShortage(shortages)


def lock_stock(
    conn: sqlite3.Connection,
    branch_code: str,
    items: dict[int, int],
    ref_code: str,
    actor: str,
) -> None:
    """提交预约时锁定可用库存（条件 UPDATE 兜底并发）。"""
    with immediate_tx(conn):
        # 持写锁后再做一次检查
        check_availability(conn, branch_code, items)
        for did, qty in items.items():
            cur = conn.execute(
                "UPDATE inventory SET locked_qty = locked_qty + ?"
                " WHERE branch_code=? AND denomination_id=?"
                " AND total_qty - locked_qty >= ?",
                (qty, branch_code, did, qty),
            )
            if cur.rowcount == 0:
                # 极端竞争：预检通过但条件更新失败
                raise ConcurrentLock(
                    f"券别 {did} 库存刚被其他柜员占用，锁定失败"
                )
            conn.execute(
                "INSERT INTO stock_events(ts,branch_code,denomination_id,"
                "delta_locked,delta_total,ref_code,kind,actor)"
                " VALUES (?,?,?,?,0,?,'lock',?)",
                (now_str(), branch_code, did, qty, ref_code, actor),
            )
        audit(conn, actor, "inventory", "lock", ref_code,
              {"branch": branch_code, "items": {str(k): v for k, v in items.items()}})


def release_stock(
    conn: sqlite3.Connection,
    branch_code: str,
    items: dict[int, int],
    ref_code: str,
    actor: str,
    kind: str = "release",
) -> None:
    """释放锁定库存（客户取消 / 到期 / 替换方案切换）。"""
    with immediate_tx(conn):
        for did, qty in items.items():
            cur = conn.execute(
                "UPDATE inventory SET locked_qty = locked_qty - ?"
                " WHERE branch_code=? AND denomination_id=? AND locked_qty >= ?",
                (qty, branch_code, did, qty),
            )
            if cur.rowcount == 0:
                raise ConcurrentLock(
                    f"券别 {did} 锁定数量不足，释放失败（数据异常）"
                )
            conn.execute(
                "INSERT INTO stock_events(ts,branch_code,denomination_id,"
                "delta_locked,delta_total,ref_code,kind,actor)"
                " VALUES (?,?,?,?,0,?,?,?)",
                (now_str(), branch_code, did, -qty, ref_code, kind, actor),
            )
        audit(conn, actor, "inventory", kind, ref_code,
              {"branch": branch_code, "items": {str(k): v for k, v in items.items()}})


def _apply_deliver(
    conn: sqlite3.Connection,
    branch_code: str,
    lines: list[dict],
    ref_code: str,
    actor: str,
) -> dict:
    """出库核心：调用方须已持有写事务。"""
    result = {}
    for ln in lines:
        did = ln["denomination_id"]
        planned, counted = ln["planned_qty"], ln["counted_qty"]
        cur = conn.execute(
            "UPDATE inventory SET total_qty = total_qty - ?,"
            " locked_qty = locked_qty - ?"
            " WHERE branch_code=? AND denomination_id=?"
            " AND total_qty >= ? AND locked_qty >= ?",
            (counted, planned, branch_code, did, counted, planned),
        )
        if cur.rowcount == 0:
            raise ConcurrentLock(f"券别 {did} 库存不足，无法出库")
        conn.execute(
            "INSERT INTO stock_events(ts,branch_code,denomination_id,"
            "delta_locked,delta_total,ref_code,kind,actor)"
            " VALUES (?,?,?,?,?,?,'deliver',?)",
            (now_str(), branch_code, did, -planned, -counted, ref_code, actor),
        )
        result[did] = {"planned": planned, "counted": counted,
                       "diff": counted - planned}
    return result


def deliver_out(
    conn: sqlite3.Connection,
    branch_code: str,
    lines: list[dict],
    ref_code: str,
    actor: str,
) -> dict:
    """现钞出库：按实点数量扣减总量，按预约数量解除锁定。

    短款（counted < planned）：差额锁定解除、重新可用（实物未出库）；
    多款（counted > planned）：以实点为准出库并登记差额，等待主管复核处置。
    """
    with immediate_tx(conn):
        result = _apply_deliver(conn, branch_code, lines, ref_code, actor)
        audit(conn, actor, "inventory", "deliver", ref_code,
              {"branch": branch_code, "lines": lines})
    return result
