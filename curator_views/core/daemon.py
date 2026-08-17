"""守护进程模式：常驻运行，每天到点执行一次每日流程。

替代外部 cron / 任务计划程序的常驻方案：
- 进程启动后进入循环，每次循环**重新加载配置文件**（改配置或改执行时间
  无需重启进程）；
- 每天到达配置的 daily_time（Asia/Shanghai）执行一次每日完整流程
  （抓取-预测-检测-提升-通知）；
- 启动时若今天尚未执行且已过执行时刻，立即补跑一次（守护进程刚启动
  或前一天失败也不影响当天任务）；
- 单次执行失败不退出进程：记录异常后等待下一天；
- 支持优雅退出：KeyboardInterrupt（Ctrl+C）或 SIGTERM。

调度逻辑（纯函数 plan_next，便于测试）：
    给定当前时间、上次执行日期与每日执行时刻，决定：
    - 是否应立即执行（今天未执行且已到点）；
    - 下一次应唤醒的时刻（今天或明天的 daily_time）。
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("curator_views.daemon")

try:
    # 首选系统时区数据库（tzdata 包）
    _TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # 精简环境（如未安装 tzdata 的 Windows）回退到固定 UTC+8。
    # Asia/Shanghai 无夏令时，固定偏移在语义上等价。
    _TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 分段睡眠步长（秒）：即使目标是几小时后，也定期醒来检查退出标志
SLEEP_STEP = 30.0


def parse_daily_time(daily_time: str) -> tuple[int, int]:
    """解析 "HH:MM" 为 (hour, minute)；非法时回退到 10:00 并警告。"""
    try:
        hour, minute = (int(part) for part in daily_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except (ValueError, AttributeError):
        logger.warning("daily_time=%r 无效，回退到 10:00。", daily_time)
        return 10, 0


def plan_next(
    now: datetime,
    last_run_date: str,
    daily_time: str,
) -> tuple[bool, datetime]:
    """根据当前时间决定是否立即执行与下一次唤醒时刻。

    Args:
        now: 当前时间（带时区）。
        last_run_date: 上次执行日期（YYYY-MM-DD）；空串表示从未执行。
        daily_time: 每日执行时刻，格式 "HH:MM"。

    Returns:
        (should_execute, next_wake)：
        - should_execute: 今天尚未执行且已到执行时刻（含启动补跑）；
        - next_wake: 下一次需要唤醒的时刻（今天或明天的 daily_time）。
    """
    hour, minute = parse_daily_time(daily_time)
    today_iso = now.strftime("%Y-%m-%d")
    today_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if last_run_date == today_iso:
        # 今天已执行 → 等到明天同一时刻
        return False, today_run + timedelta(days=1)
    if now >= today_run:
        # 今天未执行且已到点 → 立即执行；下次唤醒明天
        return True, today_run + timedelta(days=1)
    # 今天尚未到点 → 等到今天
    return False, today_run


class Daemon:
    """每日定时守护进程：循环重载配置、到点执行、失败不退出。

    Args:
        load_daily_time: 每次循环调用，返回当前配置的每日执行时刻
            （"HH:MM"）。通过它实现配置热重载——改配置无需重启。
        execute: 执行一次每日完整流程的回调（由调用方实现，
            内部应重新加载配置并运行 run_daily）。
        sleeper: 睡眠函数（默认 time.sleep），测试可注入替身。
    """

    def __init__(
        self,
        load_daily_time: Callable[[], str],
        execute: Callable[[], None],
        sleeper: Optional[Callable[[float], None]] = None,
        install_handlers: bool = True,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._load_daily_time = load_daily_time
        self._execute = execute
        self._sleep = sleeper or time.sleep
        self._now = now_fn or (lambda: datetime.now(_TZ))
        self._stop = False
        if install_handlers:
            self._install_signal_handlers()

    def stop(self) -> None:
        """请求优雅退出：设置停止标志，循环与睡眠会尽快响应。"""
        self._stop = True

    def _install_signal_handlers(self) -> None:
        """安装 SIGTERM / SIGINT 处理器，设置停止标志以便优雅退出。"""

        def _request_stop(signum, frame) -> None:  # noqa: ANN001 - 信号处理器签名
            logger.info("收到信号 %s，准备退出…", signum)
            self.stop()

        try:
            signal.signal(signal.SIGTERM, _request_stop)
            signal.signal(signal.SIGINT, _request_stop)
        except (ValueError, OSError):
            # 非主线程或平台不支持时跳过；KeyboardInterrupt 仍可中断 sleep
            pass

    def _sleep_until(self, target: datetime, step: float = SLEEP_STEP) -> None:
        """分段睡眠到目标时刻，期间定期醒来检查停止标志。"""
        while not self._stop:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(step, remaining))

    def run_forever(self, sleep_step: float = SLEEP_STEP) -> None:
        """进入常驻循环：重载配置 → 到点执行 → 睡眠到下次唤醒。"""
        logger.info("守护进程启动，等待每日任务。")
        last_run_date = ""
        while not self._stop:
            # 每次循环重载配置（可能修改了执行时间）
            daily_time = self._load_daily_time()
            now = self._now()
            should_run, next_wake = plan_next(now, last_run_date, daily_time)

            if should_run:
                logger.info("到达每日执行时刻 %s，开始执行。", daily_time)
                try:
                    self._execute()
                except Exception as exc:  # noqa: BLE001 - 单次失败不退出进程
                    logger.exception("每日执行失败，等待下一天继续: %s", exc)
                last_run_date = self._now().strftime("%Y-%m-%d")
                _, next_wake = plan_next(
                    self._now(), last_run_date, daily_time,
                )
                logger.info("今日任务完成，下次执行: %s", next_wake)

            if self._stop:
                break
            self._sleep_until(next_wake, sleep_step)

        logger.info("守护进程已退出。")
