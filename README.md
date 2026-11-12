# CuratorViewsGuard — Steam 鉴赏家浏览量预测与受控提升系统

基于 Steam 鉴赏家组的历史每日浏览量，自动预测每天应有的浏览量水平，并通过受控、
限速的方式平滑补充缺口。系统内置**反作弊检测**：识别其他小组恶意刷量造成的
浏览量虚高，自动分摊消化溢出量，让浏览曲线保持平稳，避免被 Steam 误判为
机器人行为。

## 为什么需要这个系统？

- **防止 Steam 误判作弊**：如果你的组浏览量忽高忽低（尖峰/断崖），Steam 风控
  可能把账号判定为刷量机器人。本系统让曲线始终在正常波动范围内。
- **抵御他人恶意刷量**：其他组可能访问你的推荐页面把你的浏览量刷虚高——这会
  污染你的数据、也可能害你被误判。系统能识别异常溢出并主动"少刷"，把曲线拉回。
- **刷量有的放矢**：不再盲目全量刷，而是根据历史数据算出"今天该补多少"，
  只刷最近发布的几条新推荐，数量可控、节奏自然。

## 项目结构

```
CuratorViewsGuard/
├── steam_curator_views.py   # 旧版全量刷量脚本（仅作行为参考，新系统不依赖它）
├── cookie.txt               # Steam 登录 Cookie（敏感，勿提交到版本控制）
├── config.yaml              # 本地配置（init-config 生成，含 clan_id / Cookie 路径）
├── requirements.txt         # Python 依赖清单
└── curator_views/           # 本系统主体
    ├── cli.py               # 命令行入口：run / daemon / check / boost / login / export
    ├── README.md            # 子系统说明
    └── core/
        ├── fetcher.py       # 抓取 g_StatsAppViews 与推荐列表
        ├── login.py         # Playwright 半自动登录，导出/校验 cookie.txt
        ├── predictor.py     # 中位数 + 封顶趋势预测
        ├── anti_cheat.py    # 溢出检测 + 分摊抵扣
        ├── booster.py       # 受控刷量执行器
        ├── scheduler.py     # 每日闭环编排 + 子进程执行器
        ├── notifier.py      # ntfy 推送
        └── storage.py       # SQLite 存储
```

## 功能特性

- **浏览量预测**：自适应中位数窗口 + 封顶线性趋势回归；启动阶段（数据不足一个
  窗口）自动启用保守系数，抑制早期误判。
- **反作弊检测**：三重检测叠加——相对预期超额（>30% 连续 2 天）、长期基线超额
  （更长窗口独立参照，防"平台抬升"型刷量污染短期预期后漏检）、持续上升斜率
  （防每天温和爬升的慢性注入）。溢出量自动分摊到未来 3 天抵扣；被标记日不参与
  预测，防止虚高数据污染基线；标记后进入恢复期（默认 7 天）强制不刷，
  等待曲线自然回落。
- **受控提升**：分批执行、随机间隔限速、单 App 上限、达到目标 95% 提前停止、
  连续失败自动中止，尽量模拟自然访问流量。
- **稳定 / 刷量模式切换**：`boost_mode: stable` 维持稳定（补缺口到预测/基线水平），
  `boost_mode: growth` 配合 `growth_factor` 倍率主动刷高目标；检测参照与目标同步，
  不会把自己刷的量误判为恶意刷量。
- **可配置基线 + 波动率**：可手动指定目标浏览量（`baseline_views`），并设置
  波动率（`volatility`）使每日提升量在目标附近随机波动，浏览曲线围绕基线
  自然上下起伏，避免收敛成一条笔直的不自然曲线。
- **调度器 / 执行器分离**：默认同进程直调，也可配置 `executor_cmd` 以独立子进程
  方式唤起执行器刷量——调度器崩溃不影响执行器，执行器可随时手动单独触发。
- **每日完整闭环**：抓取统计 → 入库 → 预测 → 溢出检测 → 计算缺口 → 提升 → 通知。
- **通知推送**：可选 ntfy 推送（异常/每日简报），未配置时退化为日志记录。
- **只读巡检与导出**：`check` 命令随时查看最近 7 天与预测摘要；`export` 导出
  全部历史数据为 JSON / CSV。
- **跨平台**：Windows（任务计划程序）与 Linux（systemd）均可部署。

## 工作原理

```
                      ┌────────────────────────────────────────────┐
  每天 10:00 UTC+8    │              run（调度器进程）              │
                      └────────────────────────────────────────────┘
                                      │
        ┌─────────────┬───────────────┼───────────────┬─────────────┐
        ▼             ▼               ▼               ▼             ▼
   抓取统计      程序启动日过滤     逐日预测期望    溢出检测       计算今日缺口
   g_StatsAppViews  (启动前数据     (中位数+趋势)   (连续>30%)   = 目标 − 昨日自然流量
   入库 SQLite      不参与拟合)                                       − 溢出抵扣
                                        │
                                        ▼  need > 0 且开启提升
                          ┌───────────────────────────┐
                          ▼                           ▼
               executor_cmd 配置了？         否 → 同进程调用 Booster
               ├── 是 → 子进程执行器
               │       (cli.py boost --target {need})
               ▼
           实时进度写入台账，达到目标提前停，异常中断报警
                                      │
                                      ▼
                          ntfy 推送每日简报（warning 升级提醒）
```

**核心公式（每天）：**

```
基准预期        = 中位数(最近 14 天) + 趋势修正(≤ 中位数 × 15%)
               （配置 baseline_views > 0 时直接用该固定值）
今日目标        = 基准预期 × 倍率   （stable 倍率=1；growth 倍率=growth_factor）
今日需刷量      = 今日目标 − 昨日自然流量 − 溢出抵扣    （≤ 0 则当天不刷）
昨日自然流量    = 昨日实际 − 昨日刷量（boost_log 台账，缺失时按 0 处理）
               恢复期内强制为 0；波动率 > 0 时在 ±波动率 内随机
每条新推荐刷量  = min(单条上限 300, 需刷量 ÷ 最近推荐数 N)
```

**防刷关键设计：**
- 缺口按**昨日自然流量**（昨日实际 − 昨日刷量）计算，不会把昨天的补量当成
  今天已有的基础重复扣减；否则目标永远补不满，还会在目标附近两天一摆；
- 昨天实际被恶意刷得远超预期 → 今天需刷量为负 → 自动少刷/不刷，曲线回落平稳；
- 三重检测叠加：相对预期超额（尖峰）、长期基线超额（平台抬升，防短期预期被
  污染后漏检）、持续上升斜率（慢性爬升）；连续 2 天候选才标记，单日波动不误伤；
- 溢出量分摊到未来 3 天消化，而不是明天一次性扣完（避免新的断崖）；
- 被标记的恶意刷量日**不参与预测**（剔除出中位数窗口），虚高数据无法把基线
  抬高；标记后 7 天恢复期内强制不刷，让曲线在无注入下自然回落到真实水平；
- 检测参照与补缺口目标统一（手动基线 / growth 模式下为固定目标），
  系统自己刷出来的量不会被自己的反作弊标记。

## 安装

要求 **Python 3.10+**（Windows / Linux 均可）。

```bash
# 进入项目根目录
pip install -r requirements.txt
```

`login` 命令额外需要 playwright（**可选，不属于默认安装**）。未安装时执行 `login`
会提示安装。安装命令：

```bash
pip install playwright
playwright install chromium
```

## 快速开始

```bash
# 1. 生成默认配置文件
python curator_views/cli.py init-config

# 2. 编辑 config.yaml，填写必填项：
#    clan_id      —— Steam 鉴赏家组 ID（纯数字，如 45519015）
#    cookie_file  —— 存放 Steam 登录 Cookie 的文本文件
#    proxy        —— 代理地址（如 http://127.0.0.1:10279），可选
#    ntfy_url / ntfy_topic —— ntfy 推送（可选）

# 3. 半自动登录 Steam，生成 cookie.txt（需要 playwright；会打开浏览器窗口，
#    在页面里完成密码/验证码/手机确认，脚本自动导出并校验 Cookie）
python curator_views/cli.py login --config config.yaml

# 4. 只读巡检：抓取统计并预览预测（不会产生任何提升请求）
python curator_views/cli.py check --config config.yaml

# 5. 执行每日完整流程（抓取-预测-检测-提升-通知）
python curator_views/cli.py run --config config.yaml

# 演练模式：只计算，不真正刷量
python curator_views/cli.py run --config config.yaml --dry-run

# 临时关闭提升（优先于配置中的 boost_enabled）
python curator_views/cli.py run --config config.yaml --no-boost
```

> **首次运行注意**：程序启动日之前的 364 天历史**不参与预测**（你的历史数据里混着
> 以往不规则刷量和他人恶意刷量，不可靠）。系统从启动日起积累自己的平稳数据，
> 头 14 天处于"启动期"（预测自动乘 0.9 保守系数），数据足够后逐步进入正常预测。

## 命令参考

| 命令 | 作用 |
| --- | --- |
| `run` | 每日完整流程（抓取 → 预测 → 检测 → 提升 → 通知） |
| `daemon` | 守护进程模式：常驻运行，每天到点执行每日流程（配置热重载） |
| `info` | 查看守护进程是否在运行、状态文件与本地数据概况（离线只读） |
| `check` | 只读检查：抓取统计、打印最近 7 天与预测摘要 |
| `boost` | 手动执行一轮受控提升 |
| `login` | 半自动登录 Steam，生成/校验 cookie.txt（可选 playwright） |
| `export` | 从本地数据库导出每日统计数据（json / csv） |
| `init-config` | 生成默认配置文件模板 |
| `version` | 打印版本号 |

### run

```bash
python curator_views/cli.py run --config config.yaml [--dry-run] [--boost | --no-boost]
```

- `--dry-run`：演练模式，计算一切但实际不发送提升请求；
- `--boost` / `--no-boost`：强制开启/关闭提升（优先于配置）。
- 成功后以 JSON 打印汇总（今日预期、昨日实际、缺口、溢出天数、实际提升数等），
  并在数据库同级目录写入 `curator_views.log`。

### daemon（守护进程模式）

替代 cron / 任务计划程序的常驻方案：进程启动后保持运行，每天到达配置的
`daily_time`（默认 `10:00`，Asia/Shanghai）自动执行一次每日完整流程。
**每次循环都会重新加载配置文件**——修改任意配置（含执行时间）无需重启进程。

```bash
python curator_views/cli.py daemon --config config.yaml [--dry-run] [--boost | --no-boost]
```

行为要点：
- 启动时若今天尚未执行且已过执行时刻，立即补跑一次（重启/崩溃不影响当天任务）；
- 单次执行失败不退出进程：记录日志后等待下一天（避免失败风暴）；
- **同一天不重复执行**：每日流程结束会把 `last_run_date` 写入数据库，daemon
  每轮到点前都会核对；手动 `run` 与 daemon 谁先执行，当天都只跑一次；
- **Ctrl+C 立即中断当前执行**并退出（状态文件写 `stopped`）；SIGTERM（systemd
  stop）则等在途任务收尾后优雅退出；
- **前台运行时 INFO 日志同时输出到控制台**：启动即打印日志文件路径与下次
  执行时间，执行过程与结果实时可见；文件日志仍写入数据库同级的
  `curator_views.log`；
- 建议配合 systemd（`Restart=always`）或 Windows 任务计划程序/服务包装器
  保持进程常驻。

> 想要「释放终端」只能让进程转入后台（`nohup` / `setsid` / `tmux` / systemd），
> 静默本身并不释放控制台——进程仍在前台占用终端，只是不打印任何东西。

```bash
# 后台常驻（日志已由 CLI 写入 curator_views.log，这里只兜住启动期异常）
nohup python curator_views/cli.py daemon --config config.yaml >/dev/null 2>&1 &
```

### 如何确认 daemon 在运行

`info` 是本地离线只读命令：只读取状态文件与本地数据库，不要求 `clan_id` /
Cookie，不访问网络，也不会在数据库不存在时创建空库。

```bash
python curator_views/cli.py info --config config.yaml
# 需要脚本化处理时输出原始 JSON
python curator_views/cli.py info --config config.yaml --json
```

输出示例：

```
守护进程: 运行中
  PID: 12345
  启动时间: 2026-10-27 18:03:12
  当前阶段: waiting（等待下次执行）
  上次心跳: 2026-10-27 18:20:31（12 秒前）
  上次执行: 2026-10-26 10:00:03
  下次执行: 2026-10-28 10:00
  状态文件: E:\...\daemon_state.json
配置: 每日执行 10:00；提升已关闭（采集模式）；boost_mode=stable
数据: 12 天（2026-10-15 ~ 2026-10-26）；最新 2026-10-26 1234 次；程序启动日 2026-10-15
```

daemon 会把运行状态原子写入 **`db_path` 所在目录下的 `daemon_state.json`**
（默认即项目根目录）。运行判断优先检查 PID：进程存在即视为运行中；PID 检查
不可用时回退到心跳时间（90 秒内视为正常）。状态文件中还保留 `last_run_date`，
因此 daemon 重启后同一天不会重复执行每日流程；数据库里也有一份 `last_run_date`
（由 `run_daily` 每次结束写入），daemon 每轮会核对，手动 `run` 过当天同样不会
重复执行。

> 说明：长时间执行提升任务时心跳可能暂停，此时存活判断仍以 PID 为准。
> PID 复用是已知限制（进程退出后 PID 被其他进程复用时会误判为运行中）。

在 `info` 出现之前，常用替代确认方式：

- Windows：`tasklist | findstr python`（查看 python 进程是否存在）；
- Linux / macOS：`ps aux | grep curator_views`；
- 查看数据库同目录的 `curator_views.log` 是否持续有当天日志（daemon 以
  `console=True` 运行时也会在终端打印）。

### check

只读检查，不产生任何写入，可放心用于日常巡检：

```bash
python curator_views/cli.py check --config config.yaml
```

### boost（手动执行器）

```bash
python curator_views/cli.py boost --config config.yaml --target N [--apps M] [--dry-run]
```

- `--target N`：本轮目标提升次数（必填）；
- `--apps M`：使用的最近推荐数量（默认取配置 `boost_n`）；
- `--dry-run`：仅记录提升计划，不发送任何请求。

### login

Steam 没有可靠的纯自动登录：登录页带图形验证码，新设备 / 异地登录往往还要
Steam Guard 手机 App 确认，全自动填表很容易失败或被风控。因此 `login` 采用
**Playwright 半自动登录**：脚本打开一个真实的 Chromium 窗口，由你在页面里
完成登录（密码、验证码、手机确认），程序只负责轮询等待登录标志 Cookie
`steamLoginSecure` 出现，导出 `steampowered.com` 域下的 Cookie，再用鉴赏家
后台页面校验有效性，校验通过才原子写入 `cookie.txt`。

用途：生成 / 更新 `cookie.txt`，替代手工从浏览器复制 Cookie；也可用来确认
现有 Cookie 是否还有效。

安装（可选依赖，**不属于默认安装**）：

```bash
pip install playwright
playwright install chromium
```

未安装时执行会报：`执行失败: 未安装 playwright。请运行：pip install playwright && playwright install chromium`。

```bash
python curator_views/cli.py login --config config.yaml
```

参数：

- `--config PATH`：配置文件路径（默认 `config.yaml`）；
- `--validate-only`：只校验已有 `cookie.txt`，不打开浏览器（只读，可用于定时巡检 / 排查 Cookie 是否过期）；
- `--headless`：无界面模式，通常无法完成验证码 / 手机确认，默认关闭；
- `--no-validate`：登录后跳过有效性校验直接写入（不推荐）；
- `--timeout SECONDS`：等待登录完成的超时秒数（默认 300）；
- `--profile-dir PATH`：自定义浏览器持久化目录，默认配置目录下的 `.steam_profile`。

持久化目录默认是 `config.yaml` 同目录下的 `.steam_profile/`（已加入 `.gitignore`），
登录态保存在里面，**第二次运行通常已是登录状态，无需再输密码**，脚本会直接刷新
Cookie。

校验失败时**不会覆盖**原有 `cookie.txt`，避免坏 Cookie 顶掉可用的 Cookie。

成功时以 JSON 打印汇总，**不包含 Cookie 内容本身**：

```json
{"mode": "login", "cookie_file": "cookie.txt", "clan_id": "45519015",
 "valid": true, "reason": "Cookie 有效", "cookie_count": 12}
```

- `mode`：`login`（登录）或 `validate`（仅校验）；
- `valid`：校验结果 `true` / `false` / `null`（`--no-validate` 或配置缺少 `clan_id` 时为 `null`，表示跳过校验）；
- `reason`：校验结果说明，成功为 `Cookie 有效`，失败为具体原因；
- `cookie_count`：导出的 Cookie 条数（仅登录模式输出）。

退出码：成功 `0`；校验失败或文件缺失 `1`。

```bash
# 只校验现有 cookie.txt 是否仍有效（只读，不打开浏览器）
python curator_views/cli.py login --config config.yaml --validate-only
```

> Cookie 属于敏感信息。脚本不会把它打印到日志，也不触碰你的密码；请勿将
> `cookie.txt` 提交到版本控制或分享给他人。

### export

```bash
python curator_views/cli.py export --config config.yaml --format json|csv [--out FILE]
```

### 调度器 / 执行器分离

在 `config.yaml` 中配置 `executor_cmd` 后，`run`（调度器）会把计算出的任务下发给
**独立执行器子进程**执行：

```yaml
executor_cmd: "python cli.py boost --config {config} --target {need} --apps {apps}"
```

占位符：`{config}` 配置文件路径、`{need}` 目标刷量、`{apps}` 推荐数量。
执行器完成后退回 JSON（`boosted` 字段）由调度器解析计入简报。留空则同进程直调。
两种模式可随时切换，执行器也可手动单独触发。

## 配置文件说明

`init-config` 生成的默认模板，相对路径均基于配置文件所在目录解析。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `clan_id` | （空） | Steam 鉴赏家组 ID，纯数字，**必填** |
| `cookie_file` | `cookie.txt` | Steam 登录 Cookie 文件路径 |
| `proxy` | （空） | 代理地址，如 `http://127.0.0.1:10279` |
| `db_path` | `views.db` | SQLite 数据库文件路径 |
| `ntfy_url` | （空） | ntfy 服务地址，如 `https://ntfy.sh` |
| `ntfy_topic` | （空） | ntfy 主题名；与 `ntfy_url` 同时配置才推送 |
| `boost_enabled` | `true` | 是否启用每日受控提升 |
| `daily_time` | `"10:00"` | daemon 模式的每日执行时刻（Asia/Shanghai，HH:MM） |
| `executor_cmd` | （空） | 执行器子进程命令模板；留空则同进程直调 |
| `window_days` | `14` | 中位数计算窗口（天数） |
| `trend_cap` | `0.15` | 趋势斜率相对中位数的封顶比例 |
| `startup_factor` | `0.9` | 启动阶段保守系数 |
| `trend_min_points` | `5` | 计算趋势所需的最少数据点数 |
| `outlier_k` | `0.30` | 溢出判定阈值：实际超过预期 ×(1+k) 视为候选异常 |
| `consecutive_days` | `2` | 连续候选达到该天数才标记为恶意刷量 |
| `absorb_days` | `3` | 溢出量分摊到未来多少天 |
| `exclude_flagged` | `true` | 被标记的恶意刷量日是否剔除出预测（防止污染基线） |
| `cooldown_days` | `7` | 标记恶意刷量后未来多少天内强制不刷量（恢复期） |
| `baseline_days` | `60` | 双基线检测：长期基线窗口（防"平台抬升"型刷量漏检） |
| `slope_days` | `14` | 持续上升斜率检测窗口 |
| `slope_threshold` | `0.02` | 斜率阈值（相对中位数/天），超阈值视为慢性爬升候选 |
| `baseline_views` | `0` | 手动目标浏览量；大于 0 时替代自动预测，系统围绕该值刷量 |
| `boost_mode` | `stable` | 模式切换：`stable` 维持稳定 / `growth` 刷量（目标=基准×倍率） |
| `growth_factor` | `1.5` | growth 模式的目标倍率（如 1.5 = 比基准高 50%） |
| `volatility` | `0.10` | 刷量波动率：每日提升量在「缺口 ×(1±波动率)」内随机，使曲线围绕基线波动；建议 ≤ 0.15 |
| `boost_n` | `5` | 每日提升使用的最近推荐数量 |
| `single_cap` | `300` | 单个游戏单日成功访问次数上限 |
| `batch_size` | `50` | 每批处理的游戏数量 |
| `target_ratio` | `0.95` | 累计提升达到目标值的该比例后提前停止（仅 ratio 模式生效） |
| `boost_distribution` | `"ratio"` | 提升分配方式：`ratio` 达到目标比例提前停（默认）；`exact` 按推荐条数精确分配配额，恰好刷满目标（如 500 / 30 条 → 前 20 条各 17 次 + 后 10 条各 16 次） |
| `threads` | `1` | 并发线程数（建议保持 1 精确计数） |
| `wait_min` / `wait_max` | `8.0` / `16.0` | 两次请求之间的随机等待区间（秒） |
| `max_retries` | `3` | 连续请求失败达到该次数后中止任务 |

## 定时调度

### 方式一：守护进程（daemon，推荐）

常驻运行、配置热重载、失败自动等明天、Ctrl+C/SIGTERM 优雅退出：

```bash
python curator_views/cli.py daemon --config config.yaml
```

配合 systemd 保持常驻（进程意外退出自动重启）：

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

Windows 可用 NSSM 包装：`nssm install curator-views "C:\Path\To\python.exe"
"C:\Path\To\curator_views\cli.py daemon --config C:\Path\To\config.yaml"`。

### 方式二：外部定时器（cron / 任务计划程序）

每天固定时间（**北京时间 10:00**）执行一次 `run`。Steam 统计滞后一天，
早上执行可拿到前一天的完整数据。

Linux（systemd 定时器）：

```ini
# /etc/systemd/system/curator-views.service
[Unit]
Description=Steam Curator Views Daily Run
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/CuratorViewsGuard
ExecStart=/usr/bin/python3 curator_views/cli.py run --config /path/to/config.yaml

[Install]
WantedBy=multi-user.target
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

### Windows（任务计划程序）

1. 打开「任务计划程序」→「创建任务」；
2. **触发器**：每天 10:00，勾选「已启用」；
3. **操作** → 新建 → 程序或脚本：
   - 程序或脚本：`C:\Path\To\python.exe`
   - 添加参数：`C:\Path\To\CuratorViewsGuard\curator_views\cli.py run --config C:\Path\To\config.yaml`
   - 起始于：`C:\Path\To\CuratorViewsGuard`
4. **条件**：建议取消「只有在计算机使用交流电源时才启动此任务」；
5. **设置**：勾选「如果任务失败，重新启动任务」（如 3 次，间隔 5 分钟）。

## 数据存储

SQLite 单文件（默认 `views.db`），WAL 模式支持并发读写，包含三张表：

- `daily_stats`：每日浏览量统计（date_ts → num_views）；
- `boost_log`：刷量台账（每批记录 + 手动修正记录）；
- `kv_store`：键值状态（程序启动日、溢出标记、最近抓取日期等）。

`export` 命令可随时把 `daily_stats` 导出为 JSON / CSV。

## 测试

```bash
python -m pytest tests/ -v
```

包含 81 个单元测试，覆盖预测边界、溢出检测连续段逻辑、双基线 / 斜率检测、
调度器子进程执行器、时区工具，以及手动基线 / 模式切换 / 波动率 / 恢复期 /
标记日剔除行为；`tests/test_login.py`（13 个）另外覆盖 login 的 Cookie 规范化、
有效性校验与浏览器超时路径。

## 常见问题

### Cookie 失效 / 抓取报"未找到 g_StatsAppViews"

Steam 登录态会过期。症状：`admin/overview` 被重定向到公开 curator 页（fetcher 会
提示"请求被重定向到公开页面（可能 Cookie 已失效或未登录）"）。
解决：先跑 `python curator_views/cli.py login --config config.yaml --validate-only`
确认是否已失效，再跑 `python curator_views/cli.py login --config config.yaml`
半自动重新生成 `cookie.txt`。

### 启动后头几天为什么不刷量

启动期（数据不足 14 天）预测自动乘 0.9 保守系数；且"今日需刷量 = 预期 − 昨日自然流量"
在数据不足时通常为 0。这是设计行为——先积累自己的平稳基线，不急于刷量。

### 为什么今天没有刷量

`需刷量 ≤ 0` 时当天不刷。常见原因：昨日实际已经高于预期（可能被恶意刷了，
或自然流量好），系统认为不需要补。这正是防刷逻辑在起作用。

### 如何手动修正某天的刷量记录

用 `boost` 手动刷量后记录自动写入台账；如需覆盖历史某天，可通过 SQLite 直接
操作 `boost_log` 表，或重新运行手动刷量。

## 免责声明

本工具仅用于提升**自己运营**的 Steam 鉴赏家评测的浏览量。请勿用于对其他鉴赏家组
或平台进行恶意操作。使用本工具产生的任何后果（包括但不限于 Steam 账号异常、
鉴赏家资格被撤销等）由使用者自行承担。请在遵守 Steam 订阅者协议与当地法律法规的
前提下使用。Cookie 属于敏感信息，请勿将 `cookie.txt` 提交到版本控制或分享给他人。
