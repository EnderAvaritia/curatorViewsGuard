"""每日分界点（day_boundary）测试。

Steam 后台通常在北京时间 08:00（UTC 0 点）切换“新的一天”，凌晨刷量会被
计入前一天。本文件覆盖：配置解析、逻辑日计算、run_daily 的“今天/昨天”
口径、boost_log 记账日期，以及 forecast 的今天/明天起算。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from curator_views import cli
from curator_views.core.booster import _today_str
from curator_views.core.daytime import (
    DEFAULT_DAY_BOUNDARY_MINUTES,
    day_label_from_ts,
    format_day_boundary,
    logical_date,
    parse_day_boundary,
    resolve_day_boundary,
)
from curator_views.core.scheduler import (
    _date_to_epoch,
    _today_epoch,
    _today_iso,
    run_daily,
)

TZ8 = timezone(timedelta(hours=8))


def dt(hour: int, minute: int = 0, day: int = 17) -> datetime:
    """构造北京时间 2026-11-{day} HH:MM。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ8)


def steam_ts(date_str: str) -> int:
    """构造 Steam 风格的统计时间戳：UTC 0 点（北京时间 08:00）。"""
    year, month, day = (int(part) for part in date_str.split("-"))
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


# ── 纯函数：解析与逻辑日 ─────────────────────────────────────────────────


class TestParseDayBoundary:
    def test_hh_mm_formats(self):
        assert parse_day_boundary("08:00") == 480
        assert parse_day_boundary("8:00") == 480
        assert parse_day_boundary("00:00") == 0
        assert parse_day_boundary("23:59") == 23 * 60 + 59
        assert parse_day_boundary(" 06:30 ") == 390

    def test_numeric_hours(self):
        assert parse_day_boundary(8) == 480
        assert parse_day_boundary(8.5) == 510
        assert parse_day_boundary("8") == 480
        assert parse_day_boundary(0) == 0

    def test_none_or_empty_uses_default(self):
        assert parse_day_boundary(None) == DEFAULT_DAY_BOUNDARY_MINUTES
        assert parse_day_boundary("") == DEFAULT_DAY_BOUNDARY_MINUTES

    @pytest.mark.parametrize(
        "value", ["nope", "24:00", "12:60", "-1", 24, 25.5, True, object()]
    )
    def test_invalid_falls_back_to_default(self, value, caplog):
        with caplog.at_level("WARNING", logger="curator_views.daytime"):
            assert parse_day_boundary(value) == DEFAULT_DAY_BOUNDARY_MINUTES
        assert any("day_boundary" in record.message for record in caplog.records)

    def test_format_round_trip(self):
        assert format_day_boundary(480) == "08:00"
        assert format_day_boundary(0) == "00:00"
        assert format_day_boundary(23 * 60 + 59) == "23:59"


class TestResolveDayBoundary:
    def test_string_key(self):
        assert resolve_day_boundary({"day_boundary": "06:30"}) == 390
        assert resolve_day_boundary({}) == DEFAULT_DAY_BOUNDARY_MINUTES

    def test_minutes_key_is_not_treated_as_hours(self):
        assert resolve_day_boundary({"day_boundary_minutes": 390}) == 390
        assert resolve_day_boundary({"day_boundary_minutes": 60}) == 60

    def test_minutes_key_takes_precedence(self):
        config = {"day_boundary": "00:00", "day_boundary_minutes": 390}
        assert resolve_day_boundary(config) == 390

    def test_invalid_minutes_falls_back(self, caplog):
        with caplog.at_level("WARNING", logger="curator_views.daytime"):
            assert (
                resolve_day_boundary({"day_boundary_minutes": 2000})
                == DEFAULT_DAY_BOUNDARY_MINUTES
            )
        assert any("day_boundary_minutes" in r.message for r in caplog.records)


class TestLogicalDate:
    def test_before_boundary_belongs_to_previous_day(self):
        assert logical_date(dt(0, 0), 480) == "2026-11-16"
        assert logical_date(dt(7, 59), 480) == "2026-11-16"
        assert logical_date(dt(8, 0), 480) == "2026-11-17"
        assert logical_date(dt(23, 59), 480) == "2026-11-17"

    def test_zero_boundary_is_calendar_day(self):
        assert logical_date(dt(0, 0), 0) == "2026-11-17"
        assert logical_date(dt(7, 59), 0) == "2026-11-17"

    def test_custom_boundary(self):
        assert logical_date(dt(5, 59), 360) == "2026-11-16"
        assert logical_date(dt(6, 0), 360) == "2026-11-17"

    def test_steam_style_labels(self):
        # UTC 0 点 = 北京时间 08:00，格式化后仍是同一天
        assert day_label_from_ts(steam_ts("2026-11-16")) == "2026-11-16"


class TestTodayHelpers:
    def test_default_boundary_is_08(self):
        now = dt(3, 0)
        assert _today_iso(now=now) == "2026-11-16"
        assert _today_epoch(now=now) == _date_to_epoch("2026-11-16")

    def test_zero_boundary(self):
        now = dt(3, 0)
        assert _today_iso(0, now) == "2026-11-17"
        assert _today_epoch(0, now) == _date_to_epoch("2026-11-17")

    def test_booster_ledger_date_uses_boundary(self):
        now = dt(3, 0)
        assert _today_str(480, now) == "2026-11-16"
        assert _today_str(0, now) == "2026-11-17"


class TestForecastStart:
    def test_tomorrow_starts_from_logical_today(self):
        now = dt(3, 0)
        assert cli._forecast_start_epoch(480, now) == _date_to_epoch(
            "2026-11-17"
        )
        assert cli._forecast_start_epoch(0, now) == _date_to_epoch(
            "2026-11-18"
        )


# ── run_daily 集成 ────────────────────────────────────────────────────────


class _Storage:
    def __init__(self, program_start: str, boost_totals: dict | None = None):
        self._program_start = program_start
        self.written_stats = []
        self.saved = {}
        self.boost_totals = boost_totals or {}
        self.daily_plans = {}

    def upsert_daily_stats(self, date_ts, num_views):
        self.written_stats.append((date_ts, num_views))

    def save_daily_plan(self, plan):
        self.daily_plans[plan["date"]] = dict(plan)

    def save_state(self, key, value):
        self.saved[key] = value

    def load_state(self, key, default=""):
        if key == "program_start":
            return self._program_start
        return self.saved.get(key, default)

    def get_boost_total(self, date):
        return int(self.boost_totals.get(date, 0))


class _Fetcher:
    def __init__(self, stats):
        self._stats = sorted(stats, key=lambda item: item["date_ts"])

    def fetch_stats(self):
        return list(self._stats)


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, title, body, level="info", category="alert"):
        self.sent.append((title, body, level))

    def flush_pending(self):
        return 0


class _Booster:
    def __init__(self):
        self.batch_size = 50
        self.single_cap = 300
        self.target_ratio = 0.95
        self.last_target = None

    def run(self, target_count, n_recommendations=5, dry_run=False):
        self.last_target = target_count
        return 0


def _history(end: str = "2026-11-16", days: int = 28, views: int = 1000,
             today_views: int = 99999):
    """生成 Steam 风格（UTC 0 点）的逐日统计，最后一天故意异常。"""
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    rows = []
    for offset in range(days):
        date_str = (end_dt - timedelta(days=days - 1 - offset)).strftime(
            "%Y-%m-%d"
        )
        value = today_views if offset == days - 1 else views
        rows.append({"date_ts": steam_ts(date_str), "num_views": value})
    return rows


def _run(stats, *, now, **overrides):
    storage = _Storage("2026-10-01", overrides.pop("boost_totals", None))
    config = {
        "window_days": 14,
        "trend_cap": 0.15,
        "startup_factor": 0.9,
        "trend_min_points": 5,
        "outlier_k": 0.30,
        "consecutive_days": 2,
        "absorb_days": 3,
        "exclude_flagged": True,
        "cooldown_days": 7,
        "baseline_views": 2000,
        "boost_mode": "stable",
        "growth_factor": 1.0,
        "growth_rate": 0.0,
        "volatility": 0.0,
        "boost_n": 5,
        "single_cap": 300,
        "batch_size": 50,
        "target_ratio": 0.95,
        "threads": 1,
        "wait_min": 0.0,
        "wait_max": 0.0,
        "boost_enabled": True,
        "dry_run": False,
    }
    config.update(overrides)
    summary = run_daily(
        storage,
        _Fetcher(stats),
        _Notifier(),
        _Booster(),
        config,
        now=now,
    )
    return summary, storage


class TestRunDailyBoundary:
    def test_early_morning_uses_previous_logical_day(self):
        summary, storage = _run(_history(), now=dt(3, 0))

        assert summary["date"] == "2026-11-16"
        assert summary["day_boundary"] == "08:00"
        # 逻辑今天的 Steam 记录（11-16）还没完整，必须被剔除；
        # 昨日基准应取 11-15 的 1000，而不是 99999。
        assert summary["yesterday_actual"] == 1000
        assert summary["need"] == 1000
        assert storage.saved["last_run_date"] == "2026-11-16"
        assert storage.daily_plans["2026-11-16"]["date_ts"] == _date_to_epoch(
            "2026-11-16"
        )

    def test_early_morning_ledger_uses_previous_day(self):
        summary, _ = _run(
            _history(),
            now=dt(3, 0),
            boost_totals={"2026-11-15": 400},
        )
        assert summary["yesterday_actual"] == 1000
        assert summary["yesterday_boosted"] == 400
        assert summary["yesterday_natural"] == 600
        assert summary["need"] == 1400

    def test_after_boundary_uses_calendar_day(self):
        summary, storage = _run(_history(), now=dt(9, 0))

        assert summary["date"] == "2026-11-17"
        # 11-16 现在是最近完整日
        assert summary["yesterday_actual"] == 99999
        assert summary["need"] == 0
        assert storage.saved["last_run_date"] == "2026-11-17"

    def test_zero_boundary_restores_calendar_day(self):
        # 与旧行为兼容：00:00 分界时凌晨 3 点仍算 11-17
        summary, storage = _run(
            _history(),
            now=dt(3, 0),
            day_boundary="00:00",
        )
        assert summary["date"] == "2026-11-17"
        assert summary["yesterday_actual"] == 99999
        assert storage.saved["last_run_date"] == "2026-11-17"

    def test_minutes_key_takes_precedence(self):
        summary, _ = _run(
            _history(),
            now=dt(3, 0),
            day_boundary="00:00",
            day_boundary_minutes=390,
        )
        # 06:30 分界：03:00 仍属于 11-16
        assert summary["date"] == "2026-11-16"
        assert summary["day_boundary"] == "06:30"


class TestConfigPlumbing:
    def test_template_default_is_08(self, tmp_path):
        sched = cli._scheduler_config(
            {}, boost_enabled=False, dry_run=True, cfg_dir=tmp_path
        )
        assert sched["day_boundary_minutes"] == 480

    def test_config_value_flows_to_scheduler(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            'clan_id: "1"\nday_boundary: "06:30"\n', encoding="utf-8"
        )
        cfg = cli._load_config(config_path)
        sched = cli._scheduler_config(
            cfg, boost_enabled=False, dry_run=True, cfg_dir=tmp_path
        )
        assert sched["day_boundary_minutes"] == 390

    def test_info_reports_day_boundary(self, tmp_path, capsys):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            'clan_id: "1"\ndb_path: "missing.db"\nday_boundary: "07:15"\n',
            encoding="utf-8",
        )
        import json

        code = cli.main(["info", "--config", str(config_path), "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["config"]["day_boundary"] == "07:15"