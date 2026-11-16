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
- **目标有安全边界**：`target_min` / `target_max` 是每日总量的绝对硬边界
  （自然流量 + 刷量），抖动、刷量列表与手动 `boost` 都不得突破上限；`0` 表示
  该方向不设界，run / daemon 实际执行与 forecast 预览都使用同一口径。
- **配置可预测**：正式执行前可用 `forecast` 只读推算今天/明天起 N 天的目标与缺口，
  判断配置会不会空转或超载；`--chart` 可直接在控制台输出点状字符折线图
  （目标/自然流量/配额/待刷），`--chart PATH` 还可写自包含 HTML 用浏览器查看。

### 1.2 它不做什么

- 不提供 Steam 账号注册、自动过验证码或绕过 Steam Guard 的能力；登录始终是
  半自动的浏览器操作。
- 不保证提高自然推荐曝光或排名，只维护浏览量的平稳。
- 不会自动修改历史统计数据；需要修正时只能通过 `boost` 或直接操作数据库。
- 不适用于攻击、抹黑或干扰其他鉴赏家组；仅限自己运营的组。

### 1.3 运行形态

- 单机 CLI：`python curator_views/cli.py <command> --config config.yaml`
- 常驻守护进程：`daemon` 子命令按配置的时刻执行完整流程，`daily_times` 可配置一天多个时段
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
                        每日到点（默认 10:00；可配置多个时段 daily_times，如 10:00 / 20:00）
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

今日目标     = clamp(基准预期 × 目标系数 × (1 + 目标抖动), target_min, target_max)
              stable 模式：目标系数 = 1
              growth 模式：目标系数 = growth_factor × (1 + growth_rate × 距生效日天数)
              target_min / target_max 为 0 时表示该方向不设界
              目标抖动 = ±uniform(target_volatility_min, target_volatility)  # 0 时关闭

昨日自然流量 = max(0, 昨日实际 − 昨日刷量)

未抖动缺口   = max(0, round(今日目标 − 昨日自然流量 − 溢出扣减))
当日配额     = max(0, round(未抖动缺口 × (1 + 缺口抖动)))
              缺口抖动 = ±uniform(volatility_min, volatility)  # 0 时关闭

边界夹取（绝对硬限制，所有刷量来源都要过）：
刷量下限 = max(0, target_min − 昨日自然流量)   # target_min > 0 时
刷量上限 = max(0, target_max − 昨日自然流量)   # target_max > 0 时
当日配额 = clamp(当日配额, 刷量下限, 刷量上限)  # 先夹上限，再夹下限
          下限只在配额 > 0 时生效（不刷的状态不会被强行托底）；
          target_min / target_max 为 0 表示该方向不设界
今日缺口 = max(0, 当日配额 − 今日已刷)

今日已刷     = boost_log 里今天的累计（同一日重跑时按剩余配额续刷）
实际刷量     = 今日缺口                      # 抖动已并入“当日配额”
              （list 模式使用列表首行数值，同样按上面的上下限夹取后使用）

当日总量     = 昨日自然流量 + 今日已刷 + 实际刷量
              上限是绝对硬限制；自然流量本身已超上限时只能不刷，
              总量仍会超过上限。下限约束的是“决定下发的刷量”，不是平台
              最终送达量：提升失败、容量/吞吐或 target_ratio 截断时，
              总量仍可能低于下限。

同一组目标/缺口抖动按「日期 + 波动配置签名」持久化：
同一天重跑、daemon 多时段都复用同一值，后续时段只补「配额 − 今日已刷」。

最终决策（目标、抖动、缺口、配额、恢复期/列表状态）同时写入 `daily_plan` 表：
`forecast` 首日在配置签名一致时直接复用该定档值，不再重新抽样；未来日仍是
独立抽样的预测。

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

- `growth_factor` 管“台阶高低”：`1.5` 表示目标是基准的 1.5 倍；`0.85` 表示先把
  目标压到基准的 85%（**仅固定基准时**才是“跌破才托住”）。
- `growth_rate` 管“斜率”：`0.01` 表示目标系数每天增加 `growth_factor × 1%`
  （例如 `growth_factor=0.85` 时约 +0.0085），`-0.01` 则每天下降。
- ⚠️ **自动基线（`baseline_views: 0`）配 `growth_factor < 1` 会逐日下滑**，
  不是“跌到 85% 就托住”：每天刷到的 `target`（基准的 85%）会进入下一天的预测
  窗口，把基准本身压低，于是目标继续下探，`growth_rate` 的 +1%/天远远补不回来。
  需要“降一次后稳住”时必须用 `baseline_views` 固定基准。机制与示例见
  [7.6](#76-基线与波动参数)。
- 速率锚点持久化在 `kv_store`：同一速率跨重启沿用原起点；修改速率后从当天重新
  起算，避免历史天数突然生效造成目标跳变。
- 检测参照与补缺口目标同步：预测阶段使用 `base × max(1, multiplier)` 作为异常
  判断参照，避免把自己刷出来的量误判为恶意刷量；配置波动率时参照再乘
  `(1 + target_volatility + volatility)`（长期基线同样放大），为自身抖动留出余量。
- `forecast` 输出的“目标”是托底目标 `base × 目标系数（multiplier）`，当
  `growth_factor < 1` 时与检测参照不同，不能混用。
- `target_min` / `target_max` 是每日总量的绝对硬边界（0 表示该方向不设界）：
  当日总量 = 昨日自然流量 + 今日刷量，所有实际下发的刷量都会被夹进
  `[target_min − 昨日自然, target_max − 昨日自然]`。上限防止 growth 目标随天
  无限上涨，抖动、刷量列表与手动 `boost` 都不得突破；自然流量本身已超上限时
  只能不刷，总量仍会超限。下限在“今天确实要刷量”时托底；自然流量已达标、
  恢复期、`boost_enabled: false`、列表为空/0 等不刷状态不会被强行补量。
  `run` / `daemon` 实际执行与 `forecast` 预览使用同一口径；下限同时抬高短期与
  长期反作弊参照，避免把自己托底刷出来的量误判为恶意刷量。
- 下限约束的是“决定下发的刷量”，不是平台最终送达量：提升失败、被容量/吞吐
  或 `target_ratio` 截断时，实际总量仍可能低于下限。

### 3.5 缺口与昨日自然流量

不能直接用“昨日实际”当今天的自然流量基准，因为昨天实际里包含昨天自己刷的量，
重复扣减会导致目标永远补不满、并在目标附近两天一摆。系统因此：

```
昨日自然流量 = max(0, 昨日实际 − boost_log 记录的昨日刷量)
未抖动缺口   = max(0, 今日目标 − 昨日自然流量 − 溢出扣减)
当日配额     = 未抖动缺口 × (1 + 缺口抖动)      # volatility > 0 时
当日配额     = clamp(当日配额,
                    max(0, target_min − 昨日自然流量),  # 仅配额 > 0 时托底
                    max(0, target_max − 昨日自然流量))
今日缺口     = max(0, 当日配额 − 今日已刷)
今日已刷     = boost_log 里今天的累计
```

台账缺失时刷量按 0 处理（兼容旧数据）。刷量记录大于实际值时自然流量夹到 0，
避免补成负数或超额补量。

**当日已刷**用于同一天重跑：`run` 中断、被容量/吞吐限制截断、或你手动再跑一次时，
缺口会先减去 `boost_log` 里今天已经成功的量，只补剩下的部分，不会把整份缺口
再刷一遍。例如目标缺口 5,842、已刷 1,500，再跑一次只补 4,342。

刷量列表模式例外：列表一行代表“当天总量”，当天已经刷过就不再续刷（`already_boosted`），
避免重新读取首行成功后又消费掉下一行、把明天的配额提前用掉。

### 3.6 波动率：让曲线不那么机械

系统有两个独立的波动率，**作用对象不同**：

| 配置 | 作用对象 | 抖动公式 | 适合 |
| --- | --- | --- | --- |
| `volatility` | 缺口 → 当日配额 | `配额 = round(缺口 × (1 + 抖动))` | 缺口很大时，控制每日刷量幅度 |
| `target_volatility` | 今日目标 | `目标 = 目标 × (1 + 抖动)` 后再夹上下界 | 固定基线/缺口很小时，让总浏览量自然起伏 |

两者的抖动都是“带符号的幅度”：先在 `[下限, 上限]` 内均匀抽幅度，再随机取正负号。
`*_min` 给幅度设下限（`volatility_min` / `target_volatility_min`），防止抽到
接近 0 的抖动导致曲线过于平缓；`0` 表示不设下限，上限为 `0` 表示关闭该波动率。

```
目标抖动 = ±uniform(target_volatility_min, target_volatility)
缺口抖动 = ±uniform(volatility_min, volatility)
```

**行为细节：**

- **同日复用**：目标抖动与缺口抖动按「日期 + 波动配置签名」写入 `kv_store`
  的 `daily_jitter`。同一天重跑、daemon 多时段都复用同一组值；改任一波动相关
  配置（含基准/模式/上下界）会从当天重新抽取。
- **缺口抖动作用于当日配额**：后续时段只补 `配额 − 今日已刷`。负向抖动不会被
  后面的时段补平，正负偏差都能体现在当天总量上。
- **目标抖动先于上下界**：抖动后的目标会被 `target_min` / `target_max` 夹取；
  刷量配额还会再按 `[target_min − 自然, target_max − 自然]` 夹一次，上限是
  当日总量的绝对硬上限。
- **恢复期优先**：`in_cooldown` 时强制不刷，不参与抖动，也不会被下限托底。
- **列表模式优先**：`boost_list_file` 的数值在抖动之后覆盖配额，列表数值不抖动，
  但仍会被 `target_min` / `target_max` 硬边界夹取（行 0 保持不刷）。
- **无硬性上限**：`volatility` / `target_volatility` 不再限制 0.3 或 0.15；但抖动
  幅度会同步放大反作弊检测参照（`×(1 + target_volatility + volatility)`），
  配得越大，对真实外部刷量的检出越迟钝，需要自行权衡。
- **forecast 首日复用定档**：`run` 会把当天最终决策写入 `daily_plan`；配置签名
  一致时 `forecast` 首日直接读取该定档值（不再重新抽样），因此显示的缺口与
  `run` 当天的执行口径一致。签名不一致时丢弃定档并重新抽样，同时给出
  `plan_signature_mismatch` 提示。未来日仍按 `run` 的口径独立抽样目标/缺口抖动；
  `--no-volatility` 可关闭随机化、输出未抖动基准（不读取定档），并给出
  `volatility_not_modeled` 提示。`target_low` / `target_high` 始终给出目标抖动
  区间。

例 1（缺口抖动）：目标 1000、昨日自然 900、缺口 100、`volatility: 0.10`，
今天配额 90 ~ 110，总量约 990 ~ 1010。

例 2（目标抖动）：`baseline_views: 10000`、`boost_mode: stable`、
`target_volatility: 0.50`，则今天目标在 5000 ~ 15000 之间随机；若昨日自然流量
约 9000，缺口约 0 ~ 6000。叠加 `volatility: 0.10` 时，配额再浮动 ±10%。

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

- 每天取**第一条有效行**作为今日提升目标，直接覆盖预测/基线算出的缺口；该数值
  仍会被 `target_min` / `target_max` 夹取（超过上限按上限、低于下限按下限，
  并记录 warning），列表行 `0` 保持不刷、不会被托底。
- 成功提升（`boosted > 0`）后，该行从文件中删除；其余行与换行符原样保留，
  采用临时文件 + `os.replace` 原子写回，中途失败不会写坏列表。
- 以下情况**不消费**列表行：`--dry-run`、`boost_enabled: false`、恢复期、
  缺口为 0、提升完全失败（`boosted = 0`）。
- 列表为空时**默认自动回退**到基线/自动预测口径继续刷量（`boost_list_fallback:
  true`，简报会升为 warning 提示“已回退基线”）；设 `boost_list_fallback: false`
  则用完即停、不回退自动预测。文件缺失/读取失败属于配置错误，即使开启回退也不
  回退，仍会在每日简报中标记为 warning。
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

#### 3.8.1.1 “平台抬升”具体指什么

“平台抬升”（platform-wide uplift）指**连续多天、幅度温和地注入浏览量，把整条
曲线的水位一起抬高**，而不是制造某一天的孤立尖峰。它的危险在于能让短期基线
“自我掩护”：

1. 短期 `expected` 取最近 `window_days`（默认 14 天）的中位数；
2. 每天只多注入一点，单日达不到 `outlier_k`（默认 30%）阈值，连续段判定也不触发；
3. 14 天中位数被一天天推高，`expected` 随之上移；
4. “实际 vs 预期”的差距被抹平，被污染的数据反而成了新的“正常水平”，
   真正的外部刷量从此测不出来。

也就是说，平台抬升不是“某天数据不正常”，而是**基准线本身被污染了**。

三种检测手段针对的点不同，不能互相替代：

| 检测手段 | 抓什么 | 对“平台抬升”是否够用 |
| --- | --- | --- |
| 相对预期超额 | 单日或连续的尖峰 | 不够：`expected` 本身会被同步抬高 |
| 长期基线超额 | 整条水位被抬 | 是：60 天中位数变化慢，短期注入推不动它 |
| 持续上升斜率 | 每天温和爬升 | 辅助：只标记高于长期基线的高位日，仍依赖长期基线 |

所以 `baseline_days: 0` 不只是“少一条检测”，而是直接失去对平台抬升的免疫——
只剩会被同步抬高的短期预期，等于没有独立参照。

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
7. `threads > 1` 时并发访问；线程池大小即配置的 `threads`（无内置上限），
   实际并发还受 `boost_n` / `batch_size` 与批内活跃推荐数限制。
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

子进程超时 7200 秒；调度器解析 stdout 中最后一个 JSON 对象的 `boosted` 字段
（单行、缩进多行、前后夹带日志都能识别）。若执行器写入同一个 `db_path`，调度器
以执行前后 `boost_log` 的差值为准（`boosted` 仅在没有台账新增时兜底），因此即使
执行器不打印 JSON，本次刷量数也准确。执行器
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
        ├── chart.py         # forecast 折线图（控制台点状字符 + 自包含 HTML/SVG）
        ├── boost_list.py    # 每日刷量列表解析与原子消费
        ├── booster.py       # 受控刷量执行器
        ├── scheduler.py     # 每日闭环编排 + 子进程执行器
        ├── daemon.py        # 常驻调度循环
        ├── status.py        # 守护进程状态文件与 PID/心跳判断
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
#    target_min / target_max —— 目标浏览量上下界，0=不设界，可选
#    boost_mode / growth_factor / growth_rate —— 目标计算方式，可选

# 3. 半自动登录，生成统计用 Cookie
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_stats.txt --profile-dir .steam_profile_stats

# 4. 如需刷量也带登录态，再生成一份刷量 Cookie
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_boost.txt --profile-dir .steam_profile_boost

# 5. 巡检：抓取统计并写入本地库，同时预览预测（不产生提升请求）
python curator_views/cli.py check --config config.yaml

# 6. 只读估算未来 7 天的目标与缺口
python curator_views/cli.py forecast --config config.yaml --days 7

# 6b. 只看今天该刷多少（默认含今天，首日自然 = 昨日实际 − 昨日刷量）
python curator_views/cli.py forecast --config config.yaml --fetch

# 6c. 控制台点状折线图；--chart PATH 可改为写 HTML
python curator_views/cli.py forecast --config config.yaml --chart

# 7. 演练模式：完整计算但不真正刷量
python curator_views/cli.py run --config config.yaml --dry-run

# 8. 正式执行一次每日完整流程
python curator_views/cli.py run --config config.yaml

# 9. 常驻守护进程（推荐长期运行）
python curator_views/cli.py daemon --config config.yaml
```

> **修改代码后需要重启 daemon**；只改配置不用重启（每轮热加载）。重启方法见
> [10.1 daemon 的「重启与停止」](#101-daemon推荐)。

> **首次运行注意**：程序启动日之前的 Steam 历史（约 364 天）**不参与预测**。
> 这些数据里可能混着以往不规则刷量和他人恶意刷量，不可靠。系统从启动日起积累
> 自己的平稳数据；头 `window_days` 天（默认 14 天）处于启动期，预测会乘
> `startup_factor`（默认 0.9）保守系数，缺口通常为 0，这是正常现象。

---

## 7. 配置参考

配置由 `init-config` 生成，相对路径均基于配置文件所在目录解析。所有日期与时间
使用 **Asia/Shanghai** 时区。

配置文件只需写关心的键：**没写的键自动回退到下文表格里的模板默认值**，最小配置
只写 `clan_id` 也能启动（`clan_id` 仍是必填项）。只写键名（值为 `null`）同样视为
“没写”；显式写空串 / 空列表 / `0` 则按字面值生效（例如 `cookie_file: ""` 表示
主动关闭 Cookie、以游客模式运行）。

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
| `ntfy_enabled` | `true` | 推送总开关；`false` 时所有通知只写日志，不排队也不补发 |
| `ntfy_min_level` | `"info"` | 最低推送级别 `info` / `warning` / `error`；低于该级别只写日志 |
| `ntfy_daily_summary` | `true` | `false` 时只抑制例行（`info` 级）每日简报；warning/error 级简报与告警照常推送 |
| `ntfy_quiet_hours` | `""` | 静默时段，如 `"23:00-08:00"` 或 `"12:00-13:00,23:00-08:00"`；留空不启用 |

两者都配置且 `ntfy_enabled` 为 `true` 时才启用 ntfy 推送；否则只写日志。
每日简报在以下情况升级为 warning：
存在溢出标记、处于恢复期、Booster 中止、执行器报错、刷量列表文件缺失/读取失败/
消费失败。

静默时段（`ntfy_quiet_hours`）支持多个 `HH:MM-HH:MM` 区间，用逗号、分号或空白
分隔；开始时刻含、结束时刻不含，开始晚于结束表示跨午夜（如 `23:00-08:00`）。
静默期内命中推送条件的通知不会立即发送，而是写入数据库同级的
`notify_queue.json`（队列上限 50 条，超出丢弃最旧并记录 warning）；级别过滤与
简报开关在入队前生效，被过滤的通知不会进入队列。daemon 在每次主循环与睡眠期间的
30 秒心跳里尝试补发，`run` 在开始执行时补发（非 dry-run），因此离开静默后一般很快
送达；补发中途某条失败会保留该条及其后所有条目，等下个周期重试。`--dry-run`
不发送也不补发，只写日志。

### 7.3 守护进程配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `daily_times` | `[]` | 每日执行时刻列表，如 `["10:00", "20:00"]`；非空时优先于 `daily_time`；每个时段执行一次完整流程，后一时段按当日已刷补剩余缺口 |
| `daily_time` | `"10:00"` | 单时刻回退配置，`daily_times` 为空时生效；格式 `HH:MM` |
| `retry_delay_minutes` | `30` | 每个时段执行失败后的重试间隔（分钟），`0` 表示该时段内不重试 |
| `retry_max_times` | `3` | 每个时段最多重试次数（不含首次执行），用尽后放弃该时段 |

`daemon` 每次循环都会重新加载配置文件，修改任意配置（含执行时间、重试参数）无需重启。
`daily_times` 接受列表，也接受逗号或空格分隔的字符串；非法条目跳过并记录警告。
`daily_times` 为空（或未配置）时回退到 `daily_time`；完全解析不出有效时刻时回退到
`10:00` 并记录警告。

执行失败（网络、Cookie 失效、抓取异常等）**不会**把该时段标记为已完成：daemon 会按
`retry_delay_minutes` 间隔在该时段内重试，`retry_max_times` 次仍失败才放弃该时段。
放弃后也不会写 `last_run_slot`，因此可以随时手动 `run` 补跑，或等下一个时段/第二天
自动执行。一天多个时段时，各时段独立重试，某时段重试用尽不会阻塞当天后续时段。

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
| `target_min` | `0` | 当日总量下限（自然+刷量）；> 0 且今天要刷量时至少补到该值，`0` 不设界 |
| `target_max` | `0` | 当日总量上限（自然+刷量）；> 0 时刷量不得让总量超限（抖动/列表/手动 boost 均受约束），`0` 不设界 |
| `volatility` | `0.10` | 缺口随机抖动幅度：当日配额 = 缺口 × (1 ± 波动率)；`0` 关闭 |
| `volatility_min` | `0` | 缺口抖动的最小幅度，防止抽到接近 0 导致曲线过平；`0` 不设下限 |
| `target_volatility` | `0` | 目标随机抖动幅度：目标 × (1 ± 波动率) 后再夹上下界；`0` 关闭 |
| `target_volatility_min` | `0` | 目标抖动的最小幅度；`0` 不设下限 |

**说明：**

- `baseline_views > 0` 时，今日基准直接使用该固定值，溢出检测也以它为参照。
  适合“希望稳定在 1000 左右”这类明确目标。
- `boost_mode: stable` 下 `growth_factor` 与 `growth_rate` 都不生效。
- `growth_factor < 1` 表示允许曲线先跌到基准的某个比例，跌破才托住；
  `> 1` 表示主动抬高台阶。**前提是基准固定**（`baseline_views > 0`），
  自动基线下它不会“托住”，见下一条。
- ⚠️ **`baseline_views: 0`（自动基线）配 `growth_factor < 1` 会逐日下滑**：
  不要把 0.85 理解成“只降一次就托住”。`run` 每天把总量刷到
  `target = base × 0.85`，这条真实数据第二天进入预测窗口，把 14 天中位数和
  趋势斜率一起往下压；`forecast` 用同一口径，把每天假设的 `target` 回填进
  历史，所以它的「基准」列也会逐日走低。结果是 `target` 连续下跌，
  `growth_rate: 0.01` 的 +1%/天 远不足以抵消 15% 的台阶差，最终会一路跌到
  `target_min`（未配置则趋近 0）并停在那里。`forecast --days 14` 可以直接
  看到这个轨迹：基准与目标逐日下滑，最后被下限钉住。

  想让 0.85 只降一次就稳住，必须把基准钉死：

  ```yaml
  baseline_views: 7000     # 固定基准，关闭自动预测
  boost_mode: growth
  growth_factor: 0.85      # 目标 = 7000 × 0.85
  growth_rate: 0           # 想继续缓降再填负数（如 -0.01）
  ```

  固定基准下 `target` 只由系数决定，不会再被历史回填反噬；这时
  `growth_rate` 的正负才真正等于“每天涨/跌百分之几”。
- `growth_rate` 与 `growth_factor` 相乘，可同时使用；修改速率会从当天重新起算。
- `target_min` / `target_max` 是刷量的绝对硬边界，缺口在
  `目标 − 昨日自然流量 − 溢出扣减` 算出后，还会按
  `[target_min − 昨日自然, target_max − 昨日自然]` 夹取；两者都为 `0` 时不设界。
  `run` / `daemon` 实际执行与 `forecast` 预览使用同一口径。上限对所有刷量来源
  （自动缺口、缺口抖动、刷量列表、手动 `boost`）都生效；自然流量本身已超上限
  时只能不刷，总量仍会超限。下限只在今天确实要刷量时托底：自然流量已达标、
  恢复期、`boost_enabled: false`、列表为空/0 等不刷状态不会被强行补量。
  下限会同步抬高短期与长期检测参照，托底刷量不会被判成恶意刷量。注意下限约束
  的是“决定下发的刷量”，提升失败或被容量/吞吐/`target_ratio` 截断时，实际
  总量仍可能低于下限。
- `volatility` 作用于缺口（当日配额），`target_volatility` 作用于今日目标；
  两者抖动值按日期持久化，同一天重跑/多时段复用。带 `*_min` 时幅度在
  `[min, max]` 内均匀取值、方向随机，避免曲线过于平缓。两者都没有硬性上限，
  但会按 `1 + target_volatility + volatility` 放大反作弊检测参照。详见
  [3.6](#36-波动率让曲线不那么机械)。

### 7.7 刷量列表

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `boost_list_file` | （空） | 每日刷量列表文件路径；非空启用列表模式 |
| `boost_list_fallback` | `true` | 列表耗尽（文件存在但无有效行）时自动回退基线/自动预测；`false` = 用完即停 |

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
| `threads` | `1` | 并发线程数（无内置上限，实际并发受 `boost_n` / `batch_size` 限制）；建议保持 1 |
| `wait_min` / `wait_max` | `8.0` / `16.0` | 两次请求之间的随机等待秒数 |
| `max_retries` | `3` | 连续失败达到该次数后中止本轮 |
| `executor_cmd` | （空） | 执行器子进程命令模板；留空则同进程直调 |

单日实际吞吐还受 `threads` 与 `wait_min` / `wait_max` 限制：乐观上限为
`实际并发（min(threads, boost_n, batch_size)）× 86400 /
max(wait_min, wait_max)`（未计请求耗时）。`forecast` 在缺口超过该上限时给出
`throughput_capacity` 警告；`run` 也会记录警告并在简报中提示。`threads` 没有
内置上限，但并发数不会超过推荐数 `boost_n` 与批次大小 `batch_size`。

并发越高，同一秒打到 Steam 的请求越多、越接近机器行为；且 `threads > 1` 时中止
判定按“批内失败总数”累计，不如顺序模式精确。除非单日吞吐确实不够，否则保持
`threads: 1`；详见 [12.11](#1211-并发线程数最高能到多少)。

单轮容量 `boost_n × single_cap` 是另一条硬限制：缺口超过它时 `run` 同样会给出
`capacity_warning`（“本轮跑不满”），稍后再次 `run` 会按当日已刷扣减后继续补剩余。

`boost_distribution: exact` 示例：目标 500、最近 30 条推荐 →
前 20 条各 17 次、后 10 条各 16 次，全部配额刷满即恰好 500 次。

`executor_cmd` 占位符：`{config}`、`{need}`、`{apps}`。子进程超时 7200 秒。

### 7.9 完整默认配置

`init-config` 生成的模板包含以下内容（`executor_cmd` 与 `boost_distribution`
为可选键，模板未列出，默认值分别为空串和 `ratio`，需要时手动添加）：

```yaml
clan_id: ""
cookie_file: "cookie.txt"
boost_cookie_file: ""
proxy: ""
db_path: "views.db"

ntfy_url: ""
ntfy_topic: ""
# 是否启用 ntfy 推送（false 时所有通知只写日志，不排队也不补发）
ntfy_enabled: true
# 最低推送级别：info / warning / error；低于该级别的通知只写日志
ntfy_min_level: "info"
# 成功简报是否推送；false 时仅 warning/error 级别的简报照常推送
ntfy_daily_summary: true
# 静默时段，如 "23:00-08:00" 或 "12:00-13:00,23:00-08:00"；留空不启用
ntfy_quiet_hours: ""
boost_enabled: true

daily_times: []
daily_time: "10:00"
retry_delay_minutes: 30
retry_max_times: 3

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
# 目标浏览量上下界（0=不设界）
target_min: 0
target_max: 0
volatility: 0.10
volatility_min: 0.0
target_volatility: 0.0
target_volatility_min: 0.0

boost_list_file: ""
boost_list_fallback: true

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
| `daemon` | 常驻运行，按配置时刻执行每日流程（`daily_times` 支持多时段，配置热重载） |
| `info` | 查看守护进程状态与本地数据概况（离线只读） |
| `check` | 抓取统计并写入本地库，打印最近 7 天与预测摘要（`--no-write` 只读） |
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

同一天可以安全地重复执行：缺口会先减去 `boost_log` 里今天的累计（`today_boosted`），
只补剩余部分。中断、容量/吞吐截断或手动补跑都按这个口径续刷，不会把整份缺口
再刷一遍。

成功后以 JSON 打印汇总（日期、今日预期、昨日实际/刷量/自然流量、今日已刷、缺口、
溢出扣减、异常天数、实际提升数、列表状态等），并在数据库同级目录写
`curator_views.log`。

### 8.2 daemon

```bash
python curator_views/cli.py daemon --config config.yaml [--dry-run] [--boost | --no-boost]
```

行为要点：

- 启动时若今天尚有未执行的时段且已过其时刻，立即补跑这些剩余时段；
- 每轮循环重新加载配置，修改配置或执行时间无需重启；**修改 Python 代码后必须重启进程**；
- 执行失败不退出进程，并**不把该时段标记为已完成**：按 `retry_delay_minutes`
  在该时段内重试，最多 `retry_max_times` 次；用尽后放弃该时段，当天后续时段照常
  执行，之后仍可手动 `run` 补跑（单时段时等同于等第二天）；
- 时段级去重：`daemon_state.json` 用 `last_run_slot`（`YYYY-MM-DD#HH:MM`）记录最近
  完成的时段，重启后当天只补跑尚未完成的时段；
- 每个时段执行完整流程（抓取 → 预测 → 检测 → 提升 → 通知）。目标/缺口抖动
  按天复用，后一时段只补 `need = max(0, 当日配额 − 今日已刷)`，因此第二时段
  通常比首轮短，负向抖动也不会被补平；
- 手动 `run` 只写日期级 `last_run_date`，daemon 视“此刻之前的所有时段”已完成，
  之后的时段仍会执行以补足缺口；
- 时段串行执行：上一时段的提升还没结束时，下一时段会等它结束后再开始（不并发），
  所以不要把时段安排得太密；
- 统计 Cookie 失效会单独告警（ntfy 或日志），重新 `login` 覆盖 Cookie 文件后，
  下一次重试会自动读取新文件，**不需要重启 daemon**；
- 同一时段不重复执行：`last_run_slot` 写入 `daemon_state.json`，`last_run_date`
  同时写入数据库与状态文件（`--dry-run` 不写数据库侧，因此不会顶掉当天正式执行）；
- Ctrl+C（SIGINT）立即中断当前执行并退出；SIGTERM 优雅收尾后退出；
- 前台运行时 INFO 日志同时输出到控制台。

后台运行示例：

```bash
nohup python curator_views/cli.py daemon --config config.yaml >/dev/null 2>&1 &
```

建议配合 systemd `Restart=always` 或 Windows 服务包装器保持常驻。
重启、停止与状态查看见 [10.1 daemon 的「重启与停止」](#101-daemon推荐)。

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
python curator_views/cli.py check --config config.yaml [--no-write]
```

抓取统计数据并**写入本地数据库的 `daily_stats`**（与 `run` 的抓取入库一致），
然后打印最近 7 天与预测摘要。不产生提升请求。

- 只写 `daily_stats`：不写 `boost_log`，也不写 `program_start` / `last_run_date` /
  `last_stats_date` / growth 锚点，因此不会被 daemon 当成“今天已执行”；
- `--no-write`：只抓取并打印，不建库不写库，保持旧版纯只读行为；
- 数据库打不开（路径不可写等）时报错并以 1 退出，其余情况下照常打印统计与预测。

写库后可以直接用 `forecast` 看到最新一天，不必先跑一次完整 `run`；`check` 抓到的
数据对次日 `run` 也是幂等覆盖（`daily_stats` 以 `date_ts` 为主键）。

### 8.5 forecast

```bash
python curator_views/cli.py forecast --config config.yaml \
  [--days N] [--fetch] [--today | --tomorrow] [--natural trend|target] \
  [--set KEY=VALUE ...] [--no-volatility] [--chart [PATH]] \
  [--day-step DAYS] [--chart-only] [--json]
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
- `--no-volatility`：不把 `target_volatility` / `volatility` 纳入预测，输出
  未抖动基准；默认纳入，每天独立抽样（`summary.volatility_modeled` 标记）；
- `--chart [PATH]`：不带 PATH 时在控制台输出点状字符折线图（`·` 连线，
  `●` / `○` / `◆` / `◇` 标记数据点）；带 PATH 时写自包含 HTML（内联 SVG，
  无第三方依赖，相对路径基于配置文件目录），`--json` 时路径写入
  `source.chart_path`；`--chart -` 也表示控制台；`--json` + 控制台图时图表
  写入 stderr，标准输出仍是纯 JSON；
- `--day-step DAYS`：控制输出间隔，表格与图每隔 DAYS 天显示一条/一个点，
  默认 1（每天）。同时影响表格、控制台/HTML 图和 JSON 的 `rows`；
  汇总与警告仍按完整预测区间计算。从首日开始取样，首尾日期始终保留；
  若最后一天不是间隔点会额外补上，因此末尾间隔可能小于 DAYS；
- `--chart-only`：只输出图表本身，不打印 forecast 表格、图外说明与汇总；
  未指定 `--chart` 时自动使用控制台图，不能与 `--json` 同用；
- `--json`：输出 `rows` / `warnings` / `summary` / `source` 完整结构；
  `source.today_boosted` 是今天的已刷量（`--tomorrow` 时为 `null`），
  `rows[0].already_boosted` 是首日扣除的已刷量，其余行恒为 0。

首日缺口同样按 `run` 的口径减去“今日已刷”，所以同一天重跑 `run` 前后执行
`forecast`，看到的缺口就是接下来还需要补的量。

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
未来日的 `in_cooldown` 恒为 `false`，也不做溢出扣减；自然口径同样不含恢复期与
刷量列表，只复用 `run` 的缺口公式。首日则会读取 `run` 写入 `daily_plan` 的定档
决策：配置签名一致时「目标」「缺口」「配额」直接采用 `run` 当天的最终值，
`rows[0].plan_source` 为 `persisted`，并显示「今日目标: 已定档 …」；签名不一致、
没有定档或使用 `--no-volatility` 时退回抽样/基准预测（`plan_source` 为
`predicted`）。未来日始终是独立抽样：每天独立抽样 `target_volatility` /
`volatility`，`target_low` / `target_high` 给出目标可能区间。`--no-volatility`
输出未抖动基准，并给出 `volatility_not_modeled` 提示。只有首日复用定档值，
同一天反复执行 `forecast` 时未来日结果仍可能不同。`summary.first_plan_status`
记录首日定档状态（`persisted` / `signature_mismatch` / `missing` /
`ignored_no_volatility`），`summary.first_plan_updated_at` 为定档写入时间。

forecast 输出的是托底目标 `base × 目标系数（multiplier）`；`run` 的检测参照是
`base × max(1, multiplier) × (1 + target_volatility + volatility)`。当
`growth_factor < 1` 或配置了波动率时两者不同，不要把托底目标当成检测参照。

输出示例：

```
预测区间: 2026-11-14 ~ 2026-11-20（7 天，含今天）
数据来源: 本地数据库 E:\...\views.db（18 天，最新 2026-11-13 = 5,449 次）
自然口径: 基准日 2026-11-13 实际 5,449 - 刷量 0 = 自然 5,449；未来日假设自然不变
今日已刷: 0 次（2026-11-14，来自 boost_log；首日缺口已按剩余量计算）
今日目标: 已定档 7,094.2（run 于 2026-11-14T10:00:00 写入数据库；首日不再重新抽样）
参数: boost_mode=growth factor=0.85 rate=-0.01
      基线=auto 自然模型=natural 缺口波动率=0.10（最小 0.00） 目标波动率=0.00（最小 0.00）
波动率: 已纳入预测（首日取数据库定档值；未来日独立抽样）
单日容量: 1,500 次（boost_n=5 × single_cap=300）

日期           基准  目标系数     目标  假设自然  今日已刷   缺口  备注
-----------------------------------------------------------------------
2026-11-14  8,430.4     0.842  7,094.2     5,449         0  1,645  容量不足
2026-11-15  8,352.3     0.833  6,957.5     5,449         -  1,508  容量不足
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

表格里的「今日已刷」列只在首日（今天）显示 `boost_log` 的当日累计，未来日为 `-`；
同一天重跑时首日缺口已经按剩余量算好。「备注」列会在缺口超过单日容量时显示
`容量不足`（容量为 0 时显示 `无容量`），在受线程/等待间隔限制时显示 `吞吐不足`；单日容量 =
`boost_n × single_cap`，单日吞吐上限 = `min(threads, boost_n, batch_size) ×
86400 / max(wait_min, wait_max)`（未计请求耗时），都会在表格上方单独列出。

配置 `target_min` / `target_max` 时，表格上方会多一行「目标上下界」，表格里的
「目标」是夹取后的值；JSON `rows` 中 `raw_target` 保留夹取前的原始目标，
`target_bound` 标记目标命中的边界（`min` / `max` / 空字符串）。刷量本身还有
独立的硬边界：`need` 会按 `[target_min − 假设自然, target_max − 假设自然]` 夹取，
`bound_hit` 标记刷量命中的边界；有命中时表格上方额外显示「目标边界」一行，
`summary.bound_hits` 给出上下限命中天数，`summary.target_floor_boost` /
`target_ceiling_boost` 给出首日（提供自然流量时）对应的可刷量。与 `run` 口径一致。

常见 warning：

| code | 含义 |
| --- | --- |
| `no_history` | 本地没有可用历史数据，所有基准为 0 |
| `need_all_zero` | 整个预测区间缺口都为 0，系统不会刷量 |
| `multiplier_zero` | 目标系数在某个日期归零（通常 `growth_rate` 过负）；已配置 `target_min` 托底时不提示 |
| `over_capacity` | 某些天缺口超过 `boost_n × single_cap` 的单日容量 |
| `throughput_capacity` | 某些天缺口超过按 `threads` / 等待间隔估算的单日吞吐上限（未计请求耗时） |
| `stale_history` | 自然口径下本地库最新日期早于昨天，缺口可能基于旧数据 |
| `plan_signature_mismatch` | 数据库里的首日定档是在不同波动率/基线配置下计算的，已丢弃并重新抽样（`run` 当天也会重新抽） |
| `volatility_not_modeled` | 使用了 `--no-volatility` 且配置了波动率：表格为未抖动基准（`target_low` / `target_high` 仍给出目标区间） |
| `target_bound_hit` | 有日期的刷量缺口被 `target_min` / `target_max` 改变（上限=自然+刷量）；消息列出命中方向与改动前后值（最多 5 天，其余省略） |

### 8.5.1 forecast 折线图（控制台 + HTML）

```bash
# 不带 PATH：直接在控制台输出点状字符折线图
python curator_views/cli.py forecast --config config.yaml --chart

# 带 PATH：写自包含 HTML，浏览器打开
python curator_views/cli.py forecast --config config.yaml --chart reports/week.html

# 显式指定控制台（等价于不带 PATH）
python curator_views/cli.py forecast --config config.yaml --chart -

# 表格与图每 7 天输出一次
python curator_views/cli.py forecast --config config.yaml --chart --day-step 7

# 只输出图，不打印 forecast 表格
python curator_views/cli.py forecast --config config.yaml --chart-only
```

控制台图用点状字符绘制，适合 SSH、日志和快速巡检：

```text
浏览量：目标 vs 假设自然流量
1,500 ┤
1,125 ┤ *·················*·················*
  750 ┤
  375 ┤
    0 ┤
      └ ────────────────────────────────────
        11-14           11-15           11-16
        ● 目标   ○ 假设自然流量   * 重叠
```

- 图 1「浏览量」：目标 vs 假设自然流量；
- 图 2「刷量」：当日配额 vs 实际待刷；
- 上方标注区间、数据来源、模式、基线、波动率与目标上下界；
- 控制台宽度按终端自动适配（48~120 列），超过宽度会压缩绘图区；
- `--day-step N` 控制表格与图的输出间隔，首尾日期保留；`--chart-only`
  只打印图本身；
- 两条线落在同一字符格时显示 `*`，图例里标注「重叠」。

带 PATH 时生成**自包含 HTML**（内联 SVG，无外部 JS/CSS/图片），直接双击用
浏览器打开；页面额外显示目标抖动区间带。

默认纳入随机波动时，图中曲线是**本次抽样**的结果，重新运行会变化；加
`--no-volatility` 可输出未抖动基准图。图表只读取 forecast 结果，不写数据库、
不触网；`--json` 与 `--chart` 可同时使用（控制台图写 stderr，不污染 JSON）。

### 8.6 boost

```bash
python curator_views/cli.py boost --config config.yaml --target N [--apps M] [--dry-run]
```

手动执行一轮受控提升，不经过预测/检测流程。

- `--target N`：本轮目标提升次数（必填）；超过 `target_max` 时自动按上限执行
  （stderr 给出 warning，JSON 里 `target` 为夹取后的值、`requested_target` 为原始值、
  `clamped` 标记是否夹取）；
- `--apps M`：使用的最近推荐数量，默认取配置 `boost_n`；
- `--dry-run`：仅记录提升计划，不发送任何请求。

使用 `boost_cookie_file` 对应的 Cookie；留空则游客模式。手动 `boost` 没有昨日
自然流量上下文，无法校验 `target_min`（不会强行托底），但 `target_max` 是绝对
硬上限，始终生效。

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

### 9.2 固定基线 + 目标波动（让总量自然起伏）

```yaml
baseline_views: 10000
boost_mode: stable
target_volatility: 0.50      # 目标在 5000 ~ 15000 之间随机
target_volatility_min: 0.10  # 每天至少偏离 10%，避免过于平缓
volatility: 0.10             # 当日配额再浮动 ±10%
volatility_min: 0.05         # 至少偏离 5%
```

系统每天先抽一个目标（5000 ~ 15000），再按「目标 − 昨日自然流量」算缺口并
浮动 ±10%，最后补到当日配额；同一天重跑/多时段复用同一组抖动值。
缺口很小时，只靠 `target_volatility` 就能让曲线明显起伏。

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
场景。列表用完**默认自动回退**到基线/预测继续刷量（文件缺失/读取失败仍不回退）；
设 `boost_list_fallback: false` 则用完即停。

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

# 控制台点状折线图（带 PATH 则写 HTML）
python curator_views/cli.py forecast --config config.yaml --chart

# 未来两周的空转/超载检查
python curator_views/cli.py forecast --config config.yaml --days 14
python curator_views/cli.py forecast --config config.yaml \
  --set growth_factor=1.2 --set baseline_views=1500
```

默认口径已对齐 `run` 的缺口公式；用 `--tomorrow` 看明天起，用 `--set` 做 what-if，
确认缺口和容量后再改正式配置。

### 9.8 给增长目标封顶 / 托底

```yaml
boost_mode: growth
growth_factor: 1.2
growth_rate: 0.01
target_max: 5000   # 目标不超过 5000，防止增长不收敛
target_min: 1000   # 目标不低于 1000，跌破时按下限补量
```

两个键都是 `0` 表示该方向不设界。`target_max` 是当日总量的绝对上限（自然流量 +
刷量），`target_min` 在确实要刷量时托底；`run` / `daemon` / `forecast` 使用同一
口径，抖动、刷量列表和手动 `boost` 都不得突破上限，列表/恢复期等不刷状态不会
被强行托底。详见 [3.2](#32-核心公式) 与 [7.6](#76-基线与波动参数)。

---

## 10. 定时调度

### 10.1 daemon（推荐）

```bash
python curator_views/cli.py daemon --config config.yaml
```

配置热重载、时段级去重、失败重试、优雅退出。适合长期常驻。

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

**重启与停止**

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

- **改配置不用重启**：daemon 每轮热加载配置（含 `daily_times` / `daily_time`）；
- **改代码必须重启**：进程里仍是旧模块，重启后才加载新代码；
- **重启不会重复刷**：`last_run_slot` 做时段级去重。当天已完成到当前时刻的时段 →
  重启后等下一个时段；尚有未完成时段且已过其时刻 → 启动时立即补跑；
- **多个时段串行执行**：上一时段提升未结束时下一时段会等待，单轮刷量耗时较长时
  （如 1,500 次请求、8~16 秒间隔，一轮约 5 小时）不要把时段排得太密；
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

默认 `views.db`，WAL 模式，包含四张表：

| 表 | 内容 |
| --- | --- |
| `daily_stats` | 每日浏览量（`date_ts` 主键 → `num_views`） |
| `boost_log` | 刷量台账（日期、数量、备注、创建时间） |
| `kv_store` | 键值状态 |
| `daily_plan` | 每日决策定档：`date` 主键 + `target` / `raw_target` / `target_jitter` / `gap_jitter` / `deduction` / `gross_need` / `boost_quota` / `need` / `today_boosted` / `target_bound` / `in_cooldown` / `list_status` / `list_amount` / `signature` / `updated_at`；供 `forecast` 首日复用 |

`daily_plan` 每跑一次 `run` 覆盖当天一行（同一日多时段只更新 `need` /
`today_boosted` / `updated_at`），`signature` 记录当时的波动率与基线配置；
`forecast` 签名一致时才复用该定档值。写入的 `boost_quota` / `need` 已经是按
`target_min` / `target_max` 夹取后的最终值（无新增列，旧库无需迁移）。
`--dry-run` 不写此表。

`run` 的 JSON 汇总新增 `need_bound`（`min` / `max` / 空）、
`target_floor_boost` / `target_ceiling_boost`（边界对应的可刷量）；
`forecast` 的 `summary` 新增 `bound_hits` 及同名字段，`rows[].bound_hit` 标记
该行刷量命中的边界。

`kv_store` 常见键：

- `program_start`：程序启动日，过滤更早历史；
- `last_stats_date`：最近一次抓取的统计日期与天数；
- `last_run_date`：最近一次完成每日流程的日期（日期级标记；daemon 时段去重另见
  状态文件的 `last_run_slot`）；
- `growth_rate` / `growth_start`：growth 速率锚点。

### 11.2 日志

- 文件日志：数据库所在目录的 `curator_views.log`；
- `daemon` 前台运行时同时输出控制台；
- 日志包含每日简报、提升批次、异常检测、执行器错误等。
- 静默期排队的通知写入数据库同级的 `notify_queue.json`，离开静默时段后自动补发；
  队列文件无需手工维护，删除它只会丢弃尚未补发的积压通知。

### 11.3 守护进程状态

`daemon_state.json` 写在数据库同级目录，包含：

- `pid`、`started_at`、`phase`、`updated_at`；
- `last_run_date`、`last_run_slot`（最近完成的时段 `YYYY-MM-DD#HH:MM`）、
  `last_run_at`、`last_summary`、`last_error`；
- `daily_times`、`attempts_today`（当前时段已失败次数）；
- `next_wake`。

`info` 读取该文件；数据库里的 `last_run_date` 也会被 daemon 每轮核对，手动 `run`
只写日期级 `last_run_date`，daemon 视为“此刻之前的时段都已完成”，之后的时段仍会
执行以补足缺口。

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
“统计页被重定向到公开页面（Cookie 已失效或未登录）”，或返回 HTTP 401/403。

`daemon` 的处理方式：

- 把这类错误与普通网络/解析错误区分开（`StatsAuthError`），**不把当天标记为已完成**；
- 推送一条 `error` 级通知（未配置 ntfy 时写入日志），正文直接给出重新登录命令；
- 按 `retry_delay_minutes` 在当天重试。你重新 `login` 覆盖 Cookie 文件后，
  下一次重试会自动读取新文件，无需重启 daemon。

解决：

```bash
python curator_views/cli.py login --config config.yaml --validate-only
python curator_views/cli.py login --config config.yaml
```

`run`、`check`、`forecast --fetch` 遇到同样的错误会直接提示登录命令并以 1 退出。

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
确认 `daily_times` / `daily_time` 是否正确、当前时段是否已经执行过（时段级去重）。需要重启或停止时见
[10.1 daemon 的「重启与停止」](#101-daemon推荐)。

### 12.7 PID 复用的误判

`info` 优先按 PID 判断存活。如果进程退出后 PID 被其他进程复用，可能误判为
运行中。此时以日志是否持续更新、`updated_at` 心跳是否新鲜作为辅助判断。

### 12.8 数据库被锁 / 并发读写

Storage 使用 WAL 与线程锁，单进程内并发安全。不要让多个 `run` / `daemon` / `check`
同时写同一个数据库（`check` 默认会写 `daily_stats`，需要并发巡检时加
`--no-write`）；`forecast` 使用只读 URI，不会加写锁。

### 12.9 手动修正某天的刷量记录

`boost` 手动刷量会自动写台账。若要覆盖历史某天，可直接操作 SQLite：

```sql
DELETE FROM boost_log WHERE date = '2026-11-01';
INSERT INTO boost_log (date, count, note, created_at)
VALUES ('2026-11-01', 500, 'manual', '2026-11-01T12:00:00');
```

修改前建议备份数据库。

### 12.10 同一天重复 run 会不会刷两遍

不会。缺口公式会先减去 `boost_log` 里今天的累计（`today_boosted`）：

```
当日配额 = max(0, round(未抖动缺口 × (1 + 缺口抖动)))   # 同日复用同一值
当日配额 = clamp(当日配额, 刷量下限, 刷量上限)             # 绝对边界
need     = max(0, 当日配额 - 今日已刷)
```

所以中断后重跑、容量不足再补、手动补跑，都只补剩余部分。`forecast` 的
「今日已刷」列显示的就是这个值。例外是刷量列表模式：一行代表当天总量，当天已经
刷过就跳过（列表状态 `already_boosted`），避免误消费下一行。

也不要并发启动两个 `run`：两者在开始前读到相同的 `today_boosted`，会各刷一份
剩余量，造成超额。

**配置多个时段时**：手动 `run` 只写日期级 `last_run_date`，daemon 视为“此刻之前的
所有时段都已完成”，后续时段仍会执行并自动只补剩余缺口。注意一个副作用：如果手动
`run` 之后删除 `daemon_state.json`（或全新启动 daemon），更早的时段会被当成已完成，
当天某个从未执行过的早期时段就不会再补跑；需要时手动 `run` 一次，或等第二天。

### 12.11 并发线程数最高能到多少

`threads` **没有内置上限**：配多少就用多少，代码不再做任何截断（`threads <= 1`
走顺序执行，不开线程池）。实际并发只受数据规模约束，被 `boost_n`、`batch_size`
与批内活跃推荐数再压一层：

```
实际并发 = min(threads, boost_n, batch_size, 批内活跃 app 数)
```

- 同一个游戏在同一批里只访问一次，所以并发数不会超过可用推荐条数（`boost_n: 1`
  时 `threads` 配多少实际都是 1）；
- `batch_size` 小于 `threads` 时，并发被批大小卡住；
- `forecast` / `run` 的吞吐估算与这里同口径，报出的“实际并发≤N”就是真实值；
- 代价：同一秒并发请求更多、更像机器行为；且并发模式下连续失败按“批内失败总数”
  累计，中止判定不如顺序模式精确。除非单日吞吐确实不够，否则保持 `threads: 1`。

### 12.12 forecast 的「目标」为什么逐日下跌

先看表格的「基准」列：如果它也在逐日下滑，通常是 `baseline_views: 0`（自动基线）
与 `growth_factor < 1` 同时使用造成的负反馈。以 `growth_factor: 0.85` 为例，
每天刷到的目标只有基准的 85%，这条数据进入预测窗口后把基准继续压低，
下一天目标更低；`growth_rate` 的 +1%/天 抵消不了 15% 的台阶差。
`forecast --days 14` 能看到目标一路跌到 `target_min` 后停住。

处理方式：

- 想“降一次后稳住”或“按固定速率缓降”：用 `baseline_views` 固定基准；
- 想真正上涨：把 `growth_factor` 设为 `1.0` 或更大，再用 `growth_rate: 0.01`；
- 先排除数据问题：若「基准」远高于「假设自然」且最新一天异常偏低，
  确认那天的统计是否完整（Steam 统计滞后），用 `check` 或
  `forecast --fetch` 重抓后再判断。

详见 [7.6 基线与波动参数](#76-基线与波动参数)。

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

列表模式会覆盖预测缺口；列表用完默认自动回退到基线/预测刷量（文件缺失/读取失败
仍不回退）。若只是临时排期、想让列表用完就停，设 `boost_list_fallback: false`；
想彻底退出列表模式则清空 `boost_list_file` 配置。

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

### 13.6 check 改为写库

`check` 现在会把抓取到的统计写入 `daily_stats`（默认行为），不再只是内存预览：

- 只写统计表，不写 `boost_log` / `program_start` / `last_run_date` /
  `last_stats_date` / growth 锚点，因此不影响 daemon 的时段去重；
- 想保留旧的纯只读行为，用 `check --no-write`；
- 写库失败（路径不可写、数据库损坏等）会报错并以 1 退出。

排查“`forecast` 显示的数据比 `check` 旧”时，现在跑一次 `check` 即可对齐，不必等
下一次 `run`。

### 13.7 daemon 失败当天重试，不再吃掉当天

旧版本在执行失败时也会把 `last_run_date` 记为今天，导致“10:00 失败一次，当天再也
不跑”，且重启 daemon 也无效。

现在：

- 只有执行成功才推进 `last_run_date`；
- 失败后按 `retry_delay_minutes`（默认 30 分钟）在当天重试，最多
  `retry_max_times`（默认 3 次）；
- 重试用尽也不写 `last_run_date`，可随时手动 `run` 补跑；
- 状态文件里“今天已执行 + 有错误 + 今天执行过”的旧记录会在启动时自动纠正为
  未执行（若数据库里确实有今天的成功记录，外部同步会重新采用，不会重复执行）；
- 统计 Cookie 失效单独归为 `StatsAuthError`，推送可操作告警；重新 `login` 后，
  下一次重试自动生效，无需重启 daemon。

新增配置键都有默认值，旧配置无需修改。

### 13.8 当日已刷计入缺口，支持同日续刷

`run` 的今日缺口现在会减去 `boost_log` 里今天的累计：

- 中断、容量/吞吐截断、手动重跑都只补剩余部分，不会重复刷整份缺口；
- `forecast` 新增「今日已刷」列与 `source.today_boosted`，首日缺口已按剩余量计算；
- 刷量列表模式当天已刷过时状态为 `already_boosted`，不续刷也不消费下一行；
- 判断依据是 `boost_log`，若你手动刷量没有写台账，续刷会把这部分当成未刷。

### 13.9 daemon 支持一天多个时段

新增 `daily_times`，可配置一天多个执行时刻：

```yaml
daily_times: ["10:00", "20:00"]
daily_time: "10:00"    # 仅当 daily_times 为空时生效；旧配置无需修改
```

- `daily_times` 非空时优先于 `daily_time`；接受列表，也接受逗号或空格分隔的
  字符串；非法条目跳过并记录警告。`daily_times` 为空时回退到 `daily_time`，
  完全解析不出有效时刻时回退到 `10:00`；
- 每个时段执行一次完整流程（抓取 → 预测 → 检测 → 提升 → 通知）。后一时段按
  缺口公式自动扣掉今日已刷，只补剩余部分，所以通常比首轮短；
- 时段级去重：`daemon_state.json` 新增 `last_run_slot`（`YYYY-MM-DD#HH:MM`）与
  `daily_times`，`attempts_today` 改为按当前时段计数；重启后当天只补跑尚未完成的
  时段；
- 重试参数按“每个时段”生效：某时段重试用尽只放弃该时段，当天后续时段照常执行；
  `retry_delay_minutes: 0` 表示该时段内不重试；
- 手动 `run` 只写日期级 `last_run_date`，daemon 视为此刻之前的时段已完成，后续
  时段仍会执行补足缺口；
- 旧配置不含 `daily_times` 时行为与之前完全一致（每天一次）。升级时若旧
  `daemon_state.json` 的 `last_run_date` 是今天但没有 `last_run_slot`，daemon
  视为今天已全部完成，避免启动后立刻重复执行。

### 13.10 通知级别过滤与静默时段

新增四个可选的推送控制键，均有默认值，旧配置无需修改，行为与升级前一致：

- 只收异常：`ntfy_min_level: "warning"` 让 `info` 级通知只写日志，再配合
  `ntfy_daily_summary: false` 抑制例行成功简报；warning/error 级简报与告警照常推送；
- 夜间免打扰：`ntfy_quiet_hours: "23:00-08:00"`（可写多个区间，如
  `"12:00-13:00,23:00-08:00"`）。静默期命中推送条件的通知排入数据库同级的
  `notify_queue.json`，离开静默后自动补发，队列最多 50 条，超出丢弃最旧；
- 彻底关闭推送：`ntfy_enabled: false`，或清空 `ntfy_url` / `ntfy_topic`。此时所有
  通知只写日志，不排队也不补发。

`notify_queue.json` 位于数据库（`db_path`）同级目录，已被 `.gitignore` 忽略，
无需手工维护；删除它只会丢弃尚未补发的积压通知。

### 13.11 每日目标定档

`run` 现在会把当天最终决策（目标、抖动、缺口、配额、恢复期/列表状态）写入
`daily_plan` 表；`forecast` 首日在配置签名一致时直接复用该定档值，不再重新抽样，
因此不会再出现「run 已经按某个目标开刷、forecast 却显示另一个目标和缺口」的
矛盾。未来日仍按原逻辑独立抽样预测，`--no-volatility` 仍输出未抖动基准（不读取
定档）。数据库升级完全自动：打开旧 `views.db` 时会自动补建 `daily_plan` 表，原有
`daily_stats` / `boost_log` / `kv_store` 数据不受影响；`--dry-run` 不写入定档。
如果当天没有跑过 `run`（或改了波动率/基线配置导致签名变化），`forecast` 会显示
无定档记录或 `plan_signature_mismatch` 提示，并退回抽样预测。

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

当前测试共 **402** 个，覆盖：

- 预测边界、趋势封顶、启动期系数；
- 溢出检测连续段、长期基线、持续斜率；
- 手动基线、growth 模式、growth_rate 锚点、目标上下界（封顶 / 托底 /
  不设界 / 波动不越界 / 长期基线同步）、波动率、恢复期、标记日剔除；
- 刷量绝对硬边界：抖动/列表/manual boost 不得突破 `target_max − 自然`，下限
  托底但不复活恢复期、列表 0、`boost_enabled: false`；dry-run 与正式执行同值；
  forecast 逐日 `bound_hit` / `target_bound_hit` 与旧定档夹取；
- 目标波动率、最小波动幅度、同日抖动复用（重跑/多时段/配置变更）、
  缺口配额跨时段续刷、抖动日不被误判为溢出；
- 调度器子进程执行器、时区工具；
- 每日刷量列表的解析、peek、消费与异常保留；
- 双 Cookie 解析、未登录回退、Cookie 规范化与校验；
- `forecast` 的逐日估算、默认含今天/自然口径、`--tomorrow`、警告、只读数据库行为、
  表格对齐与容量备注、按 `threads` / 等待间隔估算的吞吐上限；默认纳入波动率
  随机抽样、`--no-volatility` 关闭、目标区间与抖动字段；
- forecast 折线图：控制台点状字符渲染、宽度适配、日期标签间隔、只输出图、
  空数据、HTML/SVG 渲染、目标区间带、`--chart` 控制台/写文件、`--day-step`
  首尾保留、`--json` 兼容与写失败报错；
- `check` 写库（幂等覆盖、不写 run 状态）、`--no-write` 只读、库不可写时报错；
- 当日已刷：只扣首日缺口、`forecast` 表格与 JSON、同日续刷、列表模式不误消费；
- `StatsAuthError` 的判定（重定向 / 401 / 403）与 `run` / `check` / `forecast --fetch`
  的重新登录提示、Cookie 失效告警；
- daemon 的 plan_next、同日去重、失败当天重试与重试用尽、旧状态纠正、状态与 info 输出；
- daemon 多时段调度（时段解析、时段级去重、跨重启、手动 run 标记、时段级重试）；
- 通知的级别过滤、每日简报开关、静默时段区间解析（含跨午夜）与队列补发（FIFO、
  失败保留、上限裁剪）、daemon 补发回调与 dry-run 不补发；
- 配置默认值：缺键回退模板默认值、用户值覆盖、`null` 视为未写、显式空串保留、
  模板与默认值字典同源一致性；
- 并发数：`threads` 原样传入线程池（无内置上限），并受 `boost_n` / `batch_size`
  限制。

测试目录：

```
tests/
├── __init__.py
├── conftest.py
├── test_anti_cheat.py
├── test_boost_list.py
├── test_booster.py
├── test_chart.py
├── test_cli_check.py
├── test_cli_cookie.py
├── test_cli_info.py
├── test_cli_logging.py
├── test_cli_notify_config.py
├── test_convergence.py
├── test_cookie_optional.py
├── test_daemon.py
├── test_daemon_notify.py
├── test_daily_plan.py
├── test_fetcher.py
├── test_forecast.py
├── test_login.py
├── test_notifier.py
├── test_predictor.py
├── test_run_daily.py
├── test_scheduler.py
├── test_status.py
└── test_target_bounds_hard.py
```

提交规范：中英双语 `type(scope): English / 中文`，一个提交只做一件事。

---

## 16. 免责声明

本工具仅用于提升**自己运营**的 Steam 鉴赏家评测的浏览量。请勿用于对其他鉴赏家组
或平台进行恶意操作。使用本工具产生的任何后果（包括但不限于 Steam 账号异常、
鉴赏家资格被撤销等）由使用者自行承担。请在遵守 Steam 订阅者协议与当地法律法规的
前提下使用。Cookie 属于敏感信息，请勿将 Cookie 文件提交到版本控制或分享给他人。
