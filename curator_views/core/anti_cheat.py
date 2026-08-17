"""Steam 鉴赏家浏览量反作弊检测模块。

纯算法模块（无 I/O）：检测外部恶意刷量导致的浏览量异常虚高
（overflow），并将溢出的浏览量分摊到后续日期，从而平滑浏览曲线，
避免浏览量曲线出现突兀尖峰而被 Steam 判定为机器人刷量。

检测手段（可叠加）：
- 相对预期超额：actual > expected * (1 + k)，防单日/连续尖峰；
- 长期基线超额：actual > long_baseline * (1 + k)，防平台抬升——
  即使短期预期已被虚高数据污染，长期基线（更长窗口的中位数）仍干净；
- 持续上升斜率：detect_drift，防每天温和爬升（单日永不超阈值）
  的慢性注入。

本模块与预测模块（predictor）相互独立，不互相引用，
仅依赖 Python 标准库。
"""

from __future__ import annotations

from statistics import median


def detect_overflow(
    history: list[dict],
    predictions: dict,
    k: float = 0.30,
    consecutive_days: int = 2,
    long_baseline: float | None = None,
    extra_candidates: set[int] | None = None,
) -> list[dict]:
    """检测浏览量异常虚高（overflow）的日期。

    Args:
        history: 按 date_ts 升序排列的每日浏览量列表，元素形如
            ``{"date_ts": int, "num_views": int}``。
        predictions: date_ts -> expected 的映射，表示该日期的预期浏览量。
        k: 异常判定阈值：实际浏览量超过 expected * (1 + k) 即视为
            候选异常日，默认 0.30。
        consecutive_days: 连续候选天数达到该值才标记为恶意刷量，
            默认 2。
        long_baseline: 长期基线（如最近 60 天中位数）。提供时，实际
            浏览量超过 long_baseline * (1 + k) 同样视为候选异常日——
            即使短期预期已被虚高数据污染，长期基线仍可作为独立参照。
        extra_candidates: 额外候选日期集合（如持续上升斜率检测的结果）。
            这些日期强制视为候选异常日，参与连续段判定。

    Returns:
        按时间顺序排列的检测结果列表，每个元素形如
        ``{date_ts, actual, expected, overflow_amount, candidate, flagged}``：
        - actual: 当日实际浏览量；
        - expected: 当日预期浏览量；
        - overflow_amount: 溢出量 = actual - expected；
        - candidate: 是否候选异常日（任一判定命中）；
        - flagged: 是否被标记为恶意刷量（连续候选达标）。

    Raises:
        ValueError: consecutive_days 小于 1。

    算法要点：
        1. 仅处理同时存在于 history（actual）与 predictions（expected）
           的日期；缺失任一方的日期不参与检测。
        2. 候选判定为"任一命中即候选"：相对预期超额、长期基线超额、
           额外候选（斜率检测）三者取或。
        3. 按时间顺序统计连续候选天数：当连续候选达到 consecutive_days
           时，该连续段内的**全部**候选日均标记为 flagged=True
           （含连续段的起始日，因为"溢出量 = 这些天的超出部分之和"）。
    """
    if consecutive_days < 1:
        raise ValueError("consecutive_days 必须大于等于 1")

    results: list[dict] = []
    candidate_run = 0
    # 记录当前连续候选段在 results 中的索引，段达标后整段标记 flagged
    run_indices: list[int] = []
    for item in history:
        date_ts = item["date_ts"]
        expected = predictions.get(date_ts)
        if expected is None:
            # 当日缺少预测值：不参与检测，同时中断连续候选计数
            candidate_run = 0
            run_indices.clear()
            continue

        actual = item["num_views"]
        is_candidate = actual > expected * (1.0 + k)
        if long_baseline is not None:
            is_candidate = is_candidate or actual > long_baseline * (1.0 + k)
        if extra_candidates and date_ts in extra_candidates:
            is_candidate = True
        idx = len(results)
        results.append(
            {
                "date_ts": date_ts,
                "actual": actual,
                "expected": expected,
                "overflow_amount": float(actual - expected),
                "candidate": is_candidate,
                "flagged": False,
            }
        )
        if is_candidate:
            candidate_run += 1
            run_indices.append(idx)
            if candidate_run >= consecutive_days:
                # 连续候选达标：该段内全部候选日均标记为恶意刷量，
                # 使“溢出量 = 连续异常日超出部分之和”完整成立。
                for run_idx in run_indices:
                    results[run_idx]["flagged"] = True
        else:
            candidate_run = 0
            run_indices.clear()

    return results


def compute_deductions(
    flagged_days: list[dict],
    absorb_days: int = 3,
) -> dict:
    """将恶意刷量日的溢出量分摊到后续日期，生成每日扣减表。

    Args:
        flagged_days: detect_overflow 的输出结果，仅 flagged=True
            的条目参与分摊。
        absorb_days: 溢出量分摊覆盖的后续自然日天数，必须大于 0，
            默认 3。

    Returns:
        date_ts -> total_deduction 的字典：表示在该日期应减少的
        预期浏览量（这些浏览量已由恶意刷量“垫”过，无需再补量），
        用于平滑浏览曲线。数值保留 1 位小数。

    Raises:
        ValueError: absorb_days 小于等于 0。

    算法要点：
        对每个被标记的刷量日，将其 overflow_amount 均分为 absorb_days
        份，分别计入该日之后第 1..absorb_days 个自然日
        （date_ts + 86400 * day）；同一天的多笔分摊累加求和，
        最终结果保留 1 位小数。
    """
    if absorb_days <= 0:
        raise ValueError("absorb_days 必须大于 0")

    deduction_map: dict = {}
    for entry in flagged_days:
        if not entry.get("flagged", False):
            continue
        date_ts = int(entry["date_ts"])
        share = float(entry["overflow_amount"]) / absorb_days
        for day in range(1, absorb_days + 1):
            target_ts = date_ts + 86400 * day
            deduction_map[target_ts] = (
                deduction_map.get(target_ts, 0.0) + share
            )

    return {
        date_ts: round(total, 1) for date_ts, total in deduction_map.items()
    }


def detect_drift(
    history: list[dict],
    slope_days: int = 14,
    slope_threshold: float = 0.02,
    min_points: int = 5,
    long_baseline: float | None = None,
    k: float = 0.30,
) -> set[int]:
    """检测最近窗口内是否存在持续的异常上升斜率（慢性爬升型刷量）。

    与逐日超额检测互补：每天只温和增长（单日永不超阈值）的注入无法被
    ``detect_overflow`` 发现，但会在窗口内形成显著正斜率。

    斜率本身不足以定罪（真实增长同样有正斜率），因此本函数只做"唤醒"：
    斜率超阈值后，仅把窗口内**高于长期基线 ×(1+k)** 的高位日标记为
    候选异常日，交给 ``detect_overflow`` 的 extra_candidates 参与连续段
    判定。没有提供长期基线时视为无法区分真实增长与慢性注入，返回空集。

    Args:
        history: 按 date_ts 升序排列的每日浏览量列表。
        slope_days: 斜率检测窗口（天数），默认 14。
        slope_threshold: 斜率阈值，相对窗口中位数（每天），默认 0.02
            （即 14 天累计约 +28%，与溢出阈值 0.30 相衔接）。
        min_points: 计算斜率所需的最少数据点数，默认 5。
        long_baseline: 长期基线（更长窗口的中位数，由调用方计算）。
            为 None 时不产生任何标记。
        k: 高位日判定阈值，相对长期基线，默认 0.30。

    Returns:
        斜率超阈值且存在高于长期基线 ×(1+k) 的高位日时，返回这些日期的
        date_ts 集合；否则返回空集。
    """
    window = history[-slope_days:]
    if len(window) < min_points:
        return set()

    values = [item["num_views"] for item in window]
    median_views = median(values)
    if median_views <= 0:
        return set()

    n = len(window)
    xs = list(range(n))
    sum_x = sum(xs)
    sum_y = sum(values)
    sum_xy = sum(x * y for x, y in zip(xs, values))
    sum_x2 = sum(x * x for x in xs)
    denom = n * sum_x2 - sum_x * sum_x
    if not denom:
        return set()

    slope = (n * sum_xy - sum_x * sum_y) / denom
    if slope <= median_views * slope_threshold:
        return set()
    if long_baseline is None:
        return set()

    threshold = long_baseline * (1.0 + k)
    return {
        item["date_ts"] for item in window
        if item["num_views"] > threshold
    }
