"""daemon 守护进程模块的单元测试：定时计划与常驻循环行为。

覆盖：
- plan_next 纯函数：未到点等待 / 到点执行 / 今天已执行等明天 / 启动补跑；
- Daemon.run_forever：到点执行、单次失败不退出、每次循环重载配置、
  睡眠期间可优雅退出。
"""

import os
import signal
import threading
import time
from datetime import datetime, timedelta

import pytest

from curator_views.core import status
from curator_views.core.daemon import (
    Daemon,
    _TZ,
    parse_daily_times,
    pick_run_marker,
    plan_next,
    plan_slots,
)


def dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    """构造当月指定日期的 Asia/Shanghai 时间。"""
    return datetime(2026, 1, day, hour, minute, tzinfo=_TZ)


# ── plan_next 纯函数 ─────────────────────────────────────────────────────


class TestPlanNext:
    def test_before_time_waits_until_today(self):
        # 08:00 < 10:00：今天未执行 → 等到今天 10:00
        should, wake = plan_next(dt(8, 0), "", "10:00")
        assert should is False
        assert wake == dt(10, 0)

    def test_at_time_triggers_and_schedules_tomorrow(self):
        # 10:00 恰好到点：立即执行，下次唤醒明天 10:00
        should, wake = plan_next(dt(10, 0), "", "10:00")
        assert should is True
        assert wake == dt(10, 0, day=2)

    def test_after_time_without_run_trigger_catchup(self):
        # 15:00 > 10:00 且今天未执行 → 启动补跑
        should, wake = plan_next(dt(15, 0), "", "10:00")
        assert should is True
        assert wake == dt(10, 0, day=2)

    def test_already_ran_waits_until_tomorrow(self):
        # 今天已执行过 → 即使已过点也不重复执行，等明天
        should, wake = plan_next(dt(15, 0), "2026-01-01", "10:00")
        assert should is False
        assert wake == dt(10, 0, day=2)

    def test_invalid_time_falls_back_to_1000(self):
        should, wake = plan_next(dt(8, 0), "", "not-a-time")
        assert wake == dt(10, 0)


# ── Daemon 常驻循环 ──────────────────────────────────────────────────────


class TestDaemonLoop:
    def test_runs_once_when_past_time_then_stops(self):
        # daily_time 用 00:00（今天必然已过）→ 启动即执行一次
        calls = []
        daemon = None

        def sleeper(secs):  # 不真实睡眠
            daemon.stop()

        def execute():
            calls.append("run")
            daemon.stop()  # 执行后立即退出循环

        daemon = Daemon(
            lambda: "00:00", execute,
            sleeper=sleeper, install_handlers=False,
        )
        daemon.run_forever()
        assert calls == ["run"]

    def test_failure_does_not_exit_process(self):
        # 执行抛异常 → 异常被吞（记日志），进程继续运行直到被手动停止
        calls = []
        daemon = None

        def execute():
            calls.append("run")
            raise RuntimeError("模拟执行失败")

        def sleeper(secs):
            daemon.stop()  # 失败后的等待期间被手动停止

        daemon = Daemon(
            lambda: "00:00", execute,
            sleeper=sleeper, install_handlers=False,
        )
        daemon.run_forever()  # 不应向外抛异常
        assert calls == ["run"]  # 失败被吞，进程存活

    def test_reloads_config_each_cycle(self):
        # 共享可变时钟：08:00 → 10:00（执行日）→ 次日 10:00（再执行）
        # 每一轮 run_forever 循环都重新调用 load_daily_time（配置热重载）
        state = {"clock": dt(8, 0)}
        loads = []
        executed = []
        daemon = None

        def now_fn():
            return state["clock"]

        def load_daily_time():
            loads.append(1)
            return "10:00"

        def sleeper(secs):
            if len(loads) == 1:
                state["clock"] = dt(10, 0)           # 第一天到点
            elif len(loads) == 2:
                state["clock"] = dt(10, 0, day=2)    # 第二天到点
            else:
                daemon.stop()

        def execute():
            executed.append("run")

        daemon = Daemon(
            load_daily_time, execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=now_fn,
        )
        daemon.run_forever()
        assert len(loads) >= 3  # 每轮循环都重载配置
        assert executed == ["run", "run"]  # 连续两天各执行一次

    def test_executes_when_clock_reaches_scheduled_time(self):
        # 时钟从 08:00 推进到 10:00 → 到点执行一次
        state = {"clock": dt(8, 0)}
        executed = []
        daemon = None

        def now_fn():
            return state["clock"]

        def sleeper(secs):
            state["clock"] = dt(10, 0)  # 睡眠期间时间推进到执行时刻

        def execute():
            executed.append("run")
            daemon.stop()

        daemon = Daemon(
            lambda: "10:00", execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=now_fn,
        )
        daemon.run_forever()
        assert executed == ["run"]

    def test_waits_and_can_be_stopped_while_sleeping(self):
        # 尚未到点：不执行，睡眠期间可被 stop() 优雅退出
        executed = []
        daemon = None

        def sleeper(secs):
            daemon.stop()  # 睡眠一开始就被停止

        def execute():
            executed.append("run")

        daemon = Daemon(
            lambda: "23:59", execute,
            sleeper=sleeper, install_handlers=False,
        )
        daemon.run_forever()
        assert executed == []


# ── 状态文件持久化 ────────────────────────────────────────────────────────


class TestDaemonState:
    def test_writes_state_with_pid_and_stopped_phase(self, tmp_path):
        state_path = tmp_path / "daemon_state.json"
        daemon = None

        def sleeper(secs):
            daemon.stop()

        daemon = Daemon(
            lambda: "23:59", lambda: None,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(8, 0),
            state_path=str(state_path),
        )
        daemon.run_forever()

        state = status.read_state(str(state_path))
        assert state is not None
        assert state["pid"] == os.getpid()
        assert state["phase"] == "stopped"
        assert state["started_at"]
        assert state["updated_at"]
        assert state["stopped_at"]

    def test_persists_last_run_date_and_summary(self, tmp_path):
        state_path = tmp_path / "daemon_state.json"
        daemon = None

        def sleeper(secs):
            daemon.stop()

        during_execute = {}

        def execute():
            during_execute.update(status.read_state(str(state_path)) or {})
            return {"need": 5, "boosted": 3}

        daemon = Daemon(
            lambda: "00:00", execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(10, 0),
            state_path=str(state_path),
        )
        daemon.run_forever()

        # 执行期间阶段为 executing，且旧的 next_wake 已失效被清空
        assert during_execute["phase"] == "executing"
        assert during_execute["next_wake"] is None

        state = status.read_state(str(state_path))
        assert state is not None
        assert state["last_run_date"] == "2026-01-01"
        assert state["last_run_at"]
        assert state["last_summary"] == {"need": 5, "boosted": 3}
        assert state["last_error"] == ""

    def test_restart_same_day_does_not_execute_again(self, tmp_path):
        state_path = tmp_path / "daemon_state.json"
        first_calls = []
        first_daemon = None

        def first_sleeper(secs):
            first_daemon.stop()

        def first_execute():
            first_calls.append("run")

        first_daemon = Daemon(
            lambda: "00:00", first_execute,
            sleeper=first_sleeper, install_handlers=False,
            now_fn=lambda: dt(0, 1),
            state_path=str(state_path),
        )
        first_daemon.run_forever()
        assert first_calls == ["run"]

        # 同一天晚些时候重启：从状态文件恢复 last_run_date，不再重复执行
        second_calls = []
        second_daemon = None

        def second_sleeper(secs):
            second_daemon.stop()

        def second_execute():
            second_calls.append("run")

        second_daemon = Daemon(
            lambda: "00:00", second_execute,
            sleeper=second_sleeper, install_handlers=False,
            now_fn=lambda: dt(15, 0),
            state_path=str(state_path),
        )
        second_daemon.run_forever()

        assert second_calls == []
        state = status.read_state(str(state_path))
        assert state["last_run_date"] == "2026-01-01"

    def test_execute_exception_records_last_error_and_keeps_running(
        self, tmp_path
    ):
        state_path = tmp_path / "daemon_state.json"
        calls = []
        sleep_calls = []
        daemon = None

        def execute():
            calls.append("run")
            raise RuntimeError("模拟执行失败")

        def sleeper(secs):
            sleep_calls.append(1)
            daemon.stop()

        daemon = Daemon(
            lambda: "00:00", execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(10, 0),
            state_path=str(state_path),
        )
        daemon.run_forever()

        assert calls == ["run"]
        assert sleep_calls  # 异常后仍进入下一轮等待，进程未退出
        state = status.read_state(str(state_path))
        assert state is not None
        assert state["last_error"] == "模拟执行失败"
        # 失败不推进 last_run_date：当天仍算未完成，可重试/可手动补跑
        assert state["last_run_date"] == ""
        assert state["attempts_today"] == 1
        # 默认 30 分钟后重试
        assert state["next_wake"] == "2026-01-01T10:30:00+08:00"

    def test_retries_after_delay_then_succeeds(self, tmp_path):
        state_path = tmp_path / "daemon_state.json"
        clock = {"now": dt(10, 0)}
        calls = []
        daemon = None

        def execute():
            calls.append(clock["now"])
            if len(calls) == 1:
                raise RuntimeError("统计页被重定向到公开页面")
            daemon.stop()

        def sleeper(secs):
            clock["now"] += timedelta(seconds=secs)

        daemon = Daemon(
            lambda: "10:00", execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            state_path=str(state_path),
            retry_settings_provider=lambda: (1800.0, 3),
        )
        daemon.run_forever()

        assert len(calls) == 2
        assert calls[1] - calls[0] >= timedelta(minutes=30)
        state = status.read_state(str(state_path))
        assert state is not None
        assert state["last_run_date"] == "2026-01-01"
        assert state["last_error"] == ""
        assert state["attempts_today"] == 0

    def test_gives_up_after_max_retries_without_marking_done(self, tmp_path):
        state_path = tmp_path / "daemon_state.json"
        clock = {"now": dt(10, 0)}
        calls = []
        daemon = None

        def execute():
            calls.append(1)
            raise RuntimeError("模拟执行失败")

        def sleeper(secs):
            clock["now"] += timedelta(seconds=secs)
            if clock["now"] >= dt(12, 0):
                daemon.stop()

        daemon = Daemon(
            lambda: "10:00", execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            state_path=str(state_path),
            retry_settings_provider=lambda: (600.0, 2),
        )
        daemon.run_forever()

        assert len(calls) == 3  # 首次 + 2 次重试
        state = status.read_state(str(state_path))
        assert state is not None
        assert state["last_error"] == "模拟执行失败"
        assert state["last_run_date"] == ""
        assert state["attempts_today"] == 3

    def test_restart_retries_day_that_failed_in_old_state(self, tmp_path):
        # 旧版本把失败日写进 last_run_date；新版本启动时应纠正并重试
        state_path = tmp_path / "daemon_state.json"
        status.write_state(
            str(state_path),
            {
                "pid": 1,
                "phase": "waiting",
                "started_at": "2026-01-01T09:00:00+08:00",
                "updated_at": "2026-01-01T10:01:00+08:00",
                "last_run_date": "2026-01-01",
                "last_run_at": "2026-01-01T10:01:00+08:00",
                "last_summary": None,
                "last_error": "统计页被重定向到公开页面",
            },
        )
        calls = []
        daemon = None

        def execute():
            calls.append(1)
            daemon.stop()

        def sleeper(secs):
            daemon.stop()

        daemon = Daemon(
            lambda: "10:00", execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(11, 0),
            state_path=str(state_path),
        )
        daemon.run_forever()

        assert calls == [1]
        state = status.read_state(str(state_path))
        assert state["last_run_date"] == "2026-01-01"
        assert state["last_error"] == ""


class TestStopResponsiveness:
    def test_stop_wakes_idle_sleep_immediately(self):
        # 默认睡眠用可中断等待：stop() 后应秒级退出，而不是等满 SLEEP_STEP
        daemon = Daemon(
            lambda: "23:59", lambda: None,
            install_handlers=False,
            now_fn=lambda: dt(8, 0),
        )
        thread = threading.Thread(target=daemon.run_forever, daemon=True)
        thread.start()
        time.sleep(0.1)  # 让循环进入等待

        started = time.monotonic()
        daemon.stop()
        thread.join(timeout=5.0)

        assert not thread.is_alive()
        assert time.monotonic() - started < 1.0


class TestLastRunDateSources:
    def test_fallback_last_run_date_prevents_same_day_run(self):
        # 状态文件缺失时，构造参数传入的日期同样生效（数据库兜底）
        calls = []
        daemon = None

        def sleeper(secs):
            daemon.stop()

        daemon = Daemon(
            lambda: "10:00", lambda: calls.append("run"),
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(15, 0),
            fallback_last_run_date="2026-01-01",
        )
        daemon.run_forever()
        assert calls == []

    def test_provider_skips_run_when_db_already_has_today(self):
        # daemon 等待期间手动 run 过当天：到点后 provider 返回最新日期 → 不重复执行
        calls = []
        daemon = None

        def sleeper(secs):
            daemon.stop()

        daemon = Daemon(
            lambda: "10:00", lambda: calls.append("run"),
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(15, 0),
            last_run_date_provider=lambda: "2026-01-01",
        )
        daemon.run_forever()
        assert calls == []

    def test_provider_failure_does_not_break_daemon(self):
        calls = []
        daemon = None

        def sleeper(secs):
            daemon.stop()

        def broken_provider():
            raise RuntimeError("数据库损坏")

        daemon = Daemon(
            lambda: "10:00", lambda: calls.append("run"),
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(15, 0),
            last_run_date_provider=broken_provider,
        )
        daemon.run_forever()  # 不应抛出，按未执行处理
        assert calls == ["run"]


class TestKeyboardInterrupt:
    def test_interrupt_during_execute_writes_stopped_state(self, tmp_path):
        # Ctrl+C 打断执行：异常继续向上抛（CLI 捕获），但状态文件必须是 stopped
        state_path = tmp_path / "daemon_state.json"

        def execute():
            raise KeyboardInterrupt

        daemon = Daemon(
            lambda: "00:00", execute,
            sleeper=lambda secs: None, install_handlers=False,
            now_fn=lambda: dt(10, 0),
            state_path=str(state_path),
        )
        with pytest.raises(KeyboardInterrupt):
            daemon.run_forever()

        state = status.read_state(str(state_path))
        assert state is not None
        assert state["phase"] == "stopped"
        assert state["stopped_at"]

    def test_sigint_handler_raises_and_sets_stop(self):
        # SIGINT handler 必须抛 KeyboardInterrupt（否则刷量任务不会中断）
        prev_int = signal.getsignal(signal.SIGINT)
        prev_term = signal.getsignal(signal.SIGTERM)
        try:
            daemon = Daemon(
                lambda: "10:00", lambda: None,
                install_handlers=True,
                now_fn=lambda: dt(8, 0),
            )
            handler = signal.getsignal(signal.SIGINT)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
            assert daemon._stop is True
        finally:
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)


# ── 多时段：纯函数 ────────────────────────────────────────────────────────


class TestParseDailyTimes:
    def test_single_string(self):
        assert parse_daily_times("10:00") == ["10:00"]

    def test_comma_string_with_spaces(self):
        # 逗号/空白混合分隔，且乱序 → 去重后升序
        assert parse_daily_times("20:00, 10:00") == ["10:00", "20:00"]

    def test_list_with_duplicates_unsorted(self):
        assert (
            parse_daily_times(["20:00", "10:00", "20:00"])
            == ["10:00", "20:00"]
        )

    def test_partially_invalid_list_keeps_valid(self):
        # 单个非法/越界时刻只忽略自身，不回退成 10:00
        assert (
            parse_daily_times(["10:00", "not-a-time", "25:00", "20:00"])
            == ["10:00", "20:00"]
        )

    def test_empty_and_none_fall_back(self):
        assert parse_daily_times(None) == ["10:00"]
        assert parse_daily_times([]) == ["10:00"]
        assert parse_daily_times("") == ["10:00"]


class TestPickRunMarker:
    def test_prefers_slot_marker_on_same_day(self):
        # 同一天：时段标记更精确，优先采用
        assert (
            pick_run_marker("2026-01-01#10:00", "2026-01-01")
            == "2026-01-01#10:00"
        )

    def test_newer_date_wins_over_stale_slot(self):
        # 昨天留下的时段标记不能盖掉今天的手动 run 记录
        assert (
            pick_run_marker("2025-12-31#20:00", "2026-01-01") == "2026-01-01"
        )

    def test_falls_back_to_whichever_exists(self):
        assert pick_run_marker("", "2026-01-01") == "2026-01-01"
        assert pick_run_marker("2026-01-01#10:00", "") == "2026-01-01#10:00"
        assert pick_run_marker("", "") == ""


class TestPlanSlots:
    TIMES = ["10:00", "20:00"]

    def test_before_first_slot_waits(self):
        should, wake, due = plan_slots(dt(8, 0), "", self.TIMES)
        assert should is False
        assert wake == dt(10, 0)
        assert due == ""

    def test_at_first_slot_runs_and_wakes_second(self):
        should, wake, due = plan_slots(dt(10, 0), "", self.TIMES)
        assert should is True
        assert due == "2026-01-01#10:00"
        assert wake == dt(20, 0)

    def test_after_first_completed_runs_second_only(self):
        # 10:00 已完成，等到 20:00 时才只执行第二时段
        should, wake, due = plan_slots(
            dt(20, 0), "2026-01-01#10:00", self.TIMES
        )
        assert should is True
        assert due == "2026-01-01#20:00"
        assert wake == dt(10, 0, day=2)

    def test_midday_after_first_waits_for_second(self):
        should, wake, due = plan_slots(
            dt(15, 0), "2026-01-01#10:00", self.TIMES
        )
        assert should is False
        assert due == ""
        assert wake == dt(20, 0)

    def test_after_second_completed_waits_tomorrow_first(self):
        should, wake, due = plan_slots(
            dt(21, 0), "2026-01-01#20:00", self.TIMES
        )
        assert should is False
        assert due == ""
        assert wake == dt(10, 0, day=2)

    def test_yesterday_slot_runs_first_today(self):
        should, wake, due = plan_slots(
            dt(10, 0), "2025-12-31#20:00", self.TIMES
        )
        assert should is True
        assert due == "2026-01-01#10:00"
        assert wake == dt(20, 0)

    def test_nothing_due_has_empty_slot_id(self):
        # 今天两个时段都完成 → 无待执行，slot_id 为空
        should, _wake, due = plan_slots(
            dt(12, 0), "2026-01-01#20:00", self.TIMES
        )
        assert should is False
        assert due == ""


# ── 多时段：Daemon 循环 ───────────────────────────────────────────────────


class TestMultiSlotDaemon:
    def test_two_slots_run_same_day_with_gap(self):
        clock = {"now": dt(9, 0)}
        calls = []
        daemon = None

        def sleeper(secs):
            clock["now"] += timedelta(seconds=secs)

        def execute():
            calls.append(clock["now"])
            if len(calls) == 2:
                daemon.stop()

        daemon = Daemon(
            lambda: ["10:00", "20:00"], execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
        )
        daemon.run_forever()

        assert len(calls) == 2
        assert calls[0].strftime("%H:%M") == "10:00"
        assert calls[1].strftime("%H:%M") == "20:00"
        assert calls[1] - calls[0] > timedelta(hours=5)

    def test_restart_midday_runs_only_second_slot(self, tmp_path):
        state_path = str(tmp_path / "daemon_state.json")
        first_clock = {"now": dt(10, 0)}
        first_calls = []
        first_daemon = None

        def first_sleeper(secs):
            first_daemon.stop()

        def first_execute():
            first_calls.append(first_clock["now"])
            first_daemon.stop()

        first_daemon = Daemon(
            lambda: ["10:00", "20:00"], first_execute,
            sleeper=first_sleeper, install_handlers=False,
            now_fn=lambda: first_clock["now"],
            state_path=state_path,
        )
        first_daemon.run_forever()
        assert first_calls == [dt(10, 0)]

        # 同一天 19:00 重启：10:00 时段已完成，只补 20:00
        clock = {"now": dt(19, 0)}
        second_calls = []
        second_daemon = None

        def second_sleeper(secs):
            clock["now"] += timedelta(seconds=secs)

        def second_execute():
            second_calls.append(clock["now"])
            second_daemon.stop()

        second_daemon = Daemon(
            lambda: ["10:00", "20:00"], second_execute,
            sleeper=second_sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            state_path=state_path,
        )
        second_daemon.run_forever()

        assert len(second_calls) == 1
        assert second_calls[0].strftime("%H:%M") == "20:00"

    def test_date_only_provider_skips_first_keeps_second(self):
        # 15:00 时 provider 返回今天的仅日期标记（手动 run 已执行）：
        # 10:00 时段跳过，20:00 时段仍要执行补缺口
        clock = {"now": dt(15, 0)}
        calls = []
        daemon = None

        def sleeper(secs):
            clock["now"] += timedelta(seconds=secs)

        def execute():
            calls.append(clock["now"])
            daemon.stop()

        daemon = Daemon(
            lambda: ["10:00", "20:00"], execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            last_run_date_provider=lambda: "2026-01-01",
        )
        daemon.run_forever()

        assert len(calls) == 1
        assert calls[0].strftime("%H:%M") == "20:00"

    def test_failed_first_slot_does_not_block_second(self, tmp_path):
        # 第一时段重试用尽（delay=0, max=0）后，第二时段仍要在同一天执行
        state_path = str(tmp_path / "daemon_state.json")
        clock = {"now": dt(10, 0)}
        calls = []
        daemon = None

        def sleeper(secs):
            clock["now"] += timedelta(seconds=secs)

        def execute():
            calls.append(clock["now"])
            if len(calls) == 1:
                raise RuntimeError("时段一失败")
            daemon.stop()

        daemon = Daemon(
            lambda: ["10:00", "20:00"], execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            state_path=state_path,
            retry_settings_provider=lambda: (0.0, 0),
        )
        daemon.run_forever()

        assert len(calls) == 2
        assert calls[1].strftime("%H:%M") == "20:00"
        state = status.read_state(state_path)
        assert state is not None
        assert state["last_run_slot"] == "2026-01-01#20:00"
        # 第二时段成功后重试计数归零
        assert state["attempts_today"] == 0

    def test_slot_writer_receives_slots_in_order(self):
        clock = {"now": dt(10, 0)}
        written = []
        seen_clock = []
        daemon = None

        def sleeper(secs):
            clock["now"] += timedelta(seconds=secs)

        def slot_writer(slot):
            written.append(slot)
            seen_clock.append(clock["now"])
            if len(written) == 2:
                daemon.stop()

        daemon = Daemon(
            lambda: ["10:00", "20:00"], lambda: None,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            slot_writer=slot_writer,
        )
        daemon.run_forever()

        assert written == ["2026-01-01#10:00", "2026-01-01#20:00"]
        assert seen_clock[0].strftime("%H:%M") == "10:00"
        assert seen_clock[1].strftime("%H:%M") == "20:00"

    def test_state_records_last_run_slot_and_daily_times(self, tmp_path):
        state_path = str(tmp_path / "daemon_state.json")
        clock = {"now": dt(10, 0)}
        daemon = None

        def sleeper(secs):
            daemon.stop()

        def execute():
            daemon.stop()

        daemon = Daemon(
            lambda: ["10:00", "20:00"], execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            state_path=state_path,
        )
        daemon.run_forever()

        state = status.read_state(state_path)
        assert state is not None
        assert state["last_run_slot"] == "2026-01-01#10:00"
        assert state["daily_times"] == ["10:00", "20:00"]
