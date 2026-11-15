"""Steam 鉴赏家浏览量系统的数据存储层。

仅依赖 Python 标准库 sqlite3，提供：
- 每日浏览量统计（daily_stats 表）
- 浏览量提升日志（boost_log 表）
- 通用键值状态存储（kv_store 表）
- 每日决策定档（daily_plan 表，供 forecast 首日复用）

线程安全：使用单个连接 + ``check_same_thread=False``，
所有读写操作通过 threading.Lock 串行化保护。
"""

import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional


class Storage:
    """基于 SQLite 的本地存储，封装浏览量数据与运行状态的持久化。"""

    def __init__(self, db_path: str) -> None:
        """打开（或创建）数据库，并确保所需数据表存在。

        Args:
            db_path: SQLite 数据库文件路径。
        """
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        """按需创建 daily_stats / boost_log / kv_store / daily_plan 四张表。"""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date_ts INTEGER PRIMARY KEY,
                    num_views INTEGER
                );

                CREATE TABLE IF NOT EXISTS boost_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    count INTEGER,
                    note TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_plan (
                    date TEXT PRIMARY KEY,
                    date_ts INTEGER,
                    base REAL,
                    multiplier REAL,
                    raw_target REAL,
                    target REAL,
                    target_jitter REAL,
                    gap_jitter REAL,
                    deduction REAL,
                    gross_need INTEGER,
                    boost_quota INTEGER,
                    need INTEGER,
                    today_boosted INTEGER,
                    target_bound TEXT,
                    in_cooldown INTEGER,
                    list_status TEXT,
                    list_amount INTEGER,
                    signature TEXT,
                    updated_at TEXT
                );
                """
            )
            self._conn.commit()

    def upsert_daily_stats(self, date_ts: int, num_views: int) -> None:
        """写入或覆盖某一天的浏览量统计。

        Args:
            date_ts: 当日零点对应的 Unix 时间戳。
            num_views: 当日浏览量。
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO daily_stats (date_ts, num_views) "
                "VALUES (?, ?)",
                (date_ts, num_views),
            )
            self._conn.commit()

    def get_daily_stats(self) -> List[Dict]:
        """返回全部每日统计，按 date_ts 升序排列。

        Returns:
            形如 ``[{date_ts, num_views}, ...]`` 的列表。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT date_ts, num_views FROM daily_stats "
                "ORDER BY date_ts ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def log_boost(self, date: str, count: int, note: str = "") -> None:
        """追加一条浏览量提升记录。

        Args:
            date: 提升日期，格式 'YYYY-MM-DD'。
            count: 本次提升的浏览量数量。
            note: 备注信息（默认为空）。
        """
        created_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO boost_log (date, count, note, created_at) "
                "VALUES (?, ?, ?, ?)",
                (date, count, note, created_at),
            )
            self._conn.commit()

    def get_boost_total(self, date: str) -> int:
        """返回指定日期全部提升记录的数量总和。

        Args:
            date: 日期，格式 'YYYY-MM-DD'。

        Returns:
            该日期所有提升数量之和；无记录时为 0。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS total "
                "FROM boost_log WHERE date = ?",
                (date,),
            ).fetchone()
        return int(row["total"])

    def set_boost_total(self, date: str, count: int) -> None:
        """手动覆盖指定日期的提升总数。

        删除该日期的全部提升记录，并替换为一条
        ``note='manual'`` 的汇总记录。

        Args:
            date: 日期，格式 'YYYY-MM-DD'。
            count: 手动指定的提升总数。
        """
        created_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "DELETE FROM boost_log WHERE date = ?", (date,)
            )
            self._conn.execute(
                "INSERT INTO boost_log (date, count, note, created_at) "
                "VALUES (?, ?, ?, ?)",
                (date, count, "manual", created_at),
            )
            self._conn.commit()

    def get_boost_logs(self, date: str) -> List[Dict]:
        """返回指定日期的全部提升记录。

        Args:
            date: 日期，格式 'YYYY-MM-DD'。

        Returns:
            形如 ``[{id, date, count, note, created_at}, ...]`` 的列表。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, date, count, note, created_at "
                "FROM boost_log WHERE date = ? ORDER BY id ASC",
                (date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_state(self, key: str, value: str) -> None:
        """保存一条键值状态。

        存储层保持简单，只存取字符串；
        复杂结构请由调用方先 JSON 序列化后再传入。

        Args:
            key: 状态键。
            value: 状态值（字符串）。
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, value) "
                "VALUES (?, ?)",
                (key, value),
            )
            self._conn.commit()

    def load_state(self, key: str, default: str = "") -> str:
        """读取一条键值状态，键不存在时返回默认值。

        Args:
            key: 状态键。
            default: 键不存在时的返回值。

        Returns:
            存储的状态字符串，或默认值。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else default

    _DAILY_PLAN_COLUMNS = (
        "date",
        "date_ts",
        "base",
        "multiplier",
        "raw_target",
        "target",
        "target_jitter",
        "gap_jitter",
        "deduction",
        "gross_need",
        "boost_quota",
        "need",
        "today_boosted",
        "target_bound",
        "in_cooldown",
        "list_status",
        "list_amount",
        "signature",
        "updated_at",
    )

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> Dict:
        """把 daily_plan 行转换为字典，并恢复整数/布尔/可空字段类型。"""
        data = dict(row)
        for key in (
            "date_ts", "gross_need", "boost_quota", "need", "today_boosted",
        ):
            if data.get(key) is not None:
                data[key] = int(data[key])
        data["in_cooldown"] = bool(data.get("in_cooldown"))
        data["list_amount"] = (
            int(data["list_amount"])
            if data.get("list_amount") is not None
            else None
        )
        for key in (
            "base", "multiplier", "raw_target", "target",
            "target_jitter", "gap_jitter", "deduction",
        ):
            if data.get(key) is not None:
                data[key] = float(data[key])
        return data

    def save_daily_plan(self, plan: Dict) -> None:
        """写入或覆盖某一天的每日决策定档（daily_plan 表）。

        只写入预先定义的列，未知键直接忽略；``list_amount`` 允许为 None
        （未启用刷量列表或列表无有效行）。同一天重复调用会覆盖旧行，
        供 daemon 多时段更新 need / today_boosted / updated_at。

        Args:
            plan: 形如 ``{"date": "YYYY-MM-DD", "target": 1234.5, ...}``
                的决策字典。
        """
        columns = self._DAILY_PLAN_COLUMNS
        placeholders = ", ".join("?" for _ in columns)
        values = tuple(plan.get(column) for column in columns)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO daily_plan "
                f"({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            self._conn.commit()

    def get_daily_plan(self, date: str) -> Optional[Dict]:
        """读取指定日期的每日决策定档。

        Args:
            date: 日期，格式 'YYYY-MM-DD'。

        Returns:
            定档字典；该日期没有记录时返回 None。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daily_plan WHERE date = ?", (date,)
            ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def get_daily_plans(self) -> List[Dict]:
        """返回全部每日决策定档，按 date 升序排列。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM daily_plan ORDER BY date ASC"
            ).fetchall()
        return [self._plan_from_row(row) for row in rows]
