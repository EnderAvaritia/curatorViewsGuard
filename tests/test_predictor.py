"""curator_views.core.predictor.predict_expected 单元测试。

断言值均基于实现约定手工核算：
- 中位数窗口：最近 min(len(history), window_days) 天；
- 趋势：对最近 min(len(history), window_days * 2) 个点按 0 起始索引
  做最小二乘线性回归，斜率封顶为 ±median * trend_cap；
- expected = median + trend，启动阶段（len < window_days）再乘
  startup_factor，最终不小于 0 并保留 1 位小数。
"""

import pytest

from curator_views.core import predictor

DAY = 86400
BASE_TS = 1_700_000_000


def hist(values):
    """把 num_views 序列转换为按 date_ts 升序的 history 列表。"""
    return [
        {"date_ts": BASE_TS + i * DAY, "num_views": int(v)}
        for i, v in enumerate(values)
    ]


def test_empty_history_returns_all_zero():
    result = predictor.predict_expected([])
    assert set(result) == {
        "expected", "median", "trend", "used_points", "startup_applied",
    }
    assert result["expected"] == 0.0
    assert result["median"] == 0.0
    assert result["trend"] == 0.0
    assert result["used_points"] == 0
    assert result["startup_applied"] is True


def test_flat_history_full_window():
    # 20 天全为 100，window=14：中位数 100，趋势 0，无启动期
    result = predictor.predict_expected(hist([100] * 20))
    assert result["median"] == 100.0
    assert result["trend"] == 0.0
    assert result["expected"] == 100.0
    assert result["used_points"] == 20
    assert result["startup_applied"] is False


def test_startup_phase_applies_factor():
    # 5 天 < 14 天窗口：expected = 100 * 0.9 = 90
    result = predictor.predict_expected(hist([100] * 5))
    assert result["expected"] == 90.0
    assert result["median"] == 100.0
    assert result["trend"] == 0.0
    assert result["used_points"] == 5
    assert result["startup_applied"] is True


def test_custom_startup_factor():
    result = predictor.predict_expected(
        hist([50] * 3), window_days=10, startup_factor=0.7,
    )
    assert result["expected"] == 35.0
    assert result["startup_applied"] is True


def test_trend_requires_min_points():
    # 3 点 < trend_min_points=5：趋势保持 0，即使数据在增长
    result = predictor.predict_expected(
        hist([10, 100, 500]), window_days=14, trend_min_points=5,
    )
    assert result["trend"] == 0.0
    assert result["median"] == 100.0
    assert result["expected"] == 90.0
    assert result["used_points"] == 3
    assert result["startup_applied"] is True


def test_median_uses_only_window_days():
    # window=5：中位数只看最后 5 天 [10,20,30,40,50] -> 30
    values = [0] * 15 + [10, 20, 30, 40, 50]
    result = predictor.predict_expected(hist(values), window_days=5)
    assert result["median"] == 30.0
    # 最后 10 点斜率 5.76 > cap 30*0.15=4.5，封顶为 4.5
    assert result["trend"] == 4.5
    assert result["expected"] == 34.5
    assert result["used_points"] == 20
    assert result["startup_applied"] is False


def test_trend_clamped_positive():
    # 末尾暴涨：中位数 100000，cap=15000，斜率被压到 +15000
    values = [100000] * 12 + [1300000, 1300000]
    result = predictor.predict_expected(hist(values), window_days=14)
    assert result["median"] == 100000.0
    assert result["trend"] == 15000.0
    assert result["expected"] == 115000.0
    assert result["startup_applied"] is False


def test_trend_clamped_negative():
    # 开头暴涨后骤降：趋势被封顶到 -15000，expected 仍为正
    values = [1300000, 1300000] + [100000] * 12
    result = predictor.predict_expected(hist(values), window_days=14)
    assert result["median"] == 100000.0
    assert result["trend"] == -15000.0
    assert result["expected"] == 85000.0
    assert result["startup_applied"] is False


def test_trend_clamped_to_zero_when_median_zero():
    # 中位数为 0 时 cap=0，无论斜率多大趋势都被压为 0
    values = [1000000, 1000000] + [0] * 12
    result = predictor.predict_expected(hist(values), window_days=14)
    assert result["median"] == 0.0
    assert result["trend"] == 0.0
    assert result["expected"] == 0.0
    assert result["startup_applied"] is False


def test_trend_uses_extended_window():
    # window=5，趋势窗口 = 最后 min(20, 10)=10 点 [0*5, 3000*5]，
    # 斜率 454.5，cap = 3000*0.05 = 150 -> 封顶 150；若只用 5 点窗口
    # 则斜率为 0、expected 应为 3000，此断言可区分两者。
    values = [0] * 15 + [3000] * 5
    result = predictor.predict_expected(
        hist(values), window_days=5, trend_cap=0.05,
    )
    assert result["median"] == 3000.0
    assert result["trend"] == 150.0
    assert result["expected"] == 3150.0
    assert result["startup_applied"] is False


def test_even_count_median_averages_middle_two():
    # 最后 4 天 [20,40,60,80] -> 中位数 (40+60)/2 = 50；
    # 斜率 11.9 > cap 50*0.15=7.5 -> 趋势 7.5
    values = [0] * 10 + [20, 40, 60, 80]
    result = predictor.predict_expected(hist(values), window_days=4)
    assert result["median"] == 50.0
    assert result["trend"] == 7.5
    assert result["expected"] == 57.5
    assert result["used_points"] == 14
    assert result["startup_applied"] is False


def test_startup_factor_applies_after_trend():
    # 8 点 < 14：先算 median+trend = 150+22.5 = 172.5，再乘 0.95
    values = [100, 100, 100, 100, 200, 300, 400, 500]
    result = predictor.predict_expected(
        hist(values), window_days=14, startup_factor=0.95,
    )
    assert result["median"] == 150.0
    assert result["trend"] == 22.5
    assert result["expected"] == pytest.approx(163.9)
    assert result["startup_applied"] is True
