# CuratorViewsGuard — Steam 鉴赏家浏览量预测与受控提升系统

CuratorViewsGuard 是一个运行在本地的 Python 命令行工具，用于维护 Steam 鉴赏家组
每日浏览量的平稳曲线：它抓取历史统计、预测每天应有的浏览量水平，再以限速、分批、
可审计的方式补齐缺口。系统同时内置反作弊检测，识别外部恶意刷量造成的异常尖峰，
自动分摊溢出量并进入恢复期，尽量降低被 Steam 风控误判的概率。

本说明书覆盖：核心概念、算法原理、完整配置项、全部命令、部署方式、数据存储、
排障与升级注意事项。

## 目录

1. [项目简介](#1-项目简介)
2. [核心概念与术语](#2-核心概念与术语)
3. [工作原理](#3-工作原理)
4. [项目结构](#4-项目结构)
5. [安装](#5-安装)
6. [快速开始](#6-快速开始)
7. [配置参考](#7-配置参考)
8. [命令参考](#8-命令参考)
9. [典型使用场景](#9-典型使用场景)
10. [定时调度](#10-定时调度)
11. [数据、日志与状态](#11-数据日志与状态)
12. [排障 FAQ](#12-排障-faq)
13. [升级与迁移](#13-升级与迁移)
14. [安全与合规](#14-安全与合规)
15. [开发与测试](#15-开发与测试)
16. [免责声明](#16-免责声明)

---

## 1. 项目简介

### 1.1 它解决什么问题

- **让浏览曲线保持平稳**：浏览量忽高忽低（尖峰、断崖）容易被平台风控判定为机器人
  行为。系统根据历史数据计算每天应补多少量，让曲线稳定在合理区间。
- **抵御他人恶意刷量**：其他组可能通过访问你的推荐页面把浏览量刷虚高，污染你的
  数据。系统能识别异常溢出，主动少刷或暂停，并让曲线自然回落。
- **刷量可控、可审计**：只刷最近发布的少量推荐，单条有上限、请求有间隔、每批都有
  台账记录，不再盲目全量刷。
- **配置可预测**：正式执行前可用 `forecast` 只读推算今天/明天起 N 天的目标与缺口，
  判断配置会不会空转或超载。

### 1.2 它不做什么

- 不提供 Steam 账号注册、自动过验证码或绕过 Steam Guard 的能力；登录始终是
  半自动的浏览器操作。
- 不保证提高自然推荐曝光或排名，只维护浏览量的平稳。
- 不会自动修改历史统计数据；需要修正时只能通过 `boost` 或直接操作数据库。
- 不适用于攻击、抹黑或干扰其他鉴赏家组；仅限自己运营的组。

### 1.3 运行形态

- 单机 CLI：`python curator_views/cli.py <command> --config config.yaml`
- 常驻守护进程：`daemon` 子命令每天定时执行一次完整流程
- 也可交给 systemd timer、cron 或 Windows 任务计划程序定时调用 `run`

---

## 2. 核心概念与术语

| 术语 | 含义 |
| --- | --- |
| 每日浏览量 | Steam 鉴赏家后台按天统计的 `num_views`，通过 `g_StatsAppViews` 获取 |
| 统计滞后 | Steam 当天统计通常在次日才完整，因此系统按“昨日数据”推算今日 |
| 基准（base） | 今日浏览量的参考水平：自动预测值，或 `baseline_views` 手动固定值 |
| 今日目标（target） | `基准 × 目标系数`，系统希望今天达到的浏览量水平 |
| 目标系数（multiplier） | 目标相对基准的比例；`stable` 恒为 `1.0`，`growth` 为 `growth_factor × (1 + growth_rate × 距生效日天数)` |
| 昨日实际 | 最近一天入库的浏览量，其中可能包含昨天自己刷的量 |
| 昨日刷量 | `boost_log` 台账中记录的昨天提升总量 |
| 昨日自然流量 | `昨日实际 − 昨日刷量`，夹到不小于 0；是计算今日缺口的基准 |
| 缺口（need） | `今日目标 − 昨日自然流量 − 溢出抵扣`，小于等于 0 时当天不刷 |
| 溢出（overflow） | 某天实际浏览量显著超过预期或长期基线，视为疑似外部恶意刷量 |
| 长期基线 | 最近 `baseline_days` 天浏览量的中位数，作为独立于短期预期的参照 |
| 恢复期（cooldown） | 检出恶意刷量后强制不刷的若干天，等待曲线自然回落 |
| 刷量（boost） | 通过访问游戏商店页面增加鉴赏家推荐浏览量的受控操作 |
| 程序启动日 | 第一次运行系统并写入 `program_start` 的日期；更早历史不参与预测 |
| 台账 | SQLite 的 `boost_log` 表，记录每批提升数量与备注 |

---

## 3. 工作原理

### 3.1 每日完整流程

`run` 与 `daemon` 的每日执行都走同一条闭环：

```
                        每日到点（默认 10:00 Asia/Shanghai）
                                      │
                                      ▼
                     抓取 g_StatsAppViews 并写入 daily_stats
                                      │
                                      ▼
                 读取 program_start，过滤启动日之前的历史
                                      │
                                      ▼
              逐日预测（中位数 + 封顶趋势）＋ 反作弊检测迭代
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   ▼
          计算昨日自然流量与今日目标           溢出扣减 / 恢复期判断
                    └─────────────────┬─────────────────┘
                                      ▼
                             计算今日缺口 need
                                      │
                     ┌────────────────┴────────────────┐
                     ▼                                 ▼
          boost_list_file 已配置？               否，使用 need
          是 → 列表首行覆盖 need
                     └────────────────┬────────────────┘
                                      ▼
                    need > 0 且 boost_enabled？否 → 跳过提升
                                      │ 是
                                      ▼
                executor_cmd 已配置？是 → 子进程执行器
                                     否 → 同进程调用 Booster
                                      │
                                      ▼
                          写 boost_log、发 ntfy 简报
                                      │
                                      ▼
                 写 last_run_date（daemon 据此同日去重；dry-run 不写）
```

### 3.2 核心公式

```
基准预期     = 中位数(最近 window_days 天) + 趋势修正
              趋势修正受 |斜率| ≤ 中位数 × trend_cap 封顶
              （baseline_views > 0 时直接用该固定值，跳过自动预测）

今日目标     = 基准预期 × 目标系数
              stable 模式：目标系数 = 1
              growth 模式：目标系数 = growth_factor × (1 + growth_rate × 距生效日天数)

昨日自然流量 = max(0, 昨日实际 − 昨日刷量)

今日缺口     = max(0, round(今日目标 − 昨日自然流量 − 溢出扣减))

实际刷量     = 今日缺口 × (1 + 随机抖动)      # volatility > 0 时
              （list 模式直接使用列表首行数值，不抖动）

单条推荐上限 = single_cap
单日总容量   = 最近推荐数 boost_n × single_cap
```

### 3.3 预测：中位数 + 封顶趋势

实现位于 `curator_views/core/predictor.py`。

1. **中位数窗口**：取最近 `min(历史天数, window_days)` 天浏览量的中位数。
2. **趋势斜率**：当历史点数 ≥ `trend_min_points` 时，对最近
   `min(历史天数, window_days × 2)` 个点做最小二乘线性回归。
3. **斜率封顶**：`|斜率| ≤ 中位数 × trend_cap`，防止几天异常值把预测带飞。
4. **启动期保守**：有效历史天数 < `window_days` 时，预测再乘 `startup_factor`。
5. **最终结果**：`expected = max(0, 中位数 + 斜率)`，保留 1 位小数。

斜率的单位是“次/天”：`+20` 表示近期平均每天多 20 次，`-15` 表示平均每天少 15 次。
历史点数不足 `trend_min_points`（默认 5）时斜率为 0。

### 3.4 目标计算层：stable / growth / growth_rate

`boost_mode` 决定如何把基准转换成今日目标：

| 模式 | 公式 | 用途 |
| --- | --- | --- |
| `stable` | `target = base` | 维持稳定，补缺口到基准水平 |
| `growth` | `target = base × 目标系数`，目标系数 = `growth_factor × (1 + growth_rate × 天数)` | 主动抬高或压低台阶，并可按天匀速变化 |

`目标系数`就是 `forecast` 表格里的「目标系数」列，表示目标相对基准的比例，
不是相对自然流量的倍数。例如 `0.842` 表示目标 = 基准 × 0.842。

- `growth_factor` 管“台阶高低”：`1.5` 表示目标是基准的 1.5 倍；`0.85` 表示先允许
  跌到基准的 85%，跌破才托住。
- `growth_rate` 管“斜率”：`0.01` 表示目标系数每天增加 `growth_factor × 1%`
  （例如 `growth_factor=0.85` 时约 +0.0085），`-0.01` 则每天下降。
- 速率锚点持久化在 `kv_store`：同一速率跨重启沿用原起点；修改速率后从当天重新
  起算，避免历史天数突然生效造成目标跳变。
- 检测参照与补缺口目标同步：预测阶段使用 `base × max(1, multiplier)` 作为异常
  判断参照，避免把自己刷出来的量误判为恶意刷量；长期基线仍是原始中位数。
- `forecast` 输出的“目标”是托底目标 `base × 目标系数（multiplier）`，当
  `growth_factor < 1` 时与检测参照不同，不能混用。

### 3.5 缺口与昨日自然流量

不能直接用“昨日实际”当今天的自然流量基准，因为昨天实际里包含昨天自己刷的量，
重复扣减会导致目标永远补不满、并在目标附近两天一摆。系统因此：

```
昨日自然流量 = max(0, 昨日实际 − boost_log 记录的昨日刷量)
今日缺口     = 今日目标 − 昨日自然流量 − 溢出扣减
```

台账缺失时刷量按 0 处理（兼容旧数据）。刷量记录大于实际值时自然流量夹到 0，
避免补成负数或超额补量。

### 3.6 波动率：让曲线不那么机械

`volatility` 作用在**缺口**上，不作用在目标或总浏览量上：

```
jitter = random.uniform(-volatility, +volatility)
need   = max(0, round(need × (1 + jitter)))
```

- 每天只抽一次随机数；结果小于 0 时当天不刷。
- 缺口越小，波动对最终曲线的影响越小。
- 恢复期（`in_cooldown`）时先强制 `need = 0`，不参与抖动。
- 列表模式（`boost_list_file`）在抖动之后覆盖 `need`，列表数值不抖动。
- 建议值 ≤ `0.15`；过大反而形成不自然的小幅锯齿。

例：目标 1000、昨日自然流量 900、缺口 100、`volatility: 0.10`，则今天刷
90 ~ 110 次，今日总量约 990 ~ 1010。

### 3.7 每日刷量列表（`boost_list_file`）

希望按自己排的计划每天刷固定数量时，配置一个文本文件：

```yaml
boost_list_file: "boost_plan.txt"
```

```text
# 一行一个刷量数量，空行与 # 注释（含行尾注释）会被忽略
500
400
300  # 周三
0    # 0 表示当天不刷（该行仍会被消费）
```

行为：

- 每天取**第一条有效行**作为今日提升目标，直接覆盖预测/基线算出的缺口。
- 成功提升（`boosted > 0`）后，该行从文件中删除；其余行与换行符原样保留，
  采用临时文件 + `os.replace` 原子写回，中途失败不会写坏列表。
- 以下情况**不消费**列表行：`--dry-run`、`boost_enabled: false`、恢复期、
  缺口为 0、提升完全失败（`boosted = 0`）。
- 列表为空或文件缺失时当天**不刷量**，不会回退到自动预测；文件缺失会在每日
  简报中标记为 warning。
- 列表数值仍受 `single_cap`、`target_ratio` 等执行参数约束。需要精确刷满列表
  数量时，可把 `target_ratio` 设为 `1.0` 或使用 `boost_distribution: exact`。

### 3.8 反作弊：三重检测

实现位于 `curator_views/core/anti_cheat.py`，检测手段可叠加：

#### 3.8.1 双基线

系统同时维护两条“正常水平”参照线：

| 基线 | 窗口 | 计算方式 | 作用 |
| --- | --- | --- | --- |
| 短期预测基线 | `window_days`（默认 14 天） | 中位数 + 封顶趋势 → 每日 `expected` | 判断单日是否比预期高太多 |
| 长期基线 | `baseline_days`（默认 60 天） | 窗口内浏览量中位数 `long_baseline` | 独立参照，防止短期预期被污染 |

候选异常日满足任一条件：

```
实际 > expected × (1 + outlier_k)
或 实际 > long_baseline × (1 + outlier_k)
或 命中持续上升斜率检测
```

为什么要长期基线：如果被持续灌量，14 天中位数会被抬高，`expected` 也跟着抬高，
单看“实际 vs expected”抓不到“平台整体抬升”型刷量。60 天中位数变化慢得多，
能提供相对干净的参照。

例：长期基线 1000、`outlier_k: 0.30`，则实际 > 1300 即候选；即使 14 天预期被
污染到 1400，某天实际 1350 仍会被长期基线抓到。`baseline_days: 0` 表示关闭
长期基线。

#### 3.8.2 持续上升斜率

`detect_drift` 用于识别“每天只涨一点、单日永不超阈值”的慢性注入：

- 取最近 `slope_days` 天（默认 14 天）做线性回归；
- 当斜率 > 窗口中位数 × `slope_threshold`（默认 2%/天）时，认为窗口存在异常爬升；
- 此时只把窗口内**高于 `long_baseline × (1 + outlier_k)`** 的高位日标记为额外
  候选，交给连续段判定，避免把真实增长误判为刷量；
- 未提供长期基线时不产生任何标记。

#### 3.8.3 连续段标记

候选日必须连续出现 `consecutive_days` 天（默认 2 天）才正式标记为恶意刷量。
达标后，该连续段内的全部候选日都标记为 `flagged`，保证“溢出量 = 这些天超出
部分之和”完整成立。单日波动不误伤。

#### 3.8.4 溢出扣减与恢复期

- 每个被标记日的 `overflow_amount = 实际 − 预期`，平均分摊到其后的
  `absorb_days` 天（默认 3 天），从这些天的缺口里扣掉。
- 扣减使用第一轮检测结果（检测事实不因后续剔除而改变）。
- 最近一个标记日之后 `cooldown_days` 天（默认 7 天）内强制不刷量，让曲线在无
  注入的情况下自然回落。

#### 3.8.5 剔除标记日并迭代

`exclude_flagged: true`（默认）时，被标记日期会从预测与长期基线的计算窗口中
剔除，然后重新检测，最多迭代 3 轮，直到没有新的标记。这样虚高数据无法继续
抬高基线。

### 3.9 受控提升执行器

实现位于 `curator_views/core/booster.py`：

1. 通过 `Fetcher` 获取最近 `boost_n` 条推荐。
2. 计算预算 `budget = min(目标, 推荐条数 × single_cap)`。
3. `boost_distribution: ratio`（默认）：累计达到 `目标 × target_ratio` 后提前停止。
4. `boost_distribution: exact`：按推荐条数精确分配配额，全部配额刷满即恰好达成
   目标（如 500 / 30 条 → 前 20 条各 17 次、后 10 条各 16 次）。
5. 按 `batch_size` 分批循环访问游戏页面。
6. 每次访问（无论成功与否）后随机等待 `wait_min` ~ `wait_max` 秒；HTTP 200
   且页面包含 `<title>` 记为成功。
7. `threads > 1` 时并发访问；并发实现上限为 2 个工作线程。
8. 连续失败达到 `max_retries` 次时中止本轮，并记录已成功的数量。
9. 每批结束写入 `boost_log` 台账，无论成功、失败还是中止。

### 3.10 调度器 / 执行器分离

默认在同一进程内直接调用 Booster。配置 `executor_cmd` 后，调度器会把任务交给
独立子进程执行：

```yaml
executor_cmd: "python cli.py boost --config {config} --target {need} --apps {apps}"
```

占位符：

- `{config}`：配置文件路径
- `{need}`：本次目标刷量
- `{apps}`：推荐数量

子进程超时 7200 秒；调度器解析其最后一行 stdout JSON 的 `boosted` 字段。执行器
可独立运行、独立排障，调度器崩溃也不会影响已经启动的执行器。

---

## 4. 项目结构

```
CuratorViewsGuard/
├── steam_curator_views.py   # 旧版全量刷量脚本（仅作行为参考，新系统不依赖）
├── config.yaml              # 本地配置（init-config 生成；含 clan_id / Cookie 路径）
├── requirements.txt         # Python 依赖清单
├── README.md                # 本说明书
├── tests/                   # pytest 测试
└── curator_views/
    ├── cli.py               # 命令行入口与命令实现
    ├── README.md            # 子系统速查
    └── core/
        ├── fetcher.py       # 抓取 g_StatsAppViews 与推荐列表
        ├── login.py         # Playwright 半自动登录，导出/校验 Cookie
        ├── predictor.py     # 中位数 + 封顶趋势预测
        ├── anti_cheat.py    # 溢出检测 + 分摊抵扣 + 斜率检测
        ├── forecast.py      # 今天/明天起 N 天只读估算（纯函数，无 I/O）
        ├── booster.py       # 受控刷量执行器
        ├── scheduler.py     # 每日闭环编排 + 子进程执行器
        ├── daemon.py        # 常驻调度循环
        ├── notifier.py      # ntfy 推送
        └── storage.py       # SQLite 存储
```

---

## 5. 安装

要求 **Python 3.10+**（Windows / Linux 均可）。

```bash
# 进入项目根目录
pip install -r requirements.txt
```

依赖说明：

- 必需：`requests`、`beautifulsoup4`、`lxml`、`PyYAML`、`tzdata`
- `tzdata` 用于在缺少系统时区库的环境（如精简 Windows）提供 `Asia/Shanghai`
- 可选：`playwright`，仅 `login` 命令需要
- `onepush` 在依赖清单中保留但默认未启用；当前通知直接通过 `requests` 调 ntfy

安装 Playwright（可选）：

```bash
pip install playwright
playwright install chromium
```

未安装时执行 `login` 会提示：

```
执行失败: 未安装 playwright。请运行：pip install playwright && playwright install chromium
```

---

## 6. 快速开始

```bash
# 1. 生成默认配置文件
python curator_views/cli.py init-config

# 2. 编辑 config.yaml：
#    clan_id            —— Steam 鉴赏家组 ID（纯数字，如 45519015），必填
#    cookie_file        —— 统计用 Cookie 文件，可选；留空则未登录运行
#    boost_cookie_file  —— 刷量专用 Cookie 文件，可选；留空则游客刷量
#    proxy              —— 代理地址，可选
#    ntfy_url / ntfy_topic —— ntfy 推送，可选

# 3. 半自动登录，生成统计用 Cookie
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_stats.txt --profile-dir .steam_profile_stats

# 4. 如需刷量也带登录态，再生成一份刷量 Cookie
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_boost.txt --profile-dir .steam_profile_boost

# 5. 只读巡检：抓取统计并预览预测，不产生提升请求
python curator_views/cli.py check --config config.yaml

# 6. 只读估算未来 7 天的目标与缺口
python curator_views/cli.py forecast --config config.yaml --days 7

# 6b. 只看今天该刷多少（默认含今天，首日自然 = 昨日实际 − 昨日刷量）
python curator_views/cli.py forecast --config config.yaml --fetch

# 7. 演练模式：完整计算但不真正刷量
python curator_views/cli.py run --config config.yaml --dry-run

# 8. 正式执行一次每日完整流程
python curator_views/cli.py run --config config.yaml

# 9. 常驻守护进程（推荐长期运行）
python curator_views/cli.py daemon --config config.yaml
```

> **修改代码后需要重启 daemon**；只改配置不用重启（每轮热加载）。重启方法见
> [10.1.1](#1011-重启与停止)。

> **首次运行注意**：程序启动日之前的 Steam 历史（约 364 天）**不参与预测**。
> 这些数据里可能混着以往不规则刷量和他人恶意刷量，不可靠。系统从启动日起积累
> 自己的平稳数据；头 `window_days` 天（默认 14 天）处于启动期，预测会乘
> `startup_factor`（默认 0.9）保守系数，缺口通常为 0，这是正常现象。

---

## 7. 配置参考

配置由 `init-config` 生成，相对路径均基于配置文件所在目录解析。所有日期与时间
使用 **Asia/Shanghai** 时区。

### 7.1 基础配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `clan_id` | （空） | Steam 鉴赏家组 ID，纯数字，**必填** |
| `cookie_file` | `cookie.txt` | 统计用 Cookie 路径（访问后台 `admin/overview`） |
| `boost_cookie_file` | （空） | 刷量专用 Cookie 路径；与 `cookie_file` 完全独立 |
| `proxy` | （空） | 代理地址，如 `http://127.0.0.1:10279`；留空不使用 |
| `db_path` | `views.db` | SQLite 数据库路径 |

**Cookie 规则：**

- `cookie_file` 用于获取每日浏览量。Steam 后台统计通常必须登录，未登录会被
  重定向到公开页，抓取失败。
- `boost_cookie_file` 只用于刷量：推荐列表与游戏页面访问。留空时刷量以
  **未登录（游客）模式**运行，**不会**复用 `cookie_file`。
- 任一 Cookie 文件留空或不存在时，CLI 只打印提示/警告，不会退出；对应功能以
  游客模式继续。
- 想共用同一个账号，需要显式把 `boost_cookie_file` 写成与 `cookie_file` 相同的
  路径，而不是留空。
- 自定义 Cookie 文件名不在 `.gitignore` 的 `cookie.txt` 规则内，务必自行加入
  `.gitignore`，避免提交敏感信息。

### 7.2 通知配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `ntfy_url` | （空） | ntfy 服务地址，如 `https://ntfy.sh` |
| `ntfy_topic` | （空） | ntfy 主题名，如 `steam-curator-views` |

两者都配置才启用 ntfy 推送；否则只写日志。每日简报在以下情况升级为 warning：
存在溢出标记、处于恢复期、Booster 中止、执行器报错、刷量列表文件缺失/读取失败/
消费失败。

### 7.3 守护进程配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `daily_time` | `"10:00"` | daemon 每日执行时刻，格式 `HH:MM` |

`daemon` 每次循环都会重新加载配置文件，修改任意配置（含执行时间）无需重启。
时间非法时回退到 10:00 并记录警告。

### 7.4 预测参数

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `window_days` | `14` | 中位数计算窗口（天） |
| `trend_cap` | `0.15` | 趋势斜率相对中位数的封顶比例 |
| `startup_factor` | `0.9` | 启动期保守系数（历史天数 < `window_days` 时生效） |
| `trend_min_points` | `5` | 计算趋势所需的最少数据点数 |

**调参建议：**

- `window_days` 太长反应慢，太短容易被噪声带偏；14 天适合大多数稳定组。
- `trend_cap` 越小越保守。设为 `0` 表示完全忽略趋势，只用中位数。
- `startup_factor` 只在启动期生效；数据足够后不再影响预测。
- 斜率的完整解释见 [7.10](#710-预测参数里的斜率是什么)。

### 7.5 反作弊参数

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `outlier_k` | `0.30` | 超出预期/长期基线的判定阈值（30%） |
| `consecutive_days` | `2` | 连续候选达到该天数才标记 |
| `absorb_days` | `3` | 溢出量分摊到未来多少天 |
| `exclude_flagged` | `true` | 标记日是否剔除出预测与长期基线 |
| `cooldown_days` | `7` | 标记后强制不刷的天数 |
| `baseline_days` | `60` | 长期基线窗口；`0` 表示关闭长期基线 |
| `slope_days` | `14` | 持续上升斜率检测窗口 |
| `slope_threshold` | `0.02` | 斜率阈值（相对中位数/天），超过视为慢性爬升候选 |

**调参建议：**

- `outlier_k` 越小越敏感，误报也越多；`0.30` 对应“超出 30%”。
- `consecutive_days: 1` 会在单日异常时立即标记，适合强风控场景，但更容易误伤。
- `absorb_days` 越大，扣减越平缓，但恢复期也更长。
- `exclude_flagged: false` 会让被刷日继续参与预测，基线容易被污染，通常不建议。
- `baseline_days` 建议明显大于 `window_days`；关闭后只剩短期检测，防“平台抬升”
  的能力会消失。

详见 [3.8 反作弊](#38-反作弊三重检测)。

### 7.6 基线与波动参数

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `baseline_views` | `0` | 手动目标浏览量；> 0 时替代自动预测 |
| `boost_mode` | `stable` | `stable` 维持稳定 / `growth` 主动升降 |
| `growth_factor` | `1.5` | growth 模式目标系数的起始值；stable 模式不生效 |
| `growth_rate` | `0` | growth 模式下目标系数每天的线性变化率（0.01 = 每天 +1%，-0.01 = 每天 -1%） |
| `volatility` | `0.10` | 缺口随机抖动幅度；建议 ≤ 0.15 |

**说明：**

- `baseline_views > 0` 时，今日基准直接使用该固定值，溢出检测也以它为参照。
  适合“希望稳定在 1000 左右”这类明确目标。
- `boost_mode: stable` 下 `growth_factor` 与 `growth_rate` 都不生效。
- `growth_factor < 1` 表示允许曲线先跌到基准的某个比例，跌破才托住；
  `> 1` 表示主动抬高台阶。
- `growth_rate` 与 `growth_factor` 相乘，可同时使用；修改速率会从当天重新起算。
- `volatility` 作用于缺口而非目标，详见 [3.6](#36-波动率让曲线不那么机械)。

### 7.7 刷量列表

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `boost_list_file` | （空） | 每日刷量列表文件路径；非空启用列表模式 |

列表格式与消费规则见 [3.7](#37-每日刷量列表boost_list_file)。

### 7.8 受控提升参数

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `boost_enabled` | `true` | 是否启用每日提升 |
| `boost_n` | `5` | 每日使用的最近推荐数量 |
| `single_cap` | `300` | 单个游戏单日成功访问上限 |
| `batch_size` | `50` | 每批处理的游戏数量 |
| `target_ratio` | `0.95` | 累计达到目标该比例后提前停止（`ratio` 模式） |
| `boost_distribution` | `"ratio"` | `ratio` 按比例提前停 / `exact` 精确配额 |
| `threads` | `1` | 并发线程数；实现上限 2；建议保持 1 |
| `wait_min` / `wait_max` | `8.0` / `16.0` | 两次请求之间的随机等待秒数 |
| `max_retries` | `3` | 连续失败达到该次数后中止本轮 |
| `executor_cmd` | （空） | 执行器子进程命令模板；留空则同进程直调 |

单日实际吞吐还受 `threads` 与 `wait_min` / `wait_max` 限制：乐观上限为
`实际并发（min(threads, 2, boost_n, batch_size)）× 86400 /
max(wait_min, wait_max)`（未计请求耗时）。`forecast` 在缺口超过该上限时给出
`throughput_capacity` 警告；`run` 也会记录警告并在简报中提示。`threads` 超过 2
按 2 计算；并发还受推荐数 `boost_n` 与批次大小 `batch_size` 限制。

`boost_distribution: exact` 示例：目标 500、最近 30 条推荐 →
前 20 条各 17 次、后 10 条各 16 次，全部配额刷满即恰好 500 次。

`executor_cmd` 占位符：`{config}`、`{need}`、`{apps}`。子进程超时 7200 秒。

### 7.9 完整默认配置

`init-config` 生成的模板包含以下内容（`executor_cmd` 与 `boost_distribution`
为可选键，模板未列出，可手动添加）：

```yaml
clan_id: ""
cookie_file: "cookie.txt"
boost_cookie_file: ""
proxy: ""
db_path: "views.db"

ntfy_url: ""
ntfy_topic: ""
boost_enabled: true

daily_time: "10:00"

window_days: 14
trend_cap: 0.15
startup_factor: 0.9
trend_min_points: 5

outlier_k: 0.30
consecutive_days: 2
absorb_days: 3
exclude_flagged: true
cooldown_days: 7
baseline_days: 60
slope_days: 14
slope_threshold: 0.02

baseline_views: 0
boost_mode: stable
growth_factor: 1.5
growth_rate: 0.0
volatility: 0.10

boost_list_file: ""

boost_n: 5
single_cap: 300
batch_size: 50
target_ratio: 0.95

threads: 1
wait_min: 8.0
wait_max: 16.0
max_retries: 3
```

### 7.10 预测参数里的“斜率”是什么

预测用的斜率是**每日浏览量的线性趋势斜率**（单位：次/天），由
`predictor.py` 对最近一段时间的统计数据做最小二乘线性回归得到：

```
y = 每日浏览量
x = 日期序号（最近 N 天，N = window_days × 2，默认 28 天）
斜率 = 拟合直线的倾斜程度
```

- 正斜率：近期浏览量在上涨（如 `+20` 表示平均每天多 20 次）；
- 负斜率：近期在下降（如 `-15` 表示平均每天少 15 次）；
- 历史数据少于 `trend_min_points`（默认 5 天）时斜率为 0，只用中位数预测。

预测公式：

```
今日预期 = 中位数(最近 window_days 天) + 斜率
         （数据不足 window_days 天时再 × startup_factor）
```

`trend_cap` 用来封顶斜率：

```
|斜率| ≤ 中位数 × trend_cap
```

例：最近 14 天中位数 1000、`trend_cap: 0.15`，斜率最多按 `±150 次/天` 参与
计算。回归算出 `+40/天` 就用 `+40`；算出 `+500/天` 也只按 `+150`，今日预期
= 1000 + 150 = 1150。

> 注意：反作弊参数 `slope_days` / `slope_threshold` 也描述“斜率”，但用于检测
> 慢性刷量，不参与今日预期计算。

---

## 8. 命令参考

| 命令 | 作用 |
| --- | --- |
| `run` | 每日完整流程（抓取 → 预测 → 检测 → 提升 → 通知） |
| `daemon` | 常驻运行，每天到点执行每日流程（配置热重载） |
| `info` | 查看守护进程状态与本地数据概况（离线只读） |
| `check` | 只读检查：抓取统计并打印最近 7 天与预测摘要 |
| `forecast` | 只读推算今天/明天起 N 天的目标 / 假设自然流量 / 缺口 |
| `boost` | 手动执行一轮受控提升 |
| `login` | 半自动登录 Steam，生成/校验 Cookie |
| `export` | 从本地数据库导出每日统计（JSON / CSV） |
| `init-config` | 生成默认配置文件模板 |
| `version` | 打印版本号 |

通用约定：

- 所有命令支持 `--config PATH`（默认 `config.yaml`）。
- 除 `init-config` / `version` / `info` 外，多数命令要求在配置中提供 `clan_id`。
- 退出码：`0` 成功；`1` 执行失败或校验失败；`130` 用户 Ctrl+C 中断。

### 8.1 run

```bash
python curator_views/cli.py run --config config.yaml [--dry-run] [--boost | --no-boost]
```

- `--dry-run`：演练模式，计算一切但不发送提升请求、不发送执行器子进程、不发简报；
  也不写数据库（`daily_stats` / `boost_log`）与状态（`program_start` /
  `last_stats_date` / `last_run_date` / growth 锚点），不会把当天标记为已执行；
- `--boost` / `--no-boost`：强制开启/关闭提升，优先于配置 `boost_enabled`。

成功后以 JSON 打印汇总（日期、今日预期、昨日实际/刷量/自然流量、缺口、溢出
扣减、异常天数、实际提升数、列表状态等），并在数据库同级目录写
`curator_views.log`。

### 8.2 daemon

```bash
python curator_views/cli.py daemon --config config.yaml [--dry-run] [--boost | --no-boost]
```

行为要点：

- 启动时若今天尚未执行且已过 `daily_time`，立即补跑一次；
- 每轮循环重新加载配置，修改配置或执行时间无需重启；**修改 Python 代码后必须重启进程**；
- 单次执行失败不退出进程，记录日志后等待下一天；
- 同一天不重复执行：`last_run_date` 同时写入数据库与 `daemon_state.json`
  （`--dry-run` 不写数据库侧，因此不会顶掉当天正式执行）；
- Ctrl+C（SIGINT）立即中断当前执行并退出；SIGTERM 优雅收尾后退出；
- 前台运行时 INFO 日志同时输出到控制台。

后台运行示例：

```bash
nohup python curator_views/cli.py daemon --config config.yaml >/dev/null 2>&1 &
```

建议配合 systemd `Restart=always` 或 Windows 服务包装器保持常驻。
重启、停止与状态查看见 [10.1.1](#1011-重启与停止)。

### 8.3 info

```bash
python curator_views/cli.py info --config config.yaml [--json]
```

离线只读：读取状态文件与本地数据库，不要求 `clan_id` / Cookie，不触网，也不会在
数据库不存在时创建空库。

输出包含：守护进程状态、PID、启动时间、当前阶段、心跳、上次/下次执行时间、
状态文件路径、上次错误、配置摘要、刷量列表剩余行数、本地数据概况。

存活判断优先看 PID；PID 不可用时回退到 90 秒心跳。长时间提升期间心跳可能暂停，
此时仍以 PID 为准。PID 复用是已知限制。

### 8.4 check

```bash
python curator_views/cli.py check --config config.yaml
```

只读巡检：抓取统计数据，打印最近 7 天与预测摘要，不产生提升请求，也不写数据库。

### 8.5 forecast

```bash
python curator_views/cli.py forecast --config config.yaml \
  [--days N] [--fetch] [--today | --tomorrow] [--natural trend|target] \
  [--set KEY=VALUE ...] [--json]
```

只读推算**今天（默认）或明天起** N 天的目标浏览量、假设自然流量与刷量缺口，
用于判断配置是否会空转（缺口恒为 0）或超载（缺口超过单日容量/吞吐上限）。

- `--days N`：预测天数，默认 7，必须 ≥ 1；
- `--fetch`：额外从 Steam 在线抓取最新统计，仅在内存使用，绝不写库；要求
  `clan_id` 非空；
- `--today` / `--tomorrow`：从今天 00:00（默认）或明天 00:00 起算。今天的
  统计要明天才完整，所以预测今天本身仍然是预测；
- `--natural trend|target`：默认 `natural`，即首日自然流量取“今天之前最近一个
  完整日的实际 − 当日刷量”（与 `run` 的缺口公式一致），未来日假设自然流量不变，
  因此每天都会重新计算缺口；显式指定时改用旧模型：
  - `trend`：假设自然流量按实际趋势延续；
  - `target`：假设自然流量最终收敛到目标（首日用初始预测，之后用上一天目标）；
- `--set KEY=VALUE`：覆盖配置做 what-if，可重复，也能覆盖 `db_path`；
- `--json`：输出 `rows` / `warnings` / `summary` / `source` 完整结构。

只读保证：

- 以只读 URI 打开数据库，只执行 `SELECT`；
- 不写 `last_run_date`，不写 growth 锚点，不启用 WAL；
- 数据库不存在或无历史数据时报错并以 1 退出，不会自动建库；
- growth 起点只从 `kv_store` 读取，速率一致才沿用，否则按预测首日重新起算，
  不写回。

默认从**今天 00:00**（Asia/Shanghai）起算，`--tomorrow` 可改为明天。自然口径下
首日基准取今天之前最近一个完整日；若本地库最新日期早于昨天，会给出
`stale_history` 提示，建议加 `--fetch`。
这是**估算不是精确预测**：未来自然流量只是外推；异常标记未持久化，因此
`in_cooldown` 恒为 `false`，未来日也不做溢出扣减；自然口径同样不含恢复期、
波动率与刷量列表，只复用 `run` 的缺口公式。

forecast 输出的是托底目标 `base × 目标系数（multiplier）`；`run` 的检测参照是
`base × max(1, multiplier)`。当 `growth_factor < 1` 时两者不同，不要把托底
目标当成检测参照。

输出示例：

```
预测区间: 2026-11-14 ~ 2026-11-20（7 天，含今天）
数据来源: 本地数据库 E:\...\views.db（18 天，最新 2026-11-13 = 5,449 次）
自然口径: 基准日 2026-11-13 实际 5,449 - 刷量 0 = 自然 5,449；未来日假设自然不变
参数: boost_mode=growth factor=0.85 rate=-0.01
      基线=auto 自然模型=natural 波动率=0.10
单日容量: 1,500 次（boost_n=5 × single_cap=300）

日期           基准  目标系数     目标  假设自然   缺口  备注
-----------------------------------------------------------------
2026-11-14  8,430.4     0.842  7,094.2     5,449  1,645  容量不足
2026-11-15  8,352.3     0.833  6,957.5     5,449  1,508  容量不足
...

汇总: 7 天预计刷量 8,216（日均 1,174）；目标 7,094.2 → 5,893.0
提示:
  - [over_capacity] 以下日期超过单日容量 1,500 次（boost_n=5 × single_cap=300）：
      2026-11-14 需要 1,645 次
      2026-11-15 需要 1,508 次
```

「目标系数」= 目标 ÷ 基准，只作用于基准，不是“自然流量的倍数”：

- `stable` 模式恒为 `1.000`；
- `growth` 模式为 `growth_factor × (1 + growth_rate × 距生效日天数)`，线性变化，
  不是复利。

例如 `growth_factor=0.85`、`growth_rate=-0.01` 时，11-14 是第 1 天：
`0.85 × (1 - 0.01 × 1) = 0.8415`，显示为 `0.842`；当天目标 =
`8,430.4 × 0.842 ≈ 7,094.2`。`0.842` 表示目标是基准的 84.2%，不是自然流量的
0.842 倍。JSON 字段仍叫 `multiplier`。

表格里的「备注」列会在缺口超过单日容量时显示 `容量不足`（容量为 0 时显示
`无容量`），在受线程/等待间隔限制时显示 `吞吐不足`；单日容量 =
`boost_n × single_cap`，单日吞吐上限 = `min(threads, 2, boost_n, batch_size) ×
86400 / max(wait_min, wait_max)`（未计请求耗时），都会在表格上方单独列出。

常见 warning：

| code | 含义 |
| --- | --- |
| `no_history` | 本地没有可用历史数据，所有基准为 0 |
| `need_all_zero` | 整个预测区间缺口都为 0，系统不会刷量 |
| `multiplier_zero` | 目标系数在某个日期归零（通常 `growth_rate` 过负） |
| `over_capacity` | 某些天缺口超过 `boost_n × single_cap` 的单日容量 |
| `throughput_capacity` | 某些天缺口超过按 `threads` / 等待间隔估算的单日吞吐上限（未计请求耗时） |
| `stale_history` | 自然口径下本地库最新日期早于昨天，缺口可能基于旧数据 |

### 8.6 boost

```bash
python curator_views/cli.py boost --config config.yaml --target N [--apps M] [--dry-run]
```

手动执行一轮受控提升，不经过预测/检测流程。

- `--target N`：本轮目标提升次数（必填）；
- `--apps M`：使用的最近推荐数量，默认取配置 `boost_n`；
- `--dry-run`：仅记录提升计划，不发送任何请求。

使用 `boost_cookie_file` 对应的 Cookie；留空则游客模式。

### 8.7 login

```bash
python curator_views/cli.py login --config config.yaml \
  [--cookie-file PATH] [--profile-dir PATH] \
  [--validate-only] [--headless] [--no-validate] [--timeout SECONDS]
```

Steam 登录页有图形验证码，新设备/异地登录还可能需要 Steam Guard 手机确认，
无法可靠全自动。`login` 使用 Playwright 打开真实 Chromium 窗口，由你完成登录，
脚本轮询等待 `steamLoginSecure` Cookie 出现，导出 `steampowered.com` 域下的
Cookie，再用鉴赏家后台页面校验；校验通过才原子写入目标文件。

参数：

| 参数 | 说明 |
| --- | --- |
| `--cookie-file PATH` | 写入的 Cookie 文件；默认取配置 `cookie_file`，未配置则 `cookie.txt` |
| `--profile-dir PATH` | 浏览器持久化目录，默认 `.steam_profile` |
| `--validate-only` | 只校验已有 Cookie，不打开浏览器 |
| `--headless` | 无界面模式，通常无法完成验证码/手机确认 |
| `--no-validate` | 登录后跳过有效性校验直接写入（不推荐） |
| `--timeout SECONDS` | 等待登录完成的超时，默认 300 秒 |

登录态保存在持久化目录，第二次运行通常无需再输密码。校验失败时不会覆盖原
Cookie 文件。

**统计与刷量使用两份 Cookie：**

```bash
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_stats.txt --profile-dir .steam_profile_stats
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_boost.txt --profile-dir .steam_profile_boost
```

不同账号必须使用不同 `--profile-dir`，否则会复用同一份浏览器登录态。
`boost_cookie_file` 留空时刷量走游客模式，不会复用 `cookie_file`。

成功时输出 JSON（不含 Cookie 内容）：

```json
{"mode": "login", "cookie_file": "cookie.txt", "clan_id": "45519015",
 "valid": true, "reason": "Cookie 有效", "cookie_count": 12}
```

### 8.8 export

```bash
python curator_views/cli.py export --config config.yaml \
  --format json|csv [--out FILE]
```

导出 `daily_stats` 全部记录。`--format` 默认 `json`；`--out` 缺省时打印到标准输出。
只读取本地数据库，不需要 Cookie；但配置文件仍需提供 `clan_id`（命令共用运行时
解析）。

### 8.9 init-config

```bash
python curator_views/cli.py init-config [--path config.yaml]
```

生成默认配置模板。目标文件已存在时报错并返回 1，不会覆盖。

### 8.10 version

```bash
python curator_views/cli.py version
```

打印版本号（当前 `1.0.0`）。

---

## 9. 典型使用场景

### 9.1 只统计、不刷量

```yaml
boost_enabled: false
```

或临时执行：

```bash
python curator_views/cli.py run --config config.yaml --no-boost
```

先积累几周平稳数据，再开启提升，预测会更可靠。

### 9.2 稳定在固定浏览量

```yaml
baseline_views: 1000
boost_mode: stable
volatility: 0.10
```

系统会把曲线补到 1000 附近，缺口小的时候波动也小。

### 9.3 匀速缓降

```yaml
baseline_views: 1000
boost_mode: growth
growth_factor: 0.85
growth_rate: -0.01
```

目标会从基准的 85% 开始，每天再降 1%；跌破托底值才补量。

### 9.4 按排期列表刷量

```yaml
boost_list_file: "boost_plan.txt"
```

列表文件一行一个数量，每天消费第一行，成功刷完后删除。适合有明确投放计划的
场景；列表用完即停，不会回退到自动预测。

### 9.5 统计与刷量使用不同账号

```yaml
cookie_file: "cookie_stats.txt"
boost_cookie_file: "cookie_boost.txt"
```

分别登录生成两份 Cookie，`--profile-dir` 也要分开。详见 [8.7](#87-login)。

### 9.6 完全游客模式

```yaml
cookie_file: ""
boost_cookie_file: ""
```

CLI 不会因 Cookie 缺失退出；但统计抓取通常需要登录，`run` / `check` 可能失败。
`boost` 访问游戏页面可匿名进行，推荐列表是否可用取决于 Steam。

### 9.7 正式执行前先估算

```bash
# 今天该刷多少（默认含今天；--fetch 会在线补齐统计，仅内存使用）
python curator_views/cli.py forecast --config config.yaml --fetch

# 未来两周的空转/超载检查
python curator_views/cli.py forecast --config config.yaml --days 14
python curator_views/cli.py forecast --config config.yaml \
  --set growth_factor=1.2 --set baseline_views=1500
```

默认口径已对齐 `run` 的缺口公式；用 `--tomorrow` 看明天起，用 `--set` 做 what-if，
确认缺口和容量后再改正式配置。

---

## 10. 定时调度

### 10.1 daemon（推荐）

```bash
python curator_views/cli.py daemon --config config.yaml
```

配置热重载、同日去重、失败不退出、优雅退出。适合长期常驻。

systemd 服务示例：

```ini
# /etc/systemd/system/curator-views.service
[Unit]
Description=Steam Curator Views Daemon
After=network-online.target

[Service]
WorkingDirectory=/path/to/CuratorViewsGuard
ExecStart=/usr/bin/python3 curator_views/cli.py daemon --config /path/to/config.yaml
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now curator-views.service
```

Windows 可用 NSSM：

```text
nssm install curator-views "C:\Path\To\python.exe" "C:\Path\To\curator_views\cli.py daemon --config C:\Path\To\config.yaml"
```

#### 10.1.1 重启与停止

先确认 daemon 是怎么启动的：

```bash
python curator_views/cli.py info --config config.yaml
ps -ef | grep "[c]urator_views/cli.py daemon"
systemctl list-unit-files | grep -i curator
```

**systemd**（服务名以实际 unit 为准，下面假设为 `curator-views.service`）：

```bash
sudo systemctl restart curator-views.service
sudo systemctl status curator-views.service --no-pager
journalctl -u curator-views.service -n 50 -f
```

只有修改过 `.service` 文件才需要先 `sudo systemctl daemon-reload`；改代码直接
`restart` 即可。若提示 `Unit ... could not be found`，说明还没安装该 unit，daemon
可能是用 `nohup` 或前台方式启动的：按上面的 systemd 示例创建 unit（venv 用户把
`ExecStart` 改成 `which python` 得到的解释器路径），然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now curator-views.service
```

**nohup / 后台运行**：

```bash
# 取 PID（info 的 --json 输出里 daemon.pid）
python curator_views/cli.py info --config config.yaml --json \
  | python -c "import json,sys; print(json.load(sys.stdin)['daemon']['pid'])"

kill <PID>        # SIGTERM：优雅退出，等在途提升收尾
# kill -INT <PID> # 立即中断，等同 Ctrl+C

# 等进程完全退出后再启动
while kill -0 <PID> 2>/dev/null; do sleep 1; done
nohup python curator_views/cli.py daemon --config config.yaml >/dev/null 2>&1 &
```

**前台 / tmux / screen**：`Ctrl+C` 停止，然后重新执行上面的 `daemon` 命令。

重启后验证：

```bash
python curator_views/cli.py info --config config.yaml
```

确认 PID 已变化、`phase` 为 `running`、`next_wake` 正常；日志见数据库同级的
`curator_views.log`（systemd 用 `journalctl`）。

注意事项：

- **改配置不用重启**：daemon 每轮热加载配置（含 `daily_time`）；
- **改代码必须重启**：进程里仍是旧模块，重启后才加载新代码；
- **重启不会重复刷**：`last_run_date` 做同日去重。今天已跑过 → 重启后等明天；
  今天没跑且已过 `daily_time` → 启动时立即补跑一次；
- **信号语义**：SIGTERM 优雅退出（等在途任务收尾），SIGINT 立即中断；
- **systemd `Restart=always`**：不要用 `kill` 测试停止，会被自动拉起；请用
  `systemctl restart/stop`。

### 10.2 systemd timer（每天一次 run）

```ini
# /etc/systemd/system/curator-views.service
[Unit]
Description=Steam Curator Views Daily Run
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/CuratorViewsGuard
ExecStart=/usr/bin/python3 curator_views/cli.py run --config /path/to/config.yaml
```

```ini
# /etc/systemd/system/curator-views.timer
[Unit]
Description=Run Steam curator views daily at 10:00

[Timer]
OnCalendar=*-*-* 10:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now curator-views.timer
```

### 10.3 cron

```cron
0 10 * * * cd /path/to/CuratorViewsGuard && /usr/bin/python3 curator_views/cli.py run --config config.yaml >>/dev/null 2>&1
```

日志由 CLI 写入数据库同级 `curator_views.log`，cron 只兜住启动期异常。

### 10.4 Windows 任务计划程序

1. 打开「任务计划程序」→「创建任务」；
2. 触发器：每天 10:00；
3. 操作 → 新建：
   - 程序或脚本：`C:\Path\To\python.exe`
   - 添加参数：`C:\Path\To\CuratorViewsGuard\curator_views\cli.py run --config C:\Path\To\config.yaml`
   - 起始于：`C:\Path\To\CuratorViewsGuard`
4. 条件：取消「只有在计算机使用交流电源时才启动此任务」；
5. 设置：勾选「如果任务失败，重新启动任务」（如 3 次，间隔 5 分钟）。

> Steam 统计滞后一天，早上执行能拿到前一天的完整数据。北京时间 10:00 是推荐值。

---

## 11. 数据、日志与状态

### 11.1 SQLite 数据库

默认 `views.db`，WAL 模式，包含三张表：

| 表 | 内容 |
| --- | --- |
| `daily_stats` | 每日浏览量（`date_ts` 主键 → `num_views`） |
| `boost_log` | 刷量台账（日期、数量、备注、创建时间） |
| `kv_store` | 键值状态 |

`kv_store` 常见键：

- `program_start`：程序启动日，过滤更早历史；
- `last_stats_date`：最近一次抓取的统计日期与天数；
- `last_run_date`：最近一次完成每日流程的日期（同日去重）；
- `growth_rate` / `growth_start`：growth 速率锚点。

### 11.2 日志

- 文件日志：数据库所在目录的 `curator_views.log`；
- `daemon` 前台运行时同时输出控制台；
- 日志包含每日简报、提升批次、异常检测、执行器错误等。

### 11.3 守护进程状态

`daemon_state.json` 写在数据库同级目录，包含：

- `pid`、`started_at`、`phase`、`updated_at`；
- `last_run_date`、`last_run_at`、`last_summary`、`last_error`；
- `next_wake`。

`info` 读取该文件；数据库里的 `last_run_date` 也会被 daemon 每轮核对，因此手动
`run` 与 daemon 谁先执行，当天都只跑一次。

### 11.4 数据导出

```bash
python curator_views/cli.py export --config config.yaml --format csv --out stats.csv
```

只导出 `daily_stats`；`boost_log` 与 `kv_store` 需直接用 SQLite 查看。

---

## 12. 排障 FAQ

### 12.1 没设置 Cookie 会怎样

`cookie_file` / `boost_cookie_file` 留空或文件不存在时，对应请求不携带 Cookie，
CLI 只警告不退出。统计后台通常需要登录，因此 `run` / `check` 可能抓取失败；
`boost` 的页面访问可匿名，但推荐列表接口是否可用取决于 Steam。

### 12.2 Cookie 失效 / 抓取报“未找到 g_StatsAppViews”

症状：`admin/overview` 被重定向到公开 curator 页，fetcher 提示
“请求被重定向到公开页面（可能 Cookie 已失效或未登录）”。

解决：

```bash
python curator_views/cli.py login --config config.yaml --validate-only
python curator_views/cli.py login --config config.yaml
```

### 12.3 启动后头几天为什么不刷量

- 历史点数 < `window_days` 时预测乘 `startup_factor` 保守系数；
- 启动期“目标 − 昨日自然流量”通常为 0 或负数，缺口为 0；
- 这是设计行为：先积累平稳基线，不急于刷量。

### 12.4 为什么今天没有刷量

常见原因：

- 缺口 ≤ 0（昨日自然流量已达到目标）；
- 处于恢复期（最近有恶意刷量标记）；
- `boost_enabled: false` 或使用了 `--no-boost`；
- 刷量列表为空或文件缺失；
- 推荐列表为空、Cookie 失效或提升连续失败被中止。

查看当天 `curator_views.log` 与 `run` 的 JSON 汇总可定位具体原因。

### 12.5 刷量列表没有变化

只有 `boosted > 0` 才消费列表行。以下情况会保留：`--dry-run`、提升关闭、
恢复期、缺口为 0、提升完全失败。列表文件缺失时每天只会告警，不会自动回退。

### 12.6 daemon 看起来没在跑

```bash
python curator_views/cli.py info --config config.yaml
```

若 PID 不存在，查看 `curator_views.log` 与 `daemon_state.json` 的 `last_error`。
确认 `daily_time` 是否正确、今天是否已经执行过（同日去重）。需要重启或停止时见
[10.1.1](#1011-重启与停止)。

### 12.7 PID 复用的误判

`info` 优先按 PID 判断存活。如果进程退出后 PID 被其他进程复用，可能误判为
运行中。此时以日志是否持续更新、`updated_at` 心跳是否新鲜作为辅助判断。

### 12.8 数据库被锁 / 并发读写

Storage 使用 WAL 与线程锁，单进程内并发安全。不要让多个 `run` / `daemon` 同时
写同一个数据库；`forecast` 使用只读 URI，不会加写锁。

### 12.9 手动修正某天的刷量记录

`boost` 手动刷量会自动写台账。若要覆盖历史某天，可直接操作 SQLite：

```sql
DELETE FROM boost_log WHERE date = '2026-11-01';
INSERT INTO boost_log (date, count, note, created_at)
VALUES ('2026-11-01', 500, 'manual', '2026-11-01T12:00:00');
```

修改前建议备份数据库。

---

## 13. 升级与迁移

### 13.1 单 Cookie 升级为双 Cookie

旧版本只有一个 `cookie_file`，同时用于统计与刷量。新版本拆成两份：

```yaml
cookie_file: "cookie.txt"          # 统计
boost_cookie_file: "cookie.txt"    # 刷量：想共用同一个号就显式写同一路径
```

**重要**：`boost_cookie_file` 留空不再复用 `cookie_file`，而是游客刷量。升级后
如果希望刷量保持登录态，必须显式配置。

### 13.2 启用每日刷量列表

```yaml
boost_list_file: "boost_plan.txt"
```

列表模式会覆盖预测缺口；列表用完即停。若只是临时排期，用完记得清空该配置，
否则系统不会再自动按预测刷量。

### 13.3 forecast 默认包含今天并改用自然缺口口径

`forecast` 现在默认从**今天 00:00** 起算（今天的数据次日才完整，所以预测今天
仍然是预测），并把“今天之前最近一个完整日”的 `实际 − boost_log 刷量` 作为自然
流量基准，未来日假设自然不变。因此表格会显示每天真实需要补的缺口，而不是把
历史高位当成自然流量后显示 0。

- 想从明天起算：`--tomorrow`；
- 想恢复旧的“实际趋势外推 / 收敛到目标”模型：`--natural trend` / `--natural target`；
- 本地库不是最新时会提示 `stale_history`，可加 `--fetch` 在线补齐（仅内存）。

### 13.4 --dry-run 不再产生副作用

`run --dry-run` 现在只计算，不写 `daily_stats` / `boost_log`，不写
`program_start` / `last_stats_date` / `last_run_date` / growth 锚点，也不发送
每日简报。这样可以放心用它预览当天缺口，不会让 daemon 误以为当天已经执行过。

### 13.5 配置兼容性

- 所有旧配置键仍然有效；
- 新增键都有默认值，不配置不会报错；
- `boost_cookie_file` 的行为变化见 13.1；
- `boost_distribution` / `executor_cmd` 未写入默认模板，可手动添加。

---

## 14. 安全与合规

- Cookie 是登录凭据，等同于账号权限。不要提交到版本控制、不要发到聊天工具、
  不要写进日志或截图。
- `.gitignore` 默认忽略 `cookie.txt`、`.steam_profile/`、`*.db`、`*.log`、
  `daemon_state.json`。自定义 Cookie 文件名（如 `cookie_stats.txt`）不在默认
  规则内，请自行加入 `.gitignore`。
- `config.yaml` 可能包含 `clan_id` 等本地信息，提交前请检查。
- 登录过程不会触碰密码，也不会打印 Cookie 内容；但浏览器持久化目录
  `.steam_profile*` 含登录态，请按敏感目录保护。
- 本工具仅用于自己运营的 Steam 鉴赏家组；请遵守 Steam 订阅者协议与当地法律。

---

## 15. 开发与测试

```bash
python -m pytest tests/ -v
```

当前测试共 **204** 个，覆盖：

- 预测边界、趋势封顶、启动期系数；
- 溢出检测连续段、长期基线、持续斜率；
- 手动基线、growth 模式、growth_rate 锚点、波动率、恢复期、标记日剔除；
- 调度器子进程执行器、时区工具；
- 每日刷量列表的解析、peek、消费与异常保留；
- 双 Cookie 解析、未登录回退、Cookie 规范化与校验；
- `forecast` 的逐日估算、默认含今天/自然口径、`--tomorrow`、警告、只读数据库行为、
  表格对齐与容量备注、按 `threads` / 等待间隔估算的吞吐上限；
- daemon 的 plan_next、同日去重、状态与 info 输出。

测试目录：

```
tests/
├── conftest.py
├── test_anti_cheat.py
├── test_booster.py
├── test_cli_info.py
├── test_cli_logging.py
├── test_convergence.py
├── test_cookie_optional.py
├── test_daemon.py
├── test_forecast.py
├── test_login.py
├── test_predictor.py
├── test_run_daily.py
├── test_scheduler.py
└── test_status.py
```

提交规范：中英双语 `type(scope): English / 中文`，一个提交只做一件事。

---

## 16. 免责声明

本工具仅用于提升**自己运营**的 Steam 鉴赏家评测的浏览量。请勿用于对其他鉴赏家组
或平台进行恶意操作。使用本工具产生的任何后果（包括但不限于 Steam 账号异常、
鉴赏家资格被撤销等）由使用者自行承担。请在遵守 Steam 订阅者协议与当地法律法规的
前提下使用。Cookie 属于敏感信息，请勿将 Cookie 文件提交到版本控制或分享给他人。