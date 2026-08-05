"""推送通知模块：通过 ntfy 服务发送通知，未配置时退化为日志记录。

本模块只做一件事：把"标题 + 消息"投递出去（ntfy 或日志），
不包含任何业务逻辑，是一个薄抽象层。
通知失败永远不会抛异常，只记录 warning 日志。
"""

import logging
from typing import Dict, Optional

import requests

logger = logging.getLogger("curator_views.notifier")

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

_NTFY_TIMEOUT = 10  # 秒


class Notifier:
    """ntfy 推送通知的薄抽象。

    - 同时配置了 ntfy_url 与 ntfy_topic 时，向 ``{ntfy_url}/{ntfy_topic}``
      POST 消息，params 携带 title 与 priority；
    - 未配置时，将通知内容按级别写入日志，保证调用方无需关心推送是否可用。
    """

    def __init__(self, ntfy_url: str = "", ntfy_topic: str = "") -> None:
        self.ntfy_url = ntfy_url
        self.ntfy_topic = ntfy_topic
        self._enabled = bool(ntfy_url and ntfy_topic)

    def send(self, title: str, message: str, level: str = "info") -> None:
        """发送一条通知。

        Args:
            title: 通知标题。
            message: 通知正文。
            level: 通知级别，取值 'info' / 'warning' / 'error'，
                分别映射到 ntfy priority 的 default / high / urgent。
        """
        if self._enabled:
            self._send_via_ntfy(title, message, level)
        else:
            self._send_via_log(title, message, level)

    def _send_via_ntfy(self, title: str, message: str, level: str) -> None:
        """通过 ntfy 的 POST 接口推送；任何失败仅记录 warning，绝不抛出。"""
        priority = _LEVEL_PRIORITY.get(level, "default")
        url = f"{self.ntfy_url}/{self.ntfy_topic}"
        try:
            requests.post(
                url,
                data=message.encode("utf-8"),
                params={"title": title, "priority": priority},
                timeout=_NTFY_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败不允许上抛
            logger.warning("ntfy 推送失败 (url=%s): %s", url, exc)

    def _send_via_log(self, title: str, message: str, level: str) -> None:
        """未配置 ntfy 时，把通知内容写入本地日志。"""
        log_level = _LOG_LEVEL.get(level, logging.INFO)
        logger.log(log_level, "[%s] %s", title, message)
