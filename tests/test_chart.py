"""forecast 折线图：HTML/SVG 渲染与 --chart 集成测试。"""

import json
import sqlite3
from datetime import datetime

from curator_views import cli
from curator_views.core.chart import render_forecast_html, write_forecast_chart
from curator_views.core.forecast import forecast_days
from curator_views.core.scheduler import _TZ, _date_to_epoch, _today_epoch

DAY = 86400
START_TS = _date_to_epoch("2026-11-14")

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

    def test_write_creates_parent_directories(self, tmp_path):
        target = tmp_path / "nested" / "chart.html"
        written = write_forecast_chart(
            target, make_result(), make_config()
        )

        assert written == target
        assert target.is_file()
        assert "<svg" in target.read_text(encoding="utf-8")


class TestForecastChartCli:
    def _run(self, tmp_path, extra):
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
                "3",
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

    def test_chart_default_path_is_next_to_config(self, tmp_path):
        code = self._run(tmp_path, ["--chart"])

        assert code == 0
        assert (tmp_path / "forecast.html").is_file()

    def test_chart_with_json_keeps_stdout_valid(self, tmp_path, capsys):
        chart_path = tmp_path / "chart.html"

        code = self._run(
            tmp_path, ["--chart", str(chart_path), "--json"]
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"]["chart_path"] == str(chart_path)
        assert chart_path.is_file()

    def test_no_chart_by_default(self, tmp_path):
        code = self._run(tmp_path, [])

        assert code == 0
        assert not (tmp_path / "forecast.html").exists()

    def test_chart_write_failure_returns_error(
        self, tmp_path, capsys
    ):
        target_dir = tmp_path / "chart_dir"
        target_dir.mkdir()

        code = self._run(tmp_path, ["--chart", str(target_dir)])

        assert code == 1
        assert "生成折线图失败" in capsys.readouterr().err