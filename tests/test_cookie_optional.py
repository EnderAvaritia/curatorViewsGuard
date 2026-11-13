"""Cookie 相关行为测试：未登录回退与统计/刷量双 Cookie 解析。"""

from pathlib import Path

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
        _, _, clan_id, cookie_file, boost_cookie, _, _ = cli._build_runtime(
            config
        )
        assert clan_id == "123"
        assert cookie_file == ""
        assert boost_cookie == ""


class TestResolveCookiePaths:
    def test_boost_falls_back_to_stats_cookie(self, tmp_path):
        stats, boost = cli._resolve_cookie_paths(
            {"cookie_file": "stats.txt"}, tmp_path
        )
        assert stats == str(tmp_path / "stats.txt")
        assert boost == stats

    def test_separate_cookies(self, tmp_path):
        stats, boost = cli._resolve_cookie_paths(
            {
                "cookie_file": "stats.txt",
                "boost_cookie_file": "boost.txt",
            },
            tmp_path,
        )
        assert stats == str(tmp_path / "stats.txt")
        assert boost == str(tmp_path / "boost.txt")

    def test_boost_only_cookie(self, tmp_path):
        stats, boost = cli._resolve_cookie_paths(
            {"boost_cookie_file": "boost.txt"}, tmp_path
        )
        assert stats == ""
        assert boost == str(tmp_path / "boost.txt")

    def test_both_unset(self, tmp_path):
        assert cli._resolve_cookie_paths({}, tmp_path) == ("", "")

    def test_build_runtime_returns_separate_cookies(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(
            'clan_id: "123"\n'
            'cookie_file: "stats.txt"\n'
            'boost_cookie_file: "boost.txt"\n',
            encoding="utf-8",
        )
        (
            _cfg,
            _cfg_dir,
            _clan_id,
            stats_cookie,
            boost_cookie,
            _db,
            _proxy,
        ) = cli._build_runtime(config)
        assert Path(stats_cookie) == tmp_path / "stats.txt"
        assert Path(boost_cookie) == tmp_path / "boost.txt"