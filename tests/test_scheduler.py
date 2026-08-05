"""scheduler 内部函数的单元测试：时区工具与子进程执行器。"""

import json
import sys
import os
from unittest.mock import patch

import pytest

from curator_views.core.scheduler import (
    _date_to_epoch,
    _run_executor,
    _today_epoch,
    _today_iso,
)


class TestTimezoneHelpers:
    def test_today_iso_format(self):
        iso = _today_iso()
        # YYYY-MM-DD
        parts = iso.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2

    def test_today_epoch_matches_iso(self):
        iso = _today_iso()
        epoch = _today_epoch()
        assert _date_to_epoch(iso) == epoch

    def test_date_to_epoch_known_value(self):
        # 2026-01-01 Asia/Shanghai 00:00 = 2025-12-31 16:00 UTC
        ts = _date_to_epoch("2026-01-01")
        assert ts == 1767196800


class TestRunExecutor:
    def test_dry_run_returns_zero_without_spawning(self):
        with patch("curator_views.core.scheduler.subprocess.run") as mock_run:
            result = _run_executor(
                "python cli.py boost --config {config} --target {need} --apps {apps}",
                need=500,
                boost_n=5,
                dry_run=True,
                config_path="/tmp/cfg.yaml",
            )
        mock_run.assert_not_called()
        assert result["ok"] is True
        assert result["boosted"] == 0
        assert "{need}" not in result["cmd"]
        assert "500" in result["cmd"]

    def test_missing_executable_returns_failure(self):
        result = _run_executor(
            "definitely_not_a_real_command_xyz --target {need}",
            need=10,
            boost_n=5,
            dry_run=False,
        )
        assert result["ok"] is False
        assert result["boosted"] == 0

    def test_parses_boosted_json_from_stdout(self):
        # 用真实 python 调起一个输出 JSON 的小脚本（不含裸花括号，避免 .format 冲突）
        mini = os.path.join(os.environ["TEMP"], "cv_mini_exec_test.py")
        with open(mini, "w", encoding="utf-8") as fh:
            fh.write("import json; print(json.dumps({'boosted': 42}))\n")
        try:
            result = _run_executor(
                sys.executable + " " + mini,
                need=100,
                boost_n=5,
                dry_run=False,
            )
        finally:
            if os.path.exists(mini):
                os.remove(mini)
        assert result["ok"] is True
        assert result["boosted"] == 42

    def test_nonzero_exit_returns_failure(self):
        # python -c 返回非零退出码
        cmd = sys.executable + ' -c "import sys; sys.exit(3)"'
        result = _run_executor(cmd, need=10, boost_n=5, dry_run=False)
        assert result["ok"] is False
        assert result["boosted"] == 0
