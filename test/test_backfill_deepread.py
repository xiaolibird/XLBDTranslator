# -*- coding: utf-8 -*-
"""backfill_deepread 的核心不变量：文本手术只碰目标篇、渲染与解析无损往返。

这个脚本要在几十个月度札记（合计一千多篇论文）里就地改写其中一百来篇的精读节，
一次越界就是静默丢数据且没有 git 可回滚（output/ 全在 .gitignore 内）。所以测的
重点不是"能不能改对"，而是**"会不会改到别人"**。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.backfill_deepread as bd  # noqa: E402
from src.scholar.notes_index import _SECTION_RE, parse_note_md  # noqa: E402
from src.scholar.schema import CloseReading, CloseReadSection, CloseReadSentence  # noqa: E402


def _cr(sections, from_full_text=True, source="arxiv"):
    return CloseReading(
        from_full_text=from_full_text, source=source,
        sections=[CloseReadSection(
            heading=h, sentences=[CloseReadSentence(text=t, tag=g) for t, g in ss])
            for h, ss in sections])


MD = """---
title: t
---

# 科研札记

## 🔴 高 1. First paper [@alpha2021One]

**优先级**: `9.00`
**裁决**: `INCLUDE` · 角色 CITE_SUPPORT

### 摘要

第一篇摘要。

### 全文精读 · 来源 `arxiv`

**【关键结论】**
- 〔可引用证据〕甲的旧结论。

## 🟠 中 2. Second paper [@beta2022Two]

**优先级**: `5.00`

### 摘要

第二篇摘要。

### 全文精读 · 来源 `unpaywall`

**【方法与数据】**
- 〔方法论借鉴〕乙的方法。
- 乙的无标记句。

# 参考文献

[@alpha2021One] ...
"""


def _sections(text):
    out, cur, buf = {}, None, []
    for ln in text.splitlines():
        m = _SECTION_RE.match(ln)
        if m:
            if cur:
                out[cur] = "\n".join(buf)
            cur, buf = m.group(4), [ln]
        elif cur is not None:
            buf.append(ln)
    if cur:
        out[cur] = "\n".join(buf)
    return out


def test_replace_only_touches_target_paper():
    """核心不变量：改一篇，其余论文的字节必须**逐字节**不变。"""
    new_cr = _cr([("实验方法", [("甲的新方法细节。", "方法论借鉴"), ("甲的新对照。", None)])])
    out, _o, _n = bd.replace_closeread(MD, "alpha2021One", None, bd._render_closeread(new_cr))
    a, b = _sections(MD), _sections(out)
    assert set(a) == set(b)
    assert b["beta2022Two"] == a["beta2022Two"], "改动外溢到了另一篇"
    assert "甲的旧结论" not in out and "甲的新方法细节。" in out
    assert "乙的方法。" in out                      # 别人的精读原样还在
    assert "# 参考文献" in out                      # 一级节没被吞


def test_replace_keeps_paper_head_intact():
    """篇内也要有边界：标题/裁决/摘要不属于精读节，一个字都不能动。"""
    out, _o, _n = bd.replace_closeread(
        MD, "alpha2021One", None, bd._render_closeread(_cr([("研究问题", [("新问题。", None)])])))
    head = _sections(out)["alpha2021One"].split("### 全文精读")[0]
    assert head == _sections(MD)["alpha2021One"].split("### 全文精读")[0]
    assert "**优先级**: `9.00`" in head and "第一篇摘要。" in head


def test_render_roundtrips_through_parser():
    """渲染 → parse_note_md 必须无损：渲染函数与 notes._paper_section 是两套代码，
    只要有一处不对齐，改完的 md 就解析不出 highlights，等于静默丢证据。"""
    cr = _cr([("实验方法", [("带标记的句子。", "方法论借鉴"), ("不带标记的句子。", None)]),
              ("局限与可质疑点", [("可反驳的点。", "可反驳观点")])])
    out, _o, _n = bd.replace_closeread(MD, "alpha2021One", None, bd._render_closeread(cr))
    tmp = Path(__file__).parent / "_tmp_bd.md"
    tmp.write_text(out, encoding="utf-8")
    try:
        got = {m["citekey"]: m for m in parse_note_md(tmp)}
    finally:
        tmp.unlink(missing_ok=True)
    hl = got["alpha2021One"]["highlights"]
    assert [h["text"] for h in hl] == ["带标记的句子。", "可反驳的点。"]   # 无 tag 的不进 highlights
    assert {h["section"] for h in hl} == {"实验方法", "局限与可质疑点"}
    assert got["alpha2021One"]["tag_counts"] == {"method": 1, "refutable": 1}
    assert got["beta2022Two"]["highlights"] == [                        # 邻居原样
        {"role": "method", "tag": "方法论借鉴", "section": "方法与数据", "text": "乙的方法。"}]


def test_ambiguous_citekey_refuses_to_guess():
    """同 citekey 出现两次（近重复文献各自成节）且 note_line 对不上时必须拒绝，
    不能瞎猜——猜错就是把新精读写进另一篇。"""
    dup = MD + MD.split("# 科研札记")[1]
    with pytest.raises(RuntimeError, match="拒绝盲改"):
        bd.replace_closeread(dup, "alpha2021One", None, ["### 全文精读", ""])


def test_note_line_disambiguates_duplicates():
    """给了正确 note_line 就该能在重复中精确认领。"""
    dup = MD + MD.split("# 科研札记")[1]
    lines = dup.splitlines()
    second = [i for i, ln in enumerate(lines)
              if _SECTION_RE.match(ln) and _SECTION_RE.match(ln).group(4) == "alpha2021One"][1]
    out, _o, _n = bd.replace_closeread(
        dup, "alpha2021One", second + 1, bd._render_closeread(_cr([("研究问题", [("第二处。", None)])])))
    assert out.count("甲的旧结论。") == 1          # 只改掉了第二处
    assert "第二处。" in out


def test_select_targets_excludes_non_fulltext():
    """纯题录/只读摘要不是本课题的缺口——它们没做过全文精读，是筛选阶段的决定。"""
    idx = {"papers": [
        {"citekey": "a", "reading_depth": "unknown-legacy", "has_full_text_reading": True,
         "decision": "INCLUDE", "priority_tier": "high"},
        {"citekey": "b", "reading_depth": "unknown-legacy", "has_full_text_reading": False},
        {"citekey": "c", "reading_depth": "chunked", "has_full_text_reading": True},
        {"citekey": "d", "reading_depth": "unknown-legacy", "has_full_text_reading": True,
         "duplicate_of": "a"},
        {"citekey": "MISSING-KEY-x", "reading_depth": "unknown-legacy",
         "has_full_text_reading": True},
    ]}
    assert [e["citekey"] for e in bd.select_targets(idx)] == ["a"]
    assert bd.select_targets(idx, decision="INCLUDE", tier="high")[0]["citekey"] == "a"
    assert bd.select_targets(idx, tier="low") == []


def test_sidecar_updates_scale_fields(tmp_path):
    """reading_depth 必须**显式**写进 sidecar：notes_index 只在条目缺该字段时才推断
    （auto + has_full_text_reading → 'unknown-legacy'），不写就会被打回原形。"""
    p = tmp_path / "n.index.json"
    p.write_text(json.dumps({"schema_version": 1, "papers": [
        {"citekey": "a", "has_full_text_reading": True, "reading_source": "arxiv"}]}),
        encoding="utf-8")
    cr = _cr([("研究问题", [("x", None)])])
    cr.body_chars, cr.body_chars_raw, cr.truncated = 120000, 167539, True
    cr.reading_depth = "chunked"
    bd.update_sidecar(p, "a", cr)
    e = json.loads(p.read_text(encoding="utf-8"))["papers"][0]
    assert e["reading_depth"] == "chunked"
    assert (e["fulltext_chars"], e["fulltext_chars_raw"], e["fulltext_truncated"]) == \
        (120000, 167539, True)
    assert "highlights" not in e     # 老 sidecar 不含该字段时不擅自添加：由 md 解析供给


def test_sidecar_absent_is_not_an_error(tmp_path):
    """83 篇 md 里只有 40 篇有 sidecar，其余走 notes_index 的 md-parse 分支。"""
    msg = bd.update_sidecar(tmp_path / "nope.index.json", "a", _cr([("x", [("y", None)])]))
    assert "无 sidecar" in msg


def test_sidecar_with_highlights_must_be_synced(tmp_path):
    """新 sidecar（含 highlights）会被 notes_index 直接沿用、不看 md——
    不同步就会 md 改了而库里纹丝不动。"""
    p = tmp_path / "n.index.json"
    p.write_text(json.dumps({"schema_version": 3, "papers": [
        {"citekey": "a", "highlights": [{"role": "citable", "tag": "可引用证据",
                                         "section": "旧节", "text": "旧句"}],
         "tag_counts": {"citable": 1}}]}), encoding="utf-8")
    cr = _cr([("实验方法", [("新句。", "方法论借鉴"), ("无标记。", None)])])
    msg = bd.update_sidecar(p, "a", cr)
    e = json.loads(p.read_text(encoding="utf-8"))["papers"][0]
    assert "highlights 同步" in msg
    assert [h["text"] for h in e["highlights"]] == ["新句。"]
    assert e["tag_counts"] == {"method": 1}
