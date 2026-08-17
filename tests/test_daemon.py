"""daemon 守护进程模块的单元测试：定时计划与常驻循环行为。

覆盖：
- plan_next 纯函数：未到点等待 / 到点执行 / 今天已执行等明天 / 启动补跑；
- Daemon.run_forever：到点执行、单次失败不退出、每次循环重载配置、
  睡眠期间可优雅退出。
"""

from datetime import datetime

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
