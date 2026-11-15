"""守护进程模式：常驻运行，每天到达配置时刻执行一次每日流程。

替代外部 cron / 任务计划程序的常驻方案：
- 进程启动后进入循环，每次循环**重新加载配置文件**（改配置或改执行时间
  无需重启进程）；
- 每天到达配置的执行时刻（daily_times，可配置多个，如 ["10:00", "20:00"]；
  Asia/Shanghai）执行每日完整流程（抓取-预测-检测-提升-通知）；
- 每个时段都会完整执行一次每日流程；后一时段依赖 run_daily 的
  “今日已刷”缺口公式自动补剩余缺口，因此一天分两段刷量无需额外逻辑；
- 时段级去重：同一时段只执行一次，状态用 "YYYY-MM-DD#HH:MM" 标识；
- 重试按**时段**记账：某时段重试用尽只放弃该时段，不会阻塞当天后续时段；
- 启动时若今天已过某个尚未执行的时段，立即补跑（守护进程刚启动或前一天
  失败也不影响当天任务）；
- 执行失败不退出进程，也**不把该时段标记为已完成**：按配置的间隔在当天重试
  （retry_delay_minutes / retry_max_times），重试用尽后等下一时段或明天；
- 支持优雅退出：KeyboardInterrupt（Ctrl+C）或 SIGTERM。

调度逻辑（纯函数 plan_slots / plan_next，便于测试）：
    给定当前时间、最近完成的时段与每日执行时刻列表，决定：
    - 是否应立即执行（存在已到点且尚未完成的时段）；
    - 该执行哪个时段（时段标识 "YYYY-MM-DD#HH:MM"）；
    - 下一次应唤醒的时刻（下一时段、明天首个时段或重试时刻）。
"""

from __future__ import annotations

import logging
import os
import re
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

# 时段标识分隔符：完整时段形如 "2026-01-01#20:00"。
# 定长格式（YYYY-MM-DD 与零填充 HH:MM）保证字符串比较等价于时间比较。
SLOT_SEP = "#"

_TOKEN_SEP = re.compile(r"[,;\s]+")


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


def slot_id_of(when: datetime) -> str:
    """把时间格式化为时段标识 "YYYY-MM-DD#HH:MM"。"""
    return f"{when.strftime('%Y-%m-%d')}{SLOT_SEP}{when.strftime('%H:%M')}"


def _iter_daily_time_tokens(value: Any) -> list[str]:
    """把各种输入形态拆成候选时刻字符串列表（不做合法性校验）。"""
    if isinstance(value, str):
        return [token for token in _TOKEN_SEP.split(value) if token]
    if isinstance(value, (list, tuple)):
        tokens: list[str] = []
        for item in value:
            if isinstance(item, str):
                tokens.extend(
                    token for token in _TOKEN_SEP.split(item) if token
                )
        return tokens
    return []


def parse_daily_times(value: Any) -> list[str]:
    """解析每日执行时刻列表，返回去重升序的 "HH:MM" 列表。

    支持：
    - 单个字符串 "10:00"；
    - 逗号/分号/空白分隔的字符串 "10:00, 20:00"；
    - 字符串列表 / 元组（如 YAML 的列表）；
    - None 或空列表（YAML 未配置时）。

    单个非法时刻只忽略该时刻并记录警告；整体为空或全部非法时回退到
    ["10:00"]（与旧版 daily_time 的兜底一致）。
    """
    valid: set[str] = set()
    for token in _iter_daily_time_tokens(value):
        try:
            hour, minute = (int(part) for part in token.split(":"))
        except (ValueError, AttributeError):
            logger.warning("daily_times 中的时刻 %r 无效，已忽略。", token)
            continue
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            logger.warning("daily_times 中的时刻 %r 越界，已忽略。", token)
            continue
        valid.add(f"{hour:02d}:{minute:02d}")
    if not valid:
        logger.warning(
            "daily_times=%r 未解析出有效时刻，回退到 10:00。", value
        )
        return ["10:00"]
    return sorted(valid)


def pick_run_marker(slot: str, date: str) -> str:
    """在时段标记与日期标记之间选出更晚的一个。

    ``last_run_slot``（daemon 写入）比 ``last_run_date``（手动 run 写入）更精确，
    但日期更新时必须让位给日期标记：否则昨天留下的时段标记会盖掉今天的手动
    run 记录，daemon 会当天重复执行一次。
    """
    slot_text = str(slot or "").strip()
    date_text = str(date or "").strip()
    if slot_text and (not date_text or slot_text[:10] >= date_text):
        return slot_text
    return date_text or slot_text


def _normalize_slot(slot: str) -> str:
    """校验时段标识；非法或仅日期的值返回空串（按从未执行处理）。"""
    text = str(slot or "").strip()
    if SLOT_SEP not in text:
        return ""
    date_part, _, time_part = text.partition(SLOT_SEP)
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
        datetime.strptime(time_part, "%H:%M")
    except ValueError:
        return ""
    return f"{date_part}{SLOT_SEP}{time_part}"


def _slot_datetime(now: datetime, time_text: str) -> datetime:
    """把 "HH:MM" 组合成与 ``now`` 属于同一天、同时区的时刻。"""
    hour_text, _, minute_text = time_text.partition(":")
    return now.replace(
        hour=int(hour_text),
        minute=int(minute_text),
        second=0,
        microsecond=0,
    )


def plan_slots(
    now: datetime,
    last_slot: str,
    daily_times: Any,
) -> tuple[bool, datetime, str]:
    """根据当前时间、最近完成时段与每日时刻列表决定执行计划。

    Args:
        now: 当前时间（带时区）。
        last_slot: 最近一次**已完成**的时段标识 "YYYY-MM-DD#HH:MM"；空串表示
            从未执行。非法值或仅日期会被当作空串处理。
        daily_times: 任意 parse_daily_times 能接受的时刻集合。

    Returns:
        (should_execute, next_wake, due_slot)：
        - should_execute: 今天存在已到点且尚未完成的时段；
        - next_wake: 下一次唤醒时刻（本次/下一时段、明天首个时段）；
        - due_slot: 应执行的时段标识；无需执行时为空串。
    """
    times = parse_daily_times(daily_times)
    today = now.strftime("%Y-%m-%d")
    normalized = _normalize_slot(last_slot)

    if normalized[:10] == today and normalized:
        # 同一固定宽度格式下，字符串比较等价于时间比较
        pending = [
            t for t in times if f"{today}{SLOT_SEP}{t}" > normalized
        ]
    else:
        # 从未执行、或上次执行不属今天 → 今天的全部时段都待执行
        pending = list(times)

    if pending:
        earliest = pending[0]
        due_dt = _slot_datetime(now, earliest)
        if due_dt <= now:
            # 到点：执行该时段；下次唤醒同一天中严格晚于它的时段
            for later in times:
                if later > earliest:
                    next_wake = _slot_datetime(now, later)
                    break
            else:
                next_wake = _slot_datetime(now, times[0]) + timedelta(days=1)
            return True, next_wake, f"{today}{SLOT_SEP}{earliest}"

    # 今天无需执行：唤醒今天最近的未来时段，没有则明天首个时段
    for candidate in times:
        candidate_dt = _slot_datetime(now, candidate)
        if candidate_dt > now:
            return False, candidate_dt, ""
    return False, _slot_datetime(now, times[0]) + timedelta(days=1), ""


def plan_next(
    now: datetime,
    last_run_date: str,
    daily_time: str,
) -> tuple[bool, datetime]:
    """向后兼容的单时刻计划：委托给 plan_slots。

    Args:
        now: 当前时间（带时区）。
        last_run_date: 上次执行日期（YYYY-MM-DD）；空串表示从未执行。
        daily_time: 每日执行时刻，格式 "HH:MM"。

    Returns:
        (should_execute, next_wake)，语义与旧版本一致。
    """
    today_iso = now.strftime("%Y-%m-%d")
    last_slot = (
        f"{today_iso}{SLOT_SEP}23:59"
        if last_run_date == today_iso
        else ""
    )
    should_run, next_wake, _ = plan_slots(now, last_slot, [daily_time])
    return should_run, next_wake


class Daemon:
    """每日定时守护进程：循环重载配置、到点执行、失败不退出。

    Args:
        load_daily_times: 每次循环调用，返回当前配置的每日执行时刻集合
            （任意 parse_daily_times 能接受的形态，如 "10:00" 或
            ["10:00", "20:00"]）。通过它实现配置热重载——改配置无需重启。
        execute: 执行一次每日完整流程的回调（由调用方实现，
            内部应重新加载配置并运行 run_daily）。可返回汇总字典。
        sleeper: 睡眠函数（默认用内部可中断事件等待），测试可注入替身。
        state_path: 状态文件路径；设置后 daemon 会周期性写入运行状态，
            并通过它跨重启保留 last_run_slot / last_run_date 等字段。
        fallback_last_run_date: 状态文件里没有 last_run_date 时的兜底值
            （保留给不需要动态刷新的调用方）。
        last_run_date_provider: 每次循环开始调用的回调，返回最新完成标记：
            可以是时段标识 "YYYY-MM-DD#HH:MM"（daemon 写入），也可以是
            仅日期 "YYYY-MM-DD"（手动 run 写入）。用于覆盖「daemon 等待期间
            手动 run 已执行当天」的情况。
        retry_settings_provider: 每次失败时调用的回调，返回
            ``(重试间隔秒数, 每个时段最多重试次数)``；默认 1800 秒 / 3 次。
        slot_writer: 成功执行某时段后的回调（通常是写数据库 last_run_slot）。
            抛异常只记录警告，绝不影响主循环。
    """

    def __init__(
        self,
        load_daily_times: Callable[[], Any],
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
        slot_writer: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._load_daily_times = load_daily_times
        self._execute = execute
        self._slot_writer = slot_writer
        # 可中断的等待：stop() 会立即唤醒，避免等满一个 SLEEP_STEP（30 秒）
        self._wake = threading.Event()
        self._sleep = sleeper or self._wait
        self._now = now_fn or (lambda: datetime.now(_TZ))
        self._stop = False
        self._state_path = state_path
        self._state: Dict[str, Any] = {}

        # 从上次状态文件恢复：last_run_slot 用于防止重启后同一时段重复执行，
        # last_* 字段用于 info 展示与错误回溯。
        self._last_run_date = ""
        self._last_run_slot = ""
        self._last_run_at: Optional[str] = None
        self._last_summary: Any = None
        self._last_error = ""
        if state_path:
            previous = status.read_state(state_path)
            if previous:
                self._last_run_date = str(previous.get("last_run_date") or "")
                self._last_run_slot = str(
                    previous.get("last_run_slot") or ""
                )
                self._last_run_at = previous.get("last_run_at")
                self._last_summary = previous.get("last_summary")
                self._last_error = str(previous.get("last_error") or "")
        # 状态文件没记录时用数据库兜底（手动 run 也会写 last_run_date）
        if not self._last_run_date:
            self._last_run_date = fallback_last_run_date

        today = self._now().strftime("%Y-%m-%d")
        # 旧状态文件只有 last_run_date：若它等于今天（且下面不判定为失败），
        # 说明当天已完整执行过，视为今天所有时段都完成，避免升级后立刻重复执行。
        if not self._last_run_slot and self._last_run_date == today:
            self._last_run_slot = f"{today}{SLOT_SEP}23:59"

        # 旧版本在执行失败时也会把 last_run_date 记为今天，导致当天不再重试。
        # 状态文件同时满足「今天是 last_run_date / 有错误 / 今天执行过」时，
        # 说明今天那次是失败，按未执行处理；若数据库里确实有今天的成功记录，
        # 循环开始时的外部同步会把它重新采用，不会重复执行。
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
            self._last_run_slot = ""

        self._last_run_date_provider = last_run_date_provider
        self._retry_settings = retry_settings_provider or (
            lambda: (DEFAULT_RETRY_DELAY_SECONDS, DEFAULT_RETRY_MAX_TIMES)
        )
        # 时段级重试状态：切换时段自动清零；失败不写 last_run_slot
        self._attempt_slot = ""
        self._attempts_today = 0
        self._retry_at: Optional[datetime] = None
        self._gave_up_slot = ""

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
        last_slot = self._last_run_slot
        last_run_date = self._last_run_date
        # 仅日期（手动 run）标记换算成“当前时刻时段”，同一天内只观察一次，
        # 避免主循环推进到后一时段时把该时段也一起跳过（那会漏掉补缺口）。
        manual_slot = ""
        manual_slot_date = ""
        started = False
        try:
            while not self._stop:
                now = self._now()
                today_iso = now.strftime("%Y-%m-%d")

                # 每轮先同步外部记录（数据库）：daemon 等待期间手动 run 过当天，
                # 到点后不应再执行一次。
                if self._last_run_date_provider is not None:
                    try:
                        marker = str(
                            self._last_run_date_provider() or ""
                        ).strip()
                    except Exception:  # noqa: BLE001 - 读状态失败不影响主流程
                        logger.warning(
                            "读取外部 last_run 标记失败，沿用内存值",
                            exc_info=True,
                        )
                    else:
                        if SLOT_SEP in marker:
                            # 已完成的时段标记：只前进不后退
                            if marker > last_slot:
                                last_slot = marker
                                self._last_run_slot = marker
                            marker_date = marker[:10]
                            if marker_date > last_run_date:
                                last_run_date = marker_date
                                self._last_run_date = marker_date
                        elif marker == today_iso:
                            # 仅日期：手动 run 在今天执行过，等价于“当前时刻已执行”。
                            # 只在本进程首次观察到时换算，不写回状态文件。
                            if manual_slot_date != today_iso:
                                manual_slot = (
                                    f"{today_iso}{SLOT_SEP}"
                                    f"{now.strftime('%H:%M')}"
                                )
                                manual_slot_date = today_iso
                            if today_iso > last_run_date:
                                last_run_date = today_iso
                                self._last_run_date = today_iso
                        # 其它日期（更早的手动记录）忽略

                # 每次循环重载配置（可能修改了执行时刻）
                daily_times = parse_daily_times(self._load_daily_times())

                if not started:
                    started = True
                    self._state.update({
                        "pid": os.getpid(),
                        "started_at": now.isoformat(timespec="seconds"),
                        "phase": "waiting",
                        "daily_times": daily_times,
                        "last_run_date": last_run_date,
                        "last_run_slot": last_slot,
                        "last_run_at": self._last_run_at,
                        "last_summary": self._last_summary,
                        "last_error": self._last_error,
                    })
                    self._write_state()

                # 有效“最近完成时段”取内存完成时段、已放弃时段与手动标记的最大值
                candidates = [last_slot, self._gave_up_slot]
                if manual_slot and manual_slot[:10] == today_iso:
                    candidates.append(manual_slot)
                effective_last = max(
                    [item for item in candidates if item], default=""
                )

                should_run, next_wake, due_slot = plan_slots(
                    now, effective_last, daily_times
                )
                if (
                    should_run
                    and self._retry_at is not None
                    and now < self._retry_at
                ):
                    # 距下次重试还有一段时间
                    should_run = False
                    next_wake = self._retry_at
                if should_run and due_slot != self._attempt_slot:
                    # 进入新的时段：重置该时段的重试记账
                    self._attempt_slot = due_slot
                    self._attempts_today = 0
                    self._retry_at = None

                next_wake_iso = next_wake.isoformat(timespec="seconds")
                self._write_state(
                    phase="waiting",
                    daily_times=daily_times,
                    next_wake=next_wake_iso,
                    last_run_date=last_run_date,
                    last_run_slot=last_slot,
                    attempts_today=self._attempts_today,
                )

                if should_run:
                    logger.info("到达执行时段 %s，开始执行。", due_slot)
                    # 执行期间旧的 next_wake（该时段已过）已失效，先清空
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
                        # 只有成功才推进时段/日期，避免失败吃掉该时段
                        last_slot = due_slot
                        self._last_run_slot = due_slot
                        last_run_date = due_slot[:10]
                        self._last_run_date = last_run_date
                        self._last_run_at = self._now_iso()
                        if summary is not None:
                            self._last_summary = summary
                        self._last_error = ""
                        self._retry_at = None
                        self._attempts_today = 0
                        self._attempt_slot = ""
                        self._gave_up_slot = ""
                        if self._slot_writer is not None:
                            try:
                                self._slot_writer(due_slot)
                            except Exception:  # noqa: BLE001 - 写标记失败不影响主流程
                                logger.warning(
                                    "写入时段完成标记失败: %s",
                                    due_slot,
                                    exc_info=True,
                                )
                    else:
                        self._last_error = error
                        self._attempts_today += 1
                        delay, max_times = self._retry_settings()
                        if delay <= 0 or self._attempts_today > max_times:
                            self._gave_up_slot = due_slot
                            self._retry_at = None
                            logger.error(
                                "时段 %s 已失败 %d 次（重试上限 %d），放弃该时段；"
                                "将等待下一时段或明天，不会标记为已完成。",
                                due_slot, self._attempts_today, max_times,
                            )
                        else:
                            self._retry_at = self._now() + timedelta(
                                seconds=delay
                            )
                            logger.warning(
                                "时段 %s 第 %d 次失败，%s 重试（每时段最多重试 %d 次）",
                                due_slot,
                                self._attempts_today,
                                self._retry_at.strftime("%Y-%m-%d %H:%M:%S"),
                                max_times,
                            )

                    candidates_after = [last_slot, self._gave_up_slot]
                    effective_last_after = max(
                        [item for item in candidates_after if item],
                        default="",
                    )
                    _, next_wake, _ = plan_slots(
                        self._now(), effective_last_after, daily_times
                    )
                    if self._retry_at is not None:
                        next_wake = min(next_wake, self._retry_at)
                    next_wake_iso = next_wake.isoformat(timespec="seconds")
                    self._write_state(
                        phase="waiting",
                        daily_times=daily_times,
                        next_wake=next_wake_iso,
                        last_run_date=last_run_date,
                        last_run_slot=last_slot,
                        last_run_at=self._last_run_at,
                        last_summary=self._last_summary,
                        last_error=self._last_error,
                        attempts_today=self._attempts_today,
                    )
                    if error is None:
                        logger.info("该时段任务完成，下次执行: %s", next_wake)
                    else:
                        logger.info("该时段任务未完成，下次尝试: %s", next_wake)
                else:
                    if self._retry_at is not None:
                        reason = (
                            f"等待第 {self._attempts_today} 次失败后的重试"
                        )
                    elif (
                        self._gave_up_slot
                        and self._gave_up_slot[:10] == today_iso
                    ):
                        reason = "今日重试已用尽，等待下一时段或明天"
                    else:
                        reason = (
                            f"今日无需执行（时段 {', '.join(daily_times)}）"
                        )
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
                        daily_times=daily_times,
                        next_wake=next_wake_iso,
                        last_run_date=last_run_date,
                        last_run_slot=last_slot,
                    ),
                )

        finally:
            # 正常退出或 Ctrl+C 中断都要落盘 stopped，避免 info 残留 executing
            self._write_state(phase="stopped", stopped_at=self._now_iso())
        logger.info("守护进程已退出。")
