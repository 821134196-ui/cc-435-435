"""平台领域测试：券别竞争、汇率快照、预约过期、交付幂等、柜员权限等。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from fxplatform.db import connect, init_db
from fxplatform.services import appointments as A
from fxplatform.services import delivery as DV
from fxplatform.services import fx as FX
from fxplatform.services.errors import (
    ConcurrentLock,
    InvalidState,
    InventoryShortage,
    PermissionDenied,
)


class PlatformTestBase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.denoms = {}
        for ccy in ("USD", "EUR", "JPY", "HKD", "GBP"):
            self.denoms[ccy] = {
                str(r["face_value"]): r["id"] for r in self.conn.execute(
                    "SELECT id,face_value FROM denominations"
                    " WHERE currency_code=?", (ccy,))
            }

    def tearDown(self) -> None:
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def stock(self, branch: str, ccy: str, face: str):
        r = self.conn.execute(
            "SELECT total_qty,locked_qty FROM inventory i"
            " JOIN denominations d ON d.id=i.denomination_id"
            " WHERE i.branch_code=? AND d.currency_code=? AND d.face_value=?",
            (branch, ccy, face)).fetchone()
        return r["total_qty"], r["locked_qty"]

    def make(self, ccy="USD", branch="BR001", items=None, actor="zhang_san",
             hours=24, name="虚构客户", id_no="T0001"):
        items = items or {self.denoms["USD"]["100"]: 5}
        return A.create_reservation(
            self.conn, actor,
            {"name": name, "id_type": "护照", "id_no": id_no, "phone": "138-0000-0000"},
            ccy, branch, items, valid_hours=hours)


class TestInventoryShortage(PlatformTestBase):
    def test_insufficient_stock_rejected(self):
        """库存不足：整笔失败，不产生任何锁定。"""
        _, locked_before = self.stock("BR001", "GBP", "50")
        with self.assertRaises(InventoryShortage) as cm:
            self.make(ccy="GBP", items={self.denoms["GBP"]["50"]: 999})
        self.assertEqual(cm.exception.shortages[0]["need"], 999)
        total, locked = self.stock("BR001", "GBP", "50")
        self.assertEqual(locked, locked_before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM appointments").fetchone()[0], 0)

    def test_partial_shortage_rolls_back_all(self):
        """多券别中一个不足，全部不锁定。"""
        items = {self.denoms["USD"]["100"]: 1,
                 self.denoms["USD"]["50"]: 999_999}
        with self.assertRaises(InventoryShortage):
            self.make(items=items)
        _, locked100 = self.stock("BR001", "USD", "100")
        _, locked50 = self.stock("BR001", "USD", "50")
        self.assertEqual((locked100, locked50), (0, 0))


class TestConcurrentLock(PlatformTestBase):
    """券别竞争：两名柜员同时占用同一券别，只允许一笔成功。"""

    def test_two_tellers_one_wins(self):
        did = self.denoms["GBP"]["50"]  # BR001 共 20 张
        results = {}
        barrier = threading.Barrier(2)

        def reserve(tag, actor):
            conn = connect(self.db_path)
            try:
                barrier.wait(timeout=10)
                d = A.create_reservation(
                    conn, actor,
                    {"name": f"竞争{tag}", "id_type": "护照",
                     "id_no": f"X-{tag}", "phone": "-"},
                    "GBP", "BR001", {did: 15}, valid_hours=24)
                results[tag] = ("ok", d["appointment"]["code"])
            except (ConcurrentLock, InventoryShortage) as e:
                results[tag] = ("fail", type(e).__name__)
            finally:
                conn.close()

        t1 = threading.Thread(target=reserve, args=("A", "zhang_san"))
        t2 = threading.Thread(target=reserve, args=("B", "li_si"))
        t1.start(); t2.start(); t1.join(15); t2.join(15)
        self.assertEqual(set(results), {"A", "B"})
        statuses = sorted(v[0] for v in results.values())
        self.assertEqual(statuses, ["fail", "ok"])
        # 只有 15 张被锁定，绝不会超锁
        total, locked = self.stock("BR001", "GBP", "50")
        self.assertEqual(locked, 15)
        self.assertLessEqual(locked, total)

    def test_three_tellers_stock_for_two_runs(self):
        """库存恰好支持两笔时，并发第三笔必然失败。"""
        did = self.denoms["GBP"]["50"]  # 20 张：每笔 10 → 恰好 2 笔
        results = {}
        barrier = threading.Barrier(3)

        def reserve(tag):
            conn = connect(self.db_path)
            try:
                barrier.wait(timeout=10)
                A.create_reservation(
                    conn, "zhang_san",
                    {"name": tag, "id_type": "护照", "id_no": tag, "phone": "-"},
                    "GBP", "BR001", {did: 10}, valid_hours=24)
                results[tag] = "ok"
            except (ConcurrentLock, InventoryShortage):
                results[tag] = "fail"
            finally:
                conn.close()

        ts = [threading.Thread(target=reserve, args=(t,))
              for t in ("A", "B", "C")]
        [t.start() for t in ts]; [t.join(15) for t in ts]
        self.assertEqual(sorted(results.values()), ["fail", "ok", "ok"])
        _, locked = self.stock("BR001", "GBP", "50")
        self.assertEqual(locked, 20)


class TestFxSnapshot(PlatformTestBase):
    """汇率按预约确认时的版本计算并快照。"""

    def test_confirm_snapshots_active_rate(self):
        d = self.make(items={self.denoms["USD"]["100"]: 10})
        code = d["appointment"]["code"]
        det = A.confirm(self.conn, code, "zhang_san")
        rate = FX.active_rate(self.conn, "USD")
        self.assertEqual(det["fx_rate"]["id"], rate["id"])
        # 10 × 100 × 7.1156 = 7115.6000
        self.assertEqual(det["appointment"]["amount_foreign"], "1000.0000")
        self.assertEqual(det["appointment"]["amount_cny"], "7115.6000")
        self.assertEqual(det["appointment"]["fx_rate_id"], rate["id"])

    def test_new_rate_version_does_not_change_confirmed_amount(self):
        """确认后发布新汇率版本，已确认金额仍锁定在旧快照。"""
        d = self.make(items={self.denoms["USD"]["100"]: 1})
        code = d["appointment"]["code"]
        before = A.confirm(self.conn, code, "zhang_san")
        old_cny = before["appointment"]["amount_cny"]
        FX.publish_rate(self.conn, "wang_wu", "supervisor", "USD", "9.9900")
        detail = A.get_detail(self.conn, code)
        self.assertEqual(detail["appointment"]["amount_cny"], old_cny)
        self.assertNotEqual(detail["fx_rate"]["rate"], "9.9900")

    def test_cross_day_confirm_uses_confirm_date_rate(self):
        """预约日与确认日不同：记录跨日，以确认日版本为准。"""
        d = self.make(items={self.denoms["USD"]["100"]: 1})
        code = d["appointment"]["code"]
        yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
        self.conn.execute(
            "UPDATE appointments SET reserved_date=? WHERE code=?",
            (yesterday, code))
        self.conn.commit()
        det = A.confirm(self.conn, code, "zhang_san")
        kinds = [e["event_type"] for e in det["fx_events"]]
        self.assertIn("cross_day", kinds)
        today_rate = FX.active_rate(self.conn, "USD")
        self.assertEqual(det["fx_rate"]["id"], today_rate["id"])

    def test_reconfirm_after_replacement_resnapshots_rate(self):
        d = self.make(items={self.denoms["USD"]["20"]: 10})
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        A.propose_replacement(
            self.conn, code, "li_si",
            {self.denoms["USD"]["10"]: 20}, "替换成小面额")
        det = A.decide_replacement(self.conn, code, True, "customer")
        self.assertIn("reconfirm", [e["event_type"] for e in det["fx_events"]])
        self.assertEqual(det["appointment"]["amount_foreign"], "200.0000")
        self.assertEqual(det["appointment"]["plan_version"], 2)

    def test_teller_cannot_publish_rate(self):
        with self.assertRaises(PermissionDenied):
            FX.publish_rate(self.conn, "zhang_san", "teller", "USD", "8.0000")


class TestExpiryAndCancel(PlatformTestBase):
    def test_expire_releases_stock(self):
        """未到场预约到期：状态 EXPIRED，库存释放，留取消记录。"""
        d = self.make(ccy="JPY",
                      items={self.denoms["JPY"]["10000"]: 10}, hours=1)
        code = d["appointment"]["code"]
        _, locked = self.stock("BR001", "JPY", "10000")
        self.assertEqual(locked, 10)
        self.conn.execute(
            "UPDATE appointments SET valid_until=? WHERE code=?",
            ("2000-01-01T00:00:00", code))
        self.conn.commit()
        expired = A.expire_due(self.conn)
        self.assertEqual(expired, [code])
        det = A.get_detail(self.conn, code)
        self.assertEqual(det["appointment"]["status"], "EXPIRED")
        self.assertEqual(det["cancellations"][0]["kind"], "expired")
        _, locked_after = self.stock("BR001", "JPY", "10000")
        self.assertEqual(locked_after, 0)

    def test_expire_scan_idempotent(self):
        d = self.make(hours=1)
        code = d["appointment"]["code"]
        self.conn.execute(
            "UPDATE appointments SET valid_until='2000-01-01T00:00:00'"
            " WHERE code=?", (code,))
        self.conn.commit()
        self.assertEqual(A.expire_due(self.conn), [code])
        self.assertEqual(A.expire_due(self.conn), [])

    def test_customer_cancel_releases_stock(self):
        d = self.make(items={self.denoms["USD"]["50"]: 7})
        code = d["appointment"]["code"]
        A.cancel(self.conn, code, "zhang_san", "客户行程变更", "customer")
        det = A.get_detail(self.conn, code)
        self.assertEqual(det["appointment"]["status"], "CANCELLED")
        self.assertEqual(det["cancellations"][0]["kind"], "customer")
        _, locked = self.stock("BR001", "USD", "50")
        self.assertEqual(locked, 0)

    def test_delivered_cannot_cancel(self):
        d = self.make(items={self.denoms["USD"]["50"]: 2})
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        DV.deliver(self.conn, code, "zhang_san")
        with self.assertRaises(InvalidState):
            A.cancel(self.conn, code, "zhang_san", "迟来的取消")


class TestDelivery(PlatformTestBase):
    def _confirmed(self, items=None):
        d = self.make(items=items or {self.denoms["USD"]["100"]: 5})
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        return code

    def test_delivery_idempotent(self):
        """重复交付返回原单据，库存只扣一次。"""
        did = self.denoms["USD"]["100"]
        code = self._confirmed({did: 5})
        total0, locked0 = self.stock("BR001", "USD", "100")
        b1 = DV.deliver(self.conn, code, "zhang_san", counted={did: 5})
        self.assertFalse(b1["replay"])
        total1, locked1 = self.stock("BR001", "USD", "100")
        self.assertEqual(total0 - total1, 5)
        b2 = DV.deliver(self.conn, code, "zhang_san", counted={did: 1})
        self.assertTrue(b2["replay"])
        self.assertEqual(b1["delivery"]["id"], b2["delivery"]["id"])
        total2, locked2 = self.stock("BR001", "USD", "100")
        self.assertEqual((total2, locked2), (total1, locked1))

    def test_short_diff_recorded(self):
        did = self.denoms["USD"]["100"]
        code = self._confirmed({did: 5})
        b = DV.deliver(self.conn, code, "zhang_san", counted={did: 3})
        self.assertEqual(b["appointment"]["status"], "REVIEWED")
        shorts = [x for x in b["diffs"] if x["kind"] == "short"]
        self.assertEqual(len(shorts), 1)
        self.assertEqual(shorts[0]["planned_qty"], 5)
        self.assertEqual(shorts[0]["counted_qty"], 3)
        # 实出 3 张，5 张锁定全部解除
        total, locked = self.stock("BR001", "USD", "100")
        self.assertEqual(locked, 0)

    def test_over_diff_recorded(self):
        did = self.denoms["USD"]["100"]
        code = self._confirmed({did: 5})
        b = DV.deliver(self.conn, code, "zhang_san", counted={did: 6})
        overs = [x for x in b["diffs"] if x["kind"] == "over"]
        self.assertEqual(len(overs), 1)

    def test_counterfeit_blocks_delivery(self):
        """假钞疑点：不出库、转主管、处置后释放。"""
        did = self.denoms["USD"]["10"]
        code = self._confirmed({did: 5})
        total0, locked0 = self.stock("BR001", "USD", "10")
        b = DV.deliver(self.conn, code, "zhang_san",
                       counterfeit={did: ["FAKE-1"]})
        self.assertTrue(b["counterfeit"])
        cfs = [x for x in b["diffs"] if x["kind"] == "counterfeit"]
        self.assertEqual(len(cfs), 1)
        total1, locked1 = self.stock("BR001", "USD", "10")
        self.assertEqual((total0, locked0), (total1, locked1))  # 未出库
        DV.resolve_counterfeit(self.conn, code, "wang_wu", "收缴上报")
        self.assertEqual(A.get_detail(self.conn, code)["appointment"]["status"],
                         "CANCELLED")
        _, locked2 = self.stock("BR001", "USD", "10")
        self.assertEqual(locked2, 0)

    def test_exact_delivery_completes(self):
        code = self._confirmed()
        b = DV.deliver(self.conn, code, "zhang_san")
        self.assertEqual(b["appointment"]["status"], "DELIVERED")
        self.assertFalse(any(x["status"] == "open" for x in b["diffs"]))


class TestTellerPermissions(PlatformTestBase):
    def _delivered_short(self):
        did = self.denoms["USD"]["100"]
        d = self.make(items={did: 5})
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        DV.deliver(self.conn, code, "zhang_san", counted={did: 4})
        return code, did

    def test_teller_cannot_review(self):
        code, _ = self._delivered_short()
        with self.assertRaises(PermissionDenied):
            DV.review_delivery(self.conn, code, "zhang_san")
        with self.assertRaises(PermissionDenied):
            DV.review_delivery(self.conn, code, "li_si")  # 另一柜员也不行

    def test_supervisor_review_freezes(self):
        """复核后柜员/主管都不能再改已复核数量。"""
        code, did = self._delivered_short()
        r = DV.review_delivery(
            self.conn, code, "wang_wu",
            reviewed={did: 4}, resolution="短款客户确认")
        self.assertEqual(r["delivery"]["status"], "reviewed")
        again = DV.review_delivery(
            self.conn, code, "wang_wu", reviewed={did: 1})
        self.assertTrue(again.get("frozen"))
        # 库里的复核数量未被篡改
        qty = self.conn.execute(
            "SELECT reviewed_qty FROM delivery_lines WHERE delivery_id="
            "(SELECT id FROM deliveries WHERE appointment_id="
            "(SELECT id FROM appointments WHERE code=?))", (code,)).fetchone()
        self.assertEqual(qty["reviewed_qty"], 4)

    def test_teller_cannot_resolve_counterfeit(self):
        did = self.denoms["USD"]["10"]
        d = self.make(items={did: 2})
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        DV.deliver(self.conn, code, "zhang_san",
                   counterfeit={did: ["FAKE-9"]})
        with self.assertRaises(PermissionDenied):
            DV.resolve_counterfeit(self.conn, code, "li_si", "柜员处置")

    def test_supervisor_cannot_deliver(self):
        """主管有复核权，但不能直接办交付（职责分离）。"""
        d = self.make(items={self.denoms["USD"]["100"]: 2})
        c = d["appointment"]["code"]
        A.confirm(self.conn, c, "zhang_san")
        with self.assertRaises(PermissionDenied):
            DV.deliver(self.conn, c, "wang_wu")

    def test_verify_wrong_branch_fails(self):
        d = self.make(branch="BR001")
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        r = DV.counter_verify(self.conn, code, "li_si", "BR002")
        self.assertFalse(r["passed"])
        self.assertTrue(any("网点" in p for p in r["problems"]))


class TestReplacement(PlatformTestBase):
    def test_replacement_requires_reconfirm(self):
        d = self.make(items={self.denoms["USD"]["20"]: 50})
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        A.propose_replacement(
            self.conn, code, "zhang_san",
            {self.denoms["USD"]["20"]: 30, self.denoms["USD"]["10"]: 40},
            "20 紧张")
        self.assertEqual(A.get_detail(self.conn, code)["appointment"]["status"],
                         "REPLACE_PENDING")
        # 待确认期间不能交付
        with self.assertRaises(InvalidState):
            DV.deliver(self.conn, code, "zhang_san")
        det = A.decide_replacement(self.conn, code, True, "customer")
        self.assertEqual(det["appointment"]["status"], "CONFIRMED")
        # 锁库已换：20 锁 30，10 锁 40
        _, l20 = self.stock("BR001", "USD", "20")
        _, l10 = self.stock("BR001", "USD", "10")
        self.assertEqual((l20, l10), (30, 40))

    def test_reject_keeps_original_plan_and_locks(self):
        d = self.make(items={self.denoms["USD"]["20"]: 50})
        code = d["appointment"]["code"]
        A.confirm(self.conn, code, "zhang_san")
        A.propose_replacement(
            self.conn, code, "zhang_san",
            {self.denoms["USD"]["10"]: 100}, "改成小钞")
        A.decide_replacement(self.conn, code, False, "customer")
        det = A.get_detail(self.conn, code)
        self.assertEqual(det["appointment"]["status"], "CONFIRMED")
        self.assertEqual(det["appointment"]["plan_version"], 1)
        _, l20 = self.stock("BR001", "USD", "20")
        _, l10 = self.stock("BR001", "USD", "10")
        self.assertEqual((l20, l10), (50, 0))


if __name__ == "__main__":
    unittest.main()
