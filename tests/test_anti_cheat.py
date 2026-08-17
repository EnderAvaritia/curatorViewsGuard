"""curator_views.core.anti_cheat 单元测试。

覆盖 detect_overflow 与 compute_deductions 两个纯算法函数：
- detect_overflow：候选日 actual > expected * (1+k)。当一段连续候选的
  天数达到 consecutive_days 时，该连续段内的**全部**候选日均标记为
  flagged（含起始日），使溢出扣减 = 连续异常日超出部分之和；
  单日波动 / 未达连续天数的候选不标记。predictions 为
  {date_ts: expected} 映射，缺失一方的日期不参与检测；
- compute_deductions：仅分摊 flagged 日期，overflow_amount 均分到
  未来 absorb_days 个自然日（date_ts + 86400 * day），保留 1 位小数。
"""

import pytest

from curator_views.core import anti_cheat

DAY = 86400
BASE_TS = 1_000_000


def hist(values, base=BASE_TS):
    """构造按天升序的 history 列表。"""
    return [
        {"date_ts": base + i * DAY, "num_views": int(v)}
        for i, v in enumerate(values)
    ]


def preds(values, base=BASE_TS):
    """构造 {date_ts: expected} 映射。"""
    return {base + i * DAY: float(v) for i, v in enumerate(values)}


# ── detect_overflow ──────────────────────────────────────────────────────


def test_no_overflow_detection():
    h = hist([100, 105, 98, 110, 102])
    p = preds([100] * 5)
    result = anti_cheat.detect_overflow(h, p)
    assert len(result) == 5
    assert [r["date_ts"] for r in result] == [BASE_TS + i * DAY for i in range(5)]
    for r, actual in zip(result, [100, 105, 98, 110, 102]):
        assert r["actual"] == actual
        assert r["expected"] == 100.0
        assert r["candidate"] is False
        assert r["flagged"] is False
        assert r["overflow_amount"] == float(actual - 100)


def test_single_spike_not_flagged():
    h = hist([100, 100, 100, 300, 100])
    p = preds([100] * 5)
    result = anti_cheat.detect_overflow(h, p)
    assert [r["candidate"] for r in result] == [False, False, False, True, False]
    assert all(r["flagged"] is False for r in result)
    assert result[3]["overflow_amount"] == 200.0


def test_consecutive_spike_whole_run_flagged():
    h = hist([100, 300, 320, 100, 100])
    p = preds([100] * 5)
    result = anti_cheat.detect_overflow(h, p)
    assert [r["candidate"] for r in result] == [False, True, True, False, False]
    # 连续段 [300, 320] 达到 consecutive_days=2，整段（含首日）均被标记
    assert [r["flagged"] for r in result] == [False, True, True, False, False]
    assert result[1]["overflow_amount"] == 200.0
    assert result[2]["overflow_amount"] == 220.0


def test_longer_run_all_flagged_after_qualification():
    # 连续 3 天候选：达到 2 天后整段标记，包含第一天
    h = hist([300, 310, 320, 100, 100])
    p = preds([100] * 5)
    result = anti_cheat.detect_overflow(h, p)
    assert [r["candidate"] for r in result] == [True, True, True, False, False]
    assert [r["flagged"] for r in result] == [True, True, True, False, False]


def test_custom_threshold_k():
    # k=0.5：阈值 150，200 是候选但前后不连续，全部不标记
    h = hist([100, 200, 140, 100])
    p = preds([100] * 4)
    result = anti_cheat.detect_overflow(h, p, k=0.5)
    assert [r["candidate"] for r in result] == [False, True, False, False]
    assert all(r["flagged"] is False for r in result)


def test_missing_prediction_breaks_run_and_is_excluded():
    # 第 3 天（索引 2）缺少预测值：不参与检测，且中断连续候选计数
    h = hist([100, 300, 9999, 300])
    p = {BASE_TS: 100.0, BASE_TS + DAY: 100.0, BASE_TS + 3 * DAY: 100.0}
    result = anti_cheat.detect_overflow(h, p)
    assert [r["date_ts"] for r in result] == [
        BASE_TS, BASE_TS + DAY, BASE_TS + 3 * DAY,
    ]
    # 索引 1 与 3 各自只有 1 个连续候选，都不标记
    assert all(r["flagged"] is False for r in result)
    assert all(r["candidate"] for r in result[1:])


def test_detect_overflow_invalid_consecutive_days():
    h = hist([100, 200])
    p = preds([100] * 2)
    with pytest.raises(ValueError):
        anti_cheat.detect_overflow(h, p, consecutive_days=0)
    with pytest.raises(ValueError):
        anti_cheat.detect_overflow(h, p, consecutive_days=-1)


# ── compute_deductions ───────────────────────────────────────────────────


def test_spread_evenly_over_absorb_days():
    flagged = [{"date_ts": BASE_TS, "overflow_amount": 300.0, "flagged": True}]
    result = anti_cheat.compute_deductions(flagged, absorb_days=3)
    assert result == {
        BASE_TS + DAY: 100.0,
        BASE_TS + 2 * DAY: 100.0,
        BASE_TS + 3 * DAY: 100.0,
    }


def test_multiple_flagged_days_accumulate():
    flagged = [
        {"date_ts": BASE_TS, "overflow_amount": 300.0, "flagged": True},
        {"date_ts": BASE_TS + DAY, "overflow_amount": 300.0, "flagged": True},
    ]
    result = anti_cheat.compute_deductions(flagged, absorb_days=3)
    assert result == {
        BASE_TS + DAY: 100.0,
        BASE_TS + 2 * DAY: 200.0,  # 两个刷量日都在此分摊
        BASE_TS + 3 * DAY: 200.0,
        BASE_TS + 4 * DAY: 100.0,
    }


def test_non_flagged_entries_ignored():
    flagged = [{"date_ts": BASE_TS, "overflow_amount": 999.0, "flagged": False}]
    assert anti_cheat.compute_deductions(flagged, absorb_days=3) == {}


def test_empty_flagged_days():
    assert anti_cheat.compute_deductions([], absorb_days=3) == {}


def test_deduction_rounded_to_one_decimal():
    flagged = [{"date_ts": BASE_TS, "overflow_amount": 200.0, "flagged": True}]
    result = anti_cheat.compute_deductions(flagged, absorb_days=3)
    assert len(result) == 3
    assert all(value == pytest.approx(66.7) for value in result.values())


def test_compute_deductions_invalid_absorb_days():
    flagged = [{"date_ts": BASE_TS, "overflow_amount": 10.0, "flagged": True}]
    with pytest.raises(ValueError):
        anti_cheat.compute_deductions(flagged, absorb_days=0)
    with pytest.raises(ValueError):
        anti_cheat.compute_deductions(flagged, absorb_days=-3)


# ── 端到端：detect -> deduct ─────────────────────────────────────────────


def test_detect_then_deduct_integration():
    h = hist([100, 300, 320, 100, 100])
    p = preds([100] * 5)
    overflow = anti_cheat.detect_overflow(h, p)
    flagged = [d for d in overflow if d["flagged"]]
    # 连续段 [300, 320] 两天都被标记
    assert [d["date_ts"] for d in flagged] == [
        BASE_TS + DAY, BASE_TS + 2 * DAY,
    ]

    deductions = anti_cheat.compute_deductions(flagged, absorb_days=3)
    # 200 与 220 分别均分 3 天；两段在后续日期重叠处累加
    assert deductions == {
        BASE_TS + 2 * DAY: pytest.approx(66.7),
        BASE_TS + 3 * DAY: pytest.approx(140.0),
        BASE_TS + 4 * DAY: pytest.approx(140.0),
        BASE_TS + 5 * DAY: pytest.approx(73.3),
    }


# ── detect_overflow：双基线 / 额外候选 ────────────────────────────────────


def test_long_baseline_catches_platform_rise():
    # 预期被虚高数据抬高（expected=300），相对预期不超额；
    # 但长期基线仍为 100 → 平台抬升被独立参照捕获
    h = hist([300, 300, 300])
    p = preds([300] * 3)
    result = anti_cheat.detect_overflow(h, p, long_baseline=100.0)
    # 300 > 100 × 1.3 = 130 → 全部候选；连续 3 天 → 全部标记
    assert [r["candidate"] for r in result] == [True, True, True]
    assert [r["flagged"] for r in result] == [True, True, True]


def test_long_baseline_not_triggered_when_close():
    # 实际 120，长期基线 100：120 > 130 不成立 → 不候选
    h = hist([120, 118, 121])
    p = preds([120, 118, 121])
    result = anti_cheat.detect_overflow(h, p, long_baseline=100.0)
    assert all(r["candidate"] is False for r in result)


def test_extra_candidates_force_candidate():
    # extra_candidates 中的日期强制为候选：相对预期与长期基线均不命中
    h = hist([100, 105, 103])
    p = preds([100] * 3)
    extra = {BASE_TS + DAY, BASE_TS + 2 * DAY}
    result = anti_cheat.detect_overflow(
        h, p, long_baseline=100.0, extra_candidates=extra,
    )
    # 连续 2 天候选 → 整段标记
    assert [r["candidate"] for r in result] == [False, True, True]
    assert [r["flagged"] for r in result] == [False, True, True]


# ── detect_drift：持续上升斜率检测 ────────────────────────────────────────


def test_drift_detects_steady_climb():
    # 每天 +3%（3 > 100 × 0.02 = 2）→ 斜率超阈值；配合干净长期基线 100，
    # 只标记窗口内高于 100 × 1.3 = 130 的高位日（100+3i > 130 → i ≥ 11）
    values = [100 + 3 * i for i in range(14)]
    h = hist(values)
    result = anti_cheat.detect_drift(
        h, slope_days=14, slope_threshold=0.02, long_baseline=100.0,
    )
    assert len(result) == 3
    assert BASE_TS + 13 * DAY in result
    assert BASE_TS + 10 * DAY not in result  # 130 不高于阈值，不算高位


def test_drift_flat_history_no_flag():
    h = hist([100] * 14)
    result = anti_cheat.detect_drift(
        h, slope_days=14, slope_threshold=0.02, long_baseline=100.0,
    )
    assert result == set()


def test_drift_without_long_baseline_no_flag():
    # 没有长期基线参照：无法区分真实增长与慢性注入，不产生标记
    values = [100 + 3 * i for i in range(14)]
    result = anti_cheat.detect_drift(
        hist(values), slope_days=14, slope_threshold=0.02,
    )
    assert result == set()


def test_drift_insufficient_data():
    h = hist([100, 101, 102])
    result = anti_cheat.detect_drift(
        h, slope_days=14, slope_threshold=0.02, long_baseline=100.0,
    )
    assert result == set()


def test_drift_zero_median_safe():
    h = hist([0] * 14)
    assert anti_cheat.detect_drift(h, long_baseline=0.0) == set()
