"""每日分界点（day boundary）工具。

Steam 鉴赏家后台的每日统计并不按本地自然日 0 点切分：北京时间下通常在
早上 8 点（UTC 0 点）切换。若在 0:00~8:00 之间执行刷量，这部分访问会被
Steam 计入前一天；程序若仍按自然日记账，就会把刷量记到错误的日期，导致
“昨天刷的量没算进昨天、今天的目标又被重复扣减”。

本模块把“本地时间减去分界偏移”后的日期作为**逻辑日**（logical day）：
分界点为 ``08:00`` 时，08:00 之前仍属于前一天。所有需要“今天/昨天”语义
的地方都应调用这里的函数，而不是直接使用自然日。

分界点支持自定义（``day_boundary`` 配置）：只需与 Steam 实际切换统计的
时刻一致；填 ``"00:00"`` 即恢复按自然日切分的旧行为。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("curator_views.daytime")

try:
    # 首选系统时区数据库（tzdata 包）
    _TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # 精简环境（如未安装 tzdata 的 Windows）回退到固定 UTC+8。
    # Asia/Shanghai 无夏令时，固定偏移在语义上等价。
    _TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# Steam 后台统计的默认切换时刻（北京时间 08:00 = UTC 00:00）
DEFAULT_DAY_BOUNDARY = "08:00"
DEFAULT_DAY_BOUNDARY_MINUTES = 8 * 60

_MINUTES_PER_DAY = 24 * 60


def parse_day_boundary(value: Any = None) -> int:
    """把配置里的每日分界点解析为“零点后的分钟数”。

    支持以下写法：

    - ``"08:00"`` / ``"8:00"``（推荐，格式 ``HH:MM``）；
    - ``8`` / ``"8"``（按整点小时处理，0~23）；
    - ``8.5``（按小时的小数形式处理，等价于 08:30）。

    ``None`` / 空串表示未配置，静默回退到默认值 08:00。格式非法时记录
    warning 并回退到默认值，避免一个笔误让整轮任务无法执行。

    Args:
        value: 配置文件中的 ``day_boundary`` 值。

    Returns:
        零点后的分钟数（0~1439）。

    Raises:
        无。非法输入一律回退默认值。
    """
    if value is None:
        return DEFAULT_DAY_BOUNDARY_MINUTES
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return DEFAULT_DAY_BOUNDARY_MINUTES
        if ":" in text:
            parts = text.split(":")
            if len(parts) != 2:
                logger.warning(
                    "day_boundary=%r 格式应为 HH:MM，已回退到 %s。",
                    value, DEFAULT_DAY_BOUNDARY,
                )
                return DEFAULT_DAY_BOUNDARY_MINUTES
            hour_text, minute_text = parts
            try:
                hour = int(hour_text)
                minute = int(minute_text)
            except ValueError:
                logger.warning(
                    "day_boundary=%r 不是有效时刻，已回退到 %s。",
                    value, DEFAULT_DAY_BOUNDARY,
                )
                return DEFAULT_DAY_BOUNDARY_MINUTES
        else:
            try:
                hour = int(text)
            except ValueError:
                logger.warning(
                    "day_boundary=%r 不是有效时刻，已回退到 %s。",
                    value, DEFAULT_DAY_BOUNDARY,
                )
                return DEFAULT_DAY_BOUNDARY_MINUTES
            minute = 0
    elif isinstance(value, bool):
        # bool 是 int 的子类，避免把 true/false 当成 1/0 点
        logger.warning(
            "day_boundary=%r 不是有效时刻，已回退到 %s。",
            value, DEFAULT_DAY_BOUNDARY,
        )
        return DEFAULT_DAY_BOUNDARY_MINUTES
    elif isinstance(value, (int, float)):
        if 0 <= float(value) < 24:
            total = int(round(float(value) * 60))
            hour, minute = divmod(total, 60)
        else:
            logger.warning(
                "day_boundary=%r 超出 0~24 小时范围，已回退到 %s。",
                value, DEFAULT_DAY_BOUNDARY,
            )
            return DEFAULT_DAY_BOUNDARY_MINUTES
    else:
        logger.warning(
            "day_boundary=%r 不是有效时刻，已回退到 %s。",
            value, DEFAULT_DAY_BOUNDARY,
        )
        return DEFAULT_DAY_BOUNDARY_MINUTES

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        logger.warning(
            "day_boundary=%r 超出 00:00~23:59 范围，已回退到 %s。",
            value, DEFAULT_DAY_BOUNDARY,
        )
        return DEFAULT_DAY_BOUNDARY_MINUTES
    return hour * 60 + minute


def format_day_boundary(boundary_minutes: int) -> str:
    """把分钟数格式化回 ``HH:MM``，用于日志与状态展示。"""
    minutes = int(boundary_minutes) % _MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def resolve_day_boundary(config: Any) -> int:
    """从配置字典解析每日分界点（分钟）。

    优先使用已解析的 ``day_boundary_minutes``（0~1439 的整数，内部配置键），
    否则解析用户配置的 ``day_boundary``（``"08:00"`` 等）。两者都缺失或
    非法时回退到默认 08:00。
    """
    if isinstance(config, dict) and config.get("day_boundary_minutes") is not None:
        try:
            minutes = int(config["day_boundary_minutes"])
        except (TypeError, ValueError):
            minutes = -1
        if 0 <= minutes < _MINUTES_PER_DAY:
            return minutes
        logger.warning(
            "day_boundary_minutes=%r 不在 0~1439 范围内，已回退到默认分界点 %s。",
            config.get("day_boundary_minutes"), DEFAULT_DAY_BOUNDARY,
        )
        return DEFAULT_DAY_BOUNDARY_MINUTES
    if isinstance(config, dict):
        return parse_day_boundary(config.get("day_boundary"))
    return parse_day_boundary(config)


def logical_datetime(
    now: datetime, boundary_minutes: int = DEFAULT_DAY_BOUNDARY_MINUTES
) -> datetime:
    """返回 ``now`` 所属逻辑日的“分界时刻”。

    例如分界点为 08:00 时，``2026-11-17 03:00`` 的逻辑日是 11-16，
    返回 ``2026-11-16 08:00``；``2026-11-17 09:00`` 返回
    ``2026-11-17 08:00``。返回值的日期部分即逻辑日。
    """
    return now - timedelta(minutes=int(boundary_minutes))


def logical_date(
    now: datetime, boundary_minutes: int = DEFAULT_DAY_BOUNDARY_MINUTES
) -> str:
    """返回 ``now`` 所属逻辑日的 ``YYYY-MM-DD``。"""
    return logical_datetime(now, boundary_minutes).strftime("%Y-%m-%d")


def day_label_from_ts(date_ts: int) -> str:
    """把每日统计的 ``date_ts`` 转为 ``YYYY-MM-DD`` 日期标签。

    Steam 返回的 ``date_ts`` 建议按本地日期读取标签（北京时间 08:00 =
    UTC 00:00，格式化后日期与统计标签一致）。本函数只做日期标签转换，
    不参与“今天”的判定。
    """
    return datetime.fromtimestamp(int(date_ts), tz=_TZ).strftime("%Y-%m-%d")


def previous_date(date_str: str) -> str:
    """返回 ``YYYY-MM-DD`` 的前一天（用于“昨天”标签计算）。"""
    parsed = datetime.strptime(date_str, "%Y-%m-%d")
    return (parsed - timedelta(days=1)).strftime("%Y-%m-%d")