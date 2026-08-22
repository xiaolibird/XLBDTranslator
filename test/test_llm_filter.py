# -*- coding: utf-8 -*-
"""白名单 LLM 裁决与过滤审计回归测试。

契约：
1. 黑名单保持确定性关键词预过滤，命中即剔除且产出 excluded 记录；
2. 白名单裁决支持 llm / keyword 两种模式，llm 模式下每次裁决
   （判定+理由+模型+prompt 版本）都写入 excluded sidecar；
3. LLM 单批失败/单篇缺失时回退关键词白名单，不中断整体流程；
   回退时白名单未命中不得静默 EXCLUDE——挂 undecided 进 DigestOutput.undecided_segments 待人工复核；
4. scholar_main 不再硬编码覆盖 env 配置的黑白名单。

全部 mock LLM 调用，不发真实请求、不涉及真实密钥。
"""
import json
from pathlib import Path

import pytest

from src.scholar.schema import DigestStatus, ScholarSettings, PaperMetadata, PaperSegment
from src.scholar.workflow import ScholarWorkflow

MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _make_settings(tmp_path: Path, **processing_overrides) -> ScholarSettings:
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    settings.processing.output_dir = tmp_path / "out"
    for key, value in processing_overrides.items():
        setattr(settings.processing, key, value)
    return settings


def _make_paper(segment_id: int, title: str, abstract: str = "") -> PaperSegment:
    return PaperSegment(
        segment_id=segment_id,
        paper_id="paper_{}".format(segment_id),
        original_abstract=abstract,
        metadata=PaperMetadata(
            paper_id="paper_{}".format(segment_id),
            title=title,
            source_email_id="email_1",
        ),
    )


# ==================== 关键词匹配 ====================

def test_match_keyword_word_boundary(tmp_path):
    """ASCII 关键词整词匹配：'Gene' 不命中 'general'，命中 'gene expression'"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    miss = _make_paper(1, "A general framework for learning")
    hit = _make_paper(2, "Gene expression analysis")
    assert wf._match_keyword(miss, ["Gene"]) is None
    assert wf._match_keyword(hit, ["Gene"]) == "Gene"


def test_match_keyword_plural_and_chinese(tmp_path):
    """复数容忍（LLM->LLMs）与中文包含匹配"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    plural = _make_paper(1, "LLMs for clinical notes")
    chinese = _make_paper(2, "基于基因组学的分析")
    assert wf._match_keyword(plural, ["LLM"]) == "LLM"
    assert wf._match_keyword(chinese, ["基因"]) == "基因"


# ==================== keyword 模式白名单 ====================

def test_filter_by_whitelist_records_decisions(tmp_path):
    """关键词白名单：入选/排除都要产出裁决记录"""
    settings = _make_settings(tmp_path, whitelist=["EHR"], filter_mode="keyword")
    wf = ScholarWorkflow(settings)
    papers = [
        _make_paper(1, "EHR data mining with graphs"),
        _make_paper(2, "Astronomy observations of quasars"),
    ]
    kept = wf._filter_by_whitelist(papers, stage="whitelist_keyword")

    assert [p.segment_id for p in kept] == [1]
    assert len(wf.included_decisions) == 1
    assert wf.included_decisions[0].stage == "whitelist_keyword"
    assert "EHR" in wf.included_decisions[0].reason

    assert len(wf.excluded) == 1
    rec = wf.excluded[0]
    assert rec["paper_id"] == "paper_2"
    assert rec["stage"] == "whitelist_keyword"
    assert rec["decision"]["verdict"] == "excluded"
    # 排除论文必须固化完整元数据与摘要（可追溯）
    assert rec["metadata"]["title"] == "Astronomy observations of quasars"
    assert "original_abstract" in rec


# ==================== llm 模式裁决 ====================

def test_filter_by_llm_records_full_audit(tmp_path, monkeypatch):
    """LLM 裁决：判定+理由+模型+prompt 版本全部入记录（filter-v3 顶层对象格式）"""
    settings = _make_settings(tmp_path, filter_mode="llm")
    wf = ScholarWorkflow(settings)
    papers = [
        _make_paper(1, "Graph learning for EHR"),
        _make_paper(2, "Quasar spectroscopy"),
    ]

    response = """```json
{"verdicts": [
  {"id": 1, "decision": "INCLUDE", "bucket": ["B"], "flags": [], "role": "CITE_SUPPORT",
   "one_line": "图方法可迁移到 EHR", "confidence": 0.8},
  {"id": 2, "decision": "EXCLUDE", "exclude_reason": "X1", "flags": [], "role": "NONE",
   "one_line": "天文学研究，与医疗无关", "confidence": 0.9}
]}
```"""
    monkeypatch.setattr(wf, "_call_llm", lambda prompt, model=None, json_mode=None: response)

    kept = wf._filter_by_llm(papers)

    assert [p.segment_id for p in kept] == [1]
    inc = wf.included_decisions[0]
    assert inc.stage == "llm_judge"
    assert inc.decision == "INCLUDE"
    assert inc.bucket == ["B"]
    assert inc.model == "fake-model"
    assert inc.prompt_version and "@" in inc.prompt_version

    rec = wf.excluded[0]
    assert rec["stage"] == "llm_judge"
    assert rec["decision"]["decision"] == "EXCLUDE"
    assert rec["decision"]["exclude_reason"] == "X1"
    assert rec["decision"]["reason"] == "天文学研究，与医疗无关"
    assert rec["decision"]["model"] == "fake-model"
    assert rec["decision"]["prompt_version"] == inc.prompt_version


def test_filter_by_llm_uses_filter_model_override(tmp_path, monkeypatch):
    """LLM__FILTER_MODEL 设置时裁决使用该模型而非主模型（响应用旧顶层数组格式，验证宽容兼容）"""
    settings = _make_settings(tmp_path, filter_mode="llm")
    settings.llm.filter_model = "fake-flash"
    wf = ScholarWorkflow(settings)

    seen_models = []
    seen_json_mode = []

    def fake_call(prompt, model=None, json_mode=None):
        seen_models.append(model)
        seen_json_mode.append(json_mode)
        return '[{"id": 1, "decision": "INCLUDE", "bucket": ["A"], "one_line": "ok", "confidence": 0.7}]'

    monkeypatch.setattr(wf, "_call_llm", fake_call)
    wf._filter_by_llm([_make_paper(1, "EHR study")])

    assert seen_models == ["fake-flash"]
    assert seen_json_mode == [True]
    assert wf.included_decisions[0].model == "fake-flash"


def test_filter_by_llm_missing_id_falls_back(tmp_path, monkeypatch):
    """LLM 响应缺失某 id：该篇回退关键词白名单，stage 标 keyword_fallback"""
    settings = _make_settings(tmp_path, filter_mode="llm", whitelist=["EHR"])
    wf = ScholarWorkflow(settings)
    papers = [
        _make_paper(1, "Graph learning for EHR"),
        _make_paper(2, "EHR phenotyping study"),
    ]
    # 只返回 id=1 的裁决（旧顶层数组格式，验证宽容兼容）
    monkeypatch.setattr(
        wf, "_call_llm",
        lambda prompt, model=None, json_mode=None: '[{"id": 1, "decision": "INCLUDE", "bucket": ["A"], "one_line": "ok", "confidence": 0.7}]'
    )

    kept = wf._filter_by_llm(papers)

    assert {p.segment_id for p in kept} == {1, 2}
    stages = {d.paper_id: d.stage for d in wf.included_decisions}
    assert stages["paper_1"] == "llm_judge"
    assert stages["paper_2"] == "keyword_fallback"
    assert wf._filter_fallback_count == 1


def test_filter_by_llm_batch_failure_falls_back(tmp_path, monkeypatch):
    """LLM 整批失败：回退关键词白名单，不抛异常不中断"""
    settings = _make_settings(tmp_path, filter_mode="llm", whitelist=["EHR"])
    wf = ScholarWorkflow(settings)
    papers = [
        _make_paper(1, "EHR risk prediction"),
        _make_paper(2, "Quasar spectroscopy"),
    ]

    def boom(prompt, model=None, json_mode=None):
        raise RuntimeError("API down")

    monkeypatch.setattr(wf, "_call_llm", boom)
    kept = wf._filter_by_llm(papers)

    assert [p.segment_id for p in kept] == [1]
    assert wf.included_decisions[0].stage == "keyword_fallback"
    assert wf.excluded[0]["stage"] == "keyword_fallback"
    assert wf._filter_fallback_count == 2


def test_filter_by_llm_batch_failure_undecided_not_excluded(tmp_path, monkeypatch):
    """LLM 整批失败：未命中白名单不再静默 EXCLUDE，进 undecided_segments 待人工复核"""
    settings = _make_settings(tmp_path, filter_mode="llm", whitelist=["EHR"])
    wf = ScholarWorkflow(settings)
    wf.segments = [
        _make_paper(1, "EHR risk prediction"),
        _make_paper(2, "Quasar spectroscopy"),
    ]

    def boom(prompt, model=None, json_mode=None):
        raise RuntimeError("API down")

    monkeypatch.setattr(wf, "_call_llm", boom)
    wf._step_filter_papers()
    output = wf._step_generate_output()

    # 命中白名单者照常入选；未命中者进独立待复核队列，结构上不进 segments
    assert [p.paper_id for p in output.segments] == ["paper_1"]
    assert [p.paper_id for p in output.undecided_segments] == ["paper_2"]
    assert output.undecided_segments[0].filter_decision.verdict == "undecided"

    # sidecar 仍固化该论文供审计，但 keyword_fallback 路径不得再产生 excluded 裁决
    sidecar = json.loads(
        (wf.output_dir / "{}_excluded.json".format(wf.run_id)).read_text(encoding="utf-8"))
    rec = next(p for p in sidecar["papers"] if p["paper_id"] == "paper_2")
    assert rec["stage"] == "keyword_fallback"
    assert rec["decision"]["verdict"] == "undecided"
    assert not any(
        p["stage"] == "keyword_fallback" and p["decision"]["verdict"] == "excluded"
        for p in sidecar["papers"]
    )

    # digest markdown 显式露出待复核小节（含标题与原因）
    md = output.to_markdown()
    assert "待人工复核" in md
    assert "Quasar spectroscopy" in md
    assert "LLM 裁决失败且未命中白名单" in md

    # stats 记录 undecided_count
    stats = json.loads(
        (wf.output_dir / "{}_stats.json".format(wf.run_id)).read_text(encoding="utf-8"))
    assert stats["undecided_count"] == 1

    # 固化 JSON 可正常 round-trip（undecided_segments 随 digest 落盘）
    from src.scholar.schema import DigestOutput
    reloaded = DigestOutput.load_from_file(wf.output_dir / "{}.json".format(wf.run_id))
    assert [p.paper_id for p in reloaded.undecided_segments] == ["paper_2"]


# ==================== 响应解析 ====================

def test_parse_filter_response_variants(tmp_path):
    """裁决响应解析：{verdicts:[...]} 新契约 / 裸 JSON 数组 / ```json 围栏 / {papers:[...]} 旧包装"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    item = {"id": 1, "decision": "INCLUDE", "bucket": ["A"], "one_line": "r", "confidence": 0.6}

    verdicts_obj = json.dumps({"verdicts": [item]})
    assert wf._parse_filter_response(verdicts_obj)[1]["decision"] == "INCLUDE"
    # 旧顶层数组格式（宽容回退）
    assert wf._parse_filter_response(json.dumps([item]))[1]["decision"] == "INCLUDE"
    fenced = "```json\n{}\n```".format(json.dumps([item]))
    assert 1 in wf._parse_filter_response(fenced)
    # 旧 {papers: [...]} 包装格式（宽容回退）
    wrapped = json.dumps({"papers": [item]})
    assert 1 in wf._parse_filter_response(wrapped)


def test_parse_filter_response_invalid_raises(tmp_path):
    """非 JSON / 非数组响应必须抛异常（由调用方触发回退）"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    with pytest.raises(Exception):
        wf._parse_filter_response("sorry, I cannot help")
    with pytest.raises(ValueError):
        wf._parse_filter_response('{"not": "a list"}')


def test_parse_filter_response_drops_ids_outside_valid_set(tmp_path):
    """valid_ids 传入时，集合外的 id（示例小节回显 900001 / 幻觉 id）必须被丢弃，
    不进入返回映射；合法 id 正常保留。"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    payload = json.dumps({"verdicts": [
        {"id": 1, "decision": "INCLUDE", "bucket": ["A"], "one_line": "ok", "confidence": 0.7},
        {"id": 900001, "decision": "EXCLUDE", "bucket": [], "one_line": "示例回显", "confidence": 0.5},
    ]})
    result = wf._parse_filter_response(payload, valid_ids={1})
    assert 1 in result
    assert 900001 not in result

    # 不传 valid_ids（None）时不做过滤，兼容未指定批次上下文的调用方
    result_unfiltered = wf._parse_filter_response(payload)
    assert {1, 900001} == set(result_unfiltered.keys())


def test_parse_filter_response_skips_non_numeric_id_without_crashing(tmp_path):
    """单条 id 非数字（模型吐出字符串 "N/A"）只丢弃该条，其余条目正常解析，不拖垮整批"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    payload = json.dumps({"verdicts": [
        {"id": "N/A", "decision": "INCLUDE", "bucket": ["A"], "one_line": "脏 id", "confidence": 0.5},
        {"id": 2, "decision": "INCLUDE", "bucket": ["B"], "one_line": "正常", "confidence": 0.8},
    ]})
    result = wf._parse_filter_response(payload)
    assert 2 in result
    assert result[2]["one_line"] == "正常"
    assert len(result) == 1


def test_parse_llm_response_drops_ids_outside_valid_set(tmp_path):
    """digest 侧 _parse_llm_response 同款防护：valid_ids 传入时，集合外的 id（示例小节
    回显 900001 / 幻觉 id）必须被丢弃，不覆盖批次内真实论文；合法 id 正常写入译文。"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    batch = [_make_paper(1, "Real Paper Title")]
    payload = json.dumps({"papers": [
        {"id": 1, "translated_title": "真实译文", "translated_abstract": "真实摘要"},
        {"id": 900001, "translated_title": "示例回显标题", "translated_abstract": "示例回显摘要"},
    ]})
    wf._parse_llm_response(payload, batch, valid_ids={1})
    assert batch[0].metadata.translated_title == "真实译文"
    assert batch[0].translated_abstract == "真实摘要"

    # 不传 valid_ids（None）时不做过滤，兼容未指定批次上下文的调用方（如既有单测）
    batch2 = [_make_paper(1, "Real Paper Title")]
    wf._parse_llm_response(payload, batch2)
    assert batch2[0].metadata.translated_title == "真实译文"


def test_parse_llm_response_skips_non_numeric_id_without_crashing(tmp_path):
    """单条 id 非数字（模型吐出字符串 "N/A"）只丢弃该条，同批其余合法 id 正常解析，
    不拖垮整批（与 filter 侧 _parse_filter_response 同款容错）"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    batch = [_make_paper(2, "Paper Two")]
    payload = json.dumps({"papers": [
        {"id": "N/A", "translated_title": "脏 id 标题", "translated_abstract": "脏 id 摘要"},
        {"id": "2", "translated_title": "正常译文", "translated_abstract": "正常摘要"},
    ]})
    wf._parse_llm_response(payload, batch)
    assert batch[0].metadata.translated_title == "正常译文"
    assert batch[0].status == DigestStatus.COMPLETED


# ==================== sidecar 固化 ====================

def test_excluded_sidecar_written_with_audit_fields(tmp_path, monkeypatch):
    """excluded sidecar：头部统计 + 每条裁决的 verdict/reason/model/prompt_version"""
    settings = _make_settings(tmp_path, filter_mode="llm")
    wf = ScholarWorkflow(settings)
    papers = [
        _make_paper(1, "Graph learning for EHR"),
        _make_paper(2, "Quasar spectroscopy"),
    ]
    monkeypatch.setattr(
        wf, "_call_llm",
        lambda prompt, model=None, json_mode=None: json.dumps({"verdicts": [
            {"id": 1, "decision": "INCLUDE", "bucket": ["B"], "one_line": "相关", "confidence": 0.8},
            {"id": 2, "decision": "EXCLUDE", "exclude_reason": "X1", "one_line": "无关", "confidence": 0.9},
        ]})
    )
    wf.segments = papers
    wf._step_filter_papers()

    path = wf._write_excluded_sidecar()
    assert path is not None and path.exists()

    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert sidecar["run_id"] == wf.run_id
    assert sidecar["filter_mode"] == "llm"
    assert sidecar["model"] == "fake-model"
    assert sidecar["prompt_version"]
    assert sidecar["llm_excluded_count"] == 1
    assert sidecar["unique_excluded"] == 1

    paper = sidecar["papers"][0]
    for field in ("paper_id", "reason", "stage", "decision", "metadata", "original_abstract"):
        assert field in paper
    for field in ("verdict", "reason", "model", "prompt_version", "decided_at"):
        assert paper["decision"][field]

    included = sidecar["included_decisions"]
    assert len(included) == 1 and included[0]["paper_id"] == "paper_1"


def test_sidecar_skipped_when_nothing_filtered(tmp_path):
    """无任何裁决记录时不写空 sidecar"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    assert wf._write_excluded_sidecar() is None


# ==================== 黑名单预过滤（确定性） ====================

def test_blacklist_prefilter_records_exclusion(tmp_path, monkeypatch):
    """黑名单命中在解析阶段确定性剔除，且进入 excluded 记录"""
    settings = _make_settings(tmp_path, blacklist=["Gene"], filter_mode="keyword", whitelist=[])
    wf = ScholarWorkflow(settings)

    from src.scholar.schema import EmailMetadata
    from datetime import datetime
    meta = EmailMetadata(
        email_id="email_1", subject="Scholar Alert", sender="scholar",
        received_at=datetime(2026, 7, 1),
    )
    papers = [
        _make_paper(1, "Gene expression profiling"),
        _make_paper(2, "EHR data mining"),
    ]
    wf.emails = [{"metadata": meta, "body": "fake"}]
    monkeypatch.setattr(wf.parser, "parse_email", lambda body, m: papers)

    wf._step_parse_emails()

    assert [p.segment_id for p in wf.segments] == [2]
    assert len(wf.excluded) == 1
    rec = wf.excluded[0]
    assert rec["stage"] == "blacklist_keyword"
    assert rec["decision"]["model"] is None
    assert "Gene" in rec["reason"]


# ==================== scholar_main 不再硬编码词表 ====================

def test_scholar_main_no_hardcoded_wordlists():
    """env 配置的黑白名单不得再被 scholar_main/cli 硬编码覆盖

    scholar_main.py 下沉为薄壳后，真正的配置构建逻辑在 src/scholar/cli.py，
    两份都要检查，避免闸门被下沉打断。
    """
    source = Path("scholar_main.py").read_text(encoding="utf-8")
    source += Path("src/scholar/cli.py").read_text(encoding="utf-8")
    assert "settings.processing.whitelist = [" not in source
    assert "settings.processing.blacklist = [" not in source


def test_schema_defaults_carry_full_wordlists():
    """扩充后的词表迁入 schema 默认值；黑名单保持保守（不含影像/基因等 X1 词，交给 LLM）"""
    from src.scholar.schema import ProcessingSettings
    p = ProcessingSettings()
    assert len(p.whitelist) == 52
    assert len(p.blacklist) == 21
    assert p.filter_mode == "llm"
    # 黑名单不含会误杀对抗性证据的 X1 词（影像/基因/信号），这些交给 LLM 依 X1..X7 判定
    joined = " ".join(p.blacklist).lower()
    for forbidden in ("vision", "gene", "signal", "image"):
        assert forbidden not in joined


def test_filter_by_llm_survives_dirty_fields(tmp_path, monkeypatch):
    """flash 模型返回脏字段（confidence='high'、bucket 非 list、role 为 list）不得让整批崩溃"""
    settings = _make_settings(tmp_path, filter_mode="llm", whitelist=["EHR"])
    wf = ScholarWorkflow(settings)
    papers = [
        _make_paper(1, "EHR graph model"),
        _make_paper(2, "EHR missingness study"),
        _make_paper(3, "EHR transportability"),
    ]
    monkeypatch.setattr(
        wf, "_call_llm",
        lambda prompt, model=None, json_mode=None: json.dumps([
            {"id": 1, "decision": "INCLUDE", "bucket": "B", "flags": None,
             "role": ["MUST_ENGAGE"], "one_line": "ok", "confidence": "high"},
            {"id": 2, "decision": "MAYBE", "bucket": ["C"], "one_line": "m", "confidence": "0.9"},
            {"id": 3, "decision": "EXCLUDE", "exclude_reason": ["X1"], "one_line": "no", "confidence": 5},
        ])
    )
    # 不得抛异常
    kept = wf._filter_by_llm(papers)
    kept_ids = {p.segment_id for p in kept}
    # 1(INCLUDE) 2(MAYBE) 入选，3(EXCLUDE) 排除
    assert 1 in kept_ids and 2 in kept_ids
    assert 3 not in kept_ids
    # 脏字段被容错：confidence 无法解析→None，越界 5→clamp 到 1.0，bucket 标量→单元素 list
    d1 = next(p.filter_decision for p in kept if p.segment_id == 1)
    assert d1.confidence is None
    assert d1.bucket == ["B"]
    d2 = next(p.filter_decision for p in kept if p.segment_id == 2)
    assert d2.confidence == 0.9


def test_filter_by_llm_maybe_is_included(tmp_path, monkeypatch):
    """MAYBE 三态：当作入选一并进入后续处理，且裁决随 segment 保留"""
    settings = _make_settings(tmp_path, filter_mode="llm")
    wf = ScholarWorkflow(settings)
    papers = [_make_paper(1, "Missingness-aware model, ambiguous scope")]
    monkeypatch.setattr(
        wf, "_call_llm",
        lambda prompt, model=None, json_mode=None: json.dumps([
            {"id": 1, "decision": "MAYBE", "bucket": ["C"], "flags": ["THREAT"],
             "role": "MUST_ENGAGE", "one_line": "可能反例", "confidence": 0.3},
        ])
    )
    kept = wf._filter_by_llm(papers)
    assert [p.segment_id for p in kept] == [1]
    fd = kept[0].filter_decision
    assert fd.decision == "MAYBE"
    assert "THREAT" in fd.flags
    assert fd.role == "MUST_ENGAGE"


# ---------------- 失败批次末尾重试（2026-08-22，复盘 08-17 生产运行） ----------------
# 背景：08-17 那次 12 批里 2 个整批（40 篇）裁决失败直接掉进关键词兜底，其中 21 篇
# 未命中白名单被挂 undecided——真正的三态审稿对它们根本没发生过。根因是回退链里
# deepseek 欠费、gemini key 失效，只剩两个共用同一订阅额度的 claude-agent 档位，
# 订阅短时触顶时整条链一次性塌掉。这类故障会自愈，所以失败批不该当场判死。

def test_filter_batch_transient_failure_recovered_by_retry(tmp_path, monkeypatch):
    """首轮整批失败、末尾重试成功：拿到真裁决，不落关键词兜底。"""
    settings = _make_settings(tmp_path, filter_mode="llm", whitelist=["EHR"])
    wf = ScholarWorkflow(settings)
    papers = [_make_paper(1, "EHR risk prediction"), _make_paper(2, "Quasar spectroscopy")]

    calls = {"n": 0}

    def flaky(prompt, model=None, json_mode=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 订阅额度触顶")   # 瞬态
        return ('{"verdicts": ['
                '{"id": 1, "decision": "INCLUDE", "bucket": ["A"], "one_line": "ok", "confidence": 0.8},'
                '{"id": 2, "decision": "EXCLUDE", "bucket": [], "one_line": "无关", "confidence": 0.9}]}')

    monkeypatch.setattr(wf, "_call_llm", flaky)
    kept = wf._filter_by_llm(papers)

    assert calls["n"] == 2                       # 首轮 + 重试各一次
    assert [p.segment_id for p in kept] == [1]
    assert wf.included_decisions[0].stage == "llm_judge"   # 真裁决，不是兜底
    assert wf._filter_fallback_count == 0                  # 一篇都没掉进关键词
    assert wf._filter_retry_recovered == 2                 # 两篇都是重试救回来的
    assert wf.excluded[0]["stage"] == "llm_judge"


def test_filter_batch_retry_rewinds_provider_chain(tmp_path, monkeypatch):
    """重试前必须把粘性链位拨回链首，否则重试还在链尾那个已耗尽的位置。"""
    settings = _make_settings(tmp_path, filter_mode="llm", whitelist=["EHR"])
    wf = ScholarWorkflow(settings)
    rewound = []

    def _rewind():
        rewound.append(True)
        return True

    monkeypatch.setattr(wf.llm_client, "rewind_chain", _rewind)

    def boom(prompt, model=None, json_mode=None):
        raise RuntimeError("API down")

    monkeypatch.setattr(wf, "_call_llm", boom)
    wf._filter_by_llm([_make_paper(1, "EHR risk prediction")])
    assert rewound == [True]


def test_filter_batch_persistent_failure_records_cause(tmp_path, monkeypatch):
    """两轮都失败才落兜底，且两次原因都写进裁决 reason（08-17 事后无从定位的教训）。"""
    settings = _make_settings(tmp_path, filter_mode="llm", whitelist=["EHR"])
    wf = ScholarWorkflow(settings)
    papers = [_make_paper(1, "Quasar spectroscopy")]   # 不命中白名单 → undecided

    seq = ["首轮限流 429", "重试仍然 429"]

    def boom(prompt, model=None, json_mode=None):
        raise RuntimeError(seq.pop(0) if seq else "again")

    monkeypatch.setattr(wf, "_call_llm", boom)
    wf._filter_by_llm(papers)

    assert wf._filter_fallback_count == 1
    assert wf._filter_retry_recovered == 0
    assert len(wf.undecided_segments) == 1
    reason = wf.undecided_segments[0].filter_decision.reason
    assert "首轮限流 429" in reason and "重试仍然 429" in reason
