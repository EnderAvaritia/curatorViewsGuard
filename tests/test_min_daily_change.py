"""相邻两天最小变动（min_daily_change）行为测试。

覆盖三部分：
- ``_apply_min_daily_change`` 纯函数：方向、阈值、零变化、上下界夹取；
- ``run_daily``：从 daily_plan 读取上一日目标并按比例补足今日目标；
- ``forecast_days``：逐行套用最小变动，首日参照 prev_target。
"""

from datetime import datetime, timedelta

import pytest

from curator_views.core.daytime import logical_date
from curator_views.core.forecast import forecast_days
from curator_views.core.scheduler import (
    _TZ,
    _apply_min_daily_change,
    _date_to_epoch,
    run_daily,
)


# ── 纯函数 ───────────────────────────────────────────────────────────────


class TestApplyMinDailyChange:
    def test_above_threshold_is_untouched(self):
        adjusted, applied = _apply_min_daily_change(10500, 10000, 0.02)
        assert adjusted == 10500
        assert applied is False

    def test_small_positive_change_bumps_up(self):
        adjusted, applied = _apply_min_daily_change(10050, 10000, 0.02)
        assert adjusted == pytest.approx(10200.0)
        assert applied is True

    def test_small_negative_change_bumps_down(self):
        adjusted, applied = _apply_min_daily_change(9950, 10000, 0.02)
        assert adjusted == pytest.approx(9800.0)
        assert applied is True

    def test_zero_change_uses_direction_hint_up(self):
        adjusted, applied = _apply_min_daily_change(
            10000, 10000, 0.02, direction_hint=10
        )
        assert adjusted == pytest.approx(10200.0)
        assert applied is True

    def test_zero_change_uses_direction_hint_down(self):
        adjusted, applied = _apply_min_daily_change(
            10000, 10000, 0.02, direction_hint=-10
        )
        assert adjusted == pytest.approx(9800.0)
        assert applied is True

    def test_zero_hint_defaults_up(self):
        adjusted, applied = _apply_min_daily_change(10000, 10000, 0.02)
        assert adjusted == pytest.approx(10200.0)
        assert applied is True

    def test_no_prev_target_is_untouched(self):
        adjusted, applied = _apply_min_daily_change(10000, 0, 0.02)
        assert adjusted == 10000
        assert applied is False

    def test_disabled_threshold_is_untouched(self):
        adjusted, applied = _apply_min_daily_change(10000, 10000, 0)
        assert adjusted == 10000
        assert applied is False

    def test_clamped_to_target_max(self):
        adjusted, applied = _apply_min_daily_change(
            10000, 10000, 0.02, direction_hint=1, target_max=10100
        )
        assert adjusted == 10100
        assert applied is True

    def test_clamped_to_target_min(self):
        adjusted, applied = _apply_min_daily_change(
            10000, 10000, 0.02, direction_hint=-1, target_min=9950
        )
        assert adjusted == 9950
        assert applied is True

    def test_upward_pct_cannot_go_negative(self):
        # 比例被夹到 1.0，避免 prev × (1 - p) 出现负目标
        adjusted, applied = _apply_min_daily_change(
            10000, 10000, 5.0, direction_hint=-1
        )
        assert adjusted == pytest.approx(0.0)
        assert applied is True


# ── run_daily 集成 ───────────────────────────────────────────────────────


class FakeStorage:
    def __init__(self, program_start: str, plans: dict | None = None) -> None:
        self._program_start = program_start
        self.daily_plans = dict(plans or {})
        self.saved: dict = {}
        self.written_stats: list = []

    def upsert_daily_stats(self, date_ts: int, num_views: int) -> None:
        self.written_stats.append((date_ts, num_views))

    def save_daily_plan(self, plan: dict) -> None:
        self.daily_plans[plan["date"]] = dict(plan)

    def get_daily_plan(self, date: str):
        return self.daily_plans.get(date)

    def save_state(self, key: str, value: str) -> None:
        self.saved[key] = value

    def load_state(self, key: str, default: str = "") -> str:
        if key == "program_start":
            return self._program_start
        return self.saved.get(key, default)

    def get_boost_total(self, date: str) -> int:
        return 0


class FakeFetcher:
    def __init__(self, stats: list) -> None:
        self._stats = sorted(stats, key=lambda d: d["date_ts"])

    def fetch_stats(self):
        return list(self._stats)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent = []

    def send(self, title, body, level="info", category="alert") -> None:
        self.sent.append((title, body, level))

    def flush_pending(self) -> int:
        return 0


class FakeBooster:
    def __init__(self) -> None:
        self.batch_size = 50
        self.single_cap = 300
        self.target_ratio = 0.95
        self.aborted = False

    def run(self, target_count, n_recommendations=5, dry_run=False) -> int:
        return 0


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=_TZ)
TODAY_ISO = logical_date(NOW, 8 * 60)
YESTERDAY_ISO = (
    datetime.strptime(TODAY_ISO, "%Y-%m-%d") - timedelta(days=1)
).strftime("%Y-%m-%d")
PROGRAM_START = (
    datetime.strptime(TODAY_ISO, "%Y-%m-%d") - timedelta(days=30)
).strftime("%Y-%m-%d")


def _stats(days: int = 20, views: int = 10000) -> list:
    base_ts = _date_to_epoch(TODAY_ISO)
    return [
        {"date_ts": base_ts - (days - i) * 86400, "num_views": views}
        for i in range(days)
    ]


def _config(**overrides) -> dict:
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
        "baseline_days": 60,
        "slope_days": 14,
        "slope_threshold": 0.02,
        "baseline_views": 10000,
        "boost_mode": "stable",
        "growth_factor": 1.0,
        "growth_rate": 0.0,
        "target_min": 0,
        "target_max": 0,
        "volatility": 0.0,
        "volatility_min": 0.0,
        "target_volatility": 0.0,
        "target_volatility_min": 0.0,
        "min_daily_change": 0.02,
        "boost_enabled": False,
        "dry_run": True,
        "boost_list_file": "",
        "boost_list_fallback": True,
        "threads": 1,
        "wait_min": 8.0,
        "wait_max": 16.0,
        "single_cap": 300,
        "batch_size": 50,
        "target_ratio": 0.95,
        "boost_distribution": "ratio",
        "day_boundary": "08:00",
    }
    config.update(overrides)
    return config


def _run(storage: FakeStorage, **overrides) -> dict:
    return run_daily(
        storage=storage,
        fetcher=FakeFetcher(_stats()),
        notifier=FakeNotifier(),
        booster=FakeBooster(),
        config=_config(**overrides),
        now=NOW,
    )


class TestRunDailyMinChange:
    def test_bumps_target_up_from_yesterday_plan(self):
        storage = FakeStorage(
            PROGRAM_START,
            plans={YESTERDAY_ISO: {"date": YESTERDAY_ISO, "target": 10000}},
        )
        summary = _run(storage)

        assert summary["today_expected"] == pytest.approx(10200.0)
        assert summary["prev_target"] == pytest.approx(10000.0)
        assert summary["min_change_applied"] is True
        assert summary["min_daily_change"] == pytest.approx(0.02)

    def test_bumps_target_down_when_change_too_small(self):
        storage = FakeStorage(
            PROGRAM_START,
            plans={YESTERDAY_ISO: {"date": YESTERDAY_ISO, "target": 10050}},
        )
        summary = _run(storage)

        assert summary["today_expected"] == pytest.approx(9849.0)
        assert summary["min_change_applied"] is True

    def test_large_change_is_untouched(self):
        storage = FakeStorage(
            PROGRAM_START,
            plans={YESTERDAY_ISO: {"date": YESTERDAY_ISO, "target": 11000}},
        )
        summary = _run(storage)

        # 10000 vs 11000 变化约 -9%，已超过 2%，保持原目标
        assert summary["today_expected"] == pytest.approx(10000.0)
        assert summary["min_change_applied"] is False

    def test_without_plan_recomputes_previous_target(self):
        storage = FakeStorage(PROGRAM_START)
        summary = _run(storage)

        # 无定档时用历史重算昨日目标（固定基线 10000），再向上补到 10200
        assert summary["prev_target"] == pytest.approx(10000.0)
        assert summary["today_expected"] == pytest.approx(10200.0)
        assert summary["min_change_applied"] is True

    def test_respects_target_max(self):
        storage = FakeStorage(
            PROGRAM_START,
            plans={YESTERDAY_ISO: {"date": YESTERDAY_ISO, "target": 10000}},
        )
        summary = _run(storage, target_max=10100)

        assert summary["today_expected"] == pytest.approx(10100.0)
        assert summary["min_change_applied"] is True

    def test_disabled_keeps_natural_target(self):
        storage = FakeStorage(
            PROGRAM_START,
            plans={YESTERDAY_ISO: {"date": YESTERDAY_ISO, "target": 10000}},
        )
        summary = _run(storage, min_daily_change=0.0)

        assert summary["today_expected"] == pytest.approx(10000.0)
        assert summary["min_change_applied"] is False
        assert summary["prev_target"] is None


# ── forecast 集成 ────────────────────────────────────────────────────────


def _forecast(**overrides):
    config = _config(**overrides)
    history = _stats(days=5, views=10000)
    return forecast_days(
        history,
        config,
        _date_to_epoch(TODAY_ISO),
        days=3,
        model_volatility=False,
        first_natural=10000,
        prev_target=10000,
    )


class TestForecastMinChange:
    def test_each_adjacent_row_moves_at_least_threshold(self):
        result = _forecast()
        rows = result["rows"]

        assert rows[0]["target"] == pytest.approx(10200.0)
        assert rows[1]["target"] == pytest.approx(9996.0)
        assert rows[2]["target"] == pytest.approx(10195.9)
        for prev, cur in zip(rows, rows[1:]):
            ratio = abs(cur["target"] - prev["target"]) / prev["target"]
            # 行内 target 保留 1 位小数，允许极小的显示取整误差
            assert ratio >= 0.02 - 1e-3
        assert all(row["min_change_applied"] for row in rows)
        assert result["summary"]["min_daily_change"] == pytest.approx(0.02)

    def test_first_row_untouched_without_prev_target(self):
        config = _config()
        result = forecast_days(
            _stats(days=5, views=10000),
            config,
            _date_to_epoch(TODAY_ISO),
            days=2,
            model_volatility=False,
            first_natural=10000,
            prev_target=None,
        )
        rows = result["rows"]

        assert rows[0]["target"] == pytest.approx(10000.0)
        assert rows[0]["min_change_applied"] is False
        # 第二行起以第一行目标为参照补足
        assert rows[1]["target"] == pytest.approx(10200.0)
        assert rows[1]["min_change_applied"] is True

    def test_disabled_leaves_targets_flat(self):
        result = _forecast(min_daily_change=0.0)
        targets = [row["target"] for row in result["rows"]]

        assert targets == pytest.approx([10000.0, 10000.0, 10000.0])
        assert not any(row["min_change_applied"] for row in result["rows"])