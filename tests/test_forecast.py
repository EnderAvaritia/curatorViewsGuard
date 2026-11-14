"""forecast_days 纯函数与 forecast 子命令的测试。

覆盖：恒定历史下的空转、growth 抬高、倍率归零、手动基线、stable、
trend / target 两种自然流量假设、--today 今日视角（固定自然流量）、
容量告警、空历史、日期回填与只读 DB。
"""

import json
import re
import sqlite3
from datetime import datetime

from curator_views import cli
from curator_views.core.forecast import forecast_days
from curator_views.core.scheduler import _TZ, _date_to_epoch, _today_epoch

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
        "batch_size": 50,
        "target_ratio": 0.95,
        "threads": 1,
        "wait_min": 8.0,
        "wait_max": 16.0,
    }
    config.update(overrides)
    return config


def warning_codes(result):
    return {item["code"] for item in result["warnings"]}


class TestDisplayWidth:
    def test_cjk_counts_as_two_columns(self):
        assert cli._display_width("日期") == 4
        assert cli._display_width("假设自然") == 8
        assert cli._display_width("2026-11-14") == 10

    def test_pad_display_aligns_by_display_width(self):
        assert cli._pad_display("假设自然", 10, "right") == "  假设自然"
        assert cli._pad_display("8,430.4", 10, "left") == "8,430.4   "
        assert (
            cli._display_width(cli._pad_display("假设自然", 10, "right")) == 10
        )


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

    def test_today_natural_holds_flat_and_repeats_gap(self):
        # 今日视角：自然流量固定为 400（实际 1000 − 刷量 600），
        # 目标 1000 × 1.5 = 1500 → 每天缺口都是 1100，
        # 不会因为昨天已经刷过就把今天的缺口抹平。
        result = forecast_days(
            make_history(days=10, views=1000),
            make_config(
                baseline_views=1000.0,
                boost_mode="growth",
                growth_factor=1.5,
            ),
            START_TS,
            days=3,
            first_natural=400,
        )
        rows = result["rows"]
        assert [row["assumed_natural"] for row in rows] == [400, 400, 400]
        assert [row["need"] for row in rows] == [1100, 1100, 1100]
        assert result["summary"]["total_need"] == 3300

    def test_first_natural_overrides_natural_mode(self):
        config = make_config(
            baseline_views=1000.0,
            boost_mode="growth",
            growth_factor=1.5,
        )
        trend = forecast_days(
            make_history(), config, START_TS, days=2,
            natural_mode="trend", first_natural=400,
        )
        target = forecast_days(
            make_history(), config, START_TS, days=2,
            natural_mode="target", first_natural=400,
        )
        assert [row["need"] for row in trend["rows"]] == [1100, 1100]
        assert [row["need"] for row in target["rows"]] == [1100, 1100]

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

    def test_throughput_capacity_warning(self):
        result = forecast_days(
            make_history(),
            make_config(
                baseline_views=6000.0,
                boost_mode="growth",
                growth_factor=1.5,
                boost_n=10,
                single_cap=1000,
                threads=1,
                wait_min=8.0,
                wait_max=16.0,
            ),
            START_TS,
            days=1,
        )
        assert "throughput_capacity" in warning_codes(result)
        assert result["summary"]["throughput_capacity"] == 5400

    def test_throughput_capacity_caps_threads_at_two(self):
        result = forecast_days(
            make_history(),
            make_config(
                baseline_views=6000.0,
                boost_mode="growth",
                growth_factor=1.5,
                boost_n=10,
                single_cap=1000,
                threads=10,
                wait_min=8.0,
                wait_max=16.0,
            ),
            START_TS,
            days=1,
        )
        assert result["summary"]["throughput_capacity"] == 10800
        assert "throughput_capacity" not in warning_codes(result)

    def test_single_recommendation_limits_concurrency(self):
        result = forecast_days(
            make_history(),
            make_config(
                baseline_views=6000.0,
                boost_mode="growth",
                growth_factor=1.5,
                boost_n=1,
                single_cap=10000,
                threads=2,
                wait_min=8.0,
                wait_max=16.0,
            ),
            START_TS,
            days=1,
        )
        assert result["summary"]["effective_threads"] == 1
        assert result["summary"]["throughput_capacity"] == 5400
        assert "throughput_capacity" in warning_codes(result)

    def test_zero_wait_has_no_throughput_limit(self):
        result = forecast_days(
            make_history(),
            make_config(
                baseline_views=6000.0,
                boost_mode="growth",
                growth_factor=1.5,
                threads=1,
                wait_min=0.0,
                wait_max=0.0,
            ),
            START_TS,
            days=1,
        )
        assert result["summary"]["throughput_capacity"] is None
        assert "throughput_capacity" not in warning_codes(result)

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
    # 相对“今天”生成 10 天恒定历史（最后一条是昨天），避免依赖机器时钟
    end_ts = _today_epoch() - DAY
    for i in range(10):
        conn.execute(
            "INSERT INTO daily_stats (date_ts, num_views) VALUES (?, ?)",
            (end_ts - DAY * (9 - i), 1000),
        )
    conn.execute(
        "INSERT INTO kv_store (key, value) VALUES ('program_start', ?)",
        (_iso(end_ts - DAY * 9),),
    )
    conn.commit()
    conn.close()
    return db_path


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=_TZ).strftime("%Y-%m-%d")


def make_today_db(tmp_path, boost: int = 100, end_offset: int = 1):
    """相对“今天”建库：最后一条是 end_offset 天前（默认昨天）。

    额外写入 boost_log，用于验证 --today 会从昨日实际里扣掉昨日刷量。
    """
    db_path = tmp_path / "views.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE daily_stats (date_ts INTEGER PRIMARY KEY, num_views INTEGER)"
    )
    conn.execute(
        "CREATE TABLE boost_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date TEXT, count INTEGER, note TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT)")
    end_ts = _today_epoch() - DAY * end_offset
    # 20 天数据，超过 window_days=14，避免触发启动期 0.9 系数
    for i in range(20):
        conn.execute(
            "INSERT INTO daily_stats (date_ts, num_views) VALUES (?, ?)",
            (end_ts - DAY * (19 - i), 1000),
        )
    conn.execute(
        "INSERT INTO kv_store (key, value) VALUES ('program_start', ?)",
        (_iso(end_ts - DAY * 19),),
    )
    conn.execute(
        "INSERT INTO boost_log (date, count, note, created_at) "
        "VALUES (?, ?, '', '')",
        (_iso(end_ts), boost),
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
        assert "3 天" in out
        assert len(set(re.findall(r"\d{4}-\d{2}-\d{2}", out))) >= 3
        assert not (tmp_path / "views.db-wal").exists()

    def test_cli_today_uses_yesterday_natural(self, tmp_path, capsys):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path, boost=100)
        before = db_path.read_bytes()

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--today",
                "--days",
                "3",
                "--json",
            ]
        )

        assert code == 0
        assert db_path.read_bytes() == before
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"]["perspective"] == "today"
        assert payload["source"]["natural_basis"] == "natural"
        assert payload["source"]["yesterday_actual"] == 1000
        assert payload["source"]["yesterday_boosted"] == 100
        assert payload["source"]["yesterday_natural"] == 900
        assert [row["assumed_natural"] for row in payload["rows"]] == [900] * 3
        assert [row["need"] for row in payload["rows"]] == [100] * 3

    def test_cli_today_prints_perspective(self, tmp_path, capsys):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path)

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--today",
                "--days",
                "2",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "含今天" in out
        assert "自然口径" in out
        assert "缺口公式" in out

    def test_cli_today_warns_when_history_is_stale(self, tmp_path, capsys):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path, end_offset=3)

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--today",
                "--json",
            ]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert "stale_history" in {item["code"] for item in payload["warnings"]}

    def test_cli_default_includes_today_with_natural_gap(
        self, tmp_path, capsys
    ):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path, boost=0)

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--set",
                "baseline_views=2000",
                "--days",
                "2",
                "--json",
            ]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"]["perspective"] == "today"
        assert payload["source"]["natural_basis"] == "natural"
        assert payload["rows"][0]["date"] == _iso(_today_epoch())
        assert payload["rows"][0]["assumed_natural"] == 1000
        assert payload["rows"][0]["need"] == 1000

    def test_cli_tomorrow_starts_next_day(self, tmp_path, capsys):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path, boost=0)

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--set",
                "baseline_views=2000",
                "--tomorrow",
                "--json",
            ]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"]["perspective"] == "tomorrow"
        assert payload["rows"][0]["date"] == _iso(_today_epoch() + DAY)
        assert payload["rows"][0]["assumed_natural"] == 1000
        assert payload["rows"][0]["need"] == 1000

    def test_cli_explicit_natural_trend_keeps_old_model(
        self, tmp_path, capsys
    ):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path, boost=0)

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--natural",
                "trend",
                "--json",
            ]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"]["natural_basis"] == "trend"
        assert payload["source"]["yesterday_natural"] is None

    def test_cli_table_columns_align_and_mark_over_capacity(
        self, tmp_path, capsys
    ):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path, boost=0)

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--set",
                "baseline_views=4000",
                "--days",
                "3",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        table_lines = [
            line
            for line in out.splitlines()
            if line.startswith("日期") or line.startswith("20")
        ]
        assert len(table_lines) == 4  # 表头 + 3 天
        assert len({cli._display_width(line) for line in table_lines}) == 1
        assert "单日容量" in out
        assert "目标系数" in out
        assert "倍率" not in out
        assert "容量不足" in out

    def test_cli_table_marks_throughput_shortage(self, tmp_path, capsys):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_today_db(tmp_path, boost=0)

        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--set",
                "baseline_views=4000",
                "--set",
                "wait_max=60",
                "--days",
                "1",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "单日吞吐上限" in out
        assert "吞吐不足" in out