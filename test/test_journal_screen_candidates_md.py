# -*- coding: utf-8 -*-
"""回归 journal_screen.py:518 —— 候选列表 md 顶部降量链路表缺 Markdown 分隔行，
整块统计在任何渲染器里都显示为裸管道符文本（GFM/CommonMark 表格要求表头行紧跟
`|---|---|` 分隔行，否则整块不构成表格）。

附带回归：filter_method == "keyword_only" 时 _keyword_only_result 此前不返回
after_blacklist，scripts/screen_journal.py 的「输入论文: {}」在 --no-llm 下会打成 `?`。
"""
from pathlib import Path

from src.scholar.journal_screen import JournalScreener, _esc_cell
from src.scholar.schema import ScholarSettings

MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _settings(tmp_path: Path) -> ScholarSettings:
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    s = ScholarSettings.from_env_file(env_file)
    s.processing.output_dir = tmp_path / "out"
    return s


def _make_screener(tmp_path):
    return JournalScreener(tmp_path / "screen_out", _settings(tmp_path))


def _search_results(kept, total_pubmed=37, blacklist_excluded=12, journal="npj Digit Med"):
    return {
        "kept": kept,
        "journal": journal,
        "date_range": ("2020-01-01", "2026-01-01"),
        "total_pubmed": total_pubmed,
        "blacklist_excluded": blacklist_excluded,
    }


def _item(title):
    return {"title": title, "abstract": "abs", "authors": ["A B"], "doi": "10.1/x",
           "journal": "npj Digit Med", "published": None, "source": "pubmed"}


def test_candidates_md_header_block_is_valid_markdown_table(tmp_path):
    """顶部降量链路表头行之后必须紧跟 `|---|---|` 分隔行，否则渲染器把整块当纯文本。"""
    screener = _make_screener(tmp_path)
    result = screener.run_filter(_search_results([], total_pubmed=37, blacklist_excluded=37))
    _json_path, md_path = screener.export_candidates(result)
    lines = md_path.read_text(encoding="utf-8").splitlines()

    header_idx = next(i for i, l in enumerate(lines) if l.startswith("| ") and "|" in l[2:])
    assert lines[header_idx + 1].strip().replace(" ", "") in ("|---|---|", "|:-|:-|", "|-|-|") \
        or set(lines[header_idx + 1].strip()) <= set("|-: ")
    # 分隔行必须是纯 -/: 组成（GFM 表格分隔行语法），不能是又一行数据
    assert all(ch in "|-: " for ch in lines[header_idx + 1])


def test_keyword_only_result_carries_after_blacklist(tmp_path):
    """--no-llm 路径也要有 after_blacklist——否则 scripts/screen_journal.py 的
    「输入论文: {}」会打成 `?`（其余两条骨架 _empty_result / LLM 路径都有这个键）。"""
    screener = _make_screener(tmp_path)
    items = [_item("EHR missingness study for ICU risk prediction")]
    result = screener.run_filter(
        _search_results(items, total_pubmed=40, blacklist_excluded=3),
        llm_filter=False)

    assert result["filter_method"] == "keyword_only"
    assert result["total_pubmed"] == 40
    assert result["blacklist_excluded"] == 3
    assert result["after_blacklist"] == 37   # 此前缺失，get(..., "?") 会兜成 "?"


def test_keyword_only_candidates_md_also_has_separator_row(tmp_path):
    """--no-llm 产出的 md 同样要有合法表格（覆盖 keyword_only 这条路径的降量表）。"""
    screener = _make_screener(tmp_path)
    items = [_item("EHR missingness study for ICU risk prediction")]
    result = screener.run_filter(
        _search_results(items, total_pubmed=40, blacklist_excluded=3),
        llm_filter=False)
    _json_path, md_path = screener.export_candidates(result)
    text = md_path.read_text(encoding="utf-8")

    assert "| 项目 | 值 |" in text
    assert "|---|---|" in text
    assert "PubMed 总数 | 40" in text


def test_esc_cell_escapes_pipe_and_newline():
    """_esc_cell 是候选表所有自由文本列共用的转义函数：裸 `|` 转义、换行压成空格、None 兜空串。"""
    assert _esc_cell("A|B") == "A\\|B"
    assert _esc_cell("line1\nline2") == "line1 line2"
    assert _esc_cell(None) == ""
    assert _esc_cell(123) == "123"


def test_candidate_row_escapes_pipe_in_one_line_bucket_role(tmp_path):
    """回归 opus 补审建议3：journal_screen.py 候选表此前只给 title 转义了裸 `|`，
    one_line（LLM 自由文本，最容易带竖线）/ bucket / role / flags 没做，含 `|` 时
    会把该行错列（后面所有列漂移）。这几列现在复用同一个 _esc_cell。"""
    screener = _make_screener(tmp_path)
    result = {
        "journal": "npj Digit Med", "generated_at": "", "date_range": ["2020", "2026"],
        "filter_method": "llm", "total_pubmed": 1, "blacklist_excluded": 0,
        "keyword_prefilter_skipped": 0, "candidates_count": 1,
        "filter_stats": {},
        "candidates": [{
            "title": "Normal title", "year": 2024, "decision": "INCLUDE",
            "bucket": ["A|B"], "role": "CITE|SUPPORT",
            "one_line": "两种情形 A|B 都适用", "confidence": 0.8,
            "in_library": False, "flags": ["THREAT|X"],
        }],
    }
    md_path = tmp_path / "cand.md"
    screener._write_candidates_md(result, md_path)
    text = md_path.read_text(encoding="utf-8")
    data_row = next(l for l in text.splitlines() if l.startswith("| 1 |"))

    assert "A\\|B" in data_row          # bucket 里的裸 | 被转义
    assert "CITE\\|SUPPORT" in data_row  # role 同样转义
    assert "两种情形 A\\|B 都适用" in data_row  # one_line 同样转义
    assert "THREAT\\|X" in data_row      # flags 同样转义
