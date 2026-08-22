# -*- coding: utf-8 -*-
"""分块深读（PROCESSING__CLOSEREAD_DEEP）的降级路径回归——灰度上线的准入门禁。

深读把一篇论文的 LLM 调用从 1 次放大到 N+1 次，失败面随之变宽；而无人值守链路
（周一 09:30 的 ingest_notes、每月 1 日 21:30 的 backfill）唯一的自动闸门就是
ingest.py 的「精读 0/N 视为故障批、不写终稿」。这四条用例钉住的正是那个闸门的边界：
部分块失败必须照常出稿（a），致命错误必须收敛成一次单跳回落而不是 N 次空转（b/b2），
只有整篇真的读不出来时才允许把整批拦下（c）。

全程假 llm + 内存字符串：不发网络、不读本地缓存 PDF、不写 output/。
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import closereading as cr                       # noqa: E402
from src.scholar import ingest as ING                            # noqa: E402
from src.scholar.pdf_ingest import chunk_text, deep_read_chunks  # noqa: E402
from src.scholar.schema import PaperMetadata, PaperSegment       # noqa: E402


# ---------------- 夹具：假 llm ----------------

_CHUNK_RESP = ('{"method_details":["用可学习掩码嵌入"],"key_numbers":["AUC=0.91, n=1200"],'
               '"claims":["缺失本身有信息"],"limitations":["单中心"]}')
_SYNTH_RESP = ('{"one_line":"深读汇总",'
               '"sections":[{"heading":"研究问题","sentences":[{"text":"问题","tag":null}]},'
               '{"heading":"结果与效应量","sentences":[{"text":"AUC=0.91","tag":"可引用证据"}]},'
               '{"heading":"逐节通读要点","sentences":[{"text":"节要","tag":null}]}]}')
_SINGLE_RESP = ('{"sections":[{"heading":"关键结论","sentences":'
                '[{"text":"单跳结论","tag":"可引用证据"}]}]}')

# 单跳 prompt 的特征串直接取自被测常量的首段字面量（第一个 {} 占位之前），
# 避免手抄一份字符串后 prompt 一改就静默失配、把「回落了单跳」测成假绿。
_SINGLE_PROMPT_HEAD = cr._CLOSEREAD_PROMPT.split("{")[0]


class _DeepLLM:
    """按 prompt 类型分发的假 llm，可指定哪些块抛错、抛哪类错。

    fail_chunks="all" = 每一块都抛；也可传块号集合（块号从 1 起，与 _CHUNK_PROMPT 的
    「第 i/N 块」一致）。fail_kind 决定错误文本，进而决定 is_credit_error 的判定
    ——402/401 是致命类（触发 fatal_evt 短路），其余走「本块失败、整篇继续」。
    """

    def __init__(self, fail_chunks=(), fail_kind="boom", fail_single=False):
        self.fail_chunks = fail_chunks
        self.fail_kind = fail_kind
        self.fail_single = fail_single
        self.calls = []
        self.chunk_calls = []

    def call(self, prompt, model=None, max_tokens=None, temperature=None, json_mode=False):
        self.calls.append(prompt)
        if prompt.startswith("你在逐块通读"):
            idx = int(re.search(r"第 (\d+)/", prompt).group(1))
            self.chunk_calls.append(idx)
            if self.fail_chunks == "all" or idx in self.fail_chunks:
                raise RuntimeError(self.fail_kind)
            return _CHUNK_RESP
        if prompt.startswith("你在把一篇论文"):
            return _SYNTH_RESP
        if self.fail_single:
            raise RuntimeError("单跳也挂了")
        return _SINGLE_RESP

    def close(self):
        pass  # mock：LLMClient.close() 兼容

    @property
    def n_synth_calls(self):
        return sum(1 for p in self.calls if p.startswith("你在把一篇论文"))


def _seg(paper_id="p1", doi="10.1/oa"):
    meta = PaperMetadata(paper_id=paper_id, title="MNAR in EHR", doi=doi)
    seg = PaperSegment(segment_id=1, paper_id=paper_id, metadata=meta,
                       original_abstract="abs", priority_score=1.0)
    return seg


def _pdf_route(monkeypatch, tmp_path, body):
    """把 close_read_segment 的 PDF 分支接到内存正文上（不下载、不解析真 PDF）。"""
    class _OA:
        pdf_url = "http://x/y.pdf"
        source = "unpaywall"

    monkeypatch.setattr(cr, "resolve_oa_pdf",
                        lambda meta, email="", client=None: _OA())
    monkeypatch.setattr(cr, "download_pdf", lambda url, dest, **kw: tmp_path / "y.pdf")
    monkeypatch.setattr(cr, "_pdf_text_with_stats",
                        lambda path, max_chars=40000, page_max_chars=None: (body, len(body)))


# ---------------- 夹具：把 run_ingest 压到只剩闸门逻辑 ----------------

def _settings(tmp_path):
    from src.scholar.schema import ScholarSettings
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n",
        encoding="utf-8",
    )
    s = ScholarSettings.from_env_file(env_file)
    s.processing.notes_dir = tmp_path / "notes"
    s.processing.output_dir = tmp_path / "out"
    s.processing.closeread_deep = True      # 本文件测的就是开关打开后的那条链路
    return s


def _mock_ingest_io(monkeypatch, llm):
    """只挡掉 run_ingest 的网络/落盘副作用，**保留真的 close_read_segments**——
    闸门测的是「真精读结果 → 是否 raise」，把精读也 mock 掉就只剩同义反复。
    返回记录 write_notes 调用的列表。"""
    monkeypatch.setattr(ING, "enrich_segments", lambda segs, email, ts: (0, 0, 0))
    monkeypatch.setattr(ING, "resolve_citekeys",
                        lambda segs, base: {s.paper_id: None for s in segs})
    import src.scholar.llm_client as llm_client
    monkeypatch.setattr(llm_client, "LLMClient", lambda cfg: llm)
    import src.scholar.notes as notes
    written = []

    def fake_write(*a, **k):
        written.append(1)
        return {"note_path": "fake.md", "docx_path": None}

    monkeypatch.setattr(notes, "write_notes", fake_write)
    return written


# ---------------- (a) 部分块失败：靠可用块出稿，闸门不响 ----------------

def test_partial_chunk_failure_still_synthesizes_and_passes_ingest_gate(monkeypatch, tmp_path):
    """3 块里挂 1 块：synthesize 用剩下 2 块照样出 CloseReading，done≥1，
    ingest 的 0/N RuntimeError 不得被触发、终稿照写。

    这是最常见的一种深读失败（单块超时/单块 JSON 崩），若它能把整批拦下，
    周度入库会因为一块笔记而整周不出札记。
    """
    body = "正文段落。" * 6000                       # 30000 字符 → 3 块
    assert len(chunk_text(body)) == 3               # 夹具前提
    _pdf_route(monkeypatch, tmp_path, body)
    llm = _DeepLLM(fail_chunks={2}, fail_kind="块 2 超时")
    written = _mock_ingest_io(monkeypatch, llm)

    seg = _seg()
    rep = ING.run_ingest([seg], _settings(tmp_path), "2026-08-03",
                         top_n=1, close_read=True, seen=set())

    assert rep["status"] == "ok" and written == [1]   # 闸门没响、终稿照写
    assert rep["full_text"] == 1
    ocr = seg.close_reading
    assert ocr is not None and ocr.reading_depth == "chunked" and ocr.n_chunks == 3
    assert sorted(llm.chunk_calls) == [1, 2, 3]       # 挂掉的块不影响其余块继续读
    assert llm.n_synth_calls == 1                     # 汇总照跑（usable 非空）
    assert "结果与效应量" in [s.heading for s in ocr.sections]


# ---------------- (b) 单块致命：一次块调用 + 一次单跳回落 ----------------

def test_single_chunk_fatal_falls_back_to_exactly_one_single_call(monkeypatch, tmp_path):
    """**显式只切 1 块**的致命路径：块调用抛 402 → 无 usable → synthesize 返回 None
    → deep_close_read 返回 None → 回落单跳。总调用数必须恰好 2（1 块 + 1 单跳）。

    前提写死为单块是刻意的：deep_read_chunks 用 ThreadPoolExecutor 一次性 submit 全部块、
    fatal_evt 只在 worker 入口检查，块数 N≥2 时前 min(N,4) 个 worker 都已越过检查点各发
    一次调用，实际块调用数是 1..min(N,4) 的竞态值。在多块场景下断言精确次数只会让这条
    门禁用例在 CI 上间歇性变红。占位契约另见 (b2)。
    """
    body = "正文段落。" * 1000                        # 5000 字符
    assert len(body) <= 12000 and len(chunk_text(body)) == 1   # 夹具前提：只有 1 块
    _pdf_route(monkeypatch, tmp_path, body)
    llm = _DeepLLM(fail_chunks="all", fail_kind="402 Payment Required: insufficient balance")

    out = cr.close_read_segment(_seg(), "我的研究主线 MNAR/MA-GCT", llm,
                                scratch_dir=tmp_path, deep=True,
                                max_chars=120000, max_chunks=12)

    assert len(llm.calls) == 2                        # 1 次块调用 + 1 次单跳，无空转
    assert llm.chunk_calls == [1]
    assert llm.n_synth_calls == 0                     # 无 usable 块 → 汇总根本不发
    assert llm.calls[1].startswith(_SINGLE_PROMPT_HEAD)   # 第二次确实是单跳精读 prompt
    assert out is not None and out.reading_depth == "single-call" and out.n_chunks == 1
    assert out.from_full_text is True and out.source == "unpaywall"


# ---------------- (b2) 致命错误后的占位契约（max_workers=1，无竞态） ----------------

def test_deep_read_chunks_fills_placeholders_after_fatal_error_serial():
    """首块 402 → 其余块不再发起调用，但必须逐块补 _api_error 占位。

    max_workers=1 让 fatal_evt 的短路行为完全确定（无并发调度竞态）：
    占位条目的**数量与内容**是下游判定的输入（notes 长度 + n_ok/n_api 计数），
    少一条就会把「额度耗尽」判成别的状态。
    """
    llm = _DeepLLM(fail_chunks="all", fail_kind="402 insufficient balance")
    notes = deep_read_chunks(["c1", "c2", "c3", "c4", "c5"], llm, None, max_workers=1)

    assert len(notes) == 5
    assert notes[0]["_chunk"] == 1 and notes[0]["_error"] is True
    assert notes[0]["_api_error"] is True
    for i, n in enumerate(notes[1:], start=2):
        assert n == {"_chunk": i, "_error": True, "_api_error": True}
    assert len(llm.calls) == 1        # 致命后不再烧任何一次调用


# ---------------- (c) 单跳也失败：done==0，闸门必须响 ----------------

def test_deep_and_single_both_fail_returns_none_and_ingest_raises(monkeypatch, tmp_path):
    """块与单跳全挂 → close_read_segment 返回 None、done==0 → ingest 照旧抛
    RuntimeError 且不写终稿。深读不得削弱这条现有语义（否则降级札记会被 seen 永久固化）。
    """
    body = "正文段落。" * 6000
    _pdf_route(monkeypatch, tmp_path, body)
    llm = _DeepLLM(fail_chunks="all", fail_kind="通路挂了", fail_single=True)

    # 先看单篇：深读与单跳双双失败时必须是 None，而不是半成品
    assert cr.close_read_segment(_seg(), "ri", llm, scratch_dir=tmp_path,
                                 deep=True, max_chars=120000) is None

    llm2 = _DeepLLM(fail_chunks="all", fail_kind="通路挂了", fail_single=True)
    written = _mock_ingest_io(monkeypatch, llm2)
    seg = _seg()
    with pytest.raises(RuntimeError, match="全文精读 0/"):
        ING.run_ingest([seg], _settings(tmp_path), "2026-08-03",
                       top_n=1, close_read=True, seen=set())
    assert not written                 # 终稿一个字都没写
    assert seg.close_reading is None


def test_deep_close_read_records_synth_budget_truncation(monkeypatch):
    """R2:auto 侧的 max_chunks=12 并**不能**保证不越 60000 汇总预算(实测 12 块 ×
    每块 ~5.8k 字符即超),而 auto 没有 manual 那样的亲读核验兜底——裁剪必须留痕在条目上,
    否则 reading_depth="chunked" / n_chunks=12 是在虚报"完整分块深读"。"""
    from src.scholar import closereading as crmod
    from src.scholar import pdf_ingest as pi
    from src.scholar.schema import CloseReading, CloseReadSection, CloseReadSentence

    big = [{"claims": ["c" * 5800]} for _ in range(12)]
    monkeypatch.setattr(pi, "chunk_text", lambda *a, **k: ["x" * 1000] * 12)
    monkeypatch.setattr(pi, "deep_read_chunks", lambda *a, **k: big)

    def _fake(notes, llm, model, ri, budget_info=None):
        # 真跑打包逻辑（这才是被测对象），CloseReading 用固定桩
        pi._pack_chunk_notes([n for n in notes if not n.get("_error")], info=budget_info)
        cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
            CloseReadSection(heading="研究问题", sentences=[
                CloseReadSentence(text="x", tag=None)])])
        return cr, "", False

    monkeypatch.setattr(pi, "synthesize_deep_read", _fake)
    cr = crmod.deep_close_read("body" * 5000, None, "m", "ri",
                               source="oa-pdf", from_full_text=True)
    assert cr is not None
    assert cr.synth_truncated is True
    assert cr.synth_dropped_chunks is not None
    assert cr.n_chunks == 12 and cr.reading_depth == "chunked"
