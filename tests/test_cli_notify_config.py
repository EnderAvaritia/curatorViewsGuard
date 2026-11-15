"""CLI 把 ntfy 新配置项接入 Notifier 的接线测试。

用假的 Notifier 捕获构造参数，验证：
- enabled / min_level / daily_summary / quiet_hours 按配置透传；
- queue_path 位于 db_path 同级目录、文件名为 notify_queue.json；
- dry-run 不补发排队通知，正式 run 前补发一次。
"""

from pathlib import Path

from curator_views import cli


CONFIG_TEXT = """\
clan_id: "45519015"
cookie_file: "missing_cookie.txt"
db_path: "views.db"
boost_enabled: false
boost_mode: stable
ntfy_url: "https://ntfy.example"
ntfy_topic: "steam-curator-views"
ntfy_enabled: false
ntfy_min_level: "warning"
ntfy_daily_summary: false
ntfy_quiet_hours: "23:00-08:00"
"""


class CapturingNotifier:
    instances = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.flush_calls = 0
        self.sent = []
        CapturingNotifier.instances.append(self)

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def flush_pending(self) -> int:
        self.flush_calls += 1
        return 0


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return config_path


def test_run_builds_notifier_from_config(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    CapturingNotifier.instances = []
    monkeypatch.setattr(cli, "Notifier", CapturingNotifier)
    # run_daily 是外部副作用入口（网络/数据库），测试里直接短路
    monkeypatch.setattr(cli, "run_daily", lambda *a, **k: {"dry_run": True})

    code = cli.main(
        ["run", "--config", str(config_path), "--no-boost", "--dry-run"]
    )

    assert code == 0
    assert len(CapturingNotifier.instances) == 1
    kwargs = CapturingNotifier.instances[0].kwargs
    assert kwargs["ntfy_url"] == "https://ntfy.example"
    assert kwargs["ntfy_topic"] == "steam-curator-views"
    assert kwargs["enabled"] is False
    assert kwargs["min_level"] == "warning"
    assert kwargs["daily_summary"] is False
    assert kwargs["quiet_hours"] == "23:00-08:00"

    queue_path = Path(kwargs["queue_path"])
    assert queue_path.name == "notify_queue.json"
    assert queue_path.parent == Path(tmp_path / "views.db").parent


def test_flush_pending_only_when_not_dry_run(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    CapturingNotifier.instances = []
    monkeypatch.setattr(cli, "Notifier", CapturingNotifier)
    monkeypatch.setattr(cli, "run_daily", lambda *a, **k: {"dry_run": False})

    # dry-run：不补发
    code = cli.main(
        ["run", "--config", str(config_path), "--no-boost", "--dry-run"]
    )
    assert code == 0
    assert CapturingNotifier.instances[0].flush_calls == 0

    # 正式 run：调用一次补发
    code = cli.main(["run", "--config", str(config_path), "--no-boost"])
    assert code == 0
    assert CapturingNotifier.instances[1].flush_calls == 1