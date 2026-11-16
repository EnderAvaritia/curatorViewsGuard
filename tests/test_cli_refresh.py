"""refresh-cookie 子命令测试：解析器默认值、成功、失败与跳过路径。

全部使用 monkeypatch，不发起任何真实网络请求，也不读取真实 Cookie 内容。
"""

import json
from datetime import datetime, timezone

from curator_views import cli
from curator_views.core.login import CookieCheckResult
from curator_views.core.session_refresh import (
    CookieState,
    RefreshError,
    RefreshOutcome,
    RefreshResult,
)

CONFIG_TEXT = """\
clan_id: "45519015"
cookie_file: "cookie.txt"
db_path: "views.db"
ntfy_url: ""
ntfy_topic: ""
"""


def write_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    return path


def refreshed_outcome() -> RefreshOutcome:
    future = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
    return RefreshOutcome(
        True,
        "refreshed",
        CookieState(steamid="1", refresh_cookie="refresh-token"),
        RefreshResult(
            cookie="steamLoginSecure=SECRET_COOKIE_VALUE",
            steamid="1",
            refresh_cookie="refresh-token",
            expires_at=future,
        ),
    )


def valid_result() -> CookieCheckResult:
    return CookieCheckResult(True, "Cookie 有效", 200, "https://example.invalid")


def test_parser_registers_refresh_cookie_defaults():
    args = cli.build_parser().parse_args(["refresh-cookie"])

    assert args.func is cli.cmd_refresh_cookie
    assert args.config == "config.yaml"
    assert args.cookie_file is None
    assert args.force is False
    assert args.timeout == 20.0


def test_refresh_success_prints_json_without_cookie(
    tmp_path, monkeypatch, capsys
):
    config_path = write_config(tmp_path)
    monkeypatch.setattr(
        cli, "refresh_cookie_file", lambda *a, **k: refreshed_outcome()
    )
    monkeypatch.setattr(
        cli, "read_cookie_file", lambda *a, **k: "steamLoginSecure=SECRET_COOKIE_VALUE"
    )
    monkeypatch.setattr(cli, "validate_cookie", lambda *a, **k: valid_result())

    code = cli.main(["refresh-cookie", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert code == 0
    summary = json.loads(captured.out)
    assert summary["mode"] == "refresh"
    assert summary["refreshed"] is True
    assert summary["valid"] is True
    assert summary["skip_reason"] == ""
    assert "SECRET_COOKIE_VALUE" not in captured.out
    assert "SECRET_COOKIE_VALUE" not in captured.err


def test_refresh_failure_reports_login_hint_without_token(
    tmp_path, monkeypatch, capsys
):
    config_path = write_config(tmp_path)

    def fail_refresh(*args, **kwargs):
        raise RefreshError("缺少刷新令牌", needs_login=True)

    monkeypatch.setattr(cli, "refresh_cookie_file", fail_refresh)

    code = cli.main(["refresh-cookie", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert "login --config" in captured.err
    assert "refresh-token" not in captured.out
    assert "SECRET" not in captured.out + captured.err
    summary = json.loads(captured.out)
    assert summary["refreshed"] is False
    assert summary["valid"] is False
    assert summary["reason"] == "缺少刷新令牌"


def test_refresh_skipped_reports_skip_reason(tmp_path, monkeypatch, capsys):
    config_path = write_config(tmp_path)
    outcome = RefreshOutcome(
        False,
        "throttled",
        CookieState(steamid="1", refresh_cookie="refresh-token"),
        None,
    )
    monkeypatch.setattr(cli, "refresh_cookie_file", lambda *a, **k: outcome)
    monkeypatch.setattr(
        cli, "read_cookie_file", lambda *a, **k: "steamLoginSecure=SECRET"
    )
    monkeypatch.setattr(cli, "validate_cookie", lambda *a, **k: valid_result())

    code = cli.main(["refresh-cookie", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert code == 0
    summary = json.loads(captured.out)
    assert summary["refreshed"] is False
    assert summary["skip_reason"] == "throttled"
    assert "SECRET" not in captured.out