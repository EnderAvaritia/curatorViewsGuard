"""cli._setup_logging 的单元测试：日志文件与控制台输出开关。

覆盖：
- 默认（run 模式）只写文件，不污染 stdout/stderr；
- console=True（daemon 模式）额外挂一个控制台 handler；
- 日志确实写入 curator_views.log。
"""

import logging

import pytest

from curator_views import cli


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """_setup_logging 会重建 root handlers，测试后恢复，避免影响其他用例。"""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            handler.close()
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _setup(tmp_path, console: bool):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("clan_id: '1'\n", encoding="utf-8")
    return cli._setup_logging(
        config_path, str(tmp_path / "views.db"), console=console
    )


class TestSetupLogging:
    def test_file_only_by_default(self, tmp_path):
        log_path = _setup(tmp_path, console=False)

        root = logging.getLogger()
        assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert not any(type(h) is logging.StreamHandler for h in root.handlers)
        assert log_path == tmp_path / "curator_views.log"

    def test_console_handler_added_when_requested(self, tmp_path):
        _setup(tmp_path, console=True)

        root = logging.getLogger()
        stream_handlers = [
            h for h in root.handlers if type(h) is logging.StreamHandler
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].formatter is not None

    def test_info_written_to_file(self, tmp_path):
        log_path = _setup(tmp_path, console=False)

        logging.getLogger("curator_views.test").info("hello-file")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert log_path.is_file()
        assert "hello-file" in log_path.read_text(encoding="utf-8")