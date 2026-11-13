# CuratorViewsGuard — Steam 鉴赏家浏览量预测与受控提升系统

基于 Steam 鉴赏家组的历史每日浏览量数据，自动预测今日预期浏览量，并通过
受控、限速的访问方式平滑提升浏览量。系统内置反作弊检测：识别其他小组的
恶意刷量造成的浏览量异常虚高，并将溢出量分摊到后续日期，避免浏览曲线
出现突兀尖峰而被 Steam 误判为机器人行为。

## 功能特性

- 浏览量预测：自适应中位数窗口 + 封顶线性趋势回归，启动阶段保守系数抑制误判；
  也可配置 `baseline_views` 直接指定手动目标浏览量；
- 反作弊检测：三重检测叠加——相对预期超额（尖峰）、长期基线超额（平台抬升）、
  持续上升斜率（慢性爬升）；标记日不参与预测防止污染基线，标记后恢复期内
  强制不刷等待曲线自然回落；
- 模式切换：`boost_mode: stable` 维持稳定 / `growth` 用 `growth_factor` 调台阶
  （>1 刷高、<1 压低），`growth_rate` 按天线性推进（正数上涨、负数下滑）；
  两者相乘，检测参照不会把自己刷的量误判为恶意刷量；
- 受控提升：分批、限速、单 App 上限、按目标比例提前停止，尽量模拟自然流量；
  配置 `volatility` 波动率后，每日提升量在缺口附近随机，曲线围绕基线自然波动；
- 每日刷量列表：配置 `boost_list_file` 后按列表逐行刷量，一行一个数量，
  每天消费首行并在成功后删除该行（刷完一行删一行）；
- 缺口按昨日自然流量计算：从 `boost_log` 台账扣除昨日自己刷的量，避免把昨天的
  补量当成今天已有的基础重复扣减（否则目标补不满、还会两天一摆）；
- 调度器/执行器分离：默认同进程直调 Booster，也可配置 `executor_cmd` 以独立
  子进程方式唤起执行器刷量（调度器崩了不影响执行器，执行器可被手动单独触发）；
- 每日完整流程：抓取统计 -> 入库 -> 预测 -> 溢出检测 -> 计算缺口 -> 提升 -> 通知；
- 通知推送：可选 ntfy 推送（未配置时退化为日志记录）；
- 只读检查与数据导出：随时查看最近 7 天与预测摘要，或导出历史统计数据。

## 安装

要求 Python 3.10 及以上版本。

```bash
# 进入项目目录后安装依赖（requirements.txt 位于仓库根目录）
pip install -r requirements.txt
```

`login` 命令额外需要 playwright（可选，不属于默认安装）。未安装时执行 `login`
会提示安装。安装命令：

```bash
pip install playwright
playwright install chromium
```

## 快速开始

```bash
# 1. 生成默认配置文件（位于当前目录 config.yaml）
python curator_views/cli.py init-config

# 2. 编辑 config.yaml，填写 clan_id（鉴赏家组 ID）；cookie_file 可选
#    （留空/文件不存在则未登录运行，后台统计可能抓取失败）
#    Cookie 文件可用 login 命令半自动生成：
python curator_views/cli.py login --config config.yaml

# 3. 只读检查：抓取统计并预览预测（不会产生任何提升请求）
python curator_views/cli.py check --config config.yaml

# 4. 执行每日完整流程（抓取-预测-检测-提升-通知）
python curator_views/cli.py run --config config.yaml

# 演练模式：只计算不真正提升
python curator_views/cli.py run --config config.yaml --dry-run

# 临时关闭提升（优先于配置文件中的 boost_enabled）
python curator_views/cli.py run --config config.yaml --no-boost
```

## 命令说明

### init-config

生成默认配置文件模板。

```bash
python curator_views/cli.py init-config [--path config.yaml]
```

### daemon

守护进程模式：进程常驻运行，每天到达配置的 `daily_time`（默认 `10:00`，
Asia/Shanghai）自动执行一次每日完整流程。**每次循环重新加载配置文件**，
修改任意配置（含执行时间）无需重启进程。

```bash
python curator_views/cli.py daemon --config config.yaml [--dry-run] [--boost | --no-boost]
```

- 启动时若今天尚未执行且已过执行时刻，立即补跑一次；
- 单次执行失败不退出进程，记录日志后等待下一天；
- **同一天不重复执行**：`run_daily` 会把 `last_run_date` 写进数据库，daemon
  每轮到点前会核对它，因此手动 `run` 与 daemon 谁先执行都算当天已完成；
- 前台运行时 INFO 日志同时输出到控制台（启动即打印日志路径与下次执行时间），
  文件日志仍写入数据库同级的 `curator_views.log`；
- Ctrl+C 会**立即中断**当前执行并退出（状态文件写入 `stopped`）；SIGTERM
  （POSIX / systemd stop）则等进行中的任务收尾后优雅退出。建议配合 systemd
  `Restart=always` 或 NSSM 保持常驻；需要释放终端请用 `nohup` / `setsid` /
  systemd 后台运行，静默本身并不会释放控制台。

### info

查看守护进程是否在运行：离线只读，不要求 `clan_id` / Cookie，不触网，也
不会创建空数据库。daemon 在 `db_path` 同级目录写入 `daemon_state.json`，
`info` 读取它并优先按 PID 判断存活（PID 不可用时回退到 90 秒心跳阈值）。

```bash
python curator_views/cli.py info --config config.yaml
python curator_views/cli.py info --config config.yaml --json
```

人类可读输出包含 PID、当前阶段、心跳、上次/下次执行时间与本地数据概况；
`--json` 输出同样的原始字典，便于脚本处理。长时间提升期间心跳可能暂停，
存活仍以 PID 为准；PID 复用是已知限制。状态文件还持久化 `last_run_date`，
daemon 重启后同一天不会重复执行。

### run

执行每日完整流程：抓取 Steam 统计并入库，逐日预测期望浏览量，检测异常溢出，
计算今日缺口，启用提升时调用 Booster 执行受控提升，最后发送每日简报。
成功后以 JSON 打印汇总（含今日预期、昨日实际、缺口、溢出天数、实际提升数等），
并在数据库同级目录写入 `curator_views.log` 日志。

```bash
python curator_views/cli.py run --config config.yaml [--dry-run] [--boost | --no-boost]
```

- `--dry-run`：演练模式，计算一切但实际不发送提升请求；
- `--boost`：强制启用提升；`--no-boost`：强制关闭提升（优先于配置）。

### 调度器 / 执行器分离

默认情况下 `run` 在同一进程内直接调用 Booster 刷量。若在 `config.yaml` 中配置
`executor_cmd`（如 `python cli.py boost --config {config} --target {need} --apps {apps}`），
`run`（调度器）会把计算出的任务下发给独立执行器子进程执行，执行器完成后
调度器解析其 JSON 输出（`boosted` 字段）并计入每日简报。两种模式可随时切换，
执行器也可手动单独触发。

### 每日刷量列表（可选）

配置 `boost_list_file` 指向一个文本文件后，每日提升目标改为按列表逐行读取：

```yaml
boost_list_file: "boost_plan.txt"
```

```text
# 一行一个刷量数量，空行和 # 注释会被忽略
500
400   # 行尾注释也支持
0     # 0 表示当天不刷（该行仍会被消费）
```

每天取第一条有效行覆盖计算出的缺口，成功提升后从文件中删除该行
（原子写回，其余行与换行符保持不变）。列表用完或文件缺失时当天不刷量，
不会回退到自动预测；恢复期、`--dry-run`、`boost_enabled: false`、提升完全
失败时均不消费列表行。列表数值仍受 `single_cap` / `target_ratio` 等执行参数
约束，需要精确刷满可配合 `target_ratio: 1.0` 或 `boost_distribution: exact`。

### check

只读检查：抓取统计数据，打印最近 7 天浏览量与预测摘要。不会产生任何写入
（含网络写入），可放心用于日常巡检。

```bash
python curator_views/cli.py check --config config.yaml
```

### boost

手动执行一轮受控提升（不经过预测/检测流程，直接提升指定次数）。

```bash
python curator_views/cli.py boost --config config.yaml --target N [--apps M] [--dry-run]
```

- `--target N`：本轮目标提升次数（必填）；
- `--apps M`：使用的最近推荐数量（默认取配置中的 `boost_n`）；
- `--dry-run`：仅记录提升计划，不发送任何请求。

### login

Steam 登录页有验证码、新设备/异地还要 Steam Guard 手机确认，无法可靠全自动。
`login` 采用 Playwright 半自动登录：打开真实 Chromium 窗口由你完成登录，脚本
等待 `steamLoginSecure` 出现后导出 Cookie，并校验有效性，通过才写入 `cookie.txt`
（校验失败不覆盖原文件）。需额外安装 playwright（可选）：

```bash
pip install playwright
playwright install chromium
```

```bash
python curator_views/cli.py login --config config.yaml
python curator_views/cli.py login --config config.yaml --validate-only
```

- `--config PATH`：配置文件路径（默认 `config.yaml`）；
- `--validate-only`：只校验已有 `cookie.txt`，不打开浏览器；
- `--headless`：无界面模式（通常无法完成验证码/手机确认，默认关闭）；
- `--no-validate`：登录后跳过校验直接写入（不推荐）；
- `--timeout SECONDS`：等待登录完成的超时（默认 300）；
- `--profile-dir PATH`：自定义浏览器持久化目录，默认配置目录下的 `.steam_profile`；
- `--cookie-file PATH`：写入的 Cookie 文件路径（默认取配置 `cookie_file`）。

登录态保存在持久化目录 `.steam_profile/` 中，第二次运行通常已登录，无需再输密码。
输出为 JSON 汇总（不含 Cookie 内容）：成功退出码 0，校验失败/文件缺失 1。

统计与刷量需要不同账号时，配置 `boost_cookie_file` 并在登录时指定目标文件
（不同账号需用不同 `--profile-dir`）：

```bash
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_stats.txt --profile-dir .steam_profile_stats
python curator_views/cli.py login --config config.yaml \
  --cookie-file cookie_boost.txt --profile-dir .steam_profile_boost
```

`boost_cookie_file` 与 `cookie_file` 完全独立：留空时刷量以未登录（游客）模式
运行，不会复用 `cookie_file`。旧配置若希望刷量带登录态，需显式把
`boost_cookie_file` 指向同一文件。

### export

从本地数据库导出全部每日统计数据。

```bash
python curator_views/cli.py export --config config.yaml --format json|csv [--out FILE]
```

- `--format`：导出格式，`json`（默认）或 `csv`；
- `--out FILE`：输出文件路径；缺省时打印到标准输出。

### version

```bash
python curator_views/cli.py version
```

## 配置文件说明

默认模板通过 `init-config` 生成，相对路径均基于配置文件所在目录解析。
所有键的默认值如下：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `clan_id` | （空） | Steam 鉴赏家组 ID，纯数字，必填 |
| `cookie_file` | `cookie.txt` | 统计用登录 Cookie 文件路径；留空或文件不存在时以未登录（游客）模式运行，不发送 Cookie |
| `boost_cookie_file` | （空） | 刷量专用 Cookie 文件路径；与 `cookie_file` 完全独立，留空则刷量以未登录（游客）模式运行 |
| `proxy` | （空） | 代理地址，如 `http://127.0.0.1:10279`，留空不使用 |
| `db_path` | `views.db` | SQLite 数据库文件路径 |
| `ntfy_url` | （空） | ntfy 服务地址，如 `https://ntfy.sh` |
| `ntfy_topic` | （空） | ntfy 主题名；与 `ntfy_url` 同时配置才启用推送 |
| `boost_enabled` | `true` | 是否启用每日受控提升 |
| `daily_time` | `"10:00"` | daemon 模式的每日执行时刻（Asia/Shanghai，HH:MM） |
| `executor_cmd` | （空） | 执行器子进程命令模板，支持 `{config}`/`{need}`/`{apps}` 占位符；留空则同进程直调 |
| `boost_list_file` | （空） | 每日刷量列表文件路径；非空时一行一个刷量数，每天消费首行，成功刷完后删除该行 |
| `window_days` | `14` | 中位数计算窗口（天数） |
| `trend_cap` | `0.15` | 趋势斜率相对中位数的封顶比例 |
| `startup_factor` | `0.9` | 启动阶段保守系数 |
| `trend_min_points` | `5` | 计算趋势所需的最少数据点数 |
| `outlier_k` | `0.30` | 溢出判定阈值：实际浏览量超过预期 x (1+k) 视为候选异常 |
| `consecutive_days` | `2` | 连续候选达到该天数才标记为恶意刷量 |
| `absorb_days` | `3` | 溢出量分摊到未来多少天 |
| `exclude_flagged` | `true` | 被标记的恶意刷量日是否剔除出预测（防止污染基线） |
| `cooldown_days` | `7` | 标记恶意刷量后未来多少天内强制不刷量（恢复期） |
| `baseline_days` | `60` | 双基线检测：长期基线窗口（防"平台抬升"型刷量漏检） |
| `slope_days` | `14` | 持续上升斜率检测窗口 |
| `slope_threshold` | `0.02` | 斜率阈值（相对中位数/天），超阈值视为慢性爬升候选 |
| `baseline_views` | `0` | 手动目标浏览量；大于 0 时替代自动预测，系统围绕该值刷量 |
| `boost_mode` | `stable` | 模式切换：`stable` 维持稳定 / `growth` 刷量（目标=基准×倍率） |
| `growth_factor` | `1.5` | growth 模式的目标倍率（如 1.5 = 比基准高 50%；小于 1 表示压低台阶，如 0.85） |
| `growth_rate` | `0` | growth 模式下每天相对基准的线性变化率（0.01 = 每天 +1%，-0.01 = 每天 -1% 匀速下滑）；与 `growth_factor` 相乘，可同时使用；修改速率会从当天重新起算 |
| `volatility` | `0.10` | 刷量波动率：每日提升量在「缺口 ×(1±波动率)」内随机；建议 ≤ 0.15 |
| `boost_n` | `5` | 每日提升使用的最近推荐数量 |
| `single_cap` | `300` | 单个游戏单日成功访问次数上限 |
| `batch_size` | `50` | 每批处理的游戏数量 |
| `target_ratio` | `0.95` | 累计提升达到目标值的该比例后提前停止（仅 ratio 模式生效） |
| `boost_distribution` | `"ratio"` | 提升分配方式：`ratio` 达到目标比例提前停（默认）；`exact` 按推荐条数精确分配配额，恰好刷满目标（如 500 / 30 条 → 前 20 条各 17 次 + 后 10 条各 16 次） |
| `threads` | `1` | 并发线程数（建议保持 1） |
| `wait_min` | `8.0` | 两次请求之间的最小等待秒数 |
| `wait_max` | `16.0` | 两次请求之间的最大等待秒数 |
| `max_retries` | `3` | 连续请求失败达到该次数后中止任务 |

### 预测参数里的“斜率”是什么

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

`trend_cap` 用来封顶斜率，防止几天的异常值把预测带飞：

```
|斜率| ≤ 中位数 × trend_cap
```

例：最近 14 天中位数 1000，`trend_cap: 0.15`，斜率最多按 `±150 次/天`
参与计算。回归算出 `+40/天` 就用 `+40`；算出 `+500/天` 也只按 `+150`，
今日预期 = 1000 + 150 = 1150。

> 注意：反作弊参数 `slope_days` / `slope_threshold` 也描述“斜率”，但用途
> 不同——它们检测最近 `slope_days` 天（默认 14 天）的上涨速度是否超过
> `中位数 × slope_threshold`（默认 2%/天），用于识别“每天温和爬升”的慢性
> 刷量，不参与今日预期计算。

### 双基线检测是什么

系统同时维护两条“正常水平”参照线，任何一条被突破都可能触发异常检测：

| 基线 | 窗口 | 计算方式 | 作用 |
| --- | --- | --- | --- |
| 短期预测基线 | `window_days`（默认 14 天） | 中位数 + 封顶趋势 → 每日 `expected` | 判断单日是否“比预期高太多” |
| 长期基线 | `baseline_days`（默认 60 天） | 窗口内浏览量中位数 `long_baseline` | 独立参照，防短期预期被污染 |

判定逻辑（`anti_cheat.detect_overflow`）：

```
候选异常日 = 实际 > expected × (1 + outlier_k)
          或 实际 > long_baseline × (1 + outlier_k)
          或 命中持续上升斜率检测
连续 consecutive_days 天候选 → 这些天全部标记为恶意刷量
```

为什么要长期基线：短期预期由最近 14 天数据算出。如果被持续灌量，14 天
中位数会被抬高，`expected` 也跟着抬高，单看“实际 vs expected”就抓不到
“平台整体抬升”型刷量。60 天中位数变化慢得多，能提供一条相对干净的参照。

例：长期基线 1000、`outlier_k: 0.30` → 实际 >1300 即候选；即使 14 天预期
被污染到 1400，某天实际 1350 仍会被长期基线抓到。

`exclude_flagged`（默认 `true`）：一旦某天被标记，该天会被剔除出预测与
长期基线计算，再重新检测，最多迭代 3 轮，避免虚高数据继续污染参照。
`baseline_days: 0` 表示关闭长期基线，只保留“实际 vs 短期预期”的检测。

慢性爬升检测（`slope_days` / `slope_threshold`）也依赖长期基线：当最近
14 天斜率 > 中位数 × 2%/天 时，只把窗口内高于 `long_baseline × (1+outlier_k)`
的高位日作为候选，避免把真实增长误判为刷量。

## 定时调度

建议每天固定时间（如北京时间 10:00）执行一次 `run`。Steam 统计滞后一天，
早上执行可拿到前一天的完整数据。

### Linux（systemd）

```ini
# /etc/systemd/system/curator-views.service
[Unit]
Description=Steam Curator Views Daily Run
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/CuratorViewsGuard
ExecStart=/usr/bin/python3 curator_views/cli.py run --config /path/to/config.yaml
# 日志由 CLI 自动写入数据库同级目录的 curator_views.log

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

1. 打开「任务计划程序」->「创建任务」；
2. 触发器：每天 10:00（或按需选择），勾选「已启用」；
3. 操作 -> 新建 -> 程序或脚本：
   - 程序或脚本：`C:\Path\To\python.exe`
   - 添加参数：`C:\Path\To\CuratorViewsGuard\curator_views\cli.py run --config C:\Path\To\config.yaml`
   - 起始于：`C:\Path\To\CuratorViewsGuard`
4. 条件：建议取消勾选「只有在计算机使用交流电源时才启动此任务」；
5. 设置：勾选「如果任务失败，重新启动任务」（如 3 次，间隔 5 分钟）。

## 免责声明

本工具仅用于提升自己运营的 Steam 鉴赏家评测的浏览量，请勿用于对其他
鉴赏家组或平台进行恶意操作。使用本工具产生的任何后果（包括但不限于
Steam 账号异常、鉴赏家资格被撤销等）由使用者自行承担。请在遵守 Steam
订阅者协议与当地法律法规的前提下使用。Cookie 属于敏感信息，请勿将
`cookie.txt` 提交到版本控制或分享给他人。
