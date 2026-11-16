"""session_refresh 模块测试：Cookie 解析、JWT 过期时间与刷新流程。

全部使用伪造的 HTTP Session / Response，不发起任何真实网络请求。
"""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest

from curator_views.core import session_refresh as sr


def make_jwt(exp: int) -> str:
    """构造无签名的 base64url JWT，载荷只含 exp。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}."


def login_value(steamid: str, jwt: str) -> str:
    """构造 Steam 的 <steamid>%7C%7C<jwt> Cookie 值。"""
    return f"{steamid}%7C%7C{jwt}"


class FakeCookie:
    """伪造 requests Cookie 对象。"""

    def __init__(self, name: str, value: str, domain: str) -> None:
        self.name = name
        self.value = value
        self.domain = domain


class FakeCookieJar:
    """可迭代的伪造 Cookie jar。"""

    def __init__(self, cookies: Iterable[FakeCookie] = ()) -> None:
        self.cookies: List[FakeCookie] = list(cookies)

    def __iter__(self):
        return iter(self.cookies)

    def extend(self, cookies: Iterable[FakeCookie]) -> None:
        self.cookies.extend(cookies)


class FakeResponse:
    """伪造 requests 响应。"""

    def __init__(
        self,
        payload: Any = None,
        cookies: Iterable[FakeCookie] = (),
        set_cookie: str = "",
    ) -> None:
        self._payload = payload
        self.cookies = FakeCookieJar(cookies)
        self.headers = {"Set-Cookie": set_cookie} if set_cookie else {}

    def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeSession:
    """伪造 requests.Session，按顺序返回预置响应并记录 POST 调用。"""

    def __init__(self, responses: Iterable[Any]) -> None:
        self.headers = {}
        self.proxies = {}
        self.cookies = FakeCookieJar()
        self.posts: List[Tuple[str, Dict[str, Any], Optional[float]]] = []
        self._responses = list(responses)

    def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> FakeResponse:
        self.posts.append((url, dict(data or {}), timeout))
        if not self._responses:
            raise AssertionError(f"未预置响应，收到意外请求：{url}")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.cookies.extend(response.cookies)
        return response


def finalize_payload(steamid: str, **overrides: Any) -> Dict[str, Any]:
    """构造成功的 finalizelogin 响应载荷。"""
    payload: Dict[str, Any] = {
        "steamID": steamid,
        "transfer_info": [
            {
                "url": f"{sr.STORE_ORIGIN}/login/settoken",
                "params": {"nonce": "nonce-1", "auth": "auth-1"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def settoken_response(
    steamid: str, access_jwt: str, sessionid: str
) -> FakeResponse:
    """构造 settoken 成功响应，带商店域 Cookie。"""
    return FakeResponse(
        {"result": 1},
        cookies=[
            FakeCookie(
                sr.STORE_COOKIE_NAME,
                login_value(steamid, access_jwt),
                "store.steampowered.com",
            ),
            FakeCookie("sessionid", sessionid, "store.steampowered.com"),
        ],
    )


def write_cookie(path: Path, header: str) -> None:
    """写入一个带结尾换行的 Cookie 文件。"""
    path.write_text(header + "\n", encoding="utf-8")


def cookie_header(
    steamid: str,
    access_jwt: str,
    refresh_jwt: str,
    *,
    prefix: str = "foo=bar",
    suffix: str = "last=keep",
) -> str:
    """构造包含访问令牌与刷新令牌的 Cookie 头。"""
    return (
        f"{prefix}; "
        f"{sr.STORE_COOKIE_NAME}={login_value(steamid, access_jwt)}; "
        f"sessionid=old-session; "
        f"{sr.REFRESH_COOKIE_NAME}={login_value(steamid, refresh_jwt)}; "
        f"{suffix}"
    )


class TestCookieHeaderHelpers:
    def test_parse_build_roundtrip_and_duplicates(self) -> None:
        """重复名称后者覆盖前者，输出保持首次出现的位置。"""
        header = "a=1; b=2; a=3"
        parsed = sr.parse_cookie_header(header)
        assert parsed == {"a": "3", "b": "2"}
        rebuilt = sr.build_cookie_header(parsed.items())
        assert rebuilt == "a=3; b=2"
        assert sr.parse_cookie_header(rebuilt) == parsed

    def test_parse_empty_and_malformed_parts(self) -> None:
        """空输入与没有等号的片段被忽略。"""
        assert sr.parse_cookie_header("") == {}
        assert sr.parse_cookie_header(" ; broken ; a=1 ; ") == {"a": "1"}

    def test_build_skips_empty_name_and_preserves_order(self) -> None:
        """名称为空的项被跳过，其余按输入顺序拼接。"""
        pairs = [("b", "2"), ("", "x"), ("a", "1")]
        assert sr.build_cookie_header(pairs) == "b=2; a=1"

    def test_extract_refresh_cookie_raw_header(self) -> None:
        """直接从普通 Cookie 头提取刷新令牌。"""
        header = "a=1; steamRefresh_steam=sid%7C%7Cjwt; b=2"
        assert sr.extract_refresh_cookie(header) == "sid%7C%7Cjwt"

    def test_extract_refresh_cookie_cookie_prefix(self) -> None:
        """兼容浏览器复制的 Cookie: 前缀。"""
        header = "Cookie: a=1; steamRefresh_steam=abc"
        assert sr.extract_refresh_cookie(header) == "abc"

    def test_extract_refresh_cookie_missing(self) -> None:
        """缺少刷新令牌时返回空串。"""
        assert sr.extract_refresh_cookie("a=1; b=2") == ""
        assert sr.extract_refresh_cookie("") == ""


class TestSplitRefreshCookie:
    def test_split_encoded_value(self) -> None:
        """URL 编码的 <steamid>%7C%7C<jwt> 拆成 steamid 与 JWT。"""
        assert sr.split_refresh_cookie("76561198000000000%7C%7Cjwt-token") == (
            "76561198000000000",
            "jwt-token",
        )

    def test_split_raw_separator(self) -> None:
        """未编码的 || 同样可以拆分。"""
        assert sr.split_refresh_cookie("123||raw-jwt") == ("123", "raw-jwt")

    def test_split_without_separator_returns_empty_steamid(self) -> None:
        """没有分隔符时整段当作 JWT，steamid 为空串。"""
        assert sr.split_refresh_cookie("jwt-only") == ("", "jwt-only")
        assert sr.split_refresh_cookie("") == ("", "")


class TestJwtExpiry:
    def test_jwt_expires_at_handcrafted_token(self) -> None:
        """可解析无签名 JWT 的 exp。"""
        exp = 1794614400
        token = make_jwt(exp)
        assert sr.jwt_expires_at(token) == exp

    def test_jwt_expires_at_accepts_login_cookie_value(self) -> None:
        """也接受 <steamid>%7C%7C<jwt> 形式的 Cookie 值。"""
        exp = 1794614400
        value = login_value("76561198000000000", make_jwt(exp))
        assert sr.jwt_expires_at(value) == exp

    def test_jwt_expires_at_garbage_returns_zero(self) -> None:
        """任何无法解析的输入都返回 0，不抛异常。"""
        for value in ("", "not-a-jwt", "a.b.c", "a.!!!.c", "a.b"):
            assert sr.jwt_expires_at(value) == 0

    def test_cookie_expires_at_store_cookie(self) -> None:
        """从 steamLoginSecure Cookie 值中读取 exp。"""
        exp = 1794614400
        header = (
            f"steamLoginSecure={login_value('76561198000000000', make_jwt(exp))}; "
            "foo=bar"
        )
        assert sr.cookie_expires_at(header) == exp

    def test_cookie_expires_at_missing_cookie(self) -> None:
        """Cookie 缺失时返回 0。"""
        assert sr.cookie_expires_at("") == 0
        assert sr.cookie_expires_at("foo=bar") == 0
        assert sr.cookie_expires_at("steamLoginSecure=garbage") == 0


class TestMergeStoreCookie:
    def test_replaces_access_and_sessionid_in_place(self) -> None:
        """原位替换访问令牌与 sessionid，保持其余顺序。"""
        header = "a=1; steamLoginSecure=old; sessionid=old-sid; z=9"
        merged = sr.merge_store_cookie(
            header, access_cookie="new-token", sessionid="new-sid"
        )
        assert merged == "a=1; steamLoginSecure=new-token; sessionid=new-sid; z=9"

    def test_appends_missing_cookies(self) -> None:
        """缺失时追加到末尾。"""
        merged = sr.merge_store_cookie(
            "a=1", access_cookie="new-token", sessionid="new-sid"
        )
        assert merged == "a=1; steamLoginSecure=new-token; sessionid=new-sid"

    def test_keeps_existing_sessionid_when_not_provided(self) -> None:
        """未提供 sessionid 时不改动现有值。"""
        merged = sr.merge_store_cookie(
            "a=1; steamLoginSecure=old; sessionid=keep; z=9",
            access_cookie="new-token",
        )
        assert merged == "a=1; steamLoginSecure=new-token; sessionid=keep; z=9"

    def test_empty_access_cookie_raises(self) -> None:
        """access_cookie 为空时抛出 ValueError。"""
        with pytest.raises(ValueError):
            sr.merge_store_cookie("a=1", access_cookie="")


class TestRefreshStoreSession:
    def test_success_posts_expected_payloads_and_merges_cookie(self) -> None:
        """成功流程：精确校验两次 POST 的表单与最终 Cookie。"""
        steamid = "76561198000000000"
        refresh_jwt = make_jwt(1900000000)
        new_access_jwt = make_jwt(1800000000)
        rotated_refresh = login_value(steamid, make_jwt(1900009999))
        original_refresh = login_value(steamid, refresh_jwt)

        finalize = FakeResponse(
            finalize_payload(steamid),
            cookies=[
                FakeCookie(
                    sr.REFRESH_COOKIE_NAME,
                    rotated_refresh,
                    ".login.steampowered.com",
                )
            ],
        )
        settoken = settoken_response(steamid, new_access_jwt, "new-session")
        session = FakeSession([finalize, settoken])

        result = sr.refresh_store_session(
            original_refresh,
            session_factory=lambda: session,
            proxy="http://127.0.0.1:7890",
            user_agent="TestAgent/1.0",
        )

        assert session.headers["User-Agent"] == "TestAgent/1.0"
        assert session.headers["Accept"] == "application/json, text/plain, */*"
        assert session.proxies == {
            "http": "http://127.0.0.1:7890",
            "https": "http://127.0.0.1:7890",
        }

        assert len(session.posts) == 2
        finalize_url, finalize_data, _ = session.posts[0]
        assert finalize_url == f"{sr.LOGIN_ORIGIN}/jwt/finalizelogin"
        assert finalize_data["nonce"] == refresh_jwt
        assert finalize_data["redir"] == sr.STORE_ORIGIN + "/"
        generated_sessionid = finalize_data["sessionid"]
        assert len(generated_sessionid) == 24
        int(generated_sessionid, 16)

        settoken_url, settoken_data, _ = session.posts[1]
        assert settoken_url == f"{sr.STORE_ORIGIN}/login/settoken"
        assert settoken_data == {
            "steamID": steamid,
            "nonce": "nonce-1",
            "auth": "auth-1",
        }

        pairs = sr.parse_cookie_header(result.cookie)
        assert pairs[sr.STORE_COOKIE_NAME] == login_value(
            steamid, new_access_jwt
        )
        assert pairs["sessionid"] == "new-session"
        assert pairs[sr.REFRESH_COOKIE_NAME] == rotated_refresh
        assert result.steamid == steamid
        assert result.refresh_cookie == rotated_refresh
        assert result.expires_at == 1800000000

    @pytest.mark.parametrize(
        "code,needs_login", [(15, True), (5, False)]
    )
    def test_eresult_failure_sets_needs_login(
        self, code: int, needs_login: bool
    ) -> None:
        """终止型 EResult 需要重新登录，其他错误不需要。"""
        session = FakeSession([FakeResponse({"error": code})])
        with pytest.raises(sr.RefreshError) as excinfo:
            sr.refresh_store_session(
                login_value("76561198000000000", make_jwt(1900000000)),
                session_factory=lambda: session,
            )
        assert excinfo.value.eresult == code
        assert excinfo.value.needs_login is needs_login

    def test_missing_transfer_info_falls_back_to_ajaxrefresh(self) -> None:
        """缺少 transfer_info 时走 ajaxrefresh，仍可刷新成功。"""
        steamid = "76561198000000000"
        refresh_value = login_value(steamid, make_jwt(1900000000))
        access_jwt = make_jwt(1800000000)

        finalize = FakeResponse(
            {"steamID": steamid, "transfer_info": []}
        )
        ajax = FakeResponse(
            {
                "success": 1,
                "steamID": steamid,
                "nonce": "nonce-fallback",
                "auth": "auth-fallback",
            }
        )
        settoken = settoken_response(steamid, access_jwt, "fallback-session")
        session = FakeSession([finalize, ajax, settoken])

        result = sr.refresh_store_session(
            refresh_value, session_factory=lambda: session
        )

        assert [post[0] for post in session.posts] == [
            f"{sr.LOGIN_ORIGIN}/jwt/finalizelogin",
            f"{sr.LOGIN_ORIGIN}/jwt/ajaxrefresh",
            f"{sr.STORE_ORIGIN}/login/settoken",
        ]
        assert session.posts[1][1] == {"redir": sr.STORE_ORIGIN + "/"}
        assert session.posts[2][1] == {
            "steamID": steamid,
            "nonce": "nonce-fallback",
            "auth": "auth-fallback",
        }
        pairs = sr.parse_cookie_header(result.cookie)
        assert pairs[sr.STORE_COOKIE_NAME] == login_value(steamid, access_jwt)
        assert pairs["sessionid"] == "fallback-session"

    def test_settoken_response_cookie_header_fallback(self) -> None:
        """Cookie jar 为空时从 Set-Cookie 响应头取新令牌。"""
        steamid = "76561198000000000"
        access_jwt = make_jwt(1800000000)
        set_cookie = (
            f"{sr.STORE_COOKIE_NAME}={login_value(steamid, access_jwt)}; "
            "Path=/; "
            "sessionid=header-session; Path=/"
        )
        finalize = FakeResponse(finalize_payload(steamid))
        settoken = FakeResponse({"result": 1}, set_cookie=set_cookie)
        session = FakeSession([finalize, settoken])

        result = sr.refresh_store_session(
            login_value(steamid, make_jwt(1900000000)),
            session_factory=lambda: session,
        )

        pairs = sr.parse_cookie_header(result.cookie)
        assert pairs[sr.STORE_COOKIE_NAME] == login_value(steamid, access_jwt)
        assert pairs["sessionid"] == "header-session"

    def test_network_error_wrapped_without_token(self) -> None:
        """requests 网络异常包装为 RefreshError，且消息不含令牌。"""
        import requests

        token = make_jwt(1900000000)
        session = FakeSession([requests.exceptions.ConnectionError("boom")])
        with pytest.raises(sr.RefreshError) as excinfo:
            sr.refresh_store_session(
                login_value("76561198000000000", token),
                session_factory=lambda: session,
            )
        assert excinfo.value.needs_login is False
        assert "网络" in str(excinfo.value)
        assert token not in str(excinfo.value)


class TestStatePersistence:
    def test_session_state_path(self, tmp_path: Path) -> None:
        """状态文件路径为 Cookie 路径加 .session.json。"""
        cookie = tmp_path / "cookie.txt"
        assert sr.session_state_path(str(cookie)) == Path(
            str(cookie) + ".session.json"
        )

    def test_load_missing_or_invalid_returns_empty(self, tmp_path: Path) -> None:
        """缺失、损坏或非对象 JSON 都返回空状态。"""
        cookie = tmp_path / "cookie.txt"
        assert sr.load_cookie_state(str(cookie)) == sr.CookieState()

        state_path = sr.session_state_path(str(cookie))
        state_path.write_text("{not json", encoding="utf-8")
        assert sr.load_cookie_state(str(cookie)) == sr.CookieState()

        state_path.write_text("[1, 2]", encoding="utf-8")
        assert sr.load_cookie_state(str(cookie)) == sr.CookieState()

        state_path.write_text(
            json.dumps({"steamid": "1", "refresh_cookie": 2, "extra": "x"}),
            encoding="utf-8",
        )
        state = sr.load_cookie_state(str(cookie))
        assert state.steamid == "1"
        assert state.refresh_cookie == ""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """保存后能读回全部字符串字段。"""
        cookie = tmp_path / "cookie.txt"
        state = sr.CookieState(
            steamid="1",
            refresh_cookie="refresh",
            last_refresh_at="2026-01-01T00:00:00+00:00",
            last_error="err",
        )
        sr.save_cookie_state(str(cookie), state)
        assert sr.load_cookie_state(str(cookie)) == state
        text = sr.session_state_path(str(cookie)).read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert json.loads(text)["refresh_cookie"] == "refresh"


class TestRefreshCookieFile:
    def test_missing_cookie_file_needs_login(self, tmp_path: Path) -> None:
        """Cookie 文件不存在时要求重新登录。"""
        with pytest.raises(sr.RefreshError) as excinfo:
            sr.refresh_cookie_file(str(tmp_path / "missing.txt"))
        assert excinfo.value.needs_login is True
        assert "不存在" in str(excinfo.value)

    def test_missing_refresh_cookie_needs_login(self, tmp_path: Path) -> None:
        """Cookie 中没有刷新令牌时要求重新登录。"""
        cookie = tmp_path / "cookie.txt"
        write_cookie(cookie, "foo=bar; steamLoginSecure=abc")
        with pytest.raises(sr.RefreshError) as excinfo:
            sr.refresh_cookie_file(str(cookie))
        assert excinfo.value.needs_login is True
        assert sr.REFRESH_COOKIE_NAME in str(excinfo.value)

    def test_throttled_uses_injected_now(self, tmp_path: Path, monkeypatch) -> None:
        """未强制刷新且间隔不足时返回 throttled，不调用刷新。"""
        steamid = "76561198000000000"
        cookie = tmp_path / "cookie.txt"
        write_cookie(cookie, cookie_header(steamid, make_jwt(1800000000), make_jwt(1900000000)))
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        sr.save_cookie_state(
            str(cookie),
            sr.CookieState(
                steamid=steamid,
                refresh_cookie="refresh-value",
                last_refresh_at=now.isoformat(),
            ),
        )

        def fail_refresh(*args: Any, **kwargs: Any) -> sr.RefreshResult:
            raise AssertionError("节流路径不应调用 refresh_store_session")

        monkeypatch.setattr(sr, "refresh_store_session", fail_refresh)
        outcome = sr.refresh_cookie_file(str(cookie), now_fn=lambda: now)

        assert outcome.refreshed is False
        assert outcome.reason == "throttled"
        assert outcome.result is None

    def test_not_expiring_gate(self, tmp_path: Path) -> None:
        """访问令牌还很久才过期时返回 not_expiring。"""
        steamid = "76561198000000000"
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        access_jwt = make_jwt(int(now.timestamp()) + 100 * 3600)
        cookie = tmp_path / "cookie.txt"
        write_cookie(
            cookie, cookie_header(steamid, access_jwt, make_jwt(1900000000))
        )

        outcome = sr.refresh_cookie_file(
            str(cookie), expires_within_hours=24.0, now_fn=lambda: now
        )

        assert outcome.refreshed is False
        assert outcome.reason == "not_expiring"

    def test_success_writes_cookie_and_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """成功刷新时写回 Cookie 文件和状态文件，保留无关 Cookie。"""
        steamid = "76561198000000000"
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        access_jwt = make_jwt(int(now.timestamp()) + 30 * 3600)
        refresh_jwt = make_jwt(1900000000)
        cookie = tmp_path / "cookie.txt"
        write_cookie(cookie, cookie_header(steamid, access_jwt, refresh_jwt))

        new_access = login_value(steamid, make_jwt(1800000000))
        new_refresh = login_value(steamid, make_jwt(1900009999))
        fake_result = sr.RefreshResult(
            cookie=(
                f"{sr.STORE_COOKIE_NAME}={new_access}; "
                f"sessionid=new-session; "
                f"{sr.REFRESH_COOKIE_NAME}={new_refresh}"
            ),
            steamid=steamid,
            refresh_cookie=new_refresh,
            expires_at=1800000000,
        )
        calls: List[Tuple[Any, ...]] = []

        def fake_refresh(*args: Any, **kwargs: Any) -> sr.RefreshResult:
            calls.append(args)
            return fake_result

        monkeypatch.setattr(sr, "refresh_store_session", fake_refresh)

        outcome = sr.refresh_cookie_file(
            str(cookie), force=True, now_fn=lambda: now
        )

        assert outcome.refreshed is True
        assert outcome.reason == "refreshed"
        assert outcome.result is fake_result
        assert len(calls) == 1

        written_text = cookie.read_text(encoding="utf-8")
        pairs = sr.parse_cookie_header(written_text)
        assert pairs["foo"] == "bar"
        assert pairs["last"] == "keep"
        assert pairs[sr.STORE_COOKIE_NAME] == new_access
        assert pairs["sessionid"] == "new-session"
        assert written_text.strip().startswith(
            "foo=bar; steamLoginSecure="
        )

        state_path = sr.session_state_path(str(cookie))
        assert state_path.exists()
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
        assert state_data["steamid"] == steamid
        assert state_data["refresh_cookie"] == new_refresh
        assert state_data["last_refresh_at"] == now.isoformat()
        assert state_data["last_error"] == ""

    def test_failure_records_last_error_and_reraises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """刷新失败时记录 last_error 到状态文件并原样抛出。"""
        steamid = "76561198000000000"
        cookie = tmp_path / "cookie.txt"
        original = cookie_header(
            steamid, make_jwt(1800000000), make_jwt(1900000000)
        )
        write_cookie(cookie, original)

        def fail_refresh(*args: Any, **kwargs: Any) -> sr.RefreshResult:
            raise sr.RefreshError("网络请求失败", needs_login=False)

        monkeypatch.setattr(sr, "refresh_store_session", fail_refresh)

        with pytest.raises(sr.RefreshError, match="网络请求失败"):
            sr.refresh_cookie_file(str(cookie), force=True)

        state_path = sr.session_state_path(str(cookie))
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
        assert state_data["last_error"] == "网络请求失败"
        assert state_data["steamid"] == steamid
        assert state_data["refresh_cookie"].startswith(steamid + "%7C%7C")
        assert cookie.read_text(encoding="utf-8").strip() == original