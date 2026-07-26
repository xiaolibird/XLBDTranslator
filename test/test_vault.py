# -*- coding: utf-8 -*-
"""Obsidian vault 生成器回归。

假数据一律用 `notes.write_notes()` 真生成月度 md（而非手拼字符串），再喂 `notes_index.update_index()`
——这样 `notes._paper_section()` 一改格式，切片测试会和 `test_notes_index.test_roundtrip_md_parse`
同时挂，提醒同步。

锁三件最危险的东西：
1. 切片终止条件必须含 `^# `，否则最后一篇会把 `# 参考文献` + `::: {#refs}` 吞进正文；
2. 用户手写内容（`## 我的札记` 与自加 frontmatter 键/tag）在重建后 byte-for-byte 保留；
   哨兵被删或 YAML 改坏时**绝不覆盖**，改写 .conflict.md；
3. kNN 邻居不得指向入选集之外（否则 Obsidian 图上出现幽灵节点）。
"""
import json
from datetime import date

import pytest
import yaml

from src.scholar.schema import (
    PaperSegment, PaperMetadata, FilterDecision,
    CloseReading, CloseReadSection, CloseReadSentence,
)
from src.scholar.notes import write_notes
from src.scholar import notes_index as ni
from src.scholar import vault as V


# ---------------- 假数据 ----------------

def _fd(pid, decision="INCLUDE", **kw):
    d = dict(paper_id=pid, title="t", verdict="included", stage="llm_judge",
             decision=decision, one_line="对MNAR建模有直接借鉴", bucket=["A", "D"],
             role="CITE_SUPPORT", confidence=0.7, flags=[])
    d.update(kw)
    return FilterDecision(**d)


def _cr():
    return CloseReading(from_full_text=True, source="arxiv", sections=[
        CloseReadSection(heading="方法与数据", sentences=[
            CloseReadSentence(text="可学习掩码嵌入。", tag="方法论借鉴"),
            CloseReadSentence(text="普通句子无标记。", tag=None)]),
        CloseReadSection(heading="结果与效应量", sentences=[
            CloseReadSentence(text="AUPRC 提升 0.05。", tag="可引用证据"),
            CloseReadSentence(text="作者高估了效应。", tag="可反驳观点")])])


def _segments():
    a = PaperSegment(
        segment_id=1, paper_id="pa", priority_score=0.9, translated_title="深度EHR模型",
        metadata=PaperMetadata(paper_id="pa", title="Deep | EHR [Models]: under MNAR",
                               authors=["Jane Public", "Wei Chen"], doi="10.1/aaa",
                               journal="NEJM AI", publication_date=date(2025, 3, 2)),
        filter_decision=_fd("pa"), close_reading=_cr(),
        original_abstract="Abstract A about missingness.")
    b = PaperSegment(
        segment_id=2, paper_id="pb", priority_score=0.6,
        metadata=PaperMetadata(paper_id="pb", title="Graph Transformers for Missingness",
                               authors=["Ann Lee"], arxiv_id="2501.01234",
                               url="https://arxiv.org/abs/2501.01234",
                               publication_date=date(2025, 1, 10)),
        filter_decision=_fd("pb", decision="MAYBE", bucket=[], role="NONE", confidence=None,
                            one_line="图变换器处理缺失"),
        original_abstract="Abstract B about graphs.")
    c = PaperSegment(
        segment_id=3, paper_id="pc", priority_score=0.3,
        metadata=PaperMetadata(paper_id="pc", title="Minimal Paper"),
        filter_decision=_fd("pc", decision="MAYBE", bucket=[], role="NONE", confidence=None,
                            one_line="极简条目"),
        original_abstract="Abstract C.")
    return [a, b, c]


@pytest.fixture
def notes_dir(tmp_path):
    """真生成一个月度 md + sidecar，返回 (notes_dir, index)。"""
    segs = _segments()
    write_notes(segs, {"pa": "public2025Deep", "pb": "lee2025Graph", "pc": "min2025Paper"},
                out_dir=tmp_path, digest_title="科研札记 · 2025-03（全文精读）",
                filename="科研札记_2025-03_全文精读", fallback_citekeys=True,
                emit_index_sidecar=True)
    return tmp_path


@pytest.fixture
def index(notes_dir):
    return ni.update_index(notes_dir)


def _entry(index, citekey):
    return next(e for e in index["papers"] if e["citekey"] == citekey)


# ---------------- A. 切片 ----------------

def test_slice_middle_section_excludes_neighbours(notes_dir, index):
    lines = (notes_dir / "科研札记_2025-03_全文精读.md").read_text(encoding="utf-8").splitlines()
    e = _entry(index, "lee2025Graph")
    body = V.slice_section(lines, e["note_line"], "lee2025Graph")
    text = "\n".join(body)
    assert "Graph Transformers" in text
    assert "Deep | EHR" not in text and "Minimal Paper" not in text
    assert not text.rstrip().endswith("---")


def test_slice_last_section_stops_before_references(notes_dir, index):
    """终止条件必须含 `^# `：否则最后一篇吞掉 `# 参考文献` 与 `::: {#refs}`。"""
    md = notes_dir / "科研札记_2025-03_全文精读.md"
    raw = md.read_text(encoding="utf-8")
    assert "# 参考文献" in raw, "前提：聚合 md 末尾应有参考文献节"
    lines = raw.splitlines()
    last = max(index["papers"], key=lambda e: e["note_line"])
    body = V.slice_section(lines, last["note_line"], last["citekey"])
    text = "\n".join(body)
    assert "# 参考文献" not in text and "{#refs}" not in text


def test_slice_to_eof_when_no_references(notes_dir, index):
    md = notes_dir / "科研札记_2025-03_全文精读.md"
    raw = md.read_text(encoding="utf-8")
    trimmed = raw.split("# 参考文献")[0]
    lines = trimmed.splitlines()
    last = max(index["papers"], key=lambda e: e["note_line"])
    body = V.slice_section(lines, last["note_line"], last["citekey"])
    assert body and last["citekey"] in body[0]


@pytest.mark.parametrize("note_line, key", [
    (0, "public2025Deep"), (99999, "public2025Deep"), (2, "public2025Deep"),
])
def test_slice_returns_none_on_bad_input(notes_dir, index, note_line, key):
    lines = (notes_dir / "科研札记_2025-03_全文精读.md").read_text(encoding="utf-8").splitlines()
    assert V.slice_section(lines, note_line, key) is None


def test_slice_returns_none_on_citekey_mismatch(notes_dir, index):
    lines = (notes_dir / "科研札记_2025-03_全文精读.md").read_text(encoding="utf-8").splitlines()
    e = _entry(index, "public2025Deep")
    assert V.slice_section(lines, e["note_line"], "wrong2020Key") is None


def test_strip_meta_lines_promotes_headings(notes_dir, index):
    lines = (notes_dir / "科研札记_2025-03_全文精读.md").read_text(encoding="utf-8").splitlines()
    e = _entry(index, "public2025Deep")
    body = V.strip_meta_lines(V.slice_section(lines, e["note_line"], "public2025Deep"))
    text = "\n".join(body)
    assert "**优先级**" not in text and "**裁决**" not in text and "**作者**" not in text
    assert "## 摘要" in text and "### 摘要" not in text     # ### 提升为 ##
    assert "〔可引用证据〕" in text                            # 句级标记保留（溯源）


def test_load_bodies_reports_failures(notes_dir, index):
    entries = V.select_papers(index)
    bad = dict(entries[0])
    bad["note_line"] = 99999
    ok, failures = V.load_bodies([bad], notes_dir)
    assert not ok and len(failures) == 1


# ---------------- B. 选取 ----------------

def test_select_include_or_fulltext(index):
    keys = {e["citekey"] for e in V.select_papers(index)}
    assert "public2025Deep" in keys          # INCLUDE
    assert "lee2025Graph" not in keys        # MAYBE 且无精读
    assert {e["citekey"] for e in V.select_papers(index, include_maybe=True)} >= keys | {"lee2025Graph"}


def test_select_excludes_duplicates(index):
    idx = {"papers": [{"citekey": "dup", "decision": "INCLUDE", "duplicate_of": "x@2020-01"},
                      {"citekey": "keep", "decision": "INCLUDE", "duplicate_of": None}]}
    assert [e["citekey"] for e in V.select_papers(idx)] == ["keep"]


# ---------------- C. frontmatter ----------------

def test_frontmatter_roundtrips_through_yaml(index):
    e = _entry(index, "public2025Deep")
    fm = V.build_frontmatter(e)
    data = yaml.safe_load(fm.split("---")[1])
    assert data["citekey"] == "public2025Deep"
    assert data["title"] == "Deep | EHR [Models]: under MNAR"      # 含 | [ ] :
    assert isinstance(data["year"], int) and isinstance(data["month"], str)
    assert "tag_counts" not in data and data["n_citable"] >= 1     # 拍平，无嵌套 dict


def test_frontmatter_has_no_volatile_keys(index):
    fm = V.build_frontmatter(_entry(index, "public2025Deep"))
    assert "generated_at" not in fm and datetime_free(fm)


def datetime_free(text):
    import re as _re
    return not _re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:", text)


def test_frontmatter_truncates_authors():
    e = {"citekey": "k", "title": "T", "authors": ["A{} B".format(i) for i in range(82)]}
    data = yaml.safe_load(V.build_frontmatter(e).split("---")[1])
    assert len(data["authors"]) == 8 and data["authors_n"] == 82


def test_merge_tags_keeps_user_tags():
    new = ["scholar", "tier/mid", "bucket/A"]
    old = ["scholar", "tier/high", "待复现", "bucket/G"]
    out = V.merge_tags(new, old)
    assert "待复现" in out and "tier/mid" in out
    assert "tier/high" not in out and "bucket/G" not in out       # 受管前缀被替换


# ---------------- D. 用户内容保护（安全核心） ----------------

USER_TEXT = "\n## 我的札记\n\n这段是我手写的 ✍️ 中文 + emoji\n\n```python\nx = 1  # 代码块\n```\n\n### 二级想法\n\n收尾。\n"


def _assembled(entry, gen="# T\n\n生成内容 v1", user=USER_TEXT):
    return V.assemble(V.build_frontmatter(entry), gen, user)


def test_user_zone_preserved_on_rebuild(index):
    e = _entry(index, "public2025Deep")
    existing = _assembled(e)
    e2 = dict(e, one_line="改过的一句话用处")
    merged, status = V.merge_note(e2, "# T\n\n生成内容 v2", existing)
    assert status == "merged"
    assert USER_TEXT.strip() in merged                 # 用户区原样保留
    assert "生成内容 v2" in merged and "生成内容 v1" not in merged
    assert "改过的一句话用处" in merged                  # frontmatter 已更新


def test_user_frontmatter_keys_and_tags_preserved(index):
    e = _entry(index, "public2025Deep")
    existing = _assembled(e).replace("vault_schema: 1",
                                     'vault_schema: 1\nstatus: "已读"\nrating: 5')
    existing = existing.replace('tags: ["scholar"', 'tags: ["待复现", "scholar"')
    merged, status = V.merge_note(e, "# T\n\nv2", existing)
    assert status == "merged"
    data = yaml.safe_load(merged.split("---")[1])
    assert data["status"] == "已读" and data["rating"] == 5
    assert "待复现" in data["tags"] and "scholar" in data["tags"]


def test_missing_sentinel_is_conflict(index):
    e = _entry(index, "public2025Deep")
    broken = _assembled(e).replace(V.GEN_END, "")
    merged, status = V.merge_note(e, "# T\n\nv2", broken)
    assert status == "conflict" and merged is None      # 绝不覆盖


def test_broken_yaml_is_conflict(index):
    e = _entry(index, "public2025Deep")
    broken = "---\ntitle: [unclosed\n"                   # 无结束分隔线
    merged, status = V.merge_note(e, "# T\n\nv2", broken)
    assert status == "conflict" and merged is None


def test_new_file_gets_user_zone(index):
    merged, status = V.merge_note(_entry(index, "public2025Deep"), "# T\n\nv1", None)
    assert status == "new" and V.USER_HEADING in merged and V.GEN_END in merged


# ---------------- E. 命名 ----------------

@pytest.mark.parametrize("key", [
    "куксенко2024Аналіз", "anon2021野生动物疫病暴发成因及其防控对策",
    "düsing2022Tradeoff", "a/b:c*d?e", "x" * 300, "martin2019Predicting|",
])
def test_safe_filename_is_legal(key):
    used = set()
    name = V.safe_filename(key, used)
    assert not set(name) & set('/\\:*?"<>|#^[]%')
    assert len(name.encode("utf-8")) <= 200 and name


def test_safe_filename_case_insensitive_dedup():
    used = set()
    a = V.safe_filename("Foo2025Bar", used)
    b = V.safe_filename("foo2025bar", used)
    assert a != b and b.endswith("-2")                  # macOS APFS 大小写不敏感


# ---------------- F. 句级证据 ----------------

def test_highlight_blocks_grouped_with_stable_ids(index):
    e = _entry(index, "public2025Deep")
    out = "\n".join(V.build_highlight_blocks(e))
    assert "🟪 可引用证据" in out and "🟦 可反驳观点" in out and "🟩 方法论借鉴" in out
    ids = [l for l in out.splitlines() if l.startswith("^hl-")]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert "\n".join(V.build_highlight_blocks(e)) == out          # 稳定


def test_block_id_survives_insertion(index):
    e = _entry(index, "public2025Deep")
    before = [l for l in "\n".join(V.build_highlight_blocks(e)).splitlines()
              if l.startswith("^hl-")]
    e2 = dict(e, highlights=[{"role": "citable", "tag": "可引用证据", "section": "新",
                              "text": "插在最前的新句子。"}] + list(e["highlights"]))
    after = [l for l in "\n".join(V.build_highlight_blocks(e2)).splitlines()
             if l.startswith("^hl-")]
    assert set(before) <= set(after)                    # 原有 ID 不移位


def test_no_highlights_gets_info_callout():
    out = "\n".join(V.build_highlight_blocks({"citekey": "k", "highlights": []}))
    assert "[!info]" in out and "仅摘要级入选" in out


def test_legacy_tag_warning_present():
    e = {"citekey": "k", "highlights": [
        {"role": "citable", "tag": "重要发现", "section": "结论", "text": "旧标记句。"}]}
    out = "\n".join(V.build_highlight_blocks(e))
    assert "82%" in out and "原标记：重要发现" in out


# ---------------- G. 邻居 ----------------

def test_neighbors_no_self_no_dangling(index):
    entries = V.select_papers(index, include_maybe=True)
    nb = V.compute_neighbors(entries, k=3)
    keys = {e["citekey"] for e in entries}
    for key, lst in nb.items():
        assert all(other != key for other, _ in lst)
        assert all(other in keys for other, _ in lst)   # 防幽灵节点


# ---------------- H. 编排（write_vault） ----------------

def test_write_vault_end_to_end(notes_dir, index, tmp_path):
    vd = tmp_path / "vault"
    rep = V.write_vault(index, notes_dir, vd, k=2)
    papers = list((vd / V.PAPERS_DIR).glob("*.md"))
    assert len(papers) == rep["selected"] == len(V.select_papers(index))
    assert (vd / V.OVERVIEW).exists() and (vd / "README.md").exists()
    assert (vd / V.META_JSON).exists() and rep["conflicts"] == []
    text = (vd / V.PAPERS_DIR / "public2025Deep.md").read_text(encoding="utf-8")
    assert V.GEN_END in text and V.USER_HEADING in text
    assert "## 句级证据" in text and "## 归属" in text


def test_write_vault_is_idempotent(notes_dir, index, tmp_path):
    vd = tmp_path / "vault"
    V.write_vault(index, notes_dir, vd, k=2)
    stamps = {p: p.stat().st_mtime_ns for p in vd.rglob("*.md")}
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert rep["written"] == 0
    assert {p: p.stat().st_mtime_ns for p in vd.rglob("*.md")} == stamps   # mtime 不抖


def test_rebuild_preserves_user_zone_on_disk(notes_dir, index, tmp_path):
    vd = tmp_path / "vault"
    V.write_vault(index, notes_dir, vd, k=2)
    note = vd / V.PAPERS_DIR / "public2025Deep.md"
    note.write_text(note.read_text(encoding="utf-8") + "\n我手写的一段 ✍️\n", encoding="utf-8")
    for e in index["papers"]:
        if e["citekey"] == "public2025Deep":
            e["one_line"] = "改过的用处"
    V.write_vault(index, notes_dir, vd, k=2)
    after = note.read_text(encoding="utf-8")
    assert "我手写的一段 ✍️" in after and "改过的用处" in after


def test_conflict_does_not_touch_original(notes_dir, index, tmp_path):
    vd = tmp_path / "vault"
    V.write_vault(index, notes_dir, vd, k=2)
    note = vd / V.PAPERS_DIR / "public2025Deep.md"
    note.write_text(note.read_text(encoding="utf-8").replace(V.GEN_END, ""), encoding="utf-8")
    before = note.stat().st_mtime_ns, note.read_text(encoding="utf-8")
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert "public2025Deep" in rep["conflicts"]
    assert (note.stat().st_mtime_ns, note.read_text(encoding="utf-8")) == before   # 原文件未动
    assert (vd / V.PAPERS_DIR / "public2025Deep.conflict.md").exists()


def test_dry_run_writes_nothing(notes_dir, index, tmp_path):
    vd = tmp_path / "vault"
    rep = V.write_vault(index, notes_dir, vd, k=2, dry_run=True)
    assert rep["selected"] > 0 and not vd.exists()


def test_moc_pages_are_static_no_dataview(notes_dir, index, tmp_path):
    vd = tmp_path / "vault"
    V.write_vault(index, notes_dir, vd, k=2)
    for p in (vd / V.MOC_DIR).rglob("*.md"):
        assert "```dataview" not in p.read_text(encoding="utf-8")   # 零插件依赖回归锁


def test_unbucketed_papers_get_fallback_moc(notes_dir, index, tmp_path):
    vd = tmp_path / "vault"
    V.write_vault(index, notes_dir, vd, k=2, include_maybe=True)
    assert (vd / V.MOC_DIR / "维度" / "未分维度.md").exists()


def test_shards_large_group():
    rows = [{"citekey": "k{}".format(i), "year": 2020 + i % 3, "month": "2025-01",
             "priority_tier": "mid", "one_line": "x", "bucket": ["G"]} for i in range(300)]
    pages = V._sharded("维度", "G-临床落地场景", "d", rows)
    parent = pages["{}/维度/G-临床落地场景.md".format(V.MOC_DIR)]
    assert "分片如下" in parent and len(pages) == 4          # 父页 + 3 个年份子页


def test_no_pointless_shard_when_key_does_not_split():
    """切不动就不切：否则会产出「父页只挂一个子页」的空壳（曾把 383 条全塞进未分维度）。"""
    rows = [{"citekey": "k{}".format(i), "year": 2025, "month": "2025-01",
             "priority_tier": "mid", "one_line": "x"} for i in range(300)]
    pages = V._sharded("角色", "未标角色", "d", rows)
    assert len(pages) == 1 and "分片如下" not in next(iter(pages.values()))


def test_year_moc_shards_by_month_not_year():
    """年份 MOC 若按年份再切会产出 `2025-2025` 这种废页。"""
    rows = [{"citekey": "k{}".format(i), "year": 2025, "priority_tier": "mid", "one_line": "x",
             "month": "2025-{:02d}".format(1 + i % 4)} for i in range(300)]
    pages = V._sharded("年份", "2025", "发表年 2025", rows, shard_key=V._shard_month)
    assert "{}/年份/2025-2025.md".format(V.MOC_DIR) not in pages
    assert "{}/年份/2025-2025-01.md".format(V.MOC_DIR) in pages


def test_evidence_pages_stay_readable():
    """两级分片：手动深读条目 bucket 恒为空，只按维度切会撑出 800+ 条的单页。"""
    entries = [{"citekey": "k{}".format(i), "year": 2020 + i % 5, "priority_tier": "mid",
                "bucket": [], "highlights": [
                    {"role": "citable", "tag": "可引用证据", "section": "结果",
                     "text": "证据 {}".format(j)} for j in range(4)]}
               for i in range(100)]
    pages = V.build_evidence_pages(entries)
    biggest = max(len([l for l in t.splitlines() if l.startswith("- [[")])
                  for t in pages.values())
    assert biggest <= 120 and len(pages) > 2
