"""target_min / target_max 作为刷量绝对硬边界的测试。

覆盖：上限（抖动 / 列表 / 定档）、下限托底、无提升状态不托底、
dry-run 与正式执行一致、forecast 逐日不越界、手动 boost 上限夹取。
"""

import json
import logging
import random
from unittest.mock import patch

import pytest

from curator_views import cli
from curator_views.core.forecast import forecast_days
from curator_views.core.scheduler import (
    _bound_boost,
    _normalize_target_bounds,
)
from tests.test_daily_plan import make_plan
from tests.test_forecast import (
    START_TS,
    make_config as forecast_config,
    make_history,
    warning_codes,
)
from tests.test_run_daily import (
    FakeBooster,
    FakeStorage,
    make_stats,
    program_start_str,
    run_with,
    yesterday_str,
)

SCHED_LOG = "curator_views.core.scheduler"


class RecordingBooster(FakeBooster):
    """记录 run 参数；cmd_boost 只依赖 run。"""


# ── 纯函数 ────────────────────────────────────────────────────────────────


class TestBoundBoostHelper:
    def test_ceiling_first_then_floor(self):
        # 自然 100、上限 1500 → 最多刷 1400；下限 5000 → 至少刷 4900。
        assert _bound_boost(5000, 100, 5000, 1500) == (1400, "max")
        # 配额低于下限时抬到下限。
        assert _bound_boost(100, 100, 5000, 0) == (4900, "min")
        # 上限内且高于下限时不动。
        assert _bound_boost(5000, 100, 5000, 6000) == (5000, "")

    def test_zero_need_never_raised(self):
        assert _bound_boost(0, 100, 5000, 5000) == (0, "")

    def test_zero_bounds_disable_clamp(self):
        assert _bound_boost(123, 100, 0, 0) == (123, "")

    def test_natural_above_ceiling_forces_zero(self):
        # 自然 1181 已超过上限 1000：可刷量 0，且不托底。
        assert _bound_boost(500, 1181, 0, 1000) == (0, "max")

    def test_misconfig_floor_above_ceiling_uses_ceiling(self, caplog):
        with caplog.at_level(logging.WARNING, logger=SCHED_LOG):
            need, hit = _bound_boost(5000, 100, 5000, 1500)
        assert (need, hit) == (1400, "max")
        assert any("配置矛盾" in item.message for item in caplog.records)


class TestNormalizeTargetBounds:
    def test_inverted_bounds_raise(self):
        with pytest.raises(ValueError, match="target_min"):
            _normalize_target_bounds(20000, 5000)

    def test_zero_means_unbounded(self):
        assert _normalize_target_bounds(0, 0) == (0.0, 0.0)

    def test_equal_bounds_are_valid_single_point_window(self):
        assert _normalize_target_bounds(5000, 5000) == (5000.0, 5000.0)

    def test_negatives_clamp_to_zero(self):
        assert _normalize_target_bounds(-100, -5) == (0.0, 0.0)


# ── run_daily ────────────────────────────────────────────────────────────


class TestRunDailyHardBounds:
    def test_ceiling_absolute_with_positive_jitter(self):
        # 目标上限 1500、自然 100：缺口 1400，+50% 抖动变 2100，
        # 但实际刷量必须夹回 1400（不是 1500）。
        booster = FakeBooster()
        stats = make_stats([100] * 30)
        with patch(f"{SCHED_LOG}._sample_signed_jitter", return_value=0.50):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=1000,
                boost_mode="growth",
                growth_factor=2.0,
                target_max=1500,
                volatility=0.50,
            )
        assert summary["today_expected"] == 1500
        assert summary["boost_quota"] == 1400
        assert summary["need"] == 1400
        assert summary["need_bound"] == "max"
        assert summary["target_ceiling_boost"] == 1400
        assert booster.last_target == 1400

    def test_ceiling_zero_when_natural_exceeds_max(self, caplog):
        # 自然 1181 > target_max 1000：刷量强制 0，提示总量仍会突破上限。
        booster = FakeBooster()
        stats = make_stats([1181] * 30)
        with caplog.at_level(logging.WARNING, logger=SCHED_LOG):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=2000,
                target_max=1000,
            )
        assert summary["yesterday_natural"] == 1181
        assert summary["need"] == 0
        assert summary["target_ceiling_boost"] == 0
        assert booster.calls == []
        assert any("已超过 target_max" in item.message for item in caplog.records)

    def test_floor_raises_negative_jitter_back_to_floor(self):
        # 自然 1181、下限 5000：至少刷 3819；-50% 抖动后仍需回到 3819。
        booster = FakeBooster()
        stats = make_stats([1181] * 30)
        with patch(f"{SCHED_LOG}._sample_signed_jitter", return_value=-0.50):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=5000,
                target_min=5000,
                volatility=0.50,
            )
        assert summary["today_expected"] == 5000
        assert summary["boost_quota"] == 3819
        assert summary["need"] == 3819
        assert summary["need_bound"] == "min"
        assert summary["target_floor_boost"] == 3819
        assert booster.last_target == 3819

    def test_boost_disabled_is_not_floor_topped_up(self):
        # boost_enabled=False：允许计算配额，但下限不得把它抬回 3819。
        booster = FakeBooster()
        stats = make_stats([1181] * 30)
        with patch(f"{SCHED_LOG}._sample_signed_jitter", return_value=-0.50):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=5000,
                target_min=5000,
                volatility=0.50,
                boost_enabled=False,
            )
        assert summary["boost_quota"] == 1910
        assert summary["need"] == 1910
        assert summary["need_bound"] == ""
        assert booster.calls == []

    def test_list_row_zero_is_not_topped_up(self, tmp_path):
        list_path = tmp_path / "boost_plan.txt"
        list_path.write_text("0\n", encoding="utf-8")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            baseline_views=100,
            target_min=5000,
            boost_list_file=str(list_path),
        )
        assert summary["boost_list_status"] == "active"
        assert summary["need"] == 0
        assert summary["need_bound"] == ""
        assert booster.calls == []

    def test_list_amount_above_ceiling_is_clamped_and_logged(self, tmp_path, caplog):
        list_path = tmp_path / "boost_plan.txt"
        list_path.write_text("5000\n", encoding="utf-8")
        booster = FakeBooster()
        with caplog.at_level(logging.WARNING, logger=SCHED_LOG):
            summary = run_with(
                make_stats([100] * 30),
                program_start_str(),
                booster,
                baseline_views=100,
                target_max=1500,
                boost_list_file=str(list_path),
            )
        assert summary["need"] == 1400
        assert summary["need_bound"] == "max"
        assert booster.last_target == 1400
        assert any("刷量列表首行 5000" in item.message for item in caplog.records)

    def test_list_amount_below_floor_is_raised(self, tmp_path):
        list_path = tmp_path / "boost_plan.txt"
        list_path.write_text("100\n", encoding="utf-8")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            baseline_views=100,
            target_min=5000,
            boost_list_file=str(list_path),
        )
        assert summary["need"] == 4900
        assert summary["need_bound"] == "min"
        assert booster.last_target == 4900

    def test_cooldown_is_not_floor_topped_up(self):
        # 高位连续段触发恢复期；昨日刷量还原出较低自然流量，缺口本应 > 0，
        # 但恢复期强制不刷，且下限不得把它托起来。
        stats = make_stats([100] * 25 + [10000] * 5)
        booster = FakeBooster()
        with patch(f"{SCHED_LOG}._sample_signed_jitter", return_value=-0.50):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                boost_totals={yesterday_str(): 9500},
                baseline_views=100,
                target_min=5000,
                volatility=0.50,
                consecutive_days=2,
            )
        assert summary["in_cooldown"] is True
        assert summary["need"] == 0
        assert summary["need_bound"] == ""
        assert booster.calls == []

    def test_dry_run_matches_real_run_and_plan_need_is_bounded(self):
        stats = make_stats([100] * 30)
        config = dict(
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=2.0,
            target_max=1500,
            volatility=0.30,
        )
        dry_storage = FakeStorage(program_start_str())
        with patch(f"{SCHED_LOG}._sample_signed_jitter", return_value=0.30):
            dry = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=dry_storage,
                dry_run=True,
                **config,
            )
        real_storage = FakeStorage(program_start_str())
        with patch(f"{SCHED_LOG}._sample_signed_jitter", return_value=0.30):
            real = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=real_storage,
                **config,
            )
        assert dry["need"] == real["need"] == 1400
        assert dry["boost_quota"] == real["boost_quota"] == 1400
        assert dry["need_bound"] == real["need_bound"] == "max"
        assert dry_storage.daily_plans == {}
        plan = real_storage.daily_plans[real["date"]]
        assert plan["need"] == 1400
        assert plan["boost_quota"] == 1400


# ── forecast ─────────────────────────────────────────────────────────────


class TestForecastInvertedBounds:
    def test_forecast_days_raises_for_inverted_bounds(self):
        config = forecast_config(
            baseline_views=1000.0, target_min=20000, target_max=5000
        )
        with pytest.raises(ValueError, match="target_min"):
            forecast_days(
                make_history(views=1000),
                config,
                START_TS,
                days=3,
                first_natural=1000,
            )


# ── CLI 命令拒绝倒置配置 ─────────────────────────────────────────────────


class TestCommandsRejectInvertedBounds:
    def _write(self, tmp_path, text):
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_run_fails_before_requiring_clan_id(self, tmp_path, capsys):
        # 缺少 clan_id 也不能掩盖倒置错误：校验必须先于 _build_runtime。
        config_path = self._write(
            tmp_path,
            'target_min: 20000\ntarget_max: 5000\ndb_path: "views.db"\n',
        )
        code = cli.main(["run", "--config", str(config_path), "--dry-run"])
        captured = capsys.readouterr()
        assert code == 1
        assert "target_min (20000) 大于 target_max (5000)" in captured.err
        # 不要出现「错误: 配置错误:」这种重复前缀
        assert "错误: 配置错误" not in captured.err
        assert "缺少必填项" not in captured.err
        assert "Traceback" not in captured.err
        assert not (tmp_path / "views.db").exists()

    def test_forecast_rejects_file_bounds_without_json(self, tmp_path, capsys):
        config_path = self._write(
            tmp_path,
            'target_min: 20000\ntarget_max: 5000\ndb_path: "views.db"\n',
        )
        code = cli.main(
            ["forecast", "--config", str(config_path), "--days", "1", "--json"]
        )
        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "target_min (20000) 大于 target_max (5000)" in captured.err

    def test_forecast_rejects_inverted_set_overrides(self, tmp_path, capsys):
        # 配置文件本身合法，--set 的 what-if 值同样必须校验。
        config_path = self._write(
            tmp_path,
            'clan_id: "45519015"\ntarget_min: 0\ntarget_max: 0\n'
            'db_path: "views.db"\n',
        )
        code = cli.main(
            [
                "forecast",
                "--config",
                str(config_path),
                "--set",
                "target_min=20000",
                "--set",
                "target_max=5000",
                "--json",
            ]
        )
        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "target_min (20000) 大于 target_max (5000)" in captured.err

    def test_daemon_returns_1_at_startup(self, tmp_path, capsys, monkeypatch):
        config_path = self._write(
            tmp_path,
            'target_min: 20000\ntarget_max: 5000\ndb_path: "views.db"\n',
        )

        def _fail(self):
            pytest.fail("倒置配置不应进入 daemon 循环")

        monkeypatch.setattr(cli.Daemon, "run_forever", _fail)
        code = cli.main(["daemon", "--config", str(config_path)])
        captured = capsys.readouterr()
        assert code == 1
        assert "target_min (20000) 大于 target_max (5000)" in captured.err
        assert "Traceback" not in captured.err


class TestForecastHardBounds:
    def test_every_row_stays_inside_window_over_seeds(self):
        config = forecast_config(
            baseline_views=2000.0,
            boost_mode="growth",
            growth_factor=2.0,
            target_min=2000,
            target_max=2500,
            volatility=0.40,
            target_volatility=0.30,
        )
        saw_hit = False
        for seed in range(25):
            random.seed(seed)
            result = forecast_days(
                make_history(views=1000),
                config,
                START_TS,
                days=10,
                first_natural=1000,
            )
            for row in result["rows"]:
                total = row["assumed_natural"] + row["need"]
                assert total <= 2500
                if row["need"] > 0:
                    assert total >= 2000
                if row["bound_hit"]:
                    saw_hit = True
                    assert row["bound_hit"] in ("min", "max")
            if saw_hit:
                assert "target_bound_hit" in warning_codes(result)
        assert saw_hit is True

    def test_warning_only_when_bound_changes_gap(self):
        # 窗口足够宽、无抖动：不夹取 → 不应出现 target_bound_hit。
        config = forecast_config(
            baseline_views=1000.0,
            target_min=500,
            target_max=2000,
        )
        result = forecast_days(
            make_history(views=1000),
            config,
            START_TS,
            days=3,
            first_natural=1000,
        )
        assert "target_bound_hit" not in warning_codes(result)
        assert all(row["bound_hit"] == "" for row in result["rows"])

    def test_summary_reports_bound_hit_counts_and_limits(self):
        config = forecast_config(
            baseline_views=2000.0,
            target_max=1500,
            volatility=0.50,
        )
        with patch(
            "curator_views.core.forecast._sample_signed_jitter",
            return_value=0.50,
        ):
            result = forecast_days(
                make_history(views=1000),
                config,
                START_TS,
                days=3,
                first_natural=1000,
            )
        summary = result["summary"]
        assert summary["bound_hits"] == {"min": 0, "max": 3}
        assert summary["target_ceiling_boost"] == 500
        assert summary["target_floor_boost"] == 0
        assert all(row["need"] == 500 for row in result["rows"])

    def test_persisted_plan_out_of_window_is_bounded(self):
        config = forecast_config(baseline_views=1000.0, target_max=1500)
        # 旧版 run 写入的越界定档：配额 5000，自然 1000 → 上限只允许 500。
        plan = make_plan(
            config,
            target=1500.0,
            gross_need=500,
            boost_quota=5000,
        )
        result = forecast_days(
            make_history(views=1000),
            config,
            START_TS,
            days=1,
            first_natural=1000,
            first_day_plan=plan,
        )
        row = result["rows"][0]
        assert row["plan_source"] == "persisted"
        assert row["need"] == 500
        assert row["bound_hit"] == "max"
        assert "target_bound_hit" in warning_codes(result)

    def test_persisted_plan_zero_stays_zero(self):
        config = forecast_config(baseline_views=1000.0, target_min=5000)
        plan = make_plan(
            config,
            target=5000.0,
            gross_need=0,
            boost_quota=0,
        )
        result = forecast_days(
            make_history(views=1000),
            config,
            START_TS,
            days=1,
            first_natural=1000,
            first_day_plan=plan,
        )
        row = result["rows"][0]
        assert row["need"] == 0
        assert row["bound_hit"] == ""
        assert "target_bound_hit" not in warning_codes(result)

    def test_persisted_no_boost_list_status_stays_zero(self):
        # 定档记录 list_status=missing（配额仍非 0）时，forecast 不得用配额
        # 重新算出缺口并托底。
        config = forecast_config(baseline_views=1000.0, target_min=5000)
        plan = make_plan(
            config,
            target=5000.0,
            gross_need=4000,
            boost_quota=4000,
            list_status="missing",
            list_amount=None,
        )
        result = forecast_days(
            make_history(views=1000),
            config,
            START_TS,
            days=1,
            first_natural=1000,
            first_day_plan=plan,
        )
        row = result["rows"][0]
        assert row["need"] == 0
        assert row["bound_hit"] == ""
        assert "target_bound_hit" not in warning_codes(result)


# ── cmd_boost ────────────────────────────────────────────────────────────


class TestBoostCommandClampsTarget:
    def test_target_above_max_is_clamped(self, tmp_path, capsys, monkeypatch):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            'clan_id: "45519015"\ntarget_max: 1500\ndb_path: "views.db"\n',
            encoding="utf-8",
        )
        booster = RecordingBooster()
        monkeypatch.setattr(cli, "_make_booster", lambda *a, **k: booster)

        code = cli.main(
            [
                "boost",
                "--config",
                str(config_path),
                "--target",
                "5000",
                "--dry-run",
            ]
        )
        captured = capsys.readouterr()
        assert code == 0
        summary = json.loads(captured.out)
        assert summary["target"] == 1500
        assert summary["requested_target"] == 5000
        assert summary["clamped"] is True
        assert booster.calls[-1][0] == 1500
        assert "超过 target_max" in captured.err