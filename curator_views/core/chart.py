"""Forecast 折线图：生成自包含 HTML（内联 SVG），不依赖第三方库。

设计目标：
- 零依赖：只用标准库拼接 SVG，离线双击即可查看；
- 与 forecast 输出同源：直接消费 ``forecast_days`` 的 rows/summary，
  不再重新计算目标或缺口；
- 图表只做展示，不写数据库、不触网。
"""

from __future__ import annotations

import html
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .scheduler import _TZ

_WIDTH = 960
_HEIGHT = 340
_PAD_LEFT = 84
_PAD_RIGHT = 26
_PAD_TOP = 18
_PAD_BOTTOM = 42

_COLORS = {
    "target": "#2563eb",
    "natural": "#059669",
    "band": "#93c5fd",
    "quota": "#7c3aed",
    "need": "#dc2626",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _fmt(value: float) -> str:
    if abs(float(value) - round(float(value))) < 0.05:
        return f"{float(value):,.0f}"
    return f"{float(value):,.1f}"


def _config_float(config: Dict[str, Any], key: str) -> float:
    try:
        return float(config.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _nice_max(values: Sequence[float]) -> float:
    """把最大值向上取整到较易读的刻度，避免 y 轴出现 13,742 这类数字。"""
    peak = max((float(value) for value in values), default=0.0)
    if peak <= 0:
        return 1.0
    raw = peak * 1.08
    exponent = math.floor(math.log10(raw))
    base = 10 ** exponent
    for step in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
        if raw <= base * step:
            return base * step
    return base * 10


def _x_positions(count: int) -> List[float]:
    left = _PAD_LEFT
    width = _WIDTH - _PAD_LEFT - _PAD_RIGHT
    if count <= 1:
        return [left + width / 2]
    return [left + width * index / (count - 1) for index in range(count)]


def _x_label_indexes(count: int, limit: int = 8) -> List[int]:
    if count <= 0:
        return []
    auto_step = max(1, math.ceil(count / limit))
    indexes = list(range(0, count, auto_step))
    if indexes[-1] != count - 1:
        indexes.append(count - 1)
    return indexes


def _svg_chart(
    rows: List[Dict[str, Any]],
    series: List[Dict[str, Any]],
    *,
    aria_label: str,
    band: Optional[tuple[str, str]] = None,
) -> str:
    """绘制一张折线图；``series`` 元素包含 key/name/color/dash。"""
    if not rows:
        return '<p class="empty">没有可绘制的数据。</p>'

    count = len(rows)
    plot_width = _WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_height = _HEIGHT - _PAD_TOP - _PAD_BOTTOM
    xs = _x_positions(count)

    values: List[float] = []
    for item in series:
        values.extend(float(row.get(item["key"], 0) or 0) for row in rows)
    if band:
        low_key, high_key = band
        values.extend(float(row.get(low_key, 0) or 0) for row in rows)
        values.extend(float(row.get(high_key, 0) or 0) for row in rows)
    y_max = _nice_max(values)
    y_min = 0.0

    def y_pos(value: float) -> float:
        ratio = (float(value) - y_min) / (y_max - y_min or 1.0)
        ratio = min(1.0, max(0.0, ratio))
        return _PAD_TOP + plot_height - ratio * plot_height

    parts: List[str] = [
        f'<svg viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" '
        f'aria-label="{_esc(aria_label)}" '
        f'xmlns="http://www.w3.org/2000/svg">',
    ]

    # 网格与 y 轴刻度
    for step in range(5):
        value = y_max * step / 4
        y = y_pos(value)
        parts.append(
            f'<line x1="{_PAD_LEFT}" y1="{y:.1f}" '
            f'x2="{_WIDTH - _PAD_RIGHT}" y2="{y:.1f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_PAD_LEFT - 10}" y="{y + 4:.1f}" '
            'text-anchor="end" font-size="12" fill="#6b7280">'
            f"{_fmt(value)}</text>"
        )

    # 目标抖动区间（半透明带）
    if band:
        low_key, high_key = band
        upper = [
            (xs[index], y_pos(float(row.get(high_key, 0) or 0)))
            for index, row in enumerate(rows)
        ]
        lower = [
            (xs[index], y_pos(float(row.get(low_key, 0) or 0)))
            for index, row in enumerate(rows)
        ]
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in upper + lower[::-1])
        parts.append(
            f'<polygon points="{points}" fill="{_COLORS["band"]}" '
            'fill-opacity="0.28" stroke="none"/>'
        )

    # 折线
    for item in series:
        points = " ".join(
            f"{xs[index]:.1f},{y_pos(float(row.get(item['key'], 0) or 0)):.1f}"
            for index, row in enumerate(rows)
        )
        dash = (
            f' stroke-dasharray="{item["dash"]}"' if item.get("dash") else ""
        )
        parts.append(
            f'<polyline points="{points}" fill="none" '
            f'stroke="{item["color"]}" stroke-width="2.2" '
            f'stroke-linejoin="round" stroke-linecap="round"{dash}/>'
        )

    # 数据点 + 原生 tooltip
    for item in series:
        for index, row in enumerate(rows):
            value = float(row.get(item["key"], 0) or 0)
            parts.append(
                f'<circle cx="{xs[index]:.1f}" cy="{y_pos(value):.1f}" r="3" '
                f'fill="{item["color"]}"><title>'
                f'{_esc(row.get("date", ""))} {_esc(item["name"])}: '
                f'{_esc(_fmt(value))}</title></circle>'
            )

    # x 轴日期
    for index in _x_label_indexes(count):
        date_text = str(rows[index].get("date", ""))
        parts.append(
            f'<text x="{xs[index]:.1f}" y="{_HEIGHT - 14}" '
            'text-anchor="middle" font-size="11" fill="#6b7280">'
            f"{_esc(date_text[5:] if len(date_text) == 10 else date_text)}</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


def _legend(series: List[Dict[str, Any]], band_label: Optional[str]) -> str:
    items = [
        f'<span class="legend-item"><i style="background:{item["color"]}"></i>'
        f'{_esc(item["name"])}</span>'
        for item in series
    ]
    if band_label:
        items.append(
            f'<span class="legend-item"><i class="band" '
            f'style="background:{_COLORS["band"]}"></i>'
            f"{_esc(band_label)}</span>"
        )
    return "".join(items)


_CONSOLE_HEIGHT = 15
_CONSOLE_MARKERS = {
    "target": "●",
    "natural": "○",
    "quota": "◆",
    "need": "◇",
}


def _line_cells(
    x1: int, y1: int, x2: int, y2: int
) -> List[tuple[int, int]]:
    """Bresenham 直线，返回终端字符网格上的坐标。"""
    cells: List[tuple[int, int]] = []
    dx = abs(x2 - x1)
    sx = 1 if x1 < x2 else -1
    dy = -abs(y2 - y1)
    sy = 1 if y1 < y2 else -1
    error = dx + dy
    while True:
        cells.append((x1, y1))
        if x1 == x2 and y1 == y2:
            return cells
        double = 2 * error
        if double >= dy:
            error += dy
            x1 += sx
        if double <= dx:
            error += dx
            y1 += sy


def _console_chart(
    rows: List[Dict[str, Any]],
    series: List[Dict[str, Any]],
    width: int,
    title: str,
) -> List[str]:
    """用点状字符画一张图；返回可直接 print 的多行文本。"""
    if not rows:
        return [title, "  （没有可绘制的数据）"]

    values = [
        float(row.get(item["key"], 0) or 0)
        for item in series
        for row in rows
    ]
    y_max = _nice_max(values)
    height = _CONSOLE_HEIGHT

    def axis_text(value: float) -> str:
        if y_max >= 10:
            return f"{value:,.0f}"
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"

    label_values = {
        round((1 - value / y_max) * (height - 1)): axis_text(value)
        for value in (y_max * step / 4 for step in range(5))
    }
    y_labels = [label_values.get(row, "") for row in range(height)]
    label_width = max(len(label) for label in y_labels)
    plot_width = max(24, int(width) - label_width - 3)
    grid: List[List[Optional[str]]] = [
        [None] * plot_width for _ in range(height)
    ]

    def x_index(index: int) -> int:
        if len(rows) <= 1:
            return plot_width // 2
        return round(index * (plot_width - 1) / (len(rows) - 1))

    def y_index(value: float) -> int:
        ratio = min(1.0, max(0.0, value / (y_max or 1.0)))
        return round((1.0 - ratio) * (height - 1))

    def put_marker(x: int, y: int, marker: str) -> None:
        current = grid[y][x]
        if current is None or current == "·":
            grid[y][x] = marker
        elif current != marker and current != "*":
            grid[y][x] = "*"

    for item in series:
        marker = item.get("marker", "●")
        points = [
            (x_index(index), y_index(float(row.get(item["key"], 0) or 0)))
            for index, row in enumerate(rows)
        ]
        for x, y in points:
            put_marker(x, y, marker)
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            for x, y in _line_cells(x1, y1, x2, y2):
                current = grid[y][x]
                if current is None:
                    grid[y][x] = "·"
                elif current != marker and current not in ("·", "*"):
                    grid[y][x] = "*"

    lines = [title]
    for row_index in range(height):
        cells = "".join(cell or " " for cell in grid[row_index]).rstrip()
        lines.append(
            f"{y_labels[row_index]:>{label_width}} ┤ {cells}"
        )
    lines.append(f"{'':>{label_width}} └ " + "─" * plot_width)

    date_line = [" "] * plot_width
    for index in _x_label_indexes(len(rows), limit=max(2, plot_width // 8)):
        text = str(rows[index].get("date", ""))
        text = text[5:] if len(text) == 10 else text
        start = min(
            max(0, x_index(index) - len(text) // 2),
            plot_width - len(text),
        )
        if any(char != " " for char in date_line[start:start + len(text)]):
            continue
        date_line[start:start + len(text)] = list(text)
    lines.append(f"{'':>{label_width}}   " + "".join(date_line).rstrip())

    legend = "   ".join(
        f"{item.get('marker', '●')} {item['name']}" for item in series
    )
    if any("*" in row for row in grid):
        legend += "   * 重叠"
    lines.append(f"{'':>{label_width}}   {legend}")
    return lines


def render_forecast_console(
    result: Dict[str, Any],
    config: Dict[str, Any],
    source: Optional[Dict[str, Any]] = None,
    width: Optional[int] = None,
    include_meta: bool = True,
) -> str:
    """把 forecast 结果渲染为控制台点状折线图。"""
    rows = list(result.get("rows") or [])
    if not rows:
        return "预测折线图：没有可绘制的数据。"

    summary = result.get("summary") or {}
    source = source or {}
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    width = max(48, min(int(width), 120))

    first_date = summary.get("first_date") or rows[0]["date"]
    last_date = summary.get("last_date") or rows[-1]["date"]
    days = summary.get("days", len(rows))
    modeled = bool(summary.get("volatility_modeled", True))
    baseline = _config_float(config, "baseline_views")
    baseline_text = "自动预测" if baseline <= 0 else f"固定 {baseline:,.0f}"
    mode = str(config.get("boost_mode", "stable"))
    source_text = "Steam 在线抓取" if source.get("mode") == "fetch" else "本地数据库"
    day_step = int(source.get("day_step", 1) or 1)

    target_vol = _config_float(config, "target_volatility")
    target_vol_min = _config_float(config, "target_volatility_min")
    gap_vol = _config_float(config, "volatility")
    gap_vol_min = _config_float(config, "volatility_min")
    vol_text = "已纳入随机波动" if modeled else "未纳入随机波动"
    vol_detail = (
        f"目标 ±{target_vol:.0%}（最小 {target_vol_min:.0%}） · "
        f"缺口 ±{gap_vol:.0%}（最小 {gap_vol_min:.0%}）"
    )
    bounds = []
    if _config_float(config, "target_min") > 0:
        bounds.append(f"下限 {_config_float(config, 'target_min'):,.0f}")
    if _config_float(config, "target_max") > 0:
        bounds.append(f"上限 {_config_float(config, 'target_max'):,.0f}")
    bounds_text = " · " + " / ".join(bounds) if bounds else ""

    view_series = [
        {"key": "target", "name": "目标", "marker": _CONSOLE_MARKERS["target"]},
        {
            "key": "assumed_natural",
            "name": "假设自然流量",
            "marker": _CONSOLE_MARKERS["natural"],
        },
    ]
    boost_series = [
        {
            "key": "boost_quota",
            "name": "当日配额",
            "marker": _CONSOLE_MARKERS["quota"],
        },
        {"key": "need", "name": "实际待刷", "marker": _CONSOLE_MARKERS["need"]},
    ]

    lines = []
    if include_meta:
        lines.extend(
            [
                f"预测区间：{first_date} ~ {last_date}（{days} 天） · {source_text}",
                f"模式：{mode} · 基线：{baseline_text}{bounds_text}",
                f"波动率：{vol_text} · {vol_detail}",
                *(
                    [f"输出间隔：每 {day_step} 天（仅抽样显示，汇总按完整区间）"]
                    if day_step > 1
                    else []
                ),
                "",
            ]
        )
    lines.extend(
        _console_chart(
            rows,
            view_series,
            width,
            "浏览量：目标 vs 假设自然流量",
        )
    )
    lines.append("")
    lines.extend(
        _console_chart(
            rows,
            boost_series,
            width,
            "刷量：当日配额 vs 实际待刷",
        )
    )
    if include_meta:
        lines.append("")
        lines.append(
            "区间预计刷量 "
            f"{_fmt(float(summary.get('total_need', 0) or 0))} · 目标 "
            f"{_fmt(float(summary.get('target_first', 0) or 0))} → "
            f"{_fmt(float(summary.get('target_last', 0) or 0))}"
        )
        if modeled:
            lines.append("注：随机波动为本次抽样，重新运行可能不同。")
    return "\n".join(lines)


def render_forecast_html(
    result: Dict[str, Any],
    config: Dict[str, Any],
    source: Optional[Dict[str, Any]] = None,
) -> str:
    """把 forecast 结果渲染为自包含 HTML 字符串。"""
    rows = list(result.get("rows") or [])
    summary = result.get("summary") or {}
    source = source or {}

    first_date = summary.get("first_date") or (rows[0]["date"] if rows else "-")
    last_date = summary.get("last_date") or (rows[-1]["date"] if rows else "-")
    days = summary.get("days", len(rows))
    total_need = int(summary.get("total_need", 0) or 0)

    modeled = bool(summary.get("volatility_modeled", True))
    target_vol = _config_float(config, "target_volatility")
    target_vol_min = _config_float(config, "target_volatility_min")
    gap_vol = _config_float(config, "volatility")
    gap_vol_min = _config_float(config, "volatility_min")
    baseline = _config_float(config, "baseline_views")
    baseline_text = "自动预测" if baseline <= 0 else f"固定 {baseline:,.0f}"
    mode = str(config.get("boost_mode", "stable"))

    has_band = any(
        abs(
            float(row.get("target_high", 0) or 0)
            - float(row.get("target_low", 0) or 0)
        )
        > 0.05
        for row in rows
    )
    band = ("target_low", "target_high") if has_band else None
    band_label = (
        f"目标区间 ±{target_vol:.0%}（最小 {target_vol_min:.0%}）"
        if has_band
        else None
    )

    view_series = [
        {"key": "target", "name": "目标", "color": _COLORS["target"]},
        {"key": "assumed_natural", "name": "假设自然流量", "color": _COLORS["natural"]},
    ]
    boost_series = [
        {"key": "boost_quota", "name": "当日配额", "color": _COLORS["quota"]},
        {"key": "need", "name": "实际待刷", "color": _COLORS["need"], "dash": "6 4"},
    ]

    if modeled:
        vol_text = "已纳入随机波动（本图为一次独立抽样）"
    else:
        vol_text = "未纳入随机波动（--no-volatility，未抖动基准）"
    vol_detail = (
        f"目标 ±{target_vol:.0%}（最小 {target_vol_min:.0%}） · "
        f"缺口 ±{gap_vol:.0%}（最小 {gap_vol_min:.0%}）"
    )
    source_text = "Steam 在线抓取" if source.get("mode") == "fetch" else "本地数据库"
    day_step = int(source.get("day_step", 1) or 1)
    step_meta = (
        f'\n  <p class="meta">输出间隔：每 {day_step} 天（仅抽样显示，汇总按完整区间）</p>'
        if day_step > 1
        else ""
    )
    bounds = []
    if _config_float(config, "target_min") > 0:
        bounds.append(f"下限 {_config_float(config, 'target_min'):,.0f}")
    if _config_float(config, "target_max") > 0:
        bounds.append(f"上限 {_config_float(config, 'target_max'):,.0f}")
    bounds_text = " · 目标上下界：" + " / ".join(bounds) if bounds else ""

    generated = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    view_chart = _svg_chart(
        rows,
        view_series,
        aria_label="目标浏览量、目标区间与假设自然流量折线图",
        band=band,
    )
    boost_chart = _svg_chart(
        rows,
        boost_series,
        aria_label="当日刷量配额与实际待刷量折线图",
    )

    note = (
        "随机波动已纳入：同一天重新运行 forecast 可能得到不同曲线；"
        "图中的目标区间是可能范围，不是确定性预测。"
        if modeled
        else "随机波动未纳入：曲线为未抖动基准，适合对照配置本身。"
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CuratorViewsGuard 预测折线图 {_esc(first_date)} ~ {_esc(last_date)}</title>
<style>
  :root {{
    color-scheme: light;
    --ink: #111827;
    --muted: #6b7280;
    --line: #e5e7eb;
    --card: #ffffff;
    --bg: #f8fafc;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px;
    background: var(--bg); color: var(--ink);
    font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  }}
  main {{ max-width: 1040px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  h2 {{ font-size: 16px; margin: 0 0 10px; }}
  .meta, .note {{ color: var(--muted); font-size: 13px; margin: 4px 0; }}
  .note {{ margin-bottom: 18px; }}
  .card {{
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 16px 18px; margin: 0 0 18px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  }}
  svg {{ width: 100%; height: auto; display: block; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin: 0 0 10px; }}
  .legend-item {{ display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }}
  .legend-item i {{
    width: 14px; height: 3px; border-radius: 2px; display: inline-block;
  }}
  .legend-item i.band {{ height: 10px; border-radius: 3px; opacity: 0.55; }}
  .empty {{ color: var(--muted); padding: 28px 0; text-align: center; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 20px; font-size: 13px; margin-top: 6px; }}
  .stats b {{ font-size: 15px; }}
</style>
</head>
<body>
<main>
  <h1>CuratorViewsGuard 预测折线图</h1>
  <p class="meta">区间：{_esc(first_date)} ~ {_esc(last_date)}（{_esc(days)} 天） · 生成时间：{_esc(generated)}</p>
  <p class="meta">模式：{_esc(mode)} · 基线：{_esc(baseline_text)} · 数据来源：{_esc(source_text)}</p>
  <p class="meta">波动率：{_esc(vol_text)} · {_esc(vol_detail)}{_esc(bounds_text)}</p>{step_meta}
  <p class="note">{_esc(note)}</p>

  <section class="card">
    <h2>浏览量：目标 vs 假设自然流量</h2>
    <div class="legend">{_legend(view_series, band_label)}</div>
    {view_chart}
  </section>

  <section class="card">
    <h2>刷量：当日配额 vs 实际待刷</h2>
    <div class="legend">{_legend(boost_series, None)}</div>
    {boost_chart}
    <div class="stats">
      <span>区间预计刷量：<b>{_fmt(total_need)}</b></span>
      <span>目标：<b>{_fmt(float(summary.get("target_first", 0) or 0))}</b>
        → <b>{_fmt(float(summary.get("target_last", 0) or 0))}</b></span>
    </div>
  </section>
</main>
</body>
</html>
"""


def write_forecast_chart(
    path: Path | str,
    result: Dict[str, Any],
    config: Dict[str, Any],
    source: Optional[Dict[str, Any]] = None,
) -> Path:
    """把预测折线图写入 HTML 文件，返回实际路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_forecast_html(result, config, source), encoding="utf-8"
    )
    return target
