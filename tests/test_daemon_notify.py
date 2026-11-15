"""daemon 补发排队通知（pending_flusher）的接线测试。

覆盖：
- 每一轮主循环开始时调用补发回调；
- 长时间等待期间由睡眠心跳再次补发（静默时段结束后 ~30 秒内送达）；
- 补发回调抛异常被吞掉，不影响主循环与每日执行；
- 未配置补发回调（默认 None）时正常运行。
"""

from datetime import datetime, timedelta

from curator_views.core.daemon import Daemon, _TZ


def dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    """构造当月指定日期的 Asia/Shanghai 时间。"""
    return datetime(2026, 1, day, hour, minute, tzinfo=_TZ)


class TestPendingFlusher:
    def test_flusher_called_on_first_loop_iteration(self):
        # 未到执行时刻：第一轮循环开始时就应先补发一次，再进入等待
        events = []
        daemon = None

        def sleeper(secs):
            events.append("sleep")
            daemon.stop()

        def flusher():
            events.append("flush")

        def execute():
            events.append("execute")

        daemon = Daemon(
            lambda: "23:59", execute,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(8, 0),
            pending_flusher=flusher,
        )
        daemon.run_forever()

        assert events[0] == "flush"
        assert "execute" not in events

    def test_flusher_called_from_sleep_heartbeat(self):
        # 距离下次执行还有很久：睡眠心跳应周期补发（30 秒一步）
        clock = {"now": dt(8, 0)}
        events = []
        daemon = None

        def sleeper(secs):
            events.append("sleep")
            clock["now"] += timedelta(seconds=secs)
            if events.count("sleep") >= 2:
                daemon.stop()

        def flusher():
            events.append("flush")

        daemon = Daemon(
            lambda: "20:00", lambda: None,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: clock["now"],
            pending_flusher=flusher,
        )
        daemon.run_forever()

        # 至少一次补发发生在首次睡眠之后（即来自心跳，而非主循环开头）
        first_sleep = events.index("sleep")
        assert "flush" in events[first_sleep + 1:]

    def test_flusher_exception_does_not_block_execution(self):
        # 补发失败只记警告：不崩溃、不阻止到点后的每日执行
        executed = []
        daemon = None

        def flusher():
            raise RuntimeError("模拟补发失败")

        def execute():
            executed.append("run")
            daemon.stop()

        daemon = Daemon(
            lambda: "00:00", execute,
            sleeper=lambda secs: daemon.stop(),
            install_handlers=False,
            now_fn=lambda: dt(10, 0),
            pending_flusher=flusher,
        )
        daemon.run_forever()  # 不应向外抛异常

        assert executed == ["run"]

    def test_default_none_flusher_is_safe(self):
        # 未传 pending_flusher（默认 None）时补发逻辑直接跳过
        daemon = None

        def sleeper(secs):
            daemon.stop()

        daemon = Daemon(
            lambda: "23:59", lambda: None,
            sleeper=sleeper, install_handlers=False,
            now_fn=lambda: dt(8, 0),
        )
        daemon.run_forever()  # 不应抛异常