"""单条本地启动入口。

用法：
  python3 main.py demo      # 一条命令完成：创建预约 → 锁库 → 确认 → 交付 样例
  python3 main.py serve     # 启动 Web 平台 http://127.0.0.1:8080
  python3 main.py reset     # 重建数据库（清除演示数据）
  python3 main.py test      # 运行全部测试
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import Database, connect
from services import FxCashService, InsufficientStock, ServiceError

DEMO_DB = str(Path(__file__).parent / "fx-demo.db")


class Clock:
    """固定演示时钟：2026-09-29 上午。"""
    def __init__(self, t: datetime):
        self.t = t
    def __call__(self) -> datetime:
        return self.t


def hr(title: str) -> None:
    print("\n" + "=" * 66)
    print(f"  {title}")
    print("=" * 66)


def run_demo() -> None:
    for ext in ("", "-wal", "-shm"):
        p = Path(DEMO_DB + ext)
        if p.exists():
            p.unlink()

    clock = Clock(datetime(2026, 9, 29, 10, 0, 0))
    db = Database(DEMO_DB)
    conn = db.conn
    svc = FxCashService(db, now=clock)

    hr("1. 保存预约（草稿，不锁库）")
    rid = svc.create_reservation(
        customer_name="林一帆", id_doc="FX-330101-19900101-0001",
        currency="USD", branch_code="B01",
        lines=[{"denom_id": 6, "qty": 10}, {"denom_id": 5, "qty": 20}],  # 100×10 + 50×20
        expires_at="2026-09-30T16:00:00", teller_id="T01")
    print(f"  预约号 {rid}：USD 100×10 + 50×20，取钞网点 B01，有效期至 2026-09-30 16:00")
    free = conn.execute(
        "SELECT total_qty-locked_qty f FROM inventory WHERE branch_code='B01' AND denom_id=6"
    ).fetchone()["f"]
    print(f"  草稿阶段 USD100 可用仍为 {free} 张（未锁库）")

    hr("2. 提交预约 → 锁定可用库存")
    svc.submit(rid, "T01")
    row = conn.execute(
        "SELECT total_qty,locked_qty FROM inventory WHERE branch_code='B01' AND denom_id=6"
    ).fetchone()
    print(f"  USD100：总量 {row['total_qty']}，锁定 {row['locked_qty']}，可用 {row['total_qty']-row['locked_qty']}")

    hr("3. 确认预约 → 按确认时汇率版本快照")
    snap = svc.confirm(rid, "T01")
    print(f"  使用汇率版本 #{snap['rate_version_id']}：{snap['rate']}")
    print(f"  外币合计 {snap['fx_total']:.4f} USD → 本币 {snap['cny_amount']:.2f} CNY（写入预约，后续牌价变动不影响）")

    hr("4. 两名柜员并发占用同一券别库存 → 只允许一笔成功")
    rid2 = svc.create_reservation(
        "周慕白", "FX-310104-19880707-0002", "USD", "B01",
        [{"denom_id": 6, "qty": 25}], "2026-09-30T16:00:00", "T02")
    rid3 = svc.create_reservation(
        "陈砚秋", "FX-440300-19921212-0003", "USD", "B01",
        [{"denom_id": 6, "qty": 25}], "2026-09-30T16:00:00", "T02")
    results = {}
    barrier = threading.Barrier(2)
    def worker(key, r, teller):
        barrier.wait()
        try:
            svc.submit(r, teller)
            results[key] = "成功"
        except InsufficientStock as e:
            results[key] = f"被拒：{e}"
    t_a = threading.Thread(target=worker, args=("A", rid2, "T01"))
    t_b = threading.Thread(target=worker, args=("B", rid3, "T02"))
    t_a.start(); t_b.start(); t_a.join(); t_b.join()
    print(f"  柜员甲预约 {rid2}：{results['A']}")
    print(f"  柜员乙预约 {rid3}：{results['B']}")
    assert results["A"] == "成功" and results["B"] != "成功" or \
           results["B"] == "成功" and results["A"] != "成功"
    print("  ✓ 同一券别并发占用恰好一笔成功（条件更新 + BEGIN IMMEDIATE 串行化）")

    hr("5. 券别替换需客户重新确认")
    rid4 = svc.create_reservation(
        "苏晚晴", "FX-110108-19950303-0004", "EUR", "B01",
        [{"denom_id": 12, "qty": 6}], "2026-09-30T16:00:00", "T01")  # EUR200×6（全部库存）
    svc.submit(rid4, "T01"); svc.confirm(rid4, "T01")
    lines = svc._active_lines(rid4)
    svc.offer_replacement(rid4, [{"replace_line_id": lines[0]["id"],
                                  "denom_id": 11, "qty": 12}], "T01")  # 200×6 → 100×12
    print(f"  {rid4}：EUR200×6 替换为 EUR100×12，状态=替换待确认")
    svc.accept_replacement(rid4, "T01")
    st = conn.execute("SELECT status FROM reservations WHERE id=?", (rid4,)).fetchone()["status"]
    print(f"  客户重新确认后状态={st}，原券别锁释放、新券别锁生效，汇率快照刷新")

    hr("6. 现钞交付：模拟点验（含 1 张差额与假钞疑点），幂等键重复提交")
    res1 = svc.deliver(rid, "T01", "KEY-DEMO-001",
                       count_overrides={6: {"counted": 10}, 5: {"counted": 19, "suspicious": True}})
    print("  首次交付（USD50 实点 19 张、1 张假钞疑点）：")
    for x in res1.rows:
        print(f"    券别#{x['denom_id']} 预约 {x['booked']} 实点 {x['counted']} 差额 {x['diff']:+d}"
              + ("  [假钞疑点]" if x["suspicious"] else ""))
    res2 = svc.deliver(rid, "T01", "KEY-DEMO-001")
    print(f"  相同幂等键再次交付 → idempotent={res2.idempotent}，库存未重复扣减")

    hr("7. 柜员权限：复核仅主管，且已复核金额不可修改")
    did = conn.execute(
        "SELECT id FROM deliveries WHERE reservation_id=? AND suspicious=1", (rid,)).fetchone()["id"]
    try:
        svc.review_delivery(did, "T02", "柜员尝试复核")
    except Exception as e:
        print(f"  柜员 T02 复核被拒：{e}")
    svc.review_delivery(did, "T03", "假钞暂扣，差额挂账，账实复核无误")
    print("  主管 T03 复核通过")
    try:
        svc.amend_delivery(did, "T01", 99)
    except Exception as e:
        print(f"  复核后柜员改金额被拒：{e}")

    hr("8. 客户取消与未到场到期释放库存")
    svc.cancel(rid3, "客户行程变更", "T02")
    print(f"  {rid3} 客户取消，库存已释放")
    clock.t = datetime(2026, 10, 1, 10, 0, 0)  # 时钟拨到有效期后
    expired = svc.expire_due()
    print(f"  扫描到期未到场：释放 {expired}（{rid4} 等），锁库全部归零")
    left = conn.execute("SELECT COUNT(*) c FROM inventory_locks WHERE state='held'").fetchone()["c"]
    print(f"  当前仍处 held 的锁库记录：{left} 笔")

    hr("业务事件流水（最近 12 条）")
    for e in conn.execute("SELECT ts,type,reservation_id,detail FROM events ORDER BY id DESC LIMIT 12"):
        print(f"  [{e['ts']}] {e['type']:24s} {(e['reservation_id'] or ''):18s} {e['detail']}")

    print("\n演示完成。启动 Web 界面：python3 main.py serve  →  http://127.0.0.1:8080")
    conn.close()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "demo":
        run_demo()
    elif cmd == "serve":
        from server import serve
        port = int(os.environ.get("PORT", "8080"))
        db = os.environ.get("FX_DB", str(Path(__file__).parent / "fx.db"))
        serve(db, port=port)
    elif cmd == "reset":
        for f in ("fx.db", "fx.db-wal", "fx.db-shm"):
            p = Path(__file__).parent / f
            if p.exists():
                p.unlink()
        connect(str(Path(__file__).parent / "fx.db")).close()
        print("数据库已重建：fx.db")
    elif cmd == "test":
        import unittest
        sys.path.insert(0, str(Path(__file__).parent / "tests"))
        loader = unittest.TestLoader()
        suite = loader.discover(str(Path(__file__).parent / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
