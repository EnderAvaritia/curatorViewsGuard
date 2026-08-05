"""Steam 鉴赏家数据抓取层。

通过登录 Cookie 访问 Steam 鉴赏家后台，提供：
- 每日浏览量统计（fetch_stats）
- 最近推荐游戏列表（fetch_recent_recommendations）

依赖：requests、beautifulsoup4。
"""

import json
import re
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

# 鉴赏家后台统计页 URL（需登录 Cookie）
STATS_URL = "https://store.steampowered.com/curator/{clan_id}/admin/overview"

# 鉴赏家推荐列表 ajax 接口 URL
RECOMMENDATIONS_URL = (
    "https://store.steampowered.com/curator/{clan_id}/"
    "ajaxgetfilteredrecommendations/"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) "
    "Gecko/20100101 Firefox/138.0"
)

# 匹配页面内 JS 变量 g_StatsAppViews = [...]（约 364 天的每日浏览量）
STATS_REGEX = re.compile(r"g_StatsAppViews\s*=\s*(\[.*?\]);", re.S)

# 推荐列表接口的固定查询参数（count=300 保证取到足够多的近期推荐）
RECOMMENDATION_PARAMS = {
    "query": "",
    "start": 0,
    "count": "300",
    "dynamic_data": "",
    "tagids": "",
    "sort": "recent",
    "app_types": "",
    "curations": "",
    "reset": "false",
}


class Fetcher:
    """Steam 鉴赏家数据抓取器，负责统计与推荐列表的获取。"""

    def __init__(
        self,
        clan_id: str,
        cookie_file: str,
        proxy: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        """初始化抓取器。

        Args:
            clan_id: Steam 鉴赏家组 ID。
            cookie_file: 存放登录 Cookie 的文本文件路径。
            proxy: 可选代理地址，如 http://127.0.0.1:10279。
            timeout: 单次请求超时时间（秒），默认 30 秒。
        """
        self._clan_id = clan_id
        self._cookie_file = cookie_file
        self._proxy = proxy
        self._timeout = timeout
        self._session = self._make_session()

    def _load_cookie(self) -> str:
        """读取 Cookie 文件并去除首尾空白。"""
        with open(self._cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()

    def _make_session(self) -> requests.Session:
        """构造带 Cookie 与可选代理的 requests.Session。"""
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Cookie": self._load_cookie(),
            }
        )
        if self._proxy:
            session.proxies.update(
                {"http": self._proxy, "https": self._proxy}
            )
        return session

    def fetch_stats(self) -> List[Dict]:
        """抓取鉴赏家后台的每日浏览量统计。

        统计页 HTML 内含 JS 变量 ``g_StatsAppViews``，存放约 364 天
        的每日统计，每条形如 ``{"date": ..., "num_views": ...}``。

        Returns:
            形如 ``[{date_ts: int, num_views: int}, ...]`` 的列表，
            ``date_ts`` 为 Unix 时间戳。

        Raises:
            RuntimeError: HTTP 状态非 200，或页面中未找到 /
                无法解析 g_StatsAppViews 数据。
        """
        url = STATS_URL.format(clan_id=self._clan_id)
        response = self._session.get(url, timeout=self._timeout)
        if response.status_code != 200:
            raise RuntimeError(
                f"获取浏览量统计失败：HTTP {response.status_code}"
            )
        match = STATS_REGEX.search(response.text)
        if match is None:
            # 区分“未登录/重定向到公开页”与“页面结构改变”
            if response.url != url or "/admin/" not in response.url:
                hint = (
                    "请求被重定向到公开页面（可能 Cookie 已失效或未登录），"
                    "请更新 cookie.txt 中的 Steam 登录 Cookie"
                )
            else:
                hint = "可能未登录或页面结构已改变"
            raise RuntimeError(
                "页面中未找到 g_StatsAppViews 数据，" + hint
            )
        try:
            raw_stats = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"解析 g_StatsAppViews 数据失败：{exc}") from exc

        stats: List[Dict] = []
        for entry in raw_stats:
            if isinstance(entry, dict):
                stats.append(
                    {
                        "date_ts": int(entry["date"]),
                        "num_views": int(entry["num_views"]),
                    }
                )
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                stats.append(
                    {"date_ts": int(entry[0]), "num_views": int(entry[1])}
                )
            else:
                raise RuntimeError(f"无法识别的浏览量统计条目：{entry!r}")
        return stats

    def fetch_recent_recommendations(self, n: int = 5) -> List[Dict]:
        """获取鉴赏家最近推荐的游戏列表。

        接口通常返回 HTML，也可能返回 ``{"success": false}`` 形式的
        失败 JSON；两种情形都需处理。按最近推荐排序后取前 n 条。

        Args:
            n: 需要返回的推荐数量（必须大于 0）。

        Returns:
            形如 ``[{app_id: str, name: str}, ...]`` 的列表。

        Raises:
            RuntimeError: n 为 0、接口返回失败 JSON、HTTP 状态非 200、
                或页面中未解析到任何游戏链接。
        """
        if n <= 0:
            raise RuntimeError("请求的推荐数量 n 必须大于 0")
        url = RECOMMENDATIONS_URL.format(clan_id=self._clan_id)
        response = self._session.get(
            url, params=RECOMMENDATION_PARAMS, timeout=self._timeout
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"获取推荐列表失败：HTTP {response.status_code}"
            )
        text = response.text
        content_type = response.headers.get("Content-Type", "").lower()
        # 响应可能以 JSON 形式返回 {"success": false}，需提前识别并报错
        if "json" in content_type or text.lstrip().startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("success") is False:
                raise RuntimeError("推荐列表获取失败")

        soup = BeautifulSoup(text, "html.parser")
        # 解开外层两层 div，使推荐链接提升到可直接查找的层级
        for _ in range(2):
            outer = soup.find("div")
            if outer is not None:
                outer.unwrap()
        links = soup.find_all("a", attrs={"data-ds-appid": True})
        if not links:
            raise RuntimeError("推荐列表中没有找到任何游戏链接")

        results: List[Dict] = []
        for link in links[:n]:
            app_id = str(link["data-ds-appid"]).replace('\\"', "")
            results.append(
                {"app_id": app_id, "name": self._extract_game_name(link)}
            )
        return results

    @staticmethod
    def _extract_game_name(link: Tag) -> str:
        """从推荐链接中提取游戏名称。

        优先取链接文本，其次取内部 img 的 alt / title 属性。

        Args:
            link: 带有 data-ds-appid 属性的 <a> 标签。

        Returns:
            游戏名称；无法提取时返回空字符串。
        """
        text = link.get_text(strip=True)
        if text:
            return text
        img = link.find("img")
        if img is not None:
            alt = img.get("alt")
            if alt:
                return str(alt).strip()
            title = img.get("title")
            if title:
                return str(title).strip()
        return ""
