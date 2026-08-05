"""Steam 鉴赏家浏览量预测模块。

纯算法模块（无 I/O）：根据历史每日浏览量，预测未来的预期浏览量。
预测公式为：expected = median(最近 window_days 天) + 线性趋势斜率，
其中趋势斜率按中位数的一定比例封顶，避免极端值影响。
启动阶段（数据点少于一个窗口）会额外乘以保守系数 startup_factor，
防止在数据不足时把短期波动误判为真实增长。

仅依赖标准库 ``statistics``，与存储层 / 抓取层解耦，可独立复用。
"""

from __future__ import annotations

from statistics import median


def predict_expected(
    history: list[dict],
    window_days: int = 14,
    trend_cap: float = 0.15,
    startup_factor: float = 0.9,
    trend_min_points: int = 5,
) -> dict:
    """预测单日预期浏览量。

    Args:
        history: 按 date_ts 升序排列的每日浏览量列表，元素形如
            ``{"date_ts": int, "num_views": int}``，且只包含程序
            启动之后的日期（由调用方预先过滤）。
        window_days: 中位数计算窗口（天数），默认 14。
        trend_cap: 趋势斜率相对中位数的封顶比例，默认 0.15。
        startup_factor: 启动阶段保守系数，默认 0.9。
        trend_min_points: 计算趋势所需的最少数据点数，默认 5。

    Returns:
        形如 ``{expected, median, trend, used_points, startup_applied}``
        的字典：
        - expected: 预期浏览量，保留 1 位小数且不小于 0；
        - median: 窗口内浏览量中位数，保留 1 位小数；
        - trend: 每日线性趋势斜率（已封顶），保留 1 位小数；
        - used_points: 参与计算的历史数据点数；
        - startup_applied: 是否处于启动阶段并应用了保守系数。

    算法要点：
        1. history 为空时直接返回全 0 结果，startup_applied=True。
        2. 自适应窗口：取最近 min(len(history), window_days) 天的
           浏览量计算中位数；数据不足时使用全部可用数据。
        3. 当 len(history) >= trend_min_points 时，对最近
           min(len(history), window_days * 2) 个点做最小二乘线性回归
           （x 为 0 起始索引，y 为 num_views），斜率即每日趋势；
           否则趋势为 0。趋势绝对值封顶为 median * trend_cap。
        4. expected = median + trend，再叠加启动阶段保守系数，
           最终限制不小于 0 并保留 1 位小数。
    """
    if not history:
        return {
            "expected": 0.0,
            "median": 0.0,
            "trend": 0.0,
            "used_points": 0,
            "startup_applied": True,
        }

    used_points = len(history)

    # 自适应中位数窗口：数据点不足一个窗口时退化为使用全部数据
    window_size = max(window_days, 1)
    window = history[-min(used_points, window_size):]
    median_views = median(item["num_views"] for item in window)

    # 最小二乘线性回归，仅当数据点足够时计算趋势
    trend = 0.0
    if used_points >= trend_min_points:
        trend_window_size = max(window_days * 2, 1)
        points = history[-min(used_points, trend_window_size):]
        n = len(points)
        xs = list(range(n))
        ys = [item["num_views"] for item in points]
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_x2 = sum(x * x for x in xs)
        denom = n * sum_x2 - sum_x * sum_x
        if denom:
            slope = (n * sum_xy - sum_x * sum_y) / denom
            # 趋势封顶：|trend| <= median * trend_cap
            cap = max(0.0, median_views * trend_cap)
            trend = max(-cap, min(cap, slope))
        # denom 为 0 仅可能出现在 n <= 1 时，此时斜率无意义，trend 保持 0

    expected = median_views + trend

    # 启动阶段：数据点不足一个窗口时施加保守系数
    startup_applied = used_points < window_days
    if startup_applied:
        expected *= startup_factor

    expected = max(0.0, expected)

    return {
        "expected": round(expected, 1),
        "median": round(median_views, 1),
        "trend": round(trend, 1),
        "used_points": used_points,
        "startup_applied": startup_applied,
    }
