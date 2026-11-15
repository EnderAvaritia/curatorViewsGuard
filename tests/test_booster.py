"""Booster 提升分配方式测试：ratio（按比例提前停）与 exact（按条精确配额）。

通过 Mock 掉 Fetcher 与 _visit_app，验证配额分配与提前停止行为，
不发起任何网络请求；wait_min/wait_max 置 0 加速测试。
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from curator_views.core.booster import MAX_WORKERS, Booster


class FakeStorage:
    def __init__(self) -> None:
        self.boost_logs = []

    def log_boost(self, today: str, count: int, note: str = "") -> None:
        self.boost_logs.append((today, count, note))


def make_recommendations(n: int) -> list:
    """生成 n 条推荐（app_0 为最新）。"""
    return [{"app_id": f"app_{i}"} for i in range(n)]


def make_booster(
    distribution: str = "ratio",
    target_ratio: float = 0.95,
    single_cap: int = 300,
    batch_size: int = 50,
    threads: int = 1,
) -> Booster:
    return Booster(
        clan_id="123",
        cookie_file="cookie.txt",
        proxy=None,
        storage=FakeStorage(),
        batch_size=batch_size,
        single_cap=single_cap,
        threads=threads,
        wait_min=0.0,
        wait_max=0.0,
        max_retries=3,
        target_ratio=target_ratio,
        distribution=distribution,
    )


class TestExactDistribution:
    """exact 模式：按条分配配额，恰好刷满目标（30×16+20 = 500）。"""

    def test_exact_500_over_30(self):
        booster = make_booster(distribution="exact")
        visits = []

        def visit(app_id: str) -> bool:
            visits.append(app_id)
            return True

        with patch("curator_views.core.booster.Fetcher") as mock_cls, patch.object(
            booster, "_visit_app", side_effect=visit
        ):
            mock_cls.return_value.fetch_recent_recommendations.return_value = (
                make_recommendations(30)
            )
            result = booster.run(500, n_recommendations=30)

        assert result == 500
        assert len(visits) == 500
        counts = Counter(visits)
        # 前 20 条（最新）各 17 次，后 10 条各 16 次
        for i in range(20):
            assert counts[f"app_{i}"] == 17
        for i in range(20, 30):
            assert counts[f"app_{i}"] == 16

    def test_exact_small_target_distributes_to_newest(self):
        # 目标小于条数：只刷最新几条，每条 1 次
        booster = make_booster(distribution="exact")
        visits = []

        def visit(app_id: str) -> bool:
            visits.append(app_id)
            return True

        with patch("curator_views.core.booster.Fetcher") as mock_cls, patch.object(
            booster, "_visit_app", side_effect=visit
        ):
            mock_cls.return_value.fetch_recent_recommendations.return_value = (
                make_recommendations(30)
            )
            result = booster.run(10, n_recommendations=30)

        assert result == 10
        assert visits == [f"app_{i}" for i in range(10)]

    def test_exact_quota_capped_by_single_cap(self):
        # 目标超出 条数×上限 时，配额以 single_cap 封顶
        booster = make_booster(distribution="exact", single_cap=10)
        visits = []

        def visit(app_id: str) -> bool:
            visits.append(app_id)
            return True

        with patch("curator_views.core.booster.Fetcher") as mock_cls, patch.object(
            booster, "_visit_app", side_effect=visit
        ):
            mock_cls.return_value.fetch_recent_recommendations.return_value = (
                make_recommendations(30)
            )
            result = booster.run(500, n_recommendations=30)

        assert result == 300  # 30 × single_cap(10) 封顶


class TestRatioDistribution:
    """ratio 模式：保持原有 95% 提前停止行为，回归保护。"""

    def test_ratio_stops_at_target_ratio(self):
        booster = make_booster(distribution="ratio", target_ratio=0.95)
        visits = []

        def visit(app_id: str) -> bool:
            visits.append(app_id)
            return True

        with patch("curator_views.core.booster.Fetcher") as mock_cls, patch.object(
            booster, "_visit_app", side_effect=visit
        ):
            mock_cls.return_value.fetch_recent_recommendations.return_value = (
                make_recommendations(30)
            )
            result = booster.run(500, n_recommendations=30)

        # 16 轮 × 30 = 480 ≥ 500×95%=475 即提前停，不追满 500
        assert result == 480
        assert len(visits) == 480

    def test_ratio_ignores_distribution_attr_default(self):
        # 未显式传 distribution 时默认 ratio（向后兼容）
        booster = make_booster()
        assert booster.distribution == "ratio"


class TestConcurrencyCap:
    """线程池大小恒被 MAX_WORKERS 截断：配置再大也不会突破实现上限。"""

    def _run_with_captured_workers(self, threads: int) -> int:
        booster = make_booster(single_cap=1, threads=threads)
        captured = {}

        def fake_executor(max_workers):
            captured["max_workers"] = max_workers
            return ThreadPoolExecutor(max_workers=max_workers)

        with patch("curator_views.core.booster.Fetcher") as mock_cls, patch.object(
            booster, "_visit_app", return_value=True
        ), patch(
            "curator_views.core.booster.ThreadPoolExecutor",
            side_effect=fake_executor,
        ):
            mock_cls.return_value.fetch_recent_recommendations.return_value = (
                make_recommendations(3)
            )
            booster.run(3, n_recommendations=3)

        return captured["max_workers"]

    def test_threads_above_cap_clamped_to_max_workers(self):
        assert self._run_with_captured_workers(64) == MAX_WORKERS

    def test_threads_below_cap_kept_as_configured(self):
        assert self._run_with_captured_workers(3) == 3
