"""forecast_days 纯函数与 forecast 子命令的测试。

覆盖：恒定历史下的空转、growth 抬高、倍率归零、手动基线、stable、
trend / target 两种自然流量假设、容量告警、空历史、日期回填与只读 DB。
"""

import re
import sqlite3

from curator_views import cli
from curator_views.core.forecast import forecast_days
from curator_views.core.scheduler import _date_to_epoch

DAY = 86400
START_TS = _date_to_epoch("2026-11-14")


def make_history(days=30, views=1000, end="2026-11-13"):
    """构造按 date_ts 升序、每天浏览量恒定的历史。"""
    end_ts = _date_to_epoch(end)
    return [
        {"date_ts": end_ts - DAY * (days - 1 - i), "num_views": views}
        for i in range(days)
    ]


def make_config(**overrides):
    """与 cli._scheduler_config 返回值键一致的配置基线。"""
    config = {
        "window_days": 14,
        "trend_cap": 0.15,
        "startup_factor": 0.9,
        "trend_min_points": 5,
        "baseline_views": 0.0,
        "boost_mode": "stable",
        "growth_factor": 1.0,
        "growth_rate": 0.0,
        "boost_n": 5,
        "single_cap": 300,
        "target_ratio": 0.95,
    }
    config.update(overrides)
    return config


def warning_codes(result):
    return {item["code"] for item in result["warnings"]}


class TestForecastDays:
    def test_declining_factor_all_zero_and_warns(self):
        # 默认用户配置：0.85 托底 + 每天 -1%，目标始终低于自然流量
        result = forecast_days(
            make_history(),
            make_config(
                boost_mode="growth", growth_factor=0.85, growth_rate=-0.01
            ),
            START_TS,
            days=7,
        )
        assert all(row["need"] == 0 for row in result["rows"])
        assert "need_all_zero" in warning_codes(result)
        assert result["summary"]["total_need"] == 0

    def test_growth_active_raises_target_and_need(self):
        result = forecast_days(
            make_history(),
            make_config(boost_mode="growth", growth_factor=1.5),
            START_TS,
            days=7,
        )
        first = result["rows"][0]
        assert first["target"] > first["base"]
        assert any(row["need"] > 0 for row in result["rows"])

    def test_negative_rate_reaches_zero_multiplier(self):
        # 第 100 天（0 基）时 1 + (-0.01 * 100) = 0
        result = forecast_days(
            make_history(),
            make_config(boost_mode="growth", growth_factor=1.0, growth_rate=-0.01),
            START_TS,
            days=101,
        )
        assert "multiplier_zero" in warning_codes(result)
        assert result["rows"][100]["multiplier"] == 0.0

    def test_manual_baseline_is_fixed_base(self):
        # 历史实际是 5000，手动基线 1000 时基准必须固定为 1000
        result = forecast_days(
            make_history(views=5000),
            make_config(baseline_views=1000.0),
            START_TS,
            days=5,
        )
        assert all(row["base"] == 1000.0 for row in result["rows"])

    def test_stable_mode_multiplier_is_one(self):
        result = forecast_days(
            make_history(),
            make_config(
                boost_mode="stable", growth_factor=1.5, growth_rate=0.01
            ),
            START_TS,
            days=5,
        )
        assert all(row["multiplier"] == 1.0 for row in result["rows"])

    def test_trend_and_target_natural_modes_differ(self):
        config = make_config(boost_mode="growth", growth_factor=1.5)
        trend = forecast_days(make_history(), config, START_TS, days=3,
                              natural_mode="trend")
        target = forecast_days(make_history(), config, START_TS, days=3,
                               natural_mode="target")
        trend_needs = [row["need"] for row in trend["rows"]]
        target_needs = [row["need"] for row in target["rows"]]
        assert trend_needs != target_needs

    def test_over_capacity_warning(self):
        result = forecast_days(
            make_history(),
            make_config(
                baseline_views=5000.0,
                boost_mode="growth",
                growth_factor=1.5,
                boost_n=5,
                single_cap=300,
            ),
            START_TS,
            days=1,
        )
        assert "over_capacity" in warning_codes(result)

    def test_empty_history_does_not_raise(self):
        result = forecast_days(make_history(), make_config(), START_TS, days=0)[
            "rows"
        ]
        assert result == []

        result = forecast_days([], make_config(), START_TS, days=3)
        assert len(result["rows"]) == 3
        assert "no_history" in warning_codes(result)

    def test_date_backfill_uses_shanghai_dates(self):
        result = forecast_days(make_history(), make_config(), START_TS, days=3)
        expected_dates = ["2026-11-14", "2026-11-15", "2026-11-16"]
        for index, row in enumerate(result["rows"]):
            assert row["date_ts"] == START_TS + DAY * index
            assert row["date"] == expected_dates[index]
            assert row["in_cooldown"] is False


CLI_CONFIG = """\
clan_id: ""
cookie_file: "missing_cookie.txt"
db_path: "views.db"
boost_mode: stable
"""


def make_readonly_db(tmp_path):
    """用裸 sqlite3 建库（不经过 Storage，避免 WAL 副作用）。"""
    db_path = tmp_path / "views.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE daily_stats (date_ts INTEGER PRIMARY KEY, num_views INTEGER)"
    )
    conn.execute("CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT)")
    # 用固定 UTC+8 日期生成 10 天恒定历史
    end_ts = _date_to_epoch("2026-11-12")
    for i in range(10):
        conn.execute(
            "INSERT INTO daily_stats (date_ts, num_views) VALUES (?, ?)",
            (end_ts - DAY * (9 - i), 1000),
        )
    conn.execute(
        "INSERT INTO kv_store (key, value) VALUES ('program_start', '2026-11-03')"
    )
    conn.commit()
    conn.close()
    return db_path


class TestForecastCli:
    def test_cli_is_readonly_and_prints_dates(self, tmp_path, capsys):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_readonly_db(tmp_path)
        before = db_path.read_bytes()

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--days",
                "3",
            ]
        )

        after = db_path.read_bytes()
        assert code == 0
        assert before == after
        out = capsys.readouterr().out
        assert "（3 天）" in out
        assert len(set(re.findall(r"\d{4}-\d{2}-\d{2}", out))) >= 3
        assert not (tmp_path / "views.db-wal").exists()