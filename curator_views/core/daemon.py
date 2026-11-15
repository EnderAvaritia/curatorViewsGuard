"""守护进程模式：常驻运行，每天到点执行一次每日流程。

替代外部 cron / 任务计划程序的常驻方案：
- 进程启动后进入循环，每次循环**重新加载配置文件**（改配置或改执行时间
  无需重启进程）；
- 每天到达配置的 daily_time（Asia/Shanghai）执行一次每日完整流程
  （抓取-预测-检测-提升-通知）；
- 启动时若今天尚未执行且已过执行时刻，立即补跑一次（守护进程刚启动
  或前一天失败也不影响当天任务）；
- 执行失败不退出进程，也**不把今天标记为已完成**：按配置的间隔在当天重试
  （retry_delay_minutes / retry_max_times），重试用尽后等明天；
- 支持优雅退出：KeyboardInterrupt（Ctrl+C）或 SIGTERM。

调度逻辑（纯函数 plan_next，便于测试）：
    给定当前时间、上次执行日期与每日执行时刻，决定：
    - 是否应立即执行（今天未执行且已到点）；
    - 下一次应唤醒的时刻（今天或明天的 daily_time）。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import status

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

# 执行失败后的默认重试间隔与重试次数（可在配置里用
# retry_delay_minutes / retry_max_times 覆盖）
DEFAULT_RETRY_DELAY_SECONDS = 30 * 60.0
DEFAULT_RETRY_MAX_TIMES = 3


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
            内部应重新加载配置并运行 run_daily）。可返回汇总字典。
        sleeper: 睡眠函数（默认用内部可中断事件等待），测试可注入替身。
        state_path: 状态文件路径；设置后 daemon 会周期性写入运行状态，
            并通过它跨重启保留 last_run_date 等字段。
        fallback_last_run_date: 状态文件里没有 last_run_date 时的兜底值
            （保留给不需要动态刷新的调用方）。
        last_run_date_provider: 每次循环开始调用的回调，返回最新的已完成日期
            （通常查数据库）。用于覆盖「daemon 等待期间手动 run 已执行当天」
            的情况；返回值比内存中的日期新时才会采用。
        retry_settings_provider: 每次失败时调用的回调，返回
            ``(重试间隔秒数, 当天最多重试次数)``；默认 1800 秒 / 3 次。
    """

    def __init__(
        self,
        load_daily_time: Callable[[], str],
        execute: Callable[[], Any],
        sleeper: Optional[Callable[[float], None]] = None,
        install_handlers: bool = True,
        now_fn: Optional[Callable[[], datetime]] = None,
        state_path: Optional[str] = None,
        fallback_last_run_date: str = "",
        last_run_date_provider: Optional[Callable[[], str]] = None,
        retry_settings_provider: Optional[
            Callable[[], tuple[float, int]]
        ] = None,
    ) -> None:
        self._load_daily_time = load_daily_time
        self._execute = execute
        # 可中断的等待：stop() 会立即唤醒，避免等满一个 SLEEP_STEP（30 秒）
        self._wake = threading.Event()
        self._sleep = sleeper or self._wait
        self._now = now_fn or (lambda: datetime.now(_TZ))
        self._stop = False
        self._state_path = state_path
        self._state: Dict[str, Any] = {}

        # 从上次状态文件恢复：last_run_date 用于防止重启后同一天重复执行，
        # last_* 字段用于 info 展示与错误回溯。
        self._last_run_date = ""
        self._last_run_at: Optional[str] = None
        self._last_summary: Any = None
        self._last_error = ""
        if state_path:
            previous = status.read_state(state_path)
            if previous:
                self._last_run_date = str(previous.get("last_run_date") or "")
                self._last_run_at = previous.get("last_run_at")
                self._last_summary = previous.get("last_summary")
                self._last_error = str(previous.get("last_error") or "")
        # 状态文件没记录时用数据库兜底（手动 run 也会写 last_run_date）
        if not self._last_run_date:
            self._last_run_date = fallback_last_run_date

        # 旧版本在执行失败时也会把 last_run_date 记为今天，导致当天不再重试。
        # 状态文件同时满足「今天是 last_run_date / 有错误 / 今天执行过」时，
        # 说明今天那次是失败，按未执行处理；若数据库里确实有今天的成功记录，
        # 循环开始时的外部同步会把它重新采用，不会重复执行。
        today = self._now().strftime("%Y-%m-%d")
        if (
            self._last_run_date == today
            and self._last_error
            and str(self._last_run_at or "").startswith(today)
        ):
            logger.warning(
                "状态文件显示今天执行失败（%s），按未执行处理，今天会重新尝试。",
                self._last_error,
            )
            self._last_run_date = ""

        self._last_run_date_provider = last_run_date_provider
        self._retry_settings = retry_settings_provider or (
            lambda: (DEFAULT_RETRY_DELAY_SECONDS, DEFAULT_RETRY_MAX_TIMES)
        )
        # 当日重试状态：跨天自动清零；失败不写 last_run_date
        self._attempt_date = ""
        self._attempts_today = 0
        self._retry_at: Optional[datetime] = None
        self._gave_up_date = ""

        if install_handlers:
            self._install_signal_handlers()

    def _now_iso(self) -> str:
        """按注入时钟返回 ISO-8601 秒级时间戳（供状态文件使用）。"""
        return self._now().isoformat(timespec="seconds")

    def _write_state(self, **fields: Any) -> None:
        """合并并原子写入状态文件；失败只记警告，绝不影响 daemon 主流程。"""
        if not self._state_path:
            return
        self._state.update(fields)
        self._state.setdefault("pid", os.getpid())
        self._state["updated_at"] = self._now_iso()
        try:
            status.write_state(self._state_path, self._state)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("写入守护进程状态文件失败: %s", exc)

    def _wait(self, timeout: float) -> None:
        """默认等待实现：可被 stop() 立即唤醒的阻塞等待。"""
        self._wake.wait(timeout)

    def stop(self) -> None:
        """请求优雅退出：设置停止标志，并立即唤醒正在睡眠的循环。"""
        self._stop = True
        self._wake.set()

    def _install_signal_handlers(self) -> None:
        """安装 SIGTERM / SIGINT 处理器。

        SIGTERM：优雅退出，等在途执行收尾；
        SIGINT（Ctrl+C）：置停止标志并抛 KeyboardInterrupt，立即中断当前执行
        （否则会被 handler 吞掉，刷量任务要跑完才退出）。
        """

        def _request_stop(signum, frame) -> None:  # noqa: ANN001 - 信号处理器签名
            logger.info("收到信号 %s，准备退出…", signum)
            self.stop()

        def _interrupt(signum, frame) -> None:  # noqa: ANN001 - 信号处理器签名
            logger.info("收到 SIGINT，中断当前执行并退出…")
            self.stop()
            raise KeyboardInterrupt

        try:
            signal.signal(signal.SIGTERM, _request_stop)
            signal.signal(signal.SIGINT, _interrupt)
        except (ValueError, OSError):
            # 非主线程或平台不支持时跳过；默认 SIGINT 仍会抛 KeyboardInterrupt
            pass

    def _sleep_until(
        self,
        target: datetime,
        step: float = SLEEP_STEP,
        heartbeat: Optional[Callable[[], None]] = None,
    ) -> None:
        """分段睡眠到目标时刻，期间定期醒来检查停止标志并刷新心跳。"""
        while not self._stop:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            if heartbeat is not None:
                heartbeat()
            self._sleep(min(step, remaining))

    def run_forever(self, sleep_step: float = SLEEP_STEP) -> None:
        """进入常驻循环：重载配置 → 到点执行 → 睡眠到下次唤醒。"""
        logger.info("守护进程启动，等待每日任务。")
        last_run_date = self._last_run_date
        started = False
        try:
            while not self._stop:
                # 每轮先同步外部记录（数据库）：daemon 等待期间手动 run 过当天，
                # 到点后不应再执行一次。
                if self._last_run_date_provider is not None:
                    try:
                        latest = str(
                            self._last_run_date_provider() or ""
                        ).strip()
                    except Exception:  # noqa: BLE001 - 读状态失败不影响主流程
                        logger.warning(
                            "读取外部 last_run_date 失败，沿用内存值",
                            exc_info=True,
                        )
                    else:
                        if latest > last_run_date:
                            last_run_date = latest
                            self._last_run_date = latest
                # 每次循环重载配置（可能修改了执行时间）
                daily_time = self._load_daily_time()
                now = self._now()

                if not started:
                    started = True
                    self._state.update({
                        "pid": os.getpid(),
                        "started_at": now.isoformat(timespec="seconds"),
                        "phase": "waiting",
                        "daily_time": daily_time,
                        "last_run_date": last_run_date,
                        "last_run_at": self._last_run_at,
                        "last_summary": self._last_summary,
                        "last_error": self._last_error,
                    })
                    self._write_state()

                today_iso = now.strftime("%Y-%m-%d")
                # 跨天：清空当天的重试状态
                if self._attempt_date != today_iso:
                    self._attempt_date = today_iso
                    self._attempts_today = 0
                    self._retry_at = None
                    self._gave_up_date = ""

                should_run, next_wake = plan_next(now, last_run_date, daily_time)
                if should_run and self._gave_up_date == today_iso:
                    # 今天重试用尽：当天不再尝试，等明天
                    should_run = False
                    _, next_wake = plan_next(now, today_iso, daily_time)
                elif (
                    should_run
                    and self._retry_at is not None
                    and now < self._retry_at
                ):
                    # 距下次重试还有一段时间
                    should_run = False
                    next_wake = self._retry_at

                next_wake_iso = next_wake.isoformat(timespec="seconds")
                self._write_state(
                    phase="waiting",
                    daily_time=daily_time,
                    next_wake=next_wake_iso,
                    last_run_date=last_run_date,
                    attempts_today=self._attempts_today,
                )

                if should_run:
                    logger.info("到达每日执行时刻 %s，开始执行。", daily_time)
                    # 执行期间旧的 next_wake（今天已过点）已失效，先清空
                    self._write_state(phase="executing", next_wake=None)
                    summary: Any = None
                    error: Optional[str] = None
                    try:
                        result = self._execute()
                        if isinstance(result, dict):
                            summary = result
                    except Exception as exc:  # noqa: BLE001 - 单次失败不退出进程
                        error = str(exc)
                        logger.exception("每日执行失败: %s", exc)

                    if error is None:
                        # 只有成功才推进 last_run_date，避免失败吃掉当天
                        last_run_date = self._now().strftime("%Y-%m-%d")
                        self._last_run_date = last_run_date
                        self._last_run_at = self._now_iso()
                        if summary is not None:
                            self._last_summary = summary
                        self._last_error = ""
                        self._retry_at = None
                        self._attempts_today = 0
                    else:
                        self._last_error = error
                        self._attempts_today += 1
                        delay, max_times = self._retry_settings()
                        if delay <= 0 or self._attempts_today > max_times:
                            self._gave_up_date = self._now().strftime("%Y-%m-%d")
                            self._retry_at = None
                            logger.error(
                                "今日已失败 %d 次（重试上限 %d），放弃今天的执行；"
                                "不会标记为已完成，可手动 run 或等明天。",
                                self._attempts_today, max_times,
                            )
                        else:
                            self._retry_at = self._now() + timedelta(seconds=delay)
                            logger.warning(
                                "第 %d 次失败，%s 重试（今日最多重试 %d 次）",
                                self._attempts_today,
                                self._retry_at.strftime("%Y-%m-%d %H:%M:%S"),
                                max_times,
                            )

                    _, next_wake = plan_next(
                        self._now(), last_run_date, daily_time,
                    )
                    if self._retry_at is not None:
                        next_wake = min(next_wake, self._retry_at)
                    next_wake_iso = next_wake.isoformat(timespec="seconds")
                    self._write_state(
                        phase="waiting",
                        daily_time=daily_time,
                        next_wake=next_wake_iso,
                        last_run_date=last_run_date,
                        last_run_at=self._last_run_at,
                        last_summary=self._last_summary,
                        last_error=self._last_error,
                        attempts_today=self._attempts_today,
                    )
                    if error is None:
                        logger.info("今日任务完成，下次执行: %s", next_wake)
                    else:
                        logger.info("今日任务未完成，下次尝试: %s", next_wake)
                else:
                    if self._retry_at is not None:
                        reason = (
                            f"等待第 {self._attempts_today} 次失败后的重试"
                        )
                    elif self._gave_up_date == today_iso:
                        reason = "今日重试已用尽，等待明天"
                    else:
                        reason = f"今日无需执行（daily_time={daily_time}）"
                    logger.info(
                        "%s，下次执行: %s",
                        reason,
                        next_wake.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    )

                if self._stop:
                    break
                self._sleep_until(
                    next_wake,
                    sleep_step,
                    heartbeat=lambda: self._write_state(
                        phase="waiting",
                        daily_time=daily_time,
                        next_wake=next_wake_iso,
                        last_run_date=last_run_date,
                    ),
                )

        finally:
            # 正常退出或 Ctrl+C 中断都要落盘 stopped，避免 info 残留 executing
            self._write_state(phase="stopped", stopped_at=self._now_iso())
        logger.info("守护进程已退出。")
