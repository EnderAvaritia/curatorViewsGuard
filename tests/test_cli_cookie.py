"""统计 Cookie 失效时的 CLI 行为：推送可操作告警并以 1 退出。"""

from curator_views import cli
from curator_views.core.fetcher import StatsAuthError

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

    def send(self, title, message, level="info"):
        self.sent.append((title, message, level))


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