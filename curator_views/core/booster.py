"""受控浏览量提升执行器（Booster）。

设计目标：以尽可能平滑的方式提升 Steam 鉴赏家评测浏览量，
避免触发 Steam 反机器人机制。关键约束：
- 分批、限速：批次之间及批内请求之间随机等待 wait_min ~ wait_max 秒；
- 单 App 上限：每个推荐游戏单日最多成功 single_cap 次，达到后跳过；
- 分配方式（distribution）：
  * ratio（默认）：累计成功达到 target_count * target_ratio（默认 95%）
    即提前停止，宁可略欠不可超额（超额会制造尖峰风险）；
  * exact：按推荐条数精确分配配额（如目标 500、30 条推荐 → 前 20 条各
    17 次 + 后 10 条各 16 次），全部配额刷满即恰好达成目标，不多不少；
- 失败中止：连续请求失败达到 max_retries 时中止整个任务并返回部分结果。

批次循环：批大小为 batch_size，所有 App 用尽后从头再切（循环批次），
直到预算/目标达成、全部 App 达到单 App 上限、或触发失败中止。

参考脚本 steam_curator_views.py 中的重试封装与 BeautifulSoup 标题提取
仅作为设计参考，本模块为全新实现，不依赖该脚本。
"""

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

from .fetcher import Fetcher, read_cookie_file
from .storage import Storage

logger = logging.getLogger("curator_views.booster")

try:
    # 首选系统时区数据库（tzdata 包）
    _TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # 精简环境（如未安装 tzdata 的 Windows）回退到固定 UTC+8。
    # Asia/Shanghai 无夏令时，固定偏移在语义上等价。
    _TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 游戏页面 URL 模板：带上 curator_clanid 参数才能计入鉴赏家浏览量
APP_PAGE_URL = "https://store.steampowered.com/app/{app_id}/?curator_clanid={clan_id}"

REQUEST_TIMEOUT = 30  # 秒

# 重试之间的退避等待区间（秒），与批次间的 wait_min/wait_max 相互独立
RETRY_WAIT_MIN = 3.0
RETRY_WAIT_MAX = 8.0

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) "
        "Gecko/20100101 Firefox/138.0"
    ),
    "Accept-Language": (
        "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2"
    ),
    "DNT": "1",
}


def _today_str() -> str:
    """返回 Asia/Shanghai 时区的今天日期 (YYYY-MM-DD)。"""
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _cyclic_chunks(items: List[str], size: int) -> Iterable[List[str]]:
    """循环切分列表：所有批次用尽后从头再切，直到调用方主动停止。

    与单次切分不同，这里允许同一批 App 在后续循环中再次出现，
    使单个 App 可以在多轮批次中累积访问次数（受单 App 上限约束）。
    """
    if not items:
        return
    while True:
        for start in range(0, len(items), size):
            yield items[start : start + size]


class Booster:
    """受控浏览量提升执行器。

    Args:
        clan_id: Steam 鉴赏家组 ID。
        cookie_file: Steam cookie 文件路径（用于登录态访问，不打印内容）。
        proxy: 代理地址（可选）。
        storage: 数据存储实例，用于记录每次提升（log_boost）。
        batch_size: 每批处理的游戏数量。
        single_cap: 单个游戏单日成功次数上限。
        threads: 并发线程数，<=1 为顺序执行（推荐，计数确定）；>1 使用线程池，
            线程池大小即配置值，没有内置上限。
        wait_min / wait_max: 两次请求之间的随机等待区间（秒）。
        max_retries: 连续请求失败多少次后中止整个任务。
        target_ratio: 累计成功达到目标值的该比例后提前停止（ratio 模式）。
        distribution: 提升分配方式，"ratio"（默认，按 target_ratio 提前停）
            或 "exact"（按推荐条数分配配额，恰好刷满目标）。
    """

    def __init__(
        self,
        clan_id: str,
        cookie_file: str,
        proxy: Optional[str],
        storage: Storage,
        batch_size: int = 50,
        single_cap: int = 300,
        threads: int = 1,
        wait_min: float = 8.0,
        wait_max: float = 16.0,
        max_retries: int = 3,
        target_ratio: float = 0.95,
        distribution: str = "ratio",
    ) -> None:
        self.clan_id = clan_id
        self.cookie_file = cookie_file
        self.proxy = proxy
        self.storage = storage
        self.batch_size = batch_size
        self.single_cap = single_cap
        self.threads = threads
        self.wait_min = wait_min
        self.wait_max = wait_max
        self.max_retries = max_retries
        self.target_ratio = target_ratio
        self.distribution = str(distribution).lower()

        # 最近一次 run() 是否因连续失败而中止（供调度器判断通知级别）
        self.aborted = False

        # 每个线程独立的 requests.Session
        self._local = threading.local()

    # ── 对外接口 ──────────────────────────────────────────────────────────

    def run(
        self,
        target_count: int,
        n_recommendations: int = 5,
        dry_run: bool = False,
    ) -> int:
        """执行一轮受控浏览量提升，返回实际提升成功的次数。

        Args:
            target_count: 本轮目标提升次数。
            n_recommendations: 只提升最近 N 条鉴赏家推荐（旧的不计）。
            dry_run: 为 True 时不发送任何请求，仅记录计划并返回 0。

        流程：
        1. 通过 Fetcher 获取最近 n_recommendations 条推荐；
        2. 计算预算 budget = min(target_count, n * single_cap)；
        3. 按 batch_size 循环分批访问游戏页面；
        4. 每批结束记录提升日志（含中止时也记录实际成功数）；
        5. 累计达到 target_count * target_ratio 提前停止；
        6. 连续失败达到 max_retries 时中止并返回部分结果。
        """
        self.aborted = False
        today = _today_str()

        fetcher = Fetcher(self.clan_id, self.cookie_file, self.proxy)
        try:
            recommendations = fetcher.fetch_recent_recommendations(
                n=n_recommendations
            )
        except Exception as exc:  # noqa: BLE001 - 获取推荐失败直接终止本轮
            logger.error("获取最近推荐失败，本轮提升终止: %s", exc)
            return 0

        app_ids = [rec["app_id"] for rec in recommendations]
        budget = min(target_count, len(app_ids) * self.single_cap)

        # exact 模式：按推荐条数精确分配配额（目标 500、30 条推荐 → 前 20 条
        # 各 17 次 + 后 10 条各 16 次），全部配额刷满即恰好达成目标。
        per_app_quota: Optional[Dict[str, int]] = None
        if self.distribution == "exact" and app_ids:
            base, extra = divmod(target_count, len(app_ids))
            per_app_quota = {
                app_id: min(base + (1 if idx < extra else 0), self.single_cap)
                for idx, app_id in enumerate(app_ids)
            }
            logger.info(
                "exact 分配: 目标=%d 条数=%d 基准配额=%d 余数=%d 总配额=%d",
                target_count, len(app_ids), base, extra,
                sum(per_app_quota.values()),
            )

        # 提升计划日志（dry_run 同样需要）
        logger.info(
            "提升计划: 目标=%d 预算=%d 单App上限=%d App列表=%s",
            target_count, budget, self.single_cap, app_ids,
        )

        if dry_run:
            logger.info("dry_run 模式: 仅记录计划，不发送任何请求，返回 0。")
            return 0
        if not app_ids or budget <= 0 or target_count <= 0:
            logger.warning("无可提升的推荐或目标/预算为 0，本轮跳过。")
            return 0

        # 每个 App 的成功计数（用于配额 / 单 App 上限约束）
        per_app_success: Dict[str, int] = {app_id: 0 for app_id in app_ids}

        def app_limit(app_id: str) -> int:
            """单个 App 本轮最大成功次数（exact 为配额，否则为单日上限）。"""
            if per_app_quota is not None:
                return per_app_quota[app_id]
            return self.single_cap

        def all_at_limit() -> bool:
            return all(
                per_app_success[app_id] >= app_limit(app_id)
                for app_id in app_ids
            )

        cumulative = 0
        consecutive_failures = 0
        target_line = target_count * self.target_ratio

        for batch_index, batch in enumerate(
            _cyclic_chunks(app_ids, self.batch_size), start=1
        ):
            # 过滤已达配额（或单 App 上限）的游戏
            active = [
                app_id for app_id in batch
                if per_app_success[app_id] < app_limit(app_id)
            ]
            if not active:
                if all_at_limit():
                    logger.info(
                        "全部 App 均达到%s，停止提升。",
                        "分配配额" if per_app_quota is not None else "单日上限",
                    )
                    break
                continue

            batch_success = 0
            try:
                if self.threads > 1:
                    results = self._visit_many_parallel(active)
                    for app_id, ok in results.items():
                        if ok:
                            per_app_success[app_id] += 1
                            batch_success += 1
                    failed_now = sum(1 for ok in results.values() if not ok)
                    if failed_now:
                        consecutive_failures += failed_now
                    else:
                        consecutive_failures = 0
                    if consecutive_failures >= self.max_retries:
                        self.aborted = True
                        logger.error(
                            "连续 %d 次请求失败，中止本轮提升（已成功 %d）。",
                            consecutive_failures, cumulative,
                        )
                else:
                    # 顺序模式：逐 App 访问，精确计数并发失败中止
                    for app_id in active:
                        ok = self._visit_app(app_id)
                        if ok:
                            per_app_success[app_id] += 1
                            batch_success += 1
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                            if consecutive_failures >= self.max_retries:
                                self.aborted = True
                                logger.error(
                                    "连续 %d 次请求失败，中止本轮提升（已成功 %d）。",
                                    consecutive_failures, cumulative,
                                )
                                break
            finally:
                # 无论批次结果如何（成功/中止/异常），都要记录实际成功数
                self.storage.log_boost(
                    today, batch_success, note=f"batch {batch_index}",
                )

            cumulative += batch_success
            logger.info(
                "批次 %d 完成: 成功 %d, 累计 %d/%d",
                batch_index, batch_success, cumulative, target_count,
            )

            if self.aborted:
                break
            if per_app_quota is not None:
                # exact 模式：全部配额刷满即恰好达成目标，无需按比例提前停
                if all_at_limit():
                    logger.info(
                        "exact 分配完成: 累计 %d，全部 %d 条配额已刷满。",
                        cumulative, len(app_ids),
                    )
                    break
            elif cumulative >= target_line:
                logger.info(
                    "累计提升 %d 已达目标 %d 的 %.0f%%，提前停止以避免超额。",
                    cumulative, target_count, self.target_ratio * 100,
                )
                break

        logger.info(
            "提升结束: 实际成功 %d, 中止=%s",
            cumulative, self.aborted,
        )
        return cumulative

    # ── 内部实现 ──────────────────────────────────────────────────────────

    def _visit_many_sequential(self, app_ids: List[str]) -> Dict[str, bool]:
        """顺序访问多个 App 页面（保留给测试与子类复用）。"""
        return {app_id: self._visit_app(app_id) for app_id in app_ids}

    def _visit_many_parallel(self, app_ids: List[str]) -> Dict[str, bool]:
        """并发访问多个 App 页面，返回 {app_id: 是否成功}。"""
        max_workers = max(1, self.threads)
        results: Dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._visit_app, app_id): app_id
                for app_id in app_ids
            }
            for future in as_completed(futures):
                app_id = futures[future]
                try:
                    results[app_id] = future.result()
                except Exception as exc:  # noqa: BLE001 - 线程异常按失败计
                    logger.error("App %s 访问线程异常: %s", app_id, exc)
                    results[app_id] = False
        return results

    def _visit_app(self, app_id: str) -> bool:
        """访问单个游戏页面；HTTP 200 且页面包含 <title> 记为成功。

        无论成功与否，最后都会随机等待 wait_min ~ wait_max 秒再返回，
        保证请求间隔平滑可控。
        """
        session = self._session()
        url = APP_PAGE_URL.format(app_id=app_id, clan_id=self.clan_id)
        try:
            response = self._fetch_with_retry(session, url)
            soup = BeautifulSoup(response.text, "html.parser")
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else None
            if not title:
                logger.warning("App %s: 页面未找到标题，本次不计入成功。", app_id)
                return False
            logger.info("App %s - %s", app_id, title)
            return True
        except requests.RequestException as exc:
            logger.error("App %s: 请求最终失败: %s", app_id, exc)
            return False
        finally:
            time.sleep(random.uniform(self.wait_min, self.wait_max))

    def _fetch_with_retry(
        self, session: requests.Session, url: str,
    ) -> requests.Response:
        """带重试的 GET 请求；重试次数耗尽后抛出最后一次异常。"""
        last_exc: Optional[requests.RequestException] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = session.get(url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "请求失败 (%d/%d), url=%s: %s",
                    attempt, self.max_retries, url, exc,
                )
                if attempt < self.max_retries:
                    time.sleep(random.uniform(RETRY_WAIT_MIN, RETRY_WAIT_MAX))
        if last_exc is not None:
            raise last_exc
        raise requests.RequestException(f"请求失败: {url}")

    def _session(self) -> requests.Session:
        """返回当前线程的 Session（懒创建，含 cookie 与代理）。"""
        session = getattr(self._local, "session", None)
        if session is None:
            headers = dict(DEFAULT_HEADERS)
            cookie = self._load_cookie()
            if cookie:
                headers["Cookie"] = cookie
            session = requests.Session()
            session.headers.update(headers)
            if self.proxy:
                session.proxies.update({"http": self.proxy, "https": self.proxy})
            self._local.session = session
        return session

    def _load_cookie(self) -> str:
        """从 cookie 文件读取 cookie 字符串；未配置/文件缺失仅警告，绝不打印内容。

        Raises:
            CookieFileError: 文件存在但内容损坏（非 UTF-8 / 含非 ASCII 字符）。
        """
        try:
            return read_cookie_file(self.cookie_file)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            logger.warning(
                "读取 cookie 文件失败（%s），将尝试匿名访问。", exc,
            )
            return ""
