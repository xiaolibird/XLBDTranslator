# -*- coding: utf-8 -*-
"""scripts/search_pubs.py 纯函数离线回归（不联网）。

锁三件容易悄悄退化的东西：
1. DOI / arXiv / 标题的归一化契约（与仓库其它模块对齐）；
2. keeper 覆盖 duplicate 的写入顺序（同键相撞时命中报主札记 citekey），
   以及 duplicate-only 论文不被丢；
3. 索引损坏/结构诡异时静默降级为空、_in_library 三路优先级。

导入方式沿用 test_backfill.py：`python -m pytest` 时 cwd(repo root) 在 sys.path，
scripts 作为隐式命名空间包可直接 import；INDEX_PATH 是模块级常量，各测试 monkeypatch
指向 tmp_path 造的假索引，互不污染。
"""
import json

import pytest

import scripts.search_pubs as sp


# ---------------- 归一化契约 ----------------

@pytest.mark.parametrize("raw, expected", [
    ("https://doi.org/10.1000/AbC.123", "10.1000/abc.123"),
    ("http://doi.org/10.1/X", "10.1/x"),
    ("doi:10.1/Y", "10.1/y"),
    ("  10.1/Z  ", "10.1/z"),
    ("10.1/w.,;)", "10.1/w"),          # 去尾标点
    ("", ""),
    (None, ""),
])
def test_norm_doi(raw, expected):
    assert sp._norm_doi(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2203.00001v1", "2203.00001"),
    ("2203.00001V3", "2203.00001"),   # 大写 V 也剥（先 lower）
    ("2203.00001", "2203.00001"),     # 无版本后缀
    ("  2203.00001v12  ", "2203.00001"),
    ("", ""),
    (None, ""),
])
def test_norm_arxiv(raw, expected):
    assert sp._norm_arxiv(raw) == expected


def test_norm_title_ascii_and_cjk():
    assert sp._norm_title("Graph  Transformer, Molecular!") == "graph transformer molecular"
    # 纯 CJK → 归一化为空串（故 CJK 标题只能靠 DOI/arxiv 命中）
    assert sp._norm_title("缺失机制建模") == ""
    assert sp._norm_title("") == ""
    assert sp._norm_title(None) == ""


# ---------------- 索引装载：keeper / duplicate 顺序 ----------------

def _write_index(tmp_path, monkeypatch, papers):
    p = tmp_path / "literature_index.json"
    p.write_text(json.dumps({"papers": papers}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sp, "INDEX_PATH", p)
    return p


def test_keeper_overrides_duplicate_same_key(tmp_path, monkeypatch):
    # 同一 DOI 两条：duplicate 条目排在前、keeper 排在后 → keeper 覆盖，命中报主 citekey。
    # 两种输入顺序都测，证明覆盖靠 sorted(排序键 not duplicate_of) 而非文件里的先后。
    keeper = {"citekey": "keeperKey", "doi": "10.1/shared", "duplicate_of": None}
    dup = {"citekey": "dupKey", "doi": "10.1/shared", "duplicate_of": "keeperKey"}
    for order in ([keeper, dup], [dup, keeper]):
        _write_index(tmp_path, monkeypatch, order)
        keys = sp._load_index_keys()
        assert keys["10.1/shared"] == "keeperKey"


def test_duplicate_only_not_dropped(tmp_path, monkeypatch):
    # 唯一索引项就是 duplicate（有 8 篇真实如此）——不能跳过，否则漏标已收录。
    dup = {"citekey": "onlyDup", "doi": "10.1/lonely", "duplicate_of": "someMissingKeeper"}
    _write_index(tmp_path, monkeypatch, [dup])
    keys = sp._load_index_keys()
    assert keys["10.1/lonely"] == "onlyDup"


def test_empty_and_cjk_keys_skipped(tmp_path, monkeypatch):
    # 空 DOI / 纯 CJK 标题归一为空串 → 不得写出 keys[""]（否则空 DOI 的检索项误命中）。
    papers = [
        {"citekey": "cjk", "title": "缺失机制", "doi": "", "arxiv_id": None},
        {"citekey": "real", "title": "Real Paper", "doi": "10.1/real"},
    ]
    _write_index(tmp_path, monkeypatch, papers)
    keys = sp._load_index_keys()
    assert "" not in keys
    assert keys == {"10.1/real": "real", "real paper": "real"}


def test_all_three_key_types_indexed(tmp_path, monkeypatch):
    papers = [{"citekey": "ck", "doi": "10.5/A", "arxiv_id": "2501.00001v2",
               "title": "Some Title"}]
    _write_index(tmp_path, monkeypatch, papers)
    keys = sp._load_index_keys()
    assert keys["10.5/a"] == "ck"
    assert keys["2501.00001"] == "ck"
    assert keys["some title"] == "ck"


def test_missing_citekey_falls_back_to_qmark(tmp_path, monkeypatch):
    _write_index(tmp_path, monkeypatch, [{"doi": "10.1/nock"}])
    keys = sp._load_index_keys()
    assert keys["10.1/nock"] == "?"


# ---------------- 索引损坏 / 结构诡异：静默降级为空 ----------------

@pytest.mark.parametrize("content", [
    "not json at all {",          # 非法 JSON
    "[1, 2, 3]",                  # 顶层是 list（非 dict）
    '{"papers": [1, 2, 3]}',      # entry 是 int
    '{"papers": "nope"}',         # papers 是字符串
    '{"papers": null}',           # papers 为 null
    "{}",                          # 无 papers 字段
])
def test_corrupt_or_weird_index_degrades_to_empty(tmp_path, monkeypatch, content):
    p = tmp_path / "literature_index.json"
    p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(sp, "INDEX_PATH", p)
    assert sp._load_index_keys() == {}


def test_mixed_valid_and_junk_entries(tmp_path, monkeypatch):
    # dict 与非 dict 混在 papers 里：跳过垃圾、保留合法项，不崩。
    p = tmp_path / "literature_index.json"
    p.write_text(json.dumps({"papers": [
        {"citekey": "good", "doi": "10.1/good"}, 5, None, "junk",
    ]}), encoding="utf-8")
    monkeypatch.setattr(sp, "INDEX_PATH", p)
    assert sp._load_index_keys() == {"10.1/good": "good"}


def test_missing_index_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "INDEX_PATH", tmp_path / "does_not_exist.json")
    assert sp._load_index_keys() == {}


# ---------------- _in_library 三路优先级 ----------------

def test_in_library_doi_priority(tmp_path, monkeypatch):
    # DOI 先于 arxiv 先于 title：同 item 三键都在索引但指向不同 citekey，须报 DOI 的。
    keys = {"10.1/d": "byDoi", "2501.00002": "byArxiv", "the title": "byTitle"}
    item = {"doi": "https://doi.org/10.1/D", "arxiv_id": "2501.00002v1", "title": "The Title"}
    assert sp._in_library(item, keys) == "byDoi"


def test_in_library_arxiv_when_no_doi_match(tmp_path, monkeypatch):
    keys = {"2501.00002": "byArxiv", "the title": "byTitle"}
    item = {"doi": "10.1/unknown", "arxiv_id": "2501.00002v1", "title": "The Title"}
    assert sp._in_library(item, keys) == "byArxiv"


def test_in_library_title_last_resort(tmp_path, monkeypatch):
    keys = {"the title": "byTitle"}
    item = {"doi": "", "arxiv_id": None, "title": "The Title"}
    assert sp._in_library(item, keys) == "byTitle"


def test_in_library_miss_returns_none():
    keys = {"10.1/x": "x"}
    item = {"doi": "10.1/y", "arxiv_id": "", "title": "Unseen"}
    assert sp._in_library(item, keys) is None


def test_in_library_empty_fields_no_false_hit():
    # item 三字段全空 + 索引恰有空串键（防御：即使 _load 漏筛也不该命中）——
    # _in_library 自身对空串短路（`if doi and ...`），故返回 None。
    keys = {"": "shouldNotHit"}
    item = {"doi": "", "arxiv_id": "", "title": ""}
    assert sp._in_library(item, keys) is None


# ---------------- 结尾行按实际生效源生成（F6 文案修复） ----------------
# 此前写死「arXiv+PubMed 未去重」：单源检索或一源失败时既误导来源，又暗示做过
# 一次不存在的跨源合并（scholar-search skill 还把这行当判据引用）。契约：
# 只有 >1 个源实际成功才提「未去重」，失败源保留尾注。

def _fake_client(monkeypatch, *, arxiv=None, pubmed=None):
    """arxiv/pubmed: None=不该被调到；list=返回值；Exception 实例=抛出。"""

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def search_arxiv(self, *a, **kw):
            if isinstance(arxiv, Exception):
                raise arxiv
            assert arxiv is not None, "arXiv 不该被检索"
            return list(arxiv)

        def search_pubmed(self, *a, **kw):
            if isinstance(pubmed, Exception):
                raise pubmed
            assert pubmed is not None, "PubMed 不该被检索"
            return list(pubmed)

    monkeypatch.setattr(sp, "AcademicSearchClient", _Client)


def _item(title, source):
    return {"title": title, "source": source, "authors": [], "published": "2026-01-01"}


def _run_main(monkeypatch, capsys, argv):
    import sys as _sys
    monkeypatch.setattr(sp, "INDEX_PATH", __import__("pathlib").Path("/nonexistent/index.json"))
    monkeypatch.setattr(_sys, "argv", ["search_pubs.py"] + argv)
    sp.main()
    return capsys.readouterr()


def test_tail_single_source_omits_dedup_note(monkeypatch, capsys):
    _fake_client(monkeypatch, arxiv=[_item("A", "arXiv")])
    out = _run_main(monkeypatch, capsys, ["q", "--source", "arxiv"]).out
    assert "共 1 条" in out
    assert "未去重" not in out


def test_tail_all_sources_ok_keeps_dedup_note(monkeypatch, capsys):
    _fake_client(monkeypatch, arxiv=[_item("A", "arXiv")], pubmed=[_item("P", "PubMed")])
    out = _run_main(monkeypatch, capsys, ["q", "--source", "all"]).out
    assert "共 2 条" in out
    assert "arXiv+PubMed 未去重" in out


def test_tail_partial_failure_drops_dedup_note_keeps_failure(monkeypatch, capsys):
    _fake_client(monkeypatch, arxiv=[_item("A", "arXiv")], pubmed=RuntimeError("限流"))
    got = _run_main(monkeypatch, capsys, ["q", "--source", "all"])
    assert "共 1 条" in got.out
    assert "未去重" not in got.out               # 只剩一个成功源，没有"跨源未去重"可言
    assert "PubMed 检索失败已跳过" in got.out    # 失败尾注保留
