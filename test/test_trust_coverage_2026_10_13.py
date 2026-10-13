# -*- coding: utf-8 -*-
"""可信度加固 WO-5：覆盖范围说明 + 证据不足拒答（设计 6.8 第 1-5 条）。

全部用假 LLM / 假 embedding / 小夹具，不联网、不调真实 LLM、不碰 output/ 与生产向量库
（notes_dir 一律指向 tmp_path）。
"""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scholar import coverage as C       # noqa: E402
from src.scholar import qa as Q             # noqa: E402
from src.scholar import topics as T         # noqa: E402
from src.scholar.embed_store import VectorStore   # noqa: E402
from src.scholar.vault import split_frontmatter    # noqa: E402

_spec = importlib.util.spec_from_file_location("ask_notes_script_wo5", ROOT / "scripts" / "ask_notes.py")
AN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AN)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _paper(ck, *, dedup=None, month="2025-03", dup_of=None, flags=(), decision="INCLUDE",
           series="auto", fulltext=True, hl=3):
    return {"citekey": ck, "dedup_key": dedup or "doi:" + ck.lower(), "month": month,
            "duplicate_of": dup_of, "flags": list(flags), "decision": decision,
            "series": series, "has_full_text_reading": fulltext, "highlights": hl,
            "title": "论文 " + ck}


INDEX = {
    "generated_at": "2026-10-13T08:00:00",
    "papers": [
        _paper("a1"), _paper("a2", month="2024-01"), _paper("a3", fulltext=False),
        _paper("a4", decision="MAYBE", hl=0, fulltext=False),
        _paper("m1", series="manual", month="2026-09"),
        _paper("b1", series="book", hl=[{"text": "x"}], month="2026-02"),
        # 重复条目（不计）：指向 a1 的 keeper 槽
        _paper("a1dup", dedup="doi:a1", dup_of="doi:a1@2025-03"),
        # 撤稿 keeper（不计）
        _paper("r1", flags=["RETRACTED"]),
    ],
}
INDEX["papers"][-2]["dedup_key"] = "doi:a1"


def _ev(ref, ck, score=0.8, text=None):
    return T.Evidence(ref=ref, citekey=ck, text=text or "{} 的证据句".format(ck), role="citable",
                      section="结果", note_file="札记.md", note_line=3, score=score, year=2025,
                      title="论文 " + ck)


def _ctx(index=INDEX, **kw):
    return C.build_context(index, **kw)


# ---------------------------------------------------------------------------
# 库统计（实时从索引计数）
# ---------------------------------------------------------------------------

def test_coverage_stats_counts_are_live_from_index():
    st = C.coverage_stats(INDEX)
    # keeper = 非重复、非撤稿：a1 a2 a3 a4 m1 b1
    assert st.library_keepers == 6
    assert (st.library_include, st.library_maybe) == (5, 1)
    assert st.evidence_pool_papers == 5 and st.title_only_papers == 1     # a4 无 highlight
    assert st.no_fulltext_papers == 2                                      # a3、a4
    assert st.series_counts == {"auto": 4, "manual": 1, "book": 1}
    assert st.month_range == ("2024-01", "2026-09")
    assert st.filter_certified is None            # 无认证文件 -> 无记录，不报错


def test_certification_missing_or_garbled_does_not_raise(tmp_path):
    assert C.load_certification(tmp_path / "nope.json", "filter-v3@x") == (None, None)
    bad = tmp_path / "c.json"
    bad.write_text("{not json", encoding="utf-8")
    assert C.load_certification(bad, "filter-v3@x") == (None, None)
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"certifications": [
        {"prompt_fingerprint": "filter-v3@x", "passed": True, "certified_at": "2026-10-20"}]}),
        encoding="utf-8")
    assert C.load_certification(ok, "filter-v3@x") == (True, "2026-10-20")
    assert C.load_certification(ok, "filter-v3@other") == (False, None)


def test_certification_text_in_coverage_block(tmp_path):
    prof = C.PageEvidenceProfile(3, 3, 0, 0)
    txt = "\n".join(C.render_coverage_block(_ctx(), prof, kind="topic"))
    assert "尚未经召回认证" in txt            # certification.json 不存在也只是这句话
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"certifications": [
        {"prompt_fingerprint": "filter-v3@x", "passed": True, "certified_at": "2026-10-20"}]}),
        encoding="utf-8")
    ctx = _ctx(certification_path=ok, current_filter_fingerprint="filter-v3@x")
    txt = "\n".join(C.render_coverage_block(ctx, prof, kind="topic"))
    assert "已通过召回认证（2026-10-20）" in txt


# ---------------------------------------------------------------------------
# 独立来源计数（6.8 第 2 条）
# ---------------------------------------------------------------------------

def test_independent_sources_dedup_retracted_and_min_sim():
    km = _ctx().keeper_of
    evs = [_ev("E1", "a1", 0.9), _ev("E2", "a1dup", 0.9),      # 重复条目与 keeper 合计 1
           _ev("E3", "r1", 0.9),                                # 撤稿不计
           _ev("E4", "a2", 0.54),                               # 低于 0.55 不计
           _ev("E5", "a3", 0.6)]
    s = C.fixed_sufficiency(evs, km)
    assert s.n_sources == 2 and s.refuse and s.min_sources == 3     # a1 + a3
    assert C.fixed_sufficiency(evs + [_ev("E6", "m1", 0.7)], km).refuse is False


# ---------------------------------------------------------------------------
# 镜像：同一输入，topics 与 qa 的覆盖说明块逐字相同（6.8 第 1 条）
# ---------------------------------------------------------------------------

def _extract_block(md: str) -> str:
    lines = md.split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.startswith("> " + C.COVERAGE_HEADER))
    j = i
    while j < len(lines) and lines[j].startswith(">"):
        j += 1
    return "\n".join(lines[i:j])


def _topic_synthesis(refs_per_claim):
    return {"summary": "总述", "sections": [{"heading": "要点", "claims": [
        {"text": "论断{}".format(i), "refs": r, "citekeys": []} for i, r in enumerate(refs_per_claim)]}],
        "disputes": [], "gaps": ["某缺口"], "inline_refs": []}


def _qa_dict(refs_per_claim):
    return {"answer": "总述", "points": [{"text": "论断{}".format(i), "refs": r}
                                          for i, r in enumerate(refs_per_claim)],
            "caveats": [], "gaps": ["某缺口"], "inline_refs": []}


def test_mirror_coverage_block_identical_in_topic_and_qa_and_badges():
    evs = [_ev("E1", "a1"), _ev("E2", "a2"), _ev("E3", "a3")]
    ctx = _ctx()
    refs = [["E1"], ["E1", "E2"]]          # 第一条单一来源，第二条两个来源
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    tmd = T.render_generated_block(spec, _topic_synthesis(refs), evs, coverage=ctx)
    qmd = Q.render_qa_block("问题？", _qa_dict(refs), evs, coverage=ctx)
    assert _extract_block(tmd) == _extract_block(qmd)
    # 内容要点都在（五点必须齐全）
    blk = _extract_block(tmd)
    assert "按研究画像筛选" in blk and "系统性缺席" in blk
    assert "有句级精读的 5 篇" in blk and "只有题录或摘要" in blk
    assert "独立来源" in blk and "只基于摘要" in blk
    assert "库里没有 ≠ 文献里没有" in blk
    assert "召回认证" in blk
    # 单一来源徽标：两侧都只落在“只有一个独立来源”的那条论断上，位置同为行末
    for md in (tmd, qmd):
        badge_lines = [ln for ln in md.split("\n") if C.SINGLE_SOURCE_BADGE in ln]
        assert len(badge_lines) == 1 and badge_lines[0].endswith(C.SINGLE_SOURCE_BADGE)
        assert "论断0" in badge_lines[0]
    # 缺口免责共用一处文案生成：两侧都带“不等于库里没有”，且数字一致
    assert "不等于库里没有" in tmd and "不等于库里没有" in qmd
    assert tmd.count("不等于库里没有") == 1 and qmd.count("不等于库里没有") == 1
    assert C.render_gap_disclaimer("topic", 3) in tmd
    assert C.render_gap_disclaimer("qa", 3) in qmd


def test_no_coverage_keeps_v1_output_byte_identical():
    evs = [_ev("E1", "a1")]
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    md = T.render_generated_block(spec, _topic_synthesis([["E1"]]), evs)
    assert "本页覆盖范围" not in md and C.SINGLE_SOURCE_BADGE not in md
    qmd = Q.render_qa_block("问题？", _qa_dict([["E1"]]), evs)
    assert "本页覆盖范围" not in qmd and C.SINGLE_SOURCE_BADGE not in qmd


def test_forced_status_is_disclosed_in_block():
    ctx = _ctx()
    ctx.status = C.EVIDENCE_STATUS_FORCED
    blk = "\n".join(C.render_coverage_block(ctx, C.PageEvidenceProfile(1, 1, 0, 0), kind="qa"))
    assert "强制生成" in blk


def test_answer_notice_only_when_points_sources_insufficient():
    evs = [_ev("E1", "a1"), _ev("E2", "a2")]
    ctx = _ctx()
    one = Q.render_qa_block("问？", _qa_dict([["E1"]]), evs, coverage=ctx)
    two = Q.render_qa_block("问？", _qa_dict([["E1"], ["E2"]]), evs, coverage=ctx)
    assert "库内证据不足：下面的回答只依据 1 篇文献" in one
    assert "库内证据不足" not in two


def test_coverage_text_survives_qa_page_audit(tmp_path):
    """覆盖说明块含 E 开头的词（INCLUDE/MAYBE）和数字，不得被残留编号扫描误报；
    页面仍能被 --verify 与 list_qa_pages 读取。"""
    evs = [_ev("E1", "a1"), _ev("E2", "a2")]
    ctx = _ctx()
    qa = _qa_dict([["E1"], ["E2"]])
    rep = T.ValidationReport(kept_claims=2)
    path, status = Q.write_qa_page(tmp_path / "qa", "qa-x", "问？", qa, evs, rep, coverage=ctx)
    assert status == "new"
    idx = {"papers": [{"citekey": e.citekey, "highlights": [{"text": e.text}]} for e in evs]}
    (a,) = Q.audit_qa_pages(tmp_path / "qa", idx)
    assert a.ok and a.residual_refs == [] and not a.outdated
    assert Q.list_qa_pages(tmp_path / "qa")[0].slug == "qa-x"
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["qa_schema"] == Q.QA_SCHEMA_VERSION == 4
    assert fm["evidence_status"] == "ok" and fm["n_independent_sources"] == 2
    assert fm["coverage_pool_papers"] == 5 and fm["coverage_title_only"] == 1


# ---------------------------------------------------------------------------
# 合成模型留痕
# ---------------------------------------------------------------------------

class _MetaLLM:
    """类上有 call_with_meta 的假客户端（模拟真 LLMClient）。"""
    def __init__(self, reply, served="deepseek-v4-pro", provider="deepseek"):
        self.reply, self.served, self.provider, self.calls = reply, served, provider, 0

    def call_with_meta(self, prompt, **kw):
        self.calls += 1
        return self.reply, SimpleNamespace(provider=self.provider, served_model=self.served,
                                           fell_back=False)

    def close(self):
        pass


class _PlainLLM:
    def __init__(self, reply):
        self.reply, self.calls, self.prompts = reply, 0, []

    def call(self, prompt, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        return self.reply


class _ForbiddenLLM:
    def call(self, *a, **k):
        raise AssertionError("证据不足时不应调用 LLM")

    def call_with_meta(self, *a, **k):
        raise AssertionError("证据不足时不应调用 LLM")


def test_synth_fields_verified_vs_unverified():
    assert C.synth_fields({"provider": "deepseek", "served_model": "m1"}, "req") == {
        "synth_provider": "deepseek", "synth_model": "m1", "synth_model_verified": True}
    # 取不到应答者：记请求值，标未验证
    assert C.synth_fields({"provider": "claude-agent", "served_model": None}, "sonnet") == {
        "synth_provider": "claude-agent", "synth_model": "sonnet", "synth_model_verified": False}
    assert C.synth_fields({}, None)["synth_model_verified"] is False


# ---------------------------------------------------------------------------
# 概念页：闸门 / 存根 / 强制 / 倒退保护 / 旧页兼容（6.8 第 3-5 条）
# ---------------------------------------------------------------------------

def _rec(ck, score, *, section="结果", i=0):
    other = float((1 - score ** 2) ** 0.5)
    return ({"id": "h:{}:{}".format(ck, i), "level": "highlight", "citekey": ck, "role": "citable",
             "section": section, "month": None, "series": None, "tier": None, "bucket": [],
             "year": 2025, "has_full_text": True, "note_file": "x.md", "note_line": 1,
             "text": "{} 的证据句".format(ck)}, [score, other])


def _store(pairs):
    return VectorStore(meta={}, records=[r for r, _ in pairs],
                       mat=np.array([v for _, v in pairs], dtype=np.float32))


class _Embed:
    def embed(self, texts):
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]


_SPEC = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"], buckets=[],
                    exclude_sections=[])
_GOOD_REPLY = ('{"sections": [{"heading": "h", "claims": ['
               '{"text": "新论断", "evidence": ["E1"]}, {"text": "二论断", "evidence": ["E2", "E3"]}]}]}')


def _build(topics_dir, store, llm, **kw):
    return T.build_topic(_SPEC, store=store, embed_client=_Embed(), llm=llm, index=INDEX,
                         topics_dir=topics_dir, template="模板", min_sim=0.5, **kw)


def test_topic_insufficient_writes_stub_without_llm(tmp_path):
    store = _store([_rec("a1", 0.9), _rec("a2", 0.8)])         # 只有 2 个独立来源
    r = _build(tmp_path / "topics", store, _ForbiddenLLM())
    assert r.status == "insufficient" and r.evidence_status == "insufficient"
    assert "2 < 3" in r.error and "未调用 LLM" in r.error
    path = tmp_path / "topics" / "t.md"
    fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["evidence_status"] == "insufficient" and fm["n_independent_sources"] == 2
    assert fm["topic_schema"] == 2 and fm["synth_model"] is None and fm["synth_model_verified"] is False
    txt = path.read_text(encoding="utf-8")
    assert "## 库内证据不足" in txt and "## 下一步" in txt
    assert "独立来源：2 篇达标，要求至少 3 篇" in txt and "相似度不低于 0.55" in txt
    assert "build_topics.py --topic t --force-synthesize" in txt
    assert "本页覆盖范围" in txt and "## 本页证据（2 条 · 2 篇）" in txt
    assert "新论断" not in txt


def test_topic_stub_keeps_user_zone_and_recovers_without_polluting_prompt(tmp_path):
    d = tmp_path / "topics"
    _build(d, _store([_rec("a1", 0.9), _rec("a2", 0.8)]), _ForbiddenLLM())
    page = d / "t.md"
    page.write_text(page.read_text(encoding="utf-8") + "\n用户批注：别丢\n", encoding="utf-8")
    # 再次仍不足：批注保留
    r2 = _build(d, _store([_rec("a1", 0.9), _rec("a2", 0.8)]), _ForbiddenLLM())
    assert r2.status == "insufficient" and "用户批注：别丢" in page.read_text(encoding="utf-8")
    # 证据补足后正常合成：上一版是存根，不得作为“上一版答案”喂回 prompt
    llm = _PlainLLM(_GOOD_REPLY)
    r3 = _build(d, _store([_rec("a1", 0.9), _rec("a2", 0.88), _rec("a3", 0.86)]), llm)
    assert r3.status == "merged" and r3.evidence_status == "ok" and llm.calls == 1
    assert "库内证据不足" not in llm.prompts[0] and "本页覆盖范围" not in llm.prompts[0]
    txt = page.read_text(encoding="utf-8")
    assert "用户批注：别丢" in txt and "## 库内证据不足" not in txt
    fm, _ = split_frontmatter(txt)
    assert fm["evidence_status"] == "ok"


def test_topic_force_synthesize_calls_llm_and_marks_forced(tmp_path):
    llm = _MetaLLM(_GOOD_REPLY)
    store = _store([_rec("a1", 0.9), _rec("a2", 0.8)])
    r = _build(tmp_path / "topics", store, llm, force_synthesize=True)
    assert r.status == "new" and r.evidence_status == "forced" and llm.calls == 1
    txt = (tmp_path / "topics" / "t.md").read_text(encoding="utf-8")
    fm, _ = split_frontmatter(txt)
    assert fm["evidence_status"] == "forced" and "强制生成" in txt
    # 合成模型留痕：应答者已验证
    assert (fm["synth_provider"], fm["synth_model"], fm["synth_model_verified"]) == (
        "deepseek", "deepseek-v4-pro", True)
    assert fm["topic_schema"] == 2 and "n_independent_sources" in fm
    # 单一来源徽标：“新论断”只引 a1
    assert any(C.SINGLE_SOURCE_BADGE in ln and "新论断" in ln for ln in txt.split("\n"))
    assert fm["single_source_claims"] >= 1


def test_topic_unverified_model_when_no_meta(tmp_path):
    llm = _PlainLLM(_GOOD_REPLY)          # 无 call_with_meta：取不到应答者
    r = _build(tmp_path / "topics", _store([_rec("a1", 0.9), _rec("a2", 0.88), _rec("a3", 0.86)]),
               llm, model="sonnet")
    assert r.status == "new"
    fm, _ = split_frontmatter((tmp_path / "topics" / "t.md").read_text(encoding="utf-8"))
    assert fm["synth_model"] == "sonnet" and fm["synth_model_verified"] is False


def test_topic_regression_guard_refuses_to_overwrite_good_page(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    page = d / "t.md"
    page.write_text(T.assemble('---\ntopic: t\nn_independent_sources: 10\nevidence_status: ok\n---',
                               "旧正文：很好的一页", T.DEFAULT_USER_ZONE), encoding="utf-8")
    before = page.read_text(encoding="utf-8")
    r = _build(d, _store([_rec("a1", 0.9), _rec("a2", 0.8)]), _ForbiddenLLM())
    assert r.status == "failed" and "倒退" in r.error
    assert page.read_text(encoding="utf-8") == before
    # 旧 v1 页没有 n_independent_sources 键：退而用 n_papers
    page.write_text(T.assemble('---\ntopic: t\nn_papers: 12\n---', "旧", T.DEFAULT_USER_ZONE),
                    encoding="utf-8")
    assert _build(d, _store([_rec("a1", 0.9)]), _ForbiddenLLM()).status == "failed"
    # --force-synthesize 可越过
    assert _build(d, _store([_rec("a1", 0.9)]), _PlainLLM(_GOOD_REPLY),
                  force_synthesize=True).status == "merged"


def test_topic_old_v1_page_without_new_keys_rebuilds(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    page = d / "t.md"
    page.write_text(T.assemble('---\ntopic: t\ntopic_schema: 1\nmy_key: "kept"\n---',
                               "旧正文", T.DEFAULT_USER_ZONE + "\n旧批注\n"), encoding="utf-8")
    r = _build(d, _store([_rec("a1", 0.9), _rec("a2", 0.88), _rec("a3", 0.86)]), _PlainLLM(_GOOD_REPLY))
    assert r.status == "merged"
    fm, _ = split_frontmatter(page.read_text(encoding="utf-8"))
    assert fm["my_key"] == "kept" and fm["topic_schema"] == 2 and fm["evidence_status"] == "ok"
    assert "旧批注" in page.read_text(encoding="utf-8")
    # v2 页对 audit_topic_pages 照常
    idx = {"papers": [{"citekey": k, "highlights": [{"text": "{} 的证据句".format(k)}]}
                      for k in ("a1", "a2", "a3")]}
    assert T.audit_topic_pages(d, idx)[0].ok


def test_topic_dry_run_reports_would_stub_without_writing(tmp_path):
    d = tmp_path / "topics"
    r = _build(d, _store([_rec("a1", 0.9)]), _ForbiddenLLM(), dry_run=True)
    assert r.status == "skipped" and "存根" in r.error and not (d / "t.md").exists()


def test_read_existing_strips_coverage_and_drops_stub(tmp_path):
    d = tmp_path / "topics"
    _build(d, _store([_rec("a1", 0.9), _rec("a2", 0.8)]), _ForbiddenLLM())
    _t, fm, prev = T.read_existing(d / "t.md")
    assert fm["evidence_status"] == "insufficient" and prev == ""
    _build(d, _store([_rec("a1", 0.9), _rec("a2", 0.88), _rec("a3", 0.86)]), _PlainLLM(_GOOD_REPLY))
    _t, fm, prev = T.read_existing(d / "t.md")
    assert "新论断" in prev and "本页覆盖范围" not in prev


def test_summarize_build_topics_run_lists_insufficient_without_failing():
    out = T.summarize_build_topics_run("▶ a\n  📭 a   证据 2 条 / 2 篇\n  ✅ b   证据", "", 0)
    assert out.ok is True and out.insufficient == ["a"]
    out2 = T.summarize_build_topics_run("📭 a x\n❌ c y", "", 1)
    assert out2.ok is False and out2.insufficient == ["a"] and "c" in out2.detail


# ---------------------------------------------------------------------------
# 全库扫描等价性 + 问答页分布判据
# ---------------------------------------------------------------------------

def test_retrieval_mask_matches_retrieve_evidence_filters():
    pairs = [_rec("a1", 0.9), _rec("a2", 0.8, section="对我研究的联想"),
             _rec("a3", 0.7)]
    pairs[2][0]["role"] = "method"
    pairs[2][0]["level"] = "highlight"
    pairs.append(({**_rec("a4", 0.6)[0], "level": "paper"}, _rec("a4", 0.6)[1]))
    store = _store(pairs)
    spec = T.TopicSpec(slug="t", title="t", question="q", queries=["q"], roles=["citable"],
                       exclude_sections=["对我研究的联想"], buckets=[])
    mask = C.retrieval_mask(spec, store)
    got = T.retrieve_evidence(spec, store, _Embed(), min_sim=0.0, relative_alpha=0.0,
                              per_query_k=100, per_paper_cap=100)
    assert {store.records[i]["citekey"] for i in np.where(mask)[0]} == {e.citekey for e in got} == {"a1"}


def _qa_store(n_sources, n_bg=60):
    """n_bg 条低分背景 + n_sources 个高分独立来源（各一条）。"""
    pairs = [_rec("bg{}".format(i), 0.1, i=i) for i in range(n_bg)]
    pairs += [_rec(ck, 0.9) for ck in ("a1", "a2", "a3", "m1", "b1")[:n_sources]]
    return _store(pairs)


def test_qa_dynamic_verdict_floor_and_min_sources():
    spec = Q.build_qa_spec("一个问题？")
    km = C.keeper_map(INDEX)
    v1 = C.qa_dynamic_verdict(spec, _qa_store(1), _Embed(), km)
    v2 = C.qa_dynamic_verdict(spec, _qa_store(2), _Embed(), km)
    assert v1.refuse and v1.n_sources == 1 and v1.method == "dynamic"
    assert not v2.refuse and v2.n_sources == 2
    assert v2.sim_threshold >= 0.58 and v2.detail["floor"] == 0.58
    assert v1.top1 == pytest.approx(0.9, abs=1e-3)


class _FakeClientCls:
    """顶替 AN.LLMClient；构造即记录，close 无副作用。"""
    reply = ('{"answer": "总述", "points": [{"text": "依据一", "evidence": ["E1"]},'
             ' {"text": "依据二", "evidence": ["E2"]}], "caveats": [], "gaps": []}')
    built = 0
    calls = 0

    def __init__(self, *a, **k):
        type(self).built += 1

    def call_with_meta(self, prompt, **kw):
        type(self).calls += 1
        return self.reply, SimpleNamespace(provider="deepseek", served_model="deepseek-v4-flash",
                                           fell_back=False)

    def call(self, prompt, **kw):                       # 审计用；本测试 audit=off 不会走到
        return "{}"

    def close(self):
        pass


def _run_one(tmp_path, store, monkeypatch, **kw):
    from src.scholar.schema import ScholarSettings
    _FakeClientCls.built = _FakeClientCls.calls = 0
    monkeypatch.setattr(AN, "LLMClient", _FakeClientCls)
    monkeypatch.setattr(AN, "get_default_research_interests", lambda: "研究方向")
    qa_dir = tmp_path / "notes" / "topics" / "qa"          # notes_dir 指向 tmp，不碰生产向量库
    return AN._run_one(
        "一个问题？", "qa-x", qa_dir=qa_dir, index=INDEX, titles={}, topic_specs=[],
        template="{{QUESTION}} {{EVIDENCE_BLOCK}} {{RESEARCH_INTERESTS}} {{PREVIOUS_ANSWER}}",
        store=store, embed_client=_Embed(), settings=ScholarSettings(), topic_vecs=None,
        extra_queries=[], roles=[], min_sim=0.55, max_evidence=28, per_paper_cap=3, model=None,
        dry_run=kw.pop("dry_run", False), no_dedup_check=True, no_topic_check=True,
        audit_mode="off", **kw), qa_dir


def test_ask_notes_insufficient_archives_stub_without_llm(tmp_path, monkeypatch, capsys):
    out, qa_dir = _run_one(tmp_path, _qa_store(1), monkeypatch)
    assert out.status == "insufficient" and _FakeClientCls.built == 0       # 连 LLM 客户端都没建
    txt = (qa_dir / "qa-x.md").read_text(encoding="utf-8")
    fm, _ = split_frontmatter(txt)
    assert fm["evidence_status"] == "insufficient" and fm["n_independent_sources"] == 1
    assert fm["qa_schema"] == 4 and fm["n_points"] == 0 and fm["synth_model"] is None
    assert "## 库内证据不足" in txt and "分布判据" in txt and "动态阈值" in txt
    assert "--force-synthesize" in txt and "本页覆盖范围" in txt
    assert Q.list_qa_pages(qa_dir)[0].slug == "qa-x"
    assert "未调用 LLM" in capsys.readouterr().out


def test_ask_notes_force_synthesize_and_model_trace(tmp_path, monkeypatch):
    out, qa_dir = _run_one(tmp_path, _qa_store(1), monkeypatch, force_synthesize=True)
    assert out.status == "new" and _FakeClientCls.calls == 1
    fm, _ = split_frontmatter((qa_dir / "qa-x.md").read_text(encoding="utf-8"))
    assert fm["evidence_status"] == "forced"
    assert (fm["synth_provider"], fm["synth_model"], fm["synth_model_verified"]) == (
        "deepseek", "deepseek-v4-flash", True)


def test_ask_notes_sufficient_goes_through_and_dry_run_never_writes(tmp_path, monkeypatch):
    out, qa_dir = _run_one(tmp_path, _qa_store(2), monkeypatch)
    assert out.status == "new" and _FakeClientCls.calls == 1
    fm, _ = split_frontmatter((qa_dir / "qa-x.md").read_text(encoding="utf-8"))
    assert fm["evidence_status"] == "ok" and fm["n_independent_sources"] == 2
    out2, qa_dir2 = _run_one(tmp_path / "d2", _qa_store(1), monkeypatch, dry_run=True)
    assert out2.status == "dry_run" and not qa_dir2.exists()


def test_qa_regression_guard_and_old_v3_page_compat(tmp_path, monkeypatch):
    qa_dir = tmp_path / "notes" / "topics" / "qa"
    qa_dir.mkdir(parents=True)
    # v3 旧页：没有任何新键，n_papers=9（以前够）
    fm = ('---\nqa: "qa-x"\ntitle: "一个问题？"\nquestion: "一个问题？"\ntype: "qa"\nqa_schema: 3\n'
          'generated_at: "2026-09-01T00:00:00"\nn_papers: 9\nn_points: 3\n---')
    page = qa_dir / "qa-x.md"
    page.write_text(T.assemble(fm, "# 一个问题？\n\n> 旧答案", T.DEFAULT_USER_ZONE,
                               generator=Q.QA_GENERATOR), encoding="utf-8")
    assert Q.list_qa_pages(qa_dir)[0].n_points == 3          # 旧页照常读取
    before = page.read_text(encoding="utf-8")
    out, _ = _run_one(tmp_path, _qa_store(1), monkeypatch)
    assert out.status == "failed" and "倒退" in out.detail
    assert page.read_text(encoding="utf-8") == before
    # 来源够了：旧页正常重建，新键补齐
    out2, _ = _run_one(tmp_path, _qa_store(2), monkeypatch)
    assert out2.status == "merged"
    fm2, _ = split_frontmatter(page.read_text(encoding="utf-8"))
    assert fm2["qa_schema"] == 4 and fm2["evidence_status"] == "ok"


def test_write_qa_page_regressed_status(tmp_path):
    qa_dir = tmp_path / "qa"
    ev = [_ev("E1", "a1"), _ev("E2", "a2")]
    rep = T.ValidationReport()
    Q.write_qa_page(qa_dir, "qa-x", "问？", _qa_dict([["E1"], ["E2"]]), ev, rep,
                    coverage=_ctx())                                  # 先落一页正常页
    stub = C.Sufficiency("dynamic", True, 1, 2, 0.58, 0.7,
                         {"floor": 0.58, "z_star": 4.0, "floor_binding": True,
                          "bg_n": 100, "bg_mean": 0.1, "bg_std": 0.05, "expected_chance": 1.0})
    empty = {"answer": "", "points": [], "caveats": [], "gaps": [], "inline_refs": []}
    _p, st = Q.write_qa_page(qa_dir, "qa-x", "问？", empty, ev, rep, coverage=_ctx(), stub=stub)
    assert st == "regressed"
