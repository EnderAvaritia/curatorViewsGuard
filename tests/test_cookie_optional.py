"""Cookie 相关行为测试：未登录回退与统计/刷量双 Cookie 解析。"""

from pathlib import Path

import pytest

from curator_views import cli
from curator_views.core.booster import Booster
from curator_views.core.fetcher import (
    CookieFileError,
    Fetcher,
    read_cookie_file,
)
from curator_views.core.storage import Storage


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


class TestCookieFileValidation:
    """Cookie 文件损坏时应给出可读错误，而不是 latin-1 编码异常。"""

    def test_read_cookie_file_returns_stripped_ascii_cookie(self, tmp_path):
        path = tmp_path / "cookie.txt"
        path.write_text("  a=1; b=2  \n", encoding="utf-8")
        assert read_cookie_file(str(path)) == "a=1; b=2"

    def test_truncated_cookie_with_ellipsis_raises_clear_error(self, tmp_path):
        path = tmp_path / "cookie.txt"
        path.write_text("a" * 512 + "…" + "b=2", encoding="utf-8")

        with pytest.raises(CookieFileError) as excinfo:
            Fetcher("123", str(path))

        message = str(excinfo.value)
        assert "第 513 个字符" in message
        assert "U+2026" in message
        assert "省略号" in message

    def test_newline_inside_cookie_raises_clear_error(self, tmp_path):
        path = tmp_path / "cookie.txt"
        path.write_text("a=1\nb=2", encoding="utf-8")

        with pytest.raises(CookieFileError) as excinfo:
            Fetcher("123", str(path))

        assert "U+000A" in str(excinfo.value)

    def test_non_utf8_cookie_file_raises_clear_error(self, tmp_path):
        path = tmp_path / "cookie.txt"
        path.write_bytes(b"\xff\xfe\x00")

        with pytest.raises(CookieFileError) as excinfo:
            Fetcher("123", str(path))

        assert "UTF-8" in str(excinfo.value)

    def test_booster_propagates_cookie_file_error(self, tmp_path):
        path = tmp_path / "cookie.txt"
        path.write_text("a=1…", encoding="utf-8")
        booster = Booster(
            clan_id="123",
            cookie_file=str(path),
            proxy=None,
            storage=Storage(str(tmp_path / "views.db")),
            batch_size=50,
            single_cap=300,
            threads=1,
            wait_min=0.0,
            wait_max=0.0,
            max_retries=3,
            target_ratio=0.95,
            distribution="ratio",
        )

        with pytest.raises(CookieFileError):
            booster._load_cookie()


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
    def test_boost_does_not_fall_back_to_stats_cookie(self, tmp_path):
        # boost_cookie_file 留空 → 刷量走游客模式，不复用统计 Cookie
        stats, boost = cli._resolve_cookie_paths(
            {"cookie_file": "stats.txt"}, tmp_path
        )
        assert stats == str(tmp_path / "stats.txt")
        assert boost == ""

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