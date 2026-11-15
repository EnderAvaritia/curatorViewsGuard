"""daily_plan 定档与 forecast 首日复用的测试。

覆盖：Storage 定档读写与旧库升级、run_daily 写定档/同日覆盖/dry-run 不写、
forecast_days 首日复用定档、签名不一致回退抽样、列表/恢复期口径。
"""

import random
import sqlite3

from curator_views.core.forecast import forecast_days
from curator_views.core.scheduler import (
    _date_to_epoch,
    _jitter_signature,
    _normalize_target_bounds,
    _today_epoch,
    run_daily,
)
from curator_views.core.storage import Storage

DAY = 86400
START_TS = _date_to_epoch("2026-11-14")


# ── 依赖替身 ─────────────────────────────────────────────────────────────


class _Fetcher:
    def __init__(self, stats):
        self._stats = sorted(stats, key=lambda d: d["date_ts"])

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
        return target_count


# ── 工具函数 ─────────────────────────────────────────────────────────────


def make_history(days=30, views=1000, end="2026-11-13"):
    end_ts = _date_to_epoch(end)
    return [
        {"date_ts": end_ts - DAY * (days - 1 - i), "num_views": views}
        for i in range(days)
    ]


def make_config(**overrides):
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


def plan_signature(config):
    """按 forecast/run 的共同口径计算配置签名。"""
    low, high = _normalize_target_bounds(
        float(config.get("target_min", 0) or 0),
        float(config.get("target_max", 0) or 0),
    )
    return _jitter_signature(
        float(config.get("baseline_views", 0) or 0),
        str(config.get("boost_mode", "stable")).lower(),
        float(config.get("growth_factor", 1.0) or 1.0),
        float(config.get("growth_rate", 0) or 0),
        low,
        high,
        float(config.get("volatility", 0) or 0),
        float(config.get("volatility_min", 0) or 0),
        float(config.get("target_volatility", 0) or 0),
        float(config.get("target_volatility_min", 0) or 0),
    )


def make_plan(config, **overrides):
    plan = {
        "date": "2026-11-14",
        "signature": plan_signature(config),
        "target": 1234.5,
        "target_jitter": 0.10,
        "gap_jitter": -0.05,
        "gross_need": 800,
        "boost_quota": 760,
        "in_cooldown": False,
        "list_status": "disabled",
        "list_amount": None,
        "target_bound": "",
        "updated_at": "2026-11-14T10:00:00",
    }
    plan.update(overrides)
    return plan


def warning_codes(result):
    return {item["code"] for item in result["warnings"]}


def make_running_stats(days=30, views=500):
    end_ts = _today_epoch() - DAY
    return [
        {"date_ts": end_ts - DAY * (days - 1 - i), "num_views": views}
        for i in range(days)
    ]


def run_with_storage(storage, stats, dry_run=False, **overrides):
    config = {
        "boost_enabled": True,
        "dry_run": dry_run,
        "baseline_views": 1000,
        "volatility": 0.0,
        "target_volatility": 0.0,
    }
    config.update(overrides)
    return run_daily(
        storage, _Fetcher(stats), _Notifier(), _Booster(), config
    )


# ── Storage ──────────────────────────────────────────────────────────────


class TestStorageDailyPlan:
    def test_roundtrip_and_optional_list_amount(self, tmp_path):
        storage = Storage(str(tmp_path / "views.db"))
        plan = {
            "date": "2026-11-14",
            "date_ts": START_TS,
            "base": 1000.0,
            "multiplier": 1.0,
            "raw_target": 1000.0,
            "target": 1000.0,
            "target_jitter": 0.0,
            "gap_jitter": -0.05,
            "deduction": 0.0,
            "gross_need": 500,
            "boost_quota": 475,
            "need": 475,
            "today_boosted": 0,
            "target_bound": "",
            "in_cooldown": True,
            "list_status": "disabled",
            "list_amount": None,
            "signature": "sig",
            "updated_at": "2026-11-14T10:00:00",
        }
        storage.save_daily_plan(plan)

        got = storage.get_daily_plan("2026-11-14")
        assert got is not None
        assert got["date_ts"] == START_TS
        assert got["target"] == 1000.0
        assert got["gap_jitter"] == -0.05
        assert got["gross_need"] == 500
        assert got["boost_quota"] == 475
        assert got["need"] == 475
        assert got["in_cooldown"] is True
        assert got["list_amount"] is None
        assert got["signature"] == "sig"

    def test_unknown_keys_are_ignored(self, tmp_path):
        storage = Storage(str(tmp_path / "views.db"))
        storage.save_daily_plan(
            {"date": "2026-11-14", "target": 1.0, "unknown": "ignored"}
        )
        got = storage.get_daily_plan("2026-11-14")
        assert "unknown" not in got
        assert got["target"] == 1.0

    def test_get_daily_plans_orders_by_date(self, tmp_path):
        storage = Storage(str(tmp_path / "views.db"))
        storage.save_daily_plan({"date": "2026-11-16", "target": 3.0})
        storage.save_daily_plan({"date": "2026-11-14", "target": 1.0})
        storage.save_daily_plan({"date": "2026-11-15", "target": 2.0})

        plans = storage.get_daily_plans()
        assert [plan["date"] for plan in plans] == [
            "2026-11-14", "2026-11-15", "2026-11-16",
        ]
        assert storage.get_daily_plan("2026-11-30") is None

    def test_old_schema_upgrades_without_data_loss(self, tmp_path):
        db_path = tmp_path / "views.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE daily_stats "
            "(date_ts INTEGER PRIMARY KEY, num_views INTEGER)"
        )
        conn.execute(
            "INSERT INTO daily_stats (date_ts, num_views) VALUES (?, ?)",
            (START_TS, 1234),
        )
        conn.commit()
        conn.close()

        storage = Storage(str(db_path))
        assert storage.get_daily_stats() == [
            {"date_ts": START_TS, "num_views": 1234}
        ]
        assert storage.get_daily_plans() == []
        storage.save_daily_plan({"date": "2026-11-14", "target": 999.0})
        assert storage.get_daily_plan("2026-11-14")["target"] == 999.0


# ── run_daily 定档 ────────────────────────────────────────────────────────


class TestRunDailyPersistsPlan:
    def test_writes_one_row_with_final_decision(self, tmp_path):
        storage = Storage(str(tmp_path / "views.db"))
        summary = run_with_storage(storage, make_running_stats())

        plan = storage.get_daily_plan(summary["date"])
        assert plan is not None
        # 只写首日（今天）一行：不预写未来日期，也不重写历史
        assert [item["date"] for item in storage.get_daily_plans()] == [
            summary["date"]
        ]
        assert plan["target"] == 1000.0
        assert plan["gross_need"] == 1000
        assert plan["boost_quota"] == 1000
        assert plan["need"] == 1000
        assert plan["today_boosted"] == 0
        assert plan["in_cooldown"] is False
        assert plan["list_status"] == "disabled"
        assert plan["signature"] == _jitter_signature(
            1000.0, "stable", 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        )

    def test_same_day_rerun_overwrites_and_keeps_target(self, tmp_path):
        storage = Storage(str(tmp_path / "views.db"))
        stats = make_running_stats()
        first = run_with_storage(storage, stats)
        first_plan = storage.get_daily_plan(first["date"])

        storage.log_boost(first["date"], 300)
        second = run_with_storage(storage, stats)
        second_plan = storage.get_daily_plan(second["date"])

        assert len(storage.get_daily_plans()) == 1
        assert second_plan["need"] == 700
        assert second_plan["today_boosted"] == 300
        assert second_plan["target"] == first_plan["target"]
        assert second_plan["target_jitter"] == first_plan["target_jitter"]
        assert second_plan["gap_jitter"] == first_plan["gap_jitter"]
        assert second_plan["signature"] == first_plan["signature"]

    def test_dry_run_writes_no_plan(self, tmp_path):
        storage = Storage(str(tmp_path / "views.db"))
        summary = run_with_storage(
            storage, make_running_stats(), dry_run=True
        )
        assert summary["dry_run"] is True
        assert storage.get_daily_plans() == []


# ── forecast 首日复用 ────────────────────────────────────────────────────


class TestForecastReusesPlan:
    def test_matching_plan_is_reused_and_future_days_predicted(self):
        config = make_config(baseline_views=1000.0)
        plan = make_plan(config)
        result = forecast_days(
            make_history(), config, START_TS, days=3, first_day_plan=plan
        )

        first = result["rows"][0]
        assert first["plan_source"] == "persisted"
        assert first["plan_updated_at"] == "2026-11-14T10:00:00"
        assert first["target"] == 1234.5
        assert first["target_jitter"] == 0.10
        assert first["gap_jitter"] == -0.05
        assert first["gross_need"] == 800
        assert first["boost_quota"] == 760
        assert first["need"] == 760
        assert first["in_cooldown"] is False
        assert all(
            row["plan_source"] == "predicted"
            for row in result["rows"][1:]
        )
        assert result["summary"]["first_plan_status"] == "persisted"
        assert (
            result["summary"]["first_plan_updated_at"]
            == "2026-11-14T10:00:00"
        )

    def test_signature_mismatch_falls_back_to_sampling(self):
        config = make_config(baseline_views=1000.0)
        plan = make_plan(config, signature="different-signature")
        result = forecast_days(
            make_history(), config, START_TS, days=2, first_day_plan=plan
        )

        assert result["rows"][0]["plan_source"] == "predicted"
        assert all(
            row["plan_source"] == "predicted" for row in result["rows"]
        )
        assert result["summary"]["first_plan_status"] == "signature_mismatch"
        assert result["summary"]["first_plan_updated_at"] is None
        assert "plan_signature_mismatch" in warning_codes(result)

    def test_no_plan_matches_baseline_behavior(self):
        config = make_config(
            volatility=0.20, target_volatility=0.20
        )
        random.seed(20261114)
        baseline = forecast_days(make_history(), config, START_TS, days=4)
        random.seed(20261114)
        without_plan = forecast_days(
            make_history(), config, START_TS, days=4, first_day_plan=None
        )

        assert baseline["rows"] == without_plan["rows"]
        assert baseline["summary"] == without_plan["summary"]
        assert without_plan["summary"]["first_plan_status"] == "missing"
        assert all(
            row["plan_source"] == "predicted"
            for row in without_plan["rows"]
        )

    def test_no_volatility_ignores_plan(self):
        config = make_config(baseline_views=1000.0)
        plan = make_plan(config)
        result = forecast_days(
            make_history(),
            config,
            START_TS,
            days=1,
            model_volatility=False,
            first_day_plan=plan,
        )

        assert result["rows"][0]["plan_source"] == "predicted"
        assert result["rows"][0]["target"] != plan["target"]
        assert (
            result["summary"]["first_plan_status"]
            == "ignored_no_volatility"
        )

    def test_list_mode_plan_uses_list_amount(self):
        config = make_config(baseline_views=1000.0)
        plan = make_plan(
            config,
            list_status="active",
            list_amount=500,
            boost_quota=900,
            gross_need=800,
        )

        fresh = forecast_days(
            make_history(), config, START_TS, days=1, first_day_plan=plan
        )
        assert fresh["rows"][0]["need"] == 500

        already = forecast_days(
            make_history(),
            config,
            START_TS,
            days=1,
            first_day_boosted=120,
            first_day_plan=plan,
        )
        assert already["rows"][0]["need"] == 0

    def test_cooldown_plan_forces_zero_need(self):
        config = make_config(baseline_views=1000.0)
        plan = make_plan(config, in_cooldown=True, list_status="disabled")
        result = forecast_days(
            make_history(), config, START_TS, days=1, first_day_plan=plan
        )

        first = result["rows"][0]
        assert first["need"] == 0
        assert first["in_cooldown"] is True