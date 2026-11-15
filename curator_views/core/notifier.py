"""推送通知模块：通过 ntfy 服务发送通知，未启用时退化为日志记录。

本模块只做一件事：把"标题 + 消息"投递出去（ntfy 或日志），
不包含任何业务逻辑，是一个薄抽象层。
通知失败永远不会抛异常，只记录 warning 日志。

在基础投递之上提供四项过滤/缓冲能力：

- **总开关**：``enabled`` 关闭或 ntfy 地址/主题缺失时只写日志；
- **最低级别**：低于 ``min_level`` 的通知直接丢弃（默认 info，即不过滤）；
- **每日简报开关**：``daily_summary`` 为 False 时只抑制例行（info 级）简报，
  warning / error 级的简报与告警仍照常送达；
- **静默时段**：命中 ``quiet_hours`` 时把通知写入 JSON 队列，
  等离开静默时段后由 ``flush_pending`` 补发，避免夜间打扰。

队列文件损坏或读写失败时只记录 warning，绝不打断 run / daemon 主流程。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

import requests

from . import status

logger = logging.getLogger("curator_views.notifier")

# 合法通知级别（按严重程度升序）
LEVELS = ("info", "warning", "error")

# 通知类别：日常简报 / 异常告警
CATEGORY_DAILY_SUMMARY = "daily_summary"
CATEGORY_ALERT = "alert"

# 队列最多保留的待发通知条数（超出时丢弃最旧的）
MAX_PENDING = 50

# 通知级别 -> ntfy priority 映射
# info -> default, warning -> high, error -> urgent
_LEVEL_PRIORITY: Dict[str, str] = {
    "info": "default",
    "warning": "high",
    "error": "urgent",
}

# 通知级别 -> 本地日志级别映射（未配置 ntfy 时使用）
_LOG_LEVEL: Dict[str, int] = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# 通知级别 -> 数值等级，用于最低级别过滤
_LEVEL_RANK: Dict[str, int] = {
    level: index for index, level in enumerate(LEVELS)
}

_NTFY_TIMEOUT = 10  # 秒

# 静默时段条目的格式：HH:MM-HH:MM
_QUIET_TOKEN_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")

# 静默时段分隔符：逗号、分号或空白
_QUIET_SEPARATOR_RE = re.compile(r"[,;\s]+")


def parse_quiet_ranges(value: str) -> list[tuple[int, int]]:
    """解析静默时段配置，返回 ``(开始分钟, 结束分钟)`` 列表。

    支持 ``""``、``"23:00-08:00"``、``"12:00-13:00,23:00-08:00"`` 等形式，
    逗号、分号与空白均可作为分隔符。非法条目（格式错误、越界、缺横线）
    记录 warning 后跳过；起止相同的条目语义不明确，同样跳过。
    """
    ranges: list[tuple[int, int]] = []
    if not isinstance(value, str) or not value.strip():
        return ranges

    for token in _QUIET_SEPARATOR_RE.split(value.strip()):
        if not token:
            continue
        match = _QUIET_TOKEN_RE.match(token)
        if match is None:
            logger.warning("静默时段条目 %r 无效，已忽略。", token)
            continue
        start_hour, start_minute, end_hour, end_minute = (
            int(part) for part in match.groups()
        )
        if start_hour > 23 or end_hour > 23:
            logger.warning("静默时段条目 %r 无效，已忽略。", token)
            continue
        if start_minute > 59 or end_minute > 59:
            logger.warning("静默时段条目 %r 无效，已忽略。", token)
            continue
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start == end:
            logger.warning("静默时段条目 %r 起止相同，已忽略。", token)
            continue
        ranges.append((start, end))
    return ranges


def in_quiet_hours(now: datetime, ranges: Sequence[tuple[int, int]]) -> bool:
    """判断给定时刻是否落在任一静默时段内（开始含、结束不含）。"""
    if not ranges:
        return False
    current = now.hour * 60 + now.minute
    for start, end in ranges:
        if start < end:
            # 同日区间：[start, end)
            if start <= current < end:
                return True
        else:
            # 跨午夜区间：[start, 24:00) ∪ [00:00, end)
            if current >= start or current < end:
                return True
    return False


class Notifier:
    """ntfy 推送通知的薄抽象。

    - 同时配置了 ntfy_url 与 ntfy_topic 且 ``enabled`` 为 True 时，向
      ``{ntfy_url}/{ntfy_topic}`` POST 消息，params 携带 title 与 priority；
    - 未启用时，将通知内容按级别写入日志，保证调用方无需关心推送是否可用；
    - 静默时段内改为写入本地 JSON 队列，离开静默时段后自动补发。
    """

    def __init__(
        self,
        ntfy_url: str = "",
        ntfy_topic: str = "",
        *,
        enabled: bool = True,
        min_level: str = "info",
        daily_summary: bool = True,
        quiet_hours: str = "",
        queue_path: str = "",
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.ntfy_url = ntfy_url
        self.ntfy_topic = ntfy_topic
        self.queue_path = queue_path
        self.daily_summary = bool(daily_summary)
        self._enabled = bool(enabled and ntfy_url and ntfy_topic)

        normalized = str(min_level).strip().lower()
        if normalized not in LEVELS:
            logger.warning(
                "通知最低级别 %r 无效，回退为 info。", min_level
            )
            normalized = "info"
        self.min_level = normalized

        self._ranges = parse_quiet_ranges(quiet_hours)
        self._now = now_fn or status.now

    @property
    def enabled(self) -> bool:
        """是否真正启用了 ntfy 推送（总开关与地址/主题都满足）。"""
        return self._enabled

    def send(
        self,
        title: str,
        message: str,
        level: str = "info",
        category: str = CATEGORY_ALERT,
    ) -> None:
        """发送一条通知。

        Args:
            title: 通知标题。
            message: 通知正文。
            level: 通知级别，取值 'info' / 'warning' / 'error'，
                分别映射到 ntfy priority 的 default / high / urgent。
            category: 通知类别，日常简报用 ``daily_summary``，
                异常告警用 ``alert``（默认）。
        """
        level = self._normalize_level(level)

        if not self._enabled:
            self._send_via_log(title, message, level)
            return

        if (
            category == CATEGORY_DAILY_SUMMARY
            and not self.daily_summary
            and level == "info"
        ):
            logger.info(
                "每日简报推送已关闭（ntfy_daily_summary=false）：%s", title
            )
            return

        if _LEVEL_RANK[level] < _LEVEL_RANK[self.min_level]:
            logger.info(
                "通知被级别过滤（%s < %s）：%s", level, self.min_level, title
            )
            return

        if in_quiet_hours(self._now(), self._ranges):
            queued = self._enqueue(title, message, level, category)
            logger.info(
                "静默时段，通知已排队（%d 条待发）：%s", queued, title
            )
            return

        # 先补发积压，再投递当前通知（尽力而为，失败不阻断当前条）
        self.flush_pending()
        self._post(title, message, level)

    def flush_pending(self) -> int:
        """补发静默时段积压的通知，返回成功投递的条数。

        仍处于静默时段时保持队列原样返回 0；投递中途失败时停止，
        保留失败条目及其后的所有条目。任何文件读写异常都不会上抛。
        """
        if not self._enabled or not self.queue_path:
            return 0

        try:
            items = self._load_queue()
        except (OSError, TypeError, ValueError):
            logger.warning(
                "读取通知队列失败 (path=%s)", self.queue_path, exc_info=True
            )
            return 0
        if not items:
            return 0

        if in_quiet_hours(self._now(), self._ranges):
            return 0

        sent = 0
        remaining = list(items)
        while remaining:
            item = remaining[0]
            if not self._post(
                item["title"], item["message"], item["level"]
            ):
                break
            remaining.pop(0)
            sent += 1

        try:
            self._save_queue(remaining)
        except (OSError, TypeError, ValueError):
            logger.warning(
                "保存通知队列失败 (path=%s)", self.queue_path, exc_info=True
            )
            return sent

        if sent > 0:
            logger.info("补发 %d 条延迟通知。", sent)
        return sent

    def _normalize_level(self, level: Any) -> str:
        """把级别规范化为 LEVELS 之一；非法值记录 warning 并回退 info。"""
        text = str(level).strip().lower()
        if text not in LEVELS:
            logger.warning("通知级别 %r 无效，按 info 处理。", level)
            return "info"
        return text

    def _post(self, title: str, message: str, level: str) -> bool:
        """通过 ntfy 的 POST 接口推送；成功返回 True，失败仅记录 warning。"""
        url = f"{self.ntfy_url}/{self.ntfy_topic}"
        try:
            requests.post(
                url,
                data=message.encode("utf-8"),
                params={
                    "title": title,
                    "priority": _LEVEL_PRIORITY[level],
                },
                timeout=_NTFY_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败不允许上抛
            logger.warning("ntfy 推送失败 (url=%s): %s", url, exc)
            return False
        return True

    def _send_via_log(self, title: str, message: str, level: str) -> None:
        """未配置 ntfy 时，把通知内容写入本地日志。"""
        logger.log(_LOG_LEVEL[level], "[%s] %s", title, message)

    def _load_queue(self) -> list[Dict[str, Any]]:
        """读取并校验队列文件；文件缺失或损坏时返回空列表。"""
        if not self.queue_path:
            return []
        data = status.read_state(self.queue_path)
        if not data:
            return []
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return []

        items: list[Dict[str, Any]] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                logger.warning("通知队列条目格式无效，已忽略：%r", entry)
                continue
            title = entry.get("title")
            message = entry.get("message")
            if not isinstance(title, str) or not title:
                logger.warning("通知队列条目缺少有效标题，已忽略：%r", entry)
                continue
            if not isinstance(message, str):
                logger.warning("通知队列条目缺少有效正文，已忽略：%r", entry)
                continue
            category = entry.get("category")
            if not isinstance(category, str) or not category:
                category = CATEGORY_ALERT
            created_at = entry.get("created_at")
            if not isinstance(created_at, str):
                created_at = ""
            items.append(
                {
                    "title": title,
                    "message": message,
                    "level": self._normalize_level(
                        entry.get("level") or "info"
                    ),
                    "category": category,
                    "created_at": created_at,
                }
            )
        return items

    def _save_queue(self, items: list[Dict[str, Any]]) -> None:
        """把队列写回文件；列表为空时删除文件。"""
        if not self.queue_path:
            return
        if not items:
            try:
                Path(self.queue_path).unlink()
            except OSError:
                # 文件不存在或删除失败都不影响主流程
                pass
            return
        status.write_state(
            self.queue_path, {"version": 1, "items": items}
        )

    def _enqueue(
        self, title: str, message: str, level: str, category: str
    ) -> int:
        """把通知追加到队列并裁剪长度，返回入队后的队列条数。"""
        try:
            items = self._load_queue()
        except (OSError, TypeError, ValueError):
            logger.warning(
                "读取通知队列失败 (path=%s)", self.queue_path, exc_info=True
            )
            items = []

        items.append(
            {
                "title": title,
                "message": message,
                "level": level,
                "category": category,
                "created_at": self._now().isoformat(timespec="seconds"),
            }
        )

        if len(items) > MAX_PENDING:
            dropped = len(items) - MAX_PENDING
            logger.warning(
                "通知队列超过 %d 条，丢弃最旧的 %d 条。",
                MAX_PENDING,
                dropped,
            )
            items = items[-MAX_PENDING:]

        try:
            self._save_queue(items)
        except (OSError, TypeError, ValueError):
            logger.warning(
                "保存通知队列失败 (path=%s)", self.queue_path, exc_info=True
            )
        return len(items)