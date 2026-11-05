"""SQL 文本扫描统一底座 services/sql_text.py 的单元测试。

二期 2a 合并前的背景：metrics / time_boundary / sql_guard 各有一份扫描，
三份在"未闭合引号/注释怎么处置"上口径不一（不遮 / 整段保护 / 抛错）。
合并时与用户确认统一为"判读不懂就不动 SQL"（2026-11-05），本文件同时钉两件事：
1) 三份旧投影的既有行为原样保留（等长遮罩、保护区间、双视图折叠）；
2) 未闭合的新口径（三个投影一致抛 SqlScanError，消费方收敛成不动 SQL）。
消费方接入后的端到端痕迹见 test_metrics.py / test_time_boundary.py / test_sql_guard.py 各自小节。
"""

from __future__ import annotations

import pytest

from data_agent.services.sql_text import SqlScanError, mask_sql, protected_spans, scan_views


class TestMaskSql:
    def test_preserves_length(self):
        sql = "SELECT 'paid' AS x FROM t -- c\n/* b */ WHERE a=1"
        assert len(mask_sql(sql)) == len(sql)

    def test_masks_single_quote_string_including_quotes(self):
        assert mask_sql("WHERE status='paid'") == "WHERE status=      "

    def test_masks_double_and_backtick_quotes(self):
        assert mask_sql('WHERE "col" = `val`') == "WHERE       =      "

    def test_doubled_quote_is_escape_not_close(self):
        # 'it''s' 是一整段字符串（7 个字符），全部遮掉
        assert mask_sql("WHERE name='it''s'") == "WHERE name=" + " " * len("'it''s'")

    def test_string_interior_whitespace_kept(self):
        # 旧 metrics 口径：字符串内部的空白原样保留（串内换行不折成空格）
        assert mask_sql("WHERE note='a\n b'") == "WHERE note=  \n   "

    def test_masks_bracket_identifier(self):
        assert mask_sql("SELECT [order no] FROM t") == "SELECT" + " " * (
            1 + len("[order no]") + 1
        ) + "FROM t"

    def test_line_comment_masked_newline_kept(self):
        assert mask_sql("SELECT 1 --注释\nWHERE 1") == "SELECT 1     \nWHERE 1"

    def test_block_comment_masked_including_markers(self):
        assert mask_sql("SELECT /* c */ 1") == "SELECT" + " " * (1 + len("/* c */") + 1) + "1"

    def test_single_dash_is_not_comment(self):
        assert mask_sql("SELECT a-b FROM t") == "SELECT a-b FROM t"

    def test_unclosed_anything_raises(self):
        # 新口径（原 metrics 是"不遮继续跑"）：未闭合=判读不懂，抛错让调用方不动 SQL
        for sql in ("SELECT 'x", 'SELECT "x', "SELECT `x", "SELECT [x", "SELECT /* x"):
            with pytest.raises(SqlScanError):
                mask_sql(sql)


class TestProtectedSpans:
    def test_closed_string_span_includes_both_quotes(self):
        sql = "WHERE status='paid'"
        start = sql.index("'")
        assert (start, len(sql)) in protected_spans(sql)

    def test_escaped_doubled_quote_stays_one_span(self):
        sql = "'it''s'"
        assert protected_spans(sql) == [(0, len(sql))]

    def test_bracket_identifier_span_includes_brackets(self):
        sql = "SELECT [a b] FROM t"
        start = sql.index("[")
        end = sql.index("]") + 1
        assert (start, end) in protected_spans(sql)

    def test_line_comment_stops_before_newline(self):
        sql = "SELECT 1 -- c\nWHERE 1"
        assert (sql.index("--"), sql.index("\n")) in protected_spans(sql)

    def test_block_comment_span_includes_markers(self):
        sql = "SELECT 1 /* c */ WHERE 1"
        assert (sql.index("/*"), sql.index("*/") + 2) in protected_spans(sql)

    def test_unclosed_quote_raises(self):
        # 新口径（原 time_boundary 是"后面整段保护"）：整句读不懂，抛错
        for sql in ("SELECT 'x", "SELECT [x", "SELECT /* x"):
            with pytest.raises(SqlScanError):
                protected_spans(sql)


class TestScanViews:
    def test_string_collapsed_both_views(self):
        structure, calls = scan_views("SELECT 'delete from t' ")
        assert "delete" not in structure
        assert "delete" not in calls
        assert "''" in structure and "''" in calls

    def test_quoted_identifier_folds_to_ident_in_structure(self):
        structure, _calls = scan_views('SELECT "delete" FROM orders')
        assert structure == "SELECT ident FROM orders"

    def test_quoted_identifier_content_visible_in_calls_view(self):
        # 加引号躲不过函数检查："load_extension"(...) 仍会被调用视图抓到
        _structure, calls = scan_views('SELECT "load_extension"(x)')
        assert "load_extension" in calls

    def test_backtick_identifier_same_as_double_quote(self):
        structure, calls = scan_views("SELECT `foo bar` FROM t")
        assert structure.startswith("SELECT ident FROM t")
        assert "foo bar" in calls

    def test_bracket_identifier(self):
        structure, calls = scan_views("SELECT [a] FROM t")
        assert "ident" in structure
        assert "[a]" not in structure
        assert "a" in calls

    def test_comments_collapse_to_single_space(self):
        # guard 视图不保长度：注释整体折成一个空格、行尾换行被吃掉；
        # 注释前的原有空格保留，所以 SELECT 1 与 FROM 之间是两个空格
        structure, calls = scan_views("SELECT 1 --c\nFROM t")
        assert structure == calls == "SELECT 1  FROM t"
        assert len(structure) < len("SELECT 1 --c\nFROM t")

    def test_block_comment_collapsed(self):
        structure, _calls = scan_views("SELECT /* x */ 1")
        assert structure == "SELECT   1"

    def test_doubled_quote_unescaped_in_content(self):
        _structure, calls = scan_views('SELECT "a""b"')
        assert 'a"b' in calls

    def test_unclosed_anything_raises(self):
        for sql in ("SELECT 'x", 'SELECT "x', "SELECT `x", "SELECT [x", "SELECT /* x"):
            with pytest.raises(SqlScanError):
                scan_views(sql)
