"""柜面核验、现钞交付与差额复核。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .adapters import CounterfeitSuspicion, cash_adapter
from .audit import audit, immediate_tx, now_str
from .errors import InvalidState, PermissionDenied
from .inventory import _apply_deliver


def _teller(conn, username: str) -> sqlite3.Row:
    t = conn.execute("SELECT * FROM tellers WHERE username=?", (username,)).fetchone()
    if t is None:
        raise PermissionDenied(f"柜员 {username} 不存在")
    return t


def _appointment(conn, code: str) -> sqlite3.Row:
    ap = conn.execute("SELECT * FROM appointments WHERE code=?", (code,)).fetchone()
    if ap is None:
        raise InvalidState(f"预约 {code} 不存在")
    return ap


def _planned(conn, ap_id: int) -> dict[int, int]:
    return {r["denomination_id"]: r["planned_qty"] for r in conn.execute(
        "SELECT denomination_id,planned_qty FROM appointment_items"
        " WHERE appointment_id=?", (ap_id,))}


# ------------------------------------------------------------------ 柜面核验
def counter_verify(conn, code: str, username: str, counter_branch: str) -> dict:
    """柜员在柜面核验客户预约：身份、状态、有效期、网点、锁库一致。"""
    t = _teller(conn, username)
    if t["role"] != "teller":
        raise PermissionDenied("柜面核验须由柜员办理")
    with immediate_tx(conn):
        ap = _appointment(conn, code)
        problems: list[str] = []
        if ap["branch_code"] != counter_branch:
            problems.append(
                f"预约取钞网点为 {ap['branch_code']}，与当前柜面 {counter_branch} 不符")
        if ap["status"] != "CONFIRMED":
            problems.append(f"预约状态为 {ap['status']}，非已确认状态")
        if datetime.fromisoformat(ap["valid_until"]) < datetime.now():
            problems.append("预约已超过取钞有效期")
        if not ap["fx_rate_id"]:
            problems.append("缺少确认时汇率快照")
        # 锁库一致性：按本预约的锁定流水净额核对（多预约共享同一券别锁定）
        locked_rows = conn.execute(
            "SELECT ai.denomination_id,ai.planned_qty,"
            " COALESCE((SELECT SUM(se.delta_locked) FROM stock_events se"
            "   WHERE se.ref_code=a.code AND se.denomination_id=ai.denomination_id),0)"
            " AS ap_locked"
            " FROM appointment_items ai JOIN appointments a ON a.id=ai.appointment_id"
            " WHERE ai.appointment_id=?",
            (ap["id"],),
        ).fetchall()
        for r in locked_rows:
            if r["ap_locked"] < r["planned_qty"]:
                problems.append(
                    f"券别 {r['denomination_id']} 锁定库存不足"
                    f"（需 {r['planned_qty']}，本预约锁定 {r['ap_locked']}）")
        audit(conn, username, "delivery", "counter_verify", code,
              {"ok": not problems, "problems": problems})
    return {"code": code, "passed": not problems, "problems": problems,
            "status": ap["status"], "branch": ap["branch_code"],
            "valid_until": ap["valid_until"]}


# ------------------------------------------------------------------ 现钞交付
def deliver(
    conn,
    code: str,
    teller: str,
    counted: dict[int, int] | None = None,
    counterfeit: dict[int, list[str]] | None = None,
    note: str | None = None,
) -> dict:
    """柜员点验并交付。

    - 幂等：同一预约重复调用直接返回既有交付单，不重复出库；
    - counted：实点券别数量（缺省=按预约点足）；缺/多款登记差额；
    - counterfeit：券别 -> 可疑冠字号，抛假钞疑点并登记（交主管处置，不出库）；
    - 金额（人民币/外币）取确认时快照，柜员不可修改。
    """
    user = _teller(conn, teller)
    if user["role"] != "teller":
        raise PermissionDenied("现钞交付须由柜员办理，主管仅复核")

    with immediate_tx(conn):
        ap = _appointment(conn, code)
        existing = conn.execute(
            "SELECT * FROM deliveries WHERE appointment_id=?", (ap["id"],)
        ).fetchone()
        if existing is not None:
            # 幂等重放：返回已有单据，明确标注 replay，绝不二次出库
            return _delivery_bundle(conn, existing, replay=True)

        if ap["status"] != "CONFIRMED":
            raise InvalidState(f"预约状态 {ap['status']}，不可交付")
        if not ap["fx_rate_id"]:
            raise InvalidState("缺少汇率快照，不能交付")

        planned = _planned(conn, ap["id"])
        counted = counted or {}
        # 补齐未提供的券别：视为点足
        full_count = {did: int(counted.get(did, q)) for did, q in planned.items()}
        counterfeit = counterfeit or {}

        # 假钞疑点：先登记，交主管复核处置，本次不出库
        if counterfeit:
            for did, serials in counterfeit.items():
                conn.execute(
                    "INSERT INTO diff_records(appointment_id,denomination_id,"
                    "planned_qty,counted_qty,kind,detail,created_at)"
                    " VALUES (?,?,?,?, 'counterfeit',?,?)",
                    (ap["id"], did, planned.get(did), full_count.get(did),
                     "可疑冠字号: " + ",".join(serials), now_str()),
                )
            session_id = f"CS-{ap['code']}-{now_str()}"
            cur = conn.execute(
                "INSERT INTO deliveries(appointment_id,teller,status,"
                "cash_session_id,counterfeit_qty,note,created_at)"
                " VALUES (?,?,'counting',?,?,?,?)",
                (ap["id"], teller, session_id,
                 sum(len(s) for s in counterfeit.values()),
                 note or "发现假钞疑点，暂扣待主管处置", now_str()),
            )
            audit(conn, teller, "delivery", "counterfeit_flag", code,
                  {"counterfeit": {str(k): v for k, v in counterfeit.items()}})
            row = conn.execute(
                "SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return _delivery_bundle(conn, row, counterfeit=True)

        # 模拟点验：建立会话，实点数量以入参为准（适配器还可被直接预置）
        session_id = f"CS-{ap['code']}"
        preset = {did: {"qty": q} for did, q in full_count.items()}
        cash_adapter.open_session(session_id, preset)
        counted_rows = []
        for did, expected in planned.items():
            res = cash_adapter.count(session_id, did, expected)
            counted_rows.append((did, expected, res["qty"]))

        # 出库（同事务），按实点扣总量、按预约解锁
        lines = [{"denomination_id": did, "planned_qty": p, "counted_qty": c}
                 for did, p, c in counted_rows]
        stock_result = _apply_deliver(conn, ap["branch_code"], lines, code, teller)

        cur = conn.execute(
            "INSERT INTO deliveries(appointment_id,teller,status,"
            "cash_session_id,note,created_at)"
            " VALUES (?,?,'pending_review',?,?,?)",
            (ap["id"], teller, session_id, note, now_str()),
        )
        delivery_id = cur.lastrowid
        has_diff = False
        for did, p, c in counted_rows:
            diff = c - p
            conn.execute(
                "INSERT INTO delivery_lines(delivery_id,denomination_id,"
                "planned_qty,counted_qty,diff) VALUES (?,?,?,?,?)",
                (delivery_id, did, p, c, diff),
            )
            if diff != 0:
                has_diff = True
                kind = "short" if diff < 0 else "over"
                conn.execute(
                    "INSERT INTO diff_records(appointment_id,delivery_id,"
                    "denomination_id,planned_qty,counted_qty,kind,detail,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (ap["id"], delivery_id, did, p, c, kind,
                     f"实点 {c} 张，预约 {p} 张，差额 {diff:+d}", now_str()),
                )

        # 无差额 → 自动完成；有差额 → 保持待复核，预约状态 REVIEWED（已交付待处置）
        conn.execute(
            "UPDATE appointments SET status=? WHERE id=?",
            ("REVIEWED" if has_diff else "DELIVERED", ap["id"]),
        )
        audit(conn, teller, "delivery", "deliver", code,
              {"has_diff": has_diff, "lines": lines})
        row = conn.execute(
            "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
    return _delivery_bundle(conn, row)


def _delivery_bundle(conn, row: sqlite3.Row, replay: bool = False,
                     counterfeit: bool = False) -> dict:
    lines = [dict(r) for r in conn.execute(
        "SELECT dl.*, d.face_value, d.currency_code FROM delivery_lines dl"
        " JOIN denominations d ON d.id=dl.denomination_id"
        " WHERE dl.delivery_id=? ORDER BY d.seq", (row["id"],))]
    ap = conn.execute("SELECT * FROM appointments WHERE id=?",
                      (row["appointment_id"],)).fetchone()
    diffs = [dict(r) for r in conn.execute(
        "SELECT * FROM diff_records WHERE appointment_id=? ORDER BY id",
        (row["appointment_id"],))]
    return {"delivery": dict(row), "appointment": dict(ap), "lines": lines,
            "diffs": diffs, "replay": replay, "counterfeit": counterfeit}


# ------------------------------------------------------------------ 差额复核
def list_pending(conn) -> list[dict]:
    """待复核清单：有差额/假钞的交付与差额记录。"""
    rows = conn.execute(
        "SELECT dr.*, c.name AS customer_name, a.code, d.face_value,"
        " d.currency_code, dl.teller AS delivery_teller"
        " FROM diff_records dr"
        " JOIN appointments a ON a.id=dr.appointment_id"
        " JOIN customers c ON c.id=a.customer_id"
        " LEFT JOIN denominations d ON d.id=dr.denomination_id"
        " LEFT JOIN deliveries dl ON dl.id=dr.delivery_id"
        " WHERE dr.status='open' ORDER BY dr.id"
    ).fetchall()
    return [dict(r) for r in rows]


def review_delivery(conn, code: str, supervisor: str,
                    reviewed: dict[int, int] | None = None,
                    resolution: str = "") -> dict:
    """主管复核交付：逐条确认实点数量（预约/实点/复核三栏对照）。

    - 仅 supervisor 可复核；柜员不能复核自己或他人的交付；
    - 柜员不能修改已复核金额/数量：复核完成后单据冻结；
    - reviewed：券别 -> 复核张数；与实点不符再次登记差额。
    """
    user = _teller(conn, supervisor)
    if user["role"] != "supervisor":
        raise PermissionDenied("差额复核须由主管完成，柜员无权复核")

    with immediate_tx(conn):
        ap = _appointment(conn, code)
        drow = conn.execute(
            "SELECT * FROM deliveries WHERE appointment_id=?", (ap["id"],)
        ).fetchone()
        if drow is None:
            raise InvalidState("该预约尚无交付记录")
        if drow["status"] == "reviewed":
            # 已复核单据冻结：再次请求直接返回，禁止修改
            bundle = _delivery_bundle(conn, drow)
            bundle["frozen"] = True
            return bundle
        if drow["status"] == "counting":
            raise InvalidState("交付单点验中（可能存在假钞疑点），请先处置后再复核")

        reviewed = reviewed or {}
        lines = conn.execute(
            "SELECT * FROM delivery_lines WHERE delivery_id=?", (drow["id"],)
        ).fetchall()
        for ln in lines:
            rev_qty = int(reviewed.get(ln["denomination_id"], ln["counted_qty"]))
            conn.execute(
                "UPDATE delivery_lines SET reviewed_qty=?, diff=? WHERE id=?",
                (rev_qty, rev_qty - ln["planned_qty"], ln["id"]),
            )
            if rev_qty != ln["counted_qty"]:
                conn.execute(
                    "INSERT INTO diff_records(appointment_id,delivery_id,"
                    "denomination_id,planned_qty,counted_qty,reviewed_qty,"
                    "kind,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (ap["id"], drow["id"], ln["denomination_id"],
                     ln["planned_qty"], ln["counted_qty"], rev_qty,
                     "short" if rev_qty < ln["planned_qty"] else "over",
                     f"主管复核 {rev_qty} 张，与柜员实点 {ln['counted_qty']} 张不符",
                     now_str()),
                )

        # 关闭本交付关联的待处置差额
        conn.execute(
            "UPDATE diff_records SET status='resolved', resolved_at=?, resolved_by=?,"
            " resolution=? WHERE delivery_id=? AND status='open'",
            (now_str(), supervisor, resolution or "主管复核确认", drow["id"]),
        )
        # 假钞类差额按 resolution 文本决定是否一并关闭
        conn.execute(
            "UPDATE diff_records SET status='resolved', resolved_at=?, resolved_by=?,"
            " resolution=? WHERE appointment_id=? AND kind='counterfeit'"
            " AND status='open' AND ? <> ''",
            (now_str(), supervisor,
             resolution or "假钞疑点按规程处置", ap["id"], resolution or ""),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) FROM diff_records WHERE appointment_id=?"
            " AND status='open'", (ap["id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE deliveries SET status='reviewed', reviewer=?, reviewed_at=?",
            (supervisor, now_str()),
        )
        # 复核完成即冻结金额；预约在无遗留差额时置 DELIVERED
        if remaining == 0:
            conn.execute("UPDATE appointments SET status='DELIVERED' WHERE id=?",
                         (ap["id"],))
        audit(conn, supervisor, "delivery", "review", code,
              {"reviewed": {str(k): v for k, v in (reviewed or {}).items()},
               "resolution": resolution})
        row = conn.execute("SELECT * FROM deliveries WHERE id=?",
                           (drow["id"],)).fetchone()
    return _delivery_bundle(conn, row, replay=False)


def resolve_counterfeit(conn, code: str, supervisor: str,
                        resolution: str, release_stock: bool = True) -> dict:
    """主管处置假钞疑点：登记处置结果。

    默认释放该预约的锁定库存（假钞不得出库，交易取消）；
    预约状态置 CANCELLED 并留痕。
    """
    user = _teller(conn, supervisor)
    if user["role"] != "supervisor":
        raise PermissionDenied("假钞疑点仅主管可处置")
    with immediate_tx(conn):
        ap = _appointment(conn, code)
        open_cf = conn.execute(
            "SELECT * FROM diff_records WHERE appointment_id=?"
            " AND kind='counterfeit' AND status='open'", (ap["id"],),
        ).fetchall()
        if not open_cf:
            raise InvalidState("该预约没有待处置的假钞疑点")
        conn.execute(
            "UPDATE diff_records SET status='resolved', resolved_at=?, resolved_by=?,"
            " resolution=? WHERE appointment_id=? AND kind='counterfeit'",
            (now_str(), supervisor, resolution, ap["id"]),
        )
        drow = conn.execute(
            "SELECT * FROM deliveries WHERE appointment_id=? ORDER BY id DESC LIMIT 1",
            (ap["id"],),
        ).fetchone()
        if drow is not None:
            conn.execute("UPDATE deliveries SET status='reviewed', reviewer=?,"
                         " reviewed_at=?, note=? WHERE id=?",
                         (supervisor, now_str(), resolution, drow["id"]))
        if release_stock and ap["status"] == "CONFIRMED":
            from .appointments import _cancel
            _cancel(conn, ap, "teller", f"假钞疑点处置，交易取消：{resolution}",
                    supervisor)
        audit(conn, supervisor, "delivery", "counterfeit_resolve", code,
              {"resolution": resolution})
    return _delivery_bundle(
        conn, conn.execute("SELECT * FROM deliveries WHERE appointment_id=?",
                           (ap["id"],)).fetchone())
