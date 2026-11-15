"""cli check 子命令测试：抓取统计后写入本地库（--no-write 保留只读行为）。"""

from curator_views import cli
from curator_views.core.storage import Storage

CONFIG_TEXT = """\
clan_id: "45519015"
cookie_file: "missing_cookie.txt"
db_path: "views.db"
boost_enabled: false
boost_mode: stable
"""

STATS = [
    {"date_ts": 1794528000, "num_views": 5449},
    {"date_ts": 1794614400, "num_views": 1181},
]


class FakeFetcher:
    """替代 Fetcher：不发请求，返回固定统计。"""

    def __init__(self, *args, **kwargs):
        pass

    def fetch_stats(self):
        return [dict(item) for item in STATS]


def write_config(tmp_path, db_path="views.db"):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.replace('"views.db"', f'"{db_path}"'), encoding="utf-8"
    )
    return config_path


class TestCheckWrites:
    def test_writes_all_fetched_days(self, tmp_path, monkeypatch, capsys):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(cli, "Fetcher", FakeFetcher)

        code = cli.main(["check", "--config", str(config_path)])

        assert code == 0
        out = capsys.readouterr().out
        assert "共获取到 2 天的统计数据。" in out
        assert "已写入数据库" in out
        assert "最新 2026-11-14 = 1,181 次" in out
        assert "预测摘要" in out

        db_path = tmp_path / "views.db"
        assert db_path.is_file()
        storage = Storage(str(db_path))
        try:
            assert storage.get_daily_stats() == STATS
        finally:
            storage._conn.close()

    def test_overwrites_existing_values(self, tmp_path, monkeypatch):
        db_path = tmp_path / "views.db"
        storage = Storage(str(db_path))
        storage.upsert_daily_stats(1794614400, 9999)
        storage._conn.close()

        config_path = write_config(tmp_path)
        monkeypatch.setattr(cli, "Fetcher", FakeFetcher)
        assert cli.main(["check", "--config", str(config_path)]) == 0

        storage = Storage(str(db_path))
        try:
            assert storage.get_daily_stats() == STATS
        finally:
            storage._conn.close()

    def test_does_not_write_run_state(self, tmp_path, monkeypatch):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(cli, "Fetcher", FakeFetcher)
        assert cli.main(["check", "--config", str(config_path)]) == 0

        storage = Storage(str(tmp_path / "views.db"))
        try:
            assert storage.load_state("program_start") == ""
            assert storage.load_state("last_run_date") == ""
            assert storage.load_state("last_stats_date") == ""
        finally:
            storage._conn.close()


class TestCheckNoWrite:
    def test_no_write_skips_database(self, tmp_path, monkeypatch, capsys):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(cli, "Fetcher", FakeFetcher)

        code = cli.main(
            ["check", "--config", str(config_path), "--no-write"]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "只读模式" in out
        assert "已写入数据库" not in out
        assert not (tmp_path / "views.db").exists()


class TestCheckWriteFailure:
    def test_unwritable_db_path_returns_error(
        self, tmp_path, monkeypatch, capsys
    ):
        # db_path 指向一个普通文件的子路径，SQLite 必然打不开
        blocker = tmp_path / "blocked"
        blocker.write_text("x", encoding="utf-8")
        config_path = write_config(tmp_path, db_path="blocked/views.db")
        monkeypatch.setattr(cli, "Fetcher", FakeFetcher)

        code = cli.main(["check", "--config", str(config_path)])

        assert code == 1
        assert "写入数据库失败" in capsys.readouterr().err