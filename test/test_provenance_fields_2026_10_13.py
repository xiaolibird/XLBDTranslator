# -*- coding: utf-8 -*-
"""札记来源字段测试（可信度加固 WO-2）。

覆盖：筛选首轮/重试轮各用各自 meta、excluded sidecar 的 served_models 汇总、
entry_from_segment 的来源键（含老 segment 全 None）、manual bundle 复原（final_by + 脚本模型）、
classify_segments 手选覆盖分支（manual_pick 合法、原判保留）、改 citekey 后 sidecar 保留新键、
精读 meta / chunk_models、老 JSON 反序列化。全部假 LLM，不联网。
"""
import json
from datetime import date
from types import SimpleNamespace

from src.scholar import _citekey_utils as cu
from src.scholar import closereading as cr_mod
from src.scholar import ingest as ing
from src.scholar import notes_index as ni
from src.scholar import pdf_ingest as pi
from src.scholar import workflow as wfmod
from src.scholar.llm_client import CallMeta
from src.scholar.schema import (
    CloseReadSection, CloseReadSentence, CloseReading, FilterDecision,
    PaperMetadata, PaperSegment,
)
from src.scholar.workflow import ScholarWorkflow
from src.scholar.schema import ScholarSettings

_MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _make_settings(tmp_path, **processing_overrides):
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(_MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    settings.processing.output_dir = tmp_path / "out"
    for key, value in processing_overrides.items():
        setattr(settings.processing, key, value)
    return settings


def _make_paper(segment_id, title):
    return PaperSegment(
        segment_id=segment_id, paper_id="paper_{}".format(segment_id), original_abstract="",
        metadata=PaperMetadata(paper_id="paper_{}".format(segment_id), title=title,
                               source_email_id="email_1"))


_SECTIONS_JSON = json.dumps({"one_line": "x", "sections": [
    {"heading": "研究问题", "sentences": [{"text": "句子一。", "tag": None}]}]}, ensure_ascii=False)


def _meta(provider, served, *, fell_back=False, idx=0):
    return CallMeta(provider=provider, requested_model="req", sent_model="sent",
                    served_model=served, chain_index=idx, fell_back=fell_back,
                    degraded_partial=False, at="2026-10-13T10:00:00")


class _MetaLLM:
    """只在类上实现 call_with_meta 的假 LLM：按脚本顺序返回 (文本|异常, meta)。"""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def call_with_meta(self, prompt, **kw):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def call(self, prompt, **kw):
        return self.call_with_meta(prompt, **kw)[0]

    def rewind_chain(self):
        return True


def _verdict_json(ids):
    return json.dumps({"verdicts": [
        {"id": i, "decision": "INCLUDE", "bucket": ["B"], "flags": [], "role": "CITE_SUPPORT",
         "one_line": "有用", "confidence": 0.8} for i in ids]})


# ---------------- 筛选：首轮与重试轮各用各自的 meta ----------------

def test_filter_first_round_and_retry_round_use_own_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(wfmod, "FILTER_BATCH_SIZE", 1)
    wf = ScholarWorkflow(_make_settings(tmp_path, filter_mode="llm"))
    wf._llm_client = _MetaLLM([
        RuntimeError("首批整批失败"),                                       # 批 1 → deferred
        (_verdict_json([2]), _meta("deepseek", "ds-served-2")),             # 批 2 首轮由 A 应答
        (_verdict_json([1]), _meta("gemini", "gem-served-1", fell_back=True, idx=1)),  # 重试轮由 B 应答
    ])
    papers = [_make_paper(1, "Paper one"), _make_paper(2, "Paper two")]
    kept = wf._filter_by_llm(papers)
    assert [p.segment_id for p in kept] == [1, 2]
    d1, d2 = (p.filter_decision for p in kept)
    assert (d1.provider, d1.served_model, d1.fell_back) == ("gemini", "gem-served-1", True)
    assert (d2.provider, d2.served_model, d2.fell_back) == ("deepseek", "ds-served-2", False)
    assert d1.model == "fake-model" and d2.model == "fake-model"      # model 仍是请求值，语义不变
    # excluded sidecar 顶层汇总实际应答者（原 model 键保留）
    path = wf._write_excluded_sidecar()
    side = json.loads(path.read_text(encoding="utf-8"))
    assert side["served_models"] == {"deepseek:ds-served-2": 1, "gemini:gem-served-1": 1}
    assert side["model"] == "fake-model"


def test_filter_without_meta_leaves_fields_none(tmp_path, monkeypatch):
    """假 llm 只有 .call（无 call_with_meta）→ 三项为 None（视为未验证），不报错。"""
    wf = ScholarWorkflow(_make_settings(tmp_path, filter_mode="llm"))
    monkeypatch.setattr(wf, "_call_llm", lambda prompt, model=None, json_mode=None: _verdict_json([1]))
    kept = wf._filter_by_llm([_make_paper(1, "Paper one")])
    fd = kept[0].filter_decision
    assert (fd.provider, fd.served_model, fd.fell_back) == (None, None, None)
    assert wf._filter_served_models == {}


# ---------------- 索引条目来源键 ----------------

def _seg(fd=None, cr=None):
    return PaperSegment(
        segment_id=1, paper_id="p1", priority_score=0.5,
        metadata=PaperMetadata(paper_id="p1", title="T", doi="10.1/x",
                               publication_date=date(2026, 1, 1)),
        filter_decision=fd, close_reading=cr)


def _fd(**kw):
    d = dict(paper_id="p1", title="T", verdict="included", decision="INCLUDE", stage="llm_judge")
    d.update(kw)
    return FilterDecision(**d)


def test_entry_from_segment_has_all_provenance_keys():
    fd = _fd(model="req-model", prompt_version="filter-v3@abcd1234", provider="gemini",
             served_model="gemini-2.5-pro", fell_back=True)
    cr = CloseReading(model="req-cr", provider="claude-agent", served_model="claude-sonnet-5-5",
                      prompt_version="closeread-single@12345678", final_by="人工署名")
    e = cu.entry_from_segment(_seg(fd, cr), "k2026A", rank=0, total=1)
    for k in cu.PROVENANCE_FIELDS:
        assert k in e
    assert e["decision_source"] == "digest"
    assert e["decision_provider"] == "gemini" and e["decision_model"] == "gemini-2.5-pro"
    assert e["decision_model_verified"] is True
    assert e["decision_prompt_version"] == "filter-v3@abcd1234"
    assert e["decision_at"] == fd.decided_at.date().isoformat()
    assert e["closeread_model"] == "claude-sonnet-5-5" and e["closeread_model_verified"] is True
    assert e["closeread_final_by"] == "人工署名"
    assert e["provenance_origin"] == "native"
    json.dumps(e)   # 可落 sidecar


def test_entry_from_segment_unverified_falls_back_to_requested_model():
    e = cu.entry_from_segment(_seg(_fd(model="req-model", provider="deepseek"), None),
                              "k2026A", rank=0, total=1)
    assert e["decision_model"] == "req-model" and e["decision_model_verified"] is False
    assert e["closeread_model"] is None and e["closeread_model_verified"] is None


def test_entry_from_segment_legacy_segment_all_unknown():
    """老 segment（无应答者留痕）：来源键为 None，provenance_origin 不冒充 native。"""
    e = cu.entry_from_segment(_seg(_fd(model="m", prompt_version="filter-v2@x"),
                                   CloseReading(model="m2")), "k2026A", rank=0, total=1)
    assert e["decision_provider"] is None and e["closeread_provider"] is None
    assert e["provenance_origin"] is None
    assert e["decision_model_verified"] is False
    # 无裁决无精读：全部 None
    e2 = cu.entry_from_segment(_seg(), "k2026B", rank=0, total=1)
    assert all(e2[k] is None for k in cu.PROVENANCE_FIELDS)


def test_decision_source_by_stage_and_series():
    f = cu._decision_source
    assert f(_fd(stage="manual_pick"), "manual") == "manual"
    assert f(_fd(stage="manual_pick"), "auto") == "manual_pick"
    assert f(_fd(stage="llm_judge"), "manual") == "manual"       # 老 manual 的 stage 名不副实，按 series 纠正
    assert f(_fd(stage="keyword_fallback", verdict="undecided"), "auto") == "keyword_fallback"
    assert f(_fd(stage="llm_judge"), "auto") == "digest"
    assert f(None, "auto") is None


# ---------------- manual：bundle 复原 + build_segment ----------------

def test_build_segment_stage_is_manual_pick():
    meta = PaperMetadata(paper_id="pm", title="M")
    seg = pi.build_segment(meta, "abs", "摘要", None, "一句话")
    assert seg.filter_decision.stage == "manual_pick"
    e = cu.entry_from_segment(seg, "m2026A", rank=0, total=1, series="manual")
    assert e["decision_source"] == "manual"


def test_segment_from_bundle_splits_script_model_and_final_by():
    meta = PaperMetadata(paper_id="pm", title="M")
    script = CloseReading(model="sonnet", provider="claude-agent", served_model="claude-sonnet-5-5",
                          fell_back=False, chunk_models=["claude-sonnet-5-5"],
                          prompt_version="manual-synth@aaaabbbb",
                          sections=[CloseReadSection(heading="a", sentences=[CloseReadSentence(text="x")])])
    final = CloseReading(model="opus-gen+sonnet-adversarial",
                         sections=[CloseReadSection(heading="终稿", sentences=[CloseReadSentence(text="y")])])
    seg = pi.build_segment(meta, "abs", "摘要", script, "一句话")
    data = {"segment": seg.model_dump(mode="json"),
            "close_reading_script": script.model_dump(mode="json"),
            "close_reading_final": final.model_dump(mode="json")}
    cr = pi.segment_from_bundle(data).close_reading
    assert cr.sections[0].heading == "终稿"
    assert cr.final_by == "opus-gen+sonnet-adversarial"            # 自由文本署名原样保留
    assert cr.model == "sonnet" and cr.served_model == "claude-sonnet-5-5"   # 规范模型名取脚本
    assert cr.provider == "claude-agent" and cr.chunk_models == ["claude-sonnet-5-5"]
    # 终稿 model 为空 → 退而用 reviewer_label
    final2 = final.model_copy(update={"model": None})
    data2 = dict(data, close_reading_final=final2.model_dump(mode="json"),
                 cross_check_report={"reviewer_label": "GPT 交叉核验"})
    assert pi.segment_from_bundle(data2).close_reading.final_by == "GPT 交叉核验"
    # 没有脚本草稿：终稿自由文本不冒充规范模型名
    data3 = {"segment": seg.model_dump(mode="json"), "close_reading_script": None,
             "close_reading_final": final.model_dump(mode="json")}
    cr3 = pi.segment_from_bundle(data3).close_reading
    assert cr3.model is None and cr3.final_by == "opus-gen+sonnet-adversarial"
    e = cu.entry_from_segment(pi.segment_from_bundle(data), "m2026A", 0, 1, series="manual")
    assert e["closeread_model"] == "claude-sonnet-5-5" and e["closeread_final_by"] == "opus-gen+sonnet-adversarial"


# ---------------- ingest.classify_segments 手选覆盖 ----------------

def test_classify_segments_force_include_keeps_llm_original_decision(monkeypatch):
    excluded = _fd(verdict="excluded", decision="EXCLUDE", exclude_reason="X1", model="m",
                   provider="deepseek", served_model="ds")
    s1 = _seg(excluded)
    s2 = PaperSegment(segment_id=2, paper_id="p2", metadata=PaperMetadata(paper_id="p2", title="T2"))
    s3 = PaperSegment(segment_id=3, paper_id="p3", metadata=PaperMetadata(paper_id="p3", title="T3"),
                      filter_decision=_fd(paper_id="p3", verdict="undecided", decision=None,
                                          stage="keyword_fallback"))

    class FakeWF:
        def __init__(self, settings):
            pass

        def _step_filter_papers(self):
            pass

        def close_rag_resources(self):
            pass

    monkeypatch.setattr(wfmod, "ScholarWorkflow", FakeWF)
    ing.classify_segments([s1, s2, s3], SimpleNamespace(), force_include=True)

    fd1 = s1.filter_decision
    assert fd1.verdict == "included" and fd1.decision == "INCLUDE" and fd1.stage == "manual_pick"
    assert fd1.llm_decision_before_override == "EXCLUDE"          # 原判进条目，不只进日志
    assert fd1.served_model == "ds"                               # 原 LLM 应答者留痕保留
    fd2 = s2.filter_decision                                      # 无裁决分支：构造 manual_pick 不再报错
    assert fd2.stage == "manual_pick" and fd2.llm_decision_before_override is None
    assert s3.filter_decision.llm_decision_before_override is None   # 关键词回退无三态原判
    e = cu.entry_from_segment(s1, "k2026A", 0, 1)
    assert e["decision_source"] == "manual_pick"


# ---------------- sidecar / 索引 / 改键 ----------------

def test_sidecar_carries_provenance_and_survives_rename(tmp_path):
    def fd(**kw):
        return _fd(paper_id="pa", model="req", provider="deepseek", served_model="ds-v4",
                   prompt_version="filter-v3@abcd1234", **kw)
    from src.scholar.notes import write_notes
    seg = PaperSegment(
        segment_id=1, paper_id="pa", priority_score=0.9,
        metadata=PaperMetadata(paper_id="pa", title="Deep EHR", doi="10.1/aaa",
                               publication_date=date(2025, 3, 2)),
        filter_decision=fd(), close_reading=CloseReading(
            provider="claude-agent", served_model="claude-sonnet-5-5",
            sections=[CloseReadSection(heading="h", sentences=[CloseReadSentence(text="s")])]))
    write_notes([seg], {"pa": "public2025Deep"}, out_dir=tmp_path,
                digest_title="科研札记 · 2025-03（全文精读）",
                filename="科研札记_2025-03_全文精读",
                fallback_citekeys=True, emit_index_sidecar=True)
    stem = "科研札记_2025-03_全文精读"
    side_path = tmp_path / (stem + ".index.json")
    row = json.loads(side_path.read_text(encoding="utf-8"))["papers"][0]
    assert row["decision_model"] == "ds-v4" and row["closeread_provider"] == "claude-agent"
    assert row["provenance_origin"] == "native"

    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=side_path)
    e = dict(entries[0], references_json=stem + ".references.json", note_file=stem + ".md")
    assert e["decision_model_verified"] is True and e["closeread_model"] == "claude-sonnet-5-5"
    assert ni._rename_citekey_in_note(tmp_path, e, "public2025Deep", "public2025Deepb") == ni.RENAME_OK
    row2 = json.loads(side_path.read_text(encoding="utf-8"))["papers"][0]
    assert row2["citekey"] == "public2025Deepb"
    for k in cu.PROVENANCE_FIELDS:                                 # 改键后新键仍在
        assert row2[k] == row[k]
    # 老 sidecar（无新键）读取不报错
    old = json.loads(side_path.read_text(encoding="utf-8"))
    for r in old["papers"]:
        for k in cu.PROVENANCE_FIELDS:
            r.pop(k, None)
    side_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    entries2 = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                      ref_path=tmp_path / (stem + ".references.json"),
                                      sidecar_path=side_path)
    assert entries2[0].get("decision_model") is None


# ---------------- 精读 meta ----------------

def test_close_read_records_meta_and_prompt_version():
    seg = _seg()
    llm = _MetaLLM([(_SECTIONS_JSON, _meta("gemini", "gem-1", fell_back=True, idx=1))])
    cr = cr_mod.close_read(seg, "正文" * 50, "画像", llm, model="req-model", source="arxiv")
    assert (cr.provider, cr.served_model, cr.fell_back) == ("gemini", "gem-1", True)
    assert cr.model == "req-model"
    assert cr.prompt_version.startswith("closeread-single@") and len(cr.prompt_version) == len("closeread-single@") + 8


def test_close_read_with_plain_fake_llm_leaves_none():
    class Plain:
        def call(self, prompt, **kw):
            return _SECTIONS_JSON
    cr = cr_mod.close_read(_seg(), "正文" * 50, "画像", Plain(), model="m", source="arxiv")
    assert cr.provider is None and cr.served_model is None and cr.fell_back is None


def test_deep_close_read_uses_synth_meta_and_collects_chunk_models():
    seg = _seg()
    llm = _MetaLLM([
        (_SECTIONS_JSON, _meta("claude-agent", "claude-sonnet-5-5")),      # 块 1
        (_SECTIONS_JSON, _meta("gemini", "gem-synth", fell_back=True, idx=1)),   # 汇总步
    ])
    cr = cr_mod.deep_close_read(seg, "正文内容" * 100, "画像", llm, model="req", source="arxiv")
    assert cr is not None
    assert (cr.provider, cr.served_model, cr.fell_back) == ("gemini", "gem-synth", True)   # 汇总步的 meta
    assert cr.chunk_models == ["claude-sonnet-5-5"]
    assert cr.prompt_version.startswith("closeread-chunked@")
    assert cr.reading_depth == "chunked"


def test_chunk_models_dedup_and_none_cases():
    m = [_meta("a", "x"), _meta("b", "y"), _meta("a", "x"), _meta("c", None)]
    assert cr_mod.chunk_models_from_metas(m) == ["x", "y"]
    assert cr_mod.chunk_models_from_metas([]) is None


# ---------------- 老 JSON 反序列化 ----------------

def test_old_json_deserializes_without_new_fields():
    fd = FilterDecision.model_validate({
        "paper_id": "p", "title": "t", "verdict": "included", "stage": "llm_judge",
        "decision": "INCLUDE", "model": "m", "prompt_version": "filter-v2@x"})
    assert fd.provider is None and fd.llm_decision_before_override is None and fd.gate is None
    cr = CloseReading.model_validate({"sections": [], "model": "m"})
    assert cr.chunk_models is None and cr.final_by is None and cr.prompt_version is None
    assert FilterDecision.model_validate({"paper_id": "p", "title": "t", "verdict": "included",
                                          "stage": "manual_pick"}).stage == "manual_pick"
