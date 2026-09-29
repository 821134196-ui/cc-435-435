"""业务服务层：预约、锁库、汇率快照、替换、过期、交付与差额复核。

所有写操作在 SQLite 事务内完成；锁库采用条件更新，
两名柜员并发占用同一券别时由数据库串行化，保证只成功一笔。
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

from adapters import CashValidatorAdapter, FxAdapter

CNY_DECIMALS = 2
FX_DECIMALS = 4


class ServiceError(Exception):
    """业务规则错误。"""


class InsufficientStock(ServiceError):
    def __init__(self, branch: str, denom_id: int, need: int, free: int):
        super().__init__(
            f"库存不足：网点 {branch} 券别 #{denom_id} 需 {need} 张，可用仅 {free} 张"
        )
        self.branch, self.denom_id, self.need, self.free = branch, denom_id, need, free


class PermissionDenied(ServiceError):
    pass


# 需要库存锁定的状态
ACTIVE_STATUSES = ("held", "confirmed", "replacement_offered", "replacement_confirmed")


def _money(x: float, dp: int = 4) -> float:
    return round(float(x), dp)


@dataclass
class DeliveryResult:
    reservation_id: str
    idempotent: bool
    rows: list[dict]


class FxCashService:
    def __init__(self, db, now=None, validator: CashValidatorAdapter | None = None):
        # db 可以是 Database（多线程，推荐）或单连接（测试便捷）
        self._db = db
        self.fx = FxAdapter(self._conn_provider())
        self.validator = validator or CashValidatorAdapter()
        self._now = now or (lambda: datetime.now())
        # 进程内串行化写事务；SQLite WAL 下配合 BEGIN IMMEDIATE 跨连接也安全
        self._wlock = threading.RLock()

    def _conn_provider(self):
        from_db = hasattr(self._db, "conn") and not isinstance(self._db, sqlite3.Connection)
        return (lambda: self._db.conn) if from_db else (lambda: self._db)

    @property
    def conn(self) -> sqlite3.Connection:
        provider = self._conn_provider()
        return provider()

    # ---------- 工具 ----------
    def ts(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def log(self, etype: str, detail: str, reservation_id: str | None = None,
            actor: str | None = "system") -> None:
        self.conn.execute(
            "INSERT INTO events(ts,type,reservation_id,actor,detail) VALUES(?,?,?,?,?)",
            (self.ts(), etype, reservation_id, actor, detail),
        )

    def _teller(self, teller_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM tellers WHERE id=?", (teller_id,)).fetchone()
        if not row:
            raise PermissionDenied(f"柜员不存在：{teller_id}")
        return row

    def _require_role(self, teller_id: str, role: str) -> sqlite3.Row:
        t = self._teller(teller_id)
        if t["role"] != role:
            raise PermissionDenied(f"柜员 {teller_id} 角色为 {t['role']}，需要 {role}")
        return t

    def _get(self, rid: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
        if not row:
            raise ServiceError(f"预约不存在：{rid}")
        return row

    def _denom(self, denom_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM denominations WHERE id=?", (denom_id,)).fetchone()
        if not row:
            raise ServiceError(f"券别不存在：{denom_id}")
        return row

    def _active_lines(self, rid: str) -> list[sqlite3.Row]:
        """当前生效的预约行：替换确认后以 replacement 行为准。"""
        return self.conn.execute(
            "SELECT rl.*, d.face_value, d.currency FROM reservation_lines rl "
            "JOIN denominations d ON d.id=rl.denom_id "
            "WHERE rl.reservation_id=? AND rl.id NOT IN "
            " (SELECT replacement_of FROM reservation_lines "
            "  WHERE reservation_id=? AND replacement_of IS NOT NULL AND customer_accepted=1) "
            "ORDER BY rl.id",
            (rid, rid),
        ).fetchall()

    # ---------- 1. 保存预约（草稿，不锁库） ----------
    def create_reservation(self, customer_name: str, id_doc: str, currency: str,
                           branch_code: str, lines: list[dict], expires_at: str,
                           teller_id: str = "T01") -> str:
        if not self.conn.execute("SELECT 1 FROM currencies WHERE code=?", (currency,)).fetchone():
            raise ServiceError(f"币种不存在：{currency}")
        if not self.conn.execute("SELECT 1 FROM branches WHERE code=?", (branch_code,)).fetchone():
            raise ServiceError(f"网点不存在：{branch_code}")
        if not lines:
            raise ServiceError("至少选择一个券别")
        datetime.fromisoformat(expires_at)  # 校验
        total_face = 0.0
        for ln in lines:
            d = self._denom(ln["denom_id"])
            if d["currency"] != currency:
                raise ServiceError(f"券别 {d['face_value']} 不属于币种 {currency}")
            if int(ln["qty"]) <= 0:
                raise ServiceError("券别数量必须为正整数")
            total_face += d["face_value"] * int(ln["qty"])

        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                n = self.conn.execute("SELECT COUNT(*) c FROM reservations").fetchone()["c"] + 1
                rid = f"RV-{self._now().strftime('%Y%m%d')}-{n:04d}"
                self.conn.execute(
                    "INSERT INTO reservations(id,customer_name,id_doc,currency,branch_code,"
                    "cny_amount,status,expires_at,created_at,created_by) VALUES(?,?,?,?,?,0,'draft',?,?,?)",
                    (rid, customer_name, id_doc, currency, branch_code,
                     expires_at, self.ts(), teller_id),
                )
                self.conn.executemany(
                    "INSERT INTO reservation_lines(reservation_id,denom_id,qty) VALUES(?,?,?)",
                    [(rid, ln["denom_id"], int(ln["qty"])) for ln in lines],
                )
                self.log("reservation_created",
                         f"{customer_name} 预约 {currency} {_money(total_face)}（{len(lines)} 个券别）",
                         rid, teller_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return rid

    # ---------- 2. 提交：锁定可用库存 ----------
    def submit(self, rid: str, teller_id: str = "T01") -> None:
        self._teller(teller_id)
        r = self._get(rid)
        if r["status"] != "draft":
            raise ServiceError(f"预约状态为 {r['status']}，不能提交锁定")
        lines = self.conn.execute(
            "SELECT rl.*, d.face_value FROM reservation_lines rl "
            "JOIN denominations d ON d.id=rl.denom_id WHERE rl.reservation_id=?",
            (rid,),
        ).fetchall()

        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for ln in lines:
                    free = self.conn.execute(
                        "SELECT total_qty-locked_qty AS free FROM inventory "
                        "WHERE branch_code=? AND denom_id=?",
                        (r["branch_code"], ln["denom_id"]),
                    ).fetchone()["free"]
                    if free < ln["qty"]:
                        self.log("stock_short",
                                 f"券别 #{ln['denom_id']} 需 {ln['qty']} 张，可用 {free} 张",
                                 rid, teller_id)
                        self.conn.execute("COMMIT")  # 仅落事件，无数据变更
                        raise InsufficientStock(r["branch_code"], ln["denom_id"],
                                                ln["qty"], free)
                # 全部可用才锁定：条件更新保证并发安全
                for ln in lines:
                    cur = self.conn.execute(
                        "UPDATE inventory SET locked_qty=locked_qty+? "
                        "WHERE branch_code=? AND denom_id=? AND total_qty-locked_qty>=?",
                        (ln["qty"], r["branch_code"], ln["denom_id"], ln["qty"]),
                    )
                    if cur.rowcount != 1:
                        raise InsufficientStock(r["branch_code"], ln["denom_id"], ln["qty"], 0)
                    self.conn.execute(
                        "INSERT INTO inventory_locks(reservation_id,branch_code,denom_id,qty,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (rid, r["branch_code"], ln["denom_id"], ln["qty"], self.ts()),
                    )
                self.conn.execute(
                    "UPDATE reservations SET status='held' WHERE id=?", (rid,))
                self.log("stock_locked",
                         f"提交成功，锁定 {len(lines)} 个券别库存，有效期至 {r['expires_at']}",
                         rid, teller_id)
                self.conn.execute("COMMIT")
            except InsufficientStock:
                raise
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ---------- 3. 确认：按确认时汇率版本快照 ----------
    def confirm(self, rid: str, teller_id: str = "T01", confirm_date: str | None = None) -> dict:
        self._teller(teller_id)
        r = self._get(rid)
        if r["status"] not in ("held",):
            raise ServiceError(f"预约状态为 {r['status']}，需先锁定库存才能确认")
        confirm_date = confirm_date or self._now().strftime("%Y-%m-%d")
        ver = self.fx.version_for(r["currency"], confirm_date)
        if not ver:
            raise ServiceError(f"{r['currency']} 在 {confirm_date} 无可用汇率版本")

        lines = self._active_lines(rid)
        fx_total = sum(ln["face_value"] * ln["qty"] for ln in lines)
        cny = _money(fx_total * ver["rate"], CNY_DECIMALS)

        created_day = r["created_at"][:10]
        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE reservations SET status='confirmed',fx_rate=?,rate_version_id=?,"
                    "cny_amount=?,confirmed_at=? WHERE id=?",
                    (ver["rate"], ver["id"], cny, self.ts(), rid),
                )
                if ver["eff_date"] != created_day:
                    self.log("rate_cross_day",
                             f"跨日汇率：预约日 {created_day}，确认按 {ver['eff_date']} 牌价 "
                             f"{ver['rate']} 折算", rid, teller_id)
                self.log("rate_snapshot",
                         f"锁定汇率版本 #{ver['id']}（{ver['eff_date']} {ver['rate']}），"
                         f"外币 {_money(fx_total)} → 本币 {cny}", rid, teller_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {"rate_version_id": ver["id"], "rate": ver["rate"],
                "fx_total": _money(fx_total), "cny_amount": cny}

    # ---------- 4. 券别替换：需客户重新确认 ----------
    def offer_replacement(self, rid: str, new_lines: list[dict],
                          teller_id: str = "T01") -> None:
        """柜员提出替换方案（如 100×10 → 50×20）。不立即调整锁库。"""
        self._teller(teller_id)
        r = self._get(rid)
        if r["status"] not in ("held", "confirmed"):
            raise ServiceError(f"状态 {r['status']} 不可提出券别替换")
        if not new_lines:
            raise ServiceError("替换方案不能为空")

        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                orig_ids = {row["id"]: row for row in self._active_lines(rid)}
                mapping = {int(x["replace_line_id"]): x for x in new_lines}
                for lid in mapping:
                    if lid not in orig_ids:
                        raise ServiceError(f"被替换行 {lid} 不属于当前预约")
                for old_id, spec in mapping.items():
                    d = self._denom(int(spec["denom_id"]))
                    if d["currency"] != r["currency"]:
                        raise ServiceError("替换券别币种不一致")
                    qty = int(spec["qty"])
                    if qty <= 0:
                        raise ServiceError("替换数量必须为正整数")
                    self.conn.execute(
                        "INSERT INTO reservation_lines(reservation_id,denom_id,qty,kind,"
                        "replacement_of,customer_accepted) VALUES(?,?,?,'replacement',?,0)",
                        (rid, spec["denom_id"], qty, old_id),
                    )
                self.conn.execute(
                    "UPDATE reservations SET status='replacement_offered' WHERE id=?", (rid,))
                self.log("replacement_offered",
                         f"提出券别替换方案，涉及 {len(mapping)} 个原券别，等待客户重新确认",
                         rid, teller_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def accept_replacement(self, rid: str, teller_id: str = "T01") -> None:
        """客户重新确认：原子释放原券别锁、锁定新券别。"""
        self._teller(teller_id)
        r = self._get(rid)
        if r["status"] != "replacement_offered":
            raise ServiceError("无待确认的替换方案")

        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                reps = self.conn.execute(
                    "SELECT * FROM reservation_lines WHERE reservation_id=? "
                    "AND kind='replacement' AND customer_accepted=0", (rid,),
                ).fetchall()
                # 先检查新券别可用量（考虑原券别锁即将释放，但新旧可能相同→先不释放再判断）
                for rep in reps:
                    free = self.conn.execute(
                        "SELECT total_qty-locked_qty free FROM inventory "
                        "WHERE branch_code=? AND denom_id=?",
                        (r["branch_code"], rep["denom_id"]),
                    ).fetchone()["free"]
                    orig = self.conn.execute(
                        "SELECT * FROM reservation_lines WHERE id=?",
                        (rep["replacement_of"],),
                    ).fetchone()
                    credited = orig["qty"] if orig["denom_id"] == rep["denom_id"] else 0
                    if free + credited < rep["qty"]:
                        self.log("stock_short",
                                 f"替换券别 #{rep['denom_id']} 可用 {free} 张，不足 {rep['qty']} 张",
                                 rid, teller_id)
                        self.conn.execute("COMMIT")
                        raise InsufficientStock(r["branch_code"], rep["denom_id"],
                                                rep["qty"], free + credited)
                for rep in reps:
                    orig = self.conn.execute(
                        "SELECT * FROM reservation_lines WHERE id=?",
                        (rep["replacement_of"],),
                    ).fetchone()
                    # 释放原券别锁
                    self.conn.execute(
                        "UPDATE inventory SET locked_qty=locked_qty-? "
                        "WHERE branch_code=? AND denom_id=?",
                        (orig["qty"], r["branch_code"], orig["denom_id"]),
                    )
                    self.conn.execute(
                        "UPDATE inventory_locks SET state='released',released_at=? "
                        "WHERE reservation_id=? AND denom_id=? AND state='held'",
                        (self.ts(), rid, orig["denom_id"]),
                    )
                    # 锁定新券别（同券别已释放额度，条件更新仍成立）
                    cur = self.conn.execute(
                        "UPDATE inventory SET locked_qty=locked_qty+? "
                        "WHERE branch_code=? AND denom_id=? AND total_qty-locked_qty>=?",
                        (rep["qty"], r["branch_code"], rep["denom_id"], rep["qty"]),
                    )
                    if cur.rowcount != 1:
                        raise InsufficientStock(r["branch_code"], rep["denom_id"], rep["qty"], 0)
                    self.conn.execute(
                        "INSERT INTO inventory_locks(reservation_id,branch_code,denom_id,qty,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (rid, r["branch_code"], rep["denom_id"], rep["qty"], self.ts()),
                    )
                    self.conn.execute(
                        "UPDATE reservation_lines SET customer_accepted=1 WHERE id=?",
                        (rep["id"],),
                    )
                # 客户重新确认后按当前汇率重算快照（替换可能改变外币总额）
                confirm_date = self._now().strftime("%Y-%m-%d")
                ver = self.fx.version_for(r["currency"], confirm_date)
                active = self._active_lines(rid)
                fx_total = sum(ln["face_value"] * ln["qty"] for ln in active)
                cny = _money(fx_total * ver["rate"], CNY_DECIMALS)
                self.conn.execute(
                    "UPDATE reservations SET status='replacement_confirmed',fx_rate=?,"
                    "rate_version_id=?,cny_amount=?,confirmed_at=? WHERE id=?",
                    (ver["rate"], ver["id"], cny, self.ts(), rid),
                )
                self.log("replacement_confirmed",
                         "客户已重新确认替换方案；库存已换锁，汇率快照已刷新", rid, teller_id)
                self.conn.execute("COMMIT")
            except InsufficientStock:
                raise
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def decline_replacement(self, rid: str, teller_id: str = "T01") -> None:
        """客户拒绝替换：维持原券别与原锁库，回到已确认/锁定状态。"""
        self._teller(teller_id)
        r = self._get(rid)
        if r["status"] != "replacement_offered":
            raise ServiceError("无待确认的替换方案")
        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "DELETE FROM reservation_lines WHERE reservation_id=? "
                    "AND kind='replacement' AND customer_accepted=0", (rid,))
                back = "confirmed" if r["fx_rate"] is not None else "held"
                self.conn.execute("UPDATE reservations SET status=? WHERE id=?", (back, rid))
                self.log("replacement_declined", "客户拒绝替换，维持原券别预约", rid, teller_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ---------- 5. 客户取消 / 到期释放 ----------
    def cancel(self, rid: str, reason: str, teller_id: str = "T01") -> None:
        self._teller(teller_id)
        r = self._get(rid)
        if r["status"] in ("fulfilled", "cancelled", "expired"):
            raise ServiceError(f"预约已终结（{r['status']}），不能取消")
        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._release_locks(rid)
                self.conn.execute(
                    "UPDATE reservations SET status='cancelled',cancel_reason=? WHERE id=?",
                    (reason, rid))
                self.log("customer_cancelled", f"客户取消：{reason}", rid, teller_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _release_locks(self, rid: str) -> int:
        rows = self.conn.execute(
            "SELECT * FROM inventory_locks WHERE reservation_id=? AND state='held'", (rid,)
        ).fetchall()
        for lk in rows:
            self.conn.execute(
                "UPDATE inventory SET locked_qty=locked_qty-? "
                "WHERE branch_code=? AND denom_id=?",
                (lk["qty"], lk["branch_code"], lk["denom_id"]),
            )
            self.conn.execute(
                "UPDATE inventory_locks SET state='released',released_at=? WHERE id=?",
                (self.ts(), lk["id"]),
            )
        return len(rows)

    def expire_due(self) -> list[str]:
        """释放所有超过取钞有效期仍未交付的预约（未到场）。"""
        now_iso = self.ts()
        due = self.conn.execute(
            "SELECT id FROM reservations WHERE status IN ('held','confirmed',"
            "'replacement_offered','replacement_confirmed') AND expires_at < ?",
            (now_iso,),
        ).fetchall()
        expired = []
        for row in due:
            rid = row["id"]
            with self._wlock:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    n = self._release_locks(rid)
                    self.conn.execute(
                        "UPDATE reservations SET status='expired' WHERE id=?", (rid,))
                    self.log("reservation_expired",
                             f"客户未到场，预约到期，释放 {n} 笔券别锁库", rid, "system")
                    self.conn.execute("COMMIT")
                    expired.append(rid)
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
        return expired

    # ---------- 6. 现钞交付（幂等） ----------
    def deliver(self, rid: str, teller_id: str, idempotency_key: str,
                count_overrides: dict | None = None) -> DeliveryResult:
        self._teller(teller_id)
        r = self._get(rid)

        # 幂等优先：同一幂等键（即使订单已终结）直接返回原交付，绝不重复扣库
        existing = self.conn.execute(
            "SELECT reservation_id FROM deliveries WHERE idem_group=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            rows = self.conn.execute(
                "SELECT * FROM deliveries WHERE reservation_id=? AND idem_group=?",
                (rid, idempotency_key)).fetchall()
            self.log("delivery_idempotent_hit",
                     f"幂等键 {idempotency_key} 重复提交，直接返回原交付记录", rid, teller_id)
            return DeliveryResult(rid, True, [dict(x) for x in rows])

        if r["status"] not in ("confirmed", "replacement_confirmed"):
            raise ServiceError(f"预约状态 {r['status']}，不可交付（需已确认）")

        validator = self.validator
        if count_overrides is not None:
            # HTTP/JSON 键为字符串，统一转 int
            norm = {int(k): v for k, v in count_overrides.items()}
            validator = CashValidatorAdapter(norm)
        lines = self._active_lines(rid)

        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                out = []
                for ln in lines:
                    cr = validator.count(ln["denom_id"], ln["qty"])
                    self.conn.execute(
                        "INSERT INTO deliveries(reservation_id,denom_id,booked_qty,counted_qty,"
                        "diff_qty,suspicious,idem_group,delivered_by,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (rid, ln["denom_id"], cr.booked_qty, cr.counted_qty, cr.diff_qty,
                         1 if cr.suspicious else 0, idempotency_key, teller_id, self.ts()),
                    )
                    if cr.suspicious:
                        self.log("suspicious_note",
                                 f"券别 #{ln['denom_id']} 点验发现假钞疑点（冠字号示例 "
                                 f"{','.join(cr.serials) or '无'}），暂存待主管复核",
                                 rid, teller_id)
                    if cr.diff_qty != 0:
                        self.log("delivery_diff",
                                 f"券别 #{ln['denom_id']} 预约 {cr.booked_qty} 实点 "
                                 f"{cr.counted_qty}，差额 {cr.diff_qty:+d}", rid, teller_id)
                    # 现钞出库：按实点数量扣减实物与锁定
                    self.conn.execute(
                        "UPDATE inventory SET total_qty=total_qty-?,locked_qty=locked_qty-? "
                        "WHERE branch_code=? AND denom_id=?",
                        (cr.counted_qty, ln["qty"], r["branch_code"], ln["denom_id"]),
                    )
                    self.conn.execute(
                        "UPDATE inventory_locks SET state='delivered',released_at=? "
                        "WHERE reservation_id=? AND denom_id=? AND state='held'",
                        (self.ts(), rid, ln["denom_id"]),
                    )
                    out.append({"denom_id": ln["denom_id"], "booked": cr.booked_qty,
                                "counted": cr.counted_qty, "diff": cr.diff_qty,
                                "suspicious": cr.suspicious})
                # 若实点少于预约，锁库多减的部分仍属释放（差额短款，复核处理）
                self.conn.execute(
                    "UPDATE reservations SET status='fulfilled',fulfilled_at=? WHERE id=?",
                    (self.ts(), rid))
                self.log("cash_delivered",
                         f"现钞交付完成（幂等键 {idempotency_key}），共 {len(lines)} 个券别",
                         rid, teller_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return DeliveryResult(rid, False, out)

    # ---------- 7. 差额/疑点复核（仅主管；已复核金额不可改） ----------
    def review_delivery(self, delivery_id: int, supervisor_id: str, note: str) -> None:
        self._require_role(supervisor_id, "supervisor")
        d = self.conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if not d:
            raise ServiceError(f"交付记录不存在：{delivery_id}")
        if d["reviewed_by"] is not None:
            raise PermissionDenied(
                f"交付记录 #{delivery_id} 已由 {d['reviewed_by']} 复核，金额不可修改")
        if d["delivered_by"] == supervisor_id:
            raise PermissionDenied("交付人与复核人不能为同一柜员")
        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE deliveries SET reviewed_by=?,review_note=? WHERE id=?",
                    (supervisor_id, note, delivery_id))
                verdict = "账实相符" if d["diff_qty"] == 0 and not d["suspicious"] \
                    else f"差额 {d['diff_qty']:+d}" + ("，存在假钞疑点" if d["suspicious"] else "")
                self.log("delivery_reviewed",
                         f"主管复核交付 #{delivery_id}：{verdict}；意见：{note}",
                         d["reservation_id"], supervisor_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def amend_delivery(self, delivery_id: int, teller_id: str,
                       new_counted: int) -> None:
        """柜员尝试修改实点金额：已复核记录禁止修改。"""
        self._teller(teller_id)
        d = self.conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if not d:
            raise ServiceError("交付记录不存在")
        if d["reviewed_by"] is not None:
            raise PermissionDenied(
                f"交付记录 #{delivery_id} 已经 {d['reviewed_by']} 复核，柜员不得修改金额")
        with self._wlock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE deliveries SET counted_qty=?,diff_qty=?-booked_qty WHERE id=?",
                    (new_counted, new_counted, delivery_id))
                self.log("delivery_amended",
                         f"复核前修正实点数量为 {new_counted}",
                         d["reservation_id"], teller_id)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
