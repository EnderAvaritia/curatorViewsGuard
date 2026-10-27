"""status 模块单元测试：状态文件读写、PID 检查与状态描述。"""

import json
import os
from datetime import datetime, timedelta

from curator_views.core import status


def make_state(**overrides):
    """构造一个可用的 daemon 状态字典。"""
    state = {
        "pid": 4321,
        "phase": "waiting",
        "started_at": "2026-01-01T10:00:00+08:00",
        "updated_at": "2026-01-01T11:59:30+08:00",
        "last_run_date": "",
        "last_run_at": None,
        "next_wake": "2026-01-02T10:00:00+08:00",
        "last_summary": None,
        "last_error": "",
    }
    state.update(overrides)
    return state


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=status._TZ)


class TestStateFile:
    def test_write_read_roundtrip(self, tmp_path):
        path = tmp_path / "nested" / "daemon_state.json"
        state = make_state()

        status.write_state(str(path), state)

        assert path.is_file()
        assert status.read_state(str(path)) == state
        text = path.read_text(encoding="utf-8")
        assert '"phase": "waiting"' in text

    def test_missing_file_returns_none(self, tmp_path):
        assert status.read_state(str(tmp_path / "nope.json")) is None

    def test_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "daemon_state.json"
        path.write_text("{not valid json", encoding="utf-8")

        assert status.read_state(str(path)) is None

    def test_non_object_json_returns_none(self, tmp_path):
        path = tmp_path / "daemon_state.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        assert status.read_state(str(path)) is None

    def test_default_state_path_beside_db(self):
        assert status.default_state_path(
            str(os.path.join("data", "views.db"))
        ) == str(os.path.join("data", "daemon_state.json"))

    def test_now_iso_has_seconds_precision(self):
        value = status.now_iso()

        assert len(value) >= 19
        assert value[4] == "-" and value[10] == "T"


class TestPidAlive:
    def test_current_process_is_alive(self):
        assert status.pid_alive(os.getpid()) is True


class TestDescribeDaemon:
    def test_missing_state(self):
        result = status.describe_daemon(None, alive_fn=lambda pid: True, now=NOW)

        assert result["running"] is False
        assert "从未启动" in result["detail"]
        assert result["pid"] is None
        assert result["heartbeat_age_seconds"] is None

    def test_running_waiting(self):
        result = status.describe_daemon(
            make_state(), alive_fn=lambda pid: True, now=NOW
        )

        assert result["running"] is True
        assert result["detail"] == "运行中"
        assert result["phase"] == "waiting"
        assert result["phase_label"] == "等待下次执行"
        assert result["heartbeat_age_seconds"] == 30.0

    def test_running_executing_label(self):
        result = status.describe_daemon(
            make_state(phase="executing"),
            alive_fn=lambda pid: True,
            now=NOW,
        )

        assert result["phase_label"] == "正在执行每日流程"

    def test_stopped_gracefully(self):
        result = status.describe_daemon(
            make_state(phase="stopped"), alive_fn=lambda pid: False, now=NOW
        )

        assert result["running"] is False
        assert result["detail"] == "已停止（正常退出）"
        assert result["phase_label"] == "已停止"

    def test_crashed_or_killed(self):
        result = status.describe_daemon(
            make_state(phase="waiting"), alive_fn=lambda pid: False, now=NOW
        )

        assert result["running"] is False
        assert "上次异常退出" in result["detail"]

    def test_unknown_pid_with_fresh_heartbeat(self):
        state = make_state(
            updated_at=(NOW - timedelta(seconds=10)).isoformat()
        )

        result = status.describe_daemon(
            state, alive_fn=lambda pid: None, now=NOW
        )

        assert result["running"] is True
        assert "心跳正常" in result["detail"]
        assert result["heartbeat_age_seconds"] == 10.0

    def test_unknown_pid_with_stale_heartbeat(self):
        state = make_state(
            updated_at=(NOW - timedelta(seconds=200)).isoformat()
        )

        result = status.describe_daemon(
            state, alive_fn=lambda pid: None, now=NOW
        )

        assert result["running"] is False
        assert "心跳已过期" in result["detail"]

    def test_malformed_timestamp_does_not_raise(self):
        state = make_state(updated_at="not-a-timestamp")

        result = status.describe_daemon(
            state, alive_fn=lambda pid: None, now=NOW
        )

        assert result["running"] is False
        assert result["heartbeat_age_seconds"] is None
        assert "心跳已过期" in result["detail"]

    def test_naive_timestamp_treated_as_shanghai(self):
        state = make_state(updated_at="2026-01-01T11:59:00")

        result = status.describe_daemon(
            state, alive_fn=lambda pid: True, now=NOW
        )

        assert result["heartbeat_age_seconds"] == 60.0

    def test_utc_z_timestamp_is_parsed(self):
        # 2026-01-01T04:00:00Z 即 12:00 Asia/Shanghai，心跳年龄 0
        state = make_state(updated_at="2026-01-01T04:00:00Z")

        result = status.describe_daemon(
            state, alive_fn=lambda pid: True, now=NOW
        )

        assert result["heartbeat_age_seconds"] == 0.0