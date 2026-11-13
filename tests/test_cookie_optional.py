"""未配置 / 缺失 cookie_file 时以未登录（游客）模式运行的行为测试。"""

from curator_views import cli
from curator_views.core.fetcher import Fetcher


class TestFetcherWithoutCookie:
    def test_empty_cookie_file_sends_no_cookie_header(self):
        fetcher = Fetcher("123", "")
        assert "Cookie" not in fetcher._session.headers

    def test_missing_cookie_file_sends_no_cookie_header(self, tmp_path):
        fetcher = Fetcher("123", str(tmp_path / "missing.txt"))
        assert "Cookie" not in fetcher._session.headers

    def test_existing_cookie_file_is_used(self, tmp_path):
        path = tmp_path / "cookie.txt"
        path.write_text("  steamLoginSecure=abc;  \n", encoding="utf-8")
        fetcher = Fetcher("123", str(path))
        assert fetcher._session.headers["Cookie"] == "steamLoginSecure=abc;"


class TestCliCookieCheck:
    def test_missing_cookie_is_not_fatal(self, tmp_path, capsys):
        assert cli._check_cookie_file(str(tmp_path / "missing.txt")) is False
        assert "未登录" in capsys.readouterr().err

    def test_empty_cookie_is_not_fatal(self, capsys):
        assert cli._check_cookie_file("") is False
        assert "未登录" in capsys.readouterr().err

    def test_existing_cookie_returns_true(self, tmp_path, capsys):
        path = tmp_path / "cookie.txt"
        path.write_text("a=b", encoding="utf-8")
        assert cli._check_cookie_file(str(path)) is True
        assert capsys.readouterr().err == ""

    def test_build_runtime_allows_missing_cookie_file_key(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(
            'clan_id: "123"\ndb_path: "views.db"\n', encoding="utf-8"
        )
        _, _, clan_id, cookie_file, _, _ = cli._build_runtime(config)
        assert clan_id == "123"
        assert cookie_file == ""