from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ..config import DISPLAY_QUANT, MONEY_QUANT


def D(value) -> Decimal:
    """把字符串/数字安全转为 Decimal。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value) -> Decimal:
    return D(value).quantize(Decimal(MONEY_QUANT), rounding=ROUND_HALF_UP)


def display_money(value) -> str:
    """金额列固定 2 位小数。"""
    return f"{D(value).quantize(Decimal(DISPLAY_QUANT), rounding=ROUND_HALF_UP):,.2f}"


def display_qty(value) -> str:
    """数量列固定 0 位（张/枚），带千分位。"""
    return f"{int(value):,}"
