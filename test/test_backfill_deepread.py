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


def test_expand_mode_takes_exactly_the_index_layer():
    """--expand 与默认口径互斥：它只要没做过全文精读的，且不看 reading_depth。"""
    idx = {"papers": [
        {"citekey": "a", "reading_depth": "unknown-legacy", "has_full_text_reading": True,
         "decision": "INCLUDE", "priority_tier": "high"},
        {"citekey": "b", "has_full_text_reading": False,
         "decision": "INCLUDE", "priority_tier": "high"},
        {"citekey": "c", "reading_depth": "chunked", "has_full_text_reading": False,
         "decision": "MAYBE", "priority_tier": "low"},
        {"citekey": "d", "has_full_text_reading": False, "duplicate_of": "b"},
    ]}
    assert [e["citekey"] for e in bd.select_targets(idx, expand=True)] == ["b", "c"]
    assert [e["citekey"] for e in bd.select_targets(
        idx, decision="INCLUDE", tier="high", expand=True)] == ["b"]
    # 默认口径一个 expand 目标都不许收，否则两批会互相重复烧额度
    assert [e["citekey"] for e in bd.select_targets(idx)] == ["a"]


def test_expand_ledger_is_separate(tmp_path):
    """两批共用一个账本会让 scan 的分母互相污染，且 --redo 语义错位。"""
    bd.save_ledger(tmp_path, {"done": {"x": {}}, "failed": {}}, True)
    assert (tmp_path / bd.EXPAND_LEDGER_NAME).exists()
    assert not (tmp_path / bd.LEDGER_NAME).exists()
    assert bd.load_ledger(tmp_path, True)["done"] == {"x": {}}
    assert bd.load_ledger(tmp_path)["done"] == {}


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


# ---------------- 本地 PDF 认领（--pdf-dir）----------------

def test_find_local_pdf_by_filename(tmp_path):
    """文件名等于 citekey / DOI 后半段 / arXiv 号时直接认，不必读 PDF。"""
    (tmp_path / "alpha2021One.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "s-0041-1733908.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "2410.17506.pdf").write_bytes(b"%PDF-1.4")
    assert bd.find_local_pdf(tmp_path, {"citekey": "alpha2021One"}).name == "alpha2021One.pdf"
    assert bd.find_local_pdf(tmp_path, {"citekey": "x", "doi": "10.1055/s-0041-1733908"}
                             ).name == "s-0041-1733908.pdf"
    assert bd.find_local_pdf(tmp_path, {"citekey": "y", "arxiv_id": "2410.17506"}
                             ).name == "2410.17506.pdf"


def test_find_local_pdf_falls_back_to_title(tmp_path, monkeypatch):
    """真实下载几乎都是浏览器默认名——实测 6 个文件只有 2 个恰好等于 DOI 后半段，
    其余是 `10262_The_Illusion_of_Generali.pdf`、`786a7b62-...pdf`、`Fekih  et al.pdf`。
    认不出就得读首页按标题认（实测这 6 个的标题词重合度全部 100%）。"""
    (tmp_path / "786a7b62-c8b8-403e.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "unrelated.pdf").write_bytes(b"%PDF-1.4")
    heads = {"786a7b62-c8b8-403e.pdf": "Evaluating the Impact of Covariate Lookback Times",
             "unrelated.pdf": "Totally different paper about quantum widgets"}
    monkeypatch.setattr("src.scholar.closereading._pdf_text_with_stats",
                        lambda p, **kw: (heads[p.name], len(heads[p.name])))
    bd._PDF_HEAD_CACHE.clear()
    got = bd.find_local_pdf(tmp_path, {"citekey": "anon2021Evaluating",
                                       "title": "Evaluating the Impact of Covariate "
                                                "Lookback Times on Performance"})
    assert got is not None and got.name == "786a7b62-c8b8-403e.pdf"


def test_find_local_pdf_refuses_weak_title_match(tmp_path, monkeypatch):
    """重合度不够就认不出——宁可让这篇失败，也不能把 A 的 PDF 喂给 B 的精读。"""
    (tmp_path / "somefile.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr("src.scholar.closereading._pdf_text_with_stats",
                        lambda p, **kw: ("Totally unrelated quantum widgets paper", 40))
    bd._PDF_HEAD_CACHE.clear()
    assert bd.find_local_pdf(tmp_path, {"citekey": "x",
                                        "title": "Deep learning for sepsis prediction"}) is None
    # 标题太短时直接放弃词面匹配（词少了随便撞上几个就超阈值）
    assert bd.find_local_pdf(tmp_path, {"citekey": "x", "title": "AI"}) is None


# ---------------- 额度耗尽 ≠ 抓不到全文（docs/bugs/2026-09-04-quota-failure-looks-like-no-fulltext.md） ----------------

@pytest.mark.parametrize("msg,expect", [
    ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota'}}", True),
    ("You've hit your session limit · resets 1:10pm", True),
    ("rate limit exceeded, retry later", True),
    ("Error code: 402 - Insufficient Balance", True),
    ("401 Unauthorized", True),
    ("Overloaded", True),
    ("Connection reset by peer", False),
    ("PDF 抽不出文本", False),
    ("正文 4021 字符，429 篇里的一篇", True),        # 含裸 429：宁可误判成模型侧（会计入熔断、可重跑）
])
def test_is_llm_unavailable(msg, expect):
    from src.scholar.closereading import is_llm_unavailable
    assert is_llm_unavailable(Exception(msg)) is expect


def test_close_read_diag_separates_no_body_from_llm_failure():
    """`close_read` 此前对「没正文」与「模型挂了」都只 return None，账本因此把两者记成同一个
    `no_output`——35 篇全文已到手的论文就这样进了待下载清单。"""
    from src.scholar import closereading as C
    from src.scholar.schema import PaperSegment, PaperMetadata
    seg = PaperSegment(segment_id=1, paper_id="p1",
                       metadata=PaperMetadata(paper_id="p1", title="T"))

    d = {}
    assert C.close_read(seg, "   ", "ri", llm=None, diag=d) is None
    assert d == {"no_body": True}

    class _Boom:
        def call(self, *a, **k):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: You exceeded your current quota")
    d = {}
    assert C.close_read(seg, "body " * 50, "ri", llm=_Boom(), diag=d) is None
    assert d["llm_unavailable"] is True and "RESOURCE_EXHAUSTED" in d["llm_error"]

    class _Net:
        def call(self, *a, **k):
            raise ConnectionError("Connection reset by peer")
    d = {}
    assert C.close_read(seg, "body " * 50, "ri", llm=_Net(), diag=d) is None
    assert d["llm_unavailable"] is False and "ConnectionError" in d["llm_error"]

    class _Junk:
        def call(self, *a, **k):
            return "not json"
    d = {}
    assert C.close_read(seg, "body " * 50, "ri", llm=_Junk(), diag=d) is None
    assert d == {"parse_failed": True}
    # 不传 diag 时行为与历史一致（不抛）
    assert C.close_read(seg, "   ", "ri", llm=None) is None


def test_deep_close_read_diag_flags_all_chunks_api_error(monkeypatch):
    from src.scholar import closereading as C
    from src.scholar import pdf_ingest as pi
    from src.scholar.schema import PaperSegment, PaperMetadata
    seg = PaperSegment(segment_id=1, paper_id="p1",
                       metadata=PaperMetadata(paper_id="p1", title="T"))
    monkeypatch.setattr(pi, "chunk_text", lambda t, **k: ["a", "b"])
    monkeypatch.setattr(pi, "deep_read_chunks",
                        lambda *a, **k: [{"_chunk": 1, "_error": True, "_api_error": True},
                                         {"_chunk": 2, "_error": True, "_api_error": True}])
    monkeypatch.setattr(pi, "synthesize_deep_read", lambda *a, **k: (None, "", True))
    d = {}
    assert C.deep_close_read(seg, "body " * 100, "ri", llm=None, diag=d) is None
    assert d["llm_unavailable"] is True
    # 块失败但不是额度类 → 不标 llm_unavailable
    monkeypatch.setattr(pi, "deep_read_chunks",
                        lambda *a, **k: [{"_chunk": 1, "_error": True}, {"_chunk": 2, "_error": True}])
    monkeypatch.setattr(pi, "synthesize_deep_read", lambda *a, **k: (None, "", False))
    d = {}
    assert C.deep_close_read(seg, "body " * 100, "ri", llm=None, diag=d) is None
    assert "llm_unavailable" not in d


def test_close_read_segments_diag_is_keyed_by_paper_id(monkeypatch):
    from src.scholar import closereading as C
    from src.scholar.schema import PaperSegment, PaperMetadata
    segs = [PaperSegment(segment_id=i, paper_id="p{}".format(i), priority_score=1.0,
                         metadata=PaperMetadata(paper_id="p{}".format(i), title="T{}".format(i)))
            for i in (1, 2)]

    def _boom(seg, *a, **k):
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
    monkeypatch.setattr(C, "close_read_segment", _boom)
    diag = {}
    assert C.close_read_segments(segs, "ri", llm=None, top_n=2, diag=diag) == 0
    assert set(diag) == {"p1", "p2"}
    assert all(v["llm_unavailable"] is True for v in diag.values())


@pytest.mark.parametrize("diag,err,reason_prefix,counts", [
    ({"llm_unavailable": True, "llm_error": "429 quota", "from_full_text": True},
     None, "llm_unavailable", True),
    ({}, RuntimeError("You exceeded your current quota"), "llm_unavailable", True),
    ({}, ValueError("boom"), "error:ValueError", True),
    ({"from_full_text": False}, None, "no_output", False),
])
def test_classify_failure(diag, err, reason_prefix, counts):
    """额度耗尽必须计入熔断连败——熔断存在的全部理由就是拦它；只有 expand 批里
    「干净的 no_output」（真抓不到全文，确定性）才不计。"""
    reason, llm_dead = bd.classify_failure(diag, err)
    assert reason.startswith(reason_prefix)
    assert llm_dead is counts


def test_deterministic_failures_excludes_llm_unavailable():
    """expand 批重跑跳过 failed 是对的（抓不到全文是确定性的），但 llm_unavailable 是暂时性的：
    跳过它等于让 35 篇全文已到手的论文永远留在待下载清单里等一个不需要的 PDF。"""
    led = {"done": {}, "failed": {
        "a2025": {"reason": "no_output"},
        "b2025": {"reason": "llm_unavailable:429 RESOURCE_EXHAUSTED"},
        "c2025": {"reason": "error:TimeoutError"},
        "d2025": {"reason": "abstract_only"},
        "e2025": {},                      # 老账本条目：无 reason 字段
    }}
    assert bd.deterministic_failures(led) == {"a2025", "c2025", "d2025", "e2025"}
    assert bd.deterministic_failures({"failed": {}}) == set()
    assert bd.deterministic_failures({}) == set()


def test_cmd_run_wires_the_streak_and_gate_helpers():
    """接线本身要被盯住：`classify_failure` / `counts_toward_streak` / 摘要级覆盖闸这三处
    helper 各自的单测再全，只要 `cmd_run` 里没真的调它们（或把表达式抄回旧的），就等于没修。
    第 2 轮变异 R38/R41 正是这么存活的——helper 测试全绿、call site 被换掉照样没人喊。"""
    import inspect
    src = inspect.getsource(bd.cmd_run)
    assert "reason, llm_dead = classify_failure(cr_diag, err)" in src
    assert "if counts_toward_streak(llm_dead, expand, err):" in src
    assert "skip |= deterministic_failures(led)" in src
    # 摘要级拒收发生在 LLM 已经读完之后，每篇都真花了额度——这条 continue 也必须过熔断，
    # 否则全文通路整体坏掉时会把整批额度烧光（第 2 轮压测 CONFIRMED）
    assert "if counts_toward_streak(False, expand, None):" in src


def test_abstract_level_never_overwrites_an_existing_fulltext_reading():
    """第 2 轮审计（MAJOR）：`abstract_only` 闸此前只对 `--expand` 生效。补深度批的目标集本就限定
    `has_full_text_reading=True`——全文这次抓不到、退化成摘要级时，只要新产出的带 tag 句子数不比
    旧的少就照常写盘，把 sidecar/索引的 `has_full_text_reading` 从真翻成假，回执还打 ✅、退出码 0。
    这里钉住判据本身（闸的条件表达式）。"""
    import inspect
    src = inspect.getsource(bd.cmd_run)
    assert ('if cr and not cr.from_full_text and '
            '(entry.get("has_full_text_reading") or not accept_abstract):') in src, \
        "摘要级覆盖闸必须挡住「覆盖既有全文精读」，并在没开 --accept-abstract 时挡住全部摘要级"


@pytest.mark.parametrize("accept_abstract,had_ft,should_refuse", [
    (False, False, True),   # 默认：两批都不收摘要级（连摘要都不供给，close_read 零成本返回）
    (False, True,  True),
    (True,  True,  True),   # 逃生门也**永不**放开「覆盖既有全文精读」
    (True,  False, False),  # 闭源篇目退而求其次留一份摘要级：只有这一格放行
])
def test_abstract_only_gate_predicate(accept_abstract, had_ft, should_refuse):
    """把闸的判据当成纯逻辑钉住（cmd_run 太重，不值得为它搭一整套 LLM/索引替身）。
    注意这条**只测判据本身**，接线由 test_cmd_run_wires_the_streak_and_gate_helpers 盯。"""
    entry = {"has_full_text_reading": had_ft}

    class _CR:
        from_full_text = False
    cr = _CR()
    refused = bool(cr and not cr.from_full_text
                   and (entry.get("has_full_text_reading") or not accept_abstract))
    assert refused is should_refuse


@pytest.mark.parametrize("llm_dead,expand,err,expect", [
    (True,  True,  None,             True),   # 额度耗尽：即便在 expand 批也必须计入连败
    (True,  False, None,             True),
    (True,  True,  RuntimeError("x"), True),
    (False, True,  None,             False),  # expand 批的干净 no_output：结构性缺口，不计
    (False, True,  RuntimeError("x"), True),  # 抛异常的故障：计
    (False, False, None,             True),   # 补深度批：任何失败都计
])
def test_counts_toward_streak(llm_dead, expand, err, expect):
    """R41 变异存活暴露的接线缺口：`classify_failure` 算出的 llm_dead 有没有真的接进熔断判断。
    额度耗尽走 catch 分支、err 恒为 None，正是被 `expand and err is None` 那条豁免吃掉的。"""
    assert bd.counts_toward_streak(llm_dead, expand, err) is expect


# ---------------- 第 3 轮压测的三条（BLOCKER + 2 MAJOR） ----------------

def test_backup_files_never_overwrites_the_original_within_one_run(tmp_path):
    """BLOCKER：`stamp` 在 cmd_run 循环外只算一次，同一份月度 md 会被改很多次（一篇一改）。
    照抄覆盖的话，第 2 篇写盘前存进备份目录的已经是**第 1 篇改完之后**的 md，原件当场丢失，
    `restore` 恢复出来的是混合态还报告成功。第一份进来的才是这次 run 的原件。"""
    md = tmp_path / "科研札记_2025-03_全文精读.md"
    sc = tmp_path / "科研札记_2025-03_全文精读.index.json"
    md.write_text("ORIGINAL", encoding="utf-8")
    sc.write_text('{"papers": [1]}', encoding="utf-8")
    b1 = bd.backup_files(tmp_path, "20260904T120000", [md, sc])
    assert (b1 / md.name).read_text(encoding="utf-8") == "ORIGINAL"

    md.write_text("AFTER FIRST PAPER", encoding="utf-8")      # 第 1 篇写盘
    b2 = bd.backup_files(tmp_path, "20260904T120000", [md, sc])   # 第 2 篇写盘前再备份
    assert b2 == b1
    assert (b1 / md.name).read_text(encoding="utf-8") == "ORIGINAL", "原件被第二次备份覆盖了"
    # 换一个 stamp（另一次 run）照常存新的
    b3 = bd.backup_files(tmp_path, "20260904T130000", [md])
    assert (b3 / md.name).read_text(encoding="utf-8") == "AFTER FIRST PAPER"


def test_find_local_pdf_refuses_a_long_review_that_contains_the_title(tmp_path, monkeypatch):
    """MAJOR：单向词面覆盖对短标题恒为 1.0——一篇把本标题实词全含进去的长综述会被当成本篇的
    PDF 认下来，于是**别篇论文的全文**被喂进这篇的精读。这是 fulltext 那个缺陷的第三份副本。"""
    (tmp_path / "survey.pdf").write_bytes(b"%PDF-1.4")
    head = ("A Comprehensive Survey of Federated Learning Methods for Electronic Health Records "
            "Including Missing Data Imputation Domain Adaptation Privacy Preservation Benchmarks "
            "Evaluation Protocols Clinical Deployment Considerations and Future Research Directions")
    monkeypatch.setattr("src.scholar.closereading._pdf_text_with_stats",
                        lambda p, **kw: (head, len(head)))
    bd._PDF_HEAD_CACHE.clear()
    got = bd.find_local_pdf(tmp_path, {"citekey": "x2025Missing",
                                       "title": "Missing Data Imputation Methods"})
    assert got is None, "长综述被当成了本篇的 PDF"
    # 真正配对的（首页就是这篇）仍认得出
    bd._PDF_HEAD_CACHE.clear()
    real = ("Missing Data Imputation Methods for Electronic Health Records\n"
            "Jane Doe, Wei Chen\nAbstract We propose ...")
    monkeypatch.setattr("src.scholar.closereading._pdf_text_with_stats",
                        lambda p, **kw: (real, len(real)))
    got = bd.find_local_pdf(tmp_path, {"citekey": "x2025Missing",
                                       "title": "Missing Data Imputation Methods for Electronic Health Records"})
    assert got is not None and got.name == "survey.pdf"


def test_replace_closeread_keeps_the_paper_separator_and_handwritten_notes():
    """MAJOR：精读节的终点此前只认下一个标题，于是精读节到下一篇 `## ` 之间的一切
    （build_digest_note 每篇后追加的 `---`、以及人手写在篇末的批注）都被整段替换掉了，
    与本函数 docstring「只动这一节」相悖。"""
    md = "\n".join([
        "## 🔴 高 1. Paper A [@a2025X]",
        "",
        "### 摘要",
        "",
        "abstract text",
        "",
        "### 全文精读 · 来源 `unpaywall`",
        "",
        "**【关键结论】**",
        "- 〔可引用证据〕旧结论。",
        "",
        "> 我的手写批注：这篇的 Table 3 有问题。",
        "",
        "---",
        "",
        "## 🟠 中 2. Paper B [@b2025Y]",
        "",
        "### 摘要",
        "",
        "b abstract",
        "",
    ])
    new_lines = ["### 全文精读 · 来源 `local-pdf`", "", "**【关键结论】**", "- 〔可引用证据〕新结论。"]
    out, old_n, new_n = bd.replace_closeread(md, "a2025X", 1, new_lines)
    assert "新结论" in out and "旧结论" not in out
    assert "我的手写批注" in out, "篇末手写批注被吞掉了"
    assert out.count("---") == 1, "篇末分隔线被吞掉了"
    assert "## 🟠 中 2. Paper B [@b2025Y]" in out and "b abstract" in out


def test_abstract_source_dir_is_gated_by_accept_abstract():
    """变异 R67 存活暴露的缺口：闸的「拒收」半边有守卫，「别白读」半边没有。

    默认关时**连摘要都不供给**（notes_dir=None → segment_from_entry 不回读 md 的摘要节 →
    close_read 立刻 return None，零 LLM）。若这里传了 notes_dir，close_read 会照常花一次
    LLM 读完摘要，再被下面那道闸原样丢弃——比不供给还贵。"""
    nd = Path("/tmp/x")
    assert bd.abstract_source_dir(nd, accept_abstract=True) == nd
    assert bd.abstract_source_dir(nd, accept_abstract=False) is None
    assert bd.abstract_source_dir(None, accept_abstract=True) is None

    import inspect
    src = inspect.getsource(bd.cmd_run)
    assert "abstract_source_dir(notes_dir, accept_abstract)" in src, \
        "cmd_run 必须经 abstract_source_dir 取摘要来源目录，不许直接传 notes_dir"


def test_insert_branch_puts_the_new_section_before_the_paper_separator():
    """第 5 轮终审 R5-3.3：`--expand` 把这条插入分支从「罕见兜底」变成了主路径。

    expand 批的目标集恰恰是「没做过全文精读」的索引层篇目，篇内本来就没有精读节，
    于是每次都走 cr_start is None 那一支。插入点只回退空行、不回退 `build_digest_note`
    每篇后追加的那条 `---` 的话，精读节会落在本篇分隔线之后——与 notes._paper_section
    的规范排布（精读节在前、`---` 在后）相反，并原样带进 vault 单篇页。
    """
    md = "\n".join([
        "# 科研札记",
        "",
        "## 🔴 高 1. Title [@wang2025Fed]",
        "",
        "### 摘要",
        "We study foo.",
        "",
        "---",
        "",
        "## 🔴 高 2. Other [@li2024Other]",
        "",
        "### 摘要",
        "Bar.",
        "",
        "---",
        "",
    ])
    new_lines = ["### 全文精读 · 来源 `unpaywall`", "**【方法】**", "- 句子。"]
    out, old_n, new_n = bd.replace_closeread(md, "wang2025Fed", 3, new_lines)
    lines = out.splitlines()
    cr = next(i for i, ln in enumerate(lines) if ln.startswith("### 全文精读"))
    sep = next(i for i, ln in enumerate(lines) if i > 2 and ln.rstrip() == "---")
    assert cr < sep, "精读节必须在本篇的 `---` 之前\n" + out
    assert old_n == 0 and new_n == 3
    # 第二篇一个字节都不许动
    assert "## 🔴 高 2. Other [@li2024Other]" in out and out.count("---") == md.count("---")
