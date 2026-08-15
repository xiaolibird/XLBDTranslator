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
from pathlib import Path

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

def test_select_defaults_to_full_text_only(index):
    """默认只收已精读：INCLUDE 但只有摘要的条目会被挡在外面（曾致 2/3 节点是空壳）。"""
    default = {e["citekey"] for e in V.select_papers(index)}
    assert "public2025Deep" in default        # 有精读
    assert "lee2025Graph" not in default      # MAYBE 且无精读

    abstract_only = {e["citekey"] for e in V.select_papers(index, include_abstract_only=True)}
    assert abstract_only >= default           # 放宽后是超集
    everything = {e["citekey"] for e in V.select_papers(index, include_maybe=True)}
    assert "lee2025Graph" in everything and everything >= abstract_only


def test_select_excludes_duplicates(index):
    idx = {"papers": [{"citekey": "dup", "has_full_text_reading": True,
                       "duplicate_of": "x@2020-01"},
                      {"citekey": "keep", "has_full_text_reading": True,
                       "duplicate_of": None}]}
    assert [e["citekey"] for e in V.select_papers(idx)] == ["keep"]


def test_select_excludes_missing_key_placeholders():
    """MISSING-KEY 占位键不得生成 vault 笔记——sidecar 写明「消费方据此过滤」，此处即消费方。"""
    idx = {"papers": [{"citekey": "MISSING-KEY-10.1/x", "has_full_text_reading": True,
                       "duplicate_of": None, "citekey_source": "missing"},
                      {"citekey": "odd2024Key", "has_full_text_reading": True,
                       "duplicate_of": None, "citekey_source": "missing"},  # 键被改过名但源头仍是占位
                      {"citekey": "keep2025Ok", "has_full_text_reading": True,
                       "duplicate_of": None, "citekey_source": "zotero"}]}
    assert [e["citekey"] for e in V.select_papers(idx)] == ["keep2025Ok"]
    # 放宽口径同样不得放行占位键
    assert [e["citekey"] for e in V.select_papers(idx, include_maybe=True)] == ["keep2025Ok"]


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
    new = ["札记", "优先级/中", "维度/A-缺失机制方法学"]
    old = ["札记", "优先级/高", "待复现", "维度/G-临床落地场景"]
    out = V.merge_tags(new, old)
    assert "待复现" in out and "优先级/中" in out
    assert "优先级/高" not in out and "维度/G-临床落地场景" not in out   # 受管前缀被替换


def test_merge_tags_strips_retired_tags():
    """回归：tag 改名后旧 tag 若被当成用户 tag 保留，每篇会同时挂新旧两套，永不收敛。"""
    out = V.merge_tags(["札记", "优先级/中"],
                       ["scholar", "tier/high", "bucket/G", "role/BACKGROUND",
                        "flag/THREAT", "year/2025", "待复现"])
    assert out == ["札记", "优先级/中", "待复现"]


def test_managed_prefix_does_not_eat_lookalike_user_tag():
    """`scholar` 是整串匹配，不能顺手吃掉用户的 `scholarship`。"""
    out = V.merge_tags(["札记"], ["scholarship", "维度学习", "scholar"])
    assert "scholarship" in out and "维度学习" in out and "scholar" not in out


def test_tags_are_human_readable(index):
    e = _entry(index, "public2025Deep")
    tags = V.build_tags(dict(e, priority_tier="high", series="auto", decision="INCLUDE",
                             reading_source="arxiv", year=2025, bucket=["E"],
                             role="MUST_ENGAGE", flags=["THREAT"]))
    assert tags == ["札记", "优先级/高", "系列/月度精读", "裁决/收录", "原文/arXiv",
                    "年份/2025", "维度/E-对抗性证据", "角色/必须回应", "旗标/反向结论"]
    assert all(" " not in t for t in tags)          # Obsidian tag 不允许空格


def test_tags_fall_back_on_unknown_values(index):
    """新增枚举值（如新 bucket H）不能静默丢 tag，也不能产出带空格的非法 tag。"""
    e = _entry(index, "public2025Deep")
    tags = V.build_tags(dict(e, priority_tier="urgent now", series=None, decision="NEW",
                             reading_source="some source", year=None, bucket=["H"],
                             role="NEW_ROLE", flags=["WEIRD FLAG"]))
    assert "优先级/urgent-now" in tags and "维度/H" in tags and "旗标/WEIRD-FLAG" in tags
    assert "裁决/NEW" in tags and "原文/some-source" in tags and "角色/NEW_ROLE" in tags
    assert all(" " not in t for t in tags)


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
    existing = existing.replace('tags: ["札记"', 'tags: ["待复现", "札记"')
    merged, status = V.merge_note(e, "# T\n\nv2", existing)
    assert status == "merged"
    data = yaml.safe_load(merged.split("---")[1])
    assert data["status"] == "已读" and data["rating"] == 5
    assert "待复现" in data["tags"] and "札记" in data["tags"]


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

def _corpus(n=12):
    """够真实的语料：两个主题簇，保证 TF-IDF 相似度高于阈值真能产出邻居。

    此前这里只有 3 篇玩具论文，相似度全部低于 0.02 → 邻居恒为空列表 →
    「不含自身」「不指向集外」两个 all() 在空列表上恒真，等于没测。
    """
    out = []
    for i in range(n):
        if i % 2 == 0:
            title = "Missing data imputation under MNAR mechanism in EHR cohort {}".format(i)
            one = "缺失机制 MNAR 插补 电子健康记录 队列"
            hl = "MNAR missingness indicator imputation EHR clinical prediction"
        else:
            title = "Graph transformer representation learning for clinical notes {}".format(i)
            one = "图变换器 表示学习 临床文本"
            hl = "graph transformer attention representation learning clinical text"
        out.append({"citekey": "k{:02d}".format(i), "title": title, "one_line": one,
                    "year": 2020 + i % 5, "month": "2025-01", "priority_tier": "mid",
                    "bucket": ["G"] if i % 2 else ["A"], "duplicate_of": None,
                    "decision": "INCLUDE", "highlights": [
                        {"role": "citable", "tag": "可引用证据", "section": "结果", "text": hl}]})
    return out


def test_neighbors_are_actually_produced():
    """回归：语料太小时曾整体退化为空邻居，使下面两条断言空转。"""
    nb = V.compute_neighbors(_corpus(), k=3)
    assert sum(len(v) for v in nb.values()) > 0
    assert all(len(v) > 0 for v in nb.values())          # 无孤立节点


def test_neighbors_no_self_no_dangling():
    entries = _corpus()
    nb = V.compute_neighbors(entries, k=3)
    keys = {e["citekey"] for e in entries}
    total = 0
    for key, lst in nb.items():
        assert all(other != key for other, _ in lst)
        assert all(other in keys for other, _ in lst)    # 防幽灵节点
        total += len(lst)
    assert total > 0                                     # 防再次空转


def test_neighbors_are_symmetric():
    nb = V.compute_neighbors(_corpus(), k=2)
    for a, lst in nb.items():
        for b, _ in lst:
            assert any(x == a for x, _ in nb[b])         # 双向补全


def test_neighbors_prefer_same_topic():
    """同主题簇应互为近邻——否则相似度算法退化成噪声。"""
    nb = V.compute_neighbors(_corpus(), k=3)
    for key, lst in nb.items():
        same_parity = [o for o, _ in lst if int(o[1:]) % 2 == int(key[1:]) % 2]
        assert len(same_parity) >= 2, (key, lst)


def test_neighbors_disabled_when_k_zero():
    assert all(v == [] for v in V.compute_neighbors(_corpus(), k=0).values())


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


# ---------------- I. 链接消毒 / 月份归一 / 年份不分片 ----------------

def test_evidence_alias_strips_brackets():
    """回归：highlight 里的 `[13]` 会提前闭合 wikilink，实测曾致 88 条证据链接点不进去。"""
    entries = [{"citekey": "k1", "year": 2025, "priority_tier": "mid", "bucket": [],
                "highlights": [{"role": "citable", "tag": "可引用证据", "section": "结果",
                                "text": "纠错（p.29）：引自 [13] 的公式 1[β^⊤X 有误"}]}]
    page = V.build_evidence_pages(entries)["{}/证据/可引用证据.md".format(V.MOC_DIR)]
    link = next(l for l in page.splitlines() if l.startswith("- [["))
    inner = link[link.index("[[") + 2:link.index("]]")]
    assert "[" not in inner and "]" not in inner      # 别名里不得有裸方括号
    assert "(13)" in inner                            # 内容仍可读，只是换成圆括号


def test_moc_row_alias_strips_brackets():
    row = V._row({"citekey": "k", "priority_tier": "mid", "year": 2025, "month": "2025-01",
                  "one_line": "见 [Fig. 2] 与 |表 3|"})
    cell = row.split("|")[4]
    assert "[" not in cell and "]" not in cell


def test_month_key_normalizes_dated_buckets():
    """索引里有 `2026-07-17` 这类专题批次桶，照收会劈成两页并让月份总数虚高。"""
    assert V.month_key({"month": "2026-07-17"}) == "2026-07"
    assert V.month_key({"month": "2026-07"}) == "2026-07"
    assert V.month_key({}) == "未知月"


def test_month_moc_merges_dated_bucket():
    entries = [{"citekey": "a", "month": "2026-07", "year": 2026, "priority_tier": "mid",
                "one_line": "x", "bucket": [], "highlights": []},
               {"citekey": "b", "month": "2026-07-17", "year": 2026, "priority_tier": "mid",
                "one_line": "y", "bucket": [], "highlights": []}]
    pages = V.build_moc_pages(entries)
    assert "{}/月度/2026-07.md".format(V.MOC_DIR) in pages
    assert "{}/月度/2026-07-17.md".format(V.MOC_DIR) not in pages
    assert "[[a]]" in pages["{}/月度/2026-07.md".format(V.MOC_DIR)]
    assert "[[b]]" in pages["{}/月度/2026-07.md".format(V.MOC_DIR)]


def test_year_moc_is_not_sharded():
    """回归：按收录月切年份会产出 `2023-2021-06` 这种页，86 个子页里 27 个只含 1 篇。"""
    entries = [{"citekey": "k{}".format(i), "year": 2025, "priority_tier": "mid",
                "one_line": "x", "bucket": [], "highlights": [],
                "month": "2025-{:02d}".format(1 + i % 12)} for i in range(300)]
    pages = V.build_moc_pages(entries)
    year_pages = [p for p in pages if p.startswith("{}/年份/".format(V.MOC_DIR))]
    assert year_pages == ["{}/年份/2025.md".format(V.MOC_DIR)]


# ---------------- J. CLI（此前零覆盖） ----------------

import subprocess  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_REPO = _Path(__file__).resolve().parents[1]


def _cli(*args, cwd=None):
    return subprocess.run([_sys.executable, "scripts/build_vault.py", *args],
                          cwd=cwd or _REPO, capture_output=True, text=True)


def test_cli_exit_2_on_missing_index(tmp_path):
    r = _cli("--vault-dir", str(tmp_path / "v"), "--notes-dir", str(tmp_path / "nope"))
    assert r.returncode == 2 and "notes_index.py" in r.stderr
    assert not (tmp_path / "v").exists()


def test_cli_exit_2_on_corrupt_index(tmp_path):
    nd = tmp_path / "notes"
    nd.mkdir()
    (nd / "literature_index.json").write_text("{not json", encoding="utf-8")
    r = _cli("--vault-dir", str(tmp_path / "v"), "--notes-dir", str(nd))
    assert r.returncode == 2 and "损坏" in r.stderr


def test_cli_rejects_negative_limit(tmp_path, notes_dir):
    """argparse 层校验，不依赖索引落盘。"""
    """回归：负值走 Python 负切片会静默少生成几篇且退出码 0。"""
    r = _cli("--vault-dir", str(tmp_path / "v"), "--notes-dir", str(notes_dir), "--limit", "-3")
    assert r.returncode == 2 and "--limit" in r.stderr      # argparse error → 2


@pytest.fixture
def notes_dir_on_disk(notes_dir, index):
    """CLI 走的是磁盘上的 literature_index.json，需要真落盘（index fixture 只在内存里）。"""
    ni.write_outputs(index, notes_dir)
    return notes_dir


def test_cli_dry_run_and_build(tmp_path, notes_dir_on_disk):
    notes_dir = notes_dir_on_disk
    vd = tmp_path / "v"
    r = _cli("--vault-dir", str(vd), "--notes-dir", str(notes_dir), "--dry-run")
    assert r.returncode == 0 and not vd.exists()
    r = _cli("--vault-dir", str(vd), "--notes-dir", str(notes_dir))
    assert r.returncode == 0 and (vd / V.OVERVIEW).exists()
    r = _cli("--vault-dir", str(vd), "--notes-dir", str(notes_dir))
    assert r.returncode == 0 and "写盘 0 个文件" in r.stdout   # 幂等


def test_stale_moc_pages_are_pruned(notes_dir, index, tmp_path):
    """分片规则变更后旧页会成幽灵，继续出现在图谱和搜索里——必须清掉。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    ghost = vd / V.MOC_DIR / "年份" / "2099-2099-01.md"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_text("# 过期分片\n", encoding="utf-8")
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert not ghost.exists() and any("2099" in p for p in rep["pruned"])


def test_orphan_with_user_text_is_never_deleted(notes_dir, index, tmp_path):
    """落选笔记里只要有手写内容就必须保留——口径收紧时这是唯一的数据安全底线。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    orphan = vd / V.PAPERS_DIR / "old2019Dropped.md"
    orphan.write_text(V.assemble(
        '---\ncitekey: "old2019Dropped"\n---', "# 旧笔记\n\n生成内容",
        "\n## 我的札记\n\n重要手写 ✍️\n"), encoding="utf-8")
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert orphan.exists() and "重要手写 ✍️" in orphan.read_text(encoding="utf-8")
    assert "old2019Dropped" in rep["orphan_papers"]


def test_orphan_without_user_text_is_pruned(notes_dir, index, tmp_path):
    """纯生成产物（用户区空）落选后直接删，否则口径收紧后会留下几百个幽灵节点。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    orphan = vd / V.PAPERS_DIR / "old2019Empty.md"
    orphan.write_text(V.assemble('---\ncitekey: "old2019Empty"\n---', "# 旧笔记\n\n生成内容",
                                 V.DEFAULT_USER_ZONE), encoding="utf-8")
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert not orphan.exists()
    assert any("old2019Empty" in x for x in rep["pruned"])
    assert "old2019Empty" not in rep["orphan_papers"]


def test_conflict_file_is_never_pruned(notes_dir, index, tmp_path):
    """.conflict.md 是待用户手工合并的产物，不能被当成落选笔记清掉。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    c = vd / V.PAPERS_DIR / "zzz2019Old.conflict.md"
    c.write_text("待合并\n", encoding="utf-8")
    V.write_vault(index, notes_dir, vd, k=2)
    assert c.exists()


# ---------------- L. --limit 不得驱动落选/MOC 清理（调试抽样语义 ≠ 落选口径）----------------
#
# write_vault(limit=N) 是「只处理前 N 篇」的调试语义。此前的清理逻辑只认
# 「当前这轮 entries 有没有覆盖到」，limit 截断后的 entries 只是全集的一个前缀，
# 会把「本轮没处理但仍在全集里」的其余笔记与 MOC 页当成落选一并删掉。

def test_limit_does_not_delete_papers_outside_truncated_set(notes_dir, index, tmp_path):
    """先无 limit 全量生成 3 篇；再以 limit=1 重跑：另外 2 篇的笔记不得被当成
    orphan 删除（它们仍在索引全集里，只是这一轮没被处理到）。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2, include_maybe=True, include_abstract_only=True)
    other_papers = [vd / V.PAPERS_DIR / "{}.md".format(ck)
                    for ck in ("lee2025Graph", "min2025Paper")]
    assert all(p.exists() for p in other_papers)

    rep = V.write_vault(index, notes_dir, vd, k=2, include_maybe=True,
                        include_abstract_only=True, limit=1)

    assert all(p.exists() for p in other_papers), "limit 截断外的论文笔记被误删"
    assert rep["orphan_papers"] == []
    assert rep["pruned"] == []
    assert rep["cleanup_skipped_due_to_limit"] is True


def test_limit_does_not_prune_stale_moc_pages(notes_dir, index, tmp_path):
    """limit 生效时连本该清理的过期 MOC 页也不清（宁可漏清，不能误删未覆盖的）。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2, include_maybe=True, include_abstract_only=True)
    ghost = vd / V.MOC_DIR / "年份" / "2099-2099-01.md"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_text("# 过期分片\n", encoding="utf-8")

    rep = V.write_vault(index, notes_dir, vd, k=2, include_maybe=True,
                        include_abstract_only=True, limit=1)

    assert ghost.exists(), "limit 生效时不该清理 MOC 过期页"
    assert rep["pruned"] == []


def test_no_limit_cleanup_flag_is_false(notes_dir, index, tmp_path):
    """不传 limit（或 limit=0）时清理照常进行，report 里的标记位为假。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert not rep.get("cleanup_skipped_due_to_limit")


def test_limit_still_writes_the_truncated_entries(notes_dir, index, tmp_path):
    """limit 只影响清理，不影响本轮该写的那几篇——它们必须照常落盘。"""
    vd = tmp_path / "v"
    rep = V.write_vault(index, notes_dir, vd, k=2, limit=1)
    assert rep["selected"] == 1
    assert (vd / V.PAPERS_DIR / "public2025Deep.md").exists()  # priority 最高的 pa


# ---------------- K. 第二轮审查修复的回归 ----------------

def test_preserved_key_with_colon_stays_valid_yaml(index):
    """回归：键名不消毒会写出语法错误的 YAML，等于我们主动把用户笔记改成读不出来的文件。"""
    e = _entry(index, "public2025Deep")
    for weird in ("a: b", "a #c", "", "~", "yes", "with space"):
        fm = V.build_frontmatter(e, preserved={weird: 1})
        data = yaml.safe_load(fm.split("---")[1])           # 必须仍可解析
        assert data is not None
        assert any(str(k) == weird for k in data), (weird, list(data))


@pytest.mark.parametrize("bad_tags", [2024, 1.5, {"a": 1}, None, True])
def test_merge_tags_survives_non_list(bad_tags):
    """回归：`tags: 2024` 曾抛 TypeError 把整轮构建带崩。"""
    out = V.merge_tags(["札记", "优先级/中"], bad_tags)
    assert "札记" in out and isinstance(out, list)


def test_build_survives_weird_frontmatter(notes_dir, index, tmp_path):
    """单篇形状意外应降级为 conflict，而不是让整轮构建崩掉。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    note = vd / V.PAPERS_DIR / "public2025Deep.md"
    note.write_text(note.read_text(encoding="utf-8").replace(
        'tags: ["札记"', 'tags: 2024\nx_tags: ["札记"'), encoding="utf-8")
    rep = V.write_vault(index, notes_dir, vd, k=2)          # 不抛异常
    assert rep["selected"] >= 1


def test_moc_sorts_high_priority_first():
    """回归：reverse=True 作用在整个元组上，曾让 rank 1(🔴高) 沉到表格最后。"""
    rows = [{"citekey": "low", "priority_tier": "low", "priority_rank": 46,
             "month": "2025-01", "year": 2025, "one_line": "x"},
            {"citekey": "high", "priority_tier": "high", "priority_rank": 1,
             "month": "2025-01", "year": 2025, "one_line": "y"}]
    page = V._moc_page("t", "d", rows)
    lines = [l for l in page.splitlines() if l.startswith("| ")][1:]   # 跳过表头行
    assert "[[high]]" in lines[0] and "[[low]]" in lines[1]


def test_neighbors_sorted_by_similarity():
    """回归：双向补全把反向边 append 到末尾，曾致 377/936 篇的列表非单调。"""
    nb = V.compute_neighbors(_corpus(20), k=4)
    for key, lst in nb.items():
        sims = [s for _, s in lst]
        assert sims == sorted(sims, reverse=True), (key, sims)


def test_tampering_generated_block_is_conflict(notes_dir, index, tmp_path):
    """用户在生成块内部写批注：此前会被静默丢弃，现在判 conflict 不覆盖。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    note = vd / V.PAPERS_DIR / "public2025Deep.md"
    t = note.read_text(encoding="utf-8")
    t = t.replace("## 句级证据", "我在生成块里写了一句批注\n\n## 句级证据")
    note.write_text(t, encoding="utf-8")
    before = note.stat().st_mtime_ns, note.read_text(encoding="utf-8")
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert "public2025Deep" in rep["conflicts"]
    assert (note.stat().st_mtime_ns, note.read_text(encoding="utf-8")) == before
    assert "我在生成块里写了一句批注" in note.read_text(encoding="utf-8")


def test_untampered_rebuild_is_not_conflict(notes_dir, index, tmp_path):
    """哈希守卫不能把正常重建误判成冲突。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert rep["conflicts"] == [] and rep["written"] == 0


def test_force_regen_overwrites_tampered_conflict_and_keeps_user_zone(notes_dir, index, tmp_path):
    """回归 FIX-4：--force-regen 此前是完全空操作——force 分支与常规分支写的是同一份
    merge_note 结果，真正需要强制覆盖的 conflict 条目反而在 merge_note 早退时就 continue
    掉了，force 检查根本轮不到。现在 force_regen=True 必须把 conflict 条目也覆盖掉
    （不再产出 .conflict.md 旁路），且用户在哨兵之后写的手写区要保留。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    note = vd / V.PAPERS_DIR / "public2025Deep.md"
    t = note.read_text(encoding="utf-8")
    t = t.replace("## 句级证据", "我在生成块里写了一句批注\n\n## 句级证据")
    t += "\n我在用户区手写的一段 ✍️\n"
    note.write_text(t, encoding="utf-8")

    # 不带 --force-regen：仍按现有语义判 conflict，原文件不动
    rep = V.write_vault(index, notes_dir, vd, k=2)
    assert "public2025Deep" in rep["conflicts"]
    assert rep["force_overwritten"] == []

    # 带 --force-regen：conflict 条目被直接覆盖，不再落 .conflict.md 旁路，
    # 且原先写在哨兵之后的用户区内容仍保留在覆盖后的文件里。
    rep2 = V.write_vault(index, notes_dir, vd, k=2, force_regen=True)
    assert "public2025Deep" in rep2["force_overwritten"]
    assert "public2025Deep" not in rep2["conflicts"]
    after = note.read_text(encoding="utf-8")
    assert "我在生成块里写了一句批注" not in after   # 生成块已被新内容覆盖
    assert "我在用户区手写的一段 ✍️" in after         # 用户区仍保留
    assert not (vd / V.PAPERS_DIR / "public2025Deep.conflict.md").exists()


def test_force_regen_is_noop_when_no_conflicts(notes_dir, index, tmp_path):
    """没有冲突条目时，--force-regen 不该改变任何输出——与不加该开关逐字节一致。"""
    vd_a = tmp_path / "a"
    vd_b = tmp_path / "b"
    V.write_vault(index, notes_dir, vd_a, k=2)
    V.write_vault(index, notes_dir, vd_b, k=2, force_regen=True)
    a_files = {p.relative_to(vd_a): p.read_text(encoding="utf-8") for p in vd_a.rglob("*.md")}
    b_files = {p.relative_to(vd_b): p.read_text(encoding="utf-8") for p in vd_b.rglob("*.md")}
    assert a_files == b_files


def test_force_regen_survives_unserializable_preserved_frontmatter_key(notes_dir, index, tmp_path):
    """opus 补审必修项回归：merge_note 内 build_frontmatter(preserved=fm) 遇到形状意外的
    用户 frontmatter（如 `mydates:\\n  - 2024-01-01` 会被 yaml.safe_load 解析成
    datetime.date 列表，_dump_preserved 的 json.dumps 对其抛 TypeError）时，
    merge_note 本身包在 try/except 里能优雅降级成 conflict；但 --force-regen 覆盖
    conflict 时 _force_overwrite_content 内部会拿同一份 preserved 再调一次
    build_frontmatter，此前这次调用不在任何 try 里，异常会把 write_vault 整轮
    构建带崩——几百篇一篇不写。现在必须：force_regen 下这篇被正常 force_overwritten
    （退化成全新 frontmatter），且同一轮里其它论文照常写出，不受牵连。"""
    vd = tmp_path / "v"
    kw = dict(include_maybe=True, include_abstract_only=True)   # 把 3 篇全选进来，才能验证「其它论文不受牵连」
    V.write_vault(index, notes_dir, vd, k=2, **kw)
    note = vd / V.PAPERS_DIR / "public2025Deep.md"
    t = note.read_text(encoding="utf-8")
    t = t.replace("---\n", "---\nmydates:\n  - 2024-01-01\n", 1)
    note.write_text(t, encoding="utf-8")

    rep = V.write_vault(index, notes_dir, vd, k=2, **kw)          # 不带 force：优雅降级
    assert "public2025Deep" in rep["conflicts"]

    rep2 = V.write_vault(index, notes_dir, vd, k=2, force_regen=True, **kw)   # 不抛异常
    assert "public2025Deep" in rep2["force_overwritten"]
    assert "public2025Deep" not in rep2["conflicts"]
    # 同一轮里其它两篇正常论文没被这篇的异常连累——都照常存在且不在冲突名单里
    assert (vd / V.PAPERS_DIR / "lee2025Graph.md").exists()
    assert (vd / V.PAPERS_DIR / "min2025Paper.md").exists()
    assert "lee2025Graph" not in rep2["conflicts"]
    assert "min2025Paper" not in rep2["conflicts"]


def test_force_regen_falls_back_to_conflict_when_end_sentinel_missing(notes_dir, index, tmp_path):
    """opus 补审建议2回归：END 哨兵被用户删掉时 extract_user_zone 返回 None，
    --force-regen 此前会退回 DEFAULT_USER_ZONE 空白覆盖原文件、还顺手把旁路的
    .conflict.md 删掉（判定「冲突已用 force 解决」）——用户手写内容的唯一副本
    就此消失，且没有 .conflict.md 兜底。现在用户区抽不出来时必须整体退回常规
    conflict 分支：不覆盖原文件、旧 .conflict.md 不删。"""
    vd = tmp_path / "v"
    V.write_vault(index, notes_dir, vd, k=2)
    note = vd / V.PAPERS_DIR / "public2025Deep.md"
    t = note.read_text(encoding="utf-8")
    # 删掉 END 哨兵本身，但哨兵之后确实还有用户手写内容（模拟误删哨兵行）
    t = t.replace(V.GEN_END + "\n", "") + "\n我手写的重要内容，哨兵被我不小心删了 ✍️\n"
    note.write_text(t, encoding="utf-8")
    before = note.read_text(encoding="utf-8")

    conflict_path = vd / V.PAPERS_DIR / "public2025Deep.conflict.md"
    conflict_path.write_text("旧的人工合并占位", encoding="utf-8")

    rep = V.write_vault(index, notes_dir, vd, k=2, force_regen=True)

    assert "public2025Deep" in rep["conflicts"]
    assert "public2025Deep" not in rep["force_overwritten"]
    assert note.read_text(encoding="utf-8") == before   # 原文件一字未改，手写内容还在
    assert conflict_path.exists()                        # 旧 .conflict.md 没被删


def test_evidence_third_level_shard_keeps_pages_small():
    entries = [{"citekey": "k{}".format(i), "year": 2025, "priority_tier": ["high", "mid", "low"][i % 3],
                "bucket": [], "highlights": [
                    {"role": "citable", "tag": "可引用证据", "section": "结果",
                     "text": "证据 {}-{}".format(i, j)} for j in range(4)]}
               for i in range(120)]
    pages = V.build_evidence_pages(entries)
    biggest = max(len([l for l in t.splitlines() if l.startswith("- [[")])
                  for t in pages.values())
    assert biggest <= V.SHARD_THRESHOLD


def test_belong_month_link_matches_page(notes_dir, index, tmp_path):
    """归属链接与建页必须同口径：曾用原始 `2026-07-17` 链到只按 `2026-07` 建的页。"""
    e = dict(_entry(index, "public2025Deep"), month="2026-07-17")
    block = V.render_generated_block(e, [], [], {})
    assert "_MOC/月度/2026-07|" in block and "2026-07-17|" not in block


def test_repo_dedup_overrides_are_isolated_from_this_file():
    """R2-3：本文件的 update_index → _global_pass 链也必须读不到仓库那份真裁决文件。

    隔离原先只挂在 test_notes_index.py 上（单文件 autouse fixture），而这条链在本文件
    与 test_pdf_ingest.py 里同样会走：往 config/dedup_overrides.json 里加一条涉及
    doi:10.1/aaa 之类测试假键的真实裁决，就会让本文件里不相干的用例变红。
    夹具现在在 test/conftest.py，对整个 test/ 目录生效。
    """
    assert ni._read_override_files(None) == []
    assert not Path(ni.REPO_OVERRIDES_PATH).exists()
