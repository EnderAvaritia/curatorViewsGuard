"""boost_list 模块测试：列表文件解析、peek 与“刷完一行删一行”。"""

import pytest

from curator_views.core.boost_list import (
    consume_next,
    peek_next,
    read_entries,
)


def write_list(tmp_path, text: str):
    path = tmp_path / "boost_list.txt"
    path.write_text(text, encoding="utf-8", newline="")
    return path


class TestReadEntries:
    def test_parses_plain_numbers(self, tmp_path):
        path = write_list(tmp_path, "500\n200\n100\n")
        assert read_entries(str(path)) == [(0, 500), (1, 200), (2, 100)]

    def test_ignores_blank_comment_invalid_and_negative(self, tmp_path):
        path = write_list(
            tmp_path,
            "# 注释\n\n500\nabc\n-10\n  # 缩进注释\n200\n",
        )
        assert read_entries(str(path)) == [(2, 500), (6, 200)]

    def test_supports_inline_comment(self, tmp_path):
        path = write_list(tmp_path, "300  # 周一\n150\t# 周二\n")
        assert read_entries(str(path)) == [(0, 300), (1, 150)]

    def test_zero_is_a_valid_entry(self, tmp_path):
        path = write_list(tmp_path, "0\n")
        assert read_entries(str(path)) == [(0, 0)]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_entries(str(tmp_path / "missing.txt"))


class TestPeekNext:
    def test_returns_first_amount_and_count(self, tmp_path):
        path = write_list(tmp_path, "# 头注释\n500\n200\n")
        assert peek_next(str(path)) == (500, 2)

    def test_empty_list_returns_none_zero(self, tmp_path):
        path = write_list(tmp_path, "# 只有注释\n\nabc\n")
        assert peek_next(str(path)) == (None, 0)

    def test_peek_does_not_modify_file(self, tmp_path):
        text = "500\n200\n"
        path = write_list(tmp_path, text)
        peek_next(str(path))
        assert path.read_text(encoding="utf-8") == text


class TestConsumeNext:
    def test_removes_first_valid_line_only(self, tmp_path):
        path = write_list(tmp_path, "# 头注释\n500\n200\n")
        assert consume_next(str(path)) == 500
        assert path.read_text(encoding="utf-8") == "# 头注释\n200\n"

    def test_removes_inline_comment_line(self, tmp_path):
        path = write_list(tmp_path, "300 # 周一\n150\n")
        assert consume_next(str(path)) == 300
        assert path.read_text(encoding="utf-8") == "150\n"

    def test_preserves_crlf_line_endings(self, tmp_path):
        path = write_list(tmp_path, "500\r\n200\r\n")
        consume_next(str(path))
        assert path.read_bytes() == b"200\r\n"

    def test_empty_list_raises_value_error(self, tmp_path):
        path = write_list(tmp_path, "# 没有数字\n")
        with pytest.raises(ValueError):
            consume_next(str(path))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            consume_next(str(tmp_path / "missing.txt"))