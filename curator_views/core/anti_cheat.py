"""Steam 鉴赏家浏览量反作弊检测模块。

纯算法模块（无 I/O）：检测外部恶意刷量导致的浏览量异常虚高
（overflow），并将溢出的浏览量分摊到后续日期，从而平滑浏览曲线，
避免浏览量曲线出现突兀尖峰而被 Steam 判定为机器人刷量。

本模块与预测模块（predictor）相互独立，不互相引用，
仅依赖 Python 标准库。
"""

from __future__ import annotations


def detect_overflow(
    history: list[dict],
    predictions: dict,
    k: float = 0.30,
    consecutive_days: int = 2,
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

    Returns:
        按时间顺序排列的检测结果列表，每个元素形如
        ``{date_ts, actual, expected, overflow_amount, candidate, flagged}``：
        - actual: 当日实际浏览量；
        - expected: 当日预期浏览量；
        - overflow_amount: 溢出量 = actual - expected；
        - candidate: 是否候选异常日（单日超过阈值）；
        - flagged: 是否被标记为恶意刷量（连续候选达标）。

    Raises:
        ValueError: consecutive_days 小于 1。

    算法要点：
        1. 仅处理同时存在于 history（actual）与 predictions（expected）
           的日期；缺失任一方的日期不参与检测。
        2. 按时间顺序统计连续候选天数：连续候选达到 consecutive_days
           时，当前日及其后的连续候选日均标记为 flagged=True。
           一段连续候选的首个日期因“前一天”不是候选而不被标记。
    """
    if consecutive_days < 1:
        raise ValueError("consecutive_days 必须大于等于 1")

    results: list[dict] = []
    candidate_run = 0
    for item in history:
        date_ts = item["date_ts"]
        expected = predictions.get(date_ts)
        if expected is None:
            # 当日缺少预测值：不参与检测，同时中断连续候选计数
            candidate_run = 0
            continue

        actual = item["num_views"]
        is_candidate = actual > expected * (1.0 + k)
        candidate_run = candidate_run + 1 if is_candidate else 0
        results.append(
            {
                "date_ts": date_ts,
                "actual": actual,
                "expected": expected,
                "overflow_amount": float(actual - expected),
                "candidate": is_candidate,
                "flagged": is_candidate and candidate_run >= consecutive_days,
            }
        )

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
