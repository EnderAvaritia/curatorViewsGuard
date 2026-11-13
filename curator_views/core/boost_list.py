"""每日刷量列表文件：一行一个数量，每天消费一行，消费后从文件删除该行。

列表文件格式（UTF-8 文本）::

    # 以 # 开头的是注释行，空行会被忽略
    500
    300  # 行尾注释同样支持
    0          # 允许 0，表示当天不刷量（该行仍会被消费）

有效行按从上到下的顺序使用：每天取第一条有效行作为当日刷量目标，
成功提升后把它从文件里删掉，其余行（含注释、空行）保持原样。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple


def _parse_amount(raw: str) -> Optional[int]:
    """解析一行文本中的刷量数量；空白/注释/非法/负数返回 None。"""
    text = raw.split("#", 1)[0].strip()
    if not text:
        return None
    try:
        amount = int(text)
    except ValueError:
        return None
    return amount if amount >= 0 else None


def read_entries(path: str) -> List[Tuple[int, int]]:
    """读取全部有效列表项。

    Args:
        path: 列表文件路径。

    Returns:
        ``[(行号, 数量), ...]``，行号从 0 开始，按文件顺序排列。

    Raises:
        FileNotFoundError: 列表文件不存在。
        OSError: 文件无法读取（权限等）。
    """
    lines = _read_lines(path, keepends=False)
    entries: List[Tuple[int, int]] = []
    for index, raw in enumerate(lines):
        amount = _parse_amount(raw)
        if amount is not None:
            entries.append((index, amount))
    return entries


def peek_next(path: str) -> Tuple[Optional[int], int]:
    """查看下一条待刷数量与剩余有效行数（不修改文件）。

    Args:
        path: 列表文件路径。

    Returns:
        ``(数量, 剩余有效行数)``；没有有效行时返回 ``(None, 0)``。

    Raises:
        FileNotFoundError: 列表文件不存在。
        OSError: 文件无法读取。
    """
    entries = read_entries(path)
    if not entries:
        return None, 0
    return entries[0][1], len(entries)


def consume_next(path: str) -> int:
    """删除第一条有效行并返回其数量。

    采用"临时文件 + ``os.replace``"原子重写，避免中途失败把列表写坏；
    其余行（含注释、空行、换行符风格）保持原样。

    Args:
        path: 列表文件路径。

    Returns:
        被删除行的刷量数量。

    Raises:
        FileNotFoundError: 列表文件不存在。
        ValueError: 文件里没有可消费的有效行。
        OSError: 文件无法读取或写回。
    """
    file_path = Path(path)
    # keepends=True 保证未被消费的行连同行尾换行符原样写回
    lines = _read_lines(file_path, keepends=True)
    for index, raw in enumerate(lines):
        amount = _parse_amount(raw)
        if amount is None:
            continue
        del lines[index]
        _atomic_write(file_path, "".join(lines))
        return amount
    raise ValueError("列表文件没有可消费的有效行")


def _read_lines(path: str | Path, keepends: bool) -> List[str]:
    """读取文本行；``newline=""`` 保留原始换行符（如 CRLF），不做转换。"""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read().splitlines(keepends=keepends)


def _atomic_write(path: Path, content: str) -> None:
    """把内容原子写入目标文件（同目录临时文件 + os.replace）。"""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".boost_list_", suffix=".tmp"
    )
    try:
        # newline="" 保持内容里已有的换行符不被二次转换
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise