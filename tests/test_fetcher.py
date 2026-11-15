"""Fetcher 的统计抓取错误分类测试：Cookie 失效要能和普通失败区分开。"""

import pytest

from curator_views.core.fetcher import Fetcher, StatsAuthError, STATS_URL


class FakeResponse:
    def __init__(self, status_code=200, text="", url=""):
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.requested_url = None

    def get(self, url, timeout=None):
        self.requested_url = url
        return self._response


def make_fetcher(response):
    fetcher = Fetcher("45519015", cookie_file="")
    fetcher._session = FakeSession(response)
    return fetcher


STATS_HTML = '<script>var g_StatsAppViews = [{"date": 1794614400, "num_views": 1181}];</script>'


class TestFetchStatsAuthErrors:
    def test_redirect_to_public_page_raises_auth_error(self):
        fetcher = make_fetcher(
            FakeResponse(
                status_code=200,
                text="<html>public curator page</html>",
                url="https://steamcommunity.com/curator/45519015/",
            )
        )

        with pytest.raises(StatsAuthError):
            fetcher.fetch_stats()

    def test_http_403_raises_auth_error(self):
        url = STATS_URL.format(clan_id="45519015")
        fetcher = make_fetcher(FakeResponse(status_code=403, url=url))

        with pytest.raises(StatsAuthError):
            fetcher.fetch_stats()

    def test_http_500_is_not_auth_error(self):
        url = STATS_URL.format(clan_id="45519015")
        fetcher = make_fetcher(FakeResponse(status_code=500, url=url))

        with pytest.raises(RuntimeError) as excinfo:
            fetcher.fetch_stats()
        assert not isinstance(excinfo.value, StatsAuthError)

    def test_admin_page_without_stats_is_not_auth_error(self):
        url = STATS_URL.format(clan_id="45519015")
        fetcher = make_fetcher(
            FakeResponse(status_code=200, text="<html></html>", url=url)
        )

        with pytest.raises(RuntimeError) as excinfo:
            fetcher.fetch_stats()
        assert not isinstance(excinfo.value, StatsAuthError)


class TestFetchStatsSuccess:
    def test_parses_stats_entries(self):
        url = STATS_URL.format(clan_id="45519015")
        fetcher = make_fetcher(
            FakeResponse(status_code=200, text=STATS_HTML, url=url)
        )

        assert fetcher.fetch_stats() == [
            {"date_ts": 1794614400, "num_views": 1181}
        ]