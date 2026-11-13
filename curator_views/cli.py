#!/usr/bin/env python3
"""Steam 鉴赏家浏览量预测与受控提升系统 - 命令行入口。

子命令:
  init-config  生成默认配置文件模板
  run          执行每日完整流程（抓取 -> 预测 -> 溢出检测 -> 受控提升 -> 通知）
  daemon       守护进程模式：常驻运行，每天到点重载配置并执行每日流程
  info         查看守护进程是否在运行（本地离线，不触网）
  check        只读检查：抓取统计，打印最近 7 天与预测摘要（不提升）
  forecast     只读推算未来 N 天的目标 / 假设自然流量 / 刷量缺口
  boost        手动执行一轮受控提升
  login        半自动登录 Steam 并生成/校验 cookie.txt（需要 playwright）
  export       从本地数据库导出每日统计数据（json / csv）
  version      打印版本号

用法示例:
  python cli.py init-config
  python cli.py run --config config.yaml
  python cli.py daemon --config config.yaml
  python cli.py info --config config.yaml
  python cli.py info --config config.yaml --json
  python cli.py run --config config.yaml --dry-run --no-boost
  python cli.py check --config config.yaml
  python cli.py forecast --config config.yaml --days 7
  python cli.py forecast --config config.yaml --set growth_factor=1.5 --json
  python cli.py boost --config config.yaml --target 500 --apps 5 --dry-run
  python cli.py login --config config.yaml
  python cli.py login --config config.yaml --validate-only
  python cli.py export --config config.yaml --format csv --out stats.csv
  python cli.py version
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

# 以脚本方式运行时（python curator_views/cli.py）也能导入本包
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402

from curator_views.core import status  # noqa: E402
from curator_views.core.boost_list import peek_next  # noqa: E402
from curator_views.core.booster import Booster  # noqa: E402
from curator_views.core.daemon import Daemon  # noqa: E402
from curator_views.core.forecast import forecast_days  # noqa: E402
from curator_views.core.fetcher import (  # noqa: E402
    CookieFileError,
    Fetcher,
    extract_cookie,
    validate_cookie_header,
)
from curator_views.core.login import (  # noqa: E402
    DEFAULT_PROFILE_DIR,
    CookieCheckResult,
    login_via_browser,
    validate_cookie,
    write_cookie_file,
)
from curator_views.core.notifier import Notifier  # noqa: E402
from curator_views.core.predictor import predict_expected  # noqa: E402
from curator_views.core.scheduler import (  # noqa: E402
    _TZ,
    _date_to_epoch,
    run_daily,
)
from curator_views.core.storage import Storage  # noqa: E402

logger = logging.getLogger("curator_views.cli")

__version__ = "1.0.0"

DEFAULT_CONFIG = r"""# Steam 鉴赏家浏览量预测与受控提升系统配置文件
# 使用方法：`python cli.py init-config` 生成本模板，或手动复制后修改。
# 相对路径均相对于本文件所在目录解析。

# ── 基础配置 ──────────────────────────────────────────────────────────────

# Steam 鉴赏家组 ID（纯数字，如 45519015），必填
# 白话：你的鉴赏家组 ID，就是组页面网址里那串纯数字。
clan_id: ""

# 存放 Steam 登录 Cookie 的文本文件路径（相对本文件所在目录）
# 留空或文件不存在时以未登录（游客）模式运行（不发送 Cookie）。
# 注意：Steam 后台统计通常需要登录，未登录时抓取统计可能失败；
# 仅访问游戏页面刷量的场景一般不受影响。
# 白话：放登录凭据的文件，跑一次 login 就会自动生成；没有就先不填。
cookie_file: "cookie.txt"

# 刷量专用的 Steam 登录 Cookie 文件路径（可选，相对本文件所在目录）。
# 与 cookie_file 完全独立：留空则刷量以未登录（游客）模式运行，
# 不会复用上面的 cookie_file；要用登录态刷量必须在这里填路径。
# 想用不同账号分别"看统计"和"刷量"时填这里，例如用主号看后台统计、
# 用小号刷量；该 Cookie 用
# `python cli.py login --cookie-file boost_cookie.txt --profile-dir .steam_profile_boost`
# 单独生成（不同账号需配合不同的 --profile-dir 保存登录态）。
# 白话：刷量想用哪个号就填哪个号的 Cookie 文件；不填就是游客刷量。
boost_cookie_file: ""

# 代理地址（可选），如 http://127.0.0.1:10279；留空表示不使用代理
# 白话：国内直连 Steam 不稳就填代理，稳定就留空。
proxy: ""

# SQLite 数据库文件路径（相对本文件所在目录），存放每日统计与提升日志
# 白话：历史数据都存在这个数据库文件里，删了等于从零开始。
db_path: "views.db"

# ── 通知配置（可选，两者都配置才启用 ntfy 推送，否则仅写日志） ──────────

# ntfy 服务地址，如 https://ntfy.sh
# 白话：手机推送服务器地址，不填就只写日志、不推送。
ntfy_url: ""

# ntfy 主题名，如 steam-curator-views
# 白话：推送频道名，手机 App 要订阅同名频道才收得到。
ntfy_topic: ""

# 是否启用每日受控提升；命令行 --no-boost 可临时关闭
# 白话：刷量总开关，false 就是只统计不刷，适合先观察几天。
boost_enabled: true

# ── 守护进程模式参数 ──────────────────────────────────────────────────────

# 每日执行时刻（Asia/Shanghai，格式 HH:MM）。
# daemon 模式每天此时执行一次每日流程，且每次执行前都会重新加载
# 本配置文件——修改此处或任意配置均无需重启进程。
# 白话：每天几点跑，改这里就行，daemon 不用重启。
daily_time: "10:00"

# ── 预测参数 ──────────────────────────────────────────────────────────────

# 中位数计算窗口（天数）
# 白话：用最近多少天算“正常水平”，太长反应慢、太短容易被噪声带偏。
window_days: 14

# 趋势斜率相对中位数的封顶比例，防止极端值影响（0~1）
# 白话：涨跌趋势每天最多只影响预测的 15%（上限 = 中位数 × 0.15），防止几天异常把预测带飞。
trend_cap: 0.15

# 启动阶段（数据不足一个窗口）的保守系数，低于 1 以抑制误判
# 白话：数据还不够时先把预测打 9 折，宁可少刷也不冒进。
startup_factor: 0.9

# 计算趋势所需的最少数据点数
# 白话：少于 5 天数据就不猜涨跌，只按中位数算。
trend_min_points: 5

# ── 反作弊（异常溢出检测）参数 ────────────────────────────────────────────

# 异常判定阈值：实际浏览量超过 expected * (1 + k) 视为候选异常日
# 白话：实际比预期高出 30% 以上，就先记一笔“疑似被恶意刷量”。
outlier_k: 0.30

# 连续候选达到该天数才标记为恶意刷量
# 白话：连续 2 天超标才算真被刷，单天波动不算。
consecutive_days: 2

# 溢出量分摊到未来多少天（吸收期），用于平滑浏览曲线
# 白话：多出来的浏览量分 3 天慢慢抵扣，不一次扣完造成断崖。
absorb_days: 3

# 被标记为恶意刷量的日期是否剔除出预测（防止虚高数据污染基线）
# 白话：判定为刷量的那几天不参与预测，免得把基线抬虚。
exclude_flagged: true

# 标记恶意刷量后，未来多少天内强制不刷量，
# 让浏览曲线在无注入的情况下自然回落到真实水平
# 白话：发现被刷后 7 天不刷，让曲线自己落回真实水平。
cooldown_days: 7

# 双基线检测：长期基线窗口（天数）。即使短期预期被虚高数据污染，
# 长期基线仍作为独立参照，防止"平台抬升"型刷量漏检
# 白话：另外拿最近 60 天的长期水平当参照，防“整体被抬高”这种刷法。
baseline_days: 60

# 持续上升斜率检测：最近多少天内斜率超过 中位数×slope_threshold 即标记，
# 防每天温和爬升（单日永不超阈值）的慢性注入
# 白话：看最近 14 天的上涨速度，专抓“每天涨一点”的慢性刷量。
slope_days: 14
# 白话：每天涨幅超过中位数的 2% 就算可疑。
slope_threshold: 0.02

# ── 基线 / 波动率（可选）──────────────────────────────────────────────────

# 手动基线浏览量：大于 0 时直接使用该固定值作为基准目标（替代自动预测），
# 例如希望浏览量稳定在 1000 左右就填 1000；为 0 或留空时使用自动预测
# （中位数 + 趋势）。手动基线模式下溢出检测也以该固定值为参照。
# 白话：想稳定刷到多少就填多少（如 1000），填 0 就让程序自己算。
baseline_views: 0

# 模式切换：stable = 维持稳定（补缺口到基准目标）；growth = 刷量
# （目标 = 基准 × growth_factor，主动把浏览量刷到更高水平）。
# 刷量请优先配合 baseline_views 使用（刷到固定目标后稳定）；
# 若使用自动预测，目标会随中位数水涨船高而持续爬升，慎用。
# 白话：stable = 维持现状；growth = 往上刷（配合 growth_factor）。
boost_mode: stable

# growth 模式下的目标倍率：1.5 表示目标 = 基准 × 1.5。
# stable 模式下该值不生效。倍率不宜过大，否则曲线形态异常仍可能被
# Steam 风控察觉。
# 白话：growth 模式刷到基准的 1.5 倍；stable 模式下用不上。
growth_factor: 1.5

# growth 模式下每天相对基准的变化速率（线性），可为负：
# -0.01 = 每天目标降低基准的 1%，+0.01 = 每天升高 1%；0 = 不随时间变化。
# 只在 growth 模式下生效，且与 growth_factor 相乘，两个一起写不冲突：
# growth_factor 管"台阶高低"，growth_rate 管"每天匀速升降"。
# 生效起点是首次使用该速率的当天；改速率会从改动当天重新起算，
# 不会因为改配置而让目标突然跳变。
# 白话：想匀速下滑就填负数（如 -0.01 = 每天降 1%），想匀速上涨填正数；
# 想每天固定降多少最好配合 baseline_views（固定基准）使用。
growth_rate: 0.0

# 刷量波动率（0~0.3，建议 ≤ 0.15）：每天实际提升量在
# 「缺口 × (1 ± 波动率)」内随机，使浏览曲线在目标附近自然上下波动，
# 避免收敛成一条笔直的线而显得不自然。过大易触发反作弊误判。
# 白话：每天刷的量随机上下浮动 10%，让曲线更像真人、不像机器画的直线。
volatility: 0.10

# ── 每日刷量列表（可选）──────────────────────────────────────────────────

# 刷量列表文件路径（相对本文件所在目录），留空表示不使用列表模式。
# 列表文件一行一个刷量数量（整数），每天取第一行作为当日提升目标，
# 成功刷完后自动从文件里删除该行；空行与 # 开头的注释行会被忽略。
# 启用后列表数值直接覆盖按预测/基线算出的缺口；列表用完或文件缺失时
# 当天不刷量（不会回退到自动预测）。恢复期（cooldown）内暂停刷量，
# 列表行保留到恢复期结束；dry_run 与提升完全失败时也不会消费该行。
# 白话：想按自己排的计划每天刷固定量，就把数字一行一个写进这个文件，
# 程序每天用掉最上面一行、用完删掉一行。
boost_list_file: ""

# ── 受控提升参数 ──────────────────────────────────────────────────────────

# 每日提升使用的最近推荐数量
# 白话：每天只刷最近 5 条推荐，不撒胡椒面。
boost_n: 5

# 单个游戏单日成功访问次数上限
# 白话：同一个游戏一天最多刷 300 次，别把单条刷爆。
single_cap: 300

# 每批处理的游戏数量
# 白话：每处理 50 个游戏歇一批，避免一口气打太多请求。
batch_size: 50

# 累计提升达到目标值的该比例后提前停止，避免超额制造尖峰
# 白话：每轮只补“缺口”的 95%，故意留点余量；所以会稳定在比目标少约 5% 缺口的位置。
target_ratio: 0.95

# ── 网络与运行参数 ────────────────────────────────────────────────────────

# 并发线程数（<=1 为顺序执行，推荐保持 1 以精确计数）
# 白话：同时开几个线程发请求；保持 1 才数得准、最像真人。
threads: 1

# 两次请求之间的随机等待区间（秒），用于模拟自然访问节奏
# 白话：两次请求之间至少歇 8 秒。
wait_min: 8.0
# 白话：两次请求之间最多歇 16 秒，和上一项一起决定节奏。
wait_max: 16.0

# 连续请求失败达到该次数后中止本轮任务
# 白话：连续失败 3 次就收手，别硬刚风控。
max_retries: 3
"""


def _print_error(message: str) -> None:
    """向 stderr 打印错误信息。"""
    print(f"错误: {message}", file=sys.stderr)


def _load_config(config_path: Path) -> Dict[str, Any]:
    """读取并解析 YAML 配置；文件缺失或解析失败时给出明确提示并退出。"""
    if not config_path.is_file():
        _print_error(f"配置文件不存在: {config_path}")
        _print_error(
            f"提示: 请先运行 `python cli.py init-config --path {config_path}` "
            "生成默认配置。"
        )
        sys.exit(1)
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        _print_error(f"配置文件解析失败: {exc}")
        sys.exit(1)
    except OSError as exc:
        _print_error(f"读取配置文件失败: {exc}")
        sys.exit(1)
    return data if isinstance(data, dict) else {}


def _resolve_path(cfg_dir: Path, value: str) -> str:
    """把配置中的路径解析为绝对路径：相对路径基于配置文件所在目录。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cfg_dir / path
    return str(path)


def _require(cfg: Dict[str, Any], key: str, hint: str = "") -> str:
    """取必填配置项，缺失时提示并退出。"""
    value = cfg.get(key)
    if value in (None, ""):
        _print_error(f"配置缺少必填项: {key} {hint}")
        sys.exit(1)
    return str(value)


def _resolve_cookie_paths(
    cfg: Dict[str, Any], cfg_dir: Path
) -> tuple[str, str]:
    """解析两份 Cookie 路径：(统计用, 刷量用)。

    - 统计用读取 ``cookie_file``（访问鉴赏家后台，通常必须登录）；
    - 刷量用读取 ``boost_cookie_file``，两者完全独立；留空时刷量以
      未登录（游客）模式运行，**不会**回退复用 ``cookie_file``。
    """
    stats_value = str(cfg.get("cookie_file", "") or "").strip()
    stats_cookie = _resolve_path(cfg_dir, stats_value) if stats_value else ""
    boost_value = str(cfg.get("boost_cookie_file", "") or "").strip()
    boost_cookie = _resolve_path(cfg_dir, boost_value) if boost_value else ""
    return stats_cookie, boost_cookie


def _check_cookie_file(cookie_file: str, label: str = "Cookie") -> bool:
    """检查 Cookie 文件是否可用；缺失时不终止，改为未登录模式继续。

    Args:
        cookie_file: Cookie 文件路径；空串表示未配置。
        label: 提示用的名称（如“统计 Cookie” / “刷量 Cookie”）。

    Returns:
        True: Cookie 文件存在，按登录模式运行；
        False: 未配置或文件不存在，调用方应以未登录（游客）模式继续。
    """
    if not cookie_file:
        print(
            f"提示: 未配置{label}，将以未登录（游客）模式运行。",
            file=sys.stderr,
        )
        return False
    if not Path(cookie_file).is_file():
        print(
            f"警告: {label} 文件不存在: {cookie_file}，"
            "将以未登录（游客）模式继续；"
            "抓取后台统计可能失败（可运行 python cli.py login 生成）。",
            file=sys.stderr,
        )
        return False
    return True


def _check_cookie_files(stats_cookie: str, boost_cookie: str) -> None:
    """分别检查统计 / 刷量 Cookie；两者路径相同时只提示一次。"""
    _check_cookie_file(stats_cookie, "统计 Cookie")
    if boost_cookie != stats_cookie:
        _check_cookie_file(boost_cookie, "刷量 Cookie")


def _build_runtime(
    config_path: Path,
) -> tuple[Dict[str, Any], Path, str, str, str, str, Optional[str]]:
    """加载配置并解析公共路径。

    Returns:
        (cfg, cfg_dir, clan_id, 统计Cookie, 刷量Cookie, db_path, proxy)
    """
    cfg = _load_config(config_path)
    cfg_dir = config_path.resolve().parent
    clan_id = _require(cfg, "clan_id", "（Steam 鉴赏家组 ID）")
    cookie_file, boost_cookie_file = _resolve_cookie_paths(cfg, cfg_dir)
    db_path = _resolve_path(cfg_dir, str(cfg.get("db_path", "views.db")))
    proxy = cfg.get("proxy") or None
    return (
        cfg,
        cfg_dir,
        clan_id,
        cookie_file,
        boost_cookie_file,
        db_path,
        proxy,
    )


def _make_booster(
    cfg: Dict[str, Any],
    clan_id: str,
    cookie_file: str,
    proxy: Optional[str],
    storage: Storage,
) -> Booster:
    """按配置构造 Booster 实例。"""
    return Booster(
        clan_id=clan_id,
        cookie_file=cookie_file,
        proxy=proxy,
        storage=storage,
        batch_size=int(cfg.get("batch_size", 50)),
        single_cap=int(cfg.get("single_cap", 300)),
        threads=int(cfg.get("threads", 1)),
        wait_min=float(cfg.get("wait_min", 8.0)),
        wait_max=float(cfg.get("wait_max", 16.0)),
        max_retries=int(cfg.get("max_retries", 3)),
        target_ratio=float(cfg.get("target_ratio", 0.95)),
        distribution=str(cfg.get("boost_distribution", "ratio")).lower(),
    )


def _scheduler_config(
    cfg: Dict[str, Any], boost_enabled: bool, dry_run: bool, cfg_dir: Path
) -> Dict[str, Any]:
    """把配置映射为调度器 run_daily 所需的 config 字典。"""
    list_file = str(cfg.get("boost_list_file", "") or "").strip()
    return {
        "window_days": int(cfg.get("window_days", 14)),
        "trend_cap": float(cfg.get("trend_cap", 0.15)),
        "startup_factor": float(cfg.get("startup_factor", 0.9)),
        "trend_min_points": int(cfg.get("trend_min_points", 5)),
        "outlier_k": float(cfg.get("outlier_k", 0.30)),
        "consecutive_days": int(cfg.get("consecutive_days", 2)),
        "absorb_days": int(cfg.get("absorb_days", 3)),
        "exclude_flagged": bool(cfg.get("exclude_flagged", True)),
        "cooldown_days": int(cfg.get("cooldown_days", 7)),
        "baseline_days": int(cfg.get("baseline_days", 60)),
        "slope_days": int(cfg.get("slope_days", 14)),
        "slope_threshold": float(cfg.get("slope_threshold", 0.02)),
        "baseline_views": float(cfg.get("baseline_views", 0) or 0),
        "boost_mode": str(cfg.get("boost_mode", "stable")).lower(),
        "growth_factor": float(cfg.get("growth_factor", 1.0) or 1.0),
        "growth_rate": float(cfg.get("growth_rate", 0) or 0),
        "volatility": float(cfg.get("volatility", 0) or 0),
        "boost_n": int(cfg.get("boost_n", 5)),
        "single_cap": int(cfg.get("single_cap", 300)),
        "batch_size": int(cfg.get("batch_size", 50)),
        "target_ratio": float(cfg.get("target_ratio", 0.95)),
        "boost_distribution": str(
            cfg.get("boost_distribution", "ratio")
        ).lower(),
        "boost_enabled": bool(boost_enabled),
        "dry_run": bool(dry_run),
        "boost_list_file": _resolve_path(cfg_dir, list_file) if list_file else "",
    }


# ── 子命令实现 ────────────────────────────────────────────────────────────


def cmd_init_config(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.is_file():
        _print_error(f"配置文件已存在: {target}")
        _print_error("如需重新生成，请先删除该文件，或指定其他 --path。")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG, encoding="utf-8")
    print(f"已生成默认配置文件: {target}")
    print("请编辑 clan_id 与 cookie_file 等配置后再使用 run / check / boost 命令。")
    return 0


def _execute_daily(
    cfg: Dict[str, Any],
    config_path: str,
    boost_enabled: bool,
    dry_run: bool,
) -> Dict[str, Any]:
    """按配置构建运行时并执行一次每日完整流程（供 run / daemon 复用）。

    Args:
        cfg: 已加载的配置字典。
        config_path: 配置文件路径（用于执行器子进程占位符与相对路径解析）。
        boost_enabled: 是否启用提升（命令行 --boost/--no-boost 优先级高于配置）。
        dry_run: 演练模式，只计算不实际提升。

    Returns:
        run_daily 的汇总字典。
    """
    cfg_dir = Path(config_path).resolve().parent
    clan_id = _require(cfg, "clan_id", "（Steam 鉴赏家组 ID）")
    cookie_file, boost_cookie_file = _resolve_cookie_paths(cfg, cfg_dir)
    db_path = _resolve_path(cfg_dir, str(cfg.get("db_path", "views.db")))
    proxy = cfg.get("proxy") or None
    _check_cookie_files(cookie_file, boost_cookie_file)

    storage = Storage(db_path)
    fetcher = Fetcher(clan_id, cookie_file, proxy)
    notifier = Notifier(
        ntfy_url=str(cfg.get("ntfy_url", "") or ""),
        ntfy_topic=str(cfg.get("ntfy_topic", "") or ""),
    )
    booster = _make_booster(
        cfg, clan_id, boost_cookie_file, proxy, storage
    )

    sched_cfg = _scheduler_config(cfg, boost_enabled, dry_run, cfg_dir)
    executor_cmd = str(cfg.get("executor_cmd", "") or "")
    return run_daily(
        storage,
        fetcher,
        notifier,
        booster,
        sched_cfg,
        executor_cmd=executor_cmd,
        config_path=config_path,
    )


def _setup_logging(
    config_path: Path, db_path: str, console: bool = False
) -> Path:
    """把日志写入数据库同级目录的 curator_views.log。

    Args:
        config_path: 配置文件路径（仅用于日志上下文，不参与路径计算）。
        db_path: 数据库路径，日志写在它同级目录。
        console: 是否同时输出到控制台（daemon 模式需要，否则前台完全静默）。

    Returns:
        日志文件路径。
    """
    log_path = Path(db_path).parent / "curator_views.log"
    file_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.FileHandler(str(log_path), encoding="utf-8"),
    ]
    if console:
        # 控制台只打时间/级别/内容，不重复 logger 名，避免刷屏
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        handlers.append(console_handler)
    logging.basicConfig(
        level=logging.INFO,
        format=file_format,
        handlers=handlers,
        force=True,
    )
    return log_path


def cmd_run(args: argparse.Namespace) -> int:
    (
        cfg, _cfg_dir, _clan_id, _cookie_file,
        _boost_cookie_file, db_path, _proxy,
    ) = _build_runtime(Path(args.config))
    _setup_logging(Path(args.config), db_path)

    # --no-boost 关闭提升；--boost 强制开启；两者都不传时以配置为准
    if args.boost is None:
        boost_enabled = bool(cfg.get("boost_enabled", True))
    else:
        boost_enabled = bool(args.boost)

    summary = _execute_daily(
        cfg, str(Path(args.config)), boost_enabled, args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """守护进程模式：常驻运行，每天到点重载配置并执行每日流程。

    每次循环重新加载配置文件（改配置/执行时间无需重启）；
    启动时若今天尚未执行且已过执行时刻，立即补跑一次；
    单次执行失败不退出进程，等待下一天。
    """
    config_path = Path(args.config)
    cfg = _load_config(config_path)
    cfg_dir = config_path.resolve().parent
    db_path = _resolve_path(cfg_dir, str(cfg.get("db_path", "views.db")))
    log_path = _setup_logging(config_path, db_path, console=True)
    logger.info("守护进程日志文件: %s", log_path)

    def load_daily_time() -> str:
        # 每次循环重载配置，取每日执行时刻
        return str(_load_config(config_path).get("daily_time", "10:00"))

    def read_db_last_run_date() -> str:
        """从数据库读取最近一次完成日期（手动 run 也会写这个键）。

        数据库或 kv_store 表不存在时返回空串，绝不因此阻塞守护进程启动。
        """
        if not Path(db_path).is_file():
            return ""
        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", ("last_run_date",)
            ).fetchone()
            return str(row[0]) if row else ""
        except sqlite3.Error:
            logger.warning("读取数据库 last_run_date 失败", exc_info=True)
            return ""
        finally:
            if conn is not None:
                conn.close()

    def execute_once() -> Dict[str, Any]:
        # 每次执行前重新加载配置，命令行 --boost/--no-boost 覆盖配置
        cfg = _load_config(config_path)
        if args.boost is None:
            boost_enabled = bool(cfg.get("boost_enabled", True))
        else:
            boost_enabled = bool(args.boost)
        summary = _execute_daily(
            cfg, str(config_path), boost_enabled, args.dry_run,
        )
        logger.info(
            "每日执行完成: %s", json.dumps(summary, ensure_ascii=False),
        )
        return summary

    state_path = status.default_state_path(db_path)
    daemon = Daemon(
        load_daily_time,
        execute_once,
        state_path=state_path,
        last_run_date_provider=read_db_last_run_date,
    )
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        logger.info("守护进程被手动中断（Ctrl+C）。")
    return 0


def _fmt_state_time(value: Any, with_seconds: bool = True) -> str:
    """把状态文件中的 ISO 时间格式化为本地可读文本；非法值原样/占位返回。"""
    parsed = status.parse_timestamp(value)
    if parsed is None:
        return "-"
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return parsed.strftime(fmt)


def _collect_info_data(db_path: str) -> Dict[str, Any]:
    """读取本地数据库的统计摘要；数据库不存在时不创建、返回空值。"""
    data: Dict[str, Any] = {
        "days": 0,
        "first_date": None,
        "latest_date": None,
        "latest_views": None,
        "program_start": None,
        "last_stats_date": None,
    }
    if not Path(db_path).is_file():
        return data

    storage = Storage(db_path)
    rows = storage.get_daily_stats()
    if rows:
        data["days"] = len(rows)
        data["first_date"] = status.date_ts_to_iso(rows[0]["date_ts"])
        data["latest_date"] = status.date_ts_to_iso(rows[-1]["date_ts"])
        data["latest_views"] = int(rows[-1]["num_views"])

    program_start = storage.load_state("program_start", default="")
    data["program_start"] = program_start or None

    raw_last_stats = storage.load_state("last_stats_date", default="")
    if raw_last_stats:
        try:
            parsed = json.loads(raw_last_stats)
        except json.JSONDecodeError:
            data["last_stats_date"] = raw_last_stats
        else:
            if isinstance(parsed, dict):
                data["last_stats_date"] = parsed.get("last_date") or None
            else:
                data["last_stats_date"] = raw_last_stats
    return data


def _print_info_report(report: Dict[str, Any]) -> None:
    """以中文人类可读格式打印 info 报告。"""
    daemon = report["daemon"]
    config = report["config"]
    data = report["data"]

    print(f"守护进程: {daemon['detail']}")
    if daemon["pid"] is not None:
        print(f"  PID: {daemon['pid']}")
    if daemon["started_at"]:
        print(f"  启动时间: {_fmt_state_time(daemon['started_at'])}")
    if daemon["phase"]:
        print(
            f"  当前阶段: {daemon['phase']}（{daemon['phase_label']}）"
        )
    if daemon["updated_at"]:
        age = daemon["heartbeat_age_seconds"]
        suffix = f"（{round(age)} 秒前）" if age is not None else ""
        print(f"  上次心跳: {_fmt_state_time(daemon['updated_at'])}{suffix}")
    if daemon["last_run_at"]:
        print(f"  上次执行: {_fmt_state_time(daemon['last_run_at'])}")
    elif daemon["last_run_date"]:
        # 仅有日期时不要补成 00:00:00，避免误读为凌晨执行
        print(f"  上次执行: {daemon['last_run_date']}")
    if daemon["next_wake"]:
        label = "下次执行" if daemon["running"] else "上次计划执行"
        print(f"  {label}: {_fmt_state_time(daemon['next_wake'], False)}")
    print(f"  状态文件: {daemon['state_file']}")
    if daemon["last_error"]:
        print(f"  上次错误: {daemon['last_error']}")

    boost_text = (
        "提升已开启" if config["boost_enabled"] else "提升已关闭（采集模式）"
    )
    print(
        f"配置: 每日执行 {config['daily_time']}；{boost_text}；"
        f"boost_mode={config['boost_mode']}"
    )

    if config.get("boost_list_file"):
        labels = {
            "active": "使用中",
            "empty": "已用完",
            "missing": "文件缺失",
            "error": "读取失败",
        }
        label = labels.get(config.get("boost_list_status", ""), "未知")
        remaining = config.get("boost_list_remaining")
        suffix = f"，剩余 {remaining} 行" if remaining is not None else ""
        print(
            f"刷量列表: {label}{suffix}（{config['boost_list_file']}）"
        )

    if data["days"]:
        print(
            f"数据: {data['days']} 天（{data['first_date']} ~ "
            f"{data['latest_date']}）；最新 {data['latest_date']} "
            f"{data['latest_views']} 次；"
            f"程序启动日 {data['program_start'] or '未知'}"
        )
    else:
        print("数据: 暂无统计数据")


def cmd_info(args: argparse.Namespace) -> int:
    """查看守护进程运行状态与本地数据概况。

    只读本地状态文件/数据库，不要求 clan_id 或 Cookie，不触网，
    数据库不存在时也不会创建空库。
    """
    config_path = Path(args.config)
    cfg = _load_config(config_path)
    cfg_dir = config_path.resolve().parent
    db_path = _resolve_path(cfg_dir, str(cfg.get("db_path", "views.db")))

    state_path = status.default_state_path(db_path)
    state = status.read_state(state_path)
    daemon_info = status.describe_daemon(state)
    daemon_info["state_file"] = state_path
    daemon_info["state_file_exists"] = Path(state_path).is_file()

    list_value = str(cfg.get("boost_list_file", "") or "").strip()
    list_path = _resolve_path(cfg_dir, list_value) if list_value else ""
    list_remaining: Optional[int] = None
    list_status = "disabled"
    if list_path:
        try:
            _, list_remaining = peek_next(list_path)
            list_status = "active" if list_remaining > 0 else "empty"
        except FileNotFoundError:
            list_status = "missing"
        except OSError:
            list_status = "error"

    report = {
        "daemon": daemon_info,
        "config": {
            "daily_time": str(cfg.get("daily_time", "10:00")),
            "boost_enabled": bool(cfg.get("boost_enabled", True)),
            "boost_mode": str(cfg.get("boost_mode", "stable")).lower(),
            "db_path": db_path,
            "boost_list_file": list_path,
            "boost_list_remaining": list_remaining,
            "boost_list_status": list_status,
        },
        "data": _collect_info_data(db_path),
    }

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_info_report(report)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    (
        cfg, cfg_dir, clan_id, cookie_file,
        _boost_cookie_file, db_path, proxy,
    ) = _build_runtime(Path(args.config))
    _check_cookie_file(cookie_file, "统计 Cookie")

    fetcher = Fetcher(clan_id, cookie_file, proxy)
    history = fetcher.fetch_stats()
    history = sorted(history, key=lambda entry: entry["date_ts"])

    print(f"共获取到 {len(history)} 天的统计数据。")
    print("最近 7 天:")
    for entry in history[-7:]:
        print(f"  date_ts={entry['date_ts']}  num_views={entry['num_views']}")

    pred = predict_expected(
        history,
        window_days=int(cfg.get("window_days", 14)),
        trend_cap=float(cfg.get("trend_cap", 0.15)),
        startup_factor=float(cfg.get("startup_factor", 0.9)),
        trend_min_points=int(cfg.get("trend_min_points", 5)),
    )
    print("预测摘要:")
    print(json.dumps({"stats_days": len(history), "prediction": pred},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_boost(args: argparse.Namespace) -> int:
    (
        cfg, cfg_dir, clan_id, _cookie_file,
        boost_cookie_file, db_path, proxy,
    ) = _build_runtime(Path(args.config))
    _check_cookie_file(boost_cookie_file, "刷量 Cookie")

    storage = Storage(db_path)
    booster = _make_booster(
        cfg, clan_id, boost_cookie_file, proxy, storage
    )
    apps = args.apps if args.apps is not None else int(cfg.get("boost_n", 5))
    boosted = booster.run(args.target, apps, args.dry_run)

    summary = {
        "target": args.target,
        "apps": apps,
        "dry_run": bool(args.dry_run),
        "boosted": boosted,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """半自动登录 Steam 并生成/校验 Cookie 文件。

    --validate-only 只校验已有 Cookie；正常模式打开 Chromium 由人类完成登录，
    导出 Cookie 并通过后台页面校验后才写入文件，避免坏 Cookie 覆盖可用文件。

    统计与刷量使用两份 Cookie 时，用 ``--cookie-file`` 指定写入目标，
    并配合 ``--profile-dir`` 为不同账号保存独立登录态。
    """
    cfg = _load_config(Path(args.config))
    cfg_dir = Path(args.config).resolve().parent
    stats_cookie, _boost_cookie = _resolve_cookie_paths(cfg, cfg_dir)
    if args.cookie_file:
        cookie_file = _resolve_path(cfg_dir, args.cookie_file)
    elif stats_cookie:
        cookie_file = stats_cookie
    else:
        cookie_file = str(cfg_dir / "cookie.txt")
    clan_id = str(cfg.get("clan_id") or "")
    proxy = cfg.get("proxy") or None

    if args.validate_only:
        if not Path(cookie_file).is_file():
            _print_error(f"Cookie 文件不存在: {cookie_file}")
            _print_error(
                "提示: 请先运行 "
                f"`python cli.py login --config {args.config}` 半自动生成。"
            )
            return 1
        raw = Path(cookie_file).read_text(encoding="utf-8")
        cookie = extract_cookie(raw)
        try:
            validate_cookie_header(cookie)
        except CookieFileError as exc:
            _print_error(str(exc))
            return 1
        if not clan_id:
            _print_error("配置缺少 clan_id，无法校验 Cookie 有效性")
            return 1
        result: CookieCheckResult = validate_cookie(cookie, clan_id, proxy=proxy)
        summary = {
            "mode": "validate",
            "cookie_file": cookie_file,
            "clan_id": clan_id,
            "valid": result.valid,
            "reason": result.reason,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result.valid else 1

    if args.profile_dir:
        profile_dir = _resolve_path(cfg_dir, args.profile_dir)
    else:
        profile_dir = str(cfg_dir / DEFAULT_PROFILE_DIR)
    cookie = login_via_browser(
        profile_dir,
        timeout_s=args.timeout,
        headless=args.headless,
        proxy=proxy,
    )

    # 未显式跳过且配置了 clan_id 时，校验通过才写文件
    valid: Optional[bool] = None
    reason = "已跳过有效性校验"
    if args.no_validate:
        reason = "已跳过有效性校验（--no-validate）"
    elif not clan_id:
        reason = "配置缺少 clan_id，已跳过有效性校验"
    else:
        result = validate_cookie(cookie, clan_id, proxy=proxy)
        valid = result.valid
        reason = result.reason
        if not result.valid:
            _print_error(f"登录得到的 Cookie 校验失败：{result.reason}")
            _print_error("已放弃写入，避免覆盖原有 Cookie 文件。")
            return 1

    write_cookie_file(cookie_file, cookie)
    summary = {
        "mode": "login",
        "cookie_file": cookie_file,
        "clan_id": clan_id,
        "valid": valid,
        "reason": reason,
        "cookie_count": len(cookie.split("; ")),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    (
        _cfg, _cfg_dir, _clan_id, _cookie_file,
        _boost_cookie_file, db_path, _proxy,
    ) = _build_runtime(Path(args.config))
    storage = Storage(db_path)
    rows = storage.get_daily_stats()

    if args.format == "json":
        payload = json.dumps(rows, ensure_ascii=False, indent=2)
    else:  # csv
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["date_ts", "num_views"])
        writer.writeheader()
        writer.writerows(rows)
        payload = buffer.getvalue()

    if args.out:
        # 用 write_bytes 避免 Windows 文本模式下 \n -> \r\n 的二次换行
        Path(args.out).write_bytes(payload.encode("utf-8"))
        print(f"已导出 {len(rows)} 条记录到 {args.out}")
    else:
        print(payload, end="")
    return 0


def _coerce_override(raw: str, existing: Any) -> Any:
    """按配置中已有值的类型推断 --set 覆盖值；键不存在时按字面量猜测。"""
    text = raw.strip()
    if isinstance(existing, bool):
        return text.lower() in ("1", "true", "yes", "on")
    if isinstance(existing, int):
        try:
            return int(text)
        except ValueError:
            return raw
    if isinstance(existing, float):
        try:
            return float(text)
        except ValueError:
            return raw
    if existing is not None:
        return raw
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return raw


def _apply_overrides(cfg: Dict[str, Any], items: list[str]) -> None:
    """把 --set key=value 覆盖到配置字典，用于 what-if 估算。"""
    for item in items:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            _print_error(f"--set 参数格式应为 key=value: {item}")
            sys.exit(1)
        cfg[key] = _coerce_override(raw, cfg.get(key))


def _read_forecast_db(db_path: str) -> tuple[list[dict], Dict[str, str]]:
    """以只读 URI 打开数据库，返回 (daily_stats, kv_store)。

    绝不执行建表/写入，也不启用 WAL；数据库文件内容与 mtime 保持不变。
    """
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        try:
            rows = conn.execute(
                "SELECT date_ts, num_views FROM daily_stats ORDER BY date_ts ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        history = [
            {"date_ts": int(row[0]), "num_views": int(row[1])} for row in rows
        ]
        kv: Dict[str, str] = {}
        try:
            for key, value in conn.execute("SELECT key, value FROM kv_store"):
                kv[str(key)] = str(value)
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return history, kv


def _forecast_start_epoch() -> int:
    """预测首日：明天 00:00（Asia/Shanghai）；Steam 统计滞后一天。"""
    tomorrow = datetime.now(_TZ) + timedelta(days=1)
    return int(
        tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )


def _print_forecast_report(
    result: Dict[str, Any],
    config: Dict[str, Any],
    source: Dict[str, Any],
    fetched: bool,
    db_path: str,
) -> None:
    """以中文对齐表格打印 forecast 结果。"""
    rows = result["rows"]
    summary = result["summary"]
    print(
        f"预测区间: {summary['first_date']} ~ {summary['last_date']}"
        f"（{summary['days']} 天）"
    )
    source_text = "Steam 在线抓取" if fetched else f"本地数据库 {db_path}"
    print(
        f"数据来源: {source_text}（{source['history_days']} 天，"
        f"最新 {source['latest_date']} = {source['latest_views']} 次）"
    )
    baseline = float(config.get("baseline_views", 0) or 0)
    baseline_text = "auto" if baseline <= 0 else f"{baseline:g}"
    print(
        f"参数: boost_mode={config.get('boost_mode', 'stable')} "
        f"factor={float(config.get('growth_factor', 1.0)):g} "
        f"rate={float(config.get('growth_rate', 0)):g} "
        f"基线={baseline_text} "
        f"波动率={float(config.get('volatility', 0)):.2f}"
    )
    print()
    print(
        f"{'日期':<12}{'基准':>8}{'倍率':>8}{'目标':>9}"
        f"{'假设自然':>9}{'缺口':>8}"
    )
    for row in rows:
        print(
            f"{row['date']:<12}{row['base']:>8.1f}{row['multiplier']:>8.3f}"
            f"{row['target']:>9.1f}{row['assumed_natural']:>9}{row['need']:>8}"
        )
    print()
    print(
        f"汇总: {summary['days']} 天预计刷量 {summary['total_need']}；"
        f"目标 {summary['target_first']:.1f} → {summary['target_last']:.1f}"
    )
    if result["warnings"]:
        print("提示:")
        for warning in result["warnings"]:
            print(f"  - [{warning['code']}] {warning['message']}")


def cmd_forecast(args: argparse.Namespace) -> int:
    """只读推算未来 N 天的目标、假设自然流量与刷量缺口。

    默认只读本地数据库、不触网；--fetch 才在线抓取（仅在内存使用，不写库）。
    --set 可覆盖任意配置键做 what-if；数据库不存在或无历史时以 1 退出，绝不建库。
    """
    if args.days < 1:
        _print_error("--days 必须 >= 1")
        return 1

    config_path = Path(args.config)
    cfg = _load_config(config_path)
    cfg_dir = config_path.resolve().parent
    _apply_overrides(cfg, getattr(args, "set", None) or [])

    sched_cfg = _scheduler_config(
        cfg, bool(cfg.get("boost_enabled", True)), True, cfg_dir
    )
    db_path = _resolve_path(cfg_dir, str(cfg.get("db_path", "views.db")))
    if not Path(db_path).is_file():
        _print_error(f"数据库不存在: {db_path}")
        _print_error(
            "提示: forecast 只读，不会自动创建空库；请先运行 run 积累数据。"
        )
        return 1

    try:
        history, kv = _read_forecast_db(db_path)
    except sqlite3.Error as exc:
        _print_error(f"读取数据库失败: {exc}")
        return 1

    if getattr(args, "fetch", False):
        clan_id = str(cfg.get("clan_id") or "").strip()
        if not clan_id:
            _print_error("--fetch 需要配置 clan_id")
            return 1
        cookie_file, _boost_cookie = _resolve_cookie_paths(cfg, cfg_dir)
        fetcher = Fetcher(clan_id, cookie_file, cfg.get("proxy") or None)
        fetched = fetcher.fetch_stats()
        merged = {
            int(item["date_ts"]): int(item["num_views"]) for item in history
        }
        merged.update(
            {
                int(item["date_ts"]): int(item["num_views"])
                for item in fetched
            }
        )
        history = [
            {"date_ts": ts, "num_views": views}
            for ts, views in sorted(merged.items())
        ]

    # 程序启动日同样只读；缺失或非法时不过滤（从最早一条开始）
    program_start = kv.get("program_start", "").strip()
    if program_start:
        try:
            program_start_epoch = _date_to_epoch(program_start)
        except (ValueError, IndexError):
            program_start_epoch = 0
        if program_start_epoch:
            history = [
                item for item in history
                if item["date_ts"] >= program_start_epoch
            ]

    if not history:
        _print_error("数据库中没有历史统计数据（或 program_start 之后无数据）。")
        return 1

    # growth 锚点只读：速率一致且起点可解析才沿用，否则按预测首日重新起算
    growth_start_ts = 0
    try:
        stored_rate = float(kv.get("growth_rate", ""))
        stored_start = kv.get("growth_start", "").strip()
        if stored_start and abs(
            stored_rate - float(sched_cfg["growth_rate"])
        ) < 1e-9:
            growth_start_ts = _date_to_epoch(stored_start)
    except (TypeError, ValueError, IndexError):
        growth_start_ts = 0

    start_ts = _forecast_start_epoch()
    result = forecast_days(
        history,
        sched_cfg,
        start_ts,
        days=args.days,
        growth_start_ts=growth_start_ts,
        natural_mode=args.natural,
    )
    latest = history[-1]
    latest_date = datetime.fromtimestamp(
        int(latest["date_ts"]), tz=_TZ
    ).strftime("%Y-%m-%d")
    source = {
        "mode": "fetch" if args.fetch else "local",
        "db_path": db_path,
        "history_days": len(history),
        "latest_date": latest_date,
        "latest_views": int(latest["num_views"]),
        "program_start": program_start or None,
        "growth_start_ts": growth_start_ts,
        "start_ts": start_ts,
        "natural_mode": args.natural,
    }
    if args.json:
        payload = {
            "rows": result["rows"],
            "warnings": result["warnings"],
            "summary": result["summary"],
            "source": source,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print_forecast_report(result, sched_cfg, source, args.fetch, db_path)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"curator_views {__version__}")
    return 0


# ── 命令行解析 ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curator_views",
        description="Steam 鉴赏家浏览量预测与受控提升命令行工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行每日完整流程（抓取-预测-检测-提升-通知）")
    p_run.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_run.add_argument("--dry-run", action="store_true",
                       help="演练模式：计算缺口但实际不发送提升请求")
    boost_group = p_run.add_mutually_exclusive_group()
    boost_group.add_argument("--boost", dest="boost", action="store_true", default=None,
                             help="强制启用提升")
    boost_group.add_argument("--no-boost", dest="boost", action="store_false",
                             help="关闭提升（优先于配置中的 boost_enabled）")
    p_run.set_defaults(func=cmd_run)

    p_daemon = sub.add_parser(
        "daemon", help="守护进程模式：常驻运行，每天到点执行每日流程（配置热重载）"
    )
    p_daemon.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_daemon.add_argument("--dry-run", action="store_true",
                          help="演练模式：计算缺口但实际不发送提升请求")
    boost_group_d = p_daemon.add_mutually_exclusive_group()
    boost_group_d.add_argument("--boost", dest="boost", action="store_true", default=None,
                               help="强制启用提升")
    boost_group_d.add_argument("--no-boost", dest="boost", action="store_false",
                               help="关闭提升（优先于配置中的 boost_enabled）")
    p_daemon.set_defaults(func=cmd_daemon)

    p_info = sub.add_parser(
        "info", help="查看守护进程是否在运行（本地离线，不触网）"
    )
    p_info.add_argument("--config", default="config.yaml",
                        help="配置文件路径（默认 config.yaml）")
    p_info.add_argument("--json", action="store_true",
                        help="以 JSON 输出原始报告")
    p_info.set_defaults(func=cmd_info)

    p_check = sub.add_parser("check", help="只读检查：抓取统计并打印最近 7 天与预测摘要")
    p_check.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_check.set_defaults(func=cmd_check)

    p_boost = sub.add_parser("boost", help="手动执行一轮受控提升")
    p_boost.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_boost.add_argument("--target", type=int, required=True,
                         help="本轮目标提升次数（必填）")
    p_boost.add_argument("--apps", type=int, default=None,
                         help="使用的最近推荐数量（默认取配置中的 boost_n）")
    p_boost.add_argument("--dry-run", action="store_true",
                         help="演练模式：仅记录提升计划，不发送任何请求")
    p_boost.set_defaults(func=cmd_boost)

    p_login = sub.add_parser(
        "login", help="半自动登录 Steam 并生成/校验 cookie.txt（需要 playwright）"
    )
    p_login.add_argument("--config", default="config.yaml",
                         help="配置文件路径（默认 config.yaml）")
    p_login.add_argument("--validate-only", action="store_true",
                         help="仅校验已有 Cookie，不打开浏览器")
    p_login.add_argument("--headless", action="store_true",
                         help="无界面模式——通常无法完成验证码/手机确认")
    p_login.add_argument("--no-validate", action="store_true",
                         help="登录后跳过有效性校验直接写入")
    p_login.add_argument("--timeout", type=float, default=300.0,
                         help="等待登录完成的超时秒数（默认 300）")
    p_login.add_argument("--profile-dir", default=None,
                         help="浏览器持久化配置目录，默认配置目录下的 .steam_profile")
    p_login.add_argument("--cookie-file", default=None,
                         help="写入的 Cookie 文件路径（默认取配置 cookie_file）")
    p_login.set_defaults(func=cmd_login)

    p_export = sub.add_parser("export", help="导出每日统计数据")
    p_export.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_export.add_argument("--format", choices=["json", "csv"], default="json",
                          help="导出格式（默认 json）")
    p_export.add_argument("--out", default=None, help="输出文件路径（缺省打印到标准输出）")
    p_export.set_defaults(func=cmd_export)

    p_forecast = sub.add_parser(
        "forecast", help="只读预测未来 N 天的目标/自然流量/缺口（不写库）"
    )
    p_forecast.add_argument("--config", default="config.yaml",
                            help="配置文件路径（默认 config.yaml）")
    p_forecast.add_argument("--days", type=int, default=7,
                            help="预测天数（默认 7，必须 >= 1）")
    p_forecast.add_argument("--fetch", action="store_true",
                            help="额外从 Steam 在线抓取最新统计（仅内存，不写库）")
    p_forecast.add_argument("--natural", choices=["trend", "target"],
                            default="trend",
                            help="假设自然流量模式：trend 按趋势延续 / "
                                 "target 收敛到目标（默认 trend）")
    p_forecast.add_argument("--set", action="append", default=[],
                            metavar="KEY=VALUE",
                            help="覆盖配置项做 what-if，可重复")
    p_forecast.add_argument("--json", action="store_true",
                            help="输出完整 JSON（含 rows/warnings/summary/source）")
    p_forecast.set_defaults(func=cmd_forecast)

    p_init = sub.add_parser("init-config", help="生成默认配置文件模板")
    p_init.add_argument("--path", default="config.yaml", help="生成的配置文件路径（默认 config.yaml）")
    p_init.set_defaults(func=cmd_init_config)

    p_ver = sub.add_parser("version", help="打印版本号")
    p_ver.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        _print_error(str(exc))
        _print_error("提示: 请检查配置文件中的文件路径（cookie_file / db_path）。")
        return 1
    except KeyboardInterrupt:
        _print_error("已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        _print_error(f"执行失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
