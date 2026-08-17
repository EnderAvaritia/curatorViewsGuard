# Steam 鉴赏家浏览量预测与受控提升系统

基于 Steam 鉴赏家组的历史每日浏览量数据，自动预测今日预期浏览量，并通过
受控、限速的访问方式平滑提升浏览量。系统内置反作弊检测：识别其他小组的
恶意刷量造成的浏览量异常虚高，并将溢出量分摊到后续日期，避免浏览曲线
出现突兀尖峰而被 Steam 误判为机器人行为。

## 功能特性

- 浏览量预测：自适应中位数窗口 + 封顶线性趋势回归，启动阶段保守系数抑制误判；
  也可配置 `baseline_views` 直接指定手动目标浏览量；
- 反作弊检测：连续多日超过预期阈值时标记恶意刷量，溢出量自动分摊到后续日期，
  标记日不参与预测防止污染基线，标记后恢复期内强制不刷等待曲线自然回落；
- 受控提升：分批、限速、单 App 上限、按目标比例提前停止，尽量模拟自然流量；
  配置 `volatility` 波动率后，每日提升量在缺口附近随机，曲线围绕基线自然波动；
- 调度器/执行器分离：默认同进程直调 Booster，也可配置 `executor_cmd` 以独立
  子进程方式唤起执行器刷量（调度器崩了不影响执行器，执行器可被手动单独触发）；
- 每日完整流程：抓取统计 -> 入库 -> 预测 -> 溢出检测 -> 计算缺口 -> 提升 -> 通知；
- 通知推送：可选 ntfy 推送（未配置时退化为日志记录）；
- 只读检查与数据导出：随时查看最近 7 天与预测摘要，或导出历史统计数据。

## 安装

要求 Python 3.10 及以上版本。

```bash
# 进入项目目录后安装依赖
pip install -r curator_views/requirements.txt
```

## 快速开始

```bash
# 1. 生成默认配置文件（位于当前目录 config.yaml）
python curator_views/cli.py init-config

# 2. 编辑 config.yaml，填写 clan_id（鉴赏家组 ID）与 cookie_file（登录 Cookie 文件）
#    cookie 文件内容为浏览器中复制的 Steam 登录 Cookie 字符串

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
| `cookie_file` | `cookie.txt` | 登录 Cookie 文件路径 |
| `proxy` | （空） | 代理地址，如 `http://127.0.0.1:10279`，留空不使用 |
| `db_path` | `views.db` | SQLite 数据库文件路径 |
| `ntfy_url` | （空） | ntfy 服务地址，如 `https://ntfy.sh` |
| `ntfy_topic` | （空） | ntfy 主题名；与 `ntfy_url` 同时配置才启用推送 |
| `boost_enabled` | `true` | 是否启用每日受控提升 |
| `executor_cmd` | （空） | 执行器子进程命令模板，支持 `{config}`/`{need}`/`{apps}` 占位符；留空则同进程直调 |
| `window_days` | `14` | 中位数计算窗口（天数） |
| `trend_cap` | `0.15` | 趋势斜率相对中位数的封顶比例 |
| `startup_factor` | `0.9` | 启动阶段保守系数 |
| `trend_min_points` | `5` | 计算趋势所需的最少数据点数 |
| `outlier_k` | `0.30` | 溢出判定阈值：实际浏览量超过预期 x (1+k) 视为候选异常 |
| `consecutive_days` | `2` | 连续候选达到该天数才标记为恶意刷量 |
| `absorb_days` | `3` | 溢出量分摊到未来多少天 |
| `exclude_flagged` | `true` | 被标记的恶意刷量日是否剔除出预测（防止污染基线） |
| `cooldown_days` | `7` | 标记恶意刷量后未来多少天内强制不刷量（恢复期） |
| `baseline_views` | `0` | 手动目标浏览量；大于 0 时替代自动预测，系统围绕该值刷量 |
| `volatility` | `0.10` | 刷量波动率：每日提升量在「缺口 ×(1±波动率)」内随机；建议 ≤ 0.15 |
| `boost_n` | `5` | 每日提升使用的最近推荐数量 |
| `single_cap` | `300` | 单个游戏单日成功访问次数上限 |
| `batch_size` | `50` | 每批处理的游戏数量 |
| `target_ratio` | `0.95` | 累计提升达到目标值的该比例后提前停止 |
| `threads` | `1` | 并发线程数（建议保持 1） |
| `wait_min` | `8.0` | 两次请求之间的最小等待秒数 |
| `wait_max` | `16.0` | 两次请求之间的最大等待秒数 |
| `max_retries` | `3` | 连续请求失败达到该次数后中止任务 |

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
WorkingDirectory=/path/to/reflect_steam_curator_views
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
   - 添加参数：`C:\Path\To\reflect_steam_curator_views\curator_views\cli.py run --config C:\Path\To\config.yaml`
   - 起始于：`C:\Path\To\reflect_steam_curator_views`
4. 条件：建议取消勾选「只有在计算机使用交流电源时才启动此任务」；
5. 设置：勾选「如果任务失败，重新启动任务」（如 3 次，间隔 5 分钟）。

## 免责声明

本工具仅用于提升自己运营的 Steam 鉴赏家评测的浏览量，请勿用于对其他
鉴赏家组或平台进行恶意操作。使用本工具产生的任何后果（包括但不限于
Steam 账号异常、鉴赏家资格被撤销等）由使用者自行承担。请在遵守 Steam
订阅者协议与当地法律法规的前提下使用。Cookie 属于敏感信息，请勿将
`cookie.txt` 提交到版本控制或分享给他人。
