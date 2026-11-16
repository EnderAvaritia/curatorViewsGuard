"""Steam 网页会话自动刷新。

Steam 登录后浏览器会保留两类 Cookie：

- ``steamLoginSecure``：按域名签发的短期访问令牌（JWT），过期后需重新获取；
- ``steamRefresh_steam``：写在 ``login.steampowered.com`` 下的长期刷新令牌，
  可用于换取新的、按域名签发的访问令牌。

本模块只使用长期刷新令牌调用 Steam 的 ``finalizelogin`` / ``settoken`` 接口，
把新的 ``steamLoginSecure`` 合并回现有 Cookie 文件，并把刷新状态（刷新时间、
上次错误等）记录在 ``<cookie_file>.session.json``。所有网络请求都发生在显式调用
``refresh_store_session`` / ``refresh_cookie_file`` 时，导入模块不触网。

依赖：仅 ``requests`` 与标准库。Cookie 与令牌属于敏感凭据，日志与异常消息中
不会包含它们的值。
"""

import base64
import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import unquote

import requests

from curator_views.core import fetcher, login
from curator_views.core.fetcher import USER_AGENT

logger = logging.getLogger(__name__)

# 长期刷新令牌 Cookie 名称（login.steampowered.com 域）
REFRESH_COOKIE_NAME = "steamRefresh_steam"
# 短期访问令牌 Cookie 名称（store.steampowered.com 域）
STORE_COOKIE_NAME = "steamLoginSecure"
# 登录服务与商店域名
LOGIN_ORIGIN = "https://login.steampowered.com"
STORE_ORIGIN = "https://store.steampowered.com"

# 表示需要人类重新登录的 EResult（令牌失效、被撤销、账号需验证等）
TERMINAL_ERESULTS = frozenset({15, 21, 26, 27})

# Set-Cookie 响应头中的 name=value 片段
_SET_COOKIE_RE = re.compile(r"(?:^|[,;]\s*)([^=;,\s]+)=([^;,\s]*)")
# Steam Cookie 值中的 "||" 分隔符（可能是 URL 编码后的 %7C%7C）
_LOGIN_VALUE_SEPARATOR = "||"


class RefreshError(RuntimeError):
    """会话刷新失败。

    Attributes:
        needs_login: 是否必须由人类重新执行 ``login`` 才能恢复。
        eresult: Steam 返回的 EResult（未知时为 ``None``）。
    """

    def __init__(
        self,
        message: str,
        *,
        needs_login: bool = False,
        eresult: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.needs_login = needs_login
        self.eresult = eresult


@dataclass(frozen=True)
class RefreshResult:
    """一次成功刷新得到的会话数据。"""

    cookie: str
    """合并后的 Store Cookie 头（含新的 steamLoginSecure 与 sessionid）。"""

    steamid: str
    """SteamID64。"""

    refresh_cookie: str
    """应持久化的 steamRefresh_steam 值（Steam 轮换后为新值）。"""

    expires_at: int
    """新 steamLoginSecure 的过期时间（Unix 秒；未知时为 0）。"""


@dataclass
class CookieState:
    """Cookie 文件的刷新状态（存放在 ``<cookie_file>.session.json``）。"""

    steamid: str = ""
    refresh_cookie: str = ""
    last_refresh_at: str = ""
    last_error: str = ""


@dataclass(frozen=True)
class RefreshOutcome:
    """一次刷新决策的结果。"""

    refreshed: bool
    """是否真正执行了刷新。"""

    reason: str
    """``"refreshed"`` / ``"throttled"`` / ``"not_expiring"``。"""

    state: CookieState
    """刷新后的状态对象。"""

    result: Optional[RefreshResult] = None
    """成功刷新时的会话数据。"""


def parse_cookie_header(header: str) -> Dict[str, str]:
    """把 Cookie 头解析为名称到值的映射。

    按 ``;`` 切分，忽略没有 ``=`` 的片段；不进行 URL 解码；同名 Cookie
    后出现的值覆盖先出现的值。空输入返回空字典。

    Args:
        header: 形如 ``a=1; b=2`` 的 Cookie 字符串。

    Returns:
        名称到值的映射。
    """
    pairs: Dict[str, str] = {}
    for part in header.split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        name = name.strip()
        if not name:
            continue
        pairs[name] = value
    return pairs


def build_cookie_header(pairs: Iterable[Tuple[str, str]]) -> str:
    """把名称/值序列拼成 Cookie 头。

    跳过名称为空的项，按输入顺序用 ``"; "`` 连接，值原样保留。

    Args:
        pairs: ``(name, value)`` 可迭代对象。

    Returns:
        形如 ``"a=1; b=2"`` 的字符串；没有有效项时返回空串。
    """
    parts = [
        f"{name}={value}"
        for name, value in pairs
        if str(name or "").strip()
    ]
    return "; ".join(parts)


def extract_refresh_cookie(header: str) -> str:
    """从原始 Cookie 文本中提取 ``steamRefresh_steam`` 的值。

    兼容浏览器复制的 ``Cookie:`` 前缀、请求头块与 cURL 形式（复用
    :func:`fetcher.extract_cookie`），不发送任何网络请求。

    Args:
        header: 原始 Cookie 文本。

    Returns:
        ``steamRefresh_steam`` 的值；不存在时返回空串。
    """
    if not header:
        return ""
    normalized = fetcher.extract_cookie(header)
    return parse_cookie_header(normalized).get(REFRESH_COOKIE_NAME, "").strip()


def _split_login_value(value: str) -> Tuple[str, str]:
    """把 Steam Cookie 值拆成 ``(steamid, jwt)``。

    Steam 的访问/刷新令牌通常是 ``<steamid64>%7C%7C<jwt>``；这里先做 URL
    解码，再按 ``||`` 分割一次。没有分隔符时把整段当作 JWT。

    Args:
        value: Cookie 值（可含 URL 编码）。

    Returns:
        ``(steamid, jwt)``；缺失部分为空串。
    """
    text = unquote(str(value or "")).strip()
    if _LOGIN_VALUE_SEPARATOR in text:
        steamid, token = text.split(_LOGIN_VALUE_SEPARATOR, 1)
        return steamid.strip(), token.strip()
    return "", text


def split_refresh_cookie(value: str) -> Tuple[str, str]:
    """把 steamRefresh_steam 值拆成 (steamid, 原始 JWT)；无分隔符时 steamid 为空。

    Args:
        value: ``steamRefresh_steam`` 的值（可为 URL 编码的
            ``<steamid>%7C%7C<jwt>``）。

    Returns:
        ``(steamid, jwt)``；缺少分隔符时第一项为空串。委托
        :func:`_split_login_value` 完成 URL 解码与分割。
    """
    return _split_login_value(value)


def jwt_expires_at(token: str) -> int:
    """读取 JWT 载荷中的 ``exp``。

    支持直接传入 JWT，也支持传入 ``<steamid>||<jwt>`` 形式的 Cookie 值。
    载荷使用 base64url 编码，缺失的 padding 会自动补齐。任何解析失败
    都返回 0，绝不抛出异常。

    Args:
        token: JWT 或 Steam Cookie 值。

    Returns:
        Unix 秒级过期时间；未知或无法解析时为 0。
    """
    try:
        _, jwt = _split_login_value(token)
        parts = jwt.split(".")
        if len(parts) < 2 or not parts[1]:
            return 0
        payload = parts[1].strip()
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        if not isinstance(data, dict):
            return 0
        exp = data.get("exp")
        if exp is None or isinstance(exp, bool):
            return 0
        return int(exp)
    except Exception:  # noqa: BLE001 - 契约要求解析失败绝不抛异常
        return 0


def cookie_expires_at(header: str, name: str = STORE_COOKIE_NAME) -> int:
    """读取 Cookie 头中指定 Cookie 的 JWT 过期时间。

    Args:
        header: Cookie 头文本（可含 ``Cookie:`` 前缀）。
        name: Cookie 名称，默认 ``steamLoginSecure``。

    Returns:
        Unix 秒级过期时间；Cookie 缺失或无法解析时为 0。
    """
    if not header:
        return 0
    normalized = fetcher.extract_cookie(header)
    value = parse_cookie_header(normalized).get(name, "")
    if not value:
        return 0
    return jwt_expires_at(value)


def merge_store_cookie(
    header: str, *, access_cookie: str, sessionid: str = ""
) -> str:
    """把新的访问 Cookie 合并进现有 Cookie 头。

    保持现有 Cookie 的顺序：``steamLoginSecure`` 原位替换，``sessionid``
    在提供时原位替换；缺失则追加到末尾；其余 Cookie 原样保留。

    Args:
        header: 现有 Cookie 头。
        access_cookie: 新的 ``steamLoginSecure`` 值。
        sessionid: 可选的新的 ``sessionid`` 值；空串表示不修改现有值。

    Returns:
        合并后的 Cookie 头。

    Raises:
        ValueError: ``access_cookie`` 为空。
    """
    if not access_cookie:
        raise ValueError("access_cookie 不能为空")

    merged: list[Tuple[str, str]] = []
    access_replaced = False
    sessionid_replaced = False
    for part in header.split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        name = name.strip()
        if not name:
            continue
        if name == STORE_COOKIE_NAME:
            merged.append((name, access_cookie))
            access_replaced = True
        elif name == "sessionid" and sessionid:
            merged.append((name, sessionid))
            sessionid_replaced = True
        else:
            merged.append((name, value))

    if not access_replaced:
        merged.append((STORE_COOKIE_NAME, access_cookie))
    if sessionid and not sessionid_replaced:
        merged.append(("sessionid", sessionid))
    return build_cookie_header(merged)


def _to_int(value: Any) -> Optional[int]:
    """尽力把 EResult 转成整数；无法转换时返回 ``None``。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _eresult_error(code: Optional[int], action: str) -> RefreshError:
    """构造 EResult 失败异常，按终止码决定是否需要重新登录。"""
    needs_login = code in TERMINAL_ERESULTS if code is not None else False
    if code is None:
        message = f"{action}失败：Steam 未返回可用的 EResult，请稍后重试"
    else:
        message = f"{action}失败（EResult {code}）"
        if needs_login:
            message += "，登录态已失效，请重新运行 login"
        else:
            message += "，请稍后重试"
    return RefreshError(message, needs_login=needs_login, eresult=code)


def _json_payload(response: Any) -> Dict[str, Any]:
    """解析响应 JSON，失败时抛出不含凭据的 :class:`RefreshError`。"""
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError) as exc:
        raise RefreshError(
            "Steam 返回了无法解析的响应（JSON 解析失败），请稍后重试",
            needs_login=False,
        ) from exc
    if not isinstance(payload, dict):
        raise RefreshError(
            "Steam 返回了非预期的响应结构，请稍后重试", needs_login=False
        )
    return payload


def _cookie_values_from_jar(
    jar: Any, name: str, domain_hint: str
) -> Tuple[str, str]:
    """从 Cookie jar 中查找同名 Cookie，返回 (指定域值, 任意域值)。"""
    if jar is None:
        return "", ""
    if isinstance(jar, dict):
        value = jar.get(name)
        return "", str(value) if value else ""
    try:
        items = list(jar)
    except TypeError:
        items = []
    preferred = ""
    fallback = ""
    for item in items:
        if isinstance(item, dict):
            cookie_name = str(item.get("name") or "")
            cookie_value = str(item.get("value") or "")
            cookie_domain = str(item.get("domain") or "")
        else:
            cookie_name = str(getattr(item, "name", "") or "")
            cookie_value = str(getattr(item, "value", "") or "")
            cookie_domain = str(getattr(item, "domain", "") or "")
        if cookie_name != name or not cookie_value:
            continue
        if domain_hint and domain_hint in cookie_domain:
            if not preferred:
                preferred = cookie_value
        elif not fallback:
            fallback = cookie_value
    if preferred or fallback:
        return preferred, fallback

    # 兜底兼容只实现 get(name) 的 jar（如 requests 的 RequestsCookieJar）。
    getter = getattr(jar, "get", None)
    if callable(getter):
        try:
            value = getter(name)
        except TypeError:
            value = None
        if value:
            return "", str(value)
    return "", ""


def _set_cookie_values(response: Any, name: str) -> str:
    """从 ``Set-Cookie`` 响应头中提取指定 Cookie 的值。"""
    headers = getattr(response, "headers", None) or {}
    header_text = ""
    try:
        header_text = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    except AttributeError:
        header_text = ""
    if not header_text:
        raw = getattr(response, "raw", None)
        raw_headers = getattr(raw, "headers", None)
        getlist = getattr(raw_headers, "getlist", None)
        if callable(getlist):
            header_text = ", ".join(getlist("Set-Cookie"))
    if not header_text:
        return ""
    for match in _SET_COOKIE_RE.finditer(str(header_text)):
        if match.group(1) == name and match.group(2):
            return match.group(2)
    return ""


def _extract_response_cookie(
    response: Any, name: str, domain_hint: str
) -> str:
    """从单个响应的 Cookie 中提取指定值，优先匹配指定域名。

    先看响应携带的 Cookie jar（指定域优先于任意域），最后回退到
    ``Set-Cookie`` 响应头。只读取当前响应，避免复用 Session 里的旧令牌。
    """
    preferred, fallback = _cookie_values_from_jar(
        getattr(response, "cookies", None), name, domain_hint
    )
    if preferred:
        return preferred
    if fallback:
        return fallback
    return _set_cookie_values(response, name)


def _find_store_transfer(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """在 finalizelogin 响应中查找商店域的 transfer_info 项。"""
    entries = payload.get("transfer_info")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "store.steampowered.com" in str(entry.get("url") or ""):
            return entry
    return None


def _settoken(
    *,
    session: Any,
    url: str,
    steamid: str,
    nonce: str,
    auth: str,
    fallback_sessionid: str,
    refresh_cookie: str,
    timeout: float,
) -> RefreshResult:
    """向 settoken 接口换取新的 ``steamLoginSecure`` 并组装结果。"""
    response = session.post(
        url,
        data={"steamID": steamid, "nonce": nonce, "auth": auth},
        timeout=timeout,
    )
    payload = _json_payload(response)
    result_code = _to_int(payload.get("result"))
    if result_code != 1:
        error_code = result_code
        if error_code is None:
            error_code = _to_int(payload.get("eresult") or payload.get("error"))
        raise _eresult_error(error_code, "Steam 登录令牌交换")

    access_cookie = _extract_response_cookie(
        response, STORE_COOKIE_NAME, "store.steampowered.com"
    )
    if not access_cookie:
        raise RefreshError(
            "Steam 未返回新的 steamLoginSecure Cookie，请重新运行 login",
            needs_login=True,
        )

    returned_sessionid = _extract_response_cookie(
        response, "sessionid", "store.steampowered.com"
    )
    sessionid = returned_sessionid or fallback_sessionid
    merged = build_cookie_header(
        [
            (STORE_COOKIE_NAME, access_cookie),
            ("sessionid", sessionid),
            (REFRESH_COOKIE_NAME, refresh_cookie),
        ]
    )

    resolved_steamid = (
        steamid
        or _split_login_value(refresh_cookie)[0]
        or _split_login_value(access_cookie)[0]
    )
    return RefreshResult(
        cookie=merged,
        steamid=str(resolved_steamid),
        refresh_cookie=refresh_cookie,
        expires_at=jwt_expires_at(access_cookie),
    )


def _refresh_via_ajaxrefresh(
    *,
    session: Any,
    redir: str,
    timeout: float,
    steamid_hint: str,
    refresh_cookie: str,
    sessionid: str,
) -> RefreshResult:
    """备用刷新流程：``ajaxrefresh`` 后再调用商店的 ``settoken``。

    仅在主流程没有可用的 ``transfer_info`` 且失败不是终止型 EResult 时使用。
    """
    response = session.post(
        f"{LOGIN_ORIGIN}/jwt/ajaxrefresh",
        data={"redir": redir},
        timeout=timeout,
    )
    payload = _json_payload(response)
    if not payload.get("success"):
        code = _to_int(payload.get("error") or payload.get("eresult"))
        raise _eresult_error(code, "Steam 备用会话刷新")

    steamid = str(payload.get("steamID") or steamid_hint or "")
    nonce = str(payload.get("nonce") or "")
    auth = str(payload.get("auth") or "")
    if not steamid or not nonce or not auth:
        raise RefreshError(
            "Steam 备用刷新响应缺少必要字段，请重新运行 login",
            needs_login=True,
        )

    rotated_refresh = (
        _extract_response_cookie(
            response, REFRESH_COOKIE_NAME, "login.steampowered.com"
        )
        or refresh_cookie
    )
    return _settoken(
        session=session,
        url=f"{STORE_ORIGIN}/login/settoken",
        steamid=steamid,
        nonce=nonce,
        auth=auth,
        fallback_sessionid=sessionid,
        refresh_cookie=rotated_refresh,
        timeout=timeout,
    )


def refresh_store_session(
    refresh_cookie: str,
    *,
    redir: str = STORE_ORIGIN + "/",
    timeout: float = 20.0,
    proxy: Optional[str] = None,
    user_agent: str = USER_AGENT,
    session_factory: Optional[Callable[[], Any]] = None,
) -> RefreshResult:
    """使用长期刷新令牌换取新的商店访问令牌。

    主流程：``jwt/finalizelogin`` -> 商店 ``transfer_info`` URL（settoken）。
    当主流程没有可用的商店 transfer_info 时，自动回退到 ``jwt/ajaxrefresh``
    再调用 ``store.steampowered.com/login/settoken``。

    Args:
        refresh_cookie: ``steamRefresh_steam`` 的值（可为 URL 编码的
            ``<steamid>%7C%7C<jwt>``）。
        redir: 登录完成后的跳转地址。
        timeout: 单次 HTTP 请求超时秒数。
        proxy: 可选代理地址。
        user_agent: 请求使用的 User-Agent。
        session_factory: 可注入的 Session 工厂（测试用），默认
            :class:`requests.Session`。

    Returns:
        刷新结果；``cookie`` 包含新的 ``steamLoginSecure``、``sessionid``
        与（可能轮换后的）``steamRefresh_steam``。

    Raises:
        RefreshError: 网络失败、EResult 失败、响应缺少必要字段或 Cookie。
    """
    if not refresh_cookie:
        raise RefreshError(
            "steamRefresh_steam 为空，请重新运行 login", needs_login=True
        )

    session = session_factory() if session_factory is not None else requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
        }
    )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    steamid_hint, refresh_jwt = _split_login_value(refresh_cookie)
    if not refresh_jwt:
        raise RefreshError(
            "steamRefresh_steam 内容无效，请重新运行 login", needs_login=True
        )
    sessionid = secrets.token_hex(12)

    try:
        finalize_response = session.post(
            f"{LOGIN_ORIGIN}/jwt/finalizelogin",
            data={"nonce": refresh_jwt, "sessionid": sessionid, "redir": redir},
            timeout=timeout,
        )
        finalize_payload = _json_payload(finalize_response)

        if finalize_payload.get("error"):
            code = _to_int(finalize_payload.get("error"))
            raise _eresult_error(code, "Steam 刷新令牌校验")

        rotated_refresh = (
            _extract_response_cookie(
                finalize_response,
                REFRESH_COOKIE_NAME,
                "login.steampowered.com",
            )
            or refresh_cookie
        )
        steamid = str(
            finalize_payload.get("steamID")
            or finalize_payload.get("steamid")
            or steamid_hint
            or ""
        )

        transfer = _find_store_transfer(finalize_payload)
        params = transfer.get("params") if transfer else None
        if not isinstance(params, dict):
            params = {}
        nonce = str(params.get("nonce") or "")
        auth = str(params.get("auth") or "")
        store_url = str(transfer.get("url") or "") if transfer else ""

        if not transfer or not nonce or not auth:
            return _refresh_via_ajaxrefresh(
                session=session,
                redir=redir,
                timeout=timeout,
                steamid_hint=steamid,
                refresh_cookie=rotated_refresh,
                sessionid=sessionid,
            )

        return _settoken(
            session=session,
            url=store_url,
            steamid=steamid,
            nonce=nonce,
            auth=auth,
            fallback_sessionid=sessionid,
            refresh_cookie=rotated_refresh,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RefreshError(
            "刷新 Steam 会话时网络请求失败，请检查网络或代理后重试",
            needs_login=False,
        ) from exc


def session_state_path(cookie_file: str) -> Path:
    """返回 Cookie 文件对应的状态文件路径。

    Args:
        cookie_file: Cookie 文件路径。

    Returns:
        ``Path(str(cookie_file) + ".session.json")``。
    """
    return Path(str(cookie_file) + ".session.json")


def load_cookie_state(cookie_file: str) -> CookieState:
    """读取刷新状态文件，任何异常都退化为空状态。

    文件缺失、不可读、不是 JSON 或不是对象时返回 ``CookieState()``；
    只接受字符串字段。

    Args:
        cookie_file: Cookie 文件路径。

    Returns:
        刷新状态。
    """
    path = session_state_path(cookie_file)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return CookieState()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return CookieState()
    if not isinstance(data, dict):
        return CookieState()

    def _text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    return CookieState(
        steamid=_text("steamid"),
        refresh_cookie=_text("refresh_cookie"),
        last_refresh_at=_text("last_refresh_at"),
        last_error=_text("last_error"),
    )


def save_cookie_state(cookie_file: str, state: CookieState) -> None:
    """原子写入刷新状态文件。

    同目录临时文件 + ``os.replace``，避免中途失败留下半截 JSON；
    POSIX 下尽力把文件权限设为 0600（状态文件含刷新令牌）。

    Args:
        cookie_file: Cookie 文件路径。
        state: 待保存的状态。
    """
    path = session_state_path(cookie_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".session-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _parse_state_time(value: str) -> Optional[datetime]:
    """解析状态中的 ISO-8601 时间；无时区时按 UTC 处理。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_utc(moment: datetime) -> datetime:
    """把可能无时区的 datetime 统一成 UTC。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def refresh_cookie_file(
    cookie_file: str,
    *,
    redir: str = STORE_ORIGIN + "/",
    timeout: float = 20.0,
    proxy: Optional[str] = None,
    force: bool = False,
    min_interval_hours: float = 6.0,
    expires_within_hours: Optional[float] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> RefreshOutcome:
    """刷新 Cookie 文件中的商店访问令牌。

    流程：

    1. 读取现有 Cookie 文件（兼容浏览器请求头 / cURL）；
    2. 从状态文件或 Cookie 中取得 ``steamRefresh_steam``；
    3. 默认按 ``min_interval_hours`` 节流；
    4. 可选地只在访问令牌接近过期时刷新；
    5. 刷新成功后把新访问令牌合并回现有 Cookie 头并原子写回，
       同时更新 ``<cookie_file>.session.json``。

    Args:
        cookie_file: Cookie 文件路径。
        redir: 登录完成后的跳转地址。
        timeout: 单次 HTTP 请求超时秒数。
        proxy: 可选代理地址。
        force: 为 ``True`` 时跳过节流。
        min_interval_hours: 两次刷新之间的最小间隔（小时）。
        expires_within_hours: 仅在访问令牌将在该小时数内过期时刷新；
            ``None`` 表示不检查。
        now_fn: 可注入的当前时间函数（测试用），默认 UTC 当前时间。

    Returns:
        刷新结果或跳过原因。

    Raises:
        RefreshError: Cookie 文件缺失、缺少刷新令牌或刷新失败。
    """
    current_time_fn = now_fn or (lambda: datetime.now(timezone.utc))
    try:
        header = fetcher.read_cookie_file(cookie_file)
    except FileNotFoundError as exc:
        raise RefreshError(
            "Cookie 文件不存在，请先运行 login 生成", needs_login=True
        ) from exc
    except fetcher.CookieFileError as exc:
        raise RefreshError(
            f"Cookie 文件不可用：{exc}", needs_login=True
        ) from exc

    state = load_cookie_state(cookie_file)
    if not state.refresh_cookie:
        state.refresh_cookie = extract_refresh_cookie(header)
    if not state.steamid:
        state.steamid = _split_login_value(state.refresh_cookie)[0]

    if not state.refresh_cookie:
        raise RefreshError(
            "Cookie 中缺少 steamRefresh_steam，无法自动刷新，请重新运行 login",
            needs_login=True,
        )

    now = _as_utc(current_time_fn())

    if not force:
        last_refresh = _parse_state_time(state.last_refresh_at)
        if last_refresh is not None:
            if now - last_refresh < timedelta(hours=min_interval_hours):
                return RefreshOutcome(False, "throttled", state, None)

    if expires_within_hours is not None:
        expires_at = cookie_expires_at(header)
        if expires_at > 0:
            remaining_hours = (expires_at - now.timestamp()) / 3600.0
            if remaining_hours > expires_within_hours:
                return RefreshOutcome(False, "not_expiring", state, None)

    try:
        result = refresh_store_session(
            state.refresh_cookie,
            redir=redir,
            timeout=timeout,
            proxy=proxy,
        )
    except RefreshError as exc:
        state.last_error = str(exc)
        save_cookie_state(cookie_file, state)
        logger.warning(
            "Steam 会话刷新失败（steamid=%s）：%s", state.steamid, exc
        )
        raise

    result_pairs = parse_cookie_header(result.cookie)
    access_cookie = result_pairs.get(STORE_COOKIE_NAME, "")
    returned_sessionid = result_pairs.get("sessionid", "")
    merged_header = merge_store_cookie(
        header, access_cookie=access_cookie, sessionid=returned_sessionid
    )
    login.write_cookie_file(cookie_file, merged_header)

    state.steamid = result.steamid or state.steamid
    state.refresh_cookie = result.refresh_cookie or state.refresh_cookie
    state.last_refresh_at = now.isoformat()
    state.last_error = ""
    save_cookie_state(cookie_file, state)

    logger.info("Steam 会话刷新成功（steamid=%s）", state.steamid)
    return RefreshOutcome(True, "refreshed", state, result)