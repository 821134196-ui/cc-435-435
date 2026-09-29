"""模拟适配器：本地汇率源 + 现钞点验/鉴伪。

真实环境中两者会对接核心系统与点验钞机；这里用确定性的本地模拟，
保证演示与测试可复现（无随机副作用，通过显式参数注入异常情形）。
"""
from __future__ import annotations

from dataclasses import dataclass


class FxAdapter:
    """本地汇率适配器：只负责从版本表读取牌价，不做业务决策。

    接受 sqlite3.Connection 或返回连接的 callable（多线程场景）。
    """

    def __init__(self, conn_or_provider):
        if callable(conn_or_provider) and not hasattr(conn_or_provider, "execute"):
            self._provider = conn_or_provider
        else:
            self._provider = lambda: conn_or_provider

    @property
    def conn(self):
        return self._provider()

    def version_for(self, currency: str, eff_date: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM rate_versions WHERE currency=? AND eff_date<=? "
            "ORDER BY eff_date DESC, id DESC LIMIT 1",
            (currency, eff_date),
        ).fetchone()
        return dict(row) if row else None

    def latest(self, currency: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM rate_versions WHERE currency=? "
            "ORDER BY eff_date DESC, id DESC LIMIT 1",
            (currency,),
        ).fetchone()
        return dict(row) if row else None


@dataclass
class CountResult:
    denom_id: int
    booked_qty: int      # 预约应有张数
    counted_qty: int     # 实点张数
    diff_qty: int        # 实点 - 预约
    suspicious: bool     # 是否存在假钞疑点
    serials: list[str]   # 虚构钞券冠字号


class CashValidatorAdapter:
    """现钞点验适配器（模拟点验钞机）。

    - 默认实点 = 预约数量，无差额、无疑点；
    - overrides: {denom_id: {"counted": n, "suspicious": bool}} 注入差额/假钞场景；
    - 冠字号为虚构序列，仅用于核验留痕。
    """

    def __init__(self, overrides: dict | None = None):
        self.overrides = overrides or {}

    def count(self, denom_id: int, booked_qty: int) -> CountResult:
        ov = self.overrides.get(denom_id, {})
        counted = int(ov.get("counted", booked_qty))
        suspicious = bool(ov.get("suspicious", False))
        serials = [f"FX{denom_id:03d}-{i:04d}*" for i in range(1, min(counted, 5) + 1)]
        return CountResult(
            denom_id=denom_id,
            booked_qty=booked_qty,
            counted_qty=counted,
            diff_qty=counted - booked_qty,
            suspicious=suspicious,
            serials=serials,
        )
