"""每日调度：抓取统计数据、预测、计算提升需求并执行。

完整流程（每天 10:00 UTC+8 由外部 cron / 计划任务触发）：
1. 抓取并入库 Steam 鉴赏家 g_StatsAppViews 数据（最新一条即昨日）；
2. 以程序启动日为界过滤历史，只使用启动日之后的数据做预测；
3. 逐日预测期望浏览量，检测异常溢出并计算后续扣减；
4. 计算今日缺口 = 今日期望 - 昨日实际 - 溢出扣减；
5. 如启用提升且缺口 > 0，调用 Booster 执行受控提升
   （或通过 executor_cmd 唤起独立执行器子进程，见 run_daily）；
6. 通过 Notifier 发送每日简报（有溢出或提升中止时升级为 warning）。

所有日期/时间均使用 Asia/Shanghai 时区。
"""

import json
import logging
import os
import random
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .anti_cheat import (
    compute_deductions,
    detect_drift,
    detect_overflow,
)
from .booster import Booster
from .fetcher import Fetcher
from .notifier import Notifier
from .predictor import predict_expected
from .storage import Storage

logger = logging.getLogger("curator_views.scheduler")

try:
    # 首选系统时区数据库（tzdata 包）
    _TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # 精简环境（如未安装 tzdata 的 Windows）回退到固定 UTC+8。
    # Asia/Shanghai 无夏令时，固定偏移在语义上等价。
    _TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _today_iso() -> str:
    """返回 Asia/Shanghai 时区的今天日期 (YYYY-MM-DD)。"""
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _today_epoch() -> int:
    """返回 Asia/Shanghai 今天 00:00 的时间戳。"""
    return _date_to_epoch(_today_iso())


def _date_to_epoch(date_str: str) -> int:
    """把 YYYY-MM-DD 解析为该日期在 Asia/Shanghai 时区 00:00 的时间戳。"""
    year, month, day = (int(part) for part in date_str.split("-"))
    return int(datetime(year, month, day, tzinfo=_TZ).timestamp())


def _format_summary(summary: Dict[str, Any], dry_run: bool) -> str:
    """把汇总字典格式化为每日简报正文。"""
    mode = "（演练模式，未实际提升）" if dry_run else ""
    lines = [
        f"日期: {summary['date']}",
        f"今日预期浏览量: {summary['today_expected']:.0f}",
        f"昨日实际浏览量: {summary['yesterday_actual']}",
        f"今日缺口: {summary['need']}",
        f"溢出扣减: {summary['deduction']:.1f}",
        f"溢出异常天数: {summary['overflow_flag_count']}",
        f"统计数据天数: {summary['stats_days']}",
        f"程序启动日: {summary['program_start']}",
        f"启动期加权: {'是' if summary['startup_applied'] else '否'}",
        f"实际提升: {summary['boosted']} {mode}",
    ]
    return "\n".join(lines)


def _run_executor(
    executor_cmd: str,
    need: int,
    boost_n: int,
    dry_run: bool,
    config_path: str = "",
) -> Dict[str, Any]:
    """通过子进程唤起独立执行器并解析其 JSON 输出。

    设计意图（调度器 / 执行器分离）：
    调度器只负责计算任务并下发，执行器（如 ``python cli.py boost``）
    作为独立进程被唤醒执行刷量，二者互不拖累：
    - 执行器崩溃不影响调度器进程本身；
    - 执行器可被手动单独触发（如 ``cli.py boost --target N``）。

    命令模板支持占位符：{need} / {apps} / {config}。
    执行器应以 JSON 输出 ``{"boosted": N, ...}``。

    Returns:
        执行结果摘要：{"boosted": int, "ok": bool, "cmd": str, "stderr": str}
    """
    cmd_text = executor_cmd.format(
        need=need, apps=boost_n, config=config_path or ""
    )

    if dry_run:
        logger.info("dry_run 模式：跳过执行器子进程（命令: %s）", cmd_text)
        return {"boosted": 0, "ok": True, "cmd": cmd_text, "stderr": ""}

    logger.info("唤起执行器子进程: %s", cmd_text)
    try:
        # Windows 使用 CreateProcess 语义（字符串直接解析，路径含空格/反斜杠安全），
        # POSIX 平台使用 shlex 拆分为参数列表。
        if os.name == "nt":
            proc = subprocess.run(
                cmd_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=7200,
            )
        else:
            proc = subprocess.run(
                shlex.split(cmd_text),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=7200,
            )
    except subprocess.TimeoutExpired:
        logger.error("执行器子进程超时（2 小时），命令: %s", cmd_text)
        return {"boosted": 0, "ok": False, "cmd": cmd_text, "stderr": "timeout"}
    except OSError as exc:
        logger.error("无法启动执行器子进程: %s", exc)
        return {"boosted": 0, "ok": False, "cmd": cmd_text, "stderr": str(exc)}

    if proc.returncode != 0:
        logger.error(
            "执行器子进程退出码 %d，stderr: %s",
            proc.returncode, proc.stderr.strip()[:500],
        )
        return {
            "boosted": 0,
            "ok": False,
            "cmd": cmd_text,
            "stderr": proc.stderr.strip(),
        }

    boosted = 0
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        boosted = int(payload.get("boosted", 0))
    except (json.JSONDecodeError, ValueError, IndexError):
        logger.warning(
            "无法解析执行器 JSON 输出，按 0 处理。输出尾部: %s",
            proc.stdout.strip()[-200:],
        )
    return {"boosted": boosted, "ok": True, "cmd": cmd_text, "stderr": ""}


def run_daily(
    storage: Storage,
    fetcher: Fetcher,
    notifier: Notifier,
    booster: Booster,
    config: Dict[str, Any],
    executor_cmd: str = "",
    config_path: str = "",
) -> Dict[str, Any]:
    """执行一次每日流程，返回汇总字典供 CLI / 上报使用。

    config 支持的键：
        window_days, trend_cap, startup_factor, trend_min_points,
        outlier_k, consecutive_days, absorb_days,
        boost_n, single_cap, batch_size, target_ratio,
        baseline_views, boost_mode, growth_factor,
        volatility, cooldown_days, exclude_flagged,
        baseline_days, slope_days, slope_threshold,
        boost_enabled (bool), dry_run (bool)
    其中 single_cap / batch_size / target_ratio 会应用到传入的 Booster
    实例（以 config 为准），其余作为预测/检测参数直接传入。

    baseline_views：
        大于 0 时启用手动基线模式：基准目标直接用该固定值，替代自动预测
        （中位数+趋势）。溢出检测的参照也使用该固定值，天然免疫被污染。
        为 0 或未配置时使用自动预测。

    boost_mode / growth_factor：
        统一的目标计算层：target = 基准 × 倍率。
        - stable（默认）：倍率恒为 1，维持稳定（补缺口到预测/基线水平）；
        - growth：倍率为 growth_factor（如 1.5 = 目标比基准高 50%），
          主动把浏览量刷到更高水平。
        检测参照与补缺口目标同步使用 target，避免 growth 模式把自己刷的
        量误判为恶意刷量。注意：growth 模式配手动基线（baseline_views）
        可刷到固定目标后稳定；配自动预测时目标随中位数水涨船高，会持续
        爬升，慎用。

    volatility：
        刷量波动率（0~0.3，建议 ≤0.15）。>0 时每日实际提升目标在
        「缺口 ×(1 ± volatility)」内随机，使浏览曲线围绕目标自然波动，
        避免收敛成一条直线。

    cooldown_days：
        恢复期天数。最近存在被标记为恶意刷量的日期时，恢复期内强制不刷量，
        让曲线在无注入的情况下自然回落到真实水平。

    exclude_flagged：
        为 True 时，被标记为恶意刷量的日期不参与预测（剔除出中位数窗口），
        防止虚高数据污染基线；检测/预测迭代至多 3 轮直至标记集收敛。

    baseline_days / slope_days / slope_threshold：
        双基线检测参数。baseline_days 为长期基线窗口（默认 60 天）；
        slope_days 与 slope_threshold 为持续上升斜率检测参数
        （默认最近 14 天斜率 > 中位数 × 2%/天 即标记）。

    executor_cmd：
        非空时，刷量通过独立执行器子进程完成（调度器/执行器分离，
        例如 "python cli.py boost --config {config} --target {need} --apps {apps}"）；
        为空时回退为在当前进程内直接调用 Booster（向后兼容）。
    """
    # ── 读取配置（带默认值）──────────────────────────────────────────────
    window_days = int(config.get("window_days", 14))
    trend_cap = float(config.get("trend_cap", 0.15))
    startup_factor = float(config.get("startup_factor", 0.9))
    trend_min_points = int(config.get("trend_min_points", 5))
    outlier_k = float(config.get("outlier_k", 0.30))
    consecutive_days = int(config.get("consecutive_days", 2))
    absorb_days = int(config.get("absorb_days", 3))
    boost_n = int(config.get("boost_n", 5))
    baseline_views = float(config.get("baseline_views", 0) or 0)
    boost_mode = str(config.get("boost_mode", "stable")).lower()
    growth_factor = float(config.get("growth_factor", 1.0) or 1.0)
    volatility = float(config.get("volatility", 0) or 0)
    cooldown_days = int(config.get("cooldown_days", 7))
    exclude_flagged = bool(config.get("exclude_flagged", True))
    baseline_days = int(config.get("baseline_days", 60))
    slope_days = int(config.get("slope_days", 14))
    slope_threshold = float(config.get("slope_threshold", 0.02))
    boost_enabled = bool(config.get("boost_enabled", False))
    dry_run = bool(config.get("dry_run", False))

    # Booster 的执行参数以 config 为准（调用方构建实例时可先给默认值）
    booster.batch_size = int(config.get("batch_size", booster.batch_size))
    booster.single_cap = int(config.get("single_cap", booster.single_cap))
    booster.target_ratio = float(config.get("target_ratio", booster.target_ratio))

    today_iso = _today_iso()
    today_ts = _today_epoch()

    # 1. 抓取统计数据并入库
    stats = fetcher.fetch_stats()
    for stat in stats:
        storage.upsert_daily_stats(stat["date_ts"], stat["num_views"])
    last_date_iso = ""
    if stats:
        last_ts = stats[-1]["date_ts"]
        last_date_iso = datetime.fromtimestamp(last_ts, tz=_TZ).strftime(
            "%Y-%m-%d"
        )
    storage.save_state(
        "last_stats_date",
        json.dumps({"last_date": last_date_iso, "days": len(stats)}),
    )

    # 2. 程序启动日：首次运行记录今天，之后只使用启动日之后的历史
    program_start = storage.load_state("program_start", default="")
    if not program_start:
        program_start = today_iso
        storage.save_state("program_start", program_start)
    program_start_epoch = _date_to_epoch(program_start)
    history_sorted = sorted(
        (d for d in stats if d["date_ts"] >= program_start_epoch),
        key=lambda d: d["date_ts"],
    )
    logger.info(
        "程序启动日=%s，启动后历史数据 %d 天（共 %d 天）。",
        program_start, len(history_sorted), len(stats),
    )

    # 3. 逐日预测 + 异常溢出检测。
    #    被标记为恶意刷量的日期不参与预测（剔除出中位数窗口），防止虚高
    #    数据污染基线；标记集单调累加（只增不减），无新标记即收敛，
    #    至多迭代 3 轮。
    #    目标 = 基准 × 倍率：stable 模式倍率为 1（维持稳定）；growth 模式
    #    倍率为 growth_factor（主动刷高）。检测参照与补缺口目标统一用
    #    目标值，避免 growth 模式把自己刷的量误判为恶意刷量。
    manual_baseline = baseline_views > 0
    multiplier = growth_factor if boost_mode == "growth" else 1.0
    confirmed_flagged: set[int] = set()
    history_clean = history_sorted
    overflow: list[dict] = []
    first_overflow: list[dict] = []
    max_rounds = 3 if exclude_flagged else 1
    for _ in range(max_rounds):
        history_clean = [
            d for d in history_sorted if d["date_ts"] not in confirmed_flagged
        ]
        # 长期基线（更长窗口的中位数）：即使短期预期被污染，仍是独立参照
        long_baseline: float | None = None
        if baseline_days > 0 and history_clean:
            base_window = history_clean[-min(len(history_clean), baseline_days):]
            long_baseline = median(d["num_views"] for d in base_window)
        # 持续上升斜率检测：慢性爬升型刷量（需要长期基线参照，防止
        # 把真实增长误判为异常）
        drift_days = detect_drift(
            history_clean,
            slope_days=slope_days,
            slope_threshold=slope_threshold,
            long_baseline=long_baseline,
            k=outlier_k,
        )
        predictions: Dict[int, float] = {}
        for i, day in enumerate(history_clean):
            if manual_baseline:
                predictions[day["date_ts"]] = baseline_views * multiplier
            else:
                pred = predict_expected(
                    history_clean[: i + 1],
                    window_days=window_days,
                    trend_cap=trend_cap,
                    startup_factor=startup_factor,
                    trend_min_points=trend_min_points,
                )
                predictions[day["date_ts"]] = float(pred["expected"]) * multiplier
        overflow = detect_overflow(
            history_clean, predictions, outlier_k, consecutive_days,
            long_baseline=long_baseline,
            extra_candidates=drift_days,
        )
        if not first_overflow:
            first_overflow = overflow
        new_flagged = {d["date_ts"] for d in overflow if d.get("flagged")}
        if not new_flagged:
            break
        confirmed_flagged |= new_flagged

    # 4. 昨日实际（g_StatsAppViews 最新一条）与今日期望
    yesterday_actual = 0
    if history_clean:
        yesterday_actual = int(history_clean[-1]["num_views"])
    # Steam 统计滞后一天：今日期望基于全部已入库数据（不含今天）
    history_without_today = [
        d for d in history_clean if d["date_ts"] < today_ts
    ]
    if manual_baseline:
        base_expected = baseline_views
        startup_applied = False
    else:
        today_pred = predict_expected(
            history_without_today,
            window_days=window_days,
            trend_cap=trend_cap,
            startup_factor=startup_factor,
            trend_min_points=trend_min_points,
        )
        base_expected = float(today_pred["expected"])
        startup_applied = bool(today_pred.get("startup_applied", False))
    today_expected = base_expected * multiplier

    # 5. 溢出扣减：只对被标记日产生的溢出量分摊到后续日期。
    #    检测事实以第一轮结果为准（攻击发生与否不随剔除而改变）；
    #    剔除仅用于净化预测窗口。
    flagged_days = [d for d in first_overflow if d.get("flagged")]
    flagged_ts_first = {d["date_ts"] for d in flagged_days}
    deductions = compute_deductions(flagged_days, absorb_days)
    today_deduction = float(deductions.get(today_ts, 0.0))

    # 6. 今日缺口 = 期望 - 昨日实际 - 溢出扣减。
    #    恢复期（最近存在被标记的恶意刷量日）内强制不刷，等待曲线自然回落；
    #    波动率 > 0 时在缺口附近注入随机抖动，使曲线围绕基线上下波动。
    need = max(0, round(today_expected - yesterday_actual - today_deduction))
    in_cooldown = False
    days_since_flagged = -1
    if flagged_ts_first:
        latest_flagged_ts = max(flagged_ts_first)
        days_since_flagged = (today_ts - latest_flagged_ts) // 86400
        in_cooldown = days_since_flagged < cooldown_days
    if in_cooldown:
        logger.info(
            "处于恶意刷量恢复期（最近标记日距今 %d 天 < %d 天），"
            "今日强制不刷量。",
            days_since_flagged, cooldown_days,
        )
        need = 0
    elif volatility > 0 and need > 0:
        base_need = need
        jitter = random.uniform(-volatility, volatility)
        need = max(0, round(need * (1.0 + jitter)))
        logger.info(
            "波动率 %.2f: 缺口 %d → 提升目标 %d",
            volatility, base_need, need,
        )

    # 7. 汇总字典
    summary: Dict[str, Any] = {
        "date": today_iso,
        "today_expected": today_expected,
        "yesterday_actual": yesterday_actual,
        "need": need,
        "deduction": today_deduction,
        "overflow_flag_count": len(flagged_days),
        "stats_days": len(stats),
        "program_start": program_start,
        "startup_applied": startup_applied,
        "baseline_mode": "manual" if manual_baseline else "auto",
        "boost_mode": boost_mode,
        "growth_factor": growth_factor,
        "today_target": round(today_expected, 1),
        "volatility": volatility,
        "in_cooldown": in_cooldown,
        "cooldown_days": cooldown_days,
    }

    # 8. 执行受控提升（调度器 / 执行器分离：优先子进程，回退直调）
    summary["boosted"] = 0
    summary["executor"] = "in_process" if not executor_cmd else "subprocess"
    if boost_enabled and need > 0:
        if executor_cmd:
            exec_result = _run_executor(
                executor_cmd,
                need=need,
                boost_n=boost_n,
                dry_run=dry_run,
                config_path=config_path,
            )
            summary["boosted"] = exec_result["boosted"]
            if not exec_result["ok"]:
                summary["executor_error"] = exec_result["stderr"]
        else:
            boosted = booster.run(need, n_recommendations=boost_n, dry_run=dry_run)
            summary["boosted"] = boosted
    elif need <= 0:
        logger.info("今日缺口 %d <= 0，无需提升。", need)
    else:
        logger.info("boost_enabled=False，跳过提升（缺口 %d）。", need)

    # 9. 发送每日简报
    warning = (
        summary["overflow_flag_count"] > 0
        or summary["in_cooldown"]
        or getattr(booster, "aborted", False)
        or "executor_error" in summary
    )
    level = "warning" if warning else "info"
    notifier.send("每日简报", _format_summary(summary, dry_run), level=level)

    return summary
