#!/usr/bin/env python3
"""Steam 鉴赏家浏览量预测与受控提升系统 - 命令行入口。

子命令:
  init-config  生成默认配置文件模板
  run          执行每日完整流程（抓取 -> 预测 -> 溢出检测 -> 受控提升 -> 通知）
  daemon       守护进程模式：常驻运行，每天到点重载配置并执行每日流程
  info         查看守护进程是否在运行（本地离线，不触网）
  check        抓取统计并写入本地库，打印最近 7 天与预测摘要（不提升）
  forecast     只读推算未来 N 天的目标 / 假设自然流量 / 刷量缺口
  boost        手动执行一轮受控提升
  login        半自动登录 Steam 并生成/校验 cookie.txt（需要 playwright）
  refresh-cookie 用 steamRefresh_steam 手动续期统计 Cookie
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
  python cli.py forecast --config config.yaml --fetch
  python cli.py forecast --config config.yaml --set growth_factor=1.5 --json
  python cli.py boost --config config.yaml --target 500 --apps 5 --dry-run
  python cli.py login --config config.yaml
  python cli.py login --config config.yaml --validate-only
  python cli.py refresh-cookie --config config.yaml
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
import unicodedata
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
from curator_views.core.chart import (  # noqa: E402
    render_forecast_console,
    write_forecast_chart,
)
from curator_views.core.daemon import (  # noqa: E402
    Daemon, parse_daily_times, pick_run_marker,
)
from curator_views.core.daytime import (  # noqa: E402
    DEFAULT_DAY_BOUNDARY,
    day_label_from_ts,
    format_day_boundary,
    logical_date,
    parse_day_boundary,
    previous_date,
    resolve_day_boundary,
)
from curator_views.core.forecast import forecast_days  # noqa: E402
from curator_views.core.fetcher import (  # noqa: E402
    STATS_URL,
    CookieFileError,
    Fetcher,
    StatsAuthError,
    extract_cookie,
    read_cookie_file,
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
from curator_views.core.session_refresh import (  # noqa: E402
    CookieState,
    RefreshError,
    RefreshOutcome,
    cookie_expires_at,
    cookie_token_ip,
    extract_refresh_cookie,
    load_cookie_state,
    refresh_cookie_file,
    save_cookie_state,
    session_state_path,
    split_refresh_cookie,
)
from curator_views.core.scheduler import (  # noqa: E402
    _TZ,
    _daily_throughput_capacity,
    _date_to_epoch,
    _effective_threads,
    _normalize_target_bounds,
    _today_epoch,
    run_daily,
)
from curator_views.core.storage import Storage  # noqa: E402

logger = logging.getLogger("curator_views.cli")

__version__ = "1.0.0"

DEFAULT_CONFIG = r"""# Steam 鉴赏家浏览量预测与受控提升系统配置文件
# 使用方法：`python cli.py init-config` 生成本模板，或手动复制后修改。
# 相对路径均相对于本文件所在目录解析。
# 只需保留关心的键：没写的键自动使用模板默认值（clan_id 仍为必填）。

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

# 用 steamRefresh_steam 自动续期统计 Cookie（关闭后只读 cookie_file）
cookie_refresh_enabled: true
# 距 steamLoginSecure 过期少于该小时数时主动续期（0 = 有令牌就续期）
cookie_refresh_before_hours: 12
# 两次实际续期的最小间隔（小时），避免失败时空转（0 = 关闭限流）
cookie_refresh_min_interval_hours: 6

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

# 是否启用 ntfy 推送（false 时所有通知只写日志，不排队也不补发）
ntfy_enabled: true

# 最低推送级别：info / warning / error；低于该级别的通知只写日志
ntfy_min_level: "info"

# 成功简报是否推送；false 时仅 warning/error 级别的简报照常推送
ntfy_daily_summary: true

# 静默时段，如 "23:00-08:00" 或 "12:00-13:00,23:00-08:00"；留空不启用
ntfy_quiet_hours: ""

# 是否启用每日受控提升；命令行 --no-boost 可临时关闭
# 白话：刷量总开关，false 就是只统计不刷，适合先观察几天。
boost_enabled: true

# ── 每日分界点 ────────────────────────────────────────────────────────────

# 两天的分界时刻（Asia/Shanghai，格式 HH:MM）。
# Steam 后台的“一天”不是本地 0 点切换：通常在北京时间早上 8 点（UTC 0 点）
# 才切到新的一天，因此 0:00~8:00 之间刷的浏览量会被计入前一天。
# 默认 08:00 与 Steam 统计对齐：凌晨执行 run 时会把台账、目标和“今天”
# 都算到前一天，避免刷错日期；若你的统计确实按自然日切换，改成 "00:00"。
# 白话：Steam 的“昨天”到早上 8 点才结束；这里决定几点之后算新的一天。
day_boundary: "08:00"

# ── 守护进程模式参数 ──────────────────────────────────────────────────────

# 每日执行时刻列表（可选，优先于 daily_time）。每个时刻都会完整执行一次每日流程：
# 后一时段会按当日已刷自动补剩余缺口，所以一天分两段刷量时填这里。
# 留空 [] 则使用下面的 daily_time 单时刻（行为与旧版本一致）。
# 白话：想每天跑两次就填 ["10:00", "20:00"]，只跑一次就留空。
daily_times: []

# 每日执行时刻（Asia/Shanghai，格式 HH:MM）——daily_times 留空时的回退单时刻。
# daemon 模式每天此时执行一次每日流程，且每次执行前都会重新加载
# 本配置文件——修改此处或任意配置均无需重启进程。
# 白话：只用单时刻时填这里；想一天多次就改上面的 daily_times。
daily_time: "10:00"

# 执行失败后当天重试的间隔（分钟）与最多重试次数（不含首次执行）。
# 失败不会把当天标记为已完成：重试用尽后等第二天，也可随时手动 run。
# 白话：10:00 失败后隔 30 分钟再试，最多再试 3 次；修好 Cookie 后会自动接上。
retry_delay_minutes: 30
retry_max_times: 3

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

# growth 模式下的目标系数起始值：1.5 表示目标 = 基准 × 1.5。
# stable 模式下该值不生效。目标系数不宜过大，否则曲线形态异常仍可能被
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

# 今日目标浏览量的上下界（单位：次；0 表示该方向不设界）。
# 当日总量 = 昨日自然流量 + 今日实际刷量，最终刷量会被夹进
# [target_min − 昨日自然, target_max − 昨日自然]：
# - target_max > 0：刷量后总量不得超过上限（绝对硬上限，抖动、刷量列表、
#   手动 boost 都不得突破；自然流量本身已超上限时只能不刷，总量仍会超出）；
# - target_min > 0：只要今天确实要刷量，就至少补到下限；自然流量已达标或
#   处于不刷状态（恢复期、boost_enabled=false、列表 0 等）时不会强行托底。
# 两者都为 0 时不设界（默认）。下限约束的是“决定下发的刷量”，不是平台最终
# 实际送达量：提升失败、容量/吞吐或 target_ratio 截断时，总量仍可能低于下限。
# run / daemon 实际执行与 forecast 预览使用同一口径。
# 注意：target_min 必须 <= target_max；倒置属于配置错误，run / daemon /
# forecast 会直接拒绝执行（退出码 1），不会刷量也不写库。
# 白话：想给每天刷到的浏览量封顶就填 target_max；怕一路跌就填 target_min
# 托底；填 0 表示不管。
target_min: 0
target_max: 0

# 缺口波动率：每天刷量配额在「缺口 × (1 ± 波动率)」内随机，使浏览曲线
# 在目标附近自然上下波动，避免收敛成一条笔直的线而显得不自然。0 = 关闭。
# 不设硬性上限；但抖动幅度会同步放大反作弊检测参照，配得过大时检出能力会
# 相应下降。抖动值按日期持久化：同一天多个时段复用同一配额。
# 白话：每天刷的量随机上下浮动 10%，让曲线更像真人、不像机器画的直线。
volatility: 0.10

# 缺口波动的最小幅度：抽取幅度在 [volatility_min, volatility] 内均匀取值、
# 方向随机，防止抽到接近 0 的抖动导致曲线过于平缓。0 = 不设下限。
volatility_min: 0.0

# 目标波动率：今日目标在「目标 × (1 ± target_volatility)」内随机，再按
# target_min / target_max 夹取后参与缺口计算。与缺口波动率不同，它按目标的
# 比例抖动，即使缺口很小也能让总浏览量产生明显起伏。0 = 关闭。
# 不设硬性上限；同样会放大反作弊检测参照。同一天重跑/多时段复用同一个目标。
# 白话：想让每天的目标浏览量大起大落（如 10000 上下浮动 50%）就填 0.50。
target_volatility: 0.0

# 目标波动的最小幅度：抽取幅度在 [target_volatility_min, target_volatility]
# 内均匀取值、方向随机；0 = 不设下限。
target_volatility_min: 0.0

# 相邻两天最小变动（比例，0~1，0 = 关闭）。
# 保证「今日目标」相对「上一逻辑日目标」至少变动该比例：若按预测算出的
# 变化更小，就沿原变化方向补足（变化为 0 时沿自然趋势方向），避免连续
# 几天目标几乎不变、曲线平得像一条直线。调整后仍受 target_min / target_max
# 夹取；只改写目标，不改变历史自然流量。上一日目标优先取 daily_plan 定档，
# 没有定档时用历史重算。forecast 预览与 run 使用同一口径。
# 白话：怕每天的目标几乎一样、曲线太假，就填 0.02（相邻两天至少差 2%）。
min_daily_change: 0.0

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

# 刷量列表用完后是否自动回退到基线/自动预测（默认 true = 用完自动回退）。
# 仅在“列表文件存在但已无有效行”时生效；文件缺失/读取失败属于配置错误，
# 即使开启也不会回退（仍会记为 warning）。
# 白话：默认列表刷完就继续按预测/基线自动刷；想用完即停就设为 false。
boost_list_fallback: true

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
# 没有内置上限，配多少开多少；但实际并发不会超过推荐数 boost_n 与 batch_size。
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

# 配置默认值的单一来源：直接解析上面的 init-config 模板。
# 用户配置文件里没写的键一律回退到这里的值，保证最小配置也能跑起来。
# boost_distribution / executor_cmd 是模板未列出的可选高级键，单独补默认值。
DEFAULT_CONFIG_VALUES: Dict[str, Any] = {
    **(yaml.safe_load(DEFAULT_CONFIG) or {}),
    "boost_distribution": "ratio",
    "executor_cmd": "",
}


def _with_defaults(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """用模板默认值补齐配置里没写的键。

    显式写 ``null`` 视为“没写”，回退默认值；显式空串 / 空列表 / 0 等
    有效值原样保留（例如 ``cookie_file: ""`` 表示主动关闭 Cookie）。
    """
    merged = dict(DEFAULT_CONFIG_VALUES)
    if cfg:
        merged.update({k: v for k, v in cfg.items() if v is not None})
    return merged


def _print_error(message: str) -> None:
    """向 stderr 打印错误信息。"""
    print(f"错误: {message}", file=sys.stderr)


def _check_target_bounds(cfg: Dict[str, Any]) -> Optional[str]:
    """校验 target_min <= target_max；返回错误信息，None 表示通过。

    ``target_min`` / ``target_max`` 是当日总量的硬边界，倒置属于配置错误，
    必须拒绝执行而不是静默调换。三个命令共用本函数，保证提示口径一致。
    """
    try:
        _normalize_target_bounds(
            float(cfg.get("target_min", 0) or 0),
            float(cfg.get("target_max", 0) or 0),
        )
    except ValueError as exc:
        return str(exc)
    return None


def _load_config(config_path: Path) -> Dict[str, Any]:
    """读取并解析 YAML 配置；没写的键回退到 init-config 模板默认值。

    文件缺失或解析失败时给出明确提示并退出。配置文件只写关心的键即可，
    其余键自动使用 ``DEFAULT_CONFIG_VALUES`` 中的默认值。
    """
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
    return _with_defaults(data if isinstance(data, dict) else {})


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


def _number(value: Any, default: float) -> float:
    """把配置值转换为浮点数；缺失或类型非法时回退默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _config_enabled(value: Any, default: bool = True) -> bool:
    """解析布尔配置；字符串 ``"false"`` / ``"0"`` / ``"no"`` / ``"off"`` 视为关闭。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(value)


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
    db_path = _resolve_path(cfg_dir, str(cfg["db_path"]))
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


def _build_notifier(cfg: Dict[str, Any], db_path: str) -> Notifier:
    """按配置构造 Notifier；队列文件放在数据库同级目录。"""
    cfg = _with_defaults(cfg)
    return Notifier(
        ntfy_url=str(cfg["ntfy_url"] or ""),
        ntfy_topic=str(cfg["ntfy_topic"] or ""),
        enabled=bool(cfg["ntfy_enabled"]),
        min_level=str(cfg["ntfy_min_level"] or "info").strip().lower(),
        daily_summary=bool(cfg["ntfy_daily_summary"]),
        quiet_hours=str(cfg["ntfy_quiet_hours"] or ""),
        queue_path=str(Path(db_path).parent / "notify_queue.json"),
    )


def _make_booster(
    cfg: Dict[str, Any],
    clan_id: str,
    cookie_file: str,
    proxy: Optional[str],
    storage: Storage,
) -> Booster:
    """按配置构造 Booster 实例。"""
    cfg = _with_defaults(cfg)
    return Booster(
        clan_id=clan_id,
        cookie_file=cookie_file,
        proxy=proxy,
        storage=storage,
        batch_size=int(cfg["batch_size"]),
        single_cap=int(cfg["single_cap"]),
        threads=int(cfg["threads"]),
        wait_min=float(cfg["wait_min"]),
        wait_max=float(cfg["wait_max"]),
        max_retries=int(cfg["max_retries"]),
        target_ratio=float(cfg["target_ratio"]),
        distribution=str(cfg["boost_distribution"]).lower(),
        day_boundary_minutes=resolve_day_boundary(cfg),
    )


def _scheduler_config(
    cfg: Dict[str, Any], boost_enabled: bool, dry_run: bool, cfg_dir: Path
) -> Dict[str, Any]:
    """把配置映射为调度器 run_daily 所需的 config 字典。

    缺失键统一回退到 init-config 模板默认值（与配置文件同源）。
    """
    cfg = _with_defaults(cfg)
    list_file = str(cfg["boost_list_file"] or "").strip()
    return {
        "window_days": int(cfg["window_days"]),
        "trend_cap": float(cfg["trend_cap"]),
        "startup_factor": float(cfg["startup_factor"]),
        "trend_min_points": int(cfg["trend_min_points"]),
        "outlier_k": float(cfg["outlier_k"]),
        "consecutive_days": int(cfg["consecutive_days"]),
        "absorb_days": int(cfg["absorb_days"]),
        "exclude_flagged": bool(cfg["exclude_flagged"]),
        "cooldown_days": int(cfg["cooldown_days"]),
        "baseline_days": int(cfg["baseline_days"]),
        "slope_days": int(cfg["slope_days"]),
        "slope_threshold": float(cfg["slope_threshold"]),
        "baseline_views": float(cfg["baseline_views"] or 0),
        "boost_mode": str(cfg["boost_mode"]).lower(),
        "growth_factor": float(cfg["growth_factor"] or 1.0),
        "growth_rate": float(cfg["growth_rate"] or 0),
        "target_min": float(cfg["target_min"] or 0),
        "target_max": float(cfg["target_max"] or 0),
        "volatility": float(cfg["volatility"] or 0),
        "volatility_min": float(cfg["volatility_min"] or 0),
        "target_volatility": float(cfg["target_volatility"] or 0),
        "target_volatility_min": float(cfg["target_volatility_min"] or 0),
        "min_daily_change": float(cfg["min_daily_change"] or 0),
        "boost_n": int(cfg["boost_n"]),
        "single_cap": int(cfg["single_cap"]),
        "batch_size": int(cfg["batch_size"]),
        "target_ratio": float(cfg["target_ratio"]),
        "boost_distribution": str(cfg["boost_distribution"]).lower(),
        "threads": int(cfg["threads"]),
        "wait_min": float(cfg["wait_min"]),
        "wait_max": float(cfg["wait_max"]),
        "boost_enabled": bool(boost_enabled),
        "dry_run": bool(dry_run),
        "boost_list_file": _resolve_path(cfg_dir, list_file) if list_file else "",
        "boost_list_fallback": bool(cfg["boost_list_fallback"]),
        "day_boundary_minutes": resolve_day_boundary(cfg),
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


def _refresh_cookie_quietly(
    cookie_file: str,
    clan_id: str,
    cfg: Dict[str, Any],
    *,
    force: bool,
    before_hours: float,
    dry_run: bool,
    proxy: Optional[str],
) -> Optional[RefreshOutcome]:
    """尽力续期统计 Cookie，任何失败都只记日志、绝不抛出。

    Args:
        cookie_file: 统计用 Cookie 文件；空串表示未配置，直接跳过。
        clan_id: 鉴赏家组 ID，用于构造刷新后的跳转地址。
        cfg: 配置字典（读取 ``cookie_refresh_enabled`` 与最小间隔）。
        force: 为 ``True`` 时跳过最小间隔限流（鉴权失败后的强制刷新）。
        before_hours: 仅在访问令牌该小时数内过期时刷新。
        dry_run: 演练模式，不读写任何 Cookie。
        proxy: 可选代理地址。

    Returns:
        真正调用刷新时的结果；跳过时返回 ``None``，失败时返回带
        ``reason="failed"`` 的结果（供调用方判断是否继续重试）。
    """
    if dry_run or not cookie_file:
        return None
    if not _config_enabled(cfg.get("cookie_refresh_enabled", True)):
        return None
    min_interval = _number(
        cfg.get("cookie_refresh_min_interval_hours", 6), 6.0
    )
    try:
        outcome = refresh_cookie_file(
            cookie_file,
            redir=STATS_URL.format(clan_id=clan_id),
            timeout=20.0,
            proxy=proxy,
            force=force,
            min_interval_hours=min_interval,
            expires_within_hours=None if force else before_hours,
        )
    except RefreshError as exc:
        level = (
            logging.INFO
            if exc.reason == "missing_refresh_token"
            else logging.WARNING
        )
        logger.log(level, "统计 Cookie 自动续期失败: %s", exc)
        return RefreshOutcome(
            False,
            "failed",
            load_cookie_state(cookie_file),
            None,
            needs_login=exc.needs_login,
            reason=exc.reason,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - 自动续期绝不能阻断主流程
        logger.warning("统计 Cookie 自动续期异常: %s", exc)
        return RefreshOutcome(
            False,
            "failed",
            load_cookie_state(cookie_file),
            None,
            needs_login=False,
            error=str(exc),
        )
    if outcome.refreshed:
        logger.info("统计 Cookie 已自动续期（%s）", cookie_file)
    else:
        logger.debug(
            "统计 Cookie 本次无需续期（%s）：%s", cookie_file, outcome.reason
        )
    return outcome


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

    # 执行前静默续期：在构造 Fetcher 之前刷新临近过期的统计 Cookie，
    # 让 Fetcher 直接读到刷新后的文件。dry-run / 关闭续期 / 未配置 Cookie
    # 时 helper 会立即返回，不产生任何副作用。
    before_hours = _number(cfg.get("cookie_refresh_before_hours", 12), 12.0)
    pre_outcome = _refresh_cookie_quietly(
        cookie_file,
        clan_id,
        cfg,
        force=False,
        before_hours=before_hours,
        dry_run=dry_run,
        proxy=proxy,
    )

    storage = Storage(db_path)
    fetcher = Fetcher(clan_id, cookie_file, proxy)
    notifier = _build_notifier(cfg, db_path)
    booster = _make_booster(
        cfg, clan_id, boost_cookie_file, proxy, storage
    )

    sched_cfg = _scheduler_config(cfg, boost_enabled, dry_run, cfg_dir)
    executor_cmd = str(cfg.get("executor_cmd", "") or "")
    # 单次 run 也补发静默期 backlog；dry-run 不发送，flush_pending 自身绝不抛异常。
    if not dry_run:
        notifier.flush_pending()
    try:
        return run_daily(
            storage,
            fetcher,
            notifier,
            booster,
            sched_cfg,
            executor_cmd=executor_cmd,
            config_path=config_path,
        )
    except StatsAuthError as exc:
        # 若执行前续期已经因终止性原因失败（刷新令牌失效 / 账号不匹配），
        # 本次执行不再强制刷新，避免同一执行内反复尝试同一个坏令牌。
        if (
            pre_outcome is not None
            and pre_outcome.reason == "failed"
            and pre_outcome.needs_login
        ):
            notifier.send(
                "统计 Cookie 失效，今日执行失败",
                f"{exc}\n\n"
                f"处理：python curator_views/cli.py login --config {config_path}\n"
                "重新登录后无需重启 daemon，下一次重试会自动读取新的 Cookie 文件。",
                level="error",
            )
            raise exc

        # Cookie 失效时先强制续期一次（绕过最小间隔），成功后用新的 Fetcher
        # 重试一次；Fetcher 在构造时读取 Cookie，刷新后必须重建实例。
        forced_outcome = _refresh_cookie_quietly(
            cookie_file,
            clan_id,
            cfg,
            force=True,
            before_hours=before_hours,
            dry_run=dry_run,
            proxy=proxy,
        )
        if (
            forced_outcome is not None
            and forced_outcome.reason == "failed"
            and (
                forced_outcome.needs_login
                or forced_outcome.failure_reason == "terminal"
            )
        ):
            # 终止性失败：不再重试 run_daily，一次执行最多尝试一次强制刷新。
            notifier.send(
                "统计 Cookie 失效，今日执行失败",
                f"{exc}\n\n"
                f"处理：python curator_views/cli.py login --config {config_path}\n"
                "重新登录后无需重启 daemon，下一次重试会自动读取新的 Cookie 文件。",
                level="error",
            )
            raise exc
        if forced_outcome is not None and forced_outcome.refreshed:
            retry_fetcher = Fetcher(clan_id, cookie_file, proxy)
            try:
                return run_daily(
                    storage,
                    retry_fetcher,
                    notifier,
                    booster,
                    sched_cfg,
                    executor_cmd=executor_cmd,
                    config_path=config_path,
                )
            except StatsAuthError as retry_exc:
                # 续期成功但重试仍失效：按原有告警逻辑处理，抛重试错误。
                exc = retry_exc
        # 续期不可用或重试仍失效：保持原有的“告警 + 重试机制兜底”行为。
        notifier.send(
            "统计 Cookie 失效，今日执行失败",
            f"{exc}\n\n"
            f"处理：python curator_views/cli.py login --config {config_path}\n"
            "重新登录后无需重启 daemon，下一次重试会自动读取新的 Cookie 文件。",
            level="error",
        )
        raise exc


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
    config_path = Path(args.config)
    # 硬边界校验必须在 _build_runtime 之前：缺少 clan_id 也不能掩盖倒置错误。
    cfg = _load_config(config_path)
    bounds_error = _check_target_bounds(cfg)
    if bounds_error:
        _print_error(bounds_error)
        return 1
    (
        cfg, _cfg_dir, _clan_id, _cookie_file,
        _boost_cookie_file, db_path, _proxy,
    ) = _build_runtime(config_path)
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
    """守护进程模式：常驻运行，每天到达配置时刻执行每日流程。

    每次循环重新加载配置文件（改配置/执行时刻无需重启）；
    支持一天多个执行时段（daily_times，如 ["10:00", "20:00"]），每个时段都
    完整执行一次每日流程，后一时段按当日已刷自动补剩余缺口；
    启动时若今天已过某个尚未执行的时段，立即补跑一次；
    单个时段执行失败不退出进程，按间隔在该时段内重试，用尽后等下一时段或明天。
    """
    config_path = Path(args.config)
    cfg = _load_config(config_path)
    # 启动即校验硬边界，避免进入循环后才发现配置错误。
    bounds_error = _check_target_bounds(cfg)
    if bounds_error:
        _print_error(bounds_error)
        return 1
    cfg_dir = config_path.resolve().parent
    db_path = _resolve_path(cfg_dir, str(cfg.get("db_path", "views.db")))
    log_path = _setup_logging(config_path, db_path, console=True)
    logger.info("守护进程日志文件: %s", log_path)

    def load_daily_times() -> Any:
        # 每次循环重载配置：优先 daily_times，留空则回退 daily_time 单时刻
        cfg = _load_config(config_path)
        return cfg.get("daily_times") or cfg.get("daily_time", "10:00")

    def read_db_last_run_marker() -> str:
        """从数据库读取最近一次完成标记（时段优先，其次日期）。

        daemon 成功执行后写 ``last_run_slot``（"YYYY-MM-DD#HH:MM"），
        手动 ``run`` 只写 ``last_run_date``（"YYYY-MM-DD"）。两者都查，
        优先返回更精确的时段标记。数据库或 kv_store 表不存在时返回空串，
        绝不因此阻塞守护进程启动。
        """
        if not Path(db_path).is_file():
            return ""
        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT key, value FROM kv_store "
                "WHERE key IN ('last_run_slot', 'last_run_date')"
            ).fetchall()
            values = {str(key): str(value) for key, value in rows}
            return pick_run_marker(
                values.get("last_run_slot", ""),
                values.get("last_run_date", ""),
            )
        except sqlite3.Error:
            logger.warning("读取数据库 last_run 标记失败", exc_info=True)
            return ""
        finally:
            if conn is not None:
                conn.close()

    def mark_slot_done(slot: str) -> None:
        """成功后把已完成的时段写入数据库，供重启/手动执行核对。"""
        try:
            Storage(str(db_path)).save_state("last_run_slot", slot)
        except (OSError, sqlite3.Error):
            logger.warning("写入数据库 last_run_slot 失败", exc_info=True)

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

    def load_retry_settings() -> tuple[float, int]:
        """每次失败后重载重试参数：改配置无需重启 daemon。"""
        cfg = _load_config(config_path)
        try:
            delay_minutes = float(cfg.get("retry_delay_minutes", 30) or 0)
        except (TypeError, ValueError):
            delay_minutes = 30.0
        try:
            max_times = int(cfg.get("retry_max_times", 3) or 0)
        except (TypeError, ValueError):
            max_times = 3
        return max(0.0, delay_minutes * 60.0), max(0, max_times)

    def flush_notifications() -> None:
        """daemon 每轮/心跳时补发静默期排队的通知（dry-run 不发送）。"""
        if args.dry_run:
            return
        cfg = _load_config(config_path)
        _build_notifier(cfg, db_path).flush_pending()

    state_path = status.default_state_path(db_path)
    daemon = Daemon(
        load_daily_times,
        execute_once,
        state_path=state_path,
        last_run_date_provider=read_db_last_run_marker,
        slot_writer=mark_slot_done,
        retry_settings_provider=load_retry_settings,
        pending_flusher=flush_notifications,
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
    bounds_text = ""
    if config.get("target_min") or config.get("target_max"):
        floor_text = (
            f"{config['target_min']:g}" if config.get("target_min") else "不设"
        )
        ceiling_text = (
            f"{config['target_max']:g}" if config.get("target_max") else "不设"
        )
        bounds_text = f"；目标上下界=[{floor_text}, {ceiling_text}]"
    # 兼容手工构造的报告：缺失 daily_times 时回退到单个 daily_time
    daily_times = config.get("daily_times") or [
        config.get("daily_time", "10:00")
    ]
    print(
        f"配置: 每日执行 {', '.join(daily_times)}；{boost_text}；"
        f"boost_mode={config['boost_mode']}{bounds_text}；"
        f"每日分界 {config.get('day_boundary', DEFAULT_DAY_BOUNDARY)}"
    )

    if config.get("boost_list_file"):
        labels = {
            "active": "使用中",
            "empty": "已用完",
            "missing": "文件缺失",
            "error": "读取失败",
        }
        label = labels.get(config.get("boost_list_status", ""), "未知")
        if (
            config.get("boost_list_status") == "empty"
            and config.get("boost_list_fallback")
        ):
            label += "，已启用回退基线"
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
            "daily_times": parse_daily_times(
                cfg.get("daily_times") or cfg.get("daily_time", "10:00")
            ),
            "boost_enabled": bool(cfg.get("boost_enabled", True)),
            "boost_mode": str(cfg.get("boost_mode", "stable")).lower(),
            "target_min": float(cfg.get("target_min", 0) or 0),
            "target_max": float(cfg.get("target_max", 0) or 0),
            "db_path": db_path,
            "boost_list_file": list_path,
            "boost_list_remaining": list_remaining,
            "boost_list_status": list_status,
            "boost_list_fallback": bool(cfg.get("boost_list_fallback", False)),
            "day_boundary": format_day_boundary(
                resolve_day_boundary(cfg)
            ),
        },
        "data": _collect_info_data(db_path),
    }

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_info_report(report)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """抓取统计数据写入本地库，然后打印最近 7 天与预测摘要。

    只写 ``daily_stats``（与 ``run`` 的抓取入库一致），不写 ``boost_log``、
    ``program_start`` / ``last_run_date`` / growth 锚点，因此不会被 daemon
    误判为“今天已执行”。``--no-write`` 可恢复旧的纯内存只读行为。
    """
    (
        cfg, cfg_dir, clan_id, cookie_file,
        _boost_cookie_file, db_path, proxy,
    ) = _build_runtime(Path(args.config))
    _check_cookie_file(cookie_file, "统计 Cookie")

    fetcher = Fetcher(clan_id, cookie_file, proxy)
    try:
        history = fetcher.fetch_stats()
    except StatsAuthError as exc:
        _print_error(str(exc))
        _print_error(
            "提示: 运行 `python curator_views/cli.py login --config "
            f"{args.config}` 重新登录后再试。"
        )
        return 1
    history = sorted(history, key=lambda entry: entry["date_ts"])

    print(f"共获取到 {len(history)} 天的统计数据。")

    if history:
        latest = history[-1]
        latest_date = datetime.fromtimestamp(
            int(latest["date_ts"]), tz=_TZ
        ).strftime("%Y-%m-%d")
        written = (
            f"（{len(history)} 天，最新 {latest_date} = "
            f"{int(latest['num_views']):,} 次）"
        )
    else:
        written = "（0 天）"

    if getattr(args, "no_write", False):
        print("只读模式: 未写入数据库（--no-write）。")
    else:
        try:
            storage = Storage(db_path)
            for entry in history:
                storage.upsert_daily_stats(
                    int(entry["date_ts"]), int(entry["num_views"])
                )
        except sqlite3.Error as exc:
            _print_error(f"写入数据库失败: {exc}")
            return 1
        print(f"已写入数据库: {db_path}{written}")

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
    # 手动 boost 没有昨日自然流量上下文，无法校验 target_min 下限；
    # 但 target_max 是刷量的绝对上限，这里必须夹住，避免手动请求突破窗口。
    target_max = float(cfg.get("target_max", 0) or 0)
    target = args.target
    clamped = False
    if target_max > 0 and target > target_max:
        target = int(target_max)
        clamped = True
        print(
            f"警告: --target {args.target} 超过 target_max={target_max:g}，"
            f"已按 {target} 执行（上限是刷量绝对上限；target_min 需结合"
            "昨日自然流量，手动 boost 无法校验）。",
            file=sys.stderr,
        )
    boosted = booster.run(target, apps, args.dry_run)

    summary = {
        "target": target,
        "requested_target": args.target,
        "clamped": clamped,
        "apps": apps,
        "dry_run": bool(args.dry_run),
        "boosted": boosted,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cookie_redirect_hint(cookie: str, reason: str) -> str:
    """为“被重定向到公开页”的校验失败生成出口 IP 排查提示。

    Steam 的 ``steamLoginSecure`` 与登录时的出口 IP 绑定。Chromium 会自动
    使用系统代理，而程序只使用配置里的 ``proxy``；两者出口 IP 不一致时，
    校验和后续抓取都会被重定向。提示里会带上令牌绑定的出口 IP 便于核对。

    Args:
        cookie: 刚导出的 Cookie 字符串。
        reason: ``validate_cookie`` 返回的失败原因。

    Returns:
        提示文本；原因与重定向无关时返回空串。
    """
    if "重定向" not in reason:
        return ""
    token_ip = cookie_token_ip(cookie)
    hint = (
        "提示: Steam 登录令牌与出口 IP 绑定。Chromium 会自动使用系统代理，"
        "而程序只使用配置里的 proxy；两者出口 IP 不一致时后台会重定向到"
        "公开页。若浏览器走代理，请在配置文件中填写相同的 proxy（如 "
        "http://127.0.0.1:10279）后重试。"
    )
    if token_ip:
        hint += f" 本次令牌绑定的出口 IP: {token_ip}。"
    return hint


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
            hint = _cookie_redirect_hint(cookie, result.reason)
            if hint:
                _print_error(hint)
            _print_error("已放弃写入，避免覆盖原有 Cookie 文件。")
            return 1

    write_cookie_file(cookie_file, cookie)
    # 若导出的 Cookie 含长期刷新令牌，顺手写入 sidecar，供后续 run/daemon
    # 自动续期，无需再手动执行 refresh-cookie。
    refresh_value = extract_refresh_cookie(cookie)
    if refresh_value:
        steamid, _refresh_jwt = split_refresh_cookie(refresh_value)
        try:
            save_cookie_state(
                cookie_file,
                CookieState(steamid=steamid, refresh_cookie=refresh_value),
            )
        except OSError as exc:
            logger.warning(
                "保存刷新令牌失败（自动续期可能不可用）: %s", exc
            )
        else:
            logger.info("已保存刷新令牌，后续可自动续期统计 Cookie。")
    else:
        # 新 Cookie 不含刷新令牌时必须清掉旧 sidecar，否则后续自动续期会
        # 用上一个账号的刷新令牌覆盖这份全新登录。
        try:
            Path(session_state_path(cookie_file)).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理旧刷新令牌状态失败: %s", exc)
        else:
            logger.info("已清理旧的刷新令牌状态（新 Cookie 不含刷新令牌）。")
        logger.info(
            "登录 Cookie 不含 steamRefresh_steam，自动续期暂不可用；"
            "需使用携带刷新令牌的交互式登录。"
        )
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


def cmd_refresh_cookie(args: argparse.Namespace) -> int:
    """用 ``steamRefresh_steam`` 手动续期统计 Cookie。

    读取 Cookie 文件（或 sidecar）中的长期刷新令牌，换取新的 store 访问令牌，
    原子改写 Cookie 文件并更新 sidecar；成功后可选校验并打印 JSON 摘要。
    输出绝不包含 Cookie / 令牌内容。
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
    clan_id = str(cfg.get("clan_id") or "").strip()
    proxy = cfg.get("proxy") or None
    redir = STATS_URL.format(clan_id=clan_id or "0")
    min_interval = _number(
        cfg.get("cookie_refresh_min_interval_hours", 6), 6.0
    )
    try:
        outcome = refresh_cookie_file(
            cookie_file,
            redir=redir,
            timeout=args.timeout,
            proxy=proxy,
            force=args.force,
            min_interval_hours=min_interval,
        )
    except (RefreshError, OSError) as exc:
        print(
            json.dumps(
                {
                    "mode": "refresh",
                    "cookie_file": cookie_file,
                    "clan_id": clan_id,
                    "refreshed": False,
                    "valid": False,
                    "reason": str(exc),
                    "skip_reason": "",
                    "expires_at": "",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        _print_error(str(exc))
        _print_error(
            "提示: 运行 `python curator_views/cli.py login --config "
            f"{args.config}` 重新登录后再试。"
        )
        return 1

    cookie = read_cookie_file(cookie_file)
    valid: Optional[bool]
    if clan_id:
        check = validate_cookie(cookie, clan_id, proxy=proxy)
        valid = check.valid
        reason = check.reason
    else:
        valid = None
        reason = "未配置 clan_id，跳过有效性校验"

    expires_ts = cookie_expires_at(cookie)
    expires_at = (
        datetime.fromtimestamp(expires_ts, tz=_TZ).isoformat()
        if expires_ts > 0
        else ""
    )
    summary = {
        "mode": "refresh",
        "cookie_file": cookie_file,
        "clan_id": clan_id,
        "refreshed": bool(outcome.refreshed),
        "valid": valid,
        "reason": reason,
        "skip_reason": "" if outcome.refreshed else outcome.reason,
        "expires_at": expires_at,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if valid is False else 0


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


def _read_forecast_db(
    db_path: str,
) -> tuple[list[dict], Dict[str, str], Dict[str, int], Dict[str, Dict]]:
    """以只读 URI 打开数据库，返回 (daily_stats, kv_store, boost_totals, plans)。

    ``boost_totals`` 按日期（YYYY-MM-DD）汇总 boost_log，供默认自然口径
    扣除基准日自己刷的量；旧库没有 boost_log 时返回空字典。
    ``plans`` 以日期（YYYY-MM-DD）为键存放 daily_plan 定档行，供首日复用；
    旧库没有 daily_plan 表时返回空字典。

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
        boost_totals: Dict[str, int] = {}
        try:
            for date, total in conn.execute(
                "SELECT date, COALESCE(SUM(count), 0) AS total "
                "FROM boost_log GROUP BY date"
            ):
                boost_totals[str(date)] = int(total)
        except sqlite3.OperationalError:
            pass
        plans: Dict[str, Dict] = {}
        try:
            cursor = conn.execute("SELECT * FROM daily_plan")
            columns = [desc[0] for desc in cursor.description]
            for row in cursor.fetchall():
                plan = dict(zip(columns, row))
                plans[str(plan["date"])] = plan
        except sqlite3.OperationalError:
            # 旧库没有 daily_plan 表：按“无定档”处理，不视为错误。
            plans = {}
    finally:
        conn.close()
    return history, kv, boost_totals, plans


def _forecast_start_epoch(
    boundary_minutes: int = parse_day_boundary(DEFAULT_DAY_BOUNDARY),
    now: Optional[datetime] = None,
) -> int:
    """--tomorrow 的预测首日：逻辑明天 00:00（Asia/Shanghai）。

    默认从逻辑今天起算；逻辑日由 ``day_boundary`` 决定（默认 08:00 分界），
    因此凌晨执行时“今天”仍是前一天。Steam 当天统计要次日才完整，所以预测
    今天仍是预测。
    """
    now_dt = now or datetime.now(_TZ)
    tomorrow_iso = logical_date(
        now_dt + timedelta(days=1), int(boundary_minutes)
    )
    return _date_to_epoch(tomorrow_iso)


def _display_width(text: str) -> int:
    """按终端显示宽度计算字符串宽度（CJK 全角字符占 2 列）。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in str(text)
    )


def _pad_display(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格，保证中英文混排表格对齐。"""
    text = str(text)
    padding = max(0, width - _display_width(text))
    if align == "right":
        return " " * padding + text
    if align == "center":
        left = padding // 2
        return " " * left + text + " " * (padding - left)
    return text + " " * padding


def _sample_forecast_rows(
    rows: list[Dict[str, Any]], step: int
) -> list[Dict[str, Any]]:
    """按间隔抽样展示行；首尾始终保留，避免漏掉预测期末日。"""
    if step <= 1 or len(rows) <= 1:
        return rows
    sampled = rows[::step]
    if (len(rows) - 1) % step:
        sampled.append(rows[-1])
    return sampled


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
    today_mode = source.get("perspective") == "today"
    natural_basis = source.get("natural_basis")
    print(
        f"预测区间: {summary['first_date']} ~ {summary['last_date']}"
        f"（{summary['days']} 天"
        f"{'，含今天' if today_mode else '，从明天起'}）"
    )
    day_step = int(source.get("day_step", 1) or 1)
    if day_step > 1:
        print(
            f"输出间隔: 每 {day_step} 天（表格仅显示抽样行；"
            "汇总与警告仍按完整区间）"
        )
    source_text = "Steam 在线抓取" if fetched else f"本地数据库 {db_path}"
    print(
        f"数据来源: {source_text}（{source['history_days']} 天，"
        f"最新 {source['latest_date']} = {source['latest_views']:,} 次）"
    )
    if natural_basis == "natural":
        print(
            f"自然口径: 基准日 {source['baseline_date']} 实际 "
            f"{source['yesterday_actual']:,} - 刷量 "
            f"{source['yesterday_boosted']:,} "
            f"= 自然 {source['yesterday_natural']:,}；未来日假设自然不变"
        )
    if source.get("today_boosted") is not None:
        print(
            f"今日已刷: {source['today_boosted']:,} 次"
            f"（{summary['first_date']}，来自 boost_log；"
            "首日缺口已按剩余量计算）"
        )
        plan_status = source.get("today_plan_status", "missing")
        if plan_status == "persisted":
            print(
                f"今日目标: 已定档 {rows[0]['target']:,.1f}"
                f"（run 于 {source.get('today_plan_updated_at')} 写入数据库；"
                "首日不再重新抽样）"
            )
        elif plan_status == "signature_mismatch":
            print(
                "今日目标: 配置已变更，数据库里的定档目标已失效，"
                "本次重新抽样（run 也会重新抽）"
            )
        elif plan_status == "ignored_no_volatility":
            print(
                "今日目标: --no-volatility 生效，忽略数据库定档值，"
                "输出未抖动基准"
            )
        else:
            print(
                "今日目标: 数据库无定档记录（今天还没跑过 run），"
                "本次为抽样预测"
            )
    baseline = float(config.get("baseline_views", 0) or 0)
    baseline_text = "auto" if baseline <= 0 else f"{baseline:g}"
    print(
        f"参数: boost_mode={config.get('boost_mode', 'stable')} "
        f"factor={float(config.get('growth_factor', 1.0)):g} "
        f"rate={float(config.get('growth_rate', 0)):g}"
    )
    print(
        f"      基线={baseline_text} "
        f"自然模型={source.get('natural_mode', 'natural')} "
        f"缺口波动率={float(config.get('volatility', 0)):.2f}"
        f"（最小 {float(config.get('volatility_min', 0) or 0):.2f}） "
        f"目标波动率={float(config.get('target_volatility', 0) or 0):.2f}"
        f"（最小 {float(config.get('target_volatility_min', 0) or 0):.2f}）"
    )
    target_vol = float(config.get("target_volatility", 0) or 0)
    gap_vol = float(config.get("volatility", 0) or 0)
    modeled = bool(summary.get("volatility_modeled", True))
    if target_vol > 0 or gap_vol > 0:
        if modeled and source.get("today_plan_status") == "persisted":
            volatility_text = "已纳入预测（首日取数据库定档值；未来日独立抽样）"
        elif modeled:
            volatility_text = (
                "已纳入预测（每天独立抽样；与 run 当日持久化值可能不同）"
            )
        else:
            volatility_text = "未纳入预测（--no-volatility，输出未抖动基准）"
        print("波动率: " + volatility_text)
    if target_vol > 0:
        print(
            "目标抖动区间: 表中「目标」±"
            f"{target_vol:.0%}（最小幅度 "
            f"{float(config.get('target_volatility_min', 0) or 0):.0%}），"
            "再按上下界夹取；端点见 target_low / target_high"
        )
    boost_n = max(0, int(config.get("boost_n", 5)))
    single_cap = max(0, int(config.get("single_cap", 300)))
    batch_size = int(config.get("batch_size", 50))
    capacity = boost_n * single_cap
    target_min = float(config.get("target_min", 0) or 0)
    target_max = float(config.get("target_max", 0) or 0)
    if target_min > 0 or target_max > 0:
        floor_text = f"{target_min:g}" if target_min > 0 else "不设"
        ceiling_text = f"{target_max:g}" if target_max > 0 else "不设"
        print(
            f"目标上下界: 下限={floor_text} 上限={ceiling_text}"
            "（0=不设界；当日总量=自然+刷量，超出时按边界夹取）"
        )
        bound_hits = summary.get("bound_hits") or {}
        min_hits = int(bound_hits.get("min", 0) or 0)
        max_hits = int(bound_hits.get("max", 0) or 0)
        if min_hits or max_hits:
            print(
                f"目标边界: 已按 target_min/target_max 夹取"
                f"（下限命中 {min_hits} 天，上限命中 {max_hits} 天；"
                "上限=自然流量+刷量）"
            )
    min_change = float(config.get("min_daily_change", 0) or 0)
    if min_change > 0:
        print(
            f"相邻两天最小变动: ≥{min_change:.0%}"
            "（今日目标相对上一逻辑日目标至少变动该比例；仍受上下界夹取）"
        )
    print(
        f"单日容量: {capacity:,} 次"
        f"（boost_n={boost_n} × single_cap={single_cap}）"
    )
    threads = int(config.get("threads", 1))
    wait_min = float(config.get("wait_min", 8.0))
    wait_max = float(config.get("wait_max", 16.0))
    throughput = _daily_throughput_capacity(
        threads, wait_min, wait_max, boost_n, batch_size
    )
    if throughput is None:
        print("单日吞吐上限: 不限（等待间隔为 0）")
    else:
        print(
            f"单日吞吐上限: {throughput:,} 次"
            f"（threads={threads}，实际并发≤"
            f"{_effective_threads(threads, boost_n, batch_size)}，"
            f"最大间隔={max(wait_min, wait_max):g}s，未计请求耗时）"
        )
    print()

    header = [
        "日期", "基准", "目标系数", "目标", "假设自然", "今日已刷", "缺口", "备注",
    ]
    aligns = [
        "left", "right", "right", "right", "right", "right", "right", "left",
    ]
    body: list[list[str]] = []
    for row in rows:
        if row["need"] <= 0:
            note = ""
        else:
            notes = []
            if capacity <= 0:
                notes.append("无容量")
            elif row["need"] > capacity:
                notes.append("容量不足")
            if throughput is not None and row["need"] > throughput:
                notes.append("吞吐不足")
            note = "/".join(notes)
        body.append(
            [
                row["date"],
                f"{row['base']:,.1f}",
                f"{row['multiplier']:.3f}",
                f"{row['target']:,.1f}",
                f"{row['assumed_natural']:,}",
                (
                    f"{row['already_boosted']:,}"
                    if row["date"] == summary["first_date"]
                    and source.get("today_boosted") is not None
                    else "-"
                ),
                f"{row['need']:,}",
                note,
            ]
        )

    widths = [_display_width(item) for item in header]
    for cells in body:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], _display_width(cell))

    def render(cells: list[str]) -> str:
        return "  ".join(
            _pad_display(cell, width, align)
            for cell, width, align in zip(cells, widths, aligns)
        )

    print(render(header))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for cells in body:
        print(render(cells))
    print()
    total_need = int(summary["total_need"])
    days = max(1, int(summary["days"]))
    print(
        f"汇总: {days} 天预计刷量 {total_need:,}"
        f"（日均 {total_need / days:,.0f}）；"
        f"目标 {summary['target_first']:,.1f} → {summary['target_last']:,.1f}"
    )
    if natural_basis == "natural":
        print(
            "说明: 自然口径复用 run 的缺口公式（缺口 = 目标 - 假设自然 - 今日已刷，"
            "仅首日扣已刷）；不含反作弊溢出扣减、恢复期与刷量列表；"
            "波动率默认纳入预测（--no-volatility 可关闭）。"
        )
    if result["warnings"]:
        print("提示:")
        for warning in result["warnings"]:
            print(f"  - [{warning['code']}] {warning['message']}")
    if source.get("chart_path"):
        print(f"折线图: {source['chart_path']}")


def cmd_forecast(args: argparse.Namespace) -> int:
    """只读推算未来 N 天的目标、假设自然流量与刷量缺口。

    默认只读本地数据库、不触网；--fetch 才在线抓取（仅在内存使用，不写库）。
    --set 可覆盖任意配置键做 what-if；数据库不存在或无历史时以 1 退出，绝不建库。
    波动率默认纳入预测（每天独立抽样）；--no-volatility 关闭随机化，输出基准值。
    """
    if args.days < 1:
        _print_error("--days 必须 >= 1")
        return 1

    chart_only = bool(getattr(args, "chart_only", False))
    day_step = getattr(args, "day_step", 1)
    if chart_only and args.json:
        _print_error("--chart-only 不能与 --json 同用")
        return 1
    if day_step < 1:
        _print_error("--day-step 必须 >= 1")
        return 1

    config_path = Path(args.config)
    cfg = _load_config(config_path)
    cfg_dir = config_path.resolve().parent
    _apply_overrides(cfg, getattr(args, "set", None) or [])

    # --set 覆盖后同样校验硬边界，倒置时拒绝执行且不输出任何部分 JSON。
    bounds_error = _check_target_bounds(cfg)
    if bounds_error:
        _print_error(bounds_error)
        return 1

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
        history, kv, boost_totals, plans = _read_forecast_db(db_path)
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
        try:
            fetched = fetcher.fetch_stats()
        except StatsAuthError as exc:
            _print_error(str(exc))
            _print_error(
                "提示: 运行 `python curator_views/cli.py login --config "
                f"{args.config}` 重新登录后再试，或去掉 --fetch 只用本地库。"
            )
            return 1
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

    # 每日分界点：Steam 统计通常在北京时间 08:00 切换“新的一天”，凌晨执行
    # 时仍算前一天。forecast 的“今天/明天”与 run_daily 使用同一口径。
    boundary_minutes = resolve_day_boundary(cfg)
    today_ts = _today_epoch(boundary_minutes)
    today_iso = logical_date(datetime.now(_TZ), boundary_minutes)

    # 程序启动日同样只读；缺失或非法时不过滤（从最早一条开始）。
    # 调整 day_boundary 后旧启动日可能“晚于”逻辑今天，此时放宽一天，
    # 避免把所有历史都过滤掉。
    program_start = kv.get("program_start", "").strip()
    if program_start:
        try:
            program_start_epoch = _date_to_epoch(program_start)
        except (ValueError, IndexError):
            program_start_epoch = 0
        if program_start_epoch > today_ts:
            program_start_epoch = today_ts - 86400
        if program_start_epoch:
            history = [
                item for item in history
                if item["date_ts"] >= program_start_epoch
            ]

    if not history:
        _print_error("数据库中没有历史统计数据（或 program_start 之后无数据）。")
        return 1

    # 默认从今天起算：今天的统计要明天才完整，所以“预测今天”仍然是预测。
    # 默认自然口径 = 最近一个完整日（今天之前）的实际 − 当日刷量，与
    # run_daily 的缺口公式一致；未来日假设自然流量不变（刷量不算自然），
    # 因此每天的缺口都会重新按 target − 自然流量 计算。
    # --natural trend|target 可显式切回旧的“实际趋势外推 / 收敛到目标”模型。
    start_tomorrow = bool(getattr(args, "tomorrow", False))
    start_ts = (
        _forecast_start_epoch(boundary_minutes) if start_tomorrow else today_ts
    )
    natural_mode_arg = getattr(args, "natural", None)
    use_natural_basis = natural_mode_arg is None

    prior = [
        item for item in history
        if day_label_from_ts(item["date_ts"]) < today_iso
    ]
    if not prior:
        _print_error(
            "预测需要今天之前至少一天的历史数据"
            f"（按 day_boundary={format_day_boundary(boundary_minutes)} "
            f"计算的今天为 {today_iso}）。"
        )
        return 1
    first_natural: Optional[int] = None
    yesterday_actual: Optional[int] = None
    yesterday_boosted: Optional[int] = None
    baseline_date: Optional[str] = None
    stale_warning: Optional[str] = None
    if use_natural_basis:
        baseline_row = prior[-1]
        baseline_date = day_label_from_ts(int(baseline_row["date_ts"]))
        yesterday_actual = int(baseline_row["num_views"])
        yesterday_boosted = int(boost_totals.get(baseline_date, 0))
        first_natural = max(0, yesterday_actual - yesterday_boosted)
        expected_date = previous_date(today_iso)
        if baseline_date < expected_date:
            stale_warning = (
                f"本地库最新只到 {baseline_date}，早于昨天 {expected_date}；"
                "自然缺口按该日数据计算，建议加 --fetch 或先运行 run。"
            )
    # 两个视角都只使用“日期标签早于逻辑今天”的完整日：逻辑今天的统计
    # 尚未完整（Steam 在 day_boundary 才换日），不能当作一整天参与预测。
    history = prior

    # 今日已刷：只影响首日缺口，重跑/中断后按剩余量续刷。
    # 明天视角不需要它（明天会从“昨日自然流量”里自动扣掉今天刷的量）。
    today_boosted = (
        0 if start_tomorrow else max(0, int(boost_totals.get(today_iso, 0)))
    )

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

    # 相邻两天最小变动的首日参照：上一逻辑日的最终目标（daily_plan 定档）。
    # 没有定档时留 None，首日不套用，从第二行起按上一行目标继续约束。
    prev_target: Optional[float] = None
    prev_day_iso = day_label_from_ts(int(start_ts) - 86400)
    prev_plan = plans.get(prev_day_iso) if plans else None
    if prev_plan and prev_plan.get("target"):
        try:
            prev_target = float(prev_plan["target"])
        except (TypeError, ValueError):
            prev_target = None

    result = forecast_days(
        history,
        sched_cfg,
        start_ts,
        days=args.days,
        growth_start_ts=growth_start_ts,
        natural_mode=natural_mode_arg or "trend",
        first_natural=first_natural,
        first_day_boosted=today_boosted,
        model_volatility=bool(getattr(args, "model_volatility", True)),
        first_day_plan=(
            plans.get(today_iso) if not start_tomorrow else None
        ),
        prev_target=prev_target,
    )
    if stale_warning:
        result["warnings"].append(
            {"code": "stale_history", "message": stale_warning}
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
        "natural_mode": natural_mode_arg or "natural",
        "natural_basis": "natural" if use_natural_basis else natural_mode_arg,
        "perspective": "tomorrow" if start_tomorrow else "today",
        "day_boundary": format_day_boundary(boundary_minutes),
        "today": today_iso,
        "baseline_date": baseline_date,
        "yesterday_actual": yesterday_actual,
        "yesterday_boosted": yesterday_boosted,
        "yesterday_natural": first_natural,
        "today_boosted": None if start_tomorrow else today_boosted,
        "volatility_modeled": bool(getattr(args, "model_volatility", True)),
        "today_plan_status": result["summary"].get(
            "first_plan_status", "missing"
        ),
        "today_plan_updated_at": result["summary"].get(
            "first_plan_updated_at"
        ),
        "day_step": day_step,
    }
    if day_step > 1:
        result = {
            **result,
            "rows": _sample_forecast_rows(result["rows"], day_step),
        }
    chart_arg = getattr(args, "chart", None)
    if chart_only and chart_arg is None:
        chart_arg = "-"
    console_chart: Optional[str] = None
    if chart_arg == "-":
        console_chart = render_forecast_console(
            result,
            sched_cfg,
            source,
            include_meta=not chart_only,
        )
        source["chart_format"] = "console"
    elif chart_arg:
        chart_target = _resolve_path(cfg_dir, str(chart_arg))
        try:
            chart_path = write_forecast_chart(
                chart_target, result, sched_cfg, source
            )
        except OSError as exc:
            _print_error(f"生成折线图失败: {exc}")
            return 1
        source["chart_path"] = str(chart_path)
        source["chart_format"] = "html"
    if args.json:
        payload = {
            "rows": result["rows"],
            "warnings": result["warnings"],
            "summary": result["summary"],
            "source": source,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if console_chart:
            print(console_chart, file=sys.stderr)
        return 0

    if chart_only:
        if console_chart:
            print(console_chart)
        return 0

    _print_forecast_report(result, sched_cfg, source, args.fetch, db_path)
    if console_chart:
        print()
        print(console_chart)
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

    p_check = sub.add_parser(
        "check", help="抓取统计并写入本地库，打印最近 7 天与预测摘要"
    )
    p_check.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_check.add_argument("--no-write", action="store_true",
                         help="只抓取并打印，不写数据库（旧行为）")
    p_check.set_defaults(func=cmd_check)

    p_boost = sub.add_parser("boost", help="手动执行一轮受控提升")
    p_boost.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_boost.add_argument("--target", type=int, required=True,
                         help="本轮目标提升次数（必填）；超过 target_max 时按上限执行，"
                              "target_min 需结合昨日自然流量，手动 boost 无法校验")
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

    p_refresh = sub.add_parser(
        "refresh-cookie", help="用 steamRefresh_steam 手动续期统计 Cookie"
    )
    p_refresh.add_argument("--config", default="config.yaml",
                           help="配置文件路径（默认 config.yaml）")
    p_refresh.add_argument("--cookie-file", default=None,
                           help="要续期的 Cookie 文件路径（默认取配置 cookie_file）")
    p_refresh.add_argument("--force", action="store_true",
                           help="忽略最小续期间隔，立即续期")
    p_refresh.add_argument("--timeout", type=float, default=20.0,
                           help="单次 HTTP 请求超时秒数（默认 20）")
    p_refresh.set_defaults(func=cmd_refresh_cookie)

    p_export = sub.add_parser("export", help="导出每日统计数据")
    p_export.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p_export.add_argument("--format", choices=["json", "csv"], default="json",
                          help="导出格式（默认 json）")
    p_export.add_argument("--out", default=None, help="输出文件路径（缺省打印到标准输出）")
    p_export.set_defaults(func=cmd_export)

    p_forecast = sub.add_parser(
        "forecast", help="只读预测今天/明天起 N 天的目标/自然流量/缺口（不写库）"
    )
    p_forecast.add_argument("--config", default="config.yaml",
                            help="配置文件路径（默认 config.yaml）")
    p_forecast.add_argument("--days", type=int, default=7,
                            help="预测天数（默认 7，必须 >= 1）")
    p_forecast.add_argument("--fetch", action="store_true",
                            help="额外从 Steam 在线抓取最新统计（仅内存，不写库）")
    p_forecast.add_argument("--natural", choices=["trend", "target"],
                            default=None,
                            help="自然流量模型：默认 natural（最近完整日实际 - "
                                 "刷量，未来日不变，与 run 缺口口径一致）；"
                                 "trend 按实际趋势外推 / target 收敛到目标")
    start_group = p_forecast.add_mutually_exclusive_group()
    start_group.add_argument("--today", action="store_true",
                             help="从今天起算（默认；显式指定行为相同）")
    start_group.add_argument("--tomorrow", action="store_true",
                             help="从明天起算（旧默认行为）")
    p_forecast.add_argument("--set", action="append", default=[],
                            metavar="KEY=VALUE",
                            help="覆盖配置项做 what-if，可重复")
    p_forecast.add_argument(
        "--no-volatility", dest="model_volatility", action="store_false",
        default=True,
        help="不把目标/缺口波动率纳入预测（输出未抖动基准）；默认纳入，"
             "每天独立抽样",
    )
    p_forecast.add_argument(
        "--chart", nargs="?", const="-", default=None,
        metavar="PATH",
        help="输出控制台点状折线图；带 PATH 时写 HTML 文件（相对路径基于配置文件目录，"
             "'-' 也表示控制台）",
    )
    p_forecast.add_argument(
        "--day-step", type=int, default=1, metavar="DAYS",
        help="表格与图每隔 DAYS 天输出一次（默认 1，即每天）",
    )
    p_forecast.add_argument(
        "--chart-only", action="store_true",
        help="只输出折线图本身，不打印 forecast 表格、说明与汇总；未指定 --chart 时默认控制台",
    )
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
