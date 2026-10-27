"""cli info 子命令测试：离线读取状态文件与本地数据库摘要。"""

import json
from datetime import datetime

from curator_views import cli
from curator_views.core import status
from curator_views.core.storage import Storage

CONFIG_TEXT = """\
clan_id: ""
cookie_file: "missing_cookie.txt"
db_path: "views.db"
daily_time: "10:00"
boost_enabled: false
boost_mode: stable
"""


def write_config(tmp_path, text=CONFIG_TEXT):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def make_db(tmp_path):
    """创建含 2 天统计与 kv 状态的数据库。"""
    db_path = tmp_path / "views.db"
    storage = Storage(str(db_path))
    first = datetime(2026, 10, 15, tzinfo=status._TZ)
    latest = datetime(2026, 10, 16, tzinfo=status._TZ)
    storage.upsert_daily_stats(int(first.timestamp()), 100)
    storage.upsert_daily_stats(int(latest.timestamp()), 120)
    storage.save_state("program_start", "2026-10-15")
    storage.save_state(
        "last_stats_date",
        json.dumps({"last_date": "2026-10-16", "days": 2}),
    )
    storage._conn.close()
    return db_path


def write_running_state(db_path, phase="waiting", updated_at=None):
    state_path = status.default_state_path(str(db_path))
    updated_at = updated_at or datetime.now(status._TZ).isoformat(
        timespec="seconds"
    )
    status.write_state(
        state_path,
        {
            "pid": 12345,
            "phase": phase,
            "started_at": "2026-10-27T18:03:12+08:00",
            "updated_at": updated_at,
            "last_run_date": "2026-10-26",
            "last_run_at": "2026-10-26T10:00:03+08:00",
            "next_wake": "2026-10-28T10:00:00+08:00",
            "last_summary": {"need": 5},
            "last_error": "",
        },
    )
    return state_path


class TestInfoJson:
    def test_json_report_contains_daemon_config_and_data(
        self, tmp_path, monkeypatch, capsys
    ):
        config_path = write_config(tmp_path)
        db_path = make_db(tmp_path)
        state_path = write_running_state(db_path)
        monkeypatch.setattr(status, "pid_alive", lambda pid: True)

        code = cli.main(
            ["info", "--config", str(config_path), "--json"]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        daemon = payload["daemon"]
        assert daemon["running"] is True
        assert daemon["detail"] == "运行中"
        assert daemon["pid"] == 12345
        assert daemon["phase"] == "waiting"
        assert daemon["phase_label"] == "等待下次执行"
        assert daemon["state_file"] == state_path
        assert daemon["state_file_exists"] is True
        assert daemon["last_run_date"] == "2026-10-26"
        assert daemon["last_summary"] == {"need": 5}

        assert payload["config"]["daily_time"] == "10:00"
        assert payload["config"]["boost_enabled"] is False
        assert payload["config"]["boost_mode"] == "stable"
        assert payload["config"]["db_path"] == str(db_path)

        data = payload["data"]
        assert data["days"] == 2
        assert data["first_date"] == "2026-10-15"
        assert data["latest_date"] == "2026-10-16"
        assert data["latest_views"] == 120
        assert data["program_start"] == "2026-10-15"
        assert data["last_stats_date"] == "2026-10-16"

    def test_json_report_when_never_started(self, tmp_path, capsys):
        config_path = write_config(tmp_path)

        code = cli.main(
            ["info", "--config", str(config_path), "--json"]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["daemon"]["running"] is False
        assert "从未启动" in payload["daemon"]["detail"]
        assert payload["daemon"]["state_file_exists"] is False
        assert payload["data"]["days"] == 0
        assert payload["data"]["latest_views"] is None
        # info 不得因查询而创建空数据库
        assert not (tmp_path / "views.db").exists()


class TestInfoHuman:
    def test_human_output_for_running_daemon(
        self, tmp_path, monkeypatch, capsys
    ):
        config_path = write_config(tmp_path)
        db_path = make_db(tmp_path)
        write_running_state(db_path)
        monkeypatch.setattr(status, "pid_alive", lambda pid: True)

        code = cli.main(["info", "--config", str(config_path)])

        assert code == 0
        out = capsys.readouterr().out
        assert "守护进程: 运行中" in out
        assert "PID: 12345" in out
        assert "当前阶段: waiting（等待下次执行）" in out
        assert "配置: 每日执行 10:00；提升已关闭（采集模式）" in out
        assert "数据: 2 天" in out

    def test_human_output_when_never_started(self, tmp_path, capsys):
        config_path = write_config(tmp_path)

        code = cli.main(["info", "--config", str(config_path)])

        assert code == 0
        out = capsys.readouterr().out
        assert "从未启动" in out
        assert "数据: 暂无统计数据" in out

    def test_human_output_shows_last_error_and_stopped(
        self, tmp_path, monkeypatch, capsys
    ):
        config_path = write_config(tmp_path)
        db_path = make_db(tmp_path)
        state_path = write_running_state(db_path, phase="stopped")
        state = status.read_state(state_path)
        state["last_error"] = "模拟执行失败"
        status.write_state(state_path, state)
        monkeypatch.setattr(status, "pid_alive", lambda pid: False)

        code = cli.main(["info", "--config", str(config_path)])

        assert code == 0
        out = capsys.readouterr().out
        assert "已停止（正常退出）" in out
        assert "上次错误: 模拟执行失败" in out

    def test_empty_clan_id_and_missing_cookie_file_are_ok(
        self, tmp_path, capsys
    ):
        config_path = write_config(tmp_path)
        assert "clan_id" in config_path.read_text(encoding="utf-8")

        code = cli.main(["info", "--config", str(config_path)])

        assert code == 0
        assert "错误" not in capsys.readouterr().err