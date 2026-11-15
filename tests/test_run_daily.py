"""run_daily 调度器核心行为测试：手动基线、波动率、恢复期与标记日剔除。

通过注入 Fake 依赖（storage / fetcher / notifier / booster）直接驱动
``run_daily``，验证新增配置项的实际行为，不发起任何网络请求。
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from curator_views.core.scheduler import (
    _TZ,
    _date_to_epoch,
    _jitter_bounds,
    _sample_signed_jitter,
    _today_epoch,
    run_daily,
)


# ── 依赖替身 ─────────────────────────────────────────────────────────────


class FakeStorage:
    def __init__(self, program_start: str, boost_totals: dict | None = None) -> None:
        self._program_start = program_start
        self.written_stats = []
        self.saved = {}
        self.boost_totals = boost_totals or {}

    def upsert_daily_stats(self, date_ts: int, num_views: int) -> None:
        self.written_stats.append((date_ts, num_views))

    def save_state(self, key: str, value: str) -> None:
        self.saved[key] = value

    def load_state(self, key: str, default: str = "") -> str:
        if key == "program_start":
            return self._program_start
        return self.saved.get(key, default)

    def get_boost_total(self, date: str) -> int:
        return int(self.boost_totals.get(date, 0))


class FakeFetcher:
    def __init__(self, stats: list) -> None:
        self._stats = sorted(stats, key=lambda d: d["date_ts"])

    def fetch_stats(self):
        return list(self._stats)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent = []

    def send(
        self,
        title: str,
        body: str,
        level: str = "info",
        category: str = "alert",
    ) -> None:
        self.sent.append((title, body, level))

    def flush_pending(self) -> int:
        return 0


class FakeBooster:
    def __init__(self) -> None:
        self.batch_size = 50
        self.single_cap = 300
        self.target_ratio = 0.95
        self.last_target = None
        self.calls = []

    def run(self, target_count: int, n_recommendations: int = 5,
            dry_run: bool = False) -> int:
        self.last_target = target_count
        self.calls.append((target_count, n_recommendations, dry_run))
        return target_count


# ── 工具函数 ─────────────────────────────────────────────────────────────


def make_stats(daily_values: list, start_offset_days: int = 30) -> list:
    """生成从 start_offset_days 天前零点开始的逐日统计列表。"""
    base = _today_epoch() - start_offset_days * 86400
    return [
        {"date_ts": base + i * 86400, "num_views": value}
        for i, value in enumerate(daily_values)
    ]


def program_start_str(start_offset_days: int = 30) -> str:
    start_ts = _today_epoch() - start_offset_days * 86400
    return datetime.fromtimestamp(start_ts, tz=_TZ).strftime("%Y-%m-%d")


def today_str() -> str:
    """run_daily 眼中的“今天”（Asia/Shanghai）。"""
    return datetime.fromtimestamp(_today_epoch(), tz=_TZ).strftime("%Y-%m-%d")


def yesterday_str() -> str:
    """make_stats 最后一条统计的日期（即 run_daily 眼中的"昨日"）。"""
    return datetime.fromtimestamp(
        _today_epoch() - 86400, tz=_TZ
    ).strftime("%Y-%m-%d")


def run_with(
    stats: list,
    program_start: str,
    booster: FakeBooster,
    boost_totals: dict | None = None,
    saved_state: dict | None = None,
    storage: FakeStorage | None = None,
    **config_overrides,
) -> dict:
    """以指定配置运行一次 run_daily，返回汇总字典。

    ``storage`` 可传入复用同一个 FakeStorage，用于验证同日抖动复用等
    跨多次 run 的行为。
    """
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
        "baseline_views": 0,
        "volatility": 0.0,
        "boost_n": 5,
        "single_cap": 300,
        "batch_size": 50,
        "target_ratio": 0.95,
        "boost_enabled": True,
        "dry_run": False,
    }
    config.update(config_overrides)
    if storage is None:
        storage = FakeStorage(program_start, boost_totals)
        if saved_state:
            storage.saved.update(saved_state)
    fetcher = FakeFetcher(stats)
    notifier = FakeNotifier()
    summary = run_daily(storage, fetcher, notifier, booster, config)
    return summary


# ── 测试用例 ─────────────────────────────────────────────────────────────


class TestManualBaseline:
    def test_manual_baseline_overrides_prediction(self):
        # 30 天都是 100 的平坦历史 + 手动基线 200 → 期望直接用 200
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            baseline_views=200,
        )
        assert summary["baseline_mode"] == "manual"
        assert summary["today_expected"] == 200
        assert summary["need"] == 100  # 200 - 昨日自然流量 100
        assert booster.last_target == 100


class TestBoostCompensation:
    """昨日刷量补偿：缺口基准是“昨日自然流量”，不是“昨日实际”。"""

    def test_need_subtracts_yesterday_boost(self):
        # 昨日实际 100 里有 40 是我们刷的 → 自然流量 60，缺口 200-60=140
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            boost_totals={yesterday_str(): 40},
            baseline_views=200,
        )
        assert summary["yesterday_actual"] == 100
        assert summary["yesterday_boosted"] == 40
        assert summary["yesterday_natural"] == 60
        assert summary["need"] == 140
        assert booster.last_target == 140

    def test_missing_boost_log_falls_back_to_full_actual(self):
        # 台账没有记录（旧数据 / 手动修正缺失）→ 按 0 处理，保持旧行为
        stats = make_stats([100] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            baseline_views=200,
        )
        assert summary["yesterday_boosted"] == 0
        assert summary["yesterday_natural"] == 100
        assert summary["need"] == 100

    def test_boost_larger_than_actual_clamps_natural_to_zero(self):
        # 台账 150 > 实际 100：自然流量夹到 0，缺口 = 200，不能补成 250
        stats = make_stats([100] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            boost_totals={yesterday_str(): 150},
            baseline_views=200,
        )
        assert summary["yesterday_natural"] == 0
        assert summary["need"] == 200

    def test_growth_mode_also_uses_natural_flow(self):
        # growth 目标 300；昨日自然 60 → 缺口 240（而不是 300-100=200）
        stats = make_stats([100] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            boost_totals={yesterday_str(): 40},
            baseline_views=200,
            boost_mode="growth",
            growth_factor=1.5,
        )
        assert summary["today_expected"] == 300
        assert summary["need"] == 240


class TestTodayBoosted:
    """当日已刷：同一天重跑按剩余缺口续刷，不重复刷整份。"""

    def test_need_subtracts_today_boost(self):
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            boost_totals={today_str(): 30},
            baseline_views=200,
        )
        assert summary["today_boosted"] == 30
        assert summary["gross_need"] == 100
        assert summary["need"] == 70
        assert summary["boosted"] == 70

    def test_already_boosted_enough_skips_run(self):
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            boost_totals={today_str(): 100},
            baseline_views=200,
        )
        assert summary["today_boosted"] == 100
        assert summary["need"] == 0
        assert booster.calls == []

    def test_today_boost_is_clamped_at_zero(self):
        # 台账多于缺口（多进程/手动补过）时不产生负数缺口
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            boost_totals={today_str(): 500},
            baseline_views=200,
        )
        assert summary["need"] == 0


class TestVolatility:
    def test_volatility_jitters_need(self):
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        # 固定返回 +0.1 → 当日配额 = 100 × 1.1 = 110
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.1,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=200,
                volatility=0.10,
            )
        assert summary["volatility"] == 0.10
        assert summary["need"] == 110
        assert booster.last_target == 110

    def test_zero_volatility_no_jitter(self):
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            baseline_views=200,
            volatility=0.0,
        )
        assert summary["need"] == 100

    def test_volatility_never_pushes_need_below_zero(self):
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=-0.5,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=200,
                volatility=0.30,
            )
        assert summary["need"] == 50  # round(100 × 0.5)


class TestJitterSampling:
    def test_bounds_normalization_and_min_floor(self):
        assert _jitter_bounds(0.5, 0.2) == (0.2, 0.5)
        # 下限大于上限时夹到上限（固定幅度），而不是关闭抖动
        assert _jitter_bounds(0.1, 0.5) == (0.1, 0.1)
        # 上限为 0 表示关闭
        assert _jitter_bounds(0.0, 0.5) == (0.0, 0.0)

    def test_sample_respects_min_magnitude(self):
        # 幅度固定取区间下限 0.2，方向由 random.random 决定（>=0.5 → 负）
        with patch(
            "curator_views.core.scheduler.random.uniform", return_value=0.2
        ) as uniform_mock, patch(
            "curator_views.core.scheduler.random.random", return_value=0.9
        ):
            jitter = _sample_signed_jitter(0.5, 0.2)
        assert jitter == -0.2
        uniform_mock.assert_called_once_with(0.2, 0.5)


class TestTargetVolatility:
    def test_target_volatility_shifts_target(self):
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.50,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=10000,
                target_volatility=0.50,
            )
        assert summary["target_jitter"] == 0.50
        assert summary["today_expected_raw"] == 10000
        assert summary["today_expected"] == 15000
        assert summary["need"] == 14900
        assert booster.last_target == 14900

    def test_target_bounds_apply_after_jitter(self):
        stats = make_stats([100] * 30)
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.50,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                baseline_views=10000,
                target_volatility=0.50,
                target_max=12000,
            )
        assert summary["today_expected"] == 12000
        assert summary["target_bound"] == "max"
        assert summary["need"] == 11900

    def test_huge_negative_jitter_keeps_target_non_negative(self):
        stats = make_stats([100] * 30)
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=-3.0,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                baseline_views=10000,
                target_volatility=3.0,
            )
        assert summary["today_expected"] == 0
        assert summary["need"] == 0

    def test_same_day_reuses_target_jitter(self):
        stats = make_stats([100] * 30)
        storage = FakeStorage(program_start_str())
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.50,
        ):
            first = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=storage,
                baseline_views=10000,
                target_volatility=0.50,
            )
        # 同日第二次 run 必须复用已持久化的抖动值，不能再抽随机数
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            side_effect=AssertionError("同日不应重新抽取抖动"),
        ):
            second = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=storage,
                baseline_views=10000,
                target_volatility=0.50,
            )
        assert second["target_jitter"] == first["target_jitter"] == 0.50
        assert second["today_expected"] == 15000

    def test_config_change_redraws_same_day_jitter(self):
        stats = make_stats([100] * 30)
        storage = FakeStorage(program_start_str())
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.50,
        ):
            run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=storage,
                baseline_views=10000,
                target_volatility=0.50,
            )
        # 基准改了 → 配置签名变化 → 允许当天重新抽取
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=-0.50,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=storage,
                baseline_views=12000,
                target_volatility=0.50,
            )
        assert summary["target_jitter"] == -0.50
        assert summary["today_expected"] == 6000

    def test_dry_run_does_not_persist_jitter(self):
        stats = make_stats([100] * 30)
        storage = FakeStorage(program_start_str())
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.50,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=storage,
                baseline_views=10000,
                target_volatility=0.50,
                dry_run=True,
            )
        assert summary["today_expected"] == 15000
        assert "daily_jitter" not in storage.saved

    def test_gap_quota_survives_later_runs(self):
        stats = make_stats([100] * 30)
        storage = FakeStorage(program_start_str())
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=-0.50,
        ):
            first = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=storage,
                baseline_views=200,
                volatility=0.50,
            )
        assert first["boost_quota"] == 50
        assert first["need"] == 50

        # 模拟首轮已刷 50：后续 run 只补「配额 − 已刷」，不再把负向抖动补平
        storage.boost_totals[today_str()] = 50
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            side_effect=AssertionError("同日不应重新抽取抖动"),
        ):
            second = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                storage=storage,
                baseline_views=200,
                volatility=0.50,
            )
        assert second["boost_quota"] == 50
        assert second["need"] == 0

    def test_jittered_high_days_not_flagged_as_overflow(self):
        # 自己 +50% 抖动产生的 16000（基线 10000）不应被判成恶意刷量
        stats = make_stats([10000] * 28 + [16000, 16000])
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.0,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                FakeBooster(),
                baseline_views=10000,
                target_volatility=0.50,
                consecutive_days=2,
            )
        assert summary["overflow_flag_count"] == 0


class TestCooldown:
    def _make_attack_history(self):
        """25 天 100 + 第 26、27 天被恶意刷到 250（连续 2 天标记），
        第 28~30 天已回落到 90。被刷日的溢出分摊已滑出今天，
        缺口为正，但仍在恢复期内（最近标记日距今 4 天 < 7 天）。"""
        return make_stats([100] * 25 + [250, 250, 90, 90, 90])

    def test_cooldown_suppresses_need(self):
        stats = self._make_attack_history()
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            cooldown_days=7,
        )
        # 恶意刷量被标记；昨天已回落（缺口为正），恢复期内强制不刷
        assert summary["overflow_flag_count"] == 2
        assert summary["in_cooldown"] is True
        assert summary["need"] == 0
        assert booster.last_target is None  # 未调用提升

    def test_no_cooldown_when_cooldown_days_zero(self):
        stats = self._make_attack_history()
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            cooldown_days=0,
        )
        assert summary["in_cooldown"] is False
        assert summary["need"] > 0
        assert booster.last_target is not None


class TestGrowthMode:
    def test_growth_mode_multiplies_manual_baseline(self):
        # manual 基线 200 + growth 1.5 → 目标 300；昨日实际 100 → 缺口 200
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            baseline_views=200,
            boost_mode="growth",
            growth_factor=1.5,
        )
        assert summary["boost_mode"] == "growth"
        assert summary["today_expected"] == 300  # 200 × 1.5
        assert summary["need"] == 200
        assert booster.last_target == 200

    def test_stable_mode_ignores_growth_factor(self):
        # stable 模式 + growth_factor 配置 → 倍率不生效，目标 = 基线
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            baseline_views=200,
            boost_mode="stable",
            growth_factor=1.5,
        )
        assert summary["boost_mode"] == "stable"
        assert summary["today_expected"] == 200
        assert summary["need"] == 100

    def test_growth_mode_with_auto_prediction(self):
        # auto 预测（30 天 100 → expected=100）× growth 1.5 → 目标 150
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            boost_mode="growth",
            growth_factor=1.5,
        )
        assert summary["today_expected"] == 150
        assert summary["need"] == 50

    def test_growth_does_not_flag_own_boost(self):
        # growth 模式下检测参照同步抬高：曲线被刷到 300（目标=300）不会
        # 被自己的反作弊标记为恶意刷量
        stats = make_stats([300] * 30)  # 曲线已稳定在被刷高的水平
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            baseline_views=200,
            boost_mode="growth",
            growth_factor=1.5,
            consecutive_days=2,
        )
        assert summary["today_expected"] == 300
        assert summary["overflow_flag_count"] == 0
        assert summary["in_cooldown"] is False


class TestGrowthRate:
    """growth_rate：线性升降（负值即匀速下滑），以及与 growth_factor 的乘算。"""

    @staticmethod
    def _seed_rate(rate: float, days_ago: int) -> dict:
        """模拟"该速率已生效 days_ago 天"的持久化锚点。"""
        start_ts = _today_epoch() - days_ago * 86400
        start_iso = datetime.fromtimestamp(start_ts, tz=_TZ).strftime("%Y-%m-%d")
        return {"growth_rate": f"{rate:.6f}", "growth_start": start_iso}

    def test_linear_decline_uses_saved_anchor(self):
        # manual 1000 × 1.0 × (1 - 0.01×10 天) = 900；昨日自然流量 500 → 缺口 400
        stats = make_stats([500] * 30)
        booster = FakeBooster()
        summary = run_with(
            stats,
            program_start_str(),
            booster,
            saved_state=self._seed_rate(-0.01, 10),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=1.0,
            growth_rate=-0.01,
        )
        assert summary["today_expected"] == 900
        assert summary["target_multiplier"] == 0.9
        assert summary["need"] == 400
        assert booster.last_target == 400

    def test_factor_and_rate_compose(self):
        # 0.9（台阶）× 0.9（5 天 × -2%）= 0.81 → 1000 → 810
        stats = make_stats([500] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            saved_state=self._seed_rate(-0.02, 5),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=0.9,
            growth_rate=-0.02,
        )
        assert summary["today_expected"] == pytest.approx(810)
        assert summary["target_multiplier"] == pytest.approx(0.81)

    def test_rate_change_resets_anchor_to_today(self):
        # 持久化速率是 -0.02，配置改成 -0.01 → 锚点重置为今天，不累计旧天数
        stats = make_stats([500] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            saved_state=self._seed_rate(-0.02, 10),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=1.0,
            growth_rate=-0.01,
        )
        assert summary["today_expected"] == 1000
        assert summary["target_multiplier"] == 1.0

    def test_stable_mode_ignores_growth_rate(self):
        stats = make_stats([500] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            saved_state=self._seed_rate(-0.01, 10),
            baseline_views=1000,
            boost_mode="stable",
            growth_factor=1.5,
            growth_rate=-0.01,
        )
        assert summary["today_expected"] == 1000
        assert summary["target_multiplier"] == 1.0

    def test_decline_target_not_flagged_as_overflow(self):
        # 目标被压到 500，但真实曲线稳定在 1000：检测参照用自然预期 1000，
        # 不能把"实际高于下滑目标"误判为恶意刷量。
        stats = make_stats([1000] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=0.5,
            growth_rate=0.0,
        )
        assert summary["today_expected"] == 500
        assert summary["overflow_flag_count"] == 0
        assert summary["in_cooldown"] is False
        assert summary["need"] == 0


class TestFlaggedExclusion:
    def _make_attack_history(self):
        """25 天 100 + 连续 3 天被刷到 250（连续候选 → 全部标记）。"""
        return make_stats([100] * 25 + [250, 250, 250])

    def test_excluded_expected_not_polluted(self):
        stats = self._make_attack_history()
        # exclude_flagged=True：标记日剔除 → 预期回到干净的 100
        clean_booster = FakeBooster()
        clean = run_with(
            stats, program_start_str(), clean_booster,
            exclude_flagged=True,
        )
        # exclude_flagged=False：标记日仍参与预测 → 预期被虚高值抬高
        dirty_booster = FakeBooster()
        dirty = run_with(
            stats, program_start_str(), dirty_booster,
            exclude_flagged=False,
        )
        assert clean["overflow_flag_count"] == 3  # 检测事实保留
        assert dirty["overflow_flag_count"] == 3
        assert clean["today_expected"] == 100  # 未被污染
        assert dirty["today_expected"] > clean["today_expected"]


class TestBoostDistribution:
    def test_boost_distribution_passed_to_booster(self):
        # run_daily 应把配置中的 boost_distribution 应用到 Booster 实例
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        run_with(
            stats, program_start_str(), booster,
            boost_distribution="exact",
        )
        assert booster.distribution == "exact"

    def test_boost_distribution_default_ratio(self):
        # 未配置时默认 ratio（向后兼容）
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        run_with(stats, program_start_str(), booster)
        assert booster.distribution == "ratio"


class TestLastRunDateRecord:
    def test_records_last_run_date_for_daemon_dedupe(self):
        # run_daily 结束写 last_run_date：手动 run 后启动 daemon 不会再跑一遍
        stats = make_stats([100] * 30)
        storage = FakeStorage(program_start_str())
        summary = run_daily(
            storage,
            FakeFetcher(stats),
            FakeNotifier(),
            FakeBooster(),
            {"boost_enabled": False},
        )
        assert storage.saved["last_run_date"] == summary["date"]


class TestDryRunNoSideEffects:
    """dry_run 只预览：不写统计、不写状态、不发简报，但照常算缺口。"""

    def test_dry_run_skips_all_writes_and_notification(self):
        stats = make_stats([100] * 30)
        storage = FakeStorage(program_start_str())
        notifier = FakeNotifier()
        booster = FakeBooster()
        summary = run_daily(
            storage,
            FakeFetcher(stats),
            notifier,
            booster,
            {
                "boost_enabled": True,
                "dry_run": True,
                "baseline_views": 200,
            },
        )
        assert summary["dry_run"] is True
        assert summary["need"] == 100
        assert storage.written_stats == []
        assert storage.saved == {}
        assert notifier.sent == []
        assert booster.last_target == 100
        assert booster.calls[-1][2] is True

    def test_dry_run_does_not_create_program_start(self):
        storage = FakeStorage("")
        run_daily(
            storage,
            FakeFetcher([{"date_ts": _today_epoch(), "num_views": 100}]),
            FakeNotifier(),
            FakeBooster(),
            {"dry_run": True, "baseline_views": 200},
        )
        assert "program_start" not in storage.saved
        assert "last_run_date" not in storage.saved

    def test_dry_run_does_not_persist_growth_anchor(self):
        storage = FakeStorage(program_start_str())
        run_daily(
            storage,
            FakeFetcher(make_stats([100] * 30)),
            FakeNotifier(),
            FakeBooster(),
            {
                "dry_run": True,
                "baseline_views": 200,
                "boost_mode": "growth",
                "growth_factor": 1.0,
                "growth_rate": -0.01,
            },
        )
        assert "growth_rate" not in storage.saved
        assert "growth_start" not in storage.saved


class ZeroBooster(FakeBooster):
    """模拟提升完全失败（返回 0，模拟网络/Cookie 问题）。"""

    def run(self, target_count: int, n_recommendations: int = 5,
            dry_run: bool = False) -> int:
        self.last_target = target_count
        self.calls.append((target_count, n_recommendations, dry_run))
        return 0


class TestBoostList:
    """每日刷量列表：首行覆盖缺口，成功后消费一行，异常情况保留。"""

    @staticmethod
    def _write(tmp_path, text: str) -> str:
        path = tmp_path / "boost_list.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_list_amount_overrides_need_and_is_consumed(self, tmp_path):
        # 自动缺口 100（200 - 100），列表首行 500 → 今日目标 500；成功后删掉首行
        list_path = self._write(tmp_path, "500\n200\n")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            baseline_views=200,
            boost_list_file=list_path,
        )
        assert summary["boost_list_status"] == "active"
        assert summary["need"] == 500
        assert booster.last_target == 500
        assert summary["boost_list_consumed"] == 500
        assert summary["boost_list_remaining"] == 1
        assert Path(list_path).read_text(encoding="utf-8") == "200\n"

    def test_comments_and_blank_lines_are_preserved(self, tmp_path):
        list_path = self._write(tmp_path, "# 计划\n\n500\n# 尾注\n200\n")
        run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            baseline_views=200,
            boost_list_file=list_path,
        )
        assert (
            Path(list_path).read_text(encoding="utf-8")
            == "# 计划\n\n# 尾注\n200\n"
        )

    def test_dry_run_does_not_consume(self, tmp_path):
        list_path = self._write(tmp_path, "500\n200\n")
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            baseline_views=200,
            boost_list_file=list_path,
            dry_run=True,
        )
        assert summary["need"] == 500
        assert "boost_list_consumed" not in summary
        assert Path(list_path).read_text(encoding="utf-8") == "500\n200\n"

    def test_boost_disabled_does_not_consume(self, tmp_path):
        list_path = self._write(tmp_path, "500\n")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            baseline_views=200,
            boost_list_file=list_path,
            boost_enabled=False,
        )
        assert summary["need"] == 500
        assert booster.last_target is None
        assert "boost_list_consumed" not in summary
        assert Path(list_path).read_text(encoding="utf-8") == "500\n"

    def test_failed_boost_keeps_line(self, tmp_path):
        list_path = self._write(tmp_path, "500\n200\n")
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            ZeroBooster(),
            baseline_views=200,
            boost_list_file=list_path,
        )
        assert summary["boosted"] == 0
        assert "boost_list_consumed" not in summary
        assert Path(list_path).read_text(encoding="utf-8") == "500\n200\n"

    def test_empty_list_skips_boost(self, tmp_path):
        list_path = self._write(tmp_path, "# 用完了\n\n")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            baseline_views=200,
            boost_list_file=list_path,
        )
        assert summary["boost_list_status"] == "empty"
        assert summary["need"] == 0
        assert booster.last_target is None

    def test_missing_file_skips_boost(self, tmp_path):
        list_path = str(tmp_path / "missing.txt")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            baseline_views=200,
            boost_list_file=list_path,
        )
        assert summary["boost_list_status"] == "missing"
        assert summary["need"] == 0
        assert booster.last_target is None

    def test_already_boosted_today_keeps_line(self, tmp_path):
        # 今日已刷过：列表模式不续刷，也不消费下一行
        list_path = self._write(tmp_path, "500\n200\n")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            booster,
            boost_totals={today_str(): 30},
            baseline_views=200,
            boost_list_file=list_path,
        )
        assert summary["boost_list_status"] == "already_boosted"
        assert summary["need"] == 0
        assert booster.calls == []
        assert Path(list_path).read_text(encoding="utf-8") == "500\n200\n"

    def test_cooldown_keeps_line(self, tmp_path):
        # 25 天 100 + 连续 2 天被刷到 250 → 进入恢复期；列表行保留到恢复期结束
        list_path = self._write(tmp_path, "500\n200\n")
        booster = FakeBooster()
        summary = run_with(
            make_stats([100] * 25 + [250, 250, 90, 90, 90]),
            program_start_str(),
            booster,
            cooldown_days=7,
            boost_list_file=list_path,
        )
        assert summary["in_cooldown"] is True
        assert summary["boost_list_status"] == "cooldown"
        assert summary["need"] == 0
        assert booster.last_target is None
        assert Path(list_path).read_text(encoding="utf-8") == "500\n200\n"


class TestThroughputCapacity:
    def test_warns_when_need_exceeds_thread_wait_capacity(self):
        stats = make_stats([100] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            baseline_views=6000,
            boost_n=10,
            single_cap=1000,
            threads=1,
            wait_min=8.0,
            wait_max=16.0,
        )
        assert summary["need"] == 5900
        assert summary["throughput_capacity"] == 5400
        assert "5,900" in summary["capacity_warning"]
        assert "5,400" in summary["capacity_warning"]

    def test_warns_when_need_exceeds_single_run_capacity(self):
        # 吞吐够但 boost_n × single_cap=1,500 < 缺口 5,900：提示本轮跑不满
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            baseline_views=6000,
            boost_n=5,
            single_cap=300,
            threads=2,
            wait_min=1.0,
            wait_max=2.0,
        )
        assert summary["need"] == 5900
        assert "单轮容量" in summary["capacity_warning"]
        assert "续刷" in summary["capacity_warning"]

    def test_no_warning_when_two_threads_cover_need(self):
        stats = make_stats([100] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            baseline_views=6000,
            boost_n=10,
            single_cap=1000,
            threads=2,
            wait_min=8.0,
            wait_max=16.0,
        )
        assert summary["need"] == 5900
        assert summary["throughput_capacity"] == 10800
        assert summary["capacity_warning"] == ""


class TestTargetBounds:
    """今日目标上下界：上限封顶防增长不收敛，下限托底防跌破，0 表示不设界。"""

    def test_upper_bound_caps_growth_target(self):
        # 基线 1000 × 2.0 = 2000 → target_max 1500 → 缺口 1500 − 100
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=2.0,
            target_max=1500,
        )
        assert summary["today_expected_raw"] == 2000
        assert summary["today_expected"] == 1500
        assert summary["target_bound"] == "max"
        assert summary["need"] == 1400

    def test_lower_bound_floors_declining_target(self):
        # 基线 1000 × 0.5 = 500 → target_min 800 → 缺口 800 − 100
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=0.5,
            target_min=800,
        )
        assert summary["today_expected_raw"] == 500
        assert summary["today_expected"] == 800
        assert summary["target_bound"] == "min"
        assert summary["need"] == 700

    def test_zero_bounds_disable_clamp(self):
        # 两个都是 0 → 不设界，目标保持原始值
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=2.0,
            target_min=0,
            target_max=0,
        )
        assert summary["today_expected_raw"] == 2000
        assert summary["today_expected"] == 2000
        assert summary["target_bound"] == ""
        assert summary["need"] == 1900

    def test_swapped_bounds_are_normalized(self):
        # min 900 > max 600（笔误）→ 交换为 [600, 900]，目标 1000 压到 900
        summary = run_with(
            make_stats([100] * 30),
            program_start_str(),
            FakeBooster(),
            baseline_views=1000,
            target_min=900,
            target_max=600,
        )
        assert summary["today_expected"] == 900
        assert summary["target_bound"] == "max"

    def test_lower_bound_raises_long_baseline(self):
        # 历史低位 100、最近 5 天已被托底到 1000；长期基线若仍取 100，
        # 连续托底会被 long_baseline 检测误判为恶意刷量。下限应同步抬高
        # 长期基线，低于下限×1.3 的托底不算异常。
        stats = make_stats([100] * 25 + [1000] * 5)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            baseline_views=100,
            target_min=1000,
            consecutive_days=2,
        )
        assert summary["overflow_flag_count"] == 0
        assert summary["in_cooldown"] is False

    def test_volatility_cannot_exceed_upper_bound(self):
        # 缺口 1400 经 +30% 抖动变成 1820，但实际执行仍不得超过上限 1500
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        with patch(
            "curator_views.core.scheduler._sample_signed_jitter",
            return_value=0.30,
        ):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=1000,
                boost_mode="growth",
                growth_factor=2.0,
                target_max=1500,
                volatility=0.30,
            )
        assert summary["today_expected"] == 1500
        assert summary["need"] == 1500
        assert booster.last_target == 1500

    def test_upper_bound_caps_growth_after_long_run(self):
        # 速率已生效 30 天：原始目标 1000×1.5×(1+0.05×30)=3750，
        # 上限 1500 必须把它压住，避免持续运行后刷出恐怖数值
        stats = make_stats([1000] * 30)
        summary = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            saved_state=TestGrowthRate._seed_rate(0.05, 30),
            baseline_views=1000,
            boost_mode="growth",
            growth_factor=1.5,
            growth_rate=0.05,
            target_max=1500,
        )
        assert summary["today_expected_raw"] == 3750
        assert summary["today_expected"] == 1500
        assert summary["target_bound"] == "max"
        assert summary["need"] == 500

    def test_lower_bound_does_not_flag_own_boost(self):
        # 曲线被托底在 600：检测参照同步抬到下限，连续多天不会被误判为
        # 恶意刷量（控制组不设下限时会被连续标记并进入恢复期）。
        stats = make_stats([600] * 30)
        with_floor = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            baseline_views=300,
            target_min=600,
            consecutive_days=2,
        )
        without_floor = run_with(
            stats,
            program_start_str(),
            FakeBooster(),
            baseline_views=300,
            consecutive_days=2,
        )
        assert with_floor["target_bound"] == "min"
        assert with_floor["overflow_flag_count"] == 0
        assert with_floor["in_cooldown"] is False
        assert without_floor["overflow_flag_count"] > 0
