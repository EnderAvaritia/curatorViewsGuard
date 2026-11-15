"""守护进程运行状态文件：写入、读取与离线状态描述。

daemon 每次循环把自身状态（PID、阶段、心跳、上次执行结果等）原子写入
数据库同目录的 ``daemon_state.json``；``info`` 子命令只读该文件即可在
不触网、不依赖数据库的情况下判断"守护进程是否在运行"。

本模块只依赖标准库，不导入 daemon.py，避免循环依赖。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    # 首选系统时区数据库（tzdata 包）
    _TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # 精简环境（如未安装 tzdata 的 Windows）回退到固定 UTC+8。
    # Asia/Shanghai 无夏令时，固定偏移在语义上等价。
    _TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 心跳过期阈值：daemon 睡眠步长 SLEEP_STEP=30 秒，连续 3 步无更新视为过期
HEARTBEAT_STALE_SECONDS = 90.0

# 阶段到中文说明的映射（供 info 展示）
PHASE_LABELS: Dict[str, str] = {
    "waiting": "等待下次执行",
    "executing": "正在执行每日流程",
    "stopped": "已停止",
}


def default_state_path(db_path: str) -> str:
    """返回状态文件路径：数据库所在目录下的 daemon_state.json。"""
    return str(Path(db_path).parent / "daemon_state.json")


def now() -> datetime:
    """返回 Asia/Shanghai 当前时间（带时区）。"""
    return datetime.now(_TZ)


def now_iso() -> str:
    """返回 Asia/Shanghai 当前时间（ISO-8601，秒级精度）。"""
    return now().isoformat(timespec="seconds")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """解析 ISO-8601 时间戳；无时区视为 Asia/Shanghai，非法返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_TZ)
    return parsed


def date_ts_to_iso(date_ts: int) -> str:
    """把当日零点 Unix 时间戳转为 Asia/Shanghai 的 YYYY-MM-DD。"""
    return datetime.fromtimestamp(int(date_ts), tz=_TZ).strftime("%Y-%m-%d")


def read_state(path: str) -> Optional[Dict[str, Any]]:
    """读取状态文件；文件缺失、损坏或不是 JSON 对象时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # ValueError 覆盖 JSONDecodeError；损坏文件不应影响 info 命令
        return None
    return data if isinstance(data, dict) else None


def write_state(path: str, state: Dict[str, Any]) -> None:
    """原子写入状态文件：同目录临时文件 + os.replace。

    写入失败时清理临时文件并向上抛出 OSError，由调用方决定是否忽略
    （daemon 写状态失败不应影响主流程）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def pid_alive(pid: int) -> Optional[bool]:
    """检查进程是否存活。

    Returns:
        True 存活 / False 确定已退出 / None 无法判断。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False

    if os.name != "nt":
        # POSIX：signal 0 只做权限/存在性检查，不会真的发送信号
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OverflowError:
            return None
        except OSError:
            return None
        return True

    # Windows：绝不能调用 os.kill(pid, 0)——在 Windows 上它会终止进程。
    # 改用 OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) 查询句柄。
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    still_active = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information, False, pid
    )
    if not handle:
        # 87 = ERROR_INVALID_PARAMETER：PID 不存在；其余（拒绝访问等）无法判断
        return False if ctypes.get_last_error() == error_invalid_parameter else None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def describe_daemon(
    state: Optional[Dict[str, Any]],
    alive_fn: Optional[Callable[[int], Optional[bool]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """把状态文件内容转换为便于展示的判断结果。

    Args:
        state: read_state 的结果；None 表示从未启动或状态文件不可读。
        alive_fn: PID 存活检查函数（默认 pid_alive，测试可注入）。
        now: 当前时间（默认 Asia/Shanghai 现在，测试可固定）。

    Returns:
        包含 running/detail/pid/phase 及各时间字段的字典。
    """
    now_dt = now or datetime.now(_TZ)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=_TZ)
    alive_check = alive_fn or pid_alive
    result: Dict[str, Any] = {
        "running": False,
        "detail": "",
        "pid": None,
        "phase": None,
        "phase_label": None,
        "started_at": None,
        "updated_at": None,
        "heartbeat_age_seconds": None,
        "last_run_date": None,
        "last_run_at": None,
        "next_wake": None,
        "last_summary": None,
        "last_error": None,
    }

    if state is None:
        result["detail"] = "从未启动（未找到状态文件）"
        return result

    for key in (
        "pid",
        "phase",
        "started_at",
        "updated_at",
        "last_run_date",
        "last_run_at",
        "next_wake",
        "last_summary",
        "last_error",
    ):
        result[key] = state.get(key)

    phase = result["phase"]
    result["phase_label"] = PHASE_LABELS.get(str(phase or ""), "未知")

    updated_at = parse_timestamp(result["updated_at"])
    if updated_at is not None:
        # 时间戳损坏时保持 None，绝不因状态文件内容而崩溃
        result["heartbeat_age_seconds"] = (now_dt - updated_at).total_seconds()

    pid = result["pid"]
    alive: Optional[bool] = None
    if pid is not None:
        try:
            alive = alive_check(pid)
        except Exception:  # noqa: BLE001 - 存活检查失败按无法判断处理
            alive = None

    if alive is True:
        result["running"] = True
        result["detail"] = "运行中"
    elif alive is False:
        result["running"] = False
        if phase == "stopped":
            result["detail"] = "已停止（正常退出）"
        else:
            result["detail"] = "未运行（上次异常退出或进程被杀）"
    else:
        # PID 检查不可用：回退到心跳时间判断
        age = result["heartbeat_age_seconds"]
        if age is not None and age <= HEARTBEAT_STALE_SECONDS:
            result["running"] = True
            result["detail"] = "运行中（PID 检查不可用，心跳正常）"
        else:
            result["running"] = False
            result["detail"] = "未运行（PID 检查不可用，心跳已过期）"

    return result