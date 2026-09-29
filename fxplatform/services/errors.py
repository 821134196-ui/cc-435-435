"""领域异常。"""
from __future__ import annotations


class PlatformError(Exception):
    """业务规则错误（返回 4xx，携带可读中文信息）。"""

    code = "business_error"


class InventoryShortage(PlatformError):
    code = "inventory_shortage"

    def __init__(self, shortages: list[dict]):
        self.shortages = shortages
        super().__init__(
            "可用库存不足: "
            + "; ".join(
                f"{s['currency_code']} {s['face_value']} 需 {s['need']} 张，"
                f"可用 {s['available']} 张"
                for s in shortages
            )
        )


class ConcurrentLock(PlatformError):
    code = "concurrent_lock"

    def __init__(self, message: str = "库存正被另一笔交易占用，请重试"):
        super().__init__(message)


class InvalidState(PlatformError):
    code = "invalid_state"


class PermissionDenied(PlatformError):
    code = "permission_denied"


class RateUnavailable(PlatformError):
    code = "rate_unavailable"


class ReconfirmRequired(PlatformError):
    code = "reconfirm_required"

    def __init__(self, message: str, appointment_id: int):
        self.appointment_id = appointment_id
        super().__init__(message)


class IdempotentReplay(PlatformError):
    """重复提交同一交付（幂等：返回已存在结果而不是新建）。"""

    code = "idempotent_replay"
