"""测试：券别竞争、汇率快照、预约过期、交付幂等、柜员权限等。

运行：python3 main.py test
"""
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database, connect
from services import (
    FxCashService,
    InsufficientStock,
    PermissionDenied,
    ServiceError,
)


class Clock:
    def __init__(self, t):
        self.t = t
    def __call__(self):
        return self.t


class FxCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.path)
        self.conn = self.db.conn
        self.clock = Clock(datetime(2026, 9, 29, 10, 0, 0))
        self.svc = FxCashService(self.db, now=self.clock)

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            p = Path(self.path + ext)
            if p.exists():
                p.unlink()

    def make(self, currency="USD", branch="B01", lines=None, exp="2026-09-30T16:00:00",
             customer="测试客户", doc="FX-TEST-0001", teller="T01"):
        # denom_id 6 = USD100（B01 库存 40）
        lines = lines or [{"denom_id": 6, "qty": 5}]
        return self.svc.create_reservation(customer, doc, currency, branch, lines, exp, teller)

    def free(self, branch="B01", denom=6):
        return self.conn.execute(
            "SELECT total_qty-locked_qty f FROM inventory WHERE branch_code=? AND denom_id=?",
            (branch, denom)).fetchone()["f"]

    def locked(self, branch="B01", denom=6):
        return self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code=? AND denom_id=?",
            (branch, denom)).fetchone()["locked_qty"]

    # ---------- 1. 提交锁库 ----------
    def test_submit_locks_stock(self):
        before = self.free()
        rid = self.make(lines=[{"denom_id": 6, "qty": 10}])
        self.assertEqual(self.free(), before)  # 草稿不锁
        self.svc.submit(rid)
        self.assertEqual(self.free(), before - 10)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"], "held")

    def test_submit_insufficient_stock_recorded(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 41}])  # 库存仅 40
        with self.assertRaises(InsufficientStock):
            self.svc.submit(rid)
        ev = self.conn.execute(
            "SELECT type FROM events WHERE reservation_id=? AND type='stock_short'", (rid,)).fetchall()
        self.assertTrue(ev, "库存不足必须记录事件")
        self.assertEqual(self.locked(), 0, "锁库失败不得占用库存")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"], "draft")

    # ---------- 2. 券别并发竞争：只允许一笔成功 ----------
    def test_concurrent_same_denom_only_one_wins(self):
        r1 = self.make(lines=[{"denom_id": 6, "qty": 30}], customer="甲", doc="D1")
        r2 = self.make(lines=[{"denom_id": 6, "qty": 30}], customer="乙", doc="D2")
        results = {}
        barrier = threading.Barrier(2)

        def worker(key, rid, teller):
            barrier.wait()
            try:
                self.svc.submit(rid, teller)
                results[key] = "ok"
            except InsufficientStock:
                results[key] = "short"

        threads = [threading.Thread(target=worker, args=("a", r1, "T01")),
                   threading.Thread(target=worker, args=("b", r2, "T02"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results.values()), ["ok", "short"])
        self.assertEqual(self.locked(), 30, "赢家锁定 30，输家不得部分占用")
        # 40 总量 - 30 锁 = 10 可用，再来 11 张仍失败
        r3 = self.make(lines=[{"denom_id": 6, "qty": 11}], customer="丙", doc="D3")
        with self.assertRaises(InsufficientStock):
            self.svc.submit(r3)

    # ---------- 3. 汇率快照 ----------
    def test_rate_snapshot_at_confirm(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 10}])  # USD100 × 10 = 1000
        self.svc.submit(rid)
        snap = self.svc.confirm(rid)
        self.assertEqual(snap["rate"], 7.182)  # 2026-09-27 版（<=2026-09-29 最新）
        self.assertEqual(snap["cny_amount"], round(1000 * 7.182, 2))
        row = self.conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["fx_rate"], 7.182)
        self.assertIsNotNone(row["rate_version_id"])

        # 之后发布新牌价不影响已确认金额
        self.conn.execute(
            "INSERT INTO rate_versions(currency,rate,eff_date,published_at,note) "
            "VALUES('USD',7.9900,'2026-09-30','2026-09-30T08:30:00','演示新牌价')")
        row = self.conn.execute("SELECT cny_amount,fx_rate FROM reservations WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["fx_rate"], 7.182)
        self.assertEqual(row["cny_amount"], round(1000 * 7.182, 2))

    def test_rate_cross_day_logged(self):
        # 创建日固定为 2026-09-29（时钟），按 09-26 牌价确认 → 跨日事件
        rid = self.make(lines=[{"denom_id": 6, "qty": 1}])
        self.svc.submit(rid)
        self.svc.confirm(rid, confirm_date="2026-09-26")
        ev = self.conn.execute(
            "SELECT type FROM events WHERE reservation_id=? AND type='rate_cross_day'", (rid,)).fetchall()
        self.assertTrue(ev, "跨日汇率确认必须留痕")
        row = self.conn.execute("SELECT fx_rate FROM reservations WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["fx_rate"], 7.1765)  # 09-26 版

    # ---------- 4. 券别替换需重新确认 ----------
    def test_replacement_requires_customer_acceptance(self):
        rid = self.make(currency="EUR", lines=[{"denom_id": 12, "qty": 6}])  # EUR200×6
        self.svc.submit(rid); self.svc.confirm(rid)
        line_id = self.svc._active_lines(rid)[0]["id"]
        self.svc.offer_replacement(rid, [{"replace_line_id": line_id, "denom_id": 11, "qty": 12}])
        st = self.conn.execute("SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"]
        self.assertEqual(st, "replacement_offered")
        # 待确认期间：原券别仍锁定，新券别未锁定
        self.assertEqual(self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=12").fetchone()["locked_qty"], 6)
        self.assertEqual(self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=11").fetchone()["locked_qty"], 0)
        self.svc.accept_replacement(rid)
        self.assertEqual(self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=12").fetchone()["locked_qty"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=11").fetchone()["locked_qty"], 12)
        st = self.conn.execute("SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"]
        self.assertEqual(st, "replacement_confirmed")
        active = self.svc._active_lines(rid)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["denom_id"], 11)

    def test_replacement_decline_keeps_original(self):
        rid = self.make(currency="EUR", lines=[{"denom_id": 12, "qty": 2}])
        self.svc.submit(rid); self.svc.confirm(rid)
        line_id = self.svc._active_lines(rid)[0]["id"]
        self.svc.offer_replacement(rid, [{"replace_line_id": line_id, "denom_id": 11, "qty": 4}])
        self.svc.decline_replacement(rid)
        st = self.conn.execute("SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"]
        self.assertEqual(st, "confirmed")
        self.assertEqual(self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=12").fetchone()["locked_qty"], 2)

    # ---------- 5. 取消与到期释放 ----------
    def test_cancel_releases_locks(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 10}])
        self.svc.submit(rid)
        self.assertEqual(self.locked(), 10)
        self.svc.cancel(rid, "客户取消")
        self.assertEqual(self.locked(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"], "cancelled")
        ev = self.conn.execute(
            "SELECT type FROM events WHERE reservation_id=? AND type='customer_cancelled'", (rid,)).fetchall()
        self.assertTrue(ev)
        with self.assertRaises(ServiceError):
            self.svc.cancel(rid, "重复取消")

    def test_expire_releases_unattended(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 7}], exp="2026-09-28T16:00:00")
        self.svc.submit(rid); self.svc.confirm(rid)
        rid2 = self.make(lines=[{"denom_id": 6, "qty": 3}], exp="2026-10-05T16:00:00",
                         customer="未到期", doc="D9")
        self.svc.submit(rid2)
        expired = self.svc.expire_due()
        self.assertEqual(expired, [rid])
        self.assertEqual(self.locked(), 3)  # rid2 仍有效
        self.assertEqual(self.conn.execute(
            "SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"], "expired")
        # 幂等：再扫描无新增
        self.assertEqual(self.svc.expire_due(), [])

    # ---------- 6. 交付幂等 + 差额 + 假钞 ----------
    def _ready(self, rid):
        self.svc.submit(rid); return self.svc.confirm(rid)

    def test_delivery_idempotent(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 10}])
        self._ready(rid)
        total_before = self.conn.execute(
            "SELECT total_qty FROM inventory WHERE branch_code='B01' AND denom_id=6").fetchone()["total_qty"]
        r1 = self.svc.deliver(rid, "T01", "KEY-1")
        self.assertFalse(r1.idempotent)
        total_after = self.conn.execute(
            "SELECT total_qty FROM inventory WHERE branch_code='B01' AND denom_id=6").fetchone()["total_qty"]
        self.assertEqual(total_before - total_after, 10)
        r2 = self.svc.deliver(rid, "T01", "KEY-1")
        self.assertTrue(r2.idempotent, "相同幂等键重复提交必须命中幂等")
        total_after2 = self.conn.execute(
            "SELECT total_qty FROM inventory WHERE branch_code='B01' AND denom_id=6").fetchone()["total_qty"]
        self.assertEqual(total_after, total_after2, "幂等命中不得再次扣减库存")
        cnt = self.conn.execute(
            "SELECT COUNT(*) c FROM deliveries WHERE reservation_id=?", (rid,)).fetchone()["c"]
        self.assertEqual(cnt, 1)  # 仅一个券别一条记录，幂等重放未新增

    def test_delivery_diff_and_suspicious(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 10}])
        self._ready(rid)
        self.svc.deliver(rid, "T01", "KEY-DIFF",
                         count_overrides={6: {"counted": 8, "suspicious": True}})
        d = self.conn.execute("SELECT * FROM deliveries WHERE reservation_id=?", (rid,)).fetchone()
        self.assertEqual((d["booked_qty"], d["counted_qty"], d["diff_qty"]), (10, 8, -2))
        self.assertEqual(d["suspicious"], 1)
        ev_types = {r["type"] for r in self.conn.execute(
            "SELECT type FROM events WHERE reservation_id=?", (rid,)).fetchall()}
        self.assertIn("delivery_diff", ev_types)
        self.assertIn("suspicious_note", ev_types)

    def test_delivery_requires_confirmation(self):
        rid = self.make()
        with self.assertRaises(ServiceError):
            self.svc.deliver(rid, "T01", "KEY-X")  # 草稿不能交付
        self.svc.submit(rid)
        with self.assertRaises(ServiceError):
            self.svc.deliver(rid, "T01", "KEY-X2")  # 仅锁库未确认也不行

    # ---------- 7. 柜员权限 ----------
    def test_only_supervisor_reviews_and_locked_amount(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 5}])
        self._ready(rid)
        self.svc.deliver(rid, "T01", "KEY-P", count_overrides={6: {"counted": 4}})
        did = self.conn.execute(
            "SELECT id FROM deliveries WHERE reservation_id=?", (rid,)).fetchone()["id"]
        # 普通柜员不能复核
        with self.assertRaises(PermissionDenied):
            self.svc.review_delivery(did, "T02", "越权复核")
        # 复核前柜员可以修正
        self.svc.amend_delivery(did, "T01", 5)
        # 主管（非交付人）复核通过
        self.svc.review_delivery(did, "T03", "差额挂账")
        # 已复核金额柜员不得修改
        with self.assertRaises(PermissionDenied):
            self.svc.amend_delivery(did, "T01", 99)
        # 重复复核也被拒绝
        with self.assertRaises(PermissionDenied):
            self.svc.review_delivery(did, "T03", "再次复核")

    def test_self_review_forbidden(self):
        rid = self.make(lines=[{"denom_id": 6, "qty": 3}])
        self._ready(rid)
        self.svc.deliver(rid, "T03", "KEY-S")  # 主管本人交付
        did = self.conn.execute(
            "SELECT id FROM deliveries WHERE reservation_id=?", (rid,)).fetchone()["id"]
        with self.assertRaises(PermissionDenied):
            self.svc.review_delivery(did, "T03", "自复核")

    # ---------- 8. 其他规则 ----------
    def test_denom_currency_must_match(self):
        with self.assertRaises(ServiceError):
            self.make(currency="USD", lines=[{"denom_id": 7, "qty": 1}])  # 7 = EUR5

    def test_replacement_short_stock_fails_safely(self):
        # EUR50(id=10) B01 仅 20 张；把 EUR200×6 替换成 EUR50×30 → 新券别不足，原锁保持
        rid = self.make(currency="EUR", lines=[{"denom_id": 12, "qty": 6}])
        self.svc.submit(rid); self.svc.confirm(rid)
        line_id = self.svc._active_lines(rid)[0]["id"]
        self.svc.offer_replacement(rid, [{"replace_line_id": line_id, "denom_id": 10, "qty": 30}])
        with self.assertRaises(InsufficientStock):
            self.svc.accept_replacement(rid)
        # 失败安全：仍为替换待确认，原 200 券别锁定不动
        self.assertEqual(self.conn.execute(
            "SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"],
            "replacement_offered")
        self.assertEqual(self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=12").fetchone()["locked_qty"], 6)
        self.assertEqual(self.conn.execute(
            "SELECT locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=10").fetchone()["locked_qty"], 0)


if __name__ == "__main__":
    unittest.main()
