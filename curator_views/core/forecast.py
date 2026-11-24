"""未来 N 天「目标浏览量 / 假设自然流量 / 刷量缺口」估算（纯函数，无 I/O）。

这是**估算，不是精确预测**：Steam 统计滞后一天，预测首日基准需假设上一日实际；
未来自然流量只能按当前趋势外推，真实波动和外部刷量不会体现；异常标记未持久化，
因此不做恢复期推算。结果用于运行前判断配置是否空转或超载，不作为精确排期依据。

与 ``scheduler.run_daily`` 对齐但刻意有两处差异：未来日没有已标记的溢出量，故不做
溢出扣减（扣减恒为 0）；``in_cooldown`` 恒为 False，仅为保持输出结构稳定。

注意 ``run_daily`` 的检测参照是 ``base * max(1.0, multiplier)``，而刷量目标是
``base * multiplier``。``growth_factor < 1`` 时「目标」是托底值，检测参照仍是自然
水平；本模块输出的是**托底目标**，避免用户误把它当成检测参照。

首日（今天）可以复用 ``run`` 写入 ``daily_plan`` 的定档决策：配置签名一致时，
目标与抖动不再重新抽样，而是直接采用 ``run`` 当天的最终值，避免 forecast 显示
与 run 实际执行矛盾的缺口。未来日仍然只是独立抽样的预测。

每一行的刷量都会按 ``[target_min − 假设自然, target_max − 假设自然]`` 夹取
（``_bound_boost``），确保 ``假设自然 + need`` 不越过目标窗口；上限是绝对硬
限制，下限只在 ``need > 0`` 时生效，恢复期 / 列表不刷等状态不会被强行托底。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .predictor import predict_expected
from .scheduler import (
    _TZ,
    _apply_min_daily_change,
    _bound_boost,
    _ceiling_boost,
    _clamp_target,
    _daily_throughput_capacity,
    _effective_threads,
    _floor_boost,
    _jitter_bounds,
    _jitter_signature,
    _normalize_target_bounds,
    _sample_signed_jitter,
    _target_multiplier,
)


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
    first_day_boosted: int = 0,
    model_volatility: bool = True,
    first_day_plan: Optional[Dict[str, Any]] = None,
    prev_target: Optional[float] = None,
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
        first_day_boosted: 首日（今天）已经刷出的量；只从首日缺口里扣减，
            后续日期不受影响（明天的“昨日自然流量”已经把它扣过），与
            ``run_daily`` 的“今日已刷”续刷口径一致。
        model_volatility: 是否把 ``target_volatility`` / ``volatility`` 纳入
            预测。默认 True：每天独立抽样一次目标抖动与缺口抖动，按 ``run``
            的口径算目标与缺口；False：输出未抖动基准（用于确定性预览）。
        first_day_plan: ``run_daily`` 写入 ``daily_plan`` 的当日定档字典。
            ``model_volatility`` 为 True 且签名与日期一致时，首日直接复用定档的
            ``target`` / 抖动 / 配额 / 恢复期 / 列表状态，不再抽样；签名不一致时
            丢弃并重新抽样（warning ``plan_signature_mismatch``）；``None`` 时
            保持旧行为。未来日始终是独立抽样预测。
        prev_target: 预测首日「上一逻辑日」的最终目标，用于 ``min_daily_change``。
            通常由调用方从 ``daily_plan`` 取出上一日定档的 ``target``；``None``
            时首日不套用最小变动，从第二天起按上一行目标继续约束。

    逐日算法（与 ``run_daily`` 一致）：
        - ``base``：``baseline_views > 0`` 时取该固定值，否则取
          ``predict_expected(local_history)["expected"]``；
        - ``multiplier``：目标系数（表格列名「目标系数」），复用
          ``scheduler._target_multiplier``；stable 模式恒为 1.0；
        - ``target``：``base * multiplier`` 乘 ``(1 + 目标抖动)`` 后按
          ``target_min`` / ``target_max`` 夹取（0 表示该方向不设界），与
          ``run_daily`` 一致；不纳入波动率时目标抖动为 0；
        - ``target_low`` / ``target_high``：目标抖动区间预览
          （未抖动目标 × (1 ± ``target_volatility``) 后再夹取）；
        - ``assumed_natural``：``first_natural`` 非 None 时恒取该值，否则按
          ``natural_mode`` 推算；
        - ``gross_need = max(0, round(target - assumed_natural))``，
          未抖动缺口；未来日不做溢出扣减；
        - ``boost_quota = round(gross_need × (1 + 缺口抖动))``（不低于 0），
          再按 ``[target_min − 自然, target_max − 自然]`` 夹取（``_bound_boost``）；
        - ``need = max(0, boost_quota - first_day_boosted)``，后续日不扣已刷；
        - 首日若复用定档：``target`` / ``target_jitter`` / ``gap_jitter`` /
          ``gross_need`` / ``boost_quota`` / ``in_cooldown`` / 列表状态直接取
          ``first_day_plan``，不抽样；定档配额与最终 ``need`` 会再次按边界夹取
          （旧版定档或列表数值可能越界），``need`` 在恢复期为 0，列表模式取列表
          数值（已刷 > 0 时为 0），否则为 ``max(0, boost_quota - 已刷)``；
        - 每一行都带 ``bound_hit``（``""`` / ``"min"`` / ``"max"``）；只在实际
          改变了缺口时汇总成 ``target_bound_hit`` 警告；
        - 把假设日按 ``num_views = assumed_natural + first_day_boosted + need``
          回填进 ``local_history``，使后续天的预测窗口包含前面已刷的量。

    随机抽样只在本次调用内生效，不读写 ``kv_store``：同一次 forecast 内每天
    独立抽样，结果与 ``run`` 当天的持久化抖动值可能不同（预览是采样，不是
    精确复现）。

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
    target_min = float(config.get("target_min", 0) or 0)
    target_max = float(config.get("target_max", 0) or 0)
    target_floor, target_ceiling = _normalize_target_bounds(
        target_min, target_max
    )
    target_volatility = float(config.get("target_volatility", 0) or 0)
    target_volatility_min = float(
        config.get("target_volatility_min", 0) or 0
    )
    volatility = float(config.get("volatility", 0) or 0)
    volatility_min = float(config.get("volatility_min", 0) or 0)
    # 相邻两天最小变动（比例）：与 run_daily 同一口径，逐行套用到目标上。
    min_daily_change = abs(float(config.get("min_daily_change", 0) or 0))
    target_jitter_low, target_jitter_high = _jitter_bounds(
        target_volatility, target_volatility_min
    )
    gap_jitter_low, gap_jitter_high = _jitter_bounds(
        volatility, volatility_min
    )
    boost_n = int(config.get("boost_n", 5))
    single_cap = int(config.get("single_cap", 300))
    batch_size = int(config.get("batch_size", 50))
    threads = int(config.get("threads", 1))
    wait_min = float(config.get("wait_min", 8.0))
    wait_max = float(config.get("wait_max", 16.0))

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
    bound_counts = {"min": 0, "max": 0}
    bound_changes: List[Dict[str, Any]] = []

    # 与 run_daily 使用同一组归一化上下界与参数顺序，保证签名可比。
    current_signature = _jitter_signature(
        baseline_views,
        boost_mode,
        growth_factor,
        growth_rate,
        target_floor,
        target_ceiling,
        volatility,
        volatility_min,
        target_volatility,
        target_volatility_min,
        min_daily_change,
    )
    first_date_iso = _iso_date(int(start_ts))
    plan = first_day_plan if isinstance(first_day_plan, dict) else None
    first_plan_status = "missing"
    first_plan_updated_at: Optional[str] = None
    plan_warning: Optional[Dict[str, str]] = None
    if plan is not None:
        if not model_volatility:
            # --no-volatility 的语义是“输出未抖动基准”，不是“读取定档值”。
            first_plan_status = "ignored_no_volatility"
        elif plan.get("signature") != current_signature:
            first_plan_status = "signature_mismatch"
            plan_warning = {
                "code": "plan_signature_mismatch",
                "message": (
                    f"{first_date_iso} 的数据库定档记录是在不同的波动率/基线"
                    "配置下计算的（配置签名不一致），已丢弃并重新抽样；"
                    "run 当天也会按当前配置重新抽取抖动。"
                ),
            }
        elif plan.get("date") != first_date_iso:
            first_plan_status = "signature_mismatch"
            plan_warning = {
                "code": "plan_signature_mismatch",
                "message": (
                    "数据库定档记录的日期与预测首日不一致，已丢弃并重新抽样；"
                    "查看当天定档请用默认的 --today 视角。"
                ),
            }
        else:
            first_plan_status = "persisted"
            first_plan_updated_at = plan.get("updated_at")

    for index in range(max(0, int(days))):
        date_ts = int(start_ts) + 86400 * index
        use_plan = index == 0 and first_plan_status == "persisted"
        base = baseline_views if baseline_views > 0 else prediction
        multiplier = _target_multiplier(factor, growth_rate, drift_start, date_ts)
        raw_target = base * multiplier
        if use_plan:
            # 复用 run 已经抽好的目标抖动，不再消耗随机数。
            target_jitter = float(plan.get("target_jitter") or 0.0)
        else:
            target_jitter = 0.0
            if model_volatility and target_jitter_high > 0:
                target_jitter = _sample_signed_jitter(
                    target_jitter_high, target_jitter_low
                )
        jittered_target = raw_target
        if target_jitter:
            jittered_target = max(
                0.0, raw_target * (1.0 + target_jitter)
            )
        if use_plan:
            target = float(plan.get("target", raw_target))
            target_bound = str(plan.get("target_bound") or "")
        else:
            target, target_bound = _clamp_target(
                jittered_target, target_floor, target_ceiling
            )
        # 相邻两天最小变动：首日参照上一逻辑日目标（prev_target），之后参照
        # 上一行最终目标。首日复用定档时不再重复套用（run 写入的 target 已含）。
        min_change_applied = False
        if not use_plan and min_daily_change > 0:
            reference_target = (
                float(prev_target or 0.0) if index == 0
                else float(previous_target)
            )
            if reference_target > 0:
                adjusted, min_change_applied = _apply_min_daily_change(
                    target,
                    reference_target,
                    min_daily_change,
                    direction_hint=base - reference_target,
                    target_min=target_floor,
                    target_max=target_ceiling,
                )
                if min_change_applied:
                    target = adjusted
                    target_bound = _clamp_target(
                        target, target_floor, target_ceiling
                    )[1]
        # 目标抖动区间（预览用）：不随本次抽样变化，始终展示可能范围。
        target_low, _ = _clamp_target(
            max(0.0, raw_target * (1.0 - target_jitter_high)),
            target_floor,
            target_ceiling,
        )
        target_high, _ = _clamp_target(
            max(0.0, raw_target * (1.0 + target_jitter_high)),
            target_floor,
            target_ceiling,
        )

        if first_natural is not None:
            # 今日视角：自然流量不含刷量，刷完当天也不会抬高次日的自然基准
            assumed_natural = max(0, int(first_natural))
        elif natural_mode == "trend":
            assumed_natural = int(round(prediction))
        else:  # target：首日用初始预测，之后收敛到上一天目标
            assumed_natural = int(round(previous_target))
        assumed_natural = max(0, assumed_natural)

        # 未来日没有已被标记的溢出量，因此不做任何扣减；
        # 只有首日扣“今天已经刷过的量”，重跑时按剩余配额续刷。
        already_boosted = (
            max(0, int(first_day_boosted)) if index == 0 else 0
        )
        if use_plan:
            gross_need = int(plan.get("gross_need", 0))
            gap_jitter = float(plan.get("gap_jitter") or 0.0)
            boost_quota_raw = int(plan.get("boost_quota", gross_need))
            in_cooldown = bool(plan.get("in_cooldown"))
            list_status = plan.get("list_status")
            list_amount = plan.get("list_amount")
            bound_before = 0
            bound_hit = ""
            if in_cooldown:
                # 恢复期是“不刷”状态：即使定档配额越界也不托底、不计数。
                boost_quota = boost_quota_raw
                need = 0
            elif list_status == "active" and list_amount is not None:
                # 列表数值是当天总量，直接按边界夹取（0 行保持 0）。
                bound_before = 0 if already_boosted > 0 else int(list_amount)
                if bound_before > 0:
                    need, bound_hit = _bound_boost(
                        bound_before,
                        assumed_natural,
                        target_floor,
                        target_ceiling,
                    )
                else:
                    need = 0
                boost_quota = boost_quota_raw
            elif list_status in ("missing", "error", "empty", "already_boosted"):
                # 这些列表状态本身表示“今日不刷”，不得用配额重新补出来。
                boost_quota = boost_quota_raw
                need = 0
            else:
                boost_quota, quota_bound = _bound_boost(
                    boost_quota_raw,
                    assumed_natural,
                    target_floor,
                    target_ceiling,
                )
                bound_before = max(0, boost_quota_raw - already_boosted)
                need = max(0, boost_quota - already_boosted)
                # 再次夹取 need：定档可能写于旧版本（越界）。已刷部分计入
                # 自然基准，避免把“已刷 + 剩余”重复托底。
                if need > 0:
                    need, need_bound = _bound_boost(
                        need,
                        assumed_natural + already_boosted,
                        target_floor,
                        target_ceiling,
                    )
                else:
                    need_bound = ""
                bound_hit = need_bound or quota_bound
        else:
            gross_need = max(0, int(round(target - assumed_natural)))
            gap_jitter = 0.0
            if model_volatility and gap_jitter_high > 0:
                gap_jitter = _sample_signed_jitter(
                    gap_jitter_high, gap_jitter_low
                )
            boost_quota = gross_need
            if gap_jitter:
                boost_quota = max(
                    0, int(round(gross_need * (1.0 + gap_jitter)))
                )
            in_cooldown = False
            bound_before = max(0, boost_quota - already_boosted)
            boost_quota, bound_hit = _bound_boost(
                boost_quota, assumed_natural, target_floor, target_ceiling
            )
            need = max(0, boost_quota - already_boosted)
            if need > 0:
                need, need_bound = _bound_boost(
                    need,
                    assumed_natural + already_boosted,
                    target_floor,
                    target_ceiling,
                )
                bound_hit = need_bound or bound_hit
        if bound_hit:
            bound_counts[bound_hit] += 1
            if need != bound_before:
                bound_changes.append(
                    {
                        "date": _iso_date(date_ts),
                        "bound": bound_hit,
                        "before": bound_before,
                        "after": need,
                    }
                )
        cumulative_boost += need
        row = {
            "date": _iso_date(date_ts),
            "date_ts": date_ts,
            "base": round(float(base), 1),
            "multiplier": round(float(multiplier), 4),
            "raw_target": round(float(raw_target), 1),
            "target": round(float(target), 1),
            "target_low": round(float(target_low), 1),
            "target_high": round(float(target_high), 1),
            "target_jitter": round(float(target_jitter), 6),
            "target_bound": target_bound,
            "min_change_applied": min_change_applied,
            "assumed_natural": assumed_natural,
            "already_boosted": already_boosted,
            "gross_need": gross_need,
            "gap_jitter": round(float(gap_jitter), 6),
            "boost_quota": boost_quota,
            "need": need,
            "bound_hit": bound_hit,
            "cumulative_boost": cumulative_boost,
            "in_cooldown": in_cooldown,
            "plan_source": "persisted" if use_plan else "predicted",
        }
        if use_plan:
            row["plan_updated_at"] = plan.get("updated_at")
        rows.append(row)
        # 回填假设日：自然流量 + 今天已刷 + 当天缺口，供后续天预测窗口使用
        local_history.append(
            {
                "date_ts": date_ts,
                "num_views": assumed_natural + already_boosted + need,
            }
        )
        previous_target = target
        prediction = predict()

    throughput_capacity = _daily_throughput_capacity(
        threads, wait_min, wait_max, boost_n, batch_size
    )
    warnings = _build_warnings(
        history, rows, growth_rate, boost_n, single_cap,
        threads, wait_min, wait_max, batch_size,
    )
    if plan_warning is not None:
        warnings.append(plan_warning)
    if (target_jitter_high > 0 or gap_jitter_high > 0) and not model_volatility:
        parts: List[str] = []
        if target_jitter_high > 0:
            parts.append(
                f"目标在 ±{target_jitter_high:.0%}"
                f"（最小幅度 {target_jitter_low:.0%}）内随机"
            )
        if gap_jitter_high > 0:
            parts.append(
                f"当日缺口配额在 ±{gap_jitter_high:.0%}"
                f"（最小幅度 {gap_jitter_low:.0%}）内随机"
            )
        warnings.append(
            {
                "code": "volatility_not_modeled",
                "message": (
                    "已用 --no-volatility 关闭随机化，表中目标/缺口为未抖动基准："
                    + "；".join(parts)
                    + "。target_low/target_high 仍给出目标抖动区间。"
                ),
            }
        )
    if bound_changes:
        labels = {"min": "下限", "max": "上限"}
        shown = bound_changes[:5]
        parts = [
            f"{item['date']} {labels[item['bound']]} "
            f"{item['before']:,}→{item['after']:,}"
            for item in shown
        ]
        extra = ""
        remaining = len(bound_changes) - len(shown)
        if remaining > 0:
            extra = f"；…等 {remaining} 天"
        warnings.append(
            {
                "code": "target_bound_hit",
                "message": (
                    "以下日期的缺口被 target_min / target_max 夹取"
                    f"（下限命中 {bound_counts['min']} 天，"
                    f"上限命中 {bound_counts['max']} 天）："
                    + "；".join(parts)
                    + extra
                ),
            }
        )
    summary = _build_summary(rows)
    summary["first_plan_status"] = first_plan_status
    summary["first_plan_updated_at"] = first_plan_updated_at
    summary["throughput_capacity"] = throughput_capacity
    summary["effective_threads"] = _effective_threads(
        threads, boost_n, batch_size
    )
    summary["target_volatility"] = target_volatility
    summary["volatility"] = volatility
    summary["min_daily_change"] = min_daily_change
    summary["volatility_modeled"] = bool(model_volatility)
    summary["bound_hits"] = dict(bound_counts)
    # 边界对应的可刷量：仅在调用方给出 first_natural（今日视角）时计算，
    # 否则自然流量随预测逐日变化，统一汇总没有意义，返回 None。
    if first_natural is not None:
        natural_for_bounds = max(0, int(first_natural))
        summary["target_floor_boost"] = _floor_boost(
            natural_for_bounds, target_floor
        )
        summary["target_ceiling_boost"] = _ceiling_boost(
            natural_for_bounds, target_ceiling
        )
    else:
        summary["target_floor_boost"] = None
        summary["target_ceiling_boost"] = None
    return {"rows": rows, "warnings": warnings, "summary": summary}


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
    threads: int = 1,
    wait_min: float = 8.0,
    wait_max: float = 16.0,
    batch_size: int = 50,
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
    zero_rows = [
        row for row in rows
        if row["multiplier"] == 0.0 and row["target"] == 0.0
    ]
    if zero_rows:
        warnings.append(
            {"code": "multiplier_zero",
             "message": f"目标系数在 {zero_rows[0]['date']} 归零"
                        f"（growth_rate={growth_rate:g}），目标将归零并永久为 0，"
                        "请检查 growth_rate 配置"
                        "（或配置 target_min 托底）。"}
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

    throughput = _daily_throughput_capacity(
        threads, wait_min, wait_max, boost_n, batch_size
    )
    if throughput is not None:
        over_throughput = [
            f"      {row['date']} 需要 {row['need']:,} 次"
            for row in rows
            if row["need"] > throughput
        ]
        if over_throughput:
            warnings.append(
                {
                    "code": "throughput_capacity",
                    "message": (
                        f"以下日期超过按 threads={threads}、"
                        f"wait_min={wait_min:g}、wait_max={wait_max:g} "
                        f"估算的单日吞吐上限 {throughput:,} 次"
                        f"（实际并发 "
                        f"{_effective_threads(threads, boost_n, batch_size)}，"
                        "未计请求耗时）：\n"
                        + "\n".join(over_throughput)
                    ),
                }
            )
    return warnings
