"""统计 Cookie 失效时的 CLI 行为：推送可操作告警并以 1 退出。

同时覆盖自动续期：鉴权失败后续期并重试一次、无刷新令牌时只告警一次、
以及 dry-run 绝不触发续期。
"""

from curator_views import cli
from curator_views.core.fetcher import StatsAuthError
from curator_views.core.session_refresh import (
    CookieState,
    RefreshError,
    RefreshOutcome,
    RefreshResult,
)

CONFIG_TEXT = """\
clan_id: "45519015"
cookie_file: "missing_cookie.txt"
db_path: "views.db"
boost_enabled: false
boost_mode: stable
ntfy_url: ""
ntfy_topic: ""
"""


class FakeNotifier:
    instances = []

    def __init__(self, *args, **kwargs):
        self.sent = []
        FakeNotifier.instances.append(self)

    def send(self, title, message, level="info", category="alert"):
        self.sent.append((title, message, level))

    def flush_pending(self) -> int:
        return 0


def test_run_notifies_and_exits_on_auth_error(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    FakeNotifier.instances = []

    def boom(*args, **kwargs):
        raise StatsAuthError("统计页被重定向到公开页面（Cookie 已失效或未登录）")

    monkeypatch.setattr(cli, "Notifier", FakeNotifier)
    monkeypatch.setattr(cli, "run_daily", boom)

    code = cli.main(
        ["run", "--config", str(config_path), "--no-boost"]
    )

    assert code == 1
    assert "Cookie 已失效" in capsys.readouterr().err

    assert len(FakeNotifier.instances) == 1
    title, message, level = FakeNotifier.instances[0].sent[0]
    assert "Cookie 失效" in title
    assert "login --config" in message
    assert level == "error"


def test_check_hints_relogin_on_auth_error(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")

    class AuthFetcher:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_stats(self):
            raise StatsAuthError("统计页被重定向到公开页面（Cookie 已失效或未登录）")

    monkeypatch.setattr(cli, "Fetcher", AuthFetcher)

    code = cli.main(["check", "--config", str(config_path)])

    assert code == 1
    err = capsys.readouterr().err
    assert "Cookie 已失效" in err
    assert "login --config" in err
    assert not (tmp_path / "views.db").exists()


def test_forecast_fetch_hints_relogin_on_auth_error(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    # 先建一个可读的本地库，确保 --fetch 走到抓取分支
    db_path = tmp_path / "views.db"
    from curator_views.core.storage import Storage

    storage = Storage(str(db_path))
    storage.upsert_daily_stats(1794528000, 5449)
    storage._conn.close()

    class AuthFetcher:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_stats(self):
            raise StatsAuthError("统计页被重定向到公开页面（Cookie 已失效或未登录）")

    monkeypatch.setattr(cli, "Fetcher", AuthFetcher)

    code = cli.main(
        ["forecast", "--config", str(config_path), "--fetch", "--days", "2"]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "Cookie 已失效" in err
    assert "login --config" in err


def _refreshed_outcome() -> RefreshOutcome:
    return RefreshOutcome(
        True,
        "refreshed",
        CookieState(steamid="1", refresh_cookie="refresh-token"),
        RefreshResult(
            cookie="steamLoginSecure=a",
            steamid="1",
            refresh_cookie="refresh-token",
            expires_at=0,
        ),
    )


def _write_run_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'clan_id: "45519015"\n'
        'cookie_file: "cookie.txt"\n'
        'db_path: "views.db"\n'
        "boost_enabled: false\n"
        'ntfy_url: ""\n'
        'ntfy_topic: ""\n',
        encoding="utf-8",
    )
    (tmp_path / "cookie.txt").write_text(
        "steamLoginSecure=a\n", encoding="utf-8"
    )
    return config_path


def test_auth_error_refreshes_and_retries_once(tmp_path, monkeypatch):
    config_path = _write_run_config(tmp_path)
    FakeNotifier.instances = []
    events = []
    run_calls = {"count": 0}

    class RecordingFetcher:
        instances = []

        def __init__(self, *args, **kwargs):
            events.append("fetcher")
            RecordingFetcher.instances.append(self)

    def fake_refresh(*args, **kwargs):
        events.append("refresh")
        return _refreshed_outcome()

    def fake_run_daily(*args, **kwargs):
        events.append("run")
        run_calls["count"] += 1
        if run_calls["count"] == 1:
            raise StatsAuthError("统计页被重定向到公开页面")
        return {"refreshed_ok": True}

    monkeypatch.setattr(cli, "Notifier", FakeNotifier)
    monkeypatch.setattr(cli, "Fetcher", RecordingFetcher)
    monkeypatch.setattr(cli, "refresh_cookie_file", fake_refresh)
    monkeypatch.setattr(cli, "run_daily", fake_run_daily)

    code = cli.main(["run", "--config", str(config_path), "--no-boost"])

    assert code == 0
    assert run_calls["count"] == 2
    assert len(RecordingFetcher.instances) == 2
    assert events == [
        "refresh",
        "fetcher",
        "run",
        "refresh",
        "fetcher",
        "run",
    ]
    assert FakeNotifier.instances[0].sent == []


def test_auth_error_without_refresh_token_notifies_once(
    tmp_path, monkeypatch
):
    config_path = _write_run_config(tmp_path)
    FakeNotifier.instances = []

    class DummyFetcher:
        def __init__(self, *args, **kwargs):
            pass

    def fail_refresh(*args, **kwargs):
        raise RefreshError("缺少刷新令牌", needs_login=True)

    def fail_run_daily(*args, **kwargs):
        raise StatsAuthError("统计页被重定向到公开页面")

    monkeypatch.setattr(cli, "Notifier", FakeNotifier)
    monkeypatch.setattr(cli, "Fetcher", DummyFetcher)
    monkeypatch.setattr(cli, "refresh_cookie_file", fail_refresh)
    monkeypatch.setattr(cli, "run_daily", fail_run_daily)

    code = cli.main(["run", "--config", str(config_path), "--no-boost"])

    assert code == 1
    assert len(FakeNotifier.instances) == 1
    sent = FakeNotifier.instances[0].sent
    assert len(sent) == 1
    title, message, level = sent[0]
    assert "Cookie 失效" in title
    assert "login --config" in message
    assert level == "error"


def test_dry_run_does_not_refresh(tmp_path, monkeypatch):
    config_path = _write_run_config(tmp_path)
    FakeNotifier.instances = []

    class DummyFetcher:
        def __init__(self, *args, **kwargs):
            pass

    def fail_if_called(*args, **kwargs):
        raise AssertionError("dry-run 不能触发 Cookie 续期")

    monkeypatch.setattr(cli, "Notifier", FakeNotifier)
    monkeypatch.setattr(cli, "Fetcher", DummyFetcher)
    monkeypatch.setattr(cli, "refresh_cookie_file", fail_if_called)
    monkeypatch.setattr(
        cli, "run_daily", lambda *a, **k: {"dry_run": True}
    )

    code = cli.main(
        ["run", "--config", str(config_path), "--dry-run", "--no-boost"]
    )

    assert code == 0
    assert FakeNotifier.instances[0].sent == []


def test_terminal_prerun_failure_skips_forced_refresh_and_retry(
    tmp_path, monkeypatch
):
    """执行前续期已判定需登录：不再强制刷新、不重跑，只通知一次。"""
    config_path = _write_run_config(tmp_path)
    FakeNotifier.instances = []
    refreshes = {"count": 0}
    runs = {"count": 0}

    class DummyFetcher:
        def __init__(self, *args, **kwargs):
            pass

    def terminal_refresh(*args, **kwargs):
        refreshes["count"] += 1
        raise RefreshError("令牌已失效", needs_login=True, reason="terminal")

    def fail_run_daily(*args, **kwargs):
        runs["count"] += 1
        raise StatsAuthError("统计页被重定向到公开页面")

    monkeypatch.setattr(cli, "Notifier", FakeNotifier)
    monkeypatch.setattr(cli, "Fetcher", DummyFetcher)
    monkeypatch.setattr(cli, "refresh_cookie_file", terminal_refresh)
    monkeypatch.setattr(cli, "run_daily", fail_run_daily)

    code = cli.main(["run", "--config", str(config_path), "--no-boost"])

    assert code == 1
    assert refreshes["count"] == 1
    assert runs["count"] == 1
    assert len(FakeNotifier.instances) == 1
    assert len(FakeNotifier.instances[0].sent) == 1


def test_forced_refresh_terminal_failure_skips_retry(tmp_path, monkeypatch):
    """强制续期返回终止性失败：不再重跑 run_daily，只通知一次。"""
    config_path = _write_run_config(tmp_path)
    FakeNotifier.instances = []
    refreshes = {"count": 0}
    runs = {"count": 0}

    class DummyFetcher:
        def __init__(self, *args, **kwargs):
            pass

    def refresh_with_terminal(*args, **kwargs):
        refreshes["count"] += 1
        if kwargs.get("force"):
            raise RefreshError("令牌已失效", needs_login=True, reason="terminal")
        return RefreshOutcome(
            False,
            "throttled",
            CookieState(steamid="1", refresh_cookie="refresh-token"),
            None,
        )

    def fail_run_daily(*args, **kwargs):
        runs["count"] += 1
        raise StatsAuthError("统计页被重定向到公开页面")

    monkeypatch.setattr(cli, "Notifier", FakeNotifier)
    monkeypatch.setattr(cli, "Fetcher", DummyFetcher)
    monkeypatch.setattr(cli, "refresh_cookie_file", refresh_with_terminal)
    monkeypatch.setattr(cli, "run_daily", fail_run_daily)

    code = cli.main(["run", "--config", str(config_path), "--no-boost"])

    assert code == 1
    assert refreshes["count"] == 2
    assert runs["count"] == 1
    assert len(FakeNotifier.instances) == 1
    assert len(FakeNotifier.instances[0].sent) == 1