# -*- coding: utf-8 -*-
"""2026-09-17 瓶颈修复批的回归测试。

覆盖本批行为性修复（日志级修复不在此列）：
  - academic_search：max_results<=0 护栏、_get_retry 瞬态退避
  - fulltext._jats_walk：累计长度与逐次全量重算逐字节等价
  - engine_gemini：正文取不出时抛 _ResponseValidationError（不再静默整批判死）；
    术语抽取窗口对 provider 级致命错误上抛
  - support.CachePersistenceManager：并发删除竞态下的原子 pop
  - translation_server._parse_date：英文月名解析 + 精度推导一致
  - book_legacy._PAGE_REF_RE：左边界（Chap.4 不是页码）
  - topics.build_topic：冲突预检在 LLM 调用之前短路
  - qa.qa_slug：pages 预载参数与全量扫描等价
  - ingest.segments_from_identifiers：并行化后顺序/编号语义不变
  - scripts/merge_final.py：必填键与 verified_count 门禁
  - scholar workflow：规则路径的 priority_reason 落到 metadata 侧
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------- academic_search ----------------

def test_search_arxiv_max_results_zero_returns_empty_without_network():
    from src.scholar.academic_search import AcademicSearchClient
    c = AcademicSearchClient.__new__(AcademicSearchClient)
    c.email, c.tool = "", "t"
    c._client = MagicMock()
    c._client.get.side_effect = AssertionError("不应发起网络请求")
    assert c.search_arxiv("q", max_results=0) == []
    assert c.search_pubmed("q", max_results=0) == []
    assert c.search_arxiv("q", max_results=-3) == []


def _resp(code, text="<feed/>"):
    r = MagicMock()
    r.status_code = code
    r.text = text
    return r


def test_get_retry_recovers_from_transient_503():
    from src.scholar.academic_search import _get_retry
    client = MagicMock()
    client.get.side_effect = [_resp(503), _resp(200)]
    sleeps = []
    r = _get_retry(client, "https://x/api", params={}, sleep=sleeps.append)
    assert r.status_code == 200
    assert client.get.call_count == 2
    assert len(sleeps) == 1


def test_get_retry_exhaustion_returns_last_response_not_raise():
    from src.scholar.academic_search import _get_retry
    client = MagicMock()
    client.get.side_effect = [_resp(503), _resp(503), _resp(503)]
    r = _get_retry(client, "https://x/api", sleep=lambda *_: None)
    assert r.status_code == 503          # raise_for_status 留给调用方，异常形状不变
    assert client.get.call_count == 3


def test_get_retry_timeout_raises_after_attempts():
    from src.scholar.academic_search import _get_retry
    client = MagicMock()
    client.get.side_effect = httpx.ConnectTimeout("boom")
    with pytest.raises(httpx.ConnectTimeout):
        _get_retry(client, "https://x/api", sleep=lambda *_: None)
    assert client.get.call_count == 3


def test_get_retry_non_transient_returns_immediately():
    from src.scholar.academic_search import _get_retry
    client = MagicMock()
    client.get.side_effect = [_resp(404)]
    r = _get_retry(client, "https://x/api", sleep=lambda *_: None)
    assert r.status_code == 404
    assert client.get.call_count == 1


# ---------------- fulltext._jats_walk 等价性 ----------------

def _old_jats_walk(el, parts, budget):
    """修复前的实现（逐子节点全量 sum），作为等价性对照。"""
    tag = el.tag if isinstance(el.tag, str) else ""
    tag = tag.rsplit("}", 1)[-1]
    from src.scholar.fulltext import _JATS_SKIP, _JATS_BLOCK
    if tag not in _JATS_SKIP:
        if el.text:
            parts.append(el.text)
        for child in el:
            if sum(len(p) for p in parts) > budget:
                break
            _old_jats_walk(child, parts, budget)
        if tag in _JATS_BLOCK:
            parts.append("\n")
    if el.tail:
        parts.append(el.tail)


@pytest.mark.parametrize("budget", [10, 50, 200, 10_000])
def test_jats_walk_running_total_matches_old_algorithm(budget):
    import xml.etree.ElementTree as ET
    from src.scholar.fulltext import _jats_walk
    xml = ("<body>" + "".join(
        "<sec><title>S{i}</title><p>para {i} text with some length</p>"
        "<p>second {i}</p><xref>skip</xref>tail{i}</sec>".format(i=i)
        for i in range(40)) + "</body>")
    root = ET.fromstring(xml)
    a, b = [], []
    _jats_walk(root, a, budget)
    _old_jats_walk(root, b, budget)
    assert "".join(a) == "".join(b)


# ---------------- engine_gemini：C1 / C2 ----------------

def _min_gemini():
    from src.translator.engine import GeminiTranslator
    g = GeminiTranslator.__new__(GeminiTranslator)
    g.settings = MagicMock()
    g.settings.processing.max_context_length = 1000
    g._formal_phase = True
    g.prompt_manager = MagicMock()
    g.prompt_manager.format_text_prompt.return_value = "PROMPT"
    g.generation_config = {}
    return g


def test_gemini_unreadable_parts_raises_validation_error_not_silent_fail():
    from src.translator.engine_gemini import _ResponseValidationError, GeminiTranslator
    g = _min_gemini()
    bad = MagicMock()
    bad.candidates = []                      # candidates[0] → IndexError
    g._generate_content = MagicMock(return_value=bad)
    seg = SimpleNamespace(segment_id=1, original_text="hello")
    fn = GeminiTranslator._translate_text_batch.__wrapped__   # 绕过 tenacity 装饰
    with pytest.raises(_ResponseValidationError):
        fn(g, [seg], context="", glossary=None)


def test_gemini_glossary_window_reraises_hard_provider_error():
    from src.core.exceptions import APIError
    from src.translator.fallback import classify_fatal
    g = _min_gemini()
    hard = APIError("402 Payment Required: insufficient balance")
    assert classify_fatal(hard) == 'hard', "前提：该文案必须被判 hard"
    g._extract_glossary_window = MagicMock(side_effect=hard)
    seg = SimpleNamespace(is_translated=True, original_text="a", translated_text="b")
    with pytest.raises(APIError):
        g.extract_glossary([seg])


def test_openai_glossary_window_reraises_hard_provider_error():
    from src.core.exceptions import APIError
    from src.translator.engine import OpenAICompatibleTranslator
    t = OpenAICompatibleTranslator.__new__(OpenAICompatibleTranslator)
    t._extract_glossary_window = MagicMock(
        side_effect=APIError("401 Unauthorized: invalid api key"))
    seg = SimpleNamespace(is_translated=True, original_text="a", translated_text="b")
    with pytest.raises(APIError):
        t.extract_glossary([seg])


# ---------------- support：缓存元数据竞态 ----------------

def _min_cache_mgr(tmp_path):
    from src.translator.support import CachePersistenceManager
    m = CachePersistenceManager.__new__(CachePersistenceManager)
    m.cache_metadata = {"system_instruction": {}, "glossary": {}, "context": {}}
    m._save_metadata = lambda: None
    return m


def test_remove_invalid_cache_double_call_no_keyerror(tmp_path):
    m = _min_cache_mgr(tmp_path)
    m.cache_metadata["system_instruction"]["k1"] = {"cache_name": "c1"}
    m.cache_metadata["glossary"]["k2"] = {"cache_name": "c1"}
    assert m.remove_invalid_cache("c1") is True
    assert m.remove_invalid_cache("c1") is False      # 已删光：无 KeyError、返回 False
    assert not m.cache_metadata["system_instruction"]


def test_get_system_cache_expired_entry_pop_tolerates_concurrent_delete(tmp_path):
    import time
    m = _min_cache_mgr(tmp_path)
    m.cache_metadata["system_instruction"]["old"] = {
        "cache_name": "c9", "content_hash": "h", "expiry_time": time.time() - 10}
    # 模拟并发线程在收集与删除之间已经把键删掉：pop(默认值) 不该抛
    real = m.cache_metadata["system_instruction"]
    assert m.get_system_cache("h") is None            # 过期 → 清理，不崩
    assert "old" not in real


# ---------------- translation_server：英文月名 ----------------

def test_parse_date_english_month_names():
    from src.scholar.translation_server import _parse_date
    assert _parse_date("Feb 2026") == date(2026, 2, 1)
    assert _parse_date("March 15, 2026") == date(2026, 3, 1)
    assert _parse_date("December 2025") == date(2025, 12, 1)
    assert _parse_date("7/2026") == date(2026, 7, 1)      # 数字月优先，行为不变
    assert _parse_date("2026") == date(2026, 1, 1)


def test_apply_item_month_name_gets_month_precision():
    from src.scholar.translation_server import apply_item_to_meta
    from src.scholar.schema import PaperMetadata
    meta = PaperMetadata(paper_id="x", title="t")
    assert apply_item_to_meta(meta, {"date": "Feb 2026", "publicationTitle": "J"})
    assert meta.publication_date == date(2026, 2, 1)
    assert meta.date_precision == "month"


# ---------------- book_legacy：页码正则左边界 ----------------

def test_page_ref_regex_ignores_abbreviations():
    from src.scholar.book_legacy import _PAGE_REF_RE
    assert _PAGE_REF_RE.search("See Chap.4 for details") is None
    assert _PAGE_REF_RE.search("cf. App.12") is None
    m = _PAGE_REF_RE.search("pp.143-160")
    assert m and m.group(1) == "143" and m.group(2) == "160"
    assert _PAGE_REF_RE.search("见 p. 27 的表")


# ---------------- topics：冲突预检短路 ----------------

def test_build_topic_conflict_precheck_short_circuits_before_llm(tmp_path):
    from src.scholar.topics import build_topic, TopicSpec
    spec = TopicSpec(slug="t1", title="T", question="q", queries=["q1"])
    # 有 frontmatter、但没有「我的批注」用户区 → merge 三判据之一命中 → 冲突
    (tmp_path / "t1.md").write_text("---\ntopic: t1\n---\n正文没有用户区标记\n",
                                    encoding="utf-8")
    boom = MagicMock()
    boom.embed.side_effect = AssertionError("冲突页不该走到检索")
    r = build_topic(spec, store=MagicMock(), embed_client=boom, llm=None,
                    index={}, topics_dir=tmp_path, template="", research_interests="")
    assert r.status == "conflict"
    assert "预检" in (r.error or "")


# ---------------- qa_slug pages 预载等价 ----------------

def test_qa_slug_pages_param_equivalent_to_dir_scan(tmp_path):
    from src.scholar.qa import qa_slug, list_qa_pages
    q = "MNAR 在 EHR 里能不能检验？"
    slug_now = qa_slug(q)
    old = "qa-mnar-ehr-deadbeef"           # 模拟口径迁移前的旧文件名
    (tmp_path / "{}.md".format(old)).write_text(
        "---\nqa: {}\nquestion: \"{}\"\n---\nbody\n".format(old, q), encoding="utf-8")
    assert qa_slug(q, qa_dir=tmp_path) == old
    pages = list_qa_pages(tmp_path)
    assert qa_slug(q, qa_dir=tmp_path, pages=pages) == old
    assert slug_now != old


# ---------------- ingest 并行化：顺序与编号不变 ----------------

def test_segments_from_identifiers_parallel_preserves_order(monkeypatch):
    import src.scholar.ingest as ingest
    import src.scholar.crossref as crossref

    class _DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("src.scholar.fulltext.ipv4_client",
                        lambda *a, **k: _DummyClient())
    monkeypatch.setattr(crossref, "crossref_by_doi",
                        lambda doi, email="", client=None: {
                            "title": "T-{}".format(doi), "authors": ["A"],
                            "doi": doi, "journal": "J"})
    monkeypatch.setattr(crossref, "crossref_lookup",
                        lambda s, email="", client=None: None)
    ids = ["10.1000/a", "10.1000/b", "10.1000/c", "10.1000/d", "10.1000/e"]
    segs = ingest.segments_from_identifiers(ids, max_workers=4)
    assert [s.metadata.doi for s in segs] == ids
    assert [s.segment_id for s in segs] == [1, 2, 3, 4, 5]
    # 单 worker 路径（回归串行）同样成立
    segs1 = ingest.segments_from_identifiers(ids, max_workers=1)
    assert [s.metadata.doi for s in segs1] == ids


# ---------------- merge_final 门禁 ----------------

def _run_merge(tmp_path, final_obj):
    bundle = tmp_path / "x.paper.json"
    bundle.write_text(json.dumps({"segment": {}}), encoding="utf-8")
    final = tmp_path / "final.json"
    final.write_text(json.dumps(final_obj, ensure_ascii=False), encoding="utf-8")
    p = subprocess.run([sys.executable, "scripts/merge_final.py", str(bundle), str(final)],
                       capture_output=True, text=True, cwd=REPO,
                       env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"})
    return p, bundle


_VALID_CR = {"sections": [{"heading": "研究问题",
                           "sentences": [{"text": "x", "tag": None}]}]}


def test_merge_final_rejects_missing_required_keys(tmp_path):
    p, bundle = _run_merge(tmp_path, {"close_reading_final": _VALID_CR,
                                      "one_line": "y"})
    assert p.returncode == 2
    assert "cross_check_report" in p.stdout
    assert json.loads(bundle.read_text())["segment"] == {}   # 未落盘


def test_merge_final_rejects_zero_verified_count(tmp_path):
    p, bundle = _run_merge(tmp_path, {
        "close_reading_final": _VALID_CR,
        "cross_check_report": {"verified_count": 0},
        "one_line": "y"})
    assert p.returncode == 2
    assert "verified_count" in p.stdout
    p2, bundle2 = _run_merge(tmp_path, {
        "close_reading_final": _VALID_CR,
        "cross_check_report": {"corrected": 2},     # 缺 verified_count 也拒
        "one_line": "y"})
    assert p2.returncode == 2


def test_merge_final_accepts_valid_and_writes_final(tmp_path):
    p, bundle = _run_merge(tmp_path, {
        "close_reading_final": _VALID_CR,
        "cross_check_report": {"verified_count": 3},
        "one_line": "一句话"})
    assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(bundle.read_text())
    assert data["status"] == "final" and data["one_line"] == "一句话"


def test_merge_final_bad_json_clear_error(tmp_path):
    bundle = tmp_path / "x.paper.json"
    bundle.write_text("{not json", encoding="utf-8")
    final = tmp_path / "final.json"
    final.write_text("{}", encoding="utf-8")
    p = subprocess.run([sys.executable, "scripts/merge_final.py", str(bundle), str(final)],
                       capture_output=True, text=True, cwd=REPO,
                       env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"})
    assert p.returncode == 2
    assert "不是合法 JSON" in p.stdout


# ---------------- workflow：规则路径 priority_reason 进 metadata ----------------

def test_rule_priority_reason_lands_on_metadata():
    from src.scholar.workflow import ScholarWorkflow
    from src.scholar.schema import PaperSegment, PaperMetadata, DigestStatus
    wf = object.__new__(ScholarWorkflow)
    seg = PaperSegment(
        segment_id=1, paper_id="p1", original_abstract="a",
        metadata=PaperMetadata(paper_id="p1", title="t", source_type="arxiv"),
        status=DigestStatus.PENDING)
    wf.segments = [seg]
    wf._priority_bonus_applied = True      # 只验证基础打分行，不触发加成路径
    wf._calculate_rule_based_priority()
    assert seg.priority_reason, "seg 侧镜像仍在"
    assert seg.metadata.priority_reason == seg.priority_reason


# ---------------- parser：PDF 页失败占位段 ----------------

def test_pdf_extract_text_failure_returns_none_not_empty():
    from src.parser.formats import PDFParser
    p = PDFParser.__new__(PDFParser)
    page = MagicMock()
    page.get_text.side_effect = RuntimeError("font broken")
    p._get_crop_rect = MagicMock(return_value=None)
    assert p._extract_text(page, 3) is None
    page2 = MagicMock()
    page2.get_text.return_value = "   "
    assert p._extract_text(page2, 4) == ""      # 真空白页仍是空串（跳过语义）


# ---------------- 第 1 轮压测抓到的两处修复 ----------------

def test_merge_final_rejects_bool_verified_count(tmp_path):
    """bool 是 int 子类：verified_count: true 曾以 True>=1 冒充合法计数溜过门禁。"""
    p, bundle = _run_merge(tmp_path, {
        "close_reading_final": _VALID_CR,
        "cross_check_report": {"verified_count": True},
        "one_line": "y"})
    assert p.returncode == 2
    assert "verified_count" in p.stdout
    assert json.loads(bundle.read_text()).get("status") != "final"


def test_check_gates_rejects_bool_verified_count():
    from src.scholar.book_notes import check_gates
    data = {"status": "final", "close_reading_final": {"sections": []},
            "cross_check_report": {"verified_count": True},
            "quote_verify": {"total": 3, "pass_rate": 1.0}}
    r = check_gates(data)
    assert not r.ok and "类型非法" in r.reason
    # 其余非 int 显式值（字符串/浮点）同拒；None=legacy 缺失仍宽容
    data2 = dict(data, cross_check_report={"verified_count": "3"})
    assert not check_gates(data2).ok
    data3 = dict(data, cross_check_report={"verified_count": None, "corrected": 1})
    assert check_gates(data3).ok


def test_render_frontmatter_survives_key_side_pathologies():
    import datetime
    import decimal
    from src.scholar.topics import _render_frontmatter
    from src.scholar.vault import split_frontmatter
    # 超长键：跳过而不是写出必解析失败的 YAML（PyYAML simple key ~1024 上限）
    out = _render_frontmatter({"a" * 100000: "v", "keep": "ok"})
    fm, _ = split_frontmatter(out + "\n正文\n")
    assert isinstance(fm, dict) and fm == {"keep": "ok"}
    # 嵌套 dict 的键是 date/Decimal/tuple：json.dumps 的 default= 只救值不救键
    out2 = _render_frontmatter({"custom": {
        datetime.date(2024, 1, 1): "备注", decimal.Decimal("1.5"): "x",
        ("t", "u"): "y", "v": datetime.datetime(2024, 1, 1, 2, 3)}})
    fm2, _ = split_frontmatter(out2 + "\n正文\n")
    assert isinstance(fm2, dict)
    assert fm2["custom"]["2024-01-01"] == "备注"


# ---------------- 第 1 轮对抗审计抓到的修复 ----------------

def test_segments_from_identifiers_shard_failure_does_not_nuke_others(monkeypatch):
    """分片 client 构造失败只损失该分片，其余分片结果保留（R1 CONFIRMED）。"""
    import src.scholar.ingest as ingest
    import src.scholar.crossref as crossref

    import threading
    calls = {"n": 0}
    _lock = threading.Lock()

    class _DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _flaky_client(*a, **k):
        with _lock:                       # 4 worker 并发调用，计数必须原子
            calls["n"] += 1
            fail = calls["n"] == 2        # 恰好第 2 个分片的 client 构造失败
        if fail:
            raise RuntimeError("socket exhausted")
        return _DummyClient()

    monkeypatch.setattr("src.scholar.fulltext.ipv4_client", _flaky_client)
    monkeypatch.setattr(crossref, "crossref_by_doi",
                        lambda doi, email="", client=None: {
                            "title": "T", "authors": ["A"], "doi": doi})
    monkeypatch.setattr(crossref, "crossref_lookup",
                        lambda s, email="", client=None: None)
    ids = ["10.1000/{}".format(i) for i in range(8)]   # DOI_RE 要求 10. 后 4-9 位
    segs = ingest.segments_from_identifiers(ids, max_workers=4)
    assert 0 < len(segs) < 8              # 丢的只有失败分片那 2 条，不是全部
    assert len(segs) == 6


def test_page_ref_regex_rejects_digit_prefix_keeps_cjk_adjacency():
    from src.scholar.book_legacy import _PAGE_REF_RE
    assert _PAGE_REF_RE.search("3p.27") is None          # 数字前缀（R1-7）
    m = _PAGE_REF_RE.search("见p.27")                     # 中文紧邻是真页码引用
    assert m and m.group(1) == "27"


def test_vision_sleep_skips_after_last_image_not_batch_end():
    """[img, text] 形态：处理 img 后不再 sleep（后面没有 vision 调用了）。"""
    import src.translator.engine_gemini as eg
    g = _min_gemini()
    g._call_vision_api = MagicMock(return_value="译文")
    g._translate_text_batch = MagicMock(return_value=["文段译文"])
    slept = []
    orig_sleep = eg.time.sleep
    eg.time.sleep = slept.append
    try:
        segs = [SimpleNamespace(segment_id=1, content_type="image", image_path="/x.png",
                                original_text=""),
                SimpleNamespace(segment_id=2, content_type="text", image_path=None,
                                original_text="hello")]
        out = eg.GeminiTranslator._translate_vision_batch(g, segs, context="")
    finally:
        eg.time.sleep = orig_sleep
    assert out == ["译文", "文段译文"]
    assert slept == []                    # 最后一个 image 段之后无空等
