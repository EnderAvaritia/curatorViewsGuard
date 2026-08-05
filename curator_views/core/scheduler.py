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
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .anti_cheat import compute_deductions, detect_overflow
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
        boost_enabled (bool), dry_run (bool)
    其中 single_cap / batch_size / target_ratio 会应用到传入的 Booster
    实例（以 config 为准），其余作为预测/检测参数直接传入。

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

    # 3. 逐日预测：每个日期用截至当天的历史做一次预测
    predictions: Dict[int, float] = {}
    for i, day in enumerate(history_sorted):
        pred = predict_expected(
            history_sorted[: i + 1],
            window_days=window_days,
            trend_cap=trend_cap,
            startup_factor=startup_factor,
            trend_min_points=trend_min_points,
        )
        predictions[day["date_ts"]] = float(pred["expected"])

    # 4. 昨日实际（g_StatsAppViews 最新一条）与今日期望
    yesterday_actual = 0
    if history_sorted:
        yesterday_actual = int(history_sorted[-1]["num_views"])
    # Steam 统计滞后一天：今日期望基于全部已入库数据（不含今天）
    history_without_today = [
        d for d in history_sorted if d["date_ts"] < today_ts
    ]
    today_pred = predict_expected(
        history_without_today,
        window_days=window_days,
        trend_cap=trend_cap,
        startup_factor=startup_factor,
        trend_min_points=trend_min_points,
    )
    today_expected = float(today_pred["expected"])
    startup_applied = bool(today_pred.get("startup_applied", False))

    # 5. 异常溢出检测与后续扣减
    overflow = detect_overflow(
        stats, predictions, outlier_k, consecutive_days,
    )
    flagged_days = [d for d in overflow if d.get("flagged")]
    deductions = compute_deductions(flagged_days, absorb_days)
    today_deduction = float(deductions.get(today_ts, 0.0))

    # 6. 今日缺口 = 期望 - 昨日实际 - 溢出扣减
    need = max(0, round(today_expected - yesterday_actual - today_deduction))

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
        or getattr(booster, "aborted", False)
        or "executor_error" in summary
    )
    level = "warning" if warning else "info"
    notifier.send("每日简报", _format_summary(summary, dry_run), level=level)

    return summary
