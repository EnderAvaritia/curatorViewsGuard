"""notifier 模块单元测试：级别过滤、每日简报开关、静默时段队列与补发。

全部用例通过 monkeypatch 替换 ``requests.post`` 与注入固定 ``now_fn``，
不发起任何真实网络请求，也不依赖系统时区/当前时间。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from curator_views.core import notifier as notifier_module
from curator_views.core import status
from curator_views.core.notifier import (
    CATEGORY_ALERT,
    CATEGORY_DAILY_SUMMARY,
    MAX_PENDING,
    Notifier,
    in_quiet_hours,
    parse_quiet_ranges,
)

URL = "https://ntfy.example"
TOPIC = "curator-views"


def at(hour: int, minute: int = 0) -> datetime:
    """构造一个固定的 Asia/Shanghai 时间（2026-11-15）。"""
    return datetime(2026, 11, 15, hour, minute, tzinfo=status._TZ)


class Recorder:
    """替代 ``requests.post``，记录调用并可指定第 N 次调用抛错。"""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_on = set(fail_on or ())

    def __call__(self, url, data=None, params=None, timeout=None):
        self.calls.append(
            {"url": url, "data": data, "params": params, "timeout": timeout}
        )
        if len(self.calls) in self.fail_on:
            raise RuntimeError("模拟推送失败")


def make_notifier(
    monkeypatch: pytest.MonkeyPatch,
    recorder: Recorder | None = None,
    **kwargs,
) -> tuple[Notifier, Recorder]:
    """构造一个已替换 requests.post 的 Notifier（默认正午、非静默）。"""
    recorder = recorder or Recorder()
    monkeypatch.setattr(notifier_module.requests, "post", recorder)
    kwargs.setdefault("ntfy_url", URL)
    kwargs.setdefault("ntfy_topic", TOPIC)
    kwargs.setdefault("now_fn", lambda: at(12, 0))
    return Notifier(**kwargs), recorder


def titles(recorder: Recorder) -> list[str]:
    """按调用顺序返回推送标题。"""
    return [call["params"]["title"] for call in recorder.calls]


def read_queue(path: Path) -> dict:
    """读取队列 JSON。"""
    return json.loads(path.read_text(encoding="utf-8"))


# ── 默认行为与级别映射 ───────────────────────────────────────────────────


def test_defaults_post_once_with_expected_payload(monkeypatch):
    notifier, recorder = make_notifier(monkeypatch)

    notifier.send("标题", "正文")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{URL}/{TOPIC}"
    assert call["params"] == {"title": "标题", "priority": "default"}
    assert call["data"] == "正文".encode("utf-8")
    assert call["timeout"] == 10


@pytest.mark.parametrize(
    ("level", "priority"),
    [("info", "default"), ("warning", "high"), ("error", "urgent")],
)
def test_priority_mapping(monkeypatch, level, priority):
    notifier, recorder = make_notifier(monkeypatch)

    notifier.send("标题", "正文", level=level)

    assert recorder.calls[0]["params"]["priority"] == priority


def test_min_level_filters_lower_levels(monkeypatch):
    notifier, recorder = make_notifier(monkeypatch, min_level="warning")

    notifier.send("信息", "正文", level="info")
    notifier.send("警告", "正文", level="warning")
    notifier.send("错误", "正文", level="error")

    assert titles(recorder) == ["警告", "错误"]


def test_invalid_min_level_falls_back_to_info(monkeypatch, caplog):
    with caplog.at_level(
        logging.WARNING, logger="curator_views.notifier"
    ):
        notifier, recorder = make_notifier(monkeypatch, min_level="debug")

    assert notifier.min_level == "info"
    assert "回退为 info" in caplog.text

    notifier.send("信息", "正文", level="info")
    assert len(recorder.calls) == 1


def test_daily_summary_disabled_suppresses_only_routine_brief(monkeypatch):
    notifier, recorder = make_notifier(monkeypatch, daily_summary=False)

    # 日常简报（info 级）被丢弃
    notifier.send(
        "日常简报", "正文", level="info", category=CATEGORY_DAILY_SUMMARY
    )
    assert recorder.calls == []

    # 升级为警告/错误的简报与普通告警照常送达
    notifier.send(
        "简报-异常", "正文", level="warning", category=CATEGORY_DAILY_SUMMARY
    )
    notifier.send(
        "简报-严重", "正文", level="error", category=CATEGORY_DAILY_SUMMARY
    )
    notifier.send(
        "普通告警", "正文", level="info", category=CATEGORY_ALERT
    )
    assert titles(recorder) == ["简报-异常", "简报-严重", "普通告警"]


def test_invalid_level_treated_as_info(monkeypatch, caplog):
    with caplog.at_level(
        logging.WARNING, logger="curator_views.notifier"
    ):
        notifier, recorder = make_notifier(monkeypatch)
        notifier.send("标题", "正文", level="fatal")

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["params"]["priority"] == "default"
    assert "无效" in caplog.text


# ── 静默时段解析与判断 ───────────────────────────────────────────────────


def test_parse_quiet_ranges_single():
    assert parse_quiet_ranges("23:00-08:00") == [(1380, 480)]


@pytest.mark.parametrize(
    "value",
    [
        "12:00-13:00,23:00-08:00",
        "12:00-13:00;23:00-08:00",
        "12:00-13:00 23:00-08:00",
        " 12:00-13:00 ; 23:00-08:00 ",
    ],
)
def test_parse_quiet_ranges_multiple_and_separators(value):
    assert parse_quiet_ranges(value) == [(720, 780), (1380, 480)]


def test_parse_quiet_ranges_invalid_tokens_are_skipped(caplog):
    with caplog.at_level(
        logging.WARNING, logger="curator_views.notifier"
    ):
        result = parse_quiet_ranges("坏格式,25:00-08:00,12:00-99:00,12:00")

    assert result == []
    assert "静默时段条目" in caplog.text


def test_parse_quiet_ranges_start_equals_end_is_skipped(caplog):
    with caplog.at_level(
        logging.WARNING, logger="curator_views.notifier"
    ):
        result = parse_quiet_ranges("08:00-08:00")

    assert result == []
    assert "起止相同" in caplog.text


def test_parse_quiet_ranges_empty():
    assert parse_quiet_ranges("") == []
    assert parse_quiet_ranges("   ") == []


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(11, 59, False), (12, 0, True), (12, 59, True), (13, 0, False)],
)
def test_in_quiet_hours_same_day(hour, minute, expected):
    ranges = parse_quiet_ranges("12:00-13:00")

    assert in_quiet_hours(at(hour, minute), ranges) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(23, 30, True), (7, 59, True), (8, 0, False), (12, 0, False)],
)
def test_in_quiet_hours_cross_midnight(hour, minute, expected):
    ranges = parse_quiet_ranges("23:00-08:00")

    assert in_quiet_hours(at(hour, minute), ranges) is expected


def test_in_quiet_hours_without_ranges():
    assert in_quiet_hours(at(23, 30), []) is False


# ── 静默时段排队与补发 ───────────────────────────────────────────────────


def test_quiet_hours_enqueue_instead_of_posting(monkeypatch, tmp_path):
    queue = tmp_path / "queue.json"
    notifier, recorder = make_notifier(
        monkeypatch,
        quiet_hours="23:00-08:00",
        queue_path=str(queue),
        now_fn=lambda: at(23, 30),
    )

    notifier.send(
        "夜间告警", "正文", level="warning", category=CATEGORY_ALERT
    )

    assert recorder.calls == []
    data = read_queue(queue)
    assert data["version"] == 1
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["title"] == "夜间告警"
    assert item["message"] == "正文"
    assert item["level"] == "warning"
    assert item["category"] == CATEGORY_ALERT
    assert item["created_at"] == "2026-11-15T23:30:00+08:00"


def test_flush_pending_after_quiet_delivers_fifo(monkeypatch, tmp_path):
    queue = tmp_path / "queue.json"
    clock = {"now": at(23, 30)}
    notifier, recorder = make_notifier(
        monkeypatch,
        quiet_hours="23:00-08:00",
        queue_path=str(queue),
        now_fn=lambda: clock["now"],
    )
    notifier.send("第一条", "m1", level="warning")
    notifier.send("第二条", "m2", level="warning")
    assert recorder.calls == []

    clock["now"] = at(9, 0)
    sent = notifier.flush_pending()

    assert sent == 2
    assert titles(recorder) == ["第一条", "第二条"]
    assert not queue.exists()


def test_flush_pending_while_still_quiet_keeps_queue(monkeypatch, tmp_path):
    queue = tmp_path / "queue.json"
    notifier, recorder = make_notifier(
        monkeypatch,
        quiet_hours="23:00-08:00",
        queue_path=str(queue),
        now_fn=lambda: at(23, 30),
    )
    notifier.send("第一条", "m1", level="warning")
    notifier.send("第二条", "m2", level="warning")

    assert notifier.flush_pending() == 0
    assert recorder.calls == []
    assert [i["title"] for i in read_queue(queue)["items"]] == [
        "第一条",
        "第二条",
    ]


def test_flush_pending_stops_at_first_failure(monkeypatch, tmp_path):
    queue = tmp_path / "queue.json"
    clock = {"now": at(23, 30)}
    notifier, recorder = make_notifier(
        monkeypatch,
        quiet_hours="23:00-08:00",
        queue_path=str(queue),
        now_fn=lambda: clock["now"],
    )
    for index in range(1, 4):
        notifier.send(f"第{index}条", f"m{index}", level="warning")

    clock["now"] = at(9, 0)
    recorder.fail_on = {2}
    sent = notifier.flush_pending()

    assert sent == 1
    remaining = [i["title"] for i in read_queue(queue)["items"]]
    assert remaining == ["第2条", "第3条"]


def test_send_outside_quiet_flushes_backlog_first(monkeypatch, tmp_path):
    queue = tmp_path / "queue.json"
    clock = {"now": at(23, 30)}
    notifier, recorder = make_notifier(
        monkeypatch,
        quiet_hours="23:00-08:00",
        queue_path=str(queue),
        now_fn=lambda: clock["now"],
    )
    notifier.send("旧1", "m1", level="warning")
    notifier.send("旧2", "m2", level="warning")

    clock["now"] = at(9, 0)
    notifier.send("新通知", "m3", level="warning")

    assert titles(recorder) == ["旧1", "旧2", "新通知"]
    assert not queue.exists()


def test_queue_cap_keeps_newest_items(monkeypatch, tmp_path, caplog):
    queue = tmp_path / "queue.json"
    notifier, _ = make_notifier(
        monkeypatch,
        quiet_hours="23:00-08:00",
        queue_path=str(queue),
        now_fn=lambda: at(23, 30),
    )

    with caplog.at_level(
        logging.WARNING, logger="curator_views.notifier"
    ):
        for index in range(MAX_PENDING + 3):
            notifier.send(f"t{index}", "正文", level="warning")

    items = read_queue(queue)["items"]
    assert len(items) == MAX_PENDING
    assert items[0]["title"] == "t3"
    assert items[-1]["title"] == f"t{MAX_PENDING + 2}"
    assert "丢弃最旧的" in caplog.text


# ── 开关与容错 ───────────────────────────────────────────────────────────


def test_disabled_writes_log_only(monkeypatch, tmp_path, caplog):
    queue = tmp_path / "queue.json"

    with caplog.at_level(
        logging.INFO, logger="curator_views.notifier"
    ):
        notifier, recorder = make_notifier(
            monkeypatch, enabled=False, queue_path=str(queue)
        )
        notifier.send("标题", "正文")

    assert recorder.calls == []
    assert not queue.exists()
    assert "[标题] 正文" in caplog.text


@pytest.mark.parametrize(
    ("url", "topic", "expected"),
    [
        ("", TOPIC, False),
        (URL, "", False),
        (URL, TOPIC, True),
    ],
)
def test_enabled_requires_url_and_topic(monkeypatch, url, topic, expected):
    monkeypatch.setattr(
        notifier_module.requests, "post", Recorder()
    )

    assert Notifier(ntfy_url=url, ntfy_topic=topic).enabled is expected


def test_corrupt_queue_does_not_raise(monkeypatch, tmp_path):
    queue = tmp_path / "queue.json"
    queue.write_text("{oops", encoding="utf-8")
    notifier, recorder = make_notifier(monkeypatch, queue_path=str(queue))

    assert notifier.flush_pending() == 0
    assert recorder.calls == []