"""未来 N 天「目标浏览量 / 假设自然流量 / 刷量缺口」估算（纯函数，无 I/O）。

这是**估算，不是精确预测**：Steam 统计滞后一天，预测首日基准需假设上一日实际；
未来自然流量只能按当前趋势外推，真实波动和外部刷量不会体现；异常标记未持久化，
因此不做恢复期推算。结果用于运行前判断配置是否空转或超载，不作为精确排期依据。

与 ``scheduler.run_daily`` 对齐但刻意有两处差异：未来日没有已标记的溢出量，故不做
溢出扣减（扣减恒为 0）；``in_cooldown`` 恒为 False，仅为保持输出结构稳定。

注意 ``run_daily`` 的检测参照是 ``base * max(1.0, multiplier)``，而刷量目标是
``base * multiplier``。``growth_factor < 1`` 时「目标」是托底值，检测参照仍是自然
水平；本模块输出的是**托底目标**，避免用户误把它当成检测参照。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .predictor import predict_expected
from .scheduler import _TZ, _target_multiplier


def _iso_date(date_ts: int) -> str:
    """把 epoch 转为 Asia/Shanghai 时区的 YYYY-MM-DD。"""
    return datetime.fromtimestamp(int(date_ts), tz=_TZ).strftime("%Y-%m-%d")


def forecast_days(
    history: List[Dict[str, Any]],
    config: Dict[str, Any],
    start_ts: int,
    days: int = 7,
    growth_start_ts: int = 0,
    natural_mode: str = "trend",
    first_natural: Optional[int] = None,
) -> Dict[str, Any]:
    """逐日推算未来 ``days`` 天的目标、假设自然流量与刷量缺口。

    Args:
        history: 按 ``date_ts`` 升序的每日统计（``{date_ts, num_views}``），
            已按程序启动日过滤。
        config: 键与 ``cli._scheduler_config`` 返回字典一致的配置。
        start_ts: 预测首日 00:00 的 epoch（Asia/Shanghai）。
        days: 预测天数。
        growth_start_ts: 已解析好的 growth 速率起点 epoch；0 表示按 ``start_ts`` 起算。
        natural_mode: ``"trend"`` 假设自然流量按当前趋势延续；``"target"`` 假设
            自然流量最终收敛到目标（首日用初始历史预测值，之后用上一天目标）。
        first_natural: 非 None 时启用“今日视角”：每天的假设自然流量固定为该值
            （通常为最新实际 − 最新刷量，即 ``run_daily`` 的“昨日自然流量”）。
            此时忽略 ``natural_mode``；刷量只抬高实际浏览量，不会抬高自然流量，
            因此每一天的缺口都会重新按 ``target - first_natural`` 计算，与
            ``run_daily`` 的每日补缺口口径一致。

    逐日算法（与 ``run_daily`` 一致）：
        - ``base``：``baseline_views > 0`` 时取该固定值，否则取
          ``predict_expected(local_history)["expected"]``；
        - ``multiplier``：复用 ``scheduler._target_multiplier``；stable 模式倍率恒为 1.0；
        - ``target = base * multiplier``；
        - ``assumed_natural``：``first_natural`` 非 None 时恒取该值，否则按
          ``natural_mode`` 推算；
        - ``need = max(0, round(target - assumed_natural))``，未来日不做溢出扣减；
        - 把假设日按 ``num_views = assumed_natural + need`` 回填进 ``local_history``，
          使后续天的预测窗口包含前面已刷的量。

    异常标记未持久化，``in_cooldown`` 恒为 False，本函数不读取数据库，也不做恢复期推算。

    Returns:
        ``{"rows": [...], "warnings": [...], "summary": {...}}``。
    """
    if natural_mode not in ("trend", "target"):
        raise ValueError(f"natural_mode 只能是 trend 或 target，收到: {natural_mode}")

    window_days = int(config.get("window_days", 14))
    trend_cap = float(config.get("trend_cap", 0.15))
    startup_factor = float(config.get("startup_factor", 0.9))
    trend_min_points = int(config.get("trend_min_points", 5))
    baseline_views = float(config.get("baseline_views", 0) or 0)
    boost_mode = str(config.get("boost_mode", "stable")).lower()
    growth_factor = float(config.get("growth_factor", 1.0) or 1.0)
    growth_rate = float(config.get("growth_rate", 0) or 0)
    boost_n = int(config.get("boost_n", 5))
    single_cap = int(config.get("single_cap", 300))

    # 与 run_daily 一样：只有 growth 且速率非 0 时才启用速率锚点；stable 恒为 1.0
    factor = growth_factor if boost_mode == "growth" else 1.0
    if boost_mode == "growth" and growth_rate != 0:
        drift_start = int(growth_start_ts) if growth_start_ts > 0 else int(start_ts)
    else:
        drift_start = 0

    local_history = [
        {"date_ts": int(item["date_ts"]), "num_views": int(item["num_views"])}
        for item in history
    ]

    def predict() -> float:
        return float(
            predict_expected(
                local_history,
                window_days=window_days,
                trend_cap=trend_cap,
                startup_factor=startup_factor,
                trend_min_points=trend_min_points,
            )["expected"]
        )

    prediction = predict()
    previous_target = prediction
    rows: List[Dict[str, Any]] = []
    cumulative_boost = 0

    for index in range(max(0, int(days))):
        date_ts = int(start_ts) + 86400 * index
        base = baseline_views if baseline_views > 0 else prediction
        multiplier = _target_multiplier(factor, growth_rate, drift_start, date_ts)
        target = base * multiplier

        if first_natural is not None:
            # 今日视角：自然流量不含刷量，刷完当天也不会抬高次日的自然基准
            assumed_natural = max(0, int(first_natural))
        elif natural_mode == "trend":
            assumed_natural = int(round(prediction))
        else:  # target：首日用初始预测，之后收敛到上一天目标
            assumed_natural = int(round(previous_target))
        assumed_natural = max(0, assumed_natural)

        # 未来日没有已被标记的溢出量，因此不做任何扣减
        need = max(0, int(round(target - assumed_natural)))
        cumulative_boost += need
        rows.append(
            {
                "date": _iso_date(date_ts),
                "date_ts": date_ts,
                "base": round(float(base), 1),
                "multiplier": round(float(multiplier), 4),
                "target": round(float(target), 1),
                "assumed_natural": assumed_natural,
                "need": need,
                "cumulative_boost": cumulative_boost,
                "in_cooldown": False,
            }
        )
        # 回填假设日：自然流量 + 当天缺口，供后续天预测窗口使用
        local_history.append(
            {"date_ts": date_ts, "num_views": assumed_natural + need}
        )
        previous_target = target
        prediction = predict()

    warnings = _build_warnings(
        history, rows, growth_rate, boost_n, single_cap
    )
    return {"rows": rows, "warnings": warnings, "summary": _build_summary(rows)}


def _build_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总预测区间与刷量缺口。"""
    if not rows:
        return {
            "first_date": "", "last_date": "", "total_need": 0,
            "max_need": 0, "target_first": 0.0, "target_last": 0.0, "days": 0,
        }
    return {
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "total_need": sum(row["need"] for row in rows),
        "max_need": max(row["need"] for row in rows),
        "target_first": rows[0]["target"],
        "target_last": rows[-1]["target"],
        "days": len(rows),
    }


def _build_warnings(
    history: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    growth_rate: float,
    boost_n: int,
    single_cap: int,
) -> List[Dict[str, str]]:
    """根据预测结果生成中文提示（code + 具体到日期/数字的 message）。"""
    warnings: List[Dict[str, str]] = []
    if not history:
        warnings.append(
            {"code": "no_history",
             "message": "本地没有可用历史数据，所有基准为 0"
                        "（请先运行 run 积累统计数据）。"}
        )
    if rows and all(row["need"] == 0 for row in rows):
        warnings.append(
            {"code": "need_all_zero",
             "message": f"预测区间 {rows[0]['date']} ~ {rows[-1]['date']} 内"
                        "目标始终低于假设自然流量，系统不会产生刷量。"}
        )
    zero_rows = [row for row in rows if row["multiplier"] == 0.0]
    if zero_rows:
        warnings.append(
            {"code": "multiplier_zero",
             "message": f"目标倍率在 {zero_rows[0]['date']} 归零"
                        f"（growth_rate={growth_rate:g}），目标将归零并永久为 0，"
                        "请检查 growth_rate 配置。"}
        )

    capacity = max(0, boost_n) * max(0, single_cap)
    over_capacity: List[str] = []
    for row in rows:
        if row["need"] > capacity:
            over_capacity.append(
                f"      {row['date']} 需要 {row['need']:,} 次"
            )
    if over_capacity:
        warnings.append(
            {
                "code": "over_capacity",
                "message": (
                    f"以下日期超过单日容量 {capacity:,} 次"
                    f"（boost_n={boost_n} × single_cap={single_cap}）：\n"
                    + "\n".join(over_capacity)
                ),
            }
        )
    return warnings