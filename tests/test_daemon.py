"""daemon 守护进程模块的单元测试：定时计划与常驻循环行为。

覆盖：
- plan_next 纯函数：未到点等待 / 到点执行 / 今天已执行等明天 / 启动补跑；
- Daemon.run_forever：到点执行、单次失败不退出、每次循环重载配置、
  睡眠期间可优雅退出。
"""

import os
import threading
import time
from datetime import datetime

from curator_views.core import status
from curator_views.core.daemon import Daemon, _TZ, plan_next


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
        assert state["last_run_date"] == "2026-01-01"


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
