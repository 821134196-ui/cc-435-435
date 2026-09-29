"""全局配置（虚构演示数据）。"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "fxplatform.db"

# 预约默认/最长有效小时数
DEFAULT_VALID_HOURS = 48
MAX_VALID_HOURS = 24 * 7

# 金额统一保留 4 位小数（汇率相乘用），展示 2 位
MONEY_QUANT = "0.0001"
DISPLAY_QUANT = "0.01"

# 配色（钴蓝 / 银白 / 柠檬黄）
COLOR_COBALT = "#0047AB"
COLOR_SILVER = "#F2F4F7"
COLOR_LEMON = "#FDEE00"

HOST = "127.0.0.1"
PORT = 8000
