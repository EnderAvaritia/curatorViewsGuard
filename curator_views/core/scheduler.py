"""每日调度：抓取统计数据、预测、计算提升需求并执行。

完整流程（每天 10:00 UTC+8 由外部 cron / 计划任务触发）：
1. 抓取并入库 Steam 鉴赏家 g_StatsAppViews 数据（最新一条即昨日）；
2. 以程序启动日为界过滤历史，只使用启动日之后的数据做预测；
3. 逐日预测期望浏览量，检测异常溢出并计算后续扣减；
4. 计算今日缺口 = 今日期望 - 昨日自然流量 - 溢出扣减
   （昨日自然流量 = 昨日实际 - 昨日刷量，避免把昨天的补量当成今天的基础）；
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
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .anti_cheat import (
    compute_deductions,
    detect_drift,
    detect_overflow,
)
from .boost_list import consume_next, peek_next
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


def _resolve_growth_start(
    storage: Storage, growth_rate: float, today_iso: str
) -> str:
    """返回当前速率生效的起始日；速率变化时把起点重置为今天。

    锚点持久化在 kv_store 中（键 ``growth_rate`` / ``growth_start``）：
    同一速率跨进程重启仍沿用原起点，保证"每天降 1%"按日历连续推进；
    一旦速率被改动（含从非 0 改为 0），起点重置为今天，避免历史累计
    天数突然生效造成目标跳变。
    """
    stored_rate = storage.load_state("growth_rate", default="")
    stored_start = storage.load_state("growth_start", default="")
    same_rate = False
    try:
        same_rate = abs(float(stored_rate) - growth_rate) < 1e-9
    except (TypeError, ValueError):
        same_rate = False
    if not same_rate or not stored_start:
        storage.save_state("growth_rate", f"{growth_rate:.12g}")
        storage.save_state("growth_start", today_iso)
        return today_iso
    try:
        _date_to_epoch(stored_start)
    except (ValueError, IndexError):
        storage.save_state("growth_start", today_iso)
        return today_iso
    return stored_start


def _target_multiplier(
    factor: float, growth_rate: float, drift_start_ts: int, day_ts: int
) -> float:
    """计算某天的目标倍率：factor × (1 + growth_rate × 距生效日天数)。

    - ``growth_rate`` 为 0 时只返回 factor（不随时间变化）；
    - 天数为负（生效日之前的日期）按 0 处理，倍率不会因历史日期穿越；
    - 结果夹到非负，避免极端负速率把目标算成负数。
    """
    if growth_rate == 0 or drift_start_ts <= 0:
        return max(0.0, factor)
    days = max(0, (int(day_ts) - drift_start_ts) // 86400)
    return max(0.0, factor * (1.0 + growth_rate * days))


def _format_summary(summary: Dict[str, Any], dry_run: bool) -> str:
    """把汇总字典格式化为每日简报正文。"""
    mode = "（演练模式，未实际提升）" if dry_run else ""
    lines = [
        f"日期: {summary['date']}",
        f"今日预期浏览量: {summary['today_expected']:.0f}",
        f"昨日实际浏览量: {summary['yesterday_actual']}",
        f"昨日刷量: {summary.get('yesterday_boosted', 0)}",
        f"昨日自然流量: {summary.get('yesterday_natural', summary['yesterday_actual'])}",
        f"今日缺口: {summary['need']}",
        f"溢出扣减: {summary['deduction']:.1f}",
        f"溢出异常天数: {summary['overflow_flag_count']}",
        f"统计数据天数: {summary['stats_days']}",
        f"程序启动日: {summary['program_start']}",
        f"启动期加权: {'是' if summary['startup_applied'] else '否'}",
    ]
    list_status = summary.get("boost_list_status", "disabled")
    if list_status != "disabled":
        labels = {
            "active": "使用中",
            "empty": "已用完",
            "missing": "文件缺失",
            "error": "读取失败",
            "cooldown": "恢复期暂停",
            "consume_error": "消费失败",
        }
        lines.append(
            f"刷量列表: {labels.get(list_status, list_status)}"
            f"（今日 {summary.get('boost_list_amount') or 0}，"
            f"剩余 {summary.get('boost_list_remaining', 0)} 行）"
        )
    lines.append(f"实际提升: {summary['boosted']} {mode}")
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
        boost_n, single_cap, batch_size, target_ratio, boost_distribution,
        baseline_views, boost_mode, growth_factor,
        volatility, cooldown_days, exclude_flagged,
        baseline_days, slope_days, slope_threshold,
        boost_enabled (bool), dry_run (bool),
        boost_list_file (str)
    其中 single_cap / batch_size / target_ratio / boost_distribution 会应用到
    传入的 Booster 实例（以 config 为准），其余作为预测/检测参数直接传入。

    baseline_views：
        大于 0 时启用手动基线模式：基准目标直接用该固定值，替代自动预测
        （中位数+趋势）。溢出检测的参照也使用该固定值，天然免疫被污染。
        为 0 或未配置时使用自动预测。

    昨日刷量补偿：
        缺口以"昨日自然流量"为基准（昨日实际 − boost_log 记录的昨日刷量），
        而不是直接用昨日实际。否则昨天的补量会被当成今天已有的基础重复
        扣减，导致目标补不满并在目标附近两天一摆。台账缺失时按 0 回退。

boost_mode / growth_factor / growth_rate：
        统一的目标计算层：target = 基准 × 倍率 × 速率因子。
        - stable（默认）：倍率恒为 1，速率不生效（维持稳定，补缺口到预测/基线水平）；
        - growth：倍率为 growth_factor，速率因子为
          ``1 + growth_rate × 距生效日天数``（天数为日历天，不随重启丢失）。
        因此 growth_factor 与 growth_rate 是相乘关系、互不冲突：
        - growth_factor 管"水平"：>1 抬高台阶，<1 压低台阶（如 0.85 表示
          先允许跌到基准的 85%，跌破了才托住）；
        - growth_rate 管"斜率"：>0 每天线性抬升（如 0.01 = 每天 +基准的 1%），
          <0 每天匀速下调（如 -0.01 = 每天 -1% 的基准，即匀速下滑）。
        只在 growth 模式下生效；stable 模式下两者都被忽略。
        检测参照与补缺口目标同步：增长时用抬高后的目标，避免把自己刷的量
        误判为恶意刷量；下调时用自然预期作为参照，避免"实际高于下滑目标"
        被误判为异常。
        growth_rate 的生效起点是首次使用该速率的当天（持久化在 kv_store），
        改动速率会从改动当天重新起算，避免目标突然跳变。

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

    boost_list_file：
        可选。非空时启用"每日刷量列表"模式：列表文件一行一个刷量数量，
        每天取第一条有效行作为当日提升目标，覆盖按预测/基线算出的缺口。
        成功提升（boosted > 0）后把该行从文件删除，实现"刷完一行删一行"。
        以下情况不消费列表行：dry_run、boost_enabled=False、恢复期
        （cooldown）、缺口为 0、提升完全失败。列表为空或文件缺失时今日
        不刷量（不会回退到自动预测，避免列表模式被悄悄绕过）；文件缺失
        属于配置错误，简报会标记为 warning。
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
    growth_rate = float(config.get("growth_rate", 0) or 0)
    volatility = float(config.get("volatility", 0) or 0)
    cooldown_days = int(config.get("cooldown_days", 7))
    exclude_flagged = bool(config.get("exclude_flagged", True))
    baseline_days = int(config.get("baseline_days", 60))
    slope_days = int(config.get("slope_days", 14))
    slope_threshold = float(config.get("slope_threshold", 0.02))
    boost_enabled = bool(config.get("boost_enabled", False))
    dry_run = bool(config.get("dry_run", False))
    boost_list_file = str(config.get("boost_list_file", "") or "").strip()

    # Booster 的执行参数以 config 为准（调用方构建实例时可先给默认值）
    booster.batch_size = int(config.get("batch_size", booster.batch_size))
    booster.single_cap = int(config.get("single_cap", booster.single_cap))
    booster.target_ratio = float(config.get("target_ratio", booster.target_ratio))
    booster.distribution = str(
        config.get("boost_distribution", "ratio")
    ).lower()

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
    #    目标 = 基准 × 倍率 × 速率因子：stable 模式倍率为 1（维持稳定）；
    #    growth 模式倍率为 growth_factor，速率因子按 growth_rate 线性推进。
    #    检测参照：增长时用抬高后的目标（避免把自己刷的量误判为恶意刷量），
    #    下调时用自然预期（避免"实际高于下滑目标"被误判为异常）。
    manual_baseline = baseline_views > 0
    multiplier = growth_factor if boost_mode == "growth" else 1.0
    drift_start_ts = 0
    if boost_mode == "growth" and growth_rate != 0:
        drift_start_ts = _date_to_epoch(
            _resolve_growth_start(storage, growth_rate, today_iso)
        )
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
                base = float(baseline_views)
            else:
                pred = predict_expected(
                    history_clean[: i + 1],
                    window_days=window_days,
                    trend_cap=trend_cap,
                    startup_factor=startup_factor,
                    trend_min_points=trend_min_points,
                )
                base = float(pred["expected"])
            day_multiplier = _target_multiplier(
                multiplier, growth_rate, drift_start_ts, day["date_ts"]
            )
            predictions[day["date_ts"]] = base * max(1.0, day_multiplier)
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

    # 4. 昨日实际（g_StatsAppViews 最新一条）与今日期望。
    #    注意：昨日实际里包含昨天我们自己刷的量，而今天刷的量只计入今天。
    #    若直接拿"昨日实际"当今天的自然流量基准，等于把昨天的补量重复扣
    #    一次，会造成目标永远补不满、并在目标附近来回补/不补（两天一摆）。
    #    因此从 boost_log 取回昨日刷量，还原"昨日自然流量"作为今日基准。
    yesterday_actual = 0
    yesterday_boosted = 0
    if history_clean:
        last_stat = history_clean[-1]
        yesterday_actual = int(last_stat["num_views"])
        last_stat_date = datetime.fromtimestamp(
            int(last_stat["date_ts"]), tz=_TZ
        ).strftime("%Y-%m-%d")
        yesterday_boosted = max(
            0, int(storage.get_boost_total(last_stat_date))
        )
    # 台账缺失（旧数据 / 子进程执行器写了别的库）时退化为旧行为（按 0 处理）；
    # 刷量记录大于实际值时夹到 0，避免自然流量算成负数导致超额补量。
    yesterday_natural = max(0, yesterday_actual - yesterday_boosted)
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
    today_multiplier = _target_multiplier(
        multiplier, growth_rate, drift_start_ts, today_ts
    )
    today_expected = base_expected * today_multiplier

    # 5. 溢出扣减：只对被标记日产生的溢出量分摊到后续日期。
    #    检测事实以第一轮结果为准（攻击发生与否不随剔除而改变）；
    #    剔除仅用于净化预测窗口。
    flagged_days = [d for d in first_overflow if d.get("flagged")]
    flagged_ts_first = {d["date_ts"] for d in flagged_days}
    deductions = compute_deductions(flagged_days, absorb_days)
    today_deduction = float(deductions.get(today_ts, 0.0))

    # 6. 今日缺口 = 期望 - 昨日自然流量 - 溢出扣减。
    #    自然流量已剔除昨日自己刷的量，补多少就只补"自然流量与目标之间"
    #    的真实差额，不会把昨天的补量当成今天已有的基础重复扣减。
    #    恢复期（最近存在被标记的恶意刷量日）内强制不刷，等待曲线自然回落；
    #    波动率 > 0 时在缺口附近注入随机抖动，使曲线围绕基线上下波动。
    need = max(0, round(today_expected - yesterday_natural - today_deduction))
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

    # 6.5 每日刷量列表模式：以列表首行数量覆盖上面算出的缺口。
    #     - 列表为空/文件缺失 → 今日不刷（不回退到自动预测，避免绕过列表）；
    #     - 恢复期（刚被恶意刷量）优先于列表，列表行保留到明天；
    #     - 波动率只作用于自动缺口，列表数值视为明确要求，不做随机抖动。
    list_amount: Optional[int] = None
    list_remaining = 0
    list_status = "disabled"
    if boost_list_file:
        try:
            list_amount, list_remaining = peek_next(boost_list_file)
        except FileNotFoundError:
            list_status = "missing"
            logger.error(
                "刷量列表文件不存在: %s，今日不刷量（请检查 boost_list_file 配置）。",
                boost_list_file,
            )
            need = 0
        except OSError as exc:
            list_status = "error"
            logger.error("读取刷量列表失败: %s，今日不刷量。", exc)
            need = 0
        else:
            if list_amount is None:
                list_status = "empty"
                logger.info("刷量列表已用完（无有效行），今日不刷量。")
                need = 0
            elif in_cooldown:
                list_status = "cooldown"
                logger.info(
                    "恢复期内不刷量，列表首行 %d 保留到明天。", list_amount,
                )
                need = 0
            else:
                list_status = "active"
                logger.info(
                    "列表模式: 今日目标 %d（剩余 %d 行，原缺口 %d）",
                    list_amount, list_remaining, need,
                )
                need = list_amount

    # 7. 汇总字典
    summary: Dict[str, Any] = {
        "date": today_iso,
        "today_expected": today_expected,
        "yesterday_actual": yesterday_actual,
        "yesterday_boosted": yesterday_boosted,
        "yesterday_natural": yesterday_natural,
        "need": need,
        "deduction": today_deduction,
        "overflow_flag_count": len(flagged_days),
        "stats_days": len(stats),
        "program_start": program_start,
        "startup_applied": startup_applied,
        "baseline_mode": "manual" if manual_baseline else "auto",
        "boost_mode": boost_mode,
        "growth_factor": growth_factor,
        "growth_rate": growth_rate,
        "target_multiplier": round(today_multiplier, 4),
        "today_target": round(today_expected, 1),
        "volatility": volatility,
        "in_cooldown": in_cooldown,
        "cooldown_days": cooldown_days,
        "boost_list_status": list_status,
        "boost_list_amount": list_amount,
        "boost_list_remaining": list_remaining,
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

    # 8.5 列表模式：成功提升后消费掉今天使用的行（“刷完一行删一行”）。
    #     dry_run / 未开启提升 / 提升完全失败时保留该行，明天继续用。
    if list_status == "active" and boost_enabled and need > 0:
        if dry_run:
            logger.info(
                "dry_run 模式：不消费列表行（首行 %d 保留）。", list_amount,
            )
        elif summary["boosted"] > 0:
            try:
                consumed = consume_next(boost_list_file)
            except (OSError, ValueError) as exc:
                logger.error(
                    "消费列表行失败（今日目标 %d）: %s", list_amount, exc,
                )
                summary["boost_list_status"] = "consume_error"
            else:
                list_remaining = max(0, list_remaining - 1)
                summary["boost_list_consumed"] = consumed
                summary["boost_list_remaining"] = list_remaining
                logger.info(
                    "列表已消费一行: %d（剩余 %d 行）",
                    consumed, list_remaining,
                )
        else:
            logger.warning(
                "今日提升成功 0 次，列表首行 %d 保留到下次。", list_amount,
            )

    # 9. 发送每日简报
    warning = (
        summary["overflow_flag_count"] > 0
        or summary["in_cooldown"]
        or getattr(booster, "aborted", False)
        or "executor_error" in summary
        or summary["boost_list_status"] in ("missing", "error", "consume_error")
    )
    level = "warning" if warning else "info"
    notifier.send("每日简报", _format_summary(summary, dry_run), level=level)

    # 10. 记录当日已执行：daemon 重启后据此避免重复执行同一天；
    #     手动 run 与 daemon 共用同一状态键，谁先跑都算今天已完成。
    storage.save_state("last_run_date", today_iso)

    return summary
