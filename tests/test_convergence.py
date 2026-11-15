"""闭环收敛回归测试。

缺口基准改为“昨日自然流量”（昨日实际 − 昨日刷量）后：
- exact（100%）模式能稳定到达目标；
- ratio 模式只差 5% 的缺口，且不再出现“补一天、停一天”的两天振荡。

测试直接驱动真实的 ``run_daily``，只替换 storage / fetcher / notifier /
booster，不发起任何网络请求。
"""

from datetime import datetime

from curator_views.core.scheduler import _TZ, _today_epoch, run_daily


class LoopStorage:
    """内存存储替身，额外记录每日刷量供缺口补偿读取。"""

    def __init__(self, program_start: str) -> None:
        self._program_start = program_start
        self.boost_totals: dict[str, int] = {}
        self.saved: dict[str, str] = {}
        self.daily_plans: dict[str, dict] = {}

    def upsert_daily_stats(self, date_ts: int, num_views: int) -> None:
        pass

    def save_daily_plan(self, plan: dict) -> None:
        self.daily_plans[plan["date"]] = dict(plan)

    def save_state(self, key: str, value: str) -> None:
        self.saved[key] = value

    def load_state(self, key: str, default: str = "") -> str:
        if key == "program_start":
            return self._program_start
        return self.saved.get(key, default)

    def get_boost_total(self, date: str) -> int:
        return int(self.boost_totals.get(date, 0))


class LoopFetcher:
    def __init__(self) -> None:
        self.stats: list[dict] = []

    def fetch_stats(self) -> list[dict]:
        return list(self.stats)


class LoopNotifier:
    def send(self, *args, **kwargs) -> None:
        pass


class LoopBooster:
    """按固定比例回填缺口，并把刷量记到对应日期的台账。"""

    def __init__(self, storage: LoopStorage, date: str, ratio: float) -> None:
        self.storage = storage
        self.date = date
        self.ratio = ratio
        self.batch_size = 50
        self.single_cap = 300
        self.target_ratio = ratio
        self.distribution = "ratio"

    def run(
        self, target_count: int, n_recommendations: int = 5,
        dry_run: bool = False,
    ) -> int:
        boosted = min(
            round(target_count * self.ratio),
            n_recommendations * self.single_cap,
        )
        self.storage.boost_totals[self.date] = (
            self.storage.boost_totals.get(self.date, 0) + boosted
        )
        return boosted


def _day_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%Y-%m-%d")


def _simulate(
    days: int, organic: int, baseline: int, ratio: float
) -> list[int]:
    """逐步推进每日闭环，返回每天的实际浏览量。"""
    start_ts = _today_epoch() - (days + 40) * 86400
    storage = LoopStorage(_day_str(start_ts))
    fetcher = LoopFetcher()
    for i in range(10):
        fetcher.stats.append(
            {"date_ts": start_ts + i * 86400, "num_views": organic}
        )

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
        "baseline_views": baseline,
        "volatility": 0.0,
        "boost_n": 5,
        "single_cap": 300,
        "batch_size": 50,
        "target_ratio": ratio,
        "boost_enabled": True,
        "dry_run": False,
    }

    actuals: list[int] = []
    for i in range(10, 10 + days):
        day_ts = start_ts + i * 86400
        booster = LoopBooster(storage, _day_str(day_ts), ratio)
        summary = run_daily(storage, fetcher, LoopNotifier(), booster, config)
        actual = organic + int(summary["boosted"])
        fetcher.stats.append({"date_ts": day_ts, "num_views": actual})
        actuals.append(actual)
    return actuals


class TestClosedLoopConvergence:
    def test_exact_mode_converges_to_baseline(self):
        # 有机 1000、目标 2000、每轮补满缺口 → 每天都应稳定在 2000
        actuals = _simulate(days=30, organic=1000, baseline=2000, ratio=1.0)

        assert actuals[-5:] == [2000] * 5, actuals[-10:]

    def test_ratio_mode_holds_near_baseline_without_oscillation(self):
        # 每轮只补缺口的 95% → 稳定在 1950，且不应两天一摆
        actuals = _simulate(days=30, organic=1000, baseline=2000, ratio=0.95)

        tail = actuals[-10:]
        assert min(tail) >= 1940, tail
        assert max(tail) - min(tail) <= 2, tail