#!/usr/bin/env python3
"""单条本地命令：创建预约、锁定库存、确认与交付样例。

用法：
    python3 run_demo.py                # 重建演示库并走完整样例
    python3 run_demo.py --serve        # 走完样例后启动 Web 服务
    python3 run_demo.py --keep         # 保留现有库，仅追加样例

所有客户、证件、金额均为虚构数据。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fxplatform.config import DB_PATH, HOST, PORT
from fxplatform.db import connect, init_db
from fxplatform.services import appointments as A
from fxplatform.services import delivery as DV
from fxplatform.services import fx as FX
from fxplatform.services import inventory as INV
from fxplatform.services.errors import PlatformError
from fxplatform.services.moneyutil import display_money

LINE = "─" * 64


def head(title: str) -> None:
    print(f"\n{LINE}\n {title}\n{LINE}")


def denom_map(conn, currency: str) -> dict[str, int]:
    return {str(r["face_value"]): r["id"] for r in conn.execute(
        "SELECT id,face_value FROM denominations WHERE currency_code=?",
        (currency,))}


def show_stock(conn, branch: str, currency: str, faces: list[str]) -> None:
    dm = denom_map(conn, currency)
    for face in faces:
        r = conn.execute(
            "SELECT total_qty,locked_qty FROM inventory"
            " WHERE branch_code=? AND denomination_id=?",
            (branch, dm[face]),
        ).fetchone()
        print(f"    {currency} {face:>4}  总量 {r['total_qty']:>4}  "
              f"锁定 {r['locked_qty']:>4}  可用 {r['total_qty'] - r['locked_qty']:>4}")


def main() -> None:
    parser = argparse.ArgumentParser(description="外币现钞平台演示")
    parser.add_argument("--serve", action="store_true", help="样例结束后启动 Web 服务")
    parser.add_argument("--keep", action="store_true", help="不重建数据库")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if not args.keep and DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        print(f"已重建演示数据库：{DB_PATH}")

    conn = connect(DB_PATH)
    init_db(conn)

    usd = denom_map(conn, "USD")
    gbp = denom_map(conn, "GBP")

    head("① 新建预约并锁定库存（客户：林一舟 / 护照 E12345678 / 虚构）")
    show_stock(conn, "BR001", "USD", ["50", "100"])
    det = A.create_reservation(
        conn, "zhang_san",
        {"name": "林一舟", "id_type": "护照", "id_no": "E12345678",
         "phone": "138-0000-0001"},
        "USD", "BR001",
        {usd["100"]: 10, usd["50"]: 20},
        valid_hours=48, note="旅行用钞")
    code1 = det["appointment"]["code"]
    print(f"  预约号 {code1}，截止 {det['appointment']['valid_until']}")
    print("  锁定后：")
    show_stock(conn, "BR001", "USD", ["50", "100"])

    head("② 库存不足场景（试图预约超出可用数量，整笔失败不留锁）")
    try:
        A.create_reservation(
            conn, "li_si",
            {"name": "沈知夏", "id_type": "港澳通行证", "id_no": "H87654321",
             "phone": "138-0000-0002"},
            "GBP", "BR001", {gbp["50"]: 999}, valid_hours=24)
    except PlatformError as e:
        print(f"  已拦截：{e}")

    head("③ 客户到场确认：按确认当日汇率版本快照计算金额")
    det = A.confirm(conn, code1, "zhang_san")
    fxr = det["fx_rate"]
    print(f"  使用汇率 {fxr['rate_date']} v{fxr['version']}："
          f"1 USD = {fxr['rate']} CNY")
    print(f"  外币金额 {display_money(det['appointment']['amount_foreign'])} USD")
    print(f"  折人民币 {display_money(det['appointment']['amount_cny'])} 元（快照冻结）")

    head("④ 柜面核验（网点 / 状态 / 有效期 / 锁库一致）")
    v = DV.counter_verify(conn, code1, "zhang_san", "BR001")
    print("  核验通过" if v["passed"] else "  问题：" + "; ".join(v["problems"]))

    head("⑤ 现钞交付：按实点券别核对（本样例 100 面额短款 1 张）")
    bundle = DV.deliver(
        conn, code1, "zhang_san",
        counted={usd["100"]: 9, usd["50"]: 20},
        note="100 面额实点 9 张，客户认可")
    for ln in bundle["lines"]:
        mark = "  ← 短款" if ln["diff"] else ""
        print(f"    USD {ln['face_value']:>4}  预约 {ln['planned_qty']:>3}  "
              f"实点 {ln['counted_qty']:>3}  差额 {ln['diff']:>+3}{mark}")

    head("⑥ 交付幂等：重复提交不重复出库，返回原交付单")
    again = DV.deliver(conn, code1, "zhang_san",
                       counted={usd["100"]: 1, usd["50"]: 1})
    print(f"  replay={again['replay']}，交付单编号一致："
          f"{again['delivery']['id'] == bundle['delivery']['id']}")

    head("⑦ 柜员权限：柜员不能复核，主管复核后金额/数量冻结")
    try:
        DV.review_delivery(conn, code1, "zhang_san")
    except PlatformError as e:
        print(f"  柜员复核被拒：{e}")
    reviewed = DV.review_delivery(conn, code1, "wang_wu",
                                  resolution="短款 1 张，客户现场确认")
    print(f"  主管王五复核完成：{reviewed['delivery']['status']}，"
          f"预约状态 {reviewed['appointment']['status']}")
    frozen = DV.review_delivery(conn, code1, "wang_wu",
                                reviewed={usd["100"]: 1})
    print(f"  再次修改返回 frozen={frozen.get('frozen')}（已复核不可改）")

    head("⑧ 券别替换需客户重新确认（第二笔样例）")
    det2 = A.create_reservation(
        conn, "li_si",
        {"name": "顾南乔", "id_type": "身份证",
         "id_no": "110101199001011234", "phone": "138-0000-0003"},
        "USD", "BR001", {usd["20"]: 50}, valid_hours=24)
    code2 = det2["appointment"]["code"]
    A.confirm(conn, code2, "li_si")
    A.propose_replacement(conn, code2, "li_si",
                          {usd["20"]: 30, usd["10"]: 40},
                          reason="20 面额库存紧张")
    print(f"  {code2} 提出替换：20×50 → 20×30 + 10×40，状态 REPLACE_PENDING")
    A.decide_replacement(conn, code2, True, "customer")
    d2 = A.get_detail(conn, code2)
    print(f"  客户重新确认：方案 v{d2['appointment']['plan_version']}，"
          f"按当日汇率重算 {display_money(d2['appointment']['amount_foreign'])} USD，"
          f"库存已原子换锁")
    DV.deliver(conn, code2, "li_si")  # 按预约点足
    DV.review_delivery(conn, code2, "wang_wu", resolution="账实相符")
    print("  足额交付，主管复核结清")

    head("⑨ 假钞疑点（第三笔样例）")
    det3 = A.create_reservation(
        conn, "zhang_san",
        {"name": "苏晚棠", "id_type": "护照", "id_no": "E98765432",
         "phone": "138-0000-0004"},
        "USD", "BR001", {usd["10"]: 5}, valid_hours=12)
    code3 = det3["appointment"]["code"]
    A.confirm(conn, code3, "zhang_san")
    DV.deliver(conn, code3, "zhang_san",
               counterfeit={usd["10"]: ["CF-FAKE-0001", "CF-FAKE-0002"]})
    print("  点验发现 2 张假钞疑点：暂扣登记、不出库，转主管处置")
    DV.resolve_counterfeit(conn, code3, "wang_wu",
                           "假钞收缴并按反假币规程上报")
    print(f"  主管处置完成，预约 {A.get_detail(conn, code3)['appointment']['status']}，"
          "锁定库存释放")

    head("⑩ 未到场预约到期释放（第四笔样例，有效期已置为过去）")
    det4 = A.create_reservation(
        conn, "zhang_san",
        {"name": "何未然", "id_type": "护照", "id_no": "E55550001",
         "phone": "138-0000-0005"},
        "GBP", "BR001", {gbp["20"]: 8}, valid_hours=1)
    code4 = det4["appointment"]["code"]
    conn.execute("UPDATE appointments SET valid_until=? WHERE code=?",
                 ((datetime.now() - timedelta(minutes=1)).isoformat(
                     timespec="seconds"), code4))
    conn.commit()
    expired = A.expire_due(conn)
    print(f"  到期扫描：{expired}")
    print(f"  {code4} 状态 {A.get_detail(conn, code4)['appointment']['status']}，"
          "锁定库存已释放")

    head("样例完成")
    print(f"  数据库：{DB_PATH}")
    print(f"  预约共 {conn.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]} 笔")
    if args.serve:
        print(f"\n启动 Web 服务 → http://{args.host}:{args.port}")
        from fxplatform.server import create_server
        srv = create_server(args.host, args.port, DB_PATH)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        print("  追加 --serve 可同时打开 Web 界面（六个页面）")
    conn.close()


if __name__ == "__main__":
    main()
