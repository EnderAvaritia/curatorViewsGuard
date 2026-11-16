"""Steam 半自动登录与 Cookie 导出。

Steam 没有稳定可靠的纯无头登录方式：登录页带图形验证码，且新设备/异地登录
通常还需要 Steam Guard 手机 App 确认，任何自动化填表都极易失败或被风控。
因此本模块不走全自动路线，而是打开一个真实的 Chromium（Playwright），
由人类在页面里完成登录（验证码、手机确认等），程序只负责等待登录成功的
标志 Cookie（steamLoginSecure）出现，然后把 Cookie 导出为 cookie.txt。

为减少重复登录，Chromium 使用持久化用户目录（默认 .steam_profile），
登录态会保存在该目录中，后续运行通常已是登录状态，无需再次输入密码。

依赖 playwright 为可选依赖，模块导入时不会加载它。
"""

import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import requests

from curator_views.core.fetcher import (
    STATS_REGEX,
    STATS_URL,
    USER_AGENT,
    normalize_cookie,
)

# Steam 登录页地址
LOGIN_URL = "https://store.steampowered.com/login/"

# Playwright 持久化浏览器用户目录（相对配置文件目录），保存登录态
DEFAULT_PROFILE_DIR = ".steam_profile"

# 登录成功的标志 Cookie 名称
_LOGIN_COOKIE_NAME = "steamLoginSecure"


def build_cookie_string(cookies: Iterable[Dict[str, Any]]) -> str:
    """把 Playwright 返回的 Cookie 字典列表转成请求头用的 Cookie 字符串。

    只保留 domain 含 ``steampowered.com`` 的 Cookie；跳过名或值为空的项；
    同名 Cookie 保留最后一次出现的值；输出顺序按名称首次出现的先后。
    绝不修改 ``name=value`` 内容。

    Args:
        cookies: Playwright ``context.cookies()`` 返回的字典序列，
            每项至少含 ``name`` / ``value`` / ``domain``。

    Returns:
        形如 ``"a=1; b=2"`` 的字符串；没有可用 Cookie 时返回空串。
    """
    latest: Dict[str, str] = {}
    for item in cookies:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "")
        if not name or not value:
            continue
        if "steampowered.com" not in domain:
            continue
        # dict 重复赋值保留首次插入位置，值更新为最后一次出现
        latest[name] = value
    return "; ".join(f"{name}={value}" for name, value in latest.items())


def _cookie_domain_priority(domain: str) -> int:
    """返回 Cookie 域在 store 优先策略下的优先级（数值越大越优先）。"""
    if "store.steampowered.com" in domain:
        return 3
    if domain in (".steampowered.com", "steampowered.com"):
        return 2
    return 1


def build_store_cookie_string(cookies: Iterable[Dict[str, Any]]) -> str:
    """按 store 域优先导出 Cookie 字符串。

    自 2024 年起 Steam 会按域下发不同的访问令牌，同名 ``steamLoginSecure``
    在 store / login / community 等域下的值可能并不相同。鉴赏家后台
    ``curator/{id}/admin/*`` 需要的是 store 作用域的令牌，若像
    :func:`build_cookie_string` 那样只按“最后一次出现”取值，可能取到
    login 或 community 域的令牌，导致后台访问被重定向。

    因此本函数对每个 Cookie 名称按域优先级择值：``store.steampowered.com``
    最高，其次 ``.steampowered.com`` / ``steampowered.com``，最后是其他
    ``*steampowered.com``（login / help / checkout 等）；同优先级内仍保留
    最后一次出现的值。只保留 domain 含 ``steampowered.com`` 的 Cookie，
    跳过名或值为空的项，输出顺序按名称首次出现的先后，绝不修改值内容。

    Args:
        cookies: Playwright ``context.cookies()`` 返回的字典序列，
            每项至少含 ``name`` / ``value`` / ``domain``。

    Returns:
        形如 ``"a=1; b=2"`` 的字符串；没有可用 Cookie 时返回空串。
    """
    best: Dict[str, tuple[int, str]] = {}
    for item in cookies:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "")
        if not name or not value:
            continue
        if "steampowered.com" not in domain:
            continue
        priority = _cookie_domain_priority(domain)
        current = best.get(name)
        # dict 重复赋值保留首次插入位置，同优先级时值更新为最后一次出现
        if current is None or priority >= current[0]:
            best[name] = (priority, value)
    return "; ".join(f"{name}={value}" for name, (_, value) in best.items())


@dataclass
class CookieCheckResult:
    """Cookie 有效性校验结果。"""

    valid: bool
    reason: str
    status: Optional[int]
    final_url: str

    def to_dict(self) -> Dict[str, Any]:
        """转换为便于 JSON 序列化的字典。"""
        return asdict(self)


def validate_cookie(
    cookie: str,
    clan_id: str,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
) -> CookieCheckResult:
    """校验 Cookie 是否能访问鉴赏家后台统计页。

    判定顺序与 fetcher 保持一致：Cookie 为空、HTTP 非 200、被重定向到公开页、
    页面中找不到 ``g_StatsAppViews`` 均视为无效。任何网络异常都转为无效结果，
    本函数不抛出异常，也绝不记录或返回 Cookie 内容。

    Args:
        cookie: 待校验的 Cookie 字符串。
        clan_id: Steam 鉴赏家组 ID。
        proxy: 可选代理地址，如 ``http://127.0.0.1:10279``。
        timeout: 单次请求超时时间（秒）。

    Returns:
        包含是否有效、原因、HTTP 状态码与最终 URL 的校验结果。
    """
    if not cookie or not cookie.strip():
        return CookieCheckResult(False, "Cookie 为空", None, "")

    url = STATS_URL.format(clan_id=clan_id)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Cookie": cookie})
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    try:
        response = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return CookieCheckResult(False, f"请求失败：{exc}", None, "")

    status = response.status_code
    final_url = response.url
    if status != 200:
        return CookieCheckResult(False, f"HTTP {status}", status, final_url)
    if final_url != url or "/admin/" not in final_url:
        return CookieCheckResult(
            False,
            "请求被重定向到公开页面（Cookie 可能已失效或未登录）",
            status,
            final_url,
        )
    if STATS_REGEX.search(response.text) is None:
        return CookieCheckResult(
            False, "页面中未找到 g_StatsAppViews 数据", status, final_url
        )
    return CookieCheckResult(True, "Cookie 有效", status, final_url)


def login_via_browser(
    profile_dir: str,
    timeout_s: float = 300.0,
    headless: bool = False,
    proxy: Optional[str] = None,
    poll_interval: float = 1.0,
    launcher: Optional[Callable[[], Any]] = None,
) -> str:
    """打开 Chromium 由人类完成登录，并导出 Steam 域下的 Cookie。

    使用持久化用户目录复用登录态；轮询直至出现非空的 ``steamLoginSecure``
    Cookie 或超时。无论成功、超时还是异常，都会关闭浏览器上下文与 Playwright。

    Args:
        profile_dir: Chromium 持久化用户目录。
        timeout_s: 等待登录完成的超时秒数。
        headless: 是否无界面运行（无界面通常无法完成验证码/手机确认）。
        proxy: 可选代理地址。
        poll_interval: 轮询 Cookie 的间隔秒数。
        launcher: 可注入的 Playwright 启动器，默认 ``sync_playwright``，
            测试可传入伪造实现以避免真实浏览器依赖。

    Returns:
        形如 ``"name=value; ..."`` 的 Cookie 字符串。

    Raises:
        RuntimeError: 未安装 playwright、Chromium 启动失败、登录超时，
            或登录成功但没有取到任何 Steam Cookie。
    """
    if launcher is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "未安装 playwright。请运行：pip install playwright && "
                "playwright install chromium"
            ) from exc
        launcher = sync_playwright

    playwright = launcher()
    # sync_playwright() 返回的是上下文管理器而非 Playwright 本体，
    # 测试注入的伪造启动器通常直接返回可用对象，这里统一处理两种情况。
    if not hasattr(playwright, "chromium") and hasattr(playwright, "start"):
        playwright = playwright.start()

    context = None
    try:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                proxy={"server": proxy} if proxy else None,
            )
        except Exception as exc:  # noqa: BLE001 - 包装为可读提示
            raise RuntimeError(
                "Chromium 启动失败，请先运行：playwright install chromium"
                f"（原始错误：{exc}）"
            ) from exc

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        deadline = time.monotonic() + timeout_s
        cookies: list = []
        while True:
            cookies = context.cookies()
            logged_in = any(
                item.get("name") == _LOGIN_COOKIE_NAME and item.get("value")
                for item in cookies
            )
            if logged_in:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"登录超时（{timeout_s:.0f} 秒内未检测到登录 Cookie）。"
                )
            time.sleep(poll_interval)

        cookie = build_cookie_string(cookies)
        if not cookie:
            raise RuntimeError("登录成功但未取到任何 steampowered.com Cookie")
        return cookie
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - 清理阶段不掩盖原始异常
                pass
        try:
            playwright.stop()
        except Exception:  # noqa: BLE001 - 清理阶段不掩盖原始异常
            pass


def write_cookie_file(cookie_file: str, cookie: str) -> None:
    """原子写入 Cookie 文件（同目录临时文件 + os.replace）。

    先写同目录临时文件再替换目标文件，避免中途失败留下半截 Cookie，
    也避免 Windows 下跨盘替换失败。

    Args:
        cookie_file: 目标 Cookie 文件路径。
        cookie: 已规范化的 Cookie 字符串。
    """
    path = Path(cookie_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".cookie-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(cookie + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise