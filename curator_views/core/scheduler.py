"""每日调度：抓取统计数据、预测、计算提升需求并执行。

完整流程（每天 10:00 UTC+8 由外部 cron / 计划任务触发）：
1. 抓取并入库 Steam 鉴赏家 g_StatsAppViews 数据（最新一条即昨日）；
2. 以程序启动日为界过滤历史，只使用启动日之后的数据做预测；
3. 逐日预测期望浏览量，检测异常溢出并计算后续扣减；
4. 计算今日缺口 = 今日期望 - 昨日自然流量 - 溢出扣减
   （昨日自然流量 = 昨日实际 - 昨日刷量，避免把昨天的补量当成今天的基础）；
   再按目标上下界把“今日总刷量配额”夹进
   [target_min - 昨日自然, target_max - 昨日自然]（absolute hard limits）；
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
from .daytime import (
    DEFAULT_DAY_BOUNDARY_MINUTES,
    day_label_from_ts,
    format_day_boundary,
    logical_date,
    resolve_day_boundary,
)
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


def _today_iso(
    boundary_minutes: int = DEFAULT_DAY_BOUNDARY_MINUTES,
    now: Optional[datetime] = None,
) -> str:
    """返回 Asia/Shanghai 时区「今天」的逻辑日期 (YYYY-MM-DD)。

    逻辑日由 ``day_boundary`` 决定：默认分界点为 08:00（与 Steam 统计对齐），
    因此凌晨 0:00~7:59 仍属于前一天。传入 ``boundary_minutes=0`` 即恢复
    按自然日切分的旧行为。
    """
    return logical_date(now or datetime.now(_TZ), int(boundary_minutes))


def _today_epoch(
    boundary_minutes: int = DEFAULT_DAY_BOUNDARY_MINUTES,
    now: Optional[datetime] = None,
) -> int:
    """返回逻辑今天 00:00（Asia/Shanghai）的时间戳。"""
    return _date_to_epoch(_today_iso(boundary_minutes, now))


def _date_to_epoch(date_str: str) -> int:
    """把 YYYY-MM-DD 解析为该日期在 Asia/Shanghai 时区 00:00 的时间戳。"""
    year, month, day = (int(part) for part in date_str.split("-"))
    return int(datetime(year, month, day, tzinfo=_TZ).timestamp())


def _resolve_growth_start(
    storage: Storage, growth_rate: float, today_iso: str, persist: bool = True
) -> str:
    """返回当前速率生效的起始日；速率变化时把起点重置为今天。

    锚点持久化在 kv_store 中（键 ``growth_rate`` / ``growth_start``）：
    同一速率跨进程重启仍沿用原起点，保证"每天降 1%"按日历连续推进；
    一旦速率被改动（含从非 0 改为 0），起点重置为今天，避免历史累计
    天数突然生效造成目标跳变。

    ``persist=False`` 用于 dry_run：只按“今天”起算，不写回锚点。
    """
    stored_rate = storage.load_state("growth_rate", default="")
    stored_start = storage.load_state("growth_start", default="")
    same_rate = False
    try:
        same_rate = abs(float(stored_rate) - growth_rate) < 1e-9
    except (TypeError, ValueError):
        same_rate = False
    if not same_rate or not stored_start:
        if persist:
            storage.save_state("growth_rate", f"{growth_rate:.12g}")
            storage.save_state("growth_start", today_iso)
        return today_iso
    try:
        _date_to_epoch(stored_start)
    except (ValueError, IndexError):
        if persist:
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


def _normalize_target_bounds(
    target_min: float = 0.0, target_max: float = 0.0
) -> tuple[float, float]:
    """归一化目标上下界：负数按 0（不设界）处理；min > max 视为配置错误。

    ``target_min`` / ``target_max`` 是当日总量的硬边界，倒置意味着配置笔误
    （例如把上限 5000 写进了 target_min），绝不能静默调换——调换会把上限
    变成下限，实际总量可能翻倍。因此同时 > 0 且倒置时直接抛出 ``ValueError``，
    由调用方拒绝执行。
    """
    low = max(0.0, float(target_min or 0))
    high = max(0.0, float(target_max or 0))
    if low > 0 and high > 0 and low > high:
        raise ValueError(
            f"配置错误: target_min ({low:g}) 大于 target_max ({high:g})；"
            "target_min/target_max 是当日总量的硬边界，请修正配置后重试"
            "（要求 target_min <= target_max）。"
        )
    return low, high


_JITTER_STATE_KEY = "daily_jitter"


def _jitter_bounds(max_volatility: float, min_volatility: float = 0.0) -> tuple[float, float]:
    """归一化抖动区间，返回 ``(下限, 上限)``。

    - 负数按绝对值处理，非数按 0；
    - 上限为 0 时视为关闭抖动，返回 ``(0, 0)``；
    - ``min_volatility`` 大于上限时夹到上限（等价于固定幅度），
      避免配错方向后抖动被静默关闭。
    """
    high = abs(float(max_volatility or 0))
    low = abs(float(min_volatility or 0))
    if high <= 0:
        return 0.0, 0.0
    return min(low, high), high


def _sample_signed_jitter(max_volatility: float, min_volatility: float = 0.0) -> float:
    """抽取带符号抖动，保证 ``min <= |jitter| <= max``。

    ``min_volatility`` 用于防止“抽到接近 0 的抖动导致曲线过于平缓”：
    幅度在 ``[min, max]`` 内均匀取值，方向随机（各 50%）。
    上限为 0 时不抖动，返回 0。
    """
    low, high = _jitter_bounds(max_volatility, min_volatility)
    if high <= 0:
        return 0.0
    magnitude = random.uniform(low, high)
    sign = 1.0 if random.random() < 0.5 else -1.0
    return sign * magnitude


def _jitter_signature(
    baseline_views: float,
    boost_mode: str,
    growth_factor: float,
    growth_rate: float,
    target_min: float,
    target_max: float,
    volatility: float,
    volatility_min: float,
    target_volatility: float,
    target_volatility_min: float,
) -> str:
    """生成当日抖动配置签名：配置未变时同一天复用同一组抖动值。"""
    return json.dumps(
        [
            round(float(baseline_views), 6),
            str(boost_mode),
            round(float(growth_factor), 6),
            round(float(growth_rate), 6),
            round(float(target_min), 6),
            round(float(target_max), 6),
            round(float(volatility), 6),
            round(float(volatility_min), 6),
            round(float(target_volatility), 6),
            round(float(target_volatility_min), 6),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _resolve_daily_jitters(
    storage: Storage,
    today_iso: str,
    signature: str,
    target_high: float,
    target_low: float,
    gap_high: float,
    gap_low: float,
    persist: bool = True,
) -> tuple[float, float]:
    """取当日目标抖动与缺口抖动，返回 ``(target_jitter, gap_jitter)``。

    抖动值按“日期 + 配置签名”持久化在 ``kv_store`` 的 ``daily_jitter`` 键：

    - 同一天多次 run / daemon 多时段复用同一组值，避免目标在时段之间跳变，
      也避免缺口抖动被后续时段“补平”；
    - 改动任一波动相关配置后签名变化，从当天重新抽取；
    - ``persist=False``（dry_run）只读已有值，不写回；没有值时临时抽取。
    """
    if target_high <= 0 and gap_high <= 0:
        return 0.0, 0.0

    raw = storage.load_state(_JITTER_STATE_KEY, default="")
    if raw:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("date") == today_iso
            and payload.get("signature") == signature
        ):
            try:
                return float(payload["target"]), float(payload["gap"])
            except (KeyError, TypeError, ValueError):
                pass

    target_jitter = (
        _sample_signed_jitter(target_high, target_low) if target_high > 0 else 0.0
    )
    gap_jitter = _sample_signed_jitter(gap_high, gap_low) if gap_high > 0 else 0.0
    if persist:
        storage.save_state(
            _JITTER_STATE_KEY,
            json.dumps(
                {
                    "date": today_iso,
                    "signature": signature,
                    "target": target_jitter,
                    "gap": gap_jitter,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    return target_jitter, gap_jitter


def _clamp_target(
    target: float, target_min: float = 0.0, target_max: float = 0.0
) -> tuple[float, str]:
    """把今日目标夹到 ``[target_min, target_max]``；0 表示该方向不设界。

    - ``target_min`` > 0 且目标低于下限时抬到下限（托底，防目标一路下滑）；
    - ``target_max`` > 0 且目标高于上限时压到上限（封顶，防 growth 无限上涨）；
    - 两者都为 0 时不设界，原样返回；
    - 两者都 > 0 且 ``target_min > target_max`` 属于配置错误，由
      ``_normalize_target_bounds`` 抛出 ``ValueError``，不会静默夹成矛盾值。

    Returns:
        ``(夹取后的目标, 命中的边界)``，边界为 ``"min"`` / ``"max"`` / ``""``。
    """
    low, high = _normalize_target_bounds(target_min, target_max)
    if low > 0 and target < low:
        return low, "min"
    if high > 0 and target > high:
        return high, "max"
    return target, ""


def _floor_boost(natural: float, target_min: float) -> int:
    """返回达到 ``target_min`` 所需的最少刷量；``target_min`` 为 0 时返回 0。

    ``floor_boost = max(0, target_min - natural)``：自然流量本身已达标时为 0，
    不会额外刷量。``target_min <= 0`` 表示该方向不设界。
    """
    if target_min <= 0:
        return 0
    return max(0, int(round(target_min - natural)))


def _ceiling_boost(natural: float, target_max: float) -> int:
    """返回 ``target_max`` 允许的最大刷量；``target_max`` 为 0 时返回 0。

    ``ceiling_boost = max(0, target_max - natural)``：自然流量已超过上限时为 0，
    此时只能不刷，自然流量本身无法被压低。``target_max <= 0`` 表示该方向不设界。
    """
    if target_max <= 0:
        return 0
    return max(0, int(round(target_max - natural)))


def _bound_boost(
    need: int, natural: float, floor: float, ceiling: float
) -> tuple[int, str]:
    """把“实际刷量”夹进 ``[floor-natural, ceiling-natural]``，返回 (need, 命中边界)。

    - 先夹上限、再夹下限；``bound_hit`` 为 ``""`` / ``"min"`` / ``"max"``；
    - 下限只在传入的 ``need > 0`` 时生效，避免把“今天不刷”的空状态强行托底；
    - 上限为 0（不设界）时不夹上限；下限同理；
    - 若夹取后出现 ``floor_boost > ceiling_boost``（配置矛盾），以安全方向
      的上限为准并记录警告。
    """
    original = max(0, int(round(need)))
    if original <= 0:
        return 0, ""

    floor_boost = _floor_boost(natural, floor)
    ceiling_boost = _ceiling_boost(natural, ceiling)
    bounded = original
    bound_hit = ""

    if ceiling > 0 and bounded > ceiling_boost:
        bounded = ceiling_boost
        bound_hit = "max"

    # 上限已把刷量夹到 0 时不再托底：自然流量已越过上限，压低无从谈起。
    if bounded > 0 and floor > 0 and bounded < floor_boost:
        if ceiling > 0 and floor_boost > ceiling_boost:
            logger.warning(
                "配置矛盾：target_min=%g 对应的刷量 %d 超过 target_max=%g "
                "对应的刷量 %d，已按上限执行（安全方向）。",
                floor, floor_boost, ceiling, ceiling_boost,
            )
            bounded = ceiling_boost
            bound_hit = "max"
        else:
            bounded = floor_boost
            bound_hit = "min"

    return int(bounded), bound_hit


def _effective_threads(
    threads: int,
    app_count: Optional[int] = None,
    batch_size: Optional[int] = None,
) -> int:
    """返回 Booster 实际可同时访问的 App 数。

    ``threads`` 没有内置上限，但并发数不会超过推荐数（boost_n）与批次大小
    （batch_size）；并行批次里只有 ``len(active)`` 个任务，线程再多也不会超过
    这个数。
    """
    limit = max(1, int(threads))
    if app_count is not None:
        limit = min(limit, max(1, int(app_count)))
    if batch_size is not None:
        limit = min(limit, max(1, int(batch_size)))
    return max(1, limit)


def _daily_throughput_capacity(
    threads: int,
    wait_min: float,
    wait_max: float,
    app_count: Optional[int] = None,
    batch_size: Optional[int] = None,
) -> Optional[int]:
    """估算单日最多能发多少次请求（乐观上限，未计请求耗时）。

    Booster 每次访问后随机等待 ``wait_min`` ~ ``wait_max`` 秒，并发线程各自
    独立等待；因此单日请求上限约为::

        实际并发 × 86400 / max(wait_min, wait_max)

    ``wait_min`` 大于 ``wait_max`` 时，``random.uniform`` 的实际最大值仍是
    较大者。等待上限 <= 0 表示不等待，返回 ``None``（不构成吞吐瓶颈）。
    """
    interval = max(0.0, float(wait_min), float(wait_max))
    if interval <= 0:
        return None
    return int(
        _effective_threads(threads, app_count, batch_size) * 86400 / interval
    )


def _format_summary(summary: Dict[str, Any], dry_run: bool) -> str:
    """把汇总字典格式化为每日简报正文。"""
    mode = "（演练模式，未实际提升）" if dry_run else ""
    lines = [
        f"日期: {summary['date']}",
    ]
    if summary.get("target_bound"):
        bound_label = "下限" if summary["target_bound"] == "min" else "上限"
        raw = summary.get(
            "today_expected_jittered", summary["today_expected"]
        )
        lines.append(
            f"目标{bound_label}夹取: {summary['today_expected']:.0f}"
            f"（原始 {raw:.0f}，下限={summary.get('target_min', 0):g}，"
            f"上限={summary.get('target_max', 0):g}）"
        )
    if summary.get("target_jitter"):
        lines.append(
            f"目标抖动: {summary['target_jitter']:+.2%}"
            f"（{summary.get('today_expected_raw', 0):.0f} → "
            f"{summary.get('today_expected_jittered', 0):.0f}）"
        )
    if summary.get("gap_jitter"):
        lines.append(
            f"缺口抖动: {summary['gap_jitter']:+.2%}"
            f"（当日配额 {summary.get('boost_quota', 0):,}）"
        )
    lines.extend([
        f"今日预期浏览量: {summary['today_expected']:.0f}",
        f"昨日实际浏览量: {summary['yesterday_actual']}",
        f"昨日刷量: {summary.get('yesterday_boosted', 0)}",
        f"昨日自然流量: {summary.get('yesterday_natural', summary['yesterday_actual'])}",
        f"今日已刷: {summary.get('today_boosted', 0)}",
        f"今日缺口: {summary['need']}",
        f"溢出扣减: {summary['deduction']:.1f}",
        f"溢出异常天数: {summary['overflow_flag_count']}",
        f"统计数据天数: {summary['stats_days']}",
        f"程序启动日: {summary['program_start']}",
        f"启动期加权: {'是' if summary['startup_applied'] else '否'}",
    ])
    list_status = summary.get("boost_list_status", "disabled")
    if list_status != "disabled":
        labels = {
            "active": "使用中",
            "empty": "已用完",
            "missing": "文件缺失",
            "error": "读取失败",
            "cooldown": "恢复期暂停",
            "consume_error": "消费失败",
            "already_boosted": "今日已刷",
            "fallback": "已回退基线",
        }
        lines.append(
            f"刷量列表: {labels.get(list_status, list_status)}"
            f"（今日 {summary.get('boost_list_amount') or 0}，"
            f"剩余 {summary.get('boost_list_remaining', 0)} 行）"
        )
    throughput = summary.get("throughput_capacity")
    if throughput is not None:
        wait_min = summary.get("wait_min", 8.0)
        wait_max = summary.get("wait_max", 16.0)
        lines.append(
            f"单日吞吐上限: {throughput:,} 次"
            f"（threads={summary.get('threads', 1)}，"
            f"最大间隔={max(wait_min, wait_max):g}s）"
        )
    if summary.get("capacity_warning"):
        lines.append(f"注意: {summary['capacity_warning']}")
    lines.append(f"实际提升: {summary['boosted']} {mode}")
    return "\n".join(lines)


def _last_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从执行器 stdout 中提取最后一个合法的 JSON 对象。

    执行器可能把结果打印成单行 JSON，也可能用 ``indent=2`` 打印成多行，
    还可能在 JSON 前后夹带日志。这里用 ``raw_decode`` 逐个 ``{`` 尝试解析，
    解析成功后跳过该对象内部（避免把嵌套 dict 当成候选），保留最后出现的
    最外层对象。没有可解析对象时返回 ``None``。
    """
    decoder = json.JSONDecoder()
    last: Optional[Dict[str, Any]] = None
    index = text.find("{")
    while index != -1:
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(obj, dict):
            last = obj
        index = end
    return last


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
    执行器应以 JSON 输出 ``{"boosted": N, ...}``；单行、缩进多行以及
    前后夹带日志都可以，调度器取 stdout 中最后一个 JSON 对象。

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
    payload = _last_json_object(proc.stdout)
    if payload is None:
        logger.warning(
            "无法解析执行器 JSON 输出，按 0 处理。输出尾部: %s",
            proc.stdout.strip()[-200:],
        )
    else:
        try:
            boosted = int(payload.get("boosted", 0))
        except (TypeError, ValueError):
            logger.warning(
                "执行器 JSON 的 boosted 字段无效（%r），按 0 处理。",
                payload.get("boosted"),
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
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """执行一次每日流程，返回汇总字典供 CLI / 上报使用。

    config 支持的键：
        window_days, trend_cap, startup_factor, trend_min_points,
        outlier_k, consecutive_days, absorb_days,
        boost_n, single_cap, batch_size, target_ratio, boost_distribution,
        threads, wait_min, wait_max,
        baseline_views, boost_mode, growth_factor,
        target_min, target_max,
        volatility, volatility_min,
        target_volatility, target_volatility_min,
        cooldown_days, exclude_flagged,
        baseline_days, slope_days, slope_threshold,
        boost_enabled (bool), dry_run (bool),
        boost_list_file (str), boost_list_fallback (bool),
        day_boundary / day_boundary_minutes（每日分界点）
    其中 single_cap / batch_size / target_ratio / boost_distribution 会应用到
    传入的 Booster 实例（以 config 为准），其余作为预测/检测参数直接传入。

    baseline_views：
        大于 0 时启用手动基线模式：基准目标直接用该固定值，替代自动预测
        （中位数+趋势）。溢出检测的参照也使用该固定值，天然免疫被污染。
        为 0 或未配置时使用自动预测。

    day_boundary / day_boundary_minutes：
        Steam 统计的每日分界点（Asia/Shanghai）。Steam 后台通常在北京时间
        早上 8 点（UTC 0 点）切换“新的一天”，因此 0:00~8:00 之间执行的刷量
        会计入前一天。分界点默认 08:00：逻辑今天 = 当前时间减去分界偏移后的
        日期；刷量台账、daily_plan、抖动持久化与 last_run_date 都按逻辑日记账，
        避免把凌晨刷量记到错误的日期。填 ``"00:00"`` 即恢复按自然日切分。
        ``config`` 可传 ``day_boundary``（``"08:00"`` 等字符串）或已解析的
        ``day_boundary_minutes``（0~1439）。

    now：
        可选注入的当前时间（带时区），仅用于测试；为 None 时取系统当前时间。
        同一逻辑日内的判断、jitter 复用与台账日期都以该时间为准。

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

    volatility / volatility_min：
        缺口波动率。>0 时当日刷量配额在「缺口 ×(1 ± volatility)」内随机，
        使浏览曲线围绕目标自然波动。``volatility_min`` 给抖动幅度设下限
        （幅度在 ``[min, max]`` 内均匀取值、方向随机），防止抽到接近 0 的
        抖动导致曲线过于平缓。抖动值按日期持久化：同一天多个时段复用同一
        配额，后续时段只补 ``配额 − 今日已刷``，不会把负向抖动补平。
        不设上限，但幅度过大会挤压反作弊判定余量（见下）。

    target_volatility / target_volatility_min：
        目标波动率。>0 时今日目标在「目标 ×(1 ± target_volatility)」内随机，
        再按 ``target_min`` / ``target_max`` 夹取后参与缺口计算。与缺口波动率
        不同，它按目标的比例抖动，因此即使缺口很小也能让总浏览量产生明显
        起伏。``target_volatility_min`` 同样给抖动幅度设下限。
        目标抖动值按日期持久化：同一天重跑/多时段使用同一个目标。
        两者都无硬性上限；但抖动幅度会同步放大反作弊检测参照
        （``1 + target_volatility + volatility``），避免把自己抖出来的量
        判成恶意刷量。

    target_min / target_max：
        今日目标浏览量的上下界（0 表示该方向不设界）。目标先按
        ``基准 × 目标系数`` 算出，再夹到 ``[target_min, target_max]`` 后参与
        缺口计算：
        - ``target_max`` 防止 growth 模式目标随天数无限上涨（增长不收敛）；
        - ``target_min`` 托底，防止目标/浏览量持续下滑跌破下限。
        检测参照取 ``max(自然预期, 夹取后目标)``：托底时同步抬到下限，避免把
        自己托底刷出来的量误判为恶意刷量；封顶后参照不虚高，超出上限的外部
        异常仍能及时发现。刷量列表模式直接覆盖缺口，不受上下界约束。

    dry_run：
        为 True 时只计算、不产生任何持久化副作用：不写 daily_stats、
        program_start / last_stats_date / last_run_date / growth 锚点，
        也不发送每日简报；缺口与汇总仍照常计算，便于预览今天会刷多少。
        不写 last_run_date 可避免手动 dry-run 让 daemon 误以为当天已执行。

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
        （cooldown）、缺口为 0、提升完全失败。列表为空时今日默认不刷量
        （不会回退到自动预测，避免列表模式被悄悄绕过）；文件缺失属于
        配置错误，简报会标记为 warning。

    boost_list_fallback：
        可选，默认 True，仅在"列表文件存在但已无有效行（empty）"时生效：
        为 True 时不再停刷，而是回退到基线/自动预测口径，沿用上面算出的
        need（已扣除恢复期、今日已刷与上限），相当于列表耗尽后自动继续按
        基线刷量；出现回退且当日确有缺口时，简报升级为 warning 提示已回退。
        设为 False 则恢复"用完即停"。文件缺失（missing）或读取失败（error）
        属于配置/IO 错误，即使开启该开关也不回退，仍按 0 处理并标记 warning，
        避免掩盖配置问题。
    """
    # ── 硬边界校验：必须最先执行 ─────────────────────────────────────────
    # 配置倒置时立即失败，确保不会抓取统计、写库（daily_stats / daily_plan /
    # 抖动状态）、消费刷量列表或发起任何提升请求。
    target_min = float(config.get("target_min", 0) or 0)
    target_max = float(config.get("target_max", 0) or 0)
    target_floor, target_ceiling = _normalize_target_bounds(
        target_min, target_max
    )

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
    volatility_min = float(config.get("volatility_min", 0) or 0)
    target_volatility = float(config.get("target_volatility", 0) or 0)
    target_volatility_min = float(config.get("target_volatility_min", 0) or 0)
    gap_low, gap_high = _jitter_bounds(volatility, volatility_min)
    target_jitter_low, target_jitter_high = _jitter_bounds(
        target_volatility, target_volatility_min
    )
    cooldown_days = int(config.get("cooldown_days", 7))
    exclude_flagged = bool(config.get("exclude_flagged", True))
    baseline_days = int(config.get("baseline_days", 60))
    slope_days = int(config.get("slope_days", 14))
    slope_threshold = float(config.get("slope_threshold", 0.02))
    boost_enabled = bool(config.get("boost_enabled", False))
    dry_run = bool(config.get("dry_run", False))
    boost_list_file = str(config.get("boost_list_file", "") or "").strip()
    boost_list_fallback = bool(config.get("boost_list_fallback", True))
    threads = int(config.get("threads", getattr(booster, "threads", 1)))
    wait_min = float(config.get("wait_min", getattr(booster, "wait_min", 8.0)))
    wait_max = float(config.get("wait_max", getattr(booster, "wait_max", 16.0)))
    boundary_minutes = resolve_day_boundary(config)

    # Booster 的执行参数以 config 为准（调用方构建实例时可先给默认值）
    batch_size = int(config.get("batch_size", booster.batch_size))
    booster.batch_size = batch_size
    booster.single_cap = int(config.get("single_cap", booster.single_cap))
    booster.target_ratio = float(config.get("target_ratio", booster.target_ratio))
    booster.distribution = str(
        config.get("boost_distribution", "ratio")
    ).lower()
    # 刷量台账的日期必须与调度器使用同一个逻辑日，否则凌晨刷量会记错日期。
    booster.day_boundary_minutes = boundary_minutes

    now_dt = now or datetime.now(_TZ)
    today_iso = _today_iso(boundary_minutes, now_dt)
    today_ts = _date_to_epoch(today_iso)

    # 1. 抓取统计数据并入库（dry_run 只计算，不落库）
    stats = fetcher.fetch_stats()
    if not dry_run:
        for stat in stats:
            storage.upsert_daily_stats(stat["date_ts"], stat["num_views"])
    last_date_iso = ""
    if stats:
        last_ts = stats[-1]["date_ts"]
        last_date_iso = day_label_from_ts(last_ts)
    if not dry_run:
        storage.save_state(
            "last_stats_date",
            json.dumps({"last_date": last_date_iso, "days": len(stats)}),
        )

    # 2. 程序启动日：首次运行记录今天，之后只使用启动日之后的历史
    program_start = storage.load_state("program_start", default="")
    if not program_start:
        program_start = today_iso
        if not dry_run:
            storage.save_state("program_start", program_start)
    program_start_epoch = _date_to_epoch(program_start)
    if program_start_epoch > today_ts:
        # 调整 day_boundary 后，旧的自然日启动日可能“晚于”当前逻辑日。
        # 放宽到昨天零点，避免一行历史都被过滤掉；不覆盖已存的值。
        logger.warning(
            "program_start=%s 晚于当前逻辑日 %s（可能修改过 day_boundary），"
            "本次按昨天起算历史。",
            program_start, today_iso,
        )
        program_start_epoch = today_ts - 86400
    history_sorted = sorted(
        (d for d in stats if d["date_ts"] >= program_start_epoch),
        key=lambda d: d["date_ts"],
    )
    # Steam 当天统计尚未完整：只让“日期标签早于逻辑今天”的完整日参与
    # 预测/检测/昨日基准；标签等于逻辑今天的记录（若 Steam 已返回）剔除，
    # 避免把进行中的半天数据当成完整一天。
    history_complete = [
        d for d in history_sorted if day_label_from_ts(d["date_ts"]) < today_iso
    ]
    logger.info(
        "程序启动日=%s，逻辑今天=%s（分界点 %s），完整历史数据 %d 天"
        "（启动后 %d 天，共 %d 天）。",
        program_start, today_iso, format_day_boundary(boundary_minutes),
        len(history_complete), len(history_sorted), len(stats),
    )

    # 3. 逐日预测 + 异常溢出检测。
    #    被标记为恶意刷量的日期不参与预测（剔除出中位数窗口），防止虚高
    #    数据污染基线；标记集单调累加（只增不减），无新标记即收敛，
    #    至多迭代 3 轮。
    #    目标 = 基准 × 倍率 × 速率因子：stable 模式倍率为 1（维持稳定）；
    #    growth 模式倍率为 growth_factor，速率因子按 growth_rate 线性推进。
    #    检测参照：增长时用抬高后的目标（避免把自己刷的量误判为恶意刷量），
    #    下调时用自然预期（避免"实际高于下滑目标"被误判为异常）。
    #    自身抖动会让实际值进一步偏离目标，参照按最大抖动幅度同步放大。
    manual_baseline = baseline_views > 0
    multiplier = growth_factor if boost_mode == "growth" else 1.0
    jitter_uplift = 1.0 + target_jitter_high + gap_high
    drift_start_ts = 0
    if boost_mode == "growth" and growth_rate != 0:
        drift_start_ts = _date_to_epoch(
            _resolve_growth_start(
                storage, growth_rate, today_iso, persist=not dry_run
            )
        )
    confirmed_flagged: set[int] = set()
    history_clean = history_complete
    overflow: list[dict] = []
    first_overflow: list[dict] = []
    max_rounds = 3 if exclude_flagged else 1
    for _ in range(max_rounds):
        history_clean = [
            d for d in history_complete if d["date_ts"] not in confirmed_flagged
        ]
        # 长期基线（更长窗口的中位数）：即使短期预期被污染，仍是独立参照。
        # 配置了目标下限时，长期基线不能低于下限：我们是主动把曲线托到
        # target_min，若仍拿历史低位当基线，连续托底会被误判成恶意刷量。
        # 抬到下限后，低于下限的正常托底不算异常，高于下限 ×(1+k) 的外部
        # 尖峰仍会被抓到（漂移检测的高位日阈值也同步抬高）。
        long_baseline: float | None = None
        if baseline_days > 0 and history_clean:
            base_window = history_clean[-min(len(history_clean), baseline_days):]
            long_baseline = median(d["num_views"] for d in base_window)
            if target_floor > 0:
                long_baseline = max(long_baseline, target_floor)
        # 持续上升斜率检测：慢性爬升型刷量（需要长期基线参照，防止
        # 把真实增长误判为异常）。检测用基线同样按抖动幅度放大，
        # 避免自己抖出来的高位日被当成慢性爬升。
        detection_baseline = (
            long_baseline * jitter_uplift if long_baseline is not None else None
        )
        drift_days = detect_drift(
            history_clean,
            slope_days=slope_days,
            slope_threshold=slope_threshold,
            long_baseline=detection_baseline,
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
            # 检测参照：取自然预期 base 与夹取后目标的较大值。
            # - 增长时用抬高后的目标，避免把自己刷的量误判为异常；
            # - 下滑时不把"实际高于下滑目标"误判为异常（参照仍为 base）；
            # - 托底时参照同步抬到下限，避免托底刷量被误判；
            # - 封顶后参照不再虚高，超出上限的外部异常仍能及时发现。
            # - 最后按 jitter_uplift 放大，为自身抖动留出判定余量。
            day_target, _ = _clamp_target(
                base * day_multiplier, target_floor, target_ceiling
            )
            predictions[day["date_ts"]] = max(base, day_target) * jitter_uplift
        overflow = detect_overflow(
            history_clean, predictions, outlier_k, consecutive_days,
            long_baseline=detection_baseline,
            extra_candidates=drift_days,
        )
        if not first_overflow:
            first_overflow = overflow
        new_flagged = {d["date_ts"] for d in overflow if d.get("flagged")}
        if not new_flagged:
            break
        confirmed_flagged |= new_flagged

    # 4. 昨日实际（最近一个完整统计日）与今日期望。
    #    注意：昨日实际里包含昨天我们自己刷的量，而今天刷的量只计入今天。
    #    若直接拿"昨日实际"当今天的自然流量基准，等于把昨天的补量重复扣
    #    一次，会造成目标永远补不满、并在目标附近来回补/不补（两天一摆）。
    #    因此从 boost_log 取回昨日刷量，还原"昨日自然流量"作为今日基准。
    #    history_clean 已剔除逻辑今天，因此最后一条就是逻辑昨天（若存在）。
    yesterday_actual = 0
    yesterday_boosted = 0
    if history_clean:
        last_stat = history_clean[-1]
        yesterday_actual = int(last_stat["num_views"])
        last_stat_date = day_label_from_ts(int(last_stat["date_ts"]))
        yesterday_boosted = max(
            0, int(storage.get_boost_total(last_stat_date))
        )
    # 台账缺失（旧数据 / 子进程执行器写了别的库）时退化为旧行为（按 0 处理）；
    # 刷量记录大于实际值时夹到 0，避免自然流量算成负数导致超额补量。
    yesterday_natural = max(0, yesterday_actual - yesterday_boosted)
    # Steam 当天统计尚未完整：今日期望只基于完整历史（已剔除逻辑今天）。
    history_without_today = history_clean
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
    today_expected_raw = base_expected * today_multiplier
    # 目标层抖动：按当日配置签名抽取并持久化，同一天重跑/多时段复用同一值。
    # 签名与 forecast 首日复用共用同一口径（归一化后的上下界）。
    jitter_signature = _jitter_signature(
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
    )
    target_jitter, gap_jitter = _resolve_daily_jitters(
        storage,
        today_iso,
        jitter_signature,
        target_jitter_high,
        target_jitter_low,
        gap_high,
        gap_low,
        persist=not dry_run,
    )
    today_expected_jittered = today_expected_raw
    if target_jitter:
        today_expected_jittered = max(
            0.0, today_expected_raw * (1.0 + target_jitter)
        )
        logger.info(
            "目标抖动 %+.2f%%（最小幅度 %.2f%%）: %.1f → %.1f",
            target_jitter * 100.0,
            target_jitter_low * 100.0,
            today_expected_raw,
            today_expected_jittered,
        )
    # 目标上下界：0 表示该方向不设界。上限防 growth 目标无限上涨，
    # 下限托底防目标持续下滑；夹取后的目标再参与缺口计算。
    today_expected, target_bound = _clamp_target(
        today_expected_jittered, target_floor, target_ceiling
    )
    if target_bound:
        bound_label = "下限" if target_bound == "min" else "上限"
        logger.info(
            "今日目标 %.1f 命中%s，按 %.1f 计算缺口"
            "（target_min=%g，target_max=%g）。",
            today_expected_jittered, bound_label, today_expected,
            target_floor, target_ceiling,
        )

    # 5. 溢出扣减：只对被标记日产生的溢出量分摊到后续日期。
    #    检测事实以第一轮结果为准（攻击发生与否不随剔除而改变）；
    #    剔除仅用于净化预测窗口。
    flagged_days = [d for d in first_overflow if d.get("flagged")]
    flagged_ts_first = {d["date_ts"] for d in flagged_days}
    deductions = compute_deductions(flagged_days, absorb_days)
    # 扣减表按被标记日的 date_ts + 86400×N 生成；Steam 的 date_ts 可能不在
    # 本地零点（真实数据是 UTC 0 点 = 北京时间 08:00），因此按日期标签匹配，
    # 不直接比较时间戳，避免凌晨时段整体错开 8 小时导致扣减丢失。
    today_deduction = float(
        {
            day_label_from_ts(ts): amount
            for ts, amount in deductions.items()
        }.get(today_iso, 0.0)
    )

    # 6. 今日缺口 = 期望 - 昨日自然流量 - 溢出扣减 - 今日已刷。
    #    自然流量已剔除昨日自己刷的量，补多少就只补"自然流量与目标之间"
    #    的真实差额，不会把昨天的补量当成今天已有的基础重复扣减。
    #    今日已刷（boost_log 里今天的累计）同样要扣：中断、容量不足或手动重跑
    #    时按剩余缺口续刷，而不是把整份缺口再刷一遍。
    #    恢复期（最近存在被标记的恶意刷量日）内强制不刷，等待曲线自然回落；
    #    波动率 > 0 时在缺口附近注入随机抖动，使曲线围绕基线上下波动。
    gross_need = max(
        0, round(today_expected - yesterday_natural - today_deduction)
    )
    today_boosted = max(0, int(storage.get_boost_total(today_iso)))
    # 缺口抖动作用于“当日总量配额”，而不是每次剩余的缺口：
    # 多时段/同日重跑时 later run 只补「配额 − 今日已刷」，
    # 负向抖动不会被后续时段补平，正负抖动都能体现在当日总量上。
    boost_quota = gross_need
    if gap_high > 0 and gross_need > 0:
        boost_quota = max(0, round(gross_need * (1.0 + gap_jitter)))
        logger.info(
            "缺口抖动 %+.2f%%（最小幅度 %.2f%%）: 缺口 %d → 当日配额 %d",
            gap_jitter * 100.0, gap_low * 100.0, gross_need, boost_quota,
        )

    # 6.3 恢复期判定放在边界夹取之前：恢复期内不刷量，不应对配额做下限托底。
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

    # 6.35 目标上下界是刷量的绝对硬边界：把“今日总刷量配额”夹进
    #      [target_min − 昨日自然, target_max − 昨日自然]。
    #      - 先夹配额再减“今日已刷”，保证同日续刷时“已刷 + 剩余”仍不越界；
    #      - 无缺口（无提升状态）时不托底，避免凭空制造刷量；
    #      - 恢复期 / boost_enabled=False 只夹上限，不做下限托底。
    floor_boost = _floor_boost(yesterday_natural, target_floor)
    ceiling_boost = _ceiling_boost(yesterday_natural, target_ceiling)
    can_boost = boost_enabled and not in_cooldown
    floor_for_bound = target_floor if can_boost else 0.0
    need_bound = ""
    if gross_need > 0:
        bounded_quota, quota_bound = _bound_boost(
            boost_quota, yesterday_natural, floor_for_bound, target_ceiling
        )
        if quota_bound:
            bound_label = "下限" if quota_bound == "min" else "上限"
            logger.info(
                "波动后配额 %d 命中目标%s对应的刷量边界，按 %d 执行"
                "（昨日自然 %d，target_min=%g，target_max=%g）。",
                boost_quota, bound_label, bounded_quota,
                yesterday_natural, target_floor, target_ceiling,
            )
        boost_quota = bounded_quota
        need_bound = quota_bound
    if target_ceiling > 0 and yesterday_natural > target_ceiling:
        logger.warning(
            "昨日自然流量 %d 已超过 target_max=%g，自然流量无法被压低，"
            "今日刷量强制为 0；当日总量仍会突破上限（超出部分是自然流量本身）。",
            yesterday_natural, target_ceiling,
        )
    need = max(0, boost_quota - today_boosted)
    if today_boosted > 0:
        logger.info(
            "今日已刷 %d 次，配额 %d → %d（按剩余量继续）。",
            today_boosted, boost_quota, need,
        )
    if in_cooldown:
        need = 0
        need_bound = ""

    # 6.5 每日刷量列表模式：以列表首行数量覆盖上面算出的缺口。
    #     - 列表为空：boost_list_fallback=False → 今日不刷，且不回退
    #       到自动预测（避免绕过列表）；=True（默认）→ 回退基线/自动预测，
    #       沿用上面算出的 need（视为列表已耗尽、开始按基线继续）；
    #     - 文件缺失/读取失败属于配置/IO 错误，始终不刷并记 warning，即使
    #       开启 boost_list_fallback 也不回退（避免掩盖配置问题）；
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
            need_bound = ""
        except OSError as exc:
            list_status = "error"
            logger.error("读取刷量列表失败: %s，今日不刷量。", exc)
            need = 0
            need_bound = ""
        else:
            if list_amount is None:
                if boost_list_fallback:
                    # 列表耗尽（empty）才回退；保留第 6 步已算好的自动缺口
                    # （已扣除恢复期、今日已刷与上下界），相当于切回基线模式。
                    list_status = "fallback"
                    logger.warning(
                        "刷量列表已用完，已回退基线模式（今日按自动缺口 %d 执行）。",
                        need,
                    )
                else:
                    list_status = "empty"
                    logger.info("刷量列表已用完（无有效行），今日不刷量。")
                    need = 0
                    need_bound = ""
            elif in_cooldown:
                list_status = "cooldown"
                logger.info(
                    "恢复期内不刷量，列表首行 %d 保留到明天。", list_amount,
                )
                need = 0
                need_bound = ""
            elif today_boosted > 0:
                # 列表一行代表“当天总量”，重跑时不按剩余量续刷：
                # 否则成功后会误消费下一行，把明天的配额提前用掉。
                list_status = "already_boosted"
                logger.info(
                    "今日已刷 %d 次，列表首行 %d 保留到明天"
                    "（列表模式不续刷，避免误消费下一行）。",
                    today_boosted, list_amount,
                )
                need = 0
                need_bound = ""
            else:
                list_status = "active"
                logger.info(
                    "列表模式: 今日目标 %d（剩余 %d 行，原缺口 %d）",
                    list_amount, list_remaining, need,
                )
                original_list_amount = list_amount
                need, need_bound = _bound_boost(
                    list_amount,
                    yesterday_natural,
                    floor_for_bound,
                    target_ceiling,
                )
                if need_bound:
                    bound_label = "下限" if need_bound == "min" else "上限"
                    logger.warning(
                        "刷量列表首行 %d 超出目标%s对应的刷量边界，"
                        "按 %d 执行（昨日自然 %d，target_min=%g，target_max=%g）。",
                        original_list_amount, bound_label, need,
                        yesterday_natural, target_floor, target_ceiling,
                    )

    # 6.55 定档：把今日最终决策写入 daily_plan，供 forecast 首日复用，
    #      避免 forecast 重新抽取 target/gap 抖动、显示与 run 矛盾的缺口。
    #      同一天重跑/多时段会覆盖同一行；dry_run 不写，保持无副作用。
    if not dry_run:
        storage.save_daily_plan(
            {
                "date": today_iso,
                "date_ts": today_ts,
                "base": base_expected,
                "multiplier": today_multiplier,
                "raw_target": today_expected_raw,
                "target": today_expected,
                "target_jitter": target_jitter,
                "gap_jitter": gap_jitter,
                "deduction": today_deduction,
                "gross_need": gross_need,
                "boost_quota": boost_quota,
                "need": need,
                "today_boosted": today_boosted,
                "target_bound": target_bound,
                "in_cooldown": in_cooldown,
                "list_status": list_status,
                "list_amount": list_amount,
                "signature": jitter_signature,
                "updated_at": now_dt.isoformat(timespec="seconds"),
            }
        )

    # 6.6 吞吐能力检查：threads 与等待间隔共同决定一天最多能发多少请求。
    #     这是乐观上限（未计请求耗时）；缺口超过它时一天肯定跑不满。
    throughput_capacity = _daily_throughput_capacity(
        threads, wait_min, wait_max, boost_n, batch_size
    )
    capacity_warning = ""
    if (
        boost_enabled
        and throughput_capacity is not None
        and need > throughput_capacity
    ):
        capacity_warning = (
            f"今日缺口 {need:,} 超过单日吞吐上限 {throughput_capacity:,} 次"
            f"（threads={threads}，实际并发≤"
            f"{_effective_threads(threads, boost_n, batch_size)}，"
            f"最大间隔={max(wait_min, wait_max):g}s，未计请求耗时）"
        )
        logger.warning(
            "%s；建议提高 threads 或降低 wait_min/wait_max，"
            "否则一天跑不满。", capacity_warning,
        )

    # 单轮容量（boost_n × single_cap）限制：缺口更大时本轮必然刷不满，
    # 稍后再次 run 会按“今日已刷”扣减后继续补剩余部分。
    run_capacity = max(0, boost_n) * max(0, int(booster.single_cap))
    if boost_enabled and need > run_capacity:
        message = (
            f"今日缺口 {need:,} 超过单轮容量 {run_capacity:,}"
            f"（boost_n={boost_n} × single_cap={booster.single_cap}），"
            "本轮跑不满；稍后再次 run 会按剩余缺口续刷。"
        )
        capacity_warning = (
            f"{capacity_warning}；{message}" if capacity_warning else message
        )
        logger.warning(message)

    # 7. 汇总字典
    summary: Dict[str, Any] = {
        "date": today_iso,
        "day_boundary": format_day_boundary(boundary_minutes),
        "dry_run": dry_run,
        "today_expected": today_expected,
        "today_expected_raw": round(today_expected_raw, 1),
        "today_expected_jittered": round(today_expected_jittered, 1),
        "target_min": target_floor,
        "target_max": target_ceiling,
        "target_bound": target_bound,
        "yesterday_actual": yesterday_actual,
        "yesterday_boosted": yesterday_boosted,
        "yesterday_natural": yesterday_natural,
        "today_boosted": today_boosted,
        "gross_need": gross_need,
        "boost_quota": boost_quota,
        "need": need,
        "need_bound": need_bound,
        "target_floor_boost": floor_boost,
        "target_ceiling_boost": ceiling_boost,
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
        "volatility_min": volatility_min,
        "target_volatility": target_volatility,
        "target_volatility_min": target_volatility_min,
        "target_jitter": round(target_jitter, 6),
        "gap_jitter": round(gap_jitter, 6),
        "in_cooldown": in_cooldown,
        "cooldown_days": cooldown_days,
        "boost_list_status": list_status,
        "boost_list_amount": list_amount,
        "boost_list_remaining": list_remaining,
        "threads": threads,
        "wait_min": wait_min,
        "wait_max": wait_max,
        "throughput_capacity": throughput_capacity,
        "capacity_warning": capacity_warning,
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
            reported = exec_result["boosted"]
            # 台账优先：执行器写的是同一个 db_path 时，用执行前后 boost_log
            # 的差值作为本次实际刷量。这样即使执行器没打印 JSON、JSON 无法
            # 解析，或把结果打印成了别的格式，调度器拿到的数量仍然准确；
            # stdout 的 boosted 只在台账没有新增时兜底（自定义执行器写别的库）。
            ledger_after = max(0, int(storage.get_boost_total(today_iso)))
            ledger_delta = max(0, ledger_after - today_boosted)
            if ledger_delta != reported:
                logger.info(
                    "执行器上报 %d 次，boost_log 新增 %d 次（以台账为准）。",
                    reported, ledger_delta,
                )
            summary["boosted"] = ledger_delta if ledger_delta > 0 else reported
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
        # 列表耗尽已回退基线，且当日确有缺口 → 提示"已自动切回基线模式"
        or (
            summary["boost_list_status"] == "fallback"
            and summary["need"] > 0
        )
    )
    level = "warning" if warning else "info"
    if dry_run:
        logger.info("dry_run 模式：跳过每日简报发送。")
    else:
        notifier.send(
            "每日简报",
            _format_summary(summary, dry_run),
            level=level,
            category="daily_summary",
        )

    # 10. 记录当日已执行：daemon 重启后据此避免重复执行同一天；
    #     手动 run 与 daemon 共用同一状态键，谁先跑都算今天已完成。
    #     dry_run 不写，避免“预览一下”把当天正式执行顶掉。
    if dry_run:
        logger.info("dry_run 模式：不写 last_run_date（不影响当天正式执行）。")
    else:
        storage.save_state("last_run_date", today_iso)

    return summary
