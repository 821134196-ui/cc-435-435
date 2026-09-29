"""客户预约：创建锁库、确认汇率快照、券别替换、取消与到期释放。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..config import DEFAULT_VALID_HOURS, MAX_VALID_HOURS
from .audit import audit, immediate_tx, now_str
from .errors import InvalidState
from .fx import active_rate
from .inventory import check_availability
from .moneyutil import D, money

OPEN_STATUSES = ("RESERVED", "CONFIRMED", "REPLACE_PENDING", "REVIEWED")


# ------------------------------------------------------------------ 查询
def _row_by_code(conn, code) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM appointments WHERE code=?", (code,)).fetchone()
    if row is None:
        raise InvalidState(f"预约 {code} 不存在")
    return row


def get_detail(conn, code: str) -> dict:
    ap = _row_by_code(conn, code)
    cust = conn.execute(
        "SELECT * FROM customers WHERE id=?", (ap["customer_id"],)
    ).fetchone()
    branch = conn.execute(
        "SELECT * FROM branches WHERE code=?", (ap["branch_code"],)
    ).fetchone()
    rate = None
    if ap["fx_rate_id"]:
        rate = conn.execute(
            "SELECT * FROM fx_rates WHERE id=?", (ap["fx_rate_id"],)
        ).fetchone()
    items = conn.execute(
        "SELECT ai.denomination_id,ai.planned_qty,d.face_value,d.currency_code,d.seq"
        " FROM appointment_items ai JOIN denominations d ON d.id=ai.denomination_id"
        " WHERE ai.appointment_id=? ORDER BY d.seq",
        (ap["id"],),
    ).fetchall()
    history = conn.execute(
        "SELECT h.*, d.face_value FROM appointment_item_history h"
        " JOIN denominations d ON d.id=h.denomination_id"
        " WHERE h.appointment_id=? ORDER BY h.id",
        (ap["id"],),
    ).fetchall()
    cancels = conn.execute(
        "SELECT * FROM cancellation_records WHERE appointment_id=? ORDER BY id",
        (ap["id"],),
    ).fetchall()
    fxevents = conn.execute(
        "SELECT e.*, r.rate, r.rate_date, r.version FROM fx_events e"
        " LEFT JOIN fx_rates r ON r.id=e.fx_rate_id"
        " WHERE e.appointment_id=? ORDER BY e.id",
        (ap["id"],),
    ).fetchall()
    delivery = conn.execute(
        "SELECT * FROM deliveries WHERE appointment_id=?", (ap["id"],)
    ).fetchone()
    delivery_lines = []
    if delivery is not None:
        delivery_lines = [dict(r) for r in conn.execute(
            "SELECT dl.denomination_id,d.face_value,dl.planned_qty,"
            "dl.counted_qty,dl.reviewed_qty,dl.diff"
            " FROM delivery_lines dl JOIN denominations d ON d.id=dl.denomination_id"
            " WHERE dl.delivery_id=? ORDER BY d.seq", (delivery["id"],))]
    return {
        "appointment": dict(ap),
        "customer": dict(cust),
        "branch": dict(branch),
        "fx_rate": dict(rate) if rate else None,
        "items": [dict(i) for i in items],
        "history": [dict(h) for h in history],
        "cancellations": [dict(c) for c in cancels],
        "fx_events": [dict(e) for e in fxevents],
        "delivery": dict(delivery) if delivery else None,
        "delivery_lines": delivery_lines,
    }


def list_appointments(conn, statuses=None, date_field="reserved_date",
                      on_date=None) -> list[dict]:
    sql = (
        "SELECT a.*, c.name AS customer_name, c.id_type, c.id_no, b.name AS branch_name"
        " FROM appointments a JOIN customers c ON c.id=a.customer_id"
        " JOIN branches b ON b.code=a.branch_code"
    )
    where, params = [], []
    if statuses:
        where.append(f"a.status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if on_date:
        where.append(f"a.{date_field}=?")
        params.append(on_date)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.id"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["items"] = [dict(x) for x in conn.execute(
            "SELECT ai.denomination_id,ai.planned_qty,d.face_value"
            " FROM appointment_items ai JOIN denominations d ON d.id=ai.denomination_id"
            " WHERE ai.appointment_id=? ORDER BY d.seq", (r["id"],))]
        out.append(d)
    return out


def calendar(conn) -> dict:
    """预约日历：按预约日分组，附带状态计数与到期标记。"""
    now = datetime.now()
    rows = list_appointments(conn)
    days: dict[str, list] = {}
    for a in rows:
        a["expiring_soon"] = (
            a["status"] in ("RESERVED", "REPLACE_PENDING")
            and datetime.fromisoformat(a["valid_until"]) <= now + timedelta(hours=12)
        )
        days.setdefault(a["reserved_date"], []).append(a)
    return {"days": days}


# ------------------------------------------------------------------ 创建
def _next_code(conn, when: datetime) -> str:
    """写事务内发号：daily_seq 行锁串行化，杜绝并发票号碰撞。"""
    day = when.date().isoformat()
    n = conn.execute(
        "INSERT INTO daily_seq(seq_date,n) VALUES (?,1)"
        " ON CONFLICT(seq_date) DO UPDATE SET n=n+1 RETURNING n",
        (day,),
    ).fetchone()[0]
    return f"AP{when.strftime('%y%m%d')}{n:04d}"


def create_reservation(
    conn,
    actor: str,
    customer: dict,
    currency: str,
    branch_code: str,
    items: dict[int, int],            # denomination_id -> qty
    valid_hours: int = DEFAULT_VALID_HOURS,
    note: str | None = None,
) -> dict:
    """新建预约并立即锁定可用库存。

    items 会按币种校验；库存不足整体失败（不产生任何锁定）。
    """
    if not items or any(q <= 0 for q in items.values()):
        raise InvalidState("券别数量必须为正整数")
    valid_hours = min(max(int(valid_hours), 1), MAX_VALID_HOURS)
    when = datetime.now()
    valid_until = when + timedelta(hours=valid_hours)

    # 校验币种/券别归属
    ccy = conn.execute(
        "SELECT * FROM currencies WHERE code=? AND active=1", (currency,)
    ).fetchone()
    if ccy is None:
        raise InvalidState(f"币种 {currency} 不可用")
    for did in items:
        d = conn.execute(
            "SELECT currency_code FROM denominations WHERE id=? AND active=1", (did,)
        ).fetchone()
        if d is None or d["currency_code"] != currency:
            raise InvalidState(f"券别 {did} 不属于币种 {currency}")
    if not conn.execute("SELECT 1 FROM branches WHERE code=?",
                        (branch_code,)).fetchone():
        raise InvalidState(f"网点 {branch_code} 不存在")

    # 预检（无锁）；持写锁后再二次校验
    check_availability(conn, branch_code, items)

    with immediate_tx(conn):
        code = _next_code(conn, when)
        cur = conn.execute(
            "INSERT INTO customers(name,id_type,id_no,phone) VALUES (?,?,?,?)",
            (customer["name"], customer["id_type"], customer["id_no"],
             customer.get("phone", "-")),
        )
        customer_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO appointments(code,customer_id,currency_code,branch_code,"
            "status,reserved_date,created_at,created_by,valid_until,plan_version,note)"
            " VALUES (?,?,?,?, 'RESERVED',?,?,?,?,1,?)",
            (code, customer_id, currency, branch_code,
             when.date().isoformat(), when.isoformat(timespec="seconds"),
             actor, valid_until.isoformat(timespec="seconds"), note),
        )
        ap_id = cur.lastrowid
        # 锁库与预约同事务：任一券别锁定失败则整笔回滚
        for did, qty in items.items():
            cur = conn.execute(
                "UPDATE inventory SET locked_qty = locked_qty + ?"
                " WHERE branch_code=? AND denomination_id=?"
                " AND total_qty - locked_qty >= ?",
                (qty, branch_code, did, qty),
            )
            if cur.rowcount == 0:
                from .errors import ConcurrentLock
                raise ConcurrentLock(f"券别 {did} 库存刚被占用，预约失败")
            conn.execute(
                "INSERT INTO stock_events(ts,branch_code,denomination_id,"
                "delta_locked,delta_total,ref_code,kind,actor)"
                " VALUES (?,?,?,?,0,?,'lock',?)",
                (now_str(), branch_code, did, qty, code, actor),
            )
            conn.execute(
                "INSERT INTO appointment_items(appointment_id,denomination_id,"
                "planned_qty,plan_version) VALUES (?,?,?,1)",
                (ap_id, did, qty),
            )
            conn.execute(
                "INSERT INTO appointment_item_history(appointment_id,"
                "denomination_id,qty,plan_version,change_kind,created_at,actor)"
                " VALUES (?,?,?,1,'original',?,?)",
                (ap_id, did, qty, now_str(), actor),
            )
        audit(conn, actor, "appointment", "create", code,
              {"customer_id": customer_id, "currency": currency,
               "branch": branch_code, "items": {str(k): v for k, v in items.items()},
               "valid_until": valid_until.isoformat(timespec="seconds")})
    return get_detail(conn, code)


# ------------------------------------------------------------------ 确认
def _snapshot_rate(conn, ap: sqlite3.Row, event_type: str, detail: str) -> sqlite3.Row:
    today = date.today().isoformat()
    rate = active_rate(conn, ap["currency_code"], today)
    conn.execute(
        "INSERT INTO fx_events(appointment_id,event_type,fx_rate_id,detail,created_at)"
        " VALUES (?,?,?,?,?)",
        (ap["id"], event_type, rate["id"], detail, now_str()),
    )
    if event_type == "cross_day":
        # 跨日同时留一条 confirm 轨迹，便于审计完整链路
        conn.execute(
            "INSERT INTO fx_events(appointment_id,event_type,fx_rate_id,detail,created_at)"
            " VALUES (?,?,?,?,?)",
            (ap["id"], "confirm", rate["id"],
             f"按确认日 {today} 汇率确认", now_str()),
        )
    return rate


def _compute_amounts(items_rows, rate_value: Decimal) -> tuple[Decimal, Decimal]:
    total_foreign = Decimal("0")
    for it in items_rows:
        total_foreign += D(it["face_value"]) * it["planned_qty"]
    return money(total_foreign), money(total_foreign * rate_value)


def confirm(conn, code: str, actor: str) -> dict:
    """客户到场确认：按确认当日有效汇率版本快照并计算金额。

    预约日与确认日不同 → 记录“汇率跨日”，以确认日版本为准。
    """
    with immediate_tx(conn):
        ap = _row_by_code(conn, code)
        if ap["status"] not in ("RESERVED",):
            raise InvalidState(f"预约状态 {ap['status']} 不可确认")
        today = date.today().isoformat()
        cross_day = ap["reserved_date"] != today
        rate = _snapshot_rate(
            conn, ap,
            "cross_day" if cross_day else "confirm",
            f"预约日 {ap['reserved_date']} → 确认日 {today}" if cross_day
            else f"确认日 {today}",
        )
        items = conn.execute(
            "SELECT ai.planned_qty,d.face_value FROM appointment_items ai"
            " JOIN denominations d ON d.id=ai.denomination_id"
            " WHERE ai.appointment_id=?",
            (ap["id"],),
        ).fetchall()
        amt_f, amt_cny = _compute_amounts(items, D(rate["rate"]))
        conn.execute(
            "UPDATE appointments SET status='CONFIRMED', confirmed_date=?,"
            " confirmed_at=?, fx_rate_id=?, amount_foreign=?, amount_cny=?"
            " WHERE id=?",
            (today, now_str(), rate["id"], str(amt_f), str(amt_cny), ap["id"]),
        )
        audit(conn, actor, "appointment", "confirm", code,
              {"fx_rate_id": rate["id"], "rate": rate["rate"],
               "amount_foreign": str(amt_f), "amount_cny": str(amt_cny),
               "cross_day": cross_day})
    return get_detail(conn, code)


# ------------------------------------------------------------------ 券别替换
def propose_replacement(
    conn, code: str, actor: str, new_items: dict[int, int], reason: str
) -> dict:
    """柜员提出替换方案：暂不动库存，等客户重新确认后再换锁。"""
    if not new_items or any(q <= 0 for q in new_items.values()):
        raise InvalidState("替换券别数量必须为正整数")
    with immediate_tx(conn):
        ap = _row_by_code(conn, code)
        if ap["status"] != "CONFIRMED":
            raise InvalidState("仅已确认（CONFIRMED）预约可提出券别替换")
        cur_items = {r["denomination_id"]: r["planned_qty"] for r in conn.execute(
            "SELECT denomination_id,planned_qty FROM appointment_items"
            " WHERE appointment_id=?", (ap["id"],))}
        # 替换方案可换成同币种的任意有效券别
        for did in new_items:
            d = conn.execute(
                "SELECT currency_code FROM denominations WHERE id=? AND active=1",
                (did,),
            ).fetchone()
            if d is None or d["currency_code"] != ap["currency_code"]:
                raise InvalidState(f"券别 {did} 不属于币种 {ap['currency_code']}")
        if set(new_items) == set(cur_items) and all(
            new_items[k] == cur_items[k] for k in new_items
        ):
            raise InvalidState("替换方案与原方案完全相同，无需替换")
        # 注意：提出阶段不锁库、不做硬性库存拦截；客户重新确认时
        # 在换锁事务中强校验，避免“提案期”库存变化造成误判。
        old_plan = [[r["denomination_id"], r["planned_qty"]] for r in conn.execute(
            "SELECT denomination_id,planned_qty FROM appointment_items"
            " WHERE appointment_id=? ORDER BY denomination_id", (ap["id"],))]
        conn.execute(
            "UPDATE appointments SET status='REPLACE_PENDING' WHERE id=?",
            (ap["id"],),
        )
        conn.execute(
            "INSERT INTO replacements(appointment_id,old_plan,new_plan,reason,"
            "status,created_by,created_at) VALUES (?,?,?,?,'pending',?,?)",
            (ap["id"], json.dumps(old_plan),
             json.dumps([[k, v] for k, v in new_items.items()]),
             reason, actor, now_str()),
        )
        audit(conn, actor, "appointment", "propose_replace", code,
              {"new_items": {str(k): v for k, v in new_items.items()},
               "reason": reason})
    return get_detail(conn, code)


def decide_replacement(conn, code: str, accept: bool, actor: str) -> dict:
    """客户对替换方案重新确认。接受 → 原子换锁并重算金额；拒绝 → 维持原方案。"""
    with immediate_tx(conn):
        ap = _row_by_code(conn, code)
        if ap["status"] != "REPLACE_PENDING":
            raise InvalidState("该预约没有待确认的替换方案")
        rep = conn.execute(
            "SELECT * FROM replacements WHERE appointment_id=? AND status='pending'"
            " ORDER BY id DESC LIMIT 1", (ap["id"],),
        ).fetchone()
        new_items = {int(k): int(v) for k, v in json.loads(rep["new_plan"])}
        if accept:
            # 新方案需增加的锁定（含原券别数量变化）
            old_items = {r["denomination_id"]: r["planned_qty"] for r in conn.execute(
                "SELECT denomination_id,planned_qty FROM appointment_items"
                " WHERE appointment_id=?", (ap["id"],))}
            need_lock = {}
            for did, qty in new_items.items():
                delta = qty - old_items.get(did, 0)
                if delta > 0:
                    need_lock[did] = delta
            check_availability(conn, ap["branch_code"], need_lock)
            # 换锁：先释放旧锁，再锁新方案（同一写事务，库存状态连续）
            for did, qty in old_items.items():
                conn.execute(
                    "UPDATE inventory SET locked_qty=locked_qty-?"
                    " WHERE branch_code=? AND denomination_id=? AND locked_qty>=?",
                    (qty, ap["branch_code"], did, qty),
                )
                conn.execute(
                    "INSERT INTO stock_events(ts,branch_code,denomination_id,"
                    "delta_locked,delta_total,ref_code,kind,actor)"
                    " VALUES (?,?,?,?,0,?,'release',?)",
                    (now_str(), ap["branch_code"], did, -qty, code, actor),
                )
            for did, qty in new_items.items():
                cur = conn.execute(
                    "UPDATE inventory SET locked_qty=locked_qty+?"
                    " WHERE branch_code=? AND denomination_id=?"
                    " AND total_qty-locked_qty>=?",
                    (qty, ap["branch_code"], did, qty),
                )
                if cur.rowcount == 0:
                    from .errors import ConcurrentLock
                    raise ConcurrentLock(f"券别 {did} 换锁失败，库存被占用")
                conn.execute(
                    "INSERT INTO stock_events(ts,branch_code,denomination_id,"
                    "delta_locked,delta_total,ref_code,kind,actor)"
                    " VALUES (?,?,?,?,0,?,'lock',?)",
                    (now_str(), ap["branch_code"], did, qty, code, actor),
                )
            conn.execute(
                "DELETE FROM appointment_items WHERE appointment_id=?", (ap["id"],)
            )
            new_version = ap["plan_version"] + 1
            for did, qty in new_items.items():
                conn.execute(
                    "INSERT INTO appointment_items(appointment_id,denomination_id,"
                    "planned_qty,plan_version) VALUES (?,?,?,?)",
                    (ap["id"], did, qty, new_version),
                )
                conn.execute(
                    "INSERT INTO appointment_item_history(appointment_id,"
                    "denomination_id,qty,plan_version,change_kind,created_at,actor)"
                    " VALUES (?,?,?,?,'reconfirmed',?,?)",
                    (ap["id"], did, qty, new_version, now_str(), actor),
                )
            # 重新快照确认日汇率（可能跨日）
            rate = _snapshot_rate(
                conn, ap, "reconfirm",
                f"客户重新确认替换方案 v{new_version}，按当日版本重算")
            items = conn.execute(
                "SELECT ai.planned_qty,d.face_value FROM appointment_items ai"
                " JOIN denominations d ON d.id=ai.denomination_id"
                " WHERE ai.appointment_id=?", (ap["id"],),
            ).fetchall()
            amt_f, amt_cny = _compute_amounts(items, D(rate["rate"]))
            conn.execute(
                "UPDATE appointments SET status='CONFIRMED', plan_version=?,"
                " fx_rate_id=?, amount_foreign=?, amount_cny=? WHERE id=?",
                (new_version, rate["id"], str(amt_f), str(amt_cny), ap["id"]),
            )
            conn.execute(
                "UPDATE replacements SET status='confirmed', decided_at=? WHERE id=?",
                (now_str(), rep["id"]),
            )
            conn.execute(
                "INSERT INTO diff_records(appointment_id,denomination_id,"
                "planned_qty,counted_qty,kind,status,detail,created_at,"
                "resolved_at,resolved_by,resolution)"
                " VALUES (?,?,?,?,'substitution','resolved',?,?,?,?,?)",
                (ap["id"], None, None, None,
                 f"客户已确认替换方案 v{new_version}", now_str(),
                 now_str(), actor, "客户重新确认，库存已换锁"),
            )
            audit(conn, actor, "appointment", "replace_confirmed", code,
                  {"plan_version": new_version, "fx_rate_id": rate["id"]})
        else:
            conn.execute(
                "UPDATE appointments SET status='CONFIRMED' WHERE id=?", (ap["id"],)
            )
            conn.execute(
                "UPDATE replacements SET status='rejected', decided_at=? WHERE id=?",
                (now_str(), rep["id"]),
            )
            audit(conn, actor, "appointment", "replace_rejected", code)
    return get_detail(conn, code)


# ------------------------------------------------------------------ 取消/过期
def _cancel(conn, ap: sqlite3.Row, kind: str, reason: str, actor: str) -> None:
    items = {r["denomination_id"]: r["planned_qty"] for r in conn.execute(
        "SELECT denomination_id,planned_qty FROM appointment_items"
        " WHERE appointment_id=?", (ap["id"],))}
    for did, qty in items.items():
        cur = conn.execute(
            "UPDATE inventory SET locked_qty=locked_qty-?"
            " WHERE branch_code=? AND denomination_id=? AND locked_qty>=?",
            (qty, ap["branch_code"], did, qty),
        )
        if cur.rowcount == 0:
            raise InvalidState(f"券别 {did} 锁库数据异常，无法取消")
        conn.execute(
            "INSERT INTO stock_events(ts,branch_code,denomination_id,"
            "delta_locked,delta_total,ref_code,kind,actor)"
            " VALUES (?,?,?,?,0,?,'release',?)",
            (now_str(), ap["branch_code"], did, -qty, ap["code"], actor),
        )
    new_status = "EXPIRED" if kind == "expired" else "CANCELLED"
    conn.execute(
        "UPDATE appointments SET status=? WHERE id=?", (new_status, ap["id"])
    )
    conn.execute(
        "INSERT INTO cancellation_records(appointment_id,kind,reason,actor,created_at)"
        " VALUES (?,?,?,?,?)",
        (ap["id"], kind, reason, actor, now_str()),
    )
    audit(conn, actor, "appointment", kind, ap["code"], {"reason": reason})


def cancel(conn, code: str, actor: str, reason: str,
           kind: str = "customer") -> dict:
    if kind not in ("customer", "teller"):
        raise InvalidState("取消类型须为 customer/teller，到期请用 expire_due")
    with immediate_tx(conn):
        ap = _row_by_code(conn, code)
        if ap["status"] not in ("RESERVED", "CONFIRMED", "REPLACE_PENDING"):
            raise InvalidState(f"预约状态 {ap['status']} 不可取消")
        _cancel(conn, ap, kind, reason, actor)
    return get_detail(conn, code)


def expire_due(conn, actor: str = "system", now: datetime | None = None) -> list[str]:
    """扫描到期未到场预约，释放库存并登记 EXPIRED。返回处理的预约号。"""
    now = now or datetime.now()
    due = conn.execute(
        "SELECT * FROM appointments WHERE status IN ('RESERVED','REPLACE_PENDING')"
        " AND valid_until <= ?",
        (now.isoformat(timespec="seconds"),),
    ).fetchall()
    done = []
    for ap in due:
        with immediate_tx(conn):
            cur = conn.execute(
                "SELECT * FROM appointments WHERE id=? AND status IN"
                " ('RESERVED','REPLACE_PENDING')", (ap["id"],)).fetchone()
            if cur is None:
                continue
            _cancel(conn, cur, "expired",
                    f"超过取钞有效期 {cur['valid_until']} 未到场", actor)
            done.append(cur["code"])
    return done
