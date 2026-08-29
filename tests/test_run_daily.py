"""run_daily 调度器核心行为测试：手动基线、波动率、恢复期与标记日剔除。

通过注入 Fake 依赖（storage / fetcher / notifier / booster）直接驱动
``run_daily``，验证新增配置项的实际行为，不发起任何网络请求。
"""

from datetime import datetime
from unittest.mock import patch

from curator_views.core.scheduler import (
    _TZ,
    _date_to_epoch,
    _today_epoch,
    run_daily,
)


# ── 依赖替身 ─────────────────────────────────────────────────────────────


class FakeStorage:
    def __init__(self, program_start: str) -> None:
        self._program_start = program_start
        self.written_stats = []
        self.saved = {}

    def upsert_daily_stats(self, date_ts: int, num_views: int) -> None:
        self.written_stats.append((date_ts, num_views))

    def save_state(self, key: str, value: str) -> None:
        self.saved[key] = value

    def load_state(self, key: str, default: str = "") -> str:
        if key == "program_start":
            return self._program_start
        return self.saved.get(key, default)


class FakeFetcher:
    def __init__(self, stats: list) -> None:
        self._stats = sorted(stats, key=lambda d: d["date_ts"])

    def fetch_stats(self):
        return list(self._stats)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent = []

    def send(self, title: str, body: str, level: str = "info") -> None:
        self.sent.append((title, body, level))


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


def run_with(
    stats: list,
    program_start: str,
    booster: FakeBooster,
    **config_overrides,
) -> dict:
    """以指定配置运行一次 run_daily，返回汇总字典。"""
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
    storage = FakeStorage(program_start)
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
        assert summary["need"] == 100  # 200 - 昨日实际 100
        assert booster.last_target == 100


class TestVolatility:
    def test_volatility_jitters_need(self):
        stats = make_stats([100] * 30)
        booster = FakeBooster()
        # uniform 固定返回 +0.1 → need = 100 × 1.1 = 110
        with patch(
            "curator_views.core.scheduler.random.uniform", return_value=0.1
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
            "curator_views.core.scheduler.random.uniform", return_value=-0.5
        ):
            summary = run_with(
                stats,
                program_start_str(),
                booster,
                baseline_views=200,
                volatility=0.30,
            )
        assert summary["need"] == 50  # round(100 × 0.5)


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
