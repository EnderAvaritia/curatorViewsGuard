"""forecast 折线图：HTML/SVG 渲染与 --chart 集成测试。"""

import json
import sqlite3
from datetime import datetime

from curator_views import cli
from curator_views.core.chart import (
    render_forecast_console,
    render_forecast_html,
    write_forecast_chart,
)
from curator_views.core.forecast import forecast_days
from curator_views.core.scheduler import _TZ, _date_to_epoch, _today_epoch

DAY = 86400
START_TS = _date_to_epoch("2026-11-14")


def day_iso(offset=0):
    """返回相对“今天”偏移 offset 天的 YYYY-MM-DD（避免测试随日历失效）。"""
    return datetime.fromtimestamp(
        _today_epoch() + DAY * offset, tz=_TZ
    ).strftime("%Y-%m-%d")


def day_mmdd(offset=0):
    """返回相对“今天”偏移 offset 天的 MM-DD。"""
    return day_iso(offset)[5:]

CLI_CONFIG = """\
clan_id: ""
cookie_file: "missing_cookie.txt"
db_path: "views.db"
baseline_views: 1000
boost_mode: stable
volatility: 0.0
"""


def make_history(days=20, views=1000):
    end_ts = _date_to_epoch("2026-11-13")
    return [
        {"date_ts": end_ts - DAY * (days - 1 - index), "num_views": views}
        for index in range(days)
    ]


def make_config(**overrides):
    config = {
        "window_days": 14,
        "trend_cap": 0.15,
        "startup_factor": 0.9,
        "trend_min_points": 5,
        "baseline_views": 1000.0,
        "boost_mode": "stable",
        "growth_factor": 1.0,
        "growth_rate": 0.0,
        "volatility": 0.0,
        "volatility_min": 0.0,
        "target_volatility": 0.0,
        "target_volatility_min": 0.0,
        "target_min": 0.0,
        "target_max": 0.0,
        "boost_n": 5,
        "single_cap": 300,
        "batch_size": 50,
        "threads": 1,
        "wait_min": 8.0,
        "wait_max": 16.0,
    }
    config.update(overrides)
    return config


def make_result(**overrides):
    return forecast_days(
        make_history(),
        make_config(**overrides),
        START_TS,
        days=5,
        model_volatility=False,
    )


def make_cli_db(tmp_path):
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
    end_ts = _today_epoch() - DAY
    for index in range(20):
        conn.execute(
            "INSERT INTO daily_stats (date_ts, num_views) VALUES (?, ?)",
            (end_ts - DAY * (19 - index), 1000),
        )
    program_start = datetime.fromtimestamp(
        end_ts - DAY * 19, tz=_TZ
    ).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO kv_store (key, value) VALUES ('program_start', ?)",
        (program_start,),
    )
    conn.commit()
    conn.close()
    return db_path


class TestRenderForecastHtml:
    def test_contains_two_svg_charts_and_series(self):
        result = make_result()
        page = render_forecast_html(result, make_config())

        assert page.startswith("<!doctype html>")
        assert page.count("<svg") == 2
        assert "2026-11-14" in page
        assert "2026-11-18" in page
        assert "目标" in page
        assert "假设自然流量" in page
        assert "当日配额" in page
        assert "实际待刷" in page
        assert "<polyline" in page

    def test_renders_target_range_band(self):
        result = make_result(target_volatility=0.50)
        page = render_forecast_html(
            result, make_config(target_volatility=0.50)
        )

        assert "<polygon" in page
        assert "目标区间 ±50%" in page

    def test_no_band_without_target_volatility(self):
        page = render_forecast_html(make_result(), make_config())

        assert "<polygon" not in page
        assert "目标区间 ±" not in page

    def test_empty_rows_do_not_crash(self):
        page = render_forecast_html(
            {"rows": [], "summary": {}, "warnings": []}, make_config()
        )

        assert page.count("没有可绘制的数据") == 2

    def test_notes_random_sampling_state(self):
        modeled = forecast_days(
            make_history(),
            make_config(volatility=0.10),
            START_TS,
            days=2,
        )
        assert "已纳入随机波动" in render_forecast_html(
            modeled, make_config(volatility=0.10)
        )
        assert "未纳入随机波动" in render_forecast_html(
            make_result(), make_config()
        )

    def test_notes_persisted_first_day(self):
        config = make_config(volatility=0.10)
        result = forecast_days(make_history(), config, START_TS, days=2)
        result["summary"]["first_plan_status"] = "persisted"

        page = render_forecast_html(result, config)
        assert "首日为定档值，其余为本次抽样" in page

    def test_write_creates_parent_directories(self, tmp_path):
        target = tmp_path / "nested" / "chart.html"
        written = write_forecast_chart(
            target, make_result(), make_config()
        )

        assert written == target
        assert target.is_file()
        assert "<svg" in target.read_text(encoding="utf-8")


class TestRenderForecastConsole:
    def test_renders_two_dot_charts(self):
        text = render_forecast_console(make_result(), make_config(), width=80)

        assert "浏览量：目标 vs 假设自然流量" in text
        assert "刷量：当日配额 vs 实际待刷" in text
        assert "·" in text
        assert "● 目标" in text
        assert "○ 假设自然流量" in text
        assert "◆ 当日配额" in text
        assert "◇ 实际待刷" in text
        assert "2026-11-14" in text
        assert "区间预计刷量" in text
        assert "[" not in text

    def test_respects_requested_width(self):
        text = render_forecast_console(make_result(), make_config(), width=60)

        axis_lines = [line for line in text.splitlines() if "┤" in line or "└" in line]
        assert axis_lines
        assert max(len(line) for line in axis_lines) <= 60

    def test_reports_random_sampling_state(self):
        modeled = forecast_days(
            make_history(),
            make_config(volatility=0.10),
            START_TS,
            days=2,
        )
        text = render_forecast_console(
            modeled, make_config(volatility=0.10), width=72
        )

        assert "已纳入随机波动" in text
        assert "重新运行可能不同" in text

    def test_notes_persisted_first_day(self):
        config = make_config(volatility=0.10)
        result = forecast_days(make_history(), config, START_TS, days=2)
        text = render_forecast_console(
            result,
            config,
            {"today_plan_status": "persisted"},
            width=72,
        )

        assert "首日为定档值，其余为本次抽样" in text

    def test_empty_rows_do_not_crash(self):
        text = render_forecast_console(
            {"rows": [], "summary": {}, "warnings": []}, make_config()
        )

        assert "没有可绘制的数据" in text


class TestForecastChartCli:
    def _run(self, tmp_path, extra, days=3):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CLI_CONFIG, encoding="utf-8")
        db_path = make_cli_db(tmp_path)
        return cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                f"db_path={db_path}",
                "--days",
                str(days),
                *extra,
            ]
        )

    def test_chart_flag_writes_file_and_prints_path(
        self, tmp_path, capsys
    ):
        chart_path = tmp_path / "chart.html"

        code = self._run(tmp_path, ["--chart", str(chart_path)])

        assert code == 0
        assert chart_path.is_file()
        assert "折线图:" in capsys.readouterr().out

    def test_chart_without_path_prints_console(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--chart"])

        captured = capsys.readouterr()
        assert code == 0
        assert "浏览量：目标 vs 假设自然流量" in captured.out
        assert "刷量：当日配额 vs 实际待刷" in captured.out
        assert not (tmp_path / "forecast.html").exists()

    def test_day_step_samples_table_and_chart(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--day-step", "2", "--chart-only"])

        captured = capsys.readouterr()
        assert code == 0
        assert day_mmdd(0) in captured.out
        assert day_mmdd(2) in captured.out
        assert day_mmdd(1) not in captured.out

    def test_day_step_samples_table(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--day-step", "2"])

        captured = capsys.readouterr()
        assert code == 0
        assert "输出间隔: 每 2 天" in captured.out
        assert day_iso(0) in captured.out
        assert day_iso(2) in captured.out
        assert f"\n{day_iso(1)} " not in captured.out

    def test_day_step_samples_json_rows(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--day-step", "2", "--json"])

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert [row["date"] for row in payload["rows"]] == [
            day_iso(0),
            day_iso(2),
        ]
        assert payload["summary"]["days"] == 3
        assert payload["source"]["day_step"] == 2

    def test_chart_only_skips_forecast_table(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--chart-only"])

        captured = capsys.readouterr()
        assert code == 0
        assert "浏览量：目标 vs 假设自然流量" in captured.out
        assert "刷量：当日配额 vs 实际待刷" in captured.out
        assert "目标系数" not in captured.out
        assert "汇总:" not in captured.out
        assert "预测区间：" not in captured.out
        assert "区间预计刷量" not in captured.out
        assert not (tmp_path / "forecast.html").exists()

    def test_chart_only_with_html_path_writes_file_without_report(
        self, tmp_path, capsys
    ):
        chart_path = tmp_path / "chart.html"

        code = self._run(
            tmp_path, ["--chart", str(chart_path), "--chart-only"]
        )

        captured = capsys.readouterr()
        assert code == 0
        assert captured.out == ""
        assert chart_path.is_file()

    def test_day_step_keeps_last_day_when_not_aligned(self, tmp_path, capsys):
        code = self._run(
            tmp_path, ["--day-step", "3", "--json"], days=5
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert [row["date"] for row in payload["rows"]] == [
            day_iso(0),
            day_iso(3),
            day_iso(4),
        ]

    def test_day_step_rejects_zero(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--day-step", "0"])

        assert code == 1
        assert "--day-step" in capsys.readouterr().err

    def test_chart_only_rejects_json(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--chart-only", "--json"])

        assert code == 1
        assert "--chart-only" in capsys.readouterr().err

    def test_chart_dash_prints_console(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--chart", "-"])

        assert code == 0
        assert "浏览量：目标 vs 假设自然流量" in capsys.readouterr().out

    def test_chart_with_json_keeps_stdout_valid(self, tmp_path, capsys):
        chart_path = tmp_path / "chart.html"

        code = self._run(
            tmp_path, ["--chart", str(chart_path), "--json"]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"]["chart_path"] == str(chart_path)
        assert payload["source"]["chart_format"] == "html"
        assert chart_path.is_file()

    def test_console_chart_with_json_uses_stderr(self, tmp_path, capsys):
        code = self._run(tmp_path, ["--chart", "--json"])

        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["source"]["chart_format"] == "console"
        assert "浏览量：目标 vs 假设自然流量" in captured.err

    def test_day_step_applies_to_html(self, tmp_path):
        chart_path = tmp_path / "chart.html"

        code = self._run(
            tmp_path, ["--chart", str(chart_path), "--day-step", "2"]
        )

        assert code == 0
        page = chart_path.read_text(encoding="utf-8")
        # 只看图表本体：页面头部的“生成时间”也可能落在被跳过的日期上，
        # 不能用来判断 day-step 是否生效。
        chart_body = page[page.index("<svg"):]
        assert day_iso(0) in chart_body
        assert day_iso(2) in chart_body
        assert day_iso(1) not in chart_body

    def test_no_chart_by_default(self, tmp_path, capsys):
        code = self._run(tmp_path, [])

        captured = capsys.readouterr()
        assert code == 0
        assert "浏览量：目标 vs 假设自然流量" not in captured.out
        assert not (tmp_path / "forecast.html").exists()

    def test_chart_write_failure_returns_error(
        self, tmp_path, capsys
    ):
        target_dir = tmp_path / "chart_dir"
        target_dir.mkdir()

        code = self._run(tmp_path, ["--chart", str(target_dir)])

        assert code == 1
        assert "生成折线图失败" in capsys.readouterr().err