"""login 模块测试：Cookie 规范化、导出、有效性校验与浏览器登录流程。

全部使用伪造对象与 monkeypatch，不发起任何真实网络请求，
也不要求安装 playwright。
"""

from typing import Optional

import pytest

from curator_views.core import login


class FakeResponse:
    """伪造 requests 响应对象。"""

    def __init__(self, status_code: int, url: str, text: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.text = text


class FakeSession:
    """伪造 requests.Session，记录调用但绝不联网。"""

    response: Optional[FakeResponse] = None
    last_instance: Optional["FakeSession"] = None

    def __init__(self) -> None:
        self.headers = {}
        self.proxies = {}
        self.get_urls = []
        FakeSession.last_instance = self

    def get(self, url: str, timeout: Optional[float] = None) -> FakeResponse:
        self.get_urls.append(url)
        assert FakeSession.response is not None
        return FakeSession.response


def patch_session(monkeypatch, response: Optional[FakeResponse]) -> None:
    """把 login.requests.Session 替换为 FakeSession。"""
    FakeSession.response = response
    FakeSession.last_instance = None
    monkeypatch.setattr(login.requests, "Session", FakeSession)


class FakePage:
    """伪造 Playwright 页面。"""

    def __init__(self) -> None:
        self.goto_urls = []

    def goto(self, url: str, wait_until: Optional[str] = None) -> None:
        self.goto_urls.append(url)


class FakeContext:
    """伪造 Playwright 浏览器上下文。"""

    def __init__(self, cookies: list) -> None:
        self._cookies = cookies
        self.pages = []
        self.closed = False

    def new_page(self) -> FakePage:
        return FakePage()

    def cookies(self) -> list:
        return self._cookies

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    """伪造 Playwright chromium 分支。"""

    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self.launch_kwargs = {}

    def launch_persistent_context(self, **kwargs) -> FakeContext:
        self.launch_kwargs = kwargs
        return self._context


class FakePlaywright:
    """伪造 Playwright 对象（可直接访问 chromium / stop）。"""

    def __init__(self, context: FakeContext) -> None:
        self.chromium = FakeChromium(context)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_normalize_cookie_newlines_become_semicolons() -> None:
    """换行分隔的 Cookie 片段应合并为 '; ' 连接。"""
    raw = "a=1\nb=2\n\nc=3"
    assert login.normalize_cookie(raw) == "a=1; b=2; c=3"


def test_normalize_cookie_strips_prefix_and_trailing_semicolon() -> None:
    """去掉 Cookie: 前缀与结尾分号，保留 name=value 原内容。"""
    raw = "  Cookie: a=1; b=2;  "
    assert login.normalize_cookie(raw) == "a=1; b=2"


def test_normalize_cookie_whitespace_only_is_empty() -> None:
    """纯空白输入返回空串。"""
    assert login.normalize_cookie("   \n\t ") == ""


def test_normalize_cookie_normal_input_unchanged() -> None:
    """已规范的输入保持不变。"""
    assert login.normalize_cookie("a=1; b=2") == "a=1; b=2"


def test_build_cookie_string_filters_dedupes_and_orders() -> None:
    """只保留 Steam 域，同名保留最后一次，空值跳过，顺序按首次出现。"""
    cookies = [
        {"name": "a", "value": "1", "domain": ".steampowered.com"},
        {"name": "b", "value": "2", "domain": "store.steampowered.com"},
        {"name": "other", "value": "x", "domain": "example.com"},
        {"name": "a", "value": "9", "domain": ".steampowered.com"},
        {"name": "empty", "value": "", "domain": ".steampowered.com"},
    ]
    assert login.build_cookie_string(cookies) == "a=9; b=2"


def test_build_store_cookie_string_store_domain_wins() -> None:
    """store 域的 steamLoginSecure 胜过 community / login 副本，值原样保留。"""
    cookies = [
        {
            "name": "steamLoginSecure",
            "value": "community-token",
            "domain": "steamcommunity.com",
        },
        {
            "name": "steamLoginSecure",
            "value": "login-token",
            "domain": "login.steampowered.com",
        },
        {
            "name": "steamLoginSecure",
            "value": "store-token",
            "domain": "store.steampowered.com",
        },
    ]
    assert login.build_store_cookie_string(cookies) == "steamLoginSecure=store-token"


def test_build_store_cookie_string_keeps_non_store_and_drops_community() -> None:
    """保留 steamCountry / sessionid 等非 store 域 Cookie，排除 community 域。"""
    cookies = [
        {
            "name": "steamLoginSecure",
            "value": "store-token",
            "domain": "store.steampowered.com",
        },
        {
            "name": "steamCountry",
            "value": "CN%7Cabc",
            "domain": "store.steampowered.com",
        },
        {"name": "sessionid", "value": "s1", "domain": "login.steampowered.com"},
        {
            "name": "steamLoginSecure",
            "value": "community-token",
            "domain": "steamcommunity.com",
        },
        {
            "name": "steamCountry",
            "value": "community-country",
            "domain": "steamcommunity.com",
        },
    ]
    assert (
        login.build_store_cookie_string(cookies)
        == "steamLoginSecure=store-token; steamCountry=CN%7Cabc; sessionid=s1"
    )


def test_build_store_cookie_string_root_domain_beats_login() -> None:
    """没有 store 副本时，.steampowered.com 优先于 login.steampowered.com。"""
    cookies = [
        {
            "name": "steamLoginSecure",
            "value": "login-token",
            "domain": "login.steampowered.com",
        },
        {
            "name": "steamLoginSecure",
            "value": "root-token",
            "domain": ".steampowered.com",
        },
    ]
    assert login.build_store_cookie_string(cookies) == "steamLoginSecure=root-token"


def test_build_store_cookie_string_preserves_first_occurrence_order() -> None:
    """输出顺序按名称首次出现，同优先级重复出现时最后一次的值生效。"""
    cookies = [
        {"name": "b", "value": "1", "domain": "login.steampowered.com"},
        {"name": "a", "value": "2", "domain": "store.steampowered.com"},
        {"name": "b", "value": "3", "domain": "store.steampowered.com"},
        {"name": "b", "value": "4", "domain": "store.steampowered.com"},
    ]
    assert login.build_store_cookie_string(cookies) == "b=4; a=2"


def test_build_store_cookie_string_empty_returns_empty() -> None:
    """空输入返回空串。"""
    assert login.build_store_cookie_string([]) == ""


def test_validate_cookie_valid(monkeypatch) -> None:
    """200 且页面含 g_StatsAppViews 时判定有效。"""
    url = login.STATS_URL.format(clan_id="123")
    patch_session(
        monkeypatch,
        FakeResponse(200, url, 'g_StatsAppViews = [{"date":1,"num_views":2}];'),
    )
    result = login.validate_cookie("a=1", "123")
    assert result.valid is True
    assert result.reason == "Cookie 有效"


def test_validate_cookie_redirected(monkeypatch) -> None:
    """被重定向到公开 curator 页时判定无效。"""
    url = login.STATS_URL.format(clan_id="123")
    patch_session(
        monkeypatch,
        FakeResponse(200, "https://store.steampowered.com/curator/123/", ""),
    )
    result = login.validate_cookie("a=1", "123")
    assert result.valid is False
    assert "重定向" in result.reason
    assert result.final_url != url


def test_validate_cookie_http_error(monkeypatch) -> None:
    """HTTP 非 200 时判定无效并带上状态码。"""
    url = login.STATS_URL.format(clan_id="123")
    patch_session(monkeypatch, FakeResponse(500, url, ""))
    result = login.validate_cookie("a=1", "123")
    assert result.valid is False
    assert "HTTP 500" in result.reason


def test_validate_cookie_empty_skips_network(monkeypatch) -> None:
    """Cookie 为空时直接返回无效，不创建 Session、不发请求。"""
    patch_session(monkeypatch, None)
    result = login.validate_cookie("   ", "123")
    assert result.valid is False
    assert result.reason == "Cookie 为空"
    assert FakeSession.last_instance is None


def test_login_via_browser_timeout_closes_context() -> None:
    """始终取不到登录 Cookie 时超时，并关闭上下文与 Playwright。"""
    context = FakeContext([])
    playwright = FakePlaywright(context)
    with pytest.raises(RuntimeError, match="登录超时"):
        login.login_via_browser(
            "profile",
            timeout_s=0.01,
            poll_interval=0.001,
            launcher=lambda: playwright,
        )
    assert context.closed is True
    assert playwright.stopped is True


def test_login_via_browser_success_returns_steam_cookies() -> None:
    """出现 steamLoginSecure 后导出 Steam 域 Cookie，丢弃其他域。"""
    context = FakeContext(
        [
            {
                "name": "steamLoginSecure",
                "value": "abc",
                "domain": "store.steampowered.com",
            },
            {
                "name": "sessionid",
                "value": "s1",
                "domain": "store.steampowered.com",
            },
            {"name": "other", "value": "x", "domain": "example.com"},
        ]
    )
    playwright = FakePlaywright(context)
    cookie = login.login_via_browser(
        "profile",
        timeout_s=1.0,
        poll_interval=0.001,
        launcher=lambda: playwright,
    )
    assert cookie == "steamLoginSecure=abc; sessionid=s1"
    assert context.closed is True


def test_build_parser_registers_login() -> None:
    """login 子命令已注册，默认值与 --validate-only 解析正确。"""
    from curator_views import cli

    args = cli.build_parser().parse_args(
        ["login", "--config", "x.yaml", "--validate-only"]
    )
    assert args.func is cli.cmd_login
    assert args.validate_only is True
    assert args.timeout == 300.0
    assert args.headless is False