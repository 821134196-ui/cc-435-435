"""外部能力的模拟适配器：本地汇率发布、现金点验设备。

生产环境可替换为真实行情接口 / 点钞机驱动，服务层只依赖这两个协议。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

# ------------------------------------------------------------------- FX 适配器


class FxAdapter:
    """本地模拟汇率源。

    实际账务汇率以 fx_rates 表中发布的版本为准；适配器仅负责“生成建议牌价”，
    由主管（supervisor）确认发布，保留发布链路可审计。
    """

    # 虚构的中间价基准；系统在其附近做确定性抖动生成新版本
    REFERENCE = {
        "USD": "7.1156",
        "EUR": "7.7805",
        "JPY": "0.0475",
        "HKD": "0.9156",
        "GBP": "9.1022",
    }

    def suggest_rate(self, currency: str, when: datetime | None = None) -> Decimal:
        when = when or datetime.now()
        base = Decimal(self.REFERENCE.get(currency, "1.0000"))
        digest = hashlib.sha256(
            f"{currency}-{when.date().isoformat()}".encode()
        ).hexdigest()
        # 确定性 ±0.3% 内抖动，当日多次取同一基准（不随时刻变化）
        bump = int(digest[:6], 16) % 600 - 300  # -300..299 个基点的 1/100000
        return (base * (Decimal(1) + Decimal(bump) / Decimal(100000))).quantize(
            Decimal("0.0001")
        )


# --------------------------------------------------------------- 现金点验适配器


class CounterfeitSuspicion(Exception):
    """点验发现假钞疑点（张号被设备标记）。"""

    def __init__(self, serials: list[str], denomination_id: int):
        self.serials = serials
        self.denomination_id = denomination_id
        super().__init__(
            f"券别 {denomination_id} 发现 {len(serials)} 张假钞疑点: "
            + ",".join(serials)
        )


class CashCountAdapter:
    """本地模拟点钞机。

    - count() 返回每券别实点张数与冠字号；
    - 通过 preset 可预置“差额/假钞”场景，便于演示与测试；
    - 同一会话重复点验结果稳定（交付幂等）。
    """

    def __init__(self) -> None:
        # session_id -> {denomination_id: {"qty": n, "bad_serials": [...]}}
        self._sessions: dict[str, dict] = {}

    def open_session(self, session_id: str, preset: dict | None = None) -> None:
        """预置一次点验结果。

        preset: {denomination_id: {"qty": int, "bad_serials": [str,...]}}
        """
        self._sessions[session_id] = dict(preset or {})

    def count(
        self, session_id: str, denomination_id: int, expected_qty: int
    ) -> dict:
        """点验某券别。未预置时按预约数量全额点足。"""
        sess = self._sessions.get(session_id)
        if sess is not None and denomination_id in sess:
            entry = sess[denomination_id]
            qty = int(entry["qty"])
            bad = list(entry.get("bad_serials", []))
        else:
            qty = int(expected_qty)
            bad = []
        serials = self._serials(session_id, denomination_id, qty)
        if bad:
            raise CounterfeitSuspicion(bad, denomination_id)
        return {"denomination_id": denomination_id, "qty": qty, "serials": serials}

    def _serials(self, session_id: str, denomination_id: int, qty: int) -> list[str]:
        seed = hashlib.sha256(
            f"{session_id}:{denomination_id}".encode()
        ).hexdigest()[:8].upper()
        return [f"FX{seed}{i:04d}" for i in range(1, qty + 1)]


fx_adapter = FxAdapter()
cash_adapter = CashCountAdapter()
