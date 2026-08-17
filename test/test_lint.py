# -*- coding: utf-8 -*-
"""src/scholar/lint.py 回归（不连网、不调 LLM、不碰 Ollama）。

知识层 lint 的失败模式和概念页不一样。概念页最怕**编造**（写出库里没有的 citekey），
这份报告最怕**虚假的安心**：一份写着「✅ 没发现问题」的报告，如果那一项其实压根没跑、
或者只覆盖了库的一半，比不生成报告危险得多——它给出的是主动的保证。

所以这里锁住的四类东西是：

1. **分母不许丢**：撤稿扫描必须如实report无 DOI/查无此条/未查成三类，`0 篇撤稿`
   永远不能脱离覆盖率单独出现；
2. **跳过不许渲染成通过**：`--skip-*` / `--offline` 的那一节必须写「本轮未执行」，
   frontmatter 里对应计数必须是 `null` 而不是 `0`；
3. **编号回译**（同概念页）：裁决只认 P1..Pn，越界/非白名单分类/重复编号一律丢弃，
   `note` 里模型自写的引用一律剥掉；
4. **候选生成的配额与排除**：同篇不配对、主观批注分节不入池、单对论文与单条 highlight
   都有配额——否则几十对候选全被两篇结构相似的论文占满。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar import lint as L                                         # noqa: E402
from src.scholar import topics as T                                       # noqa: E402


# ---------------------------------------------------------------------------
# 假向量库：只要有 records + mat 两个属性即可（find_contradiction_candidates 不用 search）
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, records, vecs):
        self.records = records
        self.mat = np.array(vecs, dtype=np.float32)


def _rec(cid, citekey, text, role="citable", section="实验方法", level="highlight",
         note_file="札记.md", note_line=1, year=2024):
    return {"id": cid, "level": level, "citekey": citekey, "text": text, "role": role,
            "section": section, "note_file": note_file, "note_line": note_line,
            "year": year, "tier": "high", "bucket": []}


def _unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# 1. 候选生成
# ---------------------------------------------------------------------------

def test_same_paper_pairs_never_become_candidates():
    """一篇论文自己在「局限」里说的话与它自己在「结论」里说的话之间不存在文献间冲突。
    向量完全相同（相似度 1.0）也必须被挡住——这是 exclude 同篇的最强测试。"""
    store = FakeStore(
        [_rec("h1", "same2024Paper", "甲句", role="citable"),
         _rec("h2", "same2024Paper", "乙句", role="refutable")],
        [_unit(1, 0), _unit(1, 0)])
    assert L.find_contradiction_candidates(store, min_sim=0.5) == []


def test_cross_paper_pair_is_found_and_numbered():
    store = FakeStore(
        [_rec("h1", "a2024X", "甲句", role="citable"),
         _rec("h2", "b2024Y", "乙句", role="refutable")],
        [_unit(1, 0.02), _unit(1, 0)])
    pairs = L.find_contradiction_candidates(store, min_sim=0.5)
    assert [p.ref for p in pairs] == ["P1"]
    assert (pairs[0].a.citekey, pairs[0].b.citekey) == ("a2024X", "b2024Y")
    assert pairs[0].score > 0.99


def test_min_sim_gate_excludes_unrelated():
    store = FakeStore(
        [_rec("h1", "a2024X", "甲句", role="citable"),
         _rec("h2", "b2024Y", "乙句", role="refutable")],
        [_unit(1, 0), _unit(0, 1)])       # 正交，余弦 0
    assert L.find_contradiction_candidates(store, min_sim=0.5) == []


def test_subjective_annotation_sections_are_excluded_by_prefix():
    """「对我研究的联想」是精读者自己的推测性批注，不是文献内容——两条主观联想
    互相"冲突"没有知识意义。库里这个分节有三种带括注的变体写法，所以必须按
    **前缀**匹配（同 topics.retrieve_evidence 的既有处理）。"""
    variants = ["对我研究的联想",
                "对我研究的联想（研究者自身引申，非论文原创结论）",
                "对我研究的联想(MNAR / MA-GCT / 缺失机制 / 生存分析)"]
    for sec in variants:
        store = FakeStore(
            [_rec("h1", "a2024X", "甲句", role="citable", section=sec),
             _rec("h2", "b2024Y", "乙句", role="refutable")],
            [_unit(1, 0), _unit(1, 0)])
        assert L.find_contradiction_candidates(store, min_sim=0.5) == [], sec


def test_per_key_pair_cap_keeps_only_best_pair_per_paper_couple():
    """两篇结构相似的论文光靠「同名精读分节平行描述」就能刷出十几对候选。
    每对论文只留分数最高的那一个，否则裁决预算被两篇论文吃光。"""
    store = FakeStore(
        [_rec("h1", "a2024X", "甲一", role="citable"),
         _rec("h2", "a2024X", "甲二", role="citable"),
         _rec("h3", "b2024Y", "乙一", role="refutable"),
         _rec("h4", "b2024Y", "乙二", role="refutable")],
        [_unit(1, 0.01), _unit(1, 0.02), _unit(1, 0), _unit(1, 0.03)])
    pairs = L.find_contradiction_candidates(store, min_sim=0.5, per_key_pair_cap=1)
    assert len(pairs) == 1
    pairs2 = L.find_contradiction_candidates(store, min_sim=0.5, per_key_pair_cap=3)
    assert len(pairs2) == 3


def test_per_highlight_cap_allows_one_sentence_to_conflict_with_several_papers():
    """与上一条相反：一句话同时和三篇论文冲突恰恰是最值得看的信号
    （实测 poulain2024Graph 那句「F1 阈值固定 0.5」同时对上 3 篇），
    这个配额**不能**是 1，否则最强的发现被削成最弱的。"""
    store = FakeStore(
        [_rec("h0", "hub2024", "枢纽句", role="refutable"),
         _rec("h1", "a2024X", "甲一", role="citable"),
         _rec("h2", "b2024Y", "甲二", role="citable"),
         _rec("h3", "c2024Z", "甲三", role="citable")],
        [_unit(1, 0), _unit(1, 0.01), _unit(1, 0.02), _unit(1, 0.03)])
    pairs = L.find_contradiction_candidates(store, min_sim=0.5)
    assert len(pairs) == 3
    assert all(p.b.citekey == "hub2024" for p in pairs)
    assert L.DEFAULT_PER_HIGHLIGHT_CAP >= 3

    capped = L.find_contradiction_candidates(store, min_sim=0.5, per_highlight_cap=1)
    assert len(capped) == 1


def test_max_pairs_truncates_after_sorting_by_score():
    store = FakeStore(
        [_rec("h1", "a2024X", "甲一", role="citable"),
         _rec("h2", "b2024Y", "甲二", role="citable"),
         _rec("h3", "c2024Z", "乙", role="refutable")],
        [_unit(1, 0.5), _unit(1, 0.0), _unit(1, 0.0)])
    pairs = L.find_contradiction_candidates(store, min_sim=0.5, max_pairs=1)
    assert len(pairs) == 1
    assert pairs[0].a.citekey == "b2024Y"      # 与乙更近的那条胜出


def test_identical_text_pairs_are_dropped():
    """同一句话被两篇论文都写了（或同句因 role 不同在库里存了两份 chunk）——
    文本完全相同的"冲突"是噪音不是发现。"""
    store = FakeStore(
        [_rec("h1", "a2024X", "完全一样的一句话", role="citable"),
         _rec("h2", "b2024Y", "完全一样的一句话", role="refutable")],
        [_unit(1, 0), _unit(1, 0)])
    assert L.find_contradiction_candidates(store, min_sim=0.5) == []


def test_paper_level_chunks_never_enter_candidates():
    """paper 级 chunk 是标题+一句话，不是可引用的句级证据。"""
    store = FakeStore(
        [_rec("p1", "a2024X", "标题", role=None, level="paper"),
         _rec("h2", "b2024Y", "乙句", role="refutable")],
        [_unit(1, 0), _unit(1, 0)])
    assert L.find_contradiction_candidates(store, min_sim=0.5) == []


def test_blank_text_records_are_skipped():
    store = FakeStore(
        [_rec("h1", "a2024X", "   ", role="citable"),
         _rec("h2", "b2024Y", "乙句", role="refutable")],
        [_unit(1, 0), _unit(1, 0)])
    assert L.find_contradiction_candidates(store, min_sim=0.5) == []


def test_attach_pair_titles_fills_from_index():
    store = FakeStore(
        [_rec("h1", "a2024X", "甲句", role="citable"),
         _rec("h2", "b2024Y", "乙句", role="refutable")],
        [_unit(1, 0), _unit(1, 0.01)])
    pairs = L.find_contradiction_candidates(store, min_sim=0.5)
    L.attach_pair_titles(pairs, {"papers": [{"citekey": "a2024X", "title": "甲论文"}]})
    assert pairs[0].a.title == "甲论文"
    assert pairs[0].b.title is None


# ---------------------------------------------------------------------------
# 2. 裁决：编号回译（防幻觉主防线）
# ---------------------------------------------------------------------------

def _pair(ref, ka="a2024X", kb="b2024Y"):
    return L.CandidatePair(ref=ref, score=0.9,
                           a=L.PairSide(citekey=ka, text="甲句"),
                           b=L.PairSide(citekey=kb, text="乙句"))


def test_verdict_refs_are_back_translated_and_out_of_range_dropped():
    pairs = [_pair("P1"), _pair("P2")]
    data = {"verdicts": [
        {"pair": "P1", "relation": "conflict", "note": "真冲突"},
        {"pair": "P9", "relation": "conflict", "note": "编号越界"},     # 召回只有 2 对
    ]}
    out, rep = L.validate_verdicts(data, pairs)
    assert [v.pair.ref for v in out] == ["P1"]
    assert rep.invalid_refs == 1
    assert rep.judged == 1


@pytest.mark.parametrize("written", ["P3", "p3", "P03"])
def test_ref_spelling_variants_normalize(written):
    pairs = [_pair("P1"), _pair("P2"), _pair("P3")]
    out, rep = L.validate_verdicts(
        {"verdicts": [{"pair": written, "relation": "none"}]}, pairs)
    assert [v.pair.ref for v in out] == ["P3"]
    assert rep.invalid_refs == 0


def test_unknown_relation_is_dropped_not_defaulted_to_none():
    """兜底成 `none` 会把"模型答坏了"静默变成"确认无冲突"——恰恰是最不该被掩盖的
    那一种失败。必须整条丢弃并计数。"""
    out, rep = L.validate_verdicts(
        {"verdicts": [{"pair": "P1", "relation": "有点冲突", "note": "x"}]}, [_pair("P1")])
    assert out == []
    assert rep.unknown_relations == 1
    assert rep.judged == 0


def test_duplicate_ref_keeps_first_and_counts():
    out, rep = L.validate_verdicts({"verdicts": [
        {"pair": "P1", "relation": "conflict", "note": "先"},
        {"pair": "P1", "relation": "none", "note": "后"},
    ]}, [_pair("P1")])
    assert len(out) == 1 and out[0].relation == "conflict"
    assert rep.duplicate_refs == 1


def test_model_written_citations_are_stripped_from_note():
    """`note` 是唯一能绕开编号回译的编造通道：模型在这里写 `[@key]` 或裸 `@key`，
    pandoc 会当真引用去挂书目，而它从没经过任何校验。复用 topics._clean_text。"""
    out, rep = L.validate_verdicts({"verdicts": [
        {"pair": "P1", "relation": "conflict",
         "note": "如 [@fake2020Ghost] 与研究表明@sneaky2020X所述，两者冲突"},
    ]}, [_pair("P1")])
    assert "@" not in out[0].note
    assert "fake2020Ghost" not in out[0].note and "sneaky2020X" not in out[0].note
    # 匹配体必须限定 ASCII，否则剥离会连带吃掉后面半句中文正文
    assert "所述" in out[0].note and "两者冲突" in out[0].note
    assert rep.stripped_cites == 2


def test_non_dict_verdict_items_are_ignored():
    out, rep = L.validate_verdicts({"verdicts": ["乱七八糟", None, 42]}, [_pair("P1")])
    assert out == [] and rep.judged == 0


def test_missing_verdicts_key_is_not_a_crash():
    out, rep = L.validate_verdicts({}, [_pair("P1")])
    assert out == [] and rep.judged == 0


# ---------------------------------------------------------------------------
# 3. 裁决编排：单批失败不带走整轮
# ---------------------------------------------------------------------------

class _LLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def call(self, prompt, **kw):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


_TPL = "裁决下面这些：\n{{PAIR_BLOCK}}"


def test_one_failed_batch_does_not_discard_the_others():
    """候选对彼此独立，第 2 批撞限流不该让第 1 批已经拿到的裁决一起作废
    （同 build_topics.py 主循环"单页失败不带走整批"）。"""
    pairs = [_pair("P{}".format(i)) for i in range(1, 5)]
    llm = _LLM(['{"verdicts":[{"pair":"P1","relation":"conflict","note":"a"},'
                '{"pair":"P2","relation":"none"}]}',
                RuntimeError("所有 LLM provider 均不可用")])
    out, rep = L.adjudicate_contradictions(llm, pairs, _TPL, batch_size=2)
    assert [v.pair.ref for v in out] == ["P1", "P2"]
    assert rep.batches_failed == 1
    assert rep.errors and "所有 LLM provider" in rep.errors[0]


def test_consecutive_batch_failures_trip_the_breaker():
    """回退链一旦整条耗尽，后面每批都会以同一个错误再失败一次——连挂 2 批就停，
    剩余批次一并计入 batches_failed（不能让缺席的批次看起来像"裁决过且无冲突"）。"""
    pairs = [_pair("P{}".format(i)) for i in range(1, 11)]
    llm = _LLM([RuntimeError("挂了")] * 5)
    out, rep = L.adjudicate_contradictions(llm, pairs, _TPL, batch_size=2)
    assert out == []
    assert llm.calls == 2                      # 只真调了 2 次就熔断
    assert rep.batches_failed == 5             # 2 次真失败 + 剩余 3 批一并计入
    assert any("中止剩余" in e for e in rep.errors)


def test_unparseable_output_counts_as_batch_failure():
    llm = _LLM(["已完成裁决，要点如下：……"])       # 没有花括号，parse_synthesis 救不回来
    out, rep = L.adjudicate_contradictions(llm, [_pair("P1")], _TPL, batch_size=12)
    assert out == [] and rep.batches_failed == 1


def test_prompt_carries_only_numbers_never_citekeys():
    """喂给 LLM 的候选块**不含 citekey**——模型看不到就写不出，这是编号回译的物理前提。"""
    p = L.CandidatePair(ref="P1", score=0.9,
                        a=L.PairSide(citekey="secret2024Key", text="甲句", role="citable",
                                     section="实验方法", year=2024),
                        b=L.PairSide(citekey="other2024Key", text="乙句", role="refutable"))
    text = L.build_contradiction_prompt([p], _TPL)
    assert "secret2024Key" not in text and "other2024Key" not in text
    assert "P1" in text and "甲句" in text and "乙句" in text
    # 分节名与年份要带上：同一句在"作者自述做法"与"精读者挑毛病"两种语境下
    # 该不该算张力完全不同
    assert "实验方法" in text and "2024" in text


def test_load_prompt_template_requires_placeholder(tmp_path):
    bad = tmp_path / "p.md"
    bad.write_text("没有占位符", encoding="utf-8")
    with pytest.raises(T.TopicError):
        L.load_prompt_template(bad)
    with pytest.raises(T.TopicError):
        L.load_prompt_template(tmp_path / "不存在.md")


# ---------------------------------------------------------------------------
# 4. 撤稿扫描
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("10.1038/S41598-024-75784-5", "10.1038/s41598-024-75784-5"),
    ("https://doi.org/10.1038/X", "10.1038/x"),
    ("http://dx.doi.org/10.1038/X", "10.1038/x"),
    ("doi:10.1038/X", "10.1038/x"),
    ("  10.1038/X/  ", "10.1038/x"),
    ("10.48550/arXiv.2101.09986", "10.48550/arxiv.2101.09986"),
    (None, ""), ("", ""),
])
def test_normalize_doi(raw, want):
    """OpenAlex 回传的 DOI 一律小写全 URL 形态，索引里存的大小写混排（arXiv DOI 尤其）。
    两边不走同一个归一化就会对不上号，而且可能只有一部分对不上——那会静默缩小实际
    扫描范围。"""
    assert L.normalize_doi(raw) == want


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP {}".format(self.status_code))

    def json(self):
        return self._p


class _Client:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def get(self, url, params=None):
        self.requests.append(params or {})
        return self.handler(params or {})

    def close(self):
        pass


def _index(*papers):
    return {"papers": list(papers)}


def _paper(ck, doi=None, dup=None, deep=False, tier="high", year=2024, title=None,
           month=None):
    return {"citekey": ck, "doi": doi, "duplicate_of": dup, "year": year,
            "has_full_text_reading": deep, "priority_tier": tier, "month": month,
            "title": title or "论文 {}".format(ck)}


def test_retraction_hit_is_reported_with_openalex_signal():
    idx = _index(_paper("bad2024Paper", doi="10.1/bad"), _paper("ok2024Paper", doi="10.1/ok"))
    client = _Client(lambda p: _Resp({"results": [
        {"doi": "https://doi.org/10.1/bad", "is_retracted": True,
         "display_name": "RETRACTED ARTICLE: 某某", "id": "https://openalex.org/W1"},
        {"doi": "https://doi.org/10.1/ok", "is_retracted": False, "display_name": "正常"},
    ]}))
    scan = L.check_retractions(idx, client=client)
    assert [h.citekey for h in scan.hits] == ["bad2024Paper"]
    assert scan.hits[0].signal == "openalex-flag"
    assert scan.hits[0].openalex_id == "https://openalex.org/W1"
    assert (scan.n_papers, scan.n_with_doi, scan.n_resolved) == (2, 2, 2)
    assert scan.coverage == 1.0


def test_title_marker_is_an_independent_second_signal():
    """OpenAlex 的 `is_retracted` 有已知覆盖缺口（部分期刊只改标题不发结构化撤稿记录）。
    标题前缀必须能独立命中，否则那批论文永远查不出来。"""
    idx = _index(_paper("sneaky2024", doi="10.1/x"))
    client = _Client(lambda p: _Resp({"results": [
        {"doi": "https://doi.org/10.1/x", "is_retracted": False,
         "display_name": "WITHDRAWN: 某个被撤下的研究"}]}))
    scan = L.check_retractions(idx, client=client)
    assert [h.signal for h in scan.hits] == ["title-marker"]


def test_papers_without_doi_are_counted_not_silently_dropped():
    """`0 篇撤稿`在 358 篇没有 DOI 的前提下不等于"库是干净的"。分母必须跟着结论走。"""
    idx = _index(_paper("nodoi2024"), _paper("has2024", doi="10.1/x"))
    client = _Client(lambda p: _Resp({"results": [
        {"doi": "https://doi.org/10.1/x", "is_retracted": False, "display_name": "正常"}]}))
    scan = L.check_retractions(idx, client=client)
    assert scan.n_papers == 2 and scan.n_no_doi == 1 and scan.n_with_doi == 1
    assert scan.n_resolved == 1
    assert scan.coverage == 0.5


def test_unresolved_dois_are_counted_separately_from_clean():
    idx = _index(_paper("ghost2024", doi="10.1/nowhere"))
    client = _Client(lambda p: _Resp({"results": []}))
    scan = L.check_retractions(idx, client=client)
    assert scan.n_unresolved == 1 and scan.n_resolved == 0 and scan.hits == []


def test_network_failure_is_counted_never_raised():
    """月度无人值守链路的一环：一次 DNS 抖动不该让整份报告生成不出来，
    但失败的那批 DOI 必须计入 n_failed，与"查了、没问题"严格区分。"""
    def boom(_p):
        raise RuntimeError("Connection reset")
    idx = _index(_paper("a2024", doi="10.1/a"), _paper("b2024", doi="10.1/b"))
    scan = L.check_retractions(idx, client=_Client(boom))
    assert scan.n_failed == 2 and scan.n_resolved == 0 and scan.hits == []
    assert scan.errors and "Connection reset" in scan.errors[0]


def test_dois_with_filter_reserved_chars_are_skipped_not_injected():
    """`,` 与 `|` 是 OpenAlex filter 语法的 AND/OR 分隔符，塞进去不会报错，
    会**静默查成别的东西**。宁可漏查也不要查错。"""
    idx = _index(_paper("weird2024", doi="10.1/a,b"), _paper("pipe2024", doi="10.1/a|b"))
    client = _Client(lambda p: _Resp({"results": []}))
    scan = L.check_retractions(idx, client=client)
    assert client.requests == []               # 一个网络请求都不该发
    assert scan.n_no_doi == 2 and len(scan.errors) == 2


def test_duplicates_and_non_keepers_are_out_of_scope():
    """跨月重复条目不在库里承担证据责任，替它们查撤稿是白花网络请求
    （与 embed_store.chunks_from_index / notes_query 的入选口径一致）。"""
    idx = _index(_paper("keep2024", doi="10.1/a"),
                 _paper("dup2024", doi="10.1/b", dup="keep2024"),
                 {"citekey": None, "doi": "10.1/c"})
    client = _Client(lambda p: _Resp({"results": []}))
    scan = L.check_retractions(idx, client=client)
    assert scan.n_papers == 1


def test_batching_respects_size_and_uses_larger_per_page():
    """同一个 DOI 在 OpenAlex 里可能对应多条 work（arXiv DOI 会同时匹配 preprint 与
    正式版），per-page 必须大于 batch_size，否则边界上静默截断会把没返回到的条目
    误算成"查无此条"。"""
    idx = _index(*[_paper("p{}".format(i), doi="10.1/{}".format(i)) for i in range(5)])
    client = _Client(lambda p: _Resp({"results": []}))
    L.check_retractions(idx, client=client, batch_size=2)
    assert len(client.requests) == 3           # 5 个 DOI / 每批 2 个
    for req in client.requests:
        assert req["per-page"] > 2
        assert req["filter"].startswith("doi:")
    assert all("mailto" not in r for r in client.requests)


def test_same_citekey_hit_twice_is_deduped():
    """同一 DOI 命中两条 OpenAlex work 时，同一篇论文不该在报告里出现两次。"""
    idx = _index(_paper("dupe2024", doi="10.1/x"))
    client = _Client(lambda p: _Resp({"results": [
        {"doi": "https://doi.org/10.1/x", "is_retracted": True, "display_name": "A"},
        {"doi": "https://doi.org/10.1/x", "is_retracted": True, "display_name": "A（v2）"},
    ]}))
    scan = L.check_retractions(idx, client=client)
    assert len(scan.hits) == 1


# ---------------------------------------------------------------------------
# 5. 概念页论断解析与陈旧判定
# ---------------------------------------------------------------------------

_PAGE = """## 主要发现

- 掩码嵌入在 MIMIC-IV 上提升 AUROC 0.02 [@a2015Old] [@b2016Old]
- 一条没有任何引用的话，不该被当成论断
- 另一条较新的论断 [@c2025New]

## ⚔️ 分歧与冲突

### 某个争议

- **一方**：旧观点 [@a2015Old]
- **另一方**：新观点 [@c2025New]

## 本页证据（2 条 · 2 篇）

- ● **E1** `[@a2015Old]` 可引用证据 · 实验方法 · 札记.md:1
  > 原句一
- ○ **E2** `[@zz1999Ancient]` 可引用证据 · 实验方法 · 札记.md:2
  > 原句二
"""


def _write_page(d, slug, body=_PAGE, extra_fm=""):
    fm = '---\ntopic: "{}"\ntitle: "演示"\n{}---'.format(slug, extra_fm)
    p = d / "{}.md".format(slug)
    p.write_text(T.assemble(fm, body, T.DEFAULT_USER_ZONE), encoding="utf-8")
    return p


def test_parse_page_claims_skips_evidence_table_and_uncited_lines():
    claims = L.parse_page_claims(_PAGE, "demo")
    texts = [c.text for c in claims]
    # 小节里 2 条 + 分歧区两侧各 1 条（分歧的立场也是要追溯的论断）
    assert len(claims) == 4
    assert "没有任何引用" not in " ".join(texts)
    # 证据表那两行带 `[@key]` 但不是论断——混进来会让每页凭空多出几十条
    assert all("E1" not in t and "E2" not in t for t in texts)
    # 证据表在 `## 本页证据` 之后，其中 zz1999Ancient 只出现在那里，绝不能被当论断引用
    assert all("zz1999Ancient" not in c.citekeys for c in claims)
    assert claims[0].citekeys == ["a2015Old", "b2016Old"]
    assert claims[0].heading == "主要发现"
    assert claims[-1].heading == "某个争议"


def test_parse_page_claims_ignores_user_annotation_zone(tmp_path):
    """「我的批注」里用户自己写的 `- 某某说法 [@key]` 是私人笔记，被判成"陈旧论断"
    是纯噪音（同 audit_topic_pages 只扫生成块的理由）。"""
    text = T.assemble('---\ntopic: "d"\n---', "## 小节\n\n- 生成的论断 [@a2015Old]",
                      T.DEFAULT_USER_ZONE + "\n- 我自己记的一条 [@myown1990Note]\n")
    claims = L.parse_page_claims(text, "d")
    assert [c.citekeys for c in claims] == [["a2015Old"]]


def test_stale_claim_uses_newest_supporting_year(tmp_path):
    """判据是"最新的一篇"：一条论断只要有任何近期证据撑着就不算陈旧，
    哪怕它同时引了 1990 年的原始方法论文——引经典文献是好事，不该被报成问题。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo")
    idx = _index(_paper("a2015Old", year=2015), _paper("b2016Old", year=2016),
                 _paper("c2025New", year=2025), _paper("zz1999Ancient", year=1999))
    stale = L.find_stale_claims(d, idx, max_age_years=5, now_year=2026)
    # 主要发现那条（最新支撑 2016）+ 分歧区「一方」（只有 2015）
    assert {s.newest_year for s in stale} == {2015, 2016}
    # 引了 2025 的那两条（含分歧区的「另一方」）不算陈旧——只要有一条近期证据撑着
    # 就不算，哪怕它同时引了很老的原始方法论文
    assert all("c2025New" not in s.claim.citekeys for s in stale)
    # 从旧到新排序，最该看的排最前
    assert [s.newest_year for s in stale] == sorted(s.newest_year for s in stale)


def test_unknown_year_disables_stale_judgement(tmp_path):
    """年份未知不能当"很老"处理，否则元数据缺失会被放大成一堆假阳性。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 一条论断 [@a2015Old] [@b2016Old]")
    # b2016Old 年份未知：整条不参与判定，而不是拿 a2015Old 顶上去判成陈旧
    idx = _index(_paper("a2015Old", year=2015), _paper("b2016Old", year=None))
    assert L.find_stale_claims(d, idx, max_age_years=5, now_year=2026) == []
    # 索引里压根查不到那个 citekey，同样不判（未知 ≠ 很老）
    assert L.find_stale_claims(d, _index(_paper("a2015Old", year=2015)),
                               max_age_years=5, now_year=2026) == []


def test_lint_report_itself_is_not_scanned_as_a_topic_page(tmp_path):
    """`_lint.md` 里逐条列出的 citekey 若被当成概念页内容，覆盖率会被自己虚报上去
    （报告越长虚报越多——自己引用自己）。这是 is_topic_page_file 在 cited_citekeys
    这一处的**唯一**防线：那里是纯文本扫 `[@key]`，根本不看 frontmatter。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024Paper]")
    (d / "_lint.md").write_text(
        T.assemble('---\ntype: "lint"\n---',
                   "- `[@orphan2024Paper]` 某篇撤稿论文", T.DEFAULT_USER_ZONE,
                   generator=L.LINT_GENERATOR), encoding="utf-8")
    (d / "INDEX.md").write_text("| x | [@indexonly2024] |", encoding="utf-8")
    assert L.cited_citekeys(d) == {"cited2024Paper"}
    assert set(L.cited_by_page(d)) == {"demo"}


def test_coverage_orphans_are_high_tier_deep_read_only(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]",
                extra_fm="n_evidence: 42\n")
    idx = _index(_paper("cited2024", deep=True, tier="high"),
                 _paper("orphan2024", deep=True, tier="high"),
                 _paper("shallow2024", deep=False, tier="high"),   # 没精读，不算孤儿
                 _paper("lowtier2024", deep=True, tier="low"))     # 非高优先级，不算
    rep = L.coverage_report(d, idx)
    assert [e["citekey"] for e in rep.orphans] == ["orphan2024"]
    assert rep.n_orphans_total == 1
    assert (rep.n_keeper, rep.n_cited, rep.n_deep_read, rep.n_deep_read_cited) == (4, 1, 3, 1)
    # 第三个元素是该页配置的 max_evidence；调用方没给 specs 时是 None（A2）
    assert rep.thin_pages == [("demo", 42, None)]
    assert rep.n_pages == 1


# ---------------------------------------------------------------------------
# 6. 报告渲染：跳过 ≠ 通过
# ---------------------------------------------------------------------------

def _render(**kw):
    base = dict(verdicts=[], candidates=[], verdict_report=L.VerdictReport(),
                retraction=L.RetractionScan(), stale_claims=[],
                coverage=L.CoverageReport())
    base.update(kw)
    return L.render_lint_report(**base)


@pytest.mark.parametrize("flag,heading", [
    ("contradictions_skipped", "跨文献对撞"),
    ("stale_skipped", "证据基础可能过时"),
    ("coverage_skipped", "覆盖缺口"),
])
def test_skipped_section_never_renders_as_a_green_check(flag, heading):
    """首版实测踩过：`--skip-stale --skip-coverage` 跑出来的报告里两节都写着大绿勾
    （"✅ 没有论断的证据基础全部老于该阈值"），而它们压根没跑——这比不生成报告危险
    得多，因为它给出的是**主动的**虚假保证。"""
    text = _render(**{flag: True})
    section = text.split("## ", 1)[0]
    for block in text.split("\n## "):
        if heading in block.splitlines()[0]:
            assert "本轮**未执行**" in block
            assert "✅" not in block
            return
    pytest.fail("报告里没有 {} 这一节：{}".format(heading, section))


def test_offline_retraction_section_says_the_conclusion_does_not_hold():
    text = _render(retraction=L.RetractionScan(skipped=True))
    block = [b for b in text.split("\n## ") if "撤稿检查" in b.splitlines()[0]][0]
    assert "本轮**未执行**" in block and "本轮不成立" in block
    assert "✅" not in block


def test_retraction_section_always_carries_its_denominator():
    scan = L.RetractionScan(n_papers=100, n_with_doi=80, n_resolved=75,
                            n_no_doi=20, n_unresolved=5, n_failed=0)
    text = _render(retraction=scan)
    assert "100" in text and "80" in text and "75" in text
    assert "无 DOI 20" in text and "查无此条 5" in text


def test_retracted_paper_names_the_pages_built_on_it():
    """只报"库里有一篇撤稿"而不说它渗进了哪几页，人还是得自己全库 grep 一遍。"""
    scan = L.RetractionScan(n_papers=1, n_with_doi=1, n_resolved=1, hits=[
        L.RetractionHit(citekey="bad2024", doi="10.1/x", title="某某", signal="openalex-flag")])
    text = _render(retraction=scan,
                   cited_by_page={"mnar-diagnosis": ["bad2024"], "other-page": ["ok2024"]})
    assert "mnar-diagnosis" in text
    assert "other-page" not in text


def test_unjudged_batches_are_declared_in_the_report():
    """缺席的批次绝不能看起来像"裁决过且无冲突"。"""
    rep = L.VerdictReport(judged=0, batches_failed=3, errors=["第 1 批失败：限流"])
    text = _render(verdict_report=rep, candidates=[_pair("P1")],
                   contradictions_skipped=False)
    assert "3 批未裁决" in text and "不等于它们没问题" in text


# ---------------------------------------------------------------------------
# 7. 落盘：哨兵合并与摘要
# ---------------------------------------------------------------------------

def test_write_lint_report_creates_then_preserves_annotation(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    path, status = L.write_lint_report(d, "正文一", L.LintCounts(retracted=0))
    assert status == "new"
    txt = path.read_text(encoding="utf-8")
    # 哨兵里必须指向真正生成它的脚本（人按提示重跑时不能被指到 build_topics.py）
    assert "由 scripts/lint_notes.py 生成" in txt
    assert "build_topics.py 生成" not in txt

    path.write_text(txt.replace("## 我的批注", "## 我的批注\n\n这条我核对过，不是真冲突"),
                    encoding="utf-8")
    _p, status2 = L.write_lint_report(d, "正文二", L.LintCounts(retracted=1))
    assert status2 == "merged"
    after = path.read_text(encoding="utf-8")
    assert "这条我核对过" in after and "正文二" in after and "正文一" not in after


def test_write_lint_report_refuses_to_clobber_tampered_block(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    path, _ = L.write_lint_report(d, "正文", L.LintCounts())
    txt = path.read_text(encoding="utf-8")
    path.write_text(txt.replace("正文", "正文（我在生成块里手写了批注）"), encoding="utf-8")
    _p, status = L.write_lint_report(d, "新正文", L.LintCounts())
    assert status == "conflict"
    assert "我在生成块里手写了批注" in path.read_text(encoding="utf-8")


def test_skipped_counts_serialize_as_null_not_zero(tmp_path):
    """frontmatter 里写 `n_stale_claims: 0` 等于向任何读它的程序断言"查过了，一条
    都没有"。跳过时必须是 `null`。"""
    fm = L.build_lint_frontmatter(L.LintCounts(retracted=2), "2026-08-17T00:00:00")
    assert "n_retracted: 2" in fm
    assert "n_stale_claims: null" in fm
    assert 'type: "lint"' in fm
    # 报告不是概念页：不许有 topic 键（那是四处扫描认页面身份的唯一判据）
    assert "\ntopic:" not in fm


def test_lint_frontmatter_preserves_user_keys(tmp_path):
    fm = L.build_lint_frontmatter(L.LintCounts(), "2026-08-17T00:00:00",
                                  preserved={"我的备注": "待复核", "type": "被覆盖"})
    assert "待复核" in fm
    assert 'type: "lint"' in fm            # 受管键用新值，不被 preserved 顶掉


@pytest.mark.parametrize("rc,ok,alert", [(0, True, False), (1, True, True), (2, False, True)])
def test_summarize_lint_run_separates_tool_failure_from_finding(rc, ok, alert):
    """"lint 自己没跑成"与"lint 跑成了并发现撤稿"是两件事。混成一个布尔会让调用方
    只能二选一：要么撤稿不响，要么工具故障被当成撤稿。"""
    out = L.summarize_lint_run("🚨 已撤稿仍在库：[@bad2024] 某某\n其它输出", "某个错误", rc)
    assert (out.ok, out.alert) == (ok, alert)
    if rc == 1:
        assert "bad2024" in out.detail
    if rc == 2:
        # 诊断信息（退出码语义 + stderr）必须排在发现枚举之前：调用方 notify 按
        # 300 字符截断，排在后面会被挤掉（同 topics.summarize_build_topics_run 的 Y4）
        assert out.detail.index("退出码") < out.detail.index("某个错误")


def test_rc2_without_retraction_lines_is_not_an_alert():
    """B2 的反面：退 2 且 stdout 里没有 🚨 时，`alert` 必须还是 False——
    否则每次落盘冲突都会弹一条"库里有撤稿论文"的假警报，几次之后就没人信它了。"""
    out = L.summarize_lint_run("＝ 报告：/x/_lint.md（conflict）", "哨兵缺失", 2)
    assert (out.ok, out.alert) == (False, False)


def test_rc2_still_surfaces_a_retraction_already_found():
    """B2：撤稿命中早在扫描阶段就算出来、stdout 也已经打过 🚨 行。退出码只有一个，
    但 `ok`/`alert` 是两个独立字段——"发现了撤稿"绝不能因为报告写不进磁盘而丢失。"""
    out = L.summarize_lint_run(
        "🚨 已撤稿仍在库：[@bad2024] 某某\n⚠️ 报告落盘冲突：生成块被手改", "落盘冲突", 2)
    assert out.ok is False        # 工具确实没跑完整
    assert out.alert is True      # 但发现要响
    assert "bad2024" in out.detail


# ---------------------------------------------------------------------------
# 8. B1：撤稿标题信号在 CJK 上的边界
#
# `\b` 与 `\w` 是同一个坑的两个载体：Python 3 的 `\b` 也按 Unicode 定义单词字符，
# 汉字算 `\w`，所以「撤稿」后面紧跟另一个汉字时两侧都是 word-char、边界判定不到，
# 整条匹配失败。`撤稿：某某` 能命中纯属冒号是非 word 字符的巧合——而这恰恰是老测试
# 唯一覆盖的形状（`"WITHDRAWN: 某个…"`，英文标记后天然跟 ASCII 标点）。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "撤稿声明",                              # ← 老正则在这里直接漏判（真实撤稿通知标题）
    "撤稿声明：关于某某论文的处理决定",
    "已撤回论文",                             # ← 同上
    "已撤回：某某研究",
    "撤稿：某某论文",                          # 冒号救了老正则的那一种
    "RETRACTED ARTICLE: Deep learning for EHR",
    "WITHDRAWN: 某个被撤下的研究",
    "  撤稿声明",                             # 前导空白
    "retracted article: lowercase form",      # re.I
])
def test_retracted_title_marker_matches_cjk_and_ascii_forms(title):
    assert L._RETRACTED_TITLE_RE.match(title), title


@pytest.mark.parametrize("title", [
    "关于论文撤稿的政策研究",                   # 中间出现，不在开头
    "Policy analysis of RETRACTED papers",     # 同上，英文
    "Retractedness as a bibliometric signal",  # ASCII 标记后紧跟字母 = 另一个词
    "Withdrawnness of the cohort",
    "",
    "正常的论文标题",
])
def test_retracted_title_marker_does_not_fire_on_non_notices(title):
    assert not L._RETRACTED_TITLE_RE.match(title), title


def test_cjk_title_marker_is_an_independent_signal_end_to_end():
    """走完整条 check_retractions：OpenAlex 没打 is_retracted 标志、只有中文标题标记，
    而标记后面紧跟汉字——老正则在这里整篇漏判。"""
    idx = _index(_paper("cjk2024", doi="10.1/cjk"))
    client = _Client(lambda p: _Resp({"results": [
        {"doi": "https://doi.org/10.1/cjk", "is_retracted": False,
         "display_name": "撤稿声明：某某研究因数据问题被撤回"},
    ]}))
    scan = L.check_retractions(idx, client=client)
    assert [h.citekey for h in scan.hits] == ["cjk2024"]
    assert scan.hits[0].signal == "title-marker"


# ---------------------------------------------------------------------------
# 9. B5 / A2 / A3：覆盖缺口的口径
# ---------------------------------------------------------------------------

def _spec(slug, max_evidence=60):
    return T.TopicSpec(slug=slug, title=slug, question="q", queries=["q"],
                       max_evidence=max_evidence)


def test_n_pages_uses_the_same_criterion_as_thin_pages(tmp_path):
    """B5：`n_pages` 只按文件名计数、`thin_pages` 还要求 frontmatter 有 `topic` 键时，
    往 topics_dir 丢一个杂散 .md 就会让报告写"概念页 2 页覆盖了……"而表格只有 1 行。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]", extra_fm="n_evidence: 42\n")
    (d / "scratch.md").write_text("随手放进来的一条笔记，没有 frontmatter\n", encoding="utf-8")
    rep = L.coverage_report(d, _index(_paper("cited2024", deep=True)))
    assert rep.n_pages == 1
    assert len(rep.thin_pages) == 1


def test_thin_pages_carry_max_evidence_and_flag_saturation(tmp_path):
    """A2：实测 8 页的证据数与各页 max_evidence 一模一样——不带上限这项检查就退化成
    "复述配置文件里已经写好的数字"。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "full", body="## 小节\n\n- 论断 [@x2024]", extra_fm="n_evidence: 60\n")
    _write_page(d, "thin", body="## 小节\n\n- 论断 [@x2024]", extra_fm="n_evidence: 12\n")
    rep = L.coverage_report(d, _index(_paper("x2024")),
                            specs=[_spec("full", 60), _spec("thin", 60)])
    assert rep.thin_pages == [("thin", 12, 60), ("full", 60, 60)]


def test_saturated_pages_are_not_sold_as_thin_pages():
    cov = L.CoverageReport(n_pages=2, thin_pages=[("a", 60, 60), ("b", 70, 70)])
    text = _render(coverage=cov)
    assert "**薄**" not in text
    # 第 2 轮 L9：全饱和时收成一句话、不出表（那 8 行"我什么都告诉不了你"每月重印一次）
    assert "全部触及各自 `max_evidence` 上限" in text
    # 那句"垫底的几页要么没证据、要么 queries 写得不对"是在给一个永远不会出现的
    # 场景准备建议，诱导用户去做一件查不出结果的事
    assert "queries 写得不对" not in text


def test_genuinely_thin_page_still_gets_the_actionable_advice():
    cov = L.CoverageReport(n_pages=2, thin_pages=[("a", 3, 60), ("b", 60, 60)])
    text = _render(coverage=cov)
    assert "**薄**" in text
    assert "queries 写得不对" in text          # 这一次建议是成立的


def test_without_specs_no_misleading_advice_at_all():
    cov = L.CoverageReport(n_pages=1, thin_pages=[("a", 60, None)])
    text = _render(coverage=cov)
    assert "queries 写得不对" not in text
    assert "无从判断" in text


def test_orphans_split_recent_intake_from_settled(tmp_path):
    """A3：208 个孤儿里排前 15 清一色是 2025/2026 年论文——排序本身就是按年份倒序，
    这是分布使然，不是"该开新页"的信号。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]", extra_fm="n_evidence: 60\n")
    idx = _index(_paper("cited2024", deep=True, month="2020-01"),
                 _paper("new2026", deep=True, year=2026, month="2026-07"),
                 _paper("old2024", deep=True, year=2024, month="2024-03"),
                 _paper("nomonth", deep=True, year=2025, month=None))
    rep = L.coverage_report(d, idx, now_month="2026-08", recent_months=3)
    assert [e["citekey"] for e in rep.recent_orphans] == ["new2026"]
    # month 解析不出的一律进"值得考虑"那档（"新入库"是个需要证据的正面断言）；
    # 第 2 轮 L4：这一档改成年份**正序**（最老优先），old2024 排在 nomonth(2025) 前面
    assert [e["citekey"] for e in rep.orphans] == ["old2024", "nomonth"]
    assert (rep.n_orphans_total, rep.n_orphans_recent, rep.n_orphans_settled) == (3, 1, 2)


def test_recent_window_boundary_is_inclusive(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]")
    idx = _index(_paper("edge", deep=True, month="2026-06"),      # 窗口内最老的一个月
                 _paper("just_out", deep=True, month="2026-05"))  # 差一个月
    rep = L.coverage_report(d, idx, now_month="2026-08", recent_months=3)
    assert [e["citekey"] for e in rep.recent_orphans] == ["edge"]
    assert [e["citekey"] for e in rep.orphans] == ["just_out"]


@pytest.mark.parametrize("bad", ["", None, "2026", "2026-13", "2026-1", "去年", "2026/08"])
def test_malformed_month_never_counts_as_recent(bad):
    assert L._month_ord(bad) is None


def test_orphan_section_no_longer_claims_to_be_direct_basis_for_a_new_page():
    """A3：8 页全部饱和的前提下，「孤儿」= 没挤进任何一页的 top-max_evidence，
    不等于"跟这几个概念无关"，更不等于"queries 漏了问题域"。"""
    cov = L.CoverageReport(n_pages=1, n_orphans_total=1, n_orphans_settled=1,
                           orphans=[{"citekey": "o2024", "year": 2024, "title": "某篇"}],
                           thin_pages=[("p", 60, 60)])
    text = _render(coverage=cov)
    assert "直接依据" not in text
    assert "没挤进任何一页的 top-`max_evidence`" in text
    assert "notes_search.py" in text


def test_recent_orphan_bucket_says_why_it_is_probably_not_a_gap():
    cov = L.CoverageReport(n_pages=1, n_orphans_total=1, n_orphans_recent=1,
                           recent_orphans=[{"citekey": "n2026", "year": 2026,
                                            "month": "2026-07", "title": "新篇"}],
                           thin_pages=[("p", 60, 60)], recent_months=3)
    text = _render(coverage=cov)
    assert "最近 3 个月新入库" in text
    assert "还没排进任何一页的前 `max_evidence` 名" in text
    assert "入库 2026-07" in text


# ---------------------------------------------------------------------------
# 10. A4：单篇内部自相矛盾不套跨论文对撞的模板
# ---------------------------------------------------------------------------

def test_self_inconsistency_is_a_first_class_reportable_relation():
    assert "self-inconsistency" in L.RELATION_LABEL
    assert "self-inconsistency" in L.REPORTABLE_RELATIONS
    assert L.RELATION_EMOJI.get("self-inconsistency")


@pytest.mark.parametrize("written,want", [("甲", "a"), ("乙", "b"), ("a", "a"), ("B", "b")])
def test_subject_is_back_translated_like_the_pair_refs(written, want):
    """模型只写位置代号（甲/乙），citekey 由程序按位置填回——同编号回译，
    模型永远拿不到 citekey，也就写不出 citekey。"""
    pairs = [_pair("P1")]
    data = {"verdicts": [{"pair": "P1", "relation": "self-inconsistency",
                          "subject": written, "note": "附录不一致"}]}
    vs, _rep = L.validate_verdicts(data, pairs)
    assert vs[0].subject == want


def test_invalid_subject_does_not_discard_the_whole_verdict():
    """subject 不合法只影响标题措辞，不影响这条发现是否可追溯——丢掉整条反而会把
    一个真发现抹掉（与编号越界/分类越界的处置**不同**，那两者才必须丢）。"""
    vs, rep = L.validate_verdicts(
        {"verdicts": [{"pair": "P1", "relation": "self-inconsistency",
                       "subject": "丙", "note": "x"}]}, [_pair("P1")])
    assert len(vs) == 1 and vs[0].subject == ""
    assert rep.unknown_relations == 0


def test_self_inconsistency_names_one_paper_not_a_cross_pair():
    """报告首版把「乙自己的附录 I 与附录 R 打架」写成「甲 ↔ 乙」，读者第一反应是去
    核对这两篇谁的数字对，而真正该做的是翻乙自己的两个附录——发现被路由错了。"""
    v = L.Verdict(pair=_pair("P1", "liang2025Causal", "liang2026Learning"),
                  relation="self-inconsistency", subject="b",
                  note="乙的附录 I 与附录 R 对 GPU 型号自相矛盾")
    head = L._pair_lines(v)[0]
    assert "↔" not in head
    assert "liang2026Learning" in head
    assert "liang2025Causal" not in head          # 甲不该出现在标题里
    body = "\n".join(L._pair_lines(v))
    assert "回去翻 `[@liang2026Learning]` 自己的原文" in body
    assert "liang2025Causal" in body              # 但两句原文都还在，可自行核对


def test_self_inconsistency_without_subject_still_avoids_the_cross_template():
    v = L.Verdict(pair=_pair("P1", "aa2025X", "bb2026Y"),
                  relation="self-inconsistency", subject="", note="某处不一致")
    head = L._pair_lines(v)[0]
    assert "↔" not in head
    assert "裁决未点名" in head


def test_conflict_still_uses_the_cross_pair_template():
    head = L._pair_lines(L.Verdict(pair=_pair("P1"), relation="conflict"))[0]
    assert "↔" in head and "结论冲突" in head


def test_prompt_template_teaches_the_new_class_and_its_subject():
    text = (Path(__file__).resolve().parents[1] /
            "config/prompts/lint_contradiction_prompt.md").read_text(encoding="utf-8")
    assert "self-inconsistency" in text
    assert '"subject"' in text
    # 铁律 2（不许写 citekey）不能因为新增 subject 而被削弱
    assert "绝对不许写 citekey" in text


# ---------------------------------------------------------------------------
# 11. A6：「我已经确认过，这条不是问题」
# ---------------------------------------------------------------------------

def _pair_txt(ka, ta, kb, tb):
    return L.CandidatePair(ref="P1", score=0.9,
                           a=L.PairSide(citekey=ka, text=ta),
                           b=L.PairSide(citekey=kb, text=tb))


def test_pair_id_is_stable_under_side_swap():
    """候选生成时哪句当甲哪句当乙取决于 records 的遍历顺序，库一重建就可能反过来——
    ID 跟着变的话所有 ack 一次全失效。"""
    p1 = _pair_txt("a2024X", "甲句", "b2024Y", "乙句")
    p2 = _pair_txt("b2024Y", "乙句", "a2024X", "甲句")
    assert p1.pid == p2.pid
    assert len(p1.pid) == 8


def test_pair_id_changes_when_the_sentence_changes():
    """内容变了就是新问题，该重问——ack 必须自动失效。"""
    base = _pair_txt("a2024X", "甲句", "b2024Y", "乙句")
    assert _pair_txt("a2024X", "甲句（改了一个字）", "b2024Y", "乙句").pid != base.pid
    assert _pair_txt("a2024X", "甲句", "c2024Z", "乙句").pid != base.pid
    # 只差换行/首尾空白不算内容变化（同 _norm_text 的口径）
    assert _pair_txt("a2024X", " 甲\n句 ", "b2024Y", "乙句").pid == \
        _pair_txt("a2024X", "甲 句", "b2024Y", "乙句").pid


@pytest.mark.parametrize("line", [
    "- ack: ab12cd34 这条我看过了",
    "-ack:ab12cd34 这条我看过了",
    "* ack：ab12cd34 这条我看过了",          # 全角冒号 + 星号列表
    "  ack: #AB12CD34   这条我看过了",        # 缩进 / `#` 前缀 / 大写
])
def test_parse_acks_tolerates_sloppy_formatting(line):
    got = L.parse_acks("## 我的批注\n\n{}\n".format(line))
    assert got == {"ab12cd34": "这条我看过了"}


def test_parse_acks_never_raises_and_ignores_garbage():
    """解析不出就当没 ack，绝不抛异常——报告的生成不能因为批注写歪了就整个失败。"""
    for bad in (None, "", 12345, "## 我的批注\n随便写点什么\n",
                "- ack:\n- ack: zzzzzzzz\n- ack: abc\n- ack: ab12cd345678"):
        assert isinstance(L.parse_acks(bad), dict)
    # 第 2 轮 N3：ID 不再校验"必须是 8 位十六进制"——陈旧/孤儿两节的 ID 是 claim 哈希与
    # citekey，写死校验会把它们的合法 ack 全判成噪音。打错的 ID 现在解析得出来，
    # 由 render_lint_report 的"N 条 ack 没匹配上"反馈接住（比静默丢弃有用）。
    assert L.parse_acks("- ack: zzzzzzzz 打错的 ID") == {"zzzzzzzz": "打错的 ID"}
    assert L.parse_acks("- ack: ab12cd34") == {"ab12cd34": ""}      # 说明可省
    # 同一 ID 写两次取最后一条（人改批注时通常是在下面追加一行新说法）
    assert L.parse_acks("- ack: ab12cd34 旧说法\n- ack: ab12cd34 新说法") == \
        {"ab12cd34": "新说法"}


def test_read_lint_acks_survives_a_missing_or_broken_file(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    assert L.read_lint_acks(d) == {}                       # 文件不存在
    (d / "_lint.md").write_text("---\n乱: [写坏的\n", encoding="utf-8")
    assert L.read_lint_acks(d) == {}                       # frontmatter 解析不出


def _tension(rel="conflict", ka="a2024X", kb="b2024Y", ta="甲句", tb="乙句"):
    return L.Verdict(pair=_pair_txt(ka, ta, kb, tb), relation=rel, note="某处张力")


def test_每条张力都带上稳定_id():
    v = _tension()
    text = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False)
    assert "`#{}`".format(v.pair.pid) in text


def test_acked_tension_is_folded_not_hidden():
    """**不要直接不显示**：ack 过的条目如果原文变了 ID 就变了、会重新展开，
    这个行为本身要在报告里说清。"""
    v = _tension()
    text = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False,
                   acks={v.pair.pid: "这条我看过了，是乙内部矛盾"})
    assert "<details>" in text and "</details>" in text
    assert "你已确认过**：这条我看过了，是乙内部矛盾" in text
    assert "1 条你此前已确认" in text
    assert "a2024X" in text                     # 折叠 ≠ 删除，原文仍在
    assert "ID 就跟着变，会重新展开" in text


def test_unacked_tension_stays_in_the_main_view():
    v = _tension()
    text = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False,
                   acks={"00000000": "别的条目"})
    assert "<summary>你此前已确认的" not in text
    assert "条你此前已确认" not in text
    assert "### ⚔️ 结论冲突（1 处）" in text


def test_all_acked_says_so_instead_of_pretending_nothing_was_found():
    v = _tension()
    text = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False,
                   acks={v.pair.pid: "看过了"})
    # 不能渲染成"本轮没有判定为张力的句对"——那是另一件事
    assert "本轮没有判定为张力的句对" not in text
    assert "都是你此前确认过的" in text


# ---------------------------------------------------------------------------
# 12. A1：按 section 结转（本轮最重要的一条）
#
# 首版每次**整块重建**生成区，四项互相踩踏：月度回填固定跑 `--skip-contradictions`，
# 人手动跑一次全量对撞的结果下个月被无声抹掉；人手动只看对撞，撤稿那节被清空。
# 而 `output/` 在 .gitignore 里，抹掉就是彻底不见——这比没有这项检查更危险，
# 它给人「有个月度机制在盯着」的错觉，盯没盯全看运气。
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 8, 1, 9, 0, 0)
_T1 = datetime(2026, 8, 17, 9, 0, 0)


def _hit_scan():
    return L.RetractionScan(n_papers=10, n_with_doi=8, n_resolved=8, hits=[
        L.RetractionHit(citekey="bad2024", doi="10.1/x", title="某某被撤稿的研究",
                        signal="openalex-flag")])


def _block(text, heading):
    for b in text.split("\n## "):
        if heading in b.splitlines()[0]:
            return b
    raise AssertionError("报告里没有 {} 这一节".format(heading))


def test_split_lint_sections_roundtrips_what_render_writes():
    body = _render(now=_T0)
    secs = L.split_lint_sections(body)
    assert set(secs) == set(L.LINT_SECTIONS)
    assert all(ran == "2026-08-01T09:00:00" for ran, _t in secs.values())
    assert "撤稿检查" in secs["retraction"][1]


@pytest.mark.parametrize("bad", [
    None, "", 12345, "没有任何标记的正文",
    "<!-- LINT-SECTION -->\n内容",                       # 缺 key
    "<!-- LINT-SECTION retraction -->\n没有 ran_at",      # 缺 ran_at
    "---\n写坏的 yaml: [\n---\n",
])
def test_split_lint_sections_never_raises(bad):
    """解析失败一律降级成"没有可结转的历史"，**绝不抛异常**——这份报告的生成不能
    因为上一版被人手改坏了就整个失败。"""
    assert isinstance(L.split_lint_sections(bad), dict)


def test_split_lint_sections_ignores_markers_in_the_user_zone():
    """用户在「我的批注」里粘一段旧报告留底是很自然的动作，不该被结转回生成区。"""
    body = ("<!-- LINT-SECTION retraction ran_at=2026-08-01T09:00:00 -->\n"
            "## ☣️ 撤稿检查\n\n真的那一节\n" + T.GEN_END +
            "\n<!-- LINT-SECTION coverage ran_at=2000-01-01T00:00:00 -->\n我粘进来的旧报告\n")
    assert set(L.split_lint_sections(body)) == {"retraction"}


def test_skipped_section_carries_forward_the_previous_result(tmp_path):
    prev = _render(retraction=_hit_scan(), now=_T0)
    cur = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                  previous=L.split_lint_sections(prev))
    block = _block(cur, "撤稿检查")
    assert "bad2024" in block                       # 上一轮的发现没被抹掉
    assert "16 天前" in block
    assert "2026-08-01T09:00:00" in block
    assert "不代表当前状态" in block
    assert "没有可结转的历史结果" not in block


def test_carry_forward_marks_the_section_with_the_original_timestamp(tmp_path):
    prev = _render(retraction=_hit_scan(), now=_T0)
    cur = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                  previous=L.split_lint_sections(prev))
    secs = L.split_lint_sections(cur)
    assert secs["retraction"][0] == "2026-08-01T09:00:00"    # 结转，不是本轮时间
    assert secs["coverage"][0] == "2026-08-17T09:00:00"      # 本轮真跑的用新时间


def test_carry_forward_banner_does_not_accumulate_over_rounds():
    """连续跳过 N 轮会在同一节堆叠 N 条横幅，每条写的天数还不一样，读起来像跑过 N 次。"""
    r1 = _render(retraction=_hit_scan(), now=_T0)
    r2 = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                 previous=L.split_lint_sections(r1))
    r3 = _render(retraction=L.RetractionScan(skipped=True),
                 now=datetime(2026, 9, 1, 9, 0, 0),
                 previous=L.split_lint_sections(r2))
    block = _block(r3, "撤稿检查")
    assert block.count("⏸") == 1
    assert "2026-08-01T09:00:00" in block      # 时间戳仍是最初真跑的那次
    assert "31 天前" in block
    assert "16 天前" not in block
    assert "bad2024" in block


def test_no_history_falls_back_to_the_never_ran_wording():
    text = _render(retraction=L.RetractionScan(skipped=True))
    block = _block(text, "撤稿检查")
    assert "本轮**未执行**" in block
    assert "没有可结转的历史结果" in block
    assert "✅" not in block


def test_a_broken_previous_section_degrades_to_never_ran_not_a_crash():
    text = _render(retraction=L.RetractionScan(skipped=True),
                   previous={"retraction": ("", "   ")})     # 解析出来是空的
    assert "没有可结转的历史结果" in _block(text, "撤稿检查")


def test_status_line_says_which_sections_are_carried_over():
    prev = _render(retraction=_hit_scan(), now=_T0)
    cur = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                  previous=L.split_lint_sections(prev))
    head = cur.split("<!-- LINT-SECTION", 1)[0]
    assert "撤稿 ⏸ 结转自 16 天前" in head
    assert "缺口 ✅ 本轮刚跑" in head


def test_section_markers_are_html_comments_so_obsidian_and_pandoc_ignore_them():
    body = _render(now=_T0)
    for line in body.splitlines():
        if "LINT-SECTION" in line:
            assert line.startswith("<!--") and line.endswith("-->")


def test_section_markers_do_not_break_the_sentinel_hash(tmp_path):
    """哈希覆盖的是整个生成块正文、section 标记在其内部——但"加了新注释行之后哨兵
    仍然正常"这件事必须有测试锁住。"""
    d = tmp_path / "topics"
    d.mkdir()
    path, status = L.write_lint_report(d, _render(now=_T0), L.LintCounts(retracted=0))
    assert status == "new"
    assert "LINT-SECTION" in path.read_text(encoding="utf-8")
    # 未手改 → 正常合并
    _p, status2 = L.write_lint_report(d, _render(now=_T1), L.LintCounts(retracted=0))
    assert status2 == "merged"
    # 手改生成块里的 section 标记行 → 仍然判 conflict、绝不覆盖
    txt = path.read_text(encoding="utf-8")
    path.write_text(txt.replace("<!-- LINT-SECTION coverage",
                                "<!-- LINT-SECTION coverage 我手改了这一行"), encoding="utf-8")
    _p, status3 = L.write_lint_report(d, _render(now=_T1), L.LintCounts(retracted=0))
    assert status3 == "conflict"
    assert "我手改了这一行" in path.read_text(encoding="utf-8")


def test_checks_ran_at_survives_the_frontmatter_roundtrip(tmp_path):
    """`_render_frontmatter` 对 dict 走 json.dumps，产出的 YAML 必须能被
    split_frontmatter 正确读回——不然 A1 的结转链在这一环断掉且无人知晓。"""
    from src.scholar.vault import split_frontmatter
    d = tmp_path / "topics"
    d.mkdir()
    path, _s = L.write_lint_report(d, _render(now=_T0), L.LintCounts(retracted=0),
                                   now=_T0)
    fm, _body = split_frontmatter(path.read_text(encoding="utf-8"))
    assert isinstance(fm, dict)
    assert fm["checks_ran_at"]["retraction"] == "2026-08-01T09:00:00"
    assert set(fm["checks_ran_at"]) == set(L.LINT_SECTIONS)


def test_counts_carry_forward_so_null_means_never_ran(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    L.write_lint_report(d, _render(retraction=_hit_scan(), now=_T0),
                        L.LintCounts(retracted=1, stale_claims=7), now=_T0)
    body2 = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                    previous=L.read_previous_lint(d))
    path, _s = L.write_lint_report(d, body2, L.LintCounts(stale_claims=3), now=_T1)
    from src.scholar.vault import split_frontmatter
    fm, _b = split_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["n_retracted"] == 1        # 本轮跳过 → 从上一版结转，不再被抹成 null
    assert fm["n_stale_claims"] == 3     # 本轮跑过 → 新值
    assert fm["n_orphans"] is None       # 真的从来没跑过 → 才是 null


def test_carry_forward_counts_is_a_pure_function_that_tolerates_garbage():
    c = L.LintCounts(retracted=None, stale_claims=2)
    assert L.carry_forward_counts(c, None).retracted is None
    assert L.carry_forward_counts(c, {"n_retracted": "两篇"}).retracted is None   # 非整数
    assert L.carry_forward_counts(c, {"n_retracted": True}).retracted is None    # bool 不算
    out = L.carry_forward_counts(c, {"n_retracted": 5, "n_stale_claims": 99})
    assert (out.retracted, out.stale_claims) == (5, 2)      # 本轮跑过的不被结转顶掉
    assert c.retracted is None                              # 入参不被就地改写


def test_read_previous_lint_returns_empty_when_there_is_nothing_to_read(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    assert L.read_previous_lint(d) == {}
    (d / "_lint.md").write_text("完全不是报告格式\n", encoding="utf-8")
    assert L.read_previous_lint(d) == {}


def test_narrow_run_no_longer_wipes_the_other_three_sections(tmp_path):
    """验收 agent 现场逮到的那一幕：手动只看对撞（`--offline --skip-stale
    --skip-coverage`）跑完，撤稿那节被清空，两篇撤稿论文在报告里一个字都没有。"""
    d = tmp_path / "topics"
    d.mkdir()
    full = _render(retraction=_hit_scan(), now=_T0)
    L.write_lint_report(d, full, L.LintCounts(retracted=1, stale_claims=4, orphans=9),
                        now=_T0)
    narrow = _render(retraction=L.RetractionScan(skipped=True), stale_skipped=True,
                     coverage_skipped=True, contradictions_skipped=False, now=_T1,
                     previous=L.read_previous_lint(d))
    assert "bad2024" in narrow
    path, _s = L.write_lint_report(d, narrow, L.LintCounts(contradictions=0), now=_T1)
    from src.scholar.vault import split_frontmatter
    fm, _b = split_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["n_retracted"] == 1 and fm["n_stale_claims"] == 4 and fm["n_orphans"] == 9


# ---------------------------------------------------------------------------
# 13. A5：对撞的触发钩子（提示，不是强制触发）
# ---------------------------------------------------------------------------

def test_contradiction_reminder_fires_only_past_the_threshold():
    now = datetime(2026, 8, 17)
    assert not L.contradiction_reminder({"contradictions": "2026-08-10T00:00:00"}, now, 45)
    msg = L.contradiction_reminder({"contradictions": "2026-06-01T00:00:00"}, now, 45)
    assert "77 天" in msg and "lint_notes.py" in msg


def test_contradiction_reminder_says_never_when_it_never_ran():
    msg = L.contradiction_reminder({}, datetime(2026, 8, 17), 45)
    assert "从未执行过" in msg
    assert L.contradiction_reminder({"contradictions": ""}, datetime(2026, 8, 17), 45)


def test_contradiction_reminder_tolerates_a_broken_timestamp():
    assert L.contradiction_reminder({"contradictions": "去年某天"},
                                    datetime(2026, 8, 17), 45) == ""


def test_report_carries_the_reminder_when_contradictions_went_stale():
    old = _render(contradictions_skipped=False, now=datetime(2026, 5, 1, 9, 0, 0))
    cur = _render(contradictions_skipped=True, now=_T1,
                  previous=L.split_lint_sections(old))
    assert "距上次跨文献对撞已" in cur
    # 本轮真跑过就不该提醒
    assert "距上次跨文献对撞已" not in _render(contradictions_skipped=False, now=_T1)


# ---------------------------------------------------------------------------
# 14. B2：撤稿命中优先于落盘冲突（CLI 退出码）
#
# 不联网、不调 LLM：check_retractions 被 monkeypatch 掉，三项纯计算全跳过。
# ---------------------------------------------------------------------------

def _cli_env(tmp_path):
    notes = tmp_path / "notes"
    (notes / "topics").mkdir(parents=True)
    (notes / "literature_index.json").write_text(
        '{"papers": [{"citekey": "bad2024", "doi": "10.1/x", "duplicate_of": null}]}',
        encoding="utf-8")
    env = tmp_path / "scholar_test.env"
    env.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n".format(notes), encoding="utf-8")
    return notes, env


def _tamper_lint_report(topics_dir):
    """把 `_lint.md` 弄成"生成块被手改"的状态，逼出 conflict。"""
    path, _s = L.write_lint_report(topics_dir, "旧正文", L.LintCounts())
    txt = path.read_text(encoding="utf-8")
    path.write_text(txt.replace("旧正文", "旧正文（我在生成块里手写了批注）"), encoding="utf-8")


def _run_cli(monkeypatch, env, extra, hits):
    import scripts.lint_notes as LN
    monkeypatch.setattr(L, "check_retractions", lambda index, **kw: L.RetractionScan(
        n_papers=1, n_with_doi=1, n_resolved=1, hits=list(hits)))
    monkeypatch.setattr(sys, "argv", ["lint_notes.py", "--config", str(env),
                                      "--skip-contradictions", "--skip-stale",
                                      "--skip-coverage"] + extra)
    return LN.main()


def test_cli_retraction_beats_a_write_conflict(tmp_path, monkeypatch, capsys):
    """B2：撤稿命中早在扫描阶段就算出来了、stdout 也已经打过 🚨 行。首版会先因为
    conflict 返回 2，下游 summarize_lint_run 于是只发一条"lint 未跑成"的普通通知——
    发现被静默降级。退出码只有一个，让发现赢。"""
    notes, env = _cli_env(tmp_path)
    _tamper_lint_report(notes / "topics")
    rc = _run_cli(monkeypatch, env, [], [L.RetractionHit(
        citekey="bad2024", doi="10.1/x", title="某某", signal="openalex-flag")])
    assert rc == 1
    cap = capsys.readouterr()
    # 冲突这件事不能因为退出码被 1 占用就丢掉——stdout 与 stderr 都要写
    assert "落盘冲突" in cap.out and "落盘冲突" in cap.err
    assert "🚨" in cap.out
    # 下游拿这份 stdout/stderr + 退出码 1 时，两件事都读得出来
    outcome = L.summarize_lint_run(cap.out, cap.err, rc)
    assert outcome.alert is True
    assert "bad2024" in outcome.detail
    assert "落盘冲突" in outcome.detail


def test_cli_conflict_without_retraction_still_exits_2(tmp_path, monkeypatch, capsys):
    notes, env = _cli_env(tmp_path)
    _tamper_lint_report(notes / "topics")
    assert _run_cli(monkeypatch, env, [], []) == 2
    assert "落盘冲突" in capsys.readouterr().err


def test_cli_clean_run_exits_0(tmp_path, monkeypatch, capsys):
    notes, env = _cli_env(tmp_path)
    assert _run_cli(monkeypatch, env, [], []) == 0


def test_cli_offline_narrow_run_does_not_wipe_the_retraction_section(tmp_path, monkeypatch):
    """端到端复现验收 agent 逮到的那一幕：先跑一次查到撤稿，再跑一次 `--offline`
    的窄跑——撤稿那节必须还在，frontmatter 的 n_retracted 也不能被抹成 null。"""
    from src.scholar.vault import split_frontmatter
    notes, env = _cli_env(tmp_path)
    assert _run_cli(monkeypatch, env, [], [L.RetractionHit(
        citekey="bad2024", doi="10.1/x", title="某某", signal="openalex-flag")]) == 1
    assert _run_cli(monkeypatch, env, ["--offline"], []) == 0
    text = (notes / "topics" / "_lint.md").read_text(encoding="utf-8")
    assert "bad2024" in text
    assert "不代表当前状态" in text
    fm, _b = split_frontmatter(text)
    assert fm["n_retracted"] == 1
    assert fm["checks_ran_at"]["retraction"]


def test_carry_forward_restores_the_heading_when_the_previous_text_lost_it():
    """上一版那一节被改得只剩正文（标题被删）也要能结转——补回标准标题，
    而不是让这一节在报告里变成一段无主的文字。"""
    text = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                   previous={"retraction": ("2026-08-01T09:00:00", "只剩正文，没有标题行")})
    block = _block(text, "撤稿检查")
    assert "只剩正文" in block and "16 天前" in block


def test_carry_forward_does_not_eat_a_legitimate_blockquote():
    """`_strip_carry_banner` 只该吃掉自己上一轮插的那条 ⏸ 横幅。上一版正文里本来就
    以引用块开头的一节（如用户在生成块前贴过口径说明）不能被顺手删掉。"""
    prev_body = "## ☣️ 撤稿检查\n\n> 这是上一版正文自带的引用块，不是横幅\n\n正文内容"
    text = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                   previous={"retraction": ("2026-08-01T09:00:00", prev_body)})
    block = _block(text, "撤稿检查")
    assert "不是横幅" in block and "正文内容" in block
    assert block.count("⏸") == 1


# ---------------------------------------------------------------------------
# 15. 第 2 轮：N3 / N6 / N7 / L4 / L11
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,want_id", [
    ("- ack: #ab12cd34 我看过了", "ab12cd34"),
    ("- ack: `ab12cd34` 我看过了", "ab12cd34"),          # N3：报告里 ID 就是反引号包着的
    ("- ack: `#ab12cd34` 我看过了", "ab12cd34"),
    ("1. ack: ab12cd34 有序列表", "ab12cd34"),
    ("2) ack: ab12cd34 有序列表另一种写法", "ab12cd34"),
    ("- ack：`#AB12cd34` 大小写混排 + 全角冒号", "ab12cd34"),
    ("- ack: lee2021Multiview 孤儿/陈旧那两节的 ID 是 citekey", "lee2021multiview"),
    ("- ack: `#lee2021Multiview` citekey 也可能被反引号包着", "lee2021multiview"),
])
def test_parse_acks_accepts_every_form_the_report_itself_prints(line, want_id):
    """N3：报告把 ID 渲染成 `` `#108b782d` ``，在 Obsidian 源码模式/Vim 里复制这一串,
    反引号大概率跟着走。失败静默 = 用户只会觉得"这功能不好使"。"""
    got = L.parse_acks("## 我的批注\n\n{}\n".format(line))
    assert list(got) == [want_id]


def test_parse_acks_still_ignores_lines_that_are_not_acks():
    """放宽 ID 字符集不等于放宽"这一行是不是 ack"。

    第 3 轮 A1：`- ack: 我确认过了` 这一条**从这里挪走了**。原来靠"ID 必须以 ASCII
    字母数字开头"挡住它，而库里真有 `куксенко2024Аналіз`、`周立基于深度学习的…`
    这种首字符就不是 ASCII 的 citekey——靠字符集分不开这两者。现在它会被解析出来并
    走"这条 ack 没匹配上"的反馈路径，见
    `test_ack_line_without_any_id_is_still_not_an_ack`。"""
    assert L.parse_acks("- 这条我看过了 ab12cd34") == {}
    assert L.parse_acks("- backtrack: ab12cd34 词根撞上") == {}
    assert L.parse_acks("- ack:") == {}


def test_ack_ids_are_matched_case_insensitively():
    """citekey 大小写混排（lee2021Multiview），用户手抄大概率抄错壳。"""
    assert L.parse_acks("- ack: LEE2021multiview x") == {"lee2021multiview": "x"}


def test_ids_in_text_finds_every_ackable_id_the_report_prints():
    txt = "#### ⚔️ 结论冲突｜`[@a]` ↔ `[@b]` `#ab12cd34`\n- `[@lee2021X]` `#lee2021X`\n"
    assert L.ids_in_text(txt) == ["ab12cd34", "lee2021x"]


def test_repeatedly_skipped_section_never_pretends_to_carry_a_never_ran_note():
    """N6：从未跑过的一节被反复跳过时，横幅说"以下是上一次运行的结果"、
    正文说"没有可结转的历史结果"，同一节自相矛盾。"""
    r1 = _render(retraction=L.RetractionScan(skipped=True), now=_T0)
    r2 = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                 previous=L.split_lint_sections(r1))
    block = _block(r2, "撤稿检查")
    assert "没有可结转的历史结果" in block
    assert "⏸ **本轮未执行**" not in block          # 不该套结转横幅
    assert "那次运行的结果" not in block


@pytest.mark.parametrize("tail", [
    "",                                                     # 重复的那份是最后一节
    "<!-- LINT-SECTION coverage ran_at=2026-08-03T09:00:00 -->\n## 🕳 覆盖缺口\n\n后面还有一节\n",
])
def test_duplicate_section_markers_keep_the_first_not_the_last(tail):
    """N7：后者胜会静默吃掉前一份的发现，与 validate_verdicts 的重复编号口径也不一致。

    两种位置都要测：重复的那一份在**末尾**时走的是循环外那次收尾写入，在**中间**时
    走的是循环里那次——只测一种，另一处的去重丢了也发现不了。"""
    body = ("<!-- LINT-SECTION retraction ran_at=2026-08-01T09:00:00 -->\n"
            "## ☣️ 撤稿检查\n\n第一份：查到了 bad2024\n"
            "<!-- LINT-SECTION retraction ran_at=2026-08-02T09:00:00 -->\n"
            "## ☣️ 撤稿检查\n\n第二份：什么都没有\n" + tail)
    secs = L.split_lint_sections(body)
    assert secs["retraction"][0] == "2026-08-01T09:00:00"
    assert "bad2024" in secs["retraction"][1]
    assert "第二份" not in secs["retraction"][1]


@pytest.mark.parametrize("raw,want", [
    ("2026-08", 2026 * 12 + 8),
    ("2026-08-10", 2026 * 12 + 8),          # 手动/批次桶格式，全库 199/2343 条是这种
    ("2026-08-np", 2026 * 12 + 8),
    ("2026-07-28-TFM", 2026 * 12 + 7),
    ("2026-08-npjDM-supplement", 2026 * 12 + 8),
    ("2026-08-17-Retraction-review", 2026 * 12 + 8),
])
def test_month_ord_accepts_the_real_bucket_forms_in_the_library(raw, want):
    """L4：`^(\\d{4})-(\\d{2})$` 不认这些形式 → None → 全进 settled 档，
    8 篇本月刚入库的论文被展示成"值得考虑开新页"。"""
    assert L._month_ord(raw) == want


@pytest.mark.parametrize("bad", ["", None, "2026", "2026-13", "2026-1", "去年",
                                 "2026/08", "2026-081", "20260810", "2026-00"])
def test_month_ord_still_rejects_what_is_not_a_month(bad):
    """放宽成前缀匹配不等于放宽成"随便什么开头是四位数字都算"。
    `2026-081` 尤其要挡住：那不是 8 月，是解析不出来的东西。"""
    assert L._month_ord(bad) is None


def test_settled_orphans_are_oldest_first_so_the_old_batch_is_reachable(tmp_path):
    """L4：年份倒序保证了最老的那批（walker2009Evaluation…）永远看不见，
    而"库里最久没人碰的老文献"恰恰更可能是真缺口。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]")
    idx = _index(_paper("old2009", deep=True, year=2009, month="2019-01"),
                 _paper("mid2021", deep=True, year=2021, month="2019-01"),
                 _paper("new2026", deep=True, year=2026, month="2019-01"))
    rep = L.coverage_report(d, idx, now_month="2026-08")
    assert [e["citekey"] for e in rep.orphans] == ["old2009", "mid2021", "new2026"]


def test_implausible_years_are_flagged_not_ranked_first(tmp_path):
    """L4：`kishore2045Quantifying` 的 year=2045 稳居榜首。只标不改——不动索引数据。

    B2(a)：这条测试原来两条断言都不判别——`orphans[0] == "walker2009"` 光靠年份正序
    就成立（2009 < 2045），而 `"元数据可疑" in text` 被同 fixture 里的 `noyear`
    （走 `isinstance` 分支，不受年份判据影响）满足了。把 `year_is_implausible` 的
    数值分支变异成 `return False`，测试照样通过。

    settled 档**天然**就把 2045 排在最后（年份正序），所以"沉底"这件事在这一档里
    锁不住判据本身；真正被数值分支决定的是**标注**，以及 recent 档的排序
    （见 `test_recent_bucket_also_sinks_the_implausible_year`）。断言按这个事实重写。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]")
    idx = _index(_paper("kishore2045", deep=True, year=2045, month="2019-01"),
                 _paper("walker2009", deep=True, year=2009, month="2019-01"),
                 _paper("noyear", deep=True, year=None, month="2019-01"))
    rep = L.coverage_report(d, idx, now_month="2026-08")
    cks = [e["citekey"] for e in rep.orphans]
    assert cks[0] == "walker2009"
    assert set(cks) == {"walker2009", "kishore2045", "noyear"}      # 只标不删
    assert rep.now_year == 2026
    # 判据本身：明年是正常的（预印本/在线优先出版），再往后只能是元数据错误
    assert L.year_is_implausible(2045, 2026) and L.year_is_implausible(None, 2026)
    assert not L.year_is_implausible(2027, 2026) and not L.year_is_implausible(2026, 2026)
    text = _render(coverage=rep)
    # 绑到**那一行**：整篇断"元数据可疑"会被 noyear 顺手满足
    line = [ln for ln in text.splitlines() if "[@kishore2045]" in ln][0]
    assert "元数据可疑" in line


def test_dynagraph_retraction_case_is_still_caught(tmp_path):
    """L11：P3 计划书写的验证方式是「对已知的 DynaGraph 撤稿案例回放」，
    而库内两条记录已于 2026-08-16 删除——现在没有测试把这个历史案例钉住。
    标题标记是**独立于 OpenAlex `is_retracted` 的第二信号**。"""
    title = ("RETRACTED ARTICLE: DynaGraph: Dynamic Graph Neural Networks at Scale "
             "for Clinical Prediction")
    assert L._RETRACTED_TITLE_RE.match(title)
    idx = _index(_paper("dyna2022", doi="10.1145/3534540.3534691", deep=True,
                        title="DynaGraph: Dynamic Graph Neural Networks at Scale"))
    calls = []

    def handler(params):
        calls.append(params)
        return _Resp({"results": [
            {"id": "https://openalex.org/W1", "doi": "10.1145/3534540.3534691",
             # OpenAlex 侧没打撤稿旗标（已知覆盖缺口）——只有标题改了
             "is_retracted": False, "display_name": title}]})
    scan = L.check_retractions(idx, client=_Client(handler))
    assert [h.citekey for h in scan.hits] == ["dyna2022"]
    assert scan.hits[0].signal == "title-marker"


# ---------------------------------------------------------------------------
# 16. 第 2 轮核心三连：ack 从 vault 也能读（L1）/ 对结转文本也生效（L2）/
#     扩到每月真被读到的那两节（L3）
# ---------------------------------------------------------------------------

def test_status_line_never_calls_a_carried_section_freshly_run():
    """N1：`_status_line` 用 `days == 0` 判"本轮刚跑"。全跑发现 0 篇撤稿 → 同日
    `--offline` 窄跑 → 状态行说"撤稿 ✅ 本轮刚跑"，读者据此认为今天联网查过了。
    同一份报告往下翻 12 行写着 `⏸ 本轮未执行（--offline）`。**排在最前面、最可能被
    扫一眼就走的那处是错的**。"""
    same_day = datetime(2026, 8, 17, 18, 0, 0)
    prev = _render(retraction=_hit_scan(), now=_T1)          # _T1 是同一天早上
    cur = _render(retraction=L.RetractionScan(skipped=True), now=same_day,
                  previous=L.split_lint_sections(prev))
    head = cur.split("<!-- LINT-SECTION", 1)[0]
    assert "撤稿 ✅" not in head
    assert "撤稿 ⏸" in head
    assert "今天早些时候" in head          # 0 天不能写成"结转自 0 天前"，更不能写成 ✅


def test_status_line_carries_an_absolute_date_for_every_item():
    """L7：报告是磁盘上的静态文件，用户可能 60 天后才打开；月度 launchd 挂掉时
    （本仓库有前科）文件根本不更新，"✅ 本轮刚跑"会一直挂着。每项都要带绝对日期。"""
    head = _render(now=_T1).split("<!-- LINT-SECTION", 1)[0]
    assert head.count("2026-08-17") >= 4       # 四项各带一个绝对日期


def test_future_timestamp_is_not_squashed_into_zero_days():
    """N1 附带：`_days_since` 的 `max(0, ...)` 把未来时间戳压成 0 天，
    状态行同样写成 ✅（畸形输入第 3 例实测）。"""
    now = datetime(2026, 8, 17, 9, 0, 0)
    assert L._days_since("2026-09-17T09:00:00", now) == -31
    prev_body = "## ☣️ 撤稿检查\n\n上一轮的结果"
    text = _render(retraction=L.RetractionScan(skipped=True), now=now,
                   previous={"retraction": ("2026-09-17T09:00:00", prev_body)})
    head = text.split("<!-- LINT-SECTION", 1)[0]
    assert "撤稿 ✅" not in head
    assert "未来" in head


def test_status_line_carries_the_counts_and_a_must_do_line():
    """L6：顶部四段元信息没有一行说"本轮发现了什么"，第一处真发现在生成块第 16 行。
    30 秒内用户知道的是"哪几节是结转的"，不是"我现在要做什么"。"""
    text = _render(retraction=_hit_scan(), now=_T1,
                   counts=L.LintCounts(retracted=1, stale_claims=39, orphans=208))
    head = text.split("<!-- LINT-SECTION", 1)[0]
    assert "本轮必须处理" in head and "撤稿 1 篇" in head
    assert "39 条" in head and "208 篇" in head


def test_must_do_line_says_none_when_there_is_no_hard_signal():
    head = _render(now=_T1, counts=L.LintCounts(retracted=0, stale_claims=39)
                   ).split("<!-- LINT-SECTION", 1)[0]
    assert "**本轮必须处理**：无" in head


def test_must_do_line_does_not_forget_a_carried_retraction():
    """撤稿是"必须当天处理"的那一档：本轮没复查不等于它已经被处理掉了。"""
    prev = _render(retraction=_hit_scan(), now=_T0)
    cur = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                  previous=L.split_lint_sections(prev),
                  previous_counts={"n_retracted": 1})
    head = cur.split("<!-- LINT-SECTION", 1)[0]
    assert "本轮必须处理" in head and "撤稿 1 篇" in head and "本轮未复查" in head


# ---- L1：ack 在用户真正读报告的地方（vault）必须有效 ----------------------

def _lint_file(d, ack_lines):
    d.mkdir(parents=True, exist_ok=True)
    L.write_lint_report(d, _render(now=_T0), L.LintCounts())
    p = d / "_lint.md"
    txt = p.read_text(encoding="utf-8")
    p.write_text(txt.rstrip("\n") + "\n" + "\n".join(ack_lines) + "\n", encoding="utf-8")
    return p


def test_read_lint_acks_unions_the_vault_copy(tmp_path):
    """L1/N2：`sync_topics_to_vault` 把报告同步进 vault，`merge_topic_page` 给 vault
    副本**自己独立的一份**「我的批注」区，而报告在那里逐字教用户写 ack——
    写什么都没用，无提示无警告。人读东西的地方是 Obsidian，这是主路径不是边角。"""
    notes = tmp_path / "topics"
    vault = tmp_path / "vault" / "02-主题"
    _lint_file(notes, ["- ack: aaaaaaaa 札记库那份"])
    _lint_file(vault, ["- ack: bbbbbbbb vault 那份"])
    assert L.read_lint_acks(notes) == {"aaaaaaaa": "札记库那份"}
    got = L.read_lint_acks(notes, extra_dirs=[vault])
    assert got == {"aaaaaaaa": "札记库那份", "bbbbbbbb": "vault 那份"}


def test_read_lint_acks_never_raises_on_a_bad_extra_dir(tmp_path):
    notes = tmp_path / "topics"
    _lint_file(notes, ["- ack: aaaaaaaa x"])
    for bad in (tmp_path / "不存在", None, tmp_path / "topics" / "_lint.md"):
        assert L.read_lint_acks(notes, extra_dirs=[bad]) == {"aaaaaaaa": "x"}


def test_notes_copy_wins_when_the_same_id_is_acked_in_both(tmp_path):
    notes = tmp_path / "topics"
    vault = tmp_path / "vault"
    _lint_file(notes, ["- ack: aaaaaaaa 权威产物这份"])
    _lint_file(vault, ["- ack: aaaaaaaa vault 那份"])
    assert L.read_lint_acks(notes, extra_dirs=[vault]) == {"aaaaaaaa": "权威产物这份"}


def test_report_spells_out_the_absolute_paths_where_ack_works():
    """报告不能只说"在下方「我的批注」里写"——用户读的那一份是 vault 副本。"""
    v = _tension()
    text = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False,
                   ack_files=["/n/topics/_lint.md", "/v/02-主题/_lint.md"])
    assert "/n/topics/_lint.md" in text and "/v/02-主题/_lint.md" in text


def test_unmatched_acks_are_reported_instead_of_silently_dropped():
    """ID 打错、或原文变了导致 ID 变了，此前**没有任何反馈**——用户只会看到
    "我 ack 过的东西又出现了"，无从判断是打错了、内容变了、还是写错了文件。"""
    v = _tension()
    text = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False,
                   acks={v.pair.pid: "对上了", "deadbeef": "打错的", "zzzz1234": "另一条"})
    head = text.split("<!-- LINT-SECTION", 1)[0]
    assert "2 条 ack 没匹配上" in head
    assert "deadbeef" in head and "zzzz1234" in head
    assert v.pair.pid not in head          # 对上的那条不该出现在"没匹配上"名单里


def test_no_unmatched_notice_when_every_ack_landed():
    v = _tension()
    text = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False,
                   acks={v.pair.pid: "对上了"})
    assert "条 ack 没匹配上" not in text


# ---- L2：ack 对结转文本也要生效 -------------------------------------------

def test_ack_folds_a_carried_tension_without_rerunning_the_llm():
    """L2/N4：验收 agent 做了 13 轮模拟——M0 全跑后写 ack，之后 12 轮
    `--skip-contradictions`（backfill_notes.py 写死的形状），每轮 `'你此前已确认' in text`
    都是 False，那条张力完整展开了一整年。根因：结转的是渲染好的 markdown，
    而 ack 只作用在 fresh 分支——唯一能被 ack 的那一节恰好是唯一会被结转的那一节。"""
    v = _tension()
    m0 = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False, now=_T0)
    assert "<details>" not in m0
    text = m0
    for i in range(1, 13):                                  # 12 个月的自动化节奏
        text = _render(contradictions_skipped=True, now=_T1,
                       previous=L.split_lint_sections(text),
                       acks={v.pair.pid: "这条我核对过，不是问题"})
        block = _block(text, "跨文献对撞")
        assert "你已确认过" in block, "第 {} 轮又展开了".format(i)
        assert "<details>" in block
        assert block.count("<details>") == 1, "第 {} 轮折叠块堆叠了".format(i)
        assert "a2024X" in block                            # 折叠 ≠ 删除


def test_fold_acked_blocks_is_a_pure_idempotent_function():
    txt = ("## ⚔️ 跨文献对撞\n\n口径说明\n\n"
           "### ⚔️ 结论冲突（2 处）\n\n"
           "#### ⚔️ 结论冲突｜`[@a]` ↔ `[@b]` `#aaaaaaaa`\n\n甲乙原文一\n\n"
           "#### ⚔️ 结论冲突｜`[@c]` ↔ `[@d]` `#bbbbbbbb`\n\n甲乙原文二\n")
    once, n = L.fold_acked_blocks(txt, {"aaaaaaaa": "看过了"})
    assert n == 1
    assert "甲乙原文一" in once and "甲乙原文二" in once
    assert once.index("甲乙原文二") < once.index("甲乙原文一")   # 折叠的挪到末尾
    assert "结论冲突（1 处）" in once                            # 分组计数跟着改
    twice, n2 = L.fold_acked_blocks(once, {"aaaaaaaa": "看过了"})
    assert twice == once and n2 == 1                          # 幂等


def test_fold_drops_a_group_heading_that_became_empty():
    txt = ("## ⚔️ 跨文献对撞\n\n### 🔁 单篇内部自相矛盾（1 处）\n\n"
           "#### 🔁 单篇内部自相矛盾｜`[@a]` 自己的陈述前后不一致 `#aaaaaaaa`\n\n正文\n")
    out, n = L.fold_acked_blocks(txt, {"aaaaaaaa": "看过了"})
    assert n == 1
    assert "### 🔁 单篇内部自相矛盾（" not in out       # 组里一条不剩，标题不该留着
    assert "正文" in out


def test_fold_leaves_blocks_that_are_already_inside_another_details():
    """L8 把方法学分歧折进 `<details>`——那里面的块本来就没在主视野，不该再搬一次。"""
    txt = ("## ⚔️ 跨文献对撞\n\n<details><summary>3 处写作素材</summary>\n\n"
           "#### 🔀 方法学分歧｜`[@a]` ↔ `[@b]` `#aaaaaaaa`\n\n正文\n\n</details>\n")
    out, n = L.fold_acked_blocks(txt, {"aaaaaaaa": "看过了"})
    assert n == 0
    assert out.count("<details>") == 1


@pytest.mark.parametrize("bad", [None, "", 12345, "## 只有标题", "#### 没有 ID 的块\n正文"])
def test_fold_acked_blocks_never_raises(bad):
    out, n = L.fold_acked_blocks(bad, {"aaaaaaaa": "x"})
    assert isinstance(out, str) and n == 0


def test_carried_retraction_banner_flags_the_page_list_as_a_snapshot():
    """N8：结转来的「已被概念页引用：X、Y」是**上一轮的页面状态**，而这恰恰是最容易
    被当成当前事实去行动的一句（用户会照着去重跑那几页）。"""
    prev = _render(retraction=_hit_scan(), now=_T0,
                   cited_by_page={"mnar-diagnosis": ["bad2024"]})
    cur = _render(retraction=L.RetractionScan(skipped=True), now=_T1,
                  previous=L.split_lint_sections(prev))
    block = _block(cur, "撤稿检查")
    assert "概念页" in block and "快照" in block


# ---- L3：陈旧 / 孤儿两节的 ack 与 delta --------------------------------------

def _stale(slug="p1", text="某条论断", line=10, keys=("old2021",), newest=2021):
    return L.StaleClaim(
        claim=L.PageClaim(slug=slug, heading="小节", text=text,
                          citekeys=list(keys), line=line),
        newest_year=newest, years={k: newest for k in keys})


def test_stale_claim_id_is_stable_and_content_sensitive():
    a, b = _stale(), _stale()
    assert a.sid == b.sid and len(a.sid) == 8
    assert _stale(text="某条论断（改了一个字）").sid != a.sid
    assert _stale(slug="p2").sid != a.sid          # 换页要重问（同 pid 的口径）
    assert _stale(line=99).sid == a.sid            # 行号变了不算内容变了


def test_stale_section_can_be_acked():
    s = _stale()
    plain = _render(stale_claims=[s])
    assert "`#{}`".format(s.sid) in plain
    folded = _render(stale_claims=[s], acks={s.sid: "这条我确认过还算数"})
    block = _block(folded, "证据基础可能过时")
    assert "<details>" in block and "这条我确认过还算数" in block
    assert "某条论断" in block                      # 折叠 ≠ 删除


def test_orphans_can_be_acked_with_the_citekey_itself():
    """L3：孤儿论文的 ID 直接用 citekey——它本身就是稳定 ID，不用再哈希一层。"""
    cov = L.CoverageReport(n_pages=1, n_orphans_total=1, n_orphans_settled=1,
                           orphans=[{"citekey": "walker2009", "year": 2009, "title": "老论文"}],
                           thin_pages=[("p", 60, 60)], now_year=2026)
    plain = _render(coverage=cov)
    assert "`#walker2009`" in plain
    folded = _render(coverage=cov, acks={"walker2009": "这篇不打算开新页"})
    block = _block(folded, "覆盖缺口")
    assert "<details>" in block and "这篇不打算开新页" in block


def _cov(*cks):
    return L.CoverageReport(
        n_pages=1, n_orphans_total=len(cks), n_orphans_settled=len(cks),
        orphans=[{"citekey": c, "year": 2020, "title": "某篇"} for c in cks],
        thin_pages=[("p", 60, 60)], now_year=2026)


def test_stale_and_coverage_sections_carry_a_delta_line():
    """L3：39 条陈旧 + 25 篇孤儿逐字不变地每月重印 12 次，用户既不能消掉、
    也看不出哪条是新的。

    B2(b)：原 fixture 是 1 新增 / 1 相同 / 1 消失的**对称**局面，把 `_fmt_delta` 里
    「本轮新增」与「已消失」两个数对调，测试照样通过。现在两节各走一个**非对称**方向
    ——陈旧节 2 新增 / 1 相同 / 0 消失，缺口节 0 新增 / 1 相同 / 2 消失——
    任意两个数对调都会红。"""
    old = _render(stale_claims=[_stale(text="两轮都在")],
                  coverage=_cov("same2020", "gone1", "gone2"), now=_T0)
    cur = _render(stale_claims=[_stale(text="两轮都在"), _stale(text="新来的一条"),
                                _stale(text="又新来一条")],
                  coverage=_cov("same2020"),
                  now=_T1, previous=L.split_lint_sections(old))
    st = _block(cur, "证据基础可能过时")
    assert "本轮新增 **2**" in st and "与上轮相同 1" in st and "已消失 0" in st
    cv = _block(cur, "覆盖缺口")
    assert "本轮新增 **0**" in cv and "与上轮相同 1" in cv and "已消失 2" in cv


def test_delta_says_so_instead_of_crying_all_new_on_the_first_round():
    """上一版是旧格式（没有 ID）时不能报"全部新增"——那是格式迁移，不是发现。"""
    cur = _render(stale_claims=[_stale()], now=_T1,
                  previous={"stale": ("2026-08-01T09:00:00", "## ⏳ 旧格式\n\n- 没有 ID 的老正文")})
    st = _block(cur, "证据基础可能过时")
    assert "没有可比对的 ID" in st
    assert "本轮新增" not in st


# ---- L5：陈旧论断按 citekey 聚合 -------------------------------------------

def test_stale_section_aggregates_by_the_paper_that_props_them_up():
    """L5：39 条背后只有 24 篇老论文，`lee2021Multiview` 一篇撑 4 条。
    可执行单位是那 24 篇（"去补一轮新文献"，一个下午能做完），不是 39 条。"""
    # 故意让"按页分组"与"按支撑文献分组"给出**不同**的分组：
    # p1 上两条分别由两篇不同的老论文撑着，而 lee2021 同时撑着 p1 与 p2 上各一条。
    claims = [
        _stale(slug="p1", text="论断A", line=11, keys=("lee2021Multiview",)),
        _stale(slug="p1", text="论断B", line=12, keys=("che2018Recurrent",), newest=2018),
        _stale(slug="p2", text="论断C", line=13, keys=("lee2021Multiview",)),
    ]
    text = _render(stale_claims=claims)
    block = _block(text, "证据基础可能过时")
    assert "2 篇老文献" in block and "3 条论断" in block and "跨 2 页" in block
    # 分组标题是**支撑文献**，不是概念页
    assert "### `[@lee2021Multiview]`（2021 年 · 撑着 2 条论断）" in block
    assert "### `[@che2018Recurrent]`（2018 年 · 撑着 1 条论断）" in block
    assert "### `[@p1]`" not in block and "### [[p1]]" not in block
    # 撑得最多的排最前
    assert block.index("lee2021Multiview") < block.index("che2018Recurrent")
    assert "p1.md:11" in block and "p2.md:13" in block    # 页 + 行号仍点得回去


def test_stale_claim_is_attributed_to_its_newest_supporting_paper():
    """判据是"最新的一篇"，所以要补的也正是那一篇——不能把它挂到 1990 年的经典方法论文下。"""
    s = L.StaleClaim(claim=L.PageClaim(slug="p", heading="h", text="t",
                                       citekeys=["classic1990", "newest2020"], line=3),
                     newest_year=2020, years={"classic1990": 1990, "newest2020": 2020})
    assert s.anchor == "newest2020"


# ---- L8：方法学分歧不是待办 -------------------------------------------------

def test_method_divergence_is_folded_out_of_the_main_view():
    """L8：真实报告 13 条可报张力里 11 条是「方法学分歧」，清一色"两篇的阈值不同，
    指标不可直接比较"——不是可修的缺陷，是文献的**永久属性**，没有任何操作能让它
    下一轮消失。它们挤占了 ⚔️ 节的全部主视野。"""
    md = _tension(rel="method-divergence", ka="m1", kb="m2", ta="甲阈值", tb="乙阈值")
    sl = _tension(rel="scope-limit", ka="s1", kb="s2", ta="甲范围", tb="乙范围")
    cf = _tension(rel="conflict", ka="c1", kb="c2")
    text = _render(verdicts=[md, sl, cf], candidates=[md.pair, sl.pair, cf.pair],
                   contradictions_skipped=False)
    block = _block(text, "跨文献对撞")
    assert "写作素材" in block and "不是待办" in block
    # 主视野只剩结论冲突；方法学分歧在 <details> 里、且排在它后面
    assert block.index("`[@c1]`") < block.index("<details>")
    assert block.index("<details>") < block.index("`[@m1]`")
    assert "`[@m1]`" in block and "`[@s1]`" in block        # 折叠 ≠ 删除


def test_conflict_and_self_inconsistency_stay_in_the_main_view():
    si = _tension(rel="self-inconsistency")
    text = _render(verdicts=[si], candidates=[si.pair], contradictions_skipped=False)
    block = _block(text, "跨文献对撞")
    assert "<details>" not in block


# ---- N5 / L9：各页证据厚度 ---------------------------------------------------

def test_retired_page_is_not_reported_as_a_missing_specs_argument(tmp_path):
    """N5：`cap = max_ev.get(slug)` → None → 表格写「未知（调用方没给 specs）」，
    而 specs 明明给了。退役页留在 topics/ 是既定行为（vault 侧有 retired: true 分支）。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "live", body="## 小节\n\n- 论断 [@x2024]", extra_fm="n_evidence: 60\n")
    _write_page(d, "dead", body="## 小节\n\n- 论断 [@x2024]", extra_fm="n_evidence: 2\n")
    rep = L.coverage_report(d, _index(_paper("x2024")), specs=[_spec("live", 60)])
    assert rep.retired_pages == ["dead"]
    text = _render(coverage=rep)
    assert "没给 `specs`" not in text
    assert "已从 `config/topics.yaml` 下线" in text
    # 「已核实全部饱和」的分母不能把退役页算进去
    assert "1 页" in text


def test_all_saturated_collapses_to_one_sentence_without_the_table():
    """L9：全饱和时仍渲染 8 行"我什么都告诉不了你"的表格 + 一段说明，每月重印 12 行纯噪音。"""
    cov = L.CoverageReport(n_pages=2, thin_pages=[("a", 60, 60), ("b", 70, 70)])
    text = _render(coverage=cov)
    assert "| 概念页 | 证据条数 |" not in text
    assert "全部触及各自 `max_evidence` 上限" in text
    assert "queries 写得不对" not in text


def test_partially_saturated_still_prints_the_table():
    cov = L.CoverageReport(n_pages=2, thin_pages=[("a", 3, 60), ("b", 60, 60)])
    text = _render(coverage=cov)
    assert "| 概念页 | 证据条数 |" in text
    assert "饱和（触及配置上限 60）" in text and "**薄**" in text


# ---------------------------------------------------------------------------
# 17. 端到端：ack × 结转 × vault 副本（不联网、不调 LLM，全在 tmp 目录）
# ---------------------------------------------------------------------------

def test_ack_survives_a_real_write_read_roundtrip(tmp_path):
    """函数级端到端：真的落盘 → 真的从用户区读回 → 真的对结转文本折叠。
    render 级单测证明不了「哨兵合并没把 ack 行吃掉」这一环。"""
    d = tmp_path / "topics"
    d.mkdir()
    v = _tension()
    m0 = _render(verdicts=[v], candidates=[v.pair], contradictions_skipped=False, now=_T0)
    path, _s = L.write_lint_report(d, m0, L.LintCounts(contradictions=1), now=_T0)
    # 用户在「我的批注」区写一行 ack（生成块一个字都没碰 → 不该判 conflict）
    txt = path.read_text(encoding="utf-8")
    path.write_text(txt.rstrip("\n") + "\n- ack: `#{}` 这条我核对过\n".format(v.pair.pid),
                    encoding="utf-8")
    acks = L.read_lint_acks(d)
    assert acks == {v.pair.pid: "这条我核对过"}
    # 下一轮走月度自动化的形状：对撞跳过 → 结转 → 折叠
    body = _render(contradictions_skipped=True, now=_T1,
                   previous=L.read_previous_lint(d), acks=acks)
    _p, status = L.write_lint_report(d, body, L.LintCounts(), now=_T1)
    assert status == "merged"                       # 不是 conflict
    final = path.read_text(encoding="utf-8")
    assert "你已确认过" in final and "这条我核对过" in final
    assert "- ack: `#{}` 这条我核对过".format(v.pair.pid) in final   # 批注没被覆盖


def _cli_env_with_a_stale_page(tmp_path):
    """CLI 级夹具：一页概念页 + 一条 2018 年的论断 + 一个孤儿，全部落在 tmp 下。"""
    notes = tmp_path / "notes"
    topics = notes / "topics"
    topics.mkdir(parents=True)
    _write_page(topics, "demo", body="## 主要发现\n\n- 一条老论断 [@old2018Paper]",
                extra_fm="n_evidence: 60\n")
    papers = [{"citekey": "old2018Paper", "doi": None, "duplicate_of": None, "year": 2018,
               "has_full_text_reading": True, "priority_tier": "high", "month": "2018-03",
               "title": "老论文"},
              {"citekey": "orphan2015Paper", "doi": None, "duplicate_of": None, "year": 2015,
               "has_full_text_reading": True, "priority_tier": "high", "month": "2015-01",
               "title": "没人引的老论文"}]
    (notes / "literature_index.json").write_text(
        json.dumps({"papers": papers}, ensure_ascii=False), encoding="utf-8")
    env = tmp_path / "scholar_test.env"
    env.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n".format(notes), encoding="utf-8")
    return notes, topics, env


def _run_lint_cli(monkeypatch, env, extra):
    import scripts.lint_notes as LN
    monkeypatch.setattr(sys, "argv", ["lint_notes.py", "--config", str(env),
                                      "--offline", "--skip-contradictions"] + extra)
    return LN.main()


def test_cli_ack_written_in_the_vault_copy_actually_works(tmp_path, monkeypatch, capsys):
    """L1/N2 端到端：用户在 Obsidian 里那份副本的批注区写 ack——那是他真正读报告的
    地方，报告也逐字教他这么写——此前 `read_lint_acks` 根本不看那份文件。"""
    import json as _json
    from src.scholar.vault import split_frontmatter
    notes, topics, env = _cli_env_with_a_stale_page(tmp_path)
    vault = tmp_path / "vault"
    vtopics = vault / T.VAULT_TOPICS_DIR
    vtopics.mkdir(parents=True)

    assert _run_lint_cli(monkeypatch, env, ["--vault-dir", str(vault)]) == 0
    report = (topics / "_lint.md").read_text(encoding="utf-8")
    claims = L.find_stale_claims(topics, _json.loads(
        (notes / "literature_index.json").read_text(encoding="utf-8")), now_year=2026)
    assert len(claims) == 1
    sid = claims[0].sid
    assert "`#{}`".format(sid) in report
    # 指引里写死了两份文件的绝对路径（不做这一步用户不知道该往哪写）
    assert str(topics / "_lint.md") in report and str(vtopics / "_lint.md") in report

    # 模拟 sync_topics_to_vault：vault 侧是独立一份、有自己的批注区
    vreport = vtopics / "_lint.md"
    vreport.write_text(report.rstrip("\n") + "\n- ack: `#{}` 这条我在 Obsidian 里确认过\n"
                       .format(sid), encoding="utf-8")

    assert _run_lint_cli(monkeypatch, env, ["--vault-dir", str(vault)]) == 0
    again = (topics / "_lint.md").read_text(encoding="utf-8")
    assert "这条我在 Obsidian 里确认过" in again
    assert "<details>" in _block(again, "证据基础可能过时")
    fm, _b = split_frontmatter(again)
    assert fm["n_stale_claims"] == 1          # 折叠 ≠ 不再计数


def test_cli_without_a_vault_dir_says_nothing_and_still_works(tmp_path, monkeypatch, capsys):
    notes, topics, env = _cli_env_with_a_stale_page(tmp_path)
    assert _run_lint_cli(monkeypatch, env, []) == 0
    report = (topics / "_lint.md").read_text(encoding="utf-8")
    assert str(topics / "_lint.md") in report
    assert "02-主题" not in report            # 没探测到 vault 就不该凭空点名一个路径


def test_cli_orphan_ack_uses_the_citekey(tmp_path, monkeypatch):
    """L3：孤儿那一节的 ID 直接是 citekey——用户看着报告就能抄，不用去哪儿查哈希。"""
    notes, topics, env = _cli_env_with_a_stale_page(tmp_path)
    assert _run_lint_cli(monkeypatch, env, []) == 0
    path = topics / "_lint.md"
    assert "`#orphan2015Paper`" in path.read_text(encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n") +
                    "\n- ack: orphan2015Paper 这篇不打算开新页\n", encoding="utf-8")
    assert _run_lint_cli(monkeypatch, env, []) == 0
    block = _block(path.read_text(encoding="utf-8"), "覆盖缺口")
    assert "这篇不打算开新页" in block and "<details>" in block


def test_cli_reports_an_ack_that_matched_nothing(tmp_path, monkeypatch):
    notes, topics, env = _cli_env_with_a_stale_page(tmp_path)
    assert _run_lint_cli(monkeypatch, env, []) == 0
    path = topics / "_lint.md"
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n") +
                    "\n- ack: deadbeef 打错的 ID\n", encoding="utf-8")
    assert _run_lint_cli(monkeypatch, env, []) == 0
    head = path.read_text(encoding="utf-8").split("<!-- LINT-SECTION", 1)[0]
    assert "1 条 ack 没匹配上" in head and "deadbeef" in head


def test_cli_status_line_shows_counts_and_dates(tmp_path, monkeypatch):
    notes, topics, env = _cli_env_with_a_stale_page(tmp_path)
    assert _run_lint_cli(monkeypatch, env, []) == 0
    head = (topics / "_lint.md").read_text(encoding="utf-8").split("<!-- LINT-SECTION", 1)[0]
    today = datetime.now().date().isoformat()
    assert "陈旧 ✅ 本轮刚跑（{} · 1 条）".format(today) in head
    assert "缺口 ✅ 本轮刚跑（{} · 1 篇）".format(today) in head
    assert "撤稿 ❓ 从未执行" in head            # --offline 且无历史
    assert "**本轮必须处理**：无" in head


def test_fold_does_not_swallow_a_following_details_into_the_previous_block():
    """真实报告的形状就是「主视野 #### 块」后面紧跟 L8 的 `<details>` 写作素材区。
    块边界若不在 `<details>` 处收口，折叠主视野那一条会把整个写作素材区一起搬走。"""
    txt = ("## ⚔️ 跨文献对撞\n\n"
           "### ⚔️ 结论冲突（1 处）\n\n"
           "#### ⚔️ 结论冲突｜`[@a]` ↔ `[@b]` `#aaaaaaaa`\n\n主视野正文\n\n"
           "> 📎 另有 1 处写作素材\n\n"
           "<details><summary>1 处写作素材</summary>\n\n"
           "### 🔀 方法学分歧（1 处）\n\n"
           "#### 🔀 方法学分歧｜`[@c]` ↔ `[@d]` `#cccccccc`\n\n素材正文\n\n"
           "</details>\n")
    out, n = L.fold_acked_blocks(txt, {"aaaaaaaa": "看过了"})
    assert n == 1
    assert "素材正文" in out
    # 写作素材区仍在它自己的 details 里，且排在"你此前已确认的"折叠区之前
    assert out.index("1 处写作素材</summary>") < out.index("你此前已确认的 1 条")
    assert out.index("素材正文") < out.index("主视野正文")
    assert out.count("<details>") == 2


def test_fold_still_works_on_a_block_that_comes_after_a_closed_details():
    txt = ("## ⚔️ 跨文献对撞\n\n"
           "<details><summary>前面的折叠区</summary>\n\n里面的东西\n\n</details>\n\n"
           "#### ⚔️ 结论冲突｜`[@a]` ↔ `[@b]` `#aaaaaaaa`\n\n后面的块\n")
    out, n = L.fold_acked_blocks(txt, {"aaaaaaaa": "看过了"})
    assert n == 1
    assert "你已确认过" in out and "后面的块" in out and "里面的东西" in out


def test_repeated_folding_does_not_accumulate_blank_lines():
    """13 轮端到端跑出来的：每轮 unwrap 只删标记行、留下它们周围的空行，
    12 个月后节末堆着 40 多个空行。报告长度必须收敛。"""
    txt = ("## ⚔️ 跨文献对撞\n\n口径\n\n"
           "#### ⚔️ 结论冲突｜`[@a]` ↔ `[@b]` `#aaaaaaaa`\n\n正文\n")
    cur = txt
    lens = []
    for _ in range(12):
        cur, _n = L.fold_acked_blocks(cur, {"aaaaaaaa": "看过了"})
        lens.append(len(cur))
    assert len(set(lens)) == 1, "折叠不收敛：{}".format(lens)
    assert "\n\n\n" not in cur


def test_the_writing_material_details_is_not_dragged_into_the_ack_fold():
    """13 轮端到端跑出来的：L8 那句「另有 N 处写作素材」是 `####` 块后面的一行散文，
    被当成该块的一部分，ack 折叠时连它一起搬进了"你此前已确认"里。"""
    cf = _tension(rel="conflict", ka="c1", kb="c2")
    md = _tension(rel="method-divergence", ka="m1", kb="m2", ta="甲2", tb="乙2")
    first = _render(verdicts=[cf, md], candidates=[cf.pair, md.pair],
                    contradictions_skipped=False, now=_T0)
    carried = _render(contradictions_skipped=True, now=_T1,
                      previous=L.split_lint_sections(first),
                      acks={cf.pair.pid: "看过了"})
    block = _block(carried, "跨文献对撞")
    fold = block.split("<summary>你此前已确认的", 1)[1]
    assert "`[@c1]`" in fold                    # 被 ack 的那条确实折进去了
    assert "写作素材" not in fold               # L8 的导语不该被顺手拖进来
    assert "`[@m1]`" not in fold                # 更不该把整个素材区搬走
    assert block.index("写作素材") < block.index("你此前已确认的")


def test_thirteen_rounds_of_the_real_monthly_rhythm_converge(tmp_path):
    """本轮的验收场景，用真实文件跑满：M0 全跑（1 条冲突 + 2 条写作素材）→ 用户 ack
    掉那条冲突 → 12 轮 `--skip-contradictions`（backfill_notes.py 写死的形状）。
    这个循环在开发中真的逮到两个缺陷（节末堆空行、L8 导语被拖进折叠区），
    所以它留在这里当回归。"""
    from datetime import timedelta
    d = tmp_path / "topics"
    d.mkdir()
    cf = _tension(rel="conflict", ka="c1", kb="c2")
    md = _tension(rel="method-divergence", ka="m1", kb="m2", ta="甲2", tb="乙2")
    sl = _tension(rel="scope-limit", ka="s1", kb="s2", ta="甲3", tb="乙3")
    vs = [cf, md, sl]
    m0 = _render(verdicts=vs, candidates=[v.pair for v in vs],
                 contradictions_skipped=False, now=_T0)
    path, _s = L.write_lint_report(d, m0, L.LintCounts(contradictions=3), now=_T0)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n")
                    + "\n- ack: `#{}` 我核对过\n".format(cf.pair.pid), encoding="utf-8")

    seen_lengths = []
    for i in range(1, 13):
        now = _T0 + timedelta(days=30 * i)
        body = _render(contradictions_skipped=True, stale_skipped=True,
                       coverage_skipped=True, now=now,
                       previous=L.read_previous_lint(d), acks=L.read_lint_acks(d),
                       previous_counts=L.read_previous_lint_counts(d))
        _p, status = L.write_lint_report(d, body, L.LintCounts(), now=now)
        assert status in ("merged", "unchanged"), "第 {} 轮 {}".format(i, status)
        block = _block(path.read_text(encoding="utf-8"), "跨文献对撞")
        assert block.count("⏸") == 1, "第 {} 轮横幅堆叠".format(i)
        assert block.count("<details>") == 2, "第 {} 轮折叠块堆叠".format(i)
        assert "你已确认过" in block, "第 {} 轮 ack 又失效了".format(i)
        assert "`[@m1]`" in block and "`[@s1]`" in block      # 写作素材没丢
        assert "\n\n\n" not in block, "第 {} 轮空行堆叠".format(i)
        seen_lengths.append(len(block))
    # 天数位数从 30 变到 360 会带来 1 个字符的抖动，除此之外必须完全收敛
    assert max(seen_lengths) - min(seen_lengths) <= 2, seen_lengths


def test_removing_the_ack_unfolds_the_block_again_and_stays_tidy():
    """ack 删掉了就该重新展开（那是"我改主意了"）；这条路径不走重折逻辑，
    所以拆折叠那一步自己也必须把空行收拾干净。"""
    txt = ("## ⚔️ 跨文献对撞\n\n口径\n\n"
           "#### ⚔️ 结论冲突｜`[@a]` ↔ `[@b]` `#aaaaaaaa`\n\n正文\n")
    folded, _n = L.fold_acked_blocks(txt, {"aaaaaaaa": "看过了"})
    back, n = L.fold_acked_blocks(folded, {})
    assert n == 0
    assert "<details>" not in back and "你已确认过" not in back
    assert "正文" in back and "`#aaaaaaaa`" in back
    assert "\n\n\n" not in back
    assert L.fold_acked_blocks(back, {})[0] == back        # 再拆一次不变


def test_all_pages_retired_is_not_reported_as_a_missing_specs_argument():
    """N5 的同一类错误在另一个分支上：`topics/` 里剩下的页**全部**已从 topics.yaml
    下线时 `known` 为空，报告又回到那句"本轮没拿到 specs"——而 specs 明明给了。
    （CLI 真跑一次就撞上了：拿真实 topics.yaml 配一个 slug 对不上的概念页。）"""
    cov = L.CoverageReport(n_pages=1, thin_pages=[("dead", 60, None)],
                           retired_pages=["dead"], n_orphans_total=1,
                           n_orphans_settled=1, now_year=2026,
                           orphans=[{"citekey": "o2020", "year": 2020, "title": "某篇"}])
    text = _render(coverage=cov)
    assert "没拿到" not in text and "没给 `specs`" not in text
    assert text.count("已从 `config/topics.yaml` 下线") >= 1
    assert "全部已从" in text


def test_no_specs_at_all_still_says_exactly_that():
    """反过来也要守住：真的没给 specs 时那句话必须还在（别为了修上一条把它删了）。"""
    cov = L.CoverageReport(n_pages=1, thin_pages=[("p", 60, None)], retired_pages=[],
                           n_orphans_total=1, n_orphans_settled=1, now_year=2026,
                           orphans=[{"citekey": "o2020", "year": 2020, "title": "某篇"}])
    text = _render(coverage=cov)
    assert "没给 `specs`" in text and "无从判断" in text
    assert "已从 `config/topics.yaml` 下线" not in text


# ---------------------------------------------------------------------------
# 18. 第 3 轮：A1~A4（交付阻塞）与 B1~B7
# ---------------------------------------------------------------------------

# ---- A1：ack 的 ID 用**定界**而不是**枚举字符集** ---------------------------
#
# 同一个 bug 家族的第三次，读这一段就够了：
#   第 1 轮：`\w` 按 Unicode 判定，汉字算单词字符 → 防幻觉检查整条漏过；
#   第 2 轮：`\b` 同一个毛病 → `撤稿声明` 漏判；
#   第 3 轮（这里）：为了躲开前两个坑改成**逐字符列 ASCII**，结果把库里 40 个合法的
#     非 ASCII citekey 挡在门外，报告还反过来诬告用户"你 ack 抄错了"。
# 结论：ID 的边界要靠**反引号/空白定界**，不靠枚举允许的字符。

_NON_ASCII_KEYS = [
    "mišić2021Simulationbased",                 # 拉丁扩展
    "wang2022Doppelgänger",                     # 变音符
    "куксенко2024Аналіз",                       # 西里尔，**首字符**就不是 ASCII
    "周立基于深度学习的不完整时序数据补全方法综述",     # 纯中文 citekey（库里真有）
    "anon2021野生动物疫病暴发成因及其防控对策",
]


@pytest.mark.parametrize("ck", _NON_ASCII_KEYS)
def test_non_ascii_citekeys_survive_the_ack_round_trip(ck):
    """库里 2343 个 citekey 里 40 个含非 ASCII 字符，其中 4 个**首字符**就不是 ASCII。
    枚举字符集的写法把它们全挡在门外：ID 被截成前缀（`mišić…` → `mi`），
    而报告顶部反过来说"你有 1 条 ack 没匹配上：`mi`"——用户什么都没做错。"""
    mark = L.ACK_ID_MARK.format(ck)
    assert L.ids_in_text("- `[@{}]` 某篇 {}".format(ck, mark)) == [ck.lower()]
    assert L.parse_acks("- ack: {} 与我的问题域无关".format(ck)) == \
        {ck.lower(): "与我的问题域无关"}
    assert L.parse_acks("- ack: {} 连反引号一起复制".format(mark)) == \
        {ck.lower(): "连反引号一起复制"}


def test_non_ascii_orphan_is_not_accused_of_being_a_typo():
    """A1 的第 1 个连锁后果：用户照抄报告里的 ID 写 ack，报告回他一句
    「⚠️ 本轮有 1 条 ack 没匹配上」。这比第 1 轮的"静默无效"更糟——静默无效不指责人。"""
    ck = "mišić2021Simulationbased"
    cov = L.CoverageReport(n_pages=1, n_orphans_total=1, n_orphans_settled=1,
                           orphans=[{"citekey": ck, "year": 2021, "title": "某篇"}],
                           thin_pages=[("p", 60, 60)], now_year=2026)
    plain = _render(coverage=cov)
    assert L.ACK_ID_MARK.format(ck) in plain
    acks = L.parse_acks("- ack: {} 与我的问题域无关".format(L.ACK_ID_MARK.format(ck)))
    folded = _render(coverage=cov, acks=acks)
    head = folded.split("<!-- LINT-SECTION", 1)[0]
    assert "没匹配上" not in head                     # ← 诬告
    block = _block(folded, "覆盖缺口")
    assert "<details>" in block and "与我的问题域无关" in block


def test_non_ascii_id_does_not_become_a_permanent_delta_ghost():
    """A1 的第 2 个连锁后果：`ids_in_text` 认不出它 → 它永远不在 `known`/`prev` 集合里
    → 每个月都被算成"本轮新增 1 篇"，12 轮 delta 一字不变地带着一个永久幽灵。"""
    ck = "mišić2021Simulationbased"
    cov = L.CoverageReport(n_pages=1, n_orphans_total=1, n_orphans_settled=1,
                           orphans=[{"citekey": ck, "year": 2021, "title": "某篇"}],
                           thin_pages=[("p", 60, 60)], now_year=2026)
    first = _render(coverage=cov, now=_T0)
    second = _render(coverage=cov, now=_T1, previous=L.split_lint_sections(first))
    cv = _block(second, "覆盖缺口")
    assert "本轮新增 **0**" in cv and "与上轮相同 1" in cv


def test_ack_line_without_any_id_is_still_not_an_ack():
    """定界不等于什么都收：`ack:` 后面空无一物仍然不是 ack。
    （注意 `- ack: 我确认过了` **现在会**解析成 ID=`我确认过了`——这是必须付的代价：
    库里真有 `周立基于深度学习的…` 这种纯中文 citekey，靠字符集分不开这两者。
    它会走"这条 ack 没匹配上"那条反馈路径，而不是被静默丢弃。）"""
    assert L.parse_acks("- ack:") == {}
    assert L.parse_acks("- ack:    ") == {}
    assert L.parse_acks("- ack: ``") == {}
    assert L.parse_acks("- 这条我看过了 ab12cd34") == {}
    assert L.parse_acks("- backtrack: ab12cd34 词根撞上") == {}


# ---- A2：ack 要让待办**往前走**，不是只让它不占版面 -------------------------

def _orphan_index(n, prefix="orph", **kw):
    return _index(*[_paper("{}{:03d}".format(prefix, i), deep=True, tier="high",
                           year=2000 + i, month="2019-01", **kw) for i in range(n)])


def test_acking_the_listed_orphans_advances_the_queue(tmp_path):
    """A2：截断发生在分 ack **之前**，所以 208 篇孤儿里你永远只能看见同样那 25 篇。
    验收 agent 把列出的 25 篇全 ack 掉再渲染，那个小标题**整个消失**，
    后面 143 篇一篇没顶上来，也没有一行告诉用户"队列里还有 143 篇"。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]")
    rep = L.coverage_report(d, _orphan_index(5), orphan_limit=2, now_month="2026-08")
    # 截断不再发生在这里：渲染侧才知道哪些已 ack
    assert len(rep.orphans) == 5 and rep.orphan_limit == 2

    first = _render(coverage=rep)
    listed = [e["citekey"] for e in rep.orphans[:2]]
    assert all(L.ACK_ID_MARK.format(k) in first for k in listed)
    assert "orph002" not in first                      # 第 3 篇本轮还排不上

    after = _render(coverage=rep, acks={k.lower(): "不打算开新页" for k in listed})
    cv = _block(after, "覆盖缺口")
    assert "值得考虑要不要开新页" in cv                 # ← 小标题不许整个消失
    assert L.ACK_ID_MARK.format("orph002") in cv       # 队列往前走了
    assert L.ACK_ID_MARK.format("orph003") in cv
    assert "orph004" not in cv                         # 但仍然只列 orphan_limit 篇
    assert "已确认 2" in cv and "待办 3" in cv          # 队列剩多少必须写出来
    assert "<details>" in cv and "不打算开新页" in cv   # 已确认的那半仍折在节末


def test_orphan_limit_zero_still_lists_everything(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]")
    rep = L.coverage_report(d, _orphan_index(4), orphan_limit=0, now_month="2026-08")
    text = _render(coverage=rep)
    assert all(L.ACK_ID_MARK.format("orph{:03d}".format(i)) in text for i in range(4))


def test_orphan_delta_compares_what_was_actually_listed(tmp_path):
    """列表变长后 delta 若拿**全量**去比对上一版**列出的** 25 条，
    第一轮就会报"本轮新增 183 篇"——那是截断口径变了，不是发现。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]")
    rep = L.coverage_report(d, _orphan_index(5), orphan_limit=2, now_month="2026-08")
    first = _render(coverage=rep, now=_T0)
    second = _render(coverage=rep, now=_T1, previous=L.split_lint_sections(first))
    cv = _block(second, "覆盖缺口")
    assert "本轮新增 **0**" in cv and "与上轮相同 2" in cv


# ---- A3：结转陈旧/孤儿两节时"只拆不折" --------------------------------------

def test_fold_leaves_a_section_without_h4_blocks_completely_alone():
    """A3：`_unwrap_ack_fold` 是**无条件**拆的，而陈旧/孤儿两节的条目是 `- ` 列表项，
    拆完根本重折不回去 —— 结转这两节 = **只拆不折**：折叠区没了、ack 说明没了，
    2010 年的论文被摊到「最近 3 个月新入库」那一档下面，小标题还写着"列前 1"。"""
    txt = ("## 🕳 覆盖缺口\n\n"
           "**值得考虑要不要开新页（2 篇，列前 1）**\n\n"
           "- `[@walker2009E]`（2009 · 入库 2019-01）最老的孤儿 `#walker2009E`\n\n"
           "**最近 3 个月新入库（1 篇，列前 1）**\n\n"
           "- `[@fresh2026]`（2026 · 入库 2026-08）新的 `#fresh2026`\n\n"
           "> ✔️ 其中 **1 条你此前已确认**，已折叠到本节末尾（两句原文只要变了 ID 就跟着变，"
           "会重新展开）。\n\n"
           "<details><summary>你此前已确认的 1 条</summary>\n\n"
           "> ✔️ **你已确认过**：这篇不打算开新页\n\n"
           "- `[@acked2010X]`（2010 · 入库 2019-02）某篇 `#acked2010X`\n\n"
           "</details>\n")
    out, n = L.fold_acked_blocks(txt, {"acked2010x": "这篇不打算开新页"})
    assert n == 0
    assert out == txt.strip("\n"), "没有 `#### ` 块的一节必须原样返回，连拆都不许拆"
    # 空 acks（用户删了 ack）同样不许拆——重折不回去就别拆
    assert L.fold_acked_blocks(txt, {})[0] == txt.strip("\n")


def test_carried_coverage_section_keeps_its_fold_and_its_grouping(tmp_path):
    """端到端复现审计 agent 那条 CLI 实测：第 2 轮 ack 生效 → 第 3 轮走
    `--offline --skip-stale --skip-coverage`（报告自己每 45 天推荐的补跑命令）结转，
    折叠区、ack 说明、以及"哪条属于哪一档"全都不能丢。"""
    cov = L.CoverageReport(
        n_pages=1, n_orphans_total=2, n_orphans_settled=1, n_orphans_recent=1,
        orphans=[{"citekey": "acked2010X", "year": 2010, "title": "老的"}],
        recent_orphans=[{"citekey": "fresh2026", "year": 2026, "title": "新的"}],
        thin_pages=[("p", 60, 60)], now_year=2026)
    acks = {"acked2010x": "这篇不打算开新页"}
    r2 = _render(coverage=cov, acks=acks, now=_T0)
    r3 = _render(coverage_skipped=True, acks=acks, now=_T1,
                 previous=L.split_lint_sections(r2))
    block = _block(r3, "覆盖缺口")
    assert "<details>" in block and "这篇不打算开新页" in block
    # 被 ack 的 2010 年论文绝不能被摊到「最近 3 个月新入库」那一档下面
    assert block.index("最近 3 个月新入库") < block.index("acked2010X")
    fold = block.split("<summary>你此前已确认的", 1)[1]
    assert "acked2010X" in fold


# ---- A4：SCHOLAR_VAULT_DIR 是环境级的，不是"用户对这次调用的明确动作" -------

def test_env_vault_dir_never_leaks_into_a_non_production_run(tmp_path, monkeypatch):
    """A4：环境变量绕过 `notes_dir_is_production` 守卫，单测会去读**生产 vault**。
    本机那份文件恰好还不存在所以只是路径泄漏；一旦 vault 同步过一次，
    生产 acks 就会去折叠 tmp 报告里的条目——测试结果取决于用户批注内容。
    这正是 `notes_dir_is_production` 那段注释记录的「单测意外打到生产」事故形状。"""
    notes, topics, env = _cli_env_with_a_stale_page(tmp_path)
    prod = tmp_path / "prod_vault"
    (prod / T.VAULT_TOPICS_DIR).mkdir(parents=True)
    monkeypatch.setenv("SCHOLAR_VAULT_DIR", str(prod))
    assert _run_lint_cli(monkeypatch, env, []) == 0
    report = (topics / "_lint.md").read_text(encoding="utf-8")
    assert str(prod) not in report
    assert T.VAULT_TOPICS_DIR not in report


def test_explicit_vault_dir_flag_still_wins_unconditionally(tmp_path, monkeypatch):
    """反过来也要守住：`--vault-dir` 是用户对**这次调用**的明确动作，照办。"""
    notes, topics, env = _cli_env_with_a_stale_page(tmp_path)
    vault = tmp_path / "vault"
    (vault / T.VAULT_TOPICS_DIR).mkdir(parents=True)
    assert _run_lint_cli(monkeypatch, env, ["--vault-dir", str(vault)]) == 0
    assert str(vault / T.VAULT_TOPICS_DIR) in (topics / "_lint.md").read_text(
        encoding="utf-8")


# ---- B1：拆折叠之后 `###` 计数必须重算 --------------------------------------

_B1_TXT = ("## ⚔️ 跨文献对撞\n\n"
           "### ⚔️ 结论冲突（1 处）\n\n"
           "#### ⚔️ 结论冲突｜`[@c1]` ↔ `[@c2]` `#cccccccc`\n\nC 正文\n\n"
           "### 🔁 单篇内部自相矛盾（1 处）\n\n"
           "#### 🔁 单篇内部自相矛盾｜`[@s1]` 自己 `#ssssssss`\n\nS 正文\n")


def test_unfolding_recomputes_the_group_heading_count():
    """B1：`if not acks: return` 与 `if not folded: return` 两处早退都跳过了
    `_H3_COUNT_RE` 重算。审计实测：`### ⚔️ 结论冲突（1 处）` 下面列了 3 条。

    （fixture 必须带 `###` 分组标题——现有那条回归的 fixture 没有，整类问题落在覆盖之外。）"""
    folded, n = L.fold_acked_blocks(_B1_TXT, {"ssssssss": "看过了"})
    assert n == 1
    assert "🔁 单篇内部自相矛盾（" not in folded.split("<details>")[0]   # 空组标题删掉
    back, n2 = L.fold_acked_blocks(folded, {})                          # 用户删了 ack
    assert n2 == 0
    head = back.split("### 🔁", 1)[0] if "### 🔁" in back else back
    # 摊回节末可以接受，但标题上那个数字必须诚实
    assert "结论冲突（1 处）" not in back, "拆完 2 条都挂在 ⚔️ 下面，标题还写着 1 处"
    assert "结论冲突（2 处）" in back
    assert "C 正文" in back and "S 正文" in back
    assert L.fold_acked_blocks(back, {})[0] == back                     # 仍然幂等


def test_group_heading_count_is_recomputed_even_with_no_acks_at_all():
    """`if not acks: return` 那一处早退：ack 全删光时同样要重算。"""
    folded, _n = L.fold_acked_blocks(_B1_TXT, {"ssssssss": "看过了"})
    back, _n2 = L.fold_acked_blocks(folded, None)
    assert "结论冲突（2 处）" in back


# ---- B3：陈旧那节的量是**日历驱动**的 ---------------------------------------

def _stale_at(slug, text, newest, key):
    return L.StaleClaim(
        claim=L.PageClaim(slug=slug, heading="小节", text=text, citekeys=[key], line=1),
        newest_year=newest, years={key: newest})


def test_stale_anchor_limit_collapses_the_tail():
    """B3：`_stale_section` 对锚文献数不设 cap，而阈值是 `now_year - 5`——
    2027-01-01 那天没有任何人做错任何事，这一节自己从 195 行长到 306 行。"""
    claims = [_stale_at("p", "论断 {}".format(i), 2021, "anchor{:02d}".format(i))
              for i in range(25)]
    text = _render(stale_claims=claims, stale_anchor_limit=5,
                   params={"now_year": 2026})
    block = _block(text, "证据基础可能过时")
    assert block.count("### `[@anchor") == 5
    assert "另有 **20 篇**" in block and "--stale-anchor-limit 0" in block
    # 未列出的那 20 篇仍然计入分母，不许静默消失
    assert "25 篇老文献是这 25 条论断的唯一地基" in block
    full = _render(stale_claims=claims, stale_anchor_limit=0, params={"now_year": 2026})
    assert _block(full, "证据基础可能过时").count("### `[@anchor") == 25


def test_stale_delta_separates_a_threshold_shift_from_a_real_change():
    """B3 之二：11 个月 delta 都是 0，然后每年 1 月放一次烟花——而那次"新增"不是发现，
    是时钟走了一格。报告必须自己说明这一点。"""
    old = [_stale_at("p", "老论断", 2020, "a2020")]
    # 阈值前移到 2021：2021 年那批**整批**掉进来，一条内容都没变
    new = old + [_stale_at("p", "新掉进来的 {}".format(i), 2021, "b{}".format(i))
                 for i in range(3)]
    r1 = _render(stale_claims=old, now=_T0, params={"now_year": 2026})
    r2 = _render(stale_claims=new, now=_T1, params={"now_year": 2026},
                 previous=L.split_lint_sections(r1))
    block = _block(r2, "证据基础可能过时")
    assert "本轮新增 **3**" in block
    assert "阈值前移" in block and "3 条" in block
    assert "不是内容变化" in block


def test_no_threshold_note_when_the_new_claims_are_not_on_the_boundary():
    """反面：新增的论断不在阈值边界年上时，那句话不该出现（否则它就成了噪音）。"""
    old = [_stale_at("p", "老论断", 2019, "a2019")]
    new = old + [_stale_at("p", "另一条老的", 2019, "b2019")]
    r1 = _render(stale_claims=old, now=_T0, params={"now_year": 2026})
    r2 = _render(stale_claims=new, now=_T1, params={"now_year": 2026},
                 previous=L.split_lint_sections(r1))
    assert "阈值前移" not in _block(r2, "证据基础可能过时")


# ---- B4：「元数据可疑沉底」在 recent 档里自己打自己脸 ------------------------

def test_recent_bucket_also_sinks_the_implausible_year(tmp_path):
    """B4：recent 档按年份**倒序**，而 `_orphan_lines` **不分档**地追加
    "本节排序已把它沉底"。验收 agent 渲染出的真实报告第 461 行：year=2045 那篇
    是「最近 3 个月新入库」那一档的**第 1 行**，紧跟着一句"已把它沉底"。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_page(d, "demo", body="## 小节\n\n- 论断 [@cited2024]")
    idx = _index(_paper("kishore2045", deep=True, year=2045, month="2026-08"),
                 _paper("normal2026", deep=True, year=2026, month="2026-08"),
                 _paper("older2024", deep=True, year=2024, month="2026-08"))
    rep = L.coverage_report(d, idx, now_month="2026-08")
    cks = [e["citekey"] for e in rep.recent_orphans]
    assert cks == ["normal2026", "older2024", "kishore2045"]
    text = _render(coverage=rep)
    line = [ln for ln in text.splitlines() if "[@kishore2045]" in ln][0]
    assert "元数据可疑" in line


# ---- B5：顶部的计数要能随我干活而变小 ---------------------------------------

def test_status_line_and_delta_show_the_remaining_todo_after_acks():
    """B5：M12 报告顶部 `陈旧 ✅ 本轮刚跑（… · 39 条）`、正文
    `22 篇老文献是这 36 条论断的唯一地基`——39/36 之间没有一句话解释差在哪
    （差的是 3 条已 ack，而说明它的那行在第 403 行）。"""
    a = _stale_at("p", "已确认的", 2021, "x2021")
    b = _stale_at("p", "还没看的", 2021, "y2021")
    text = _render(stale_claims=[a, b], acks={a.sid: "看过了"},
                   counts=L.LintCounts(stale_claims=2), params={"now_year": 2026})
    head = text.split("<!-- LINT-SECTION", 1)[0]
    assert "陈旧 ✅ 本轮刚跑" in head and "2 条 · 待办 1" in head
    assert "待办 1" in _block(text, "证据基础可能过时")


def test_status_line_omits_the_todo_clause_when_nothing_was_acked():
    text = _render(stale_claims=[_stale_at("p", "一条", 2021, "x2021")],
                   counts=L.LintCounts(stale_claims=1), params={"now_year": 2026})
    head = text.split("<!-- LINT-SECTION", 1)[0]
    assert "1 条）" in head and "待办" not in head


# ---- B6：陈旧那节说了可执行单位，却没说用什么工具执行 ------------------------

def test_stale_section_names_the_tool_and_the_concentration():
    """B6：孤儿那节给了命令（`notes_search.py`），陈旧那节说"去补一轮新文献"
    却没有任何命令，而仓库里现成有 `scripts/search_pubs.py` 能干这件事。
    真实分布是 4/4/3/3/2/2/2/2/2 + 15 个 singleton——**前 9 篇覆盖 62%**，
    报告应该直接把这句写出来，而不是让读者自己数。"""
    claims = ([_stale_at("p", "多的 {}".format(i), 2021, "hot") for i in range(4)]
              + [_stale_at("p", "少的 {}".format(i), 2021, "cold{}".format(i))
                 for i in range(2)])
    block = _block(_render(stale_claims=claims, params={"now_year": 2026}),
                   "证据基础可能过时")
    assert "scripts/search_pubs.py" in block
    # 撑着 ≥2 条的只有 hot 一篇，它一篇就覆盖 4/6 = 67%
    assert "1 篇" in block and "67%" in block


# ---- B7：两条小的 ------------------------------------------------------------

def test_fold_note_with_a_newline_does_not_grow_one_line_per_round():
    """B7：`_fold_note` 的说明含换行时每轮增长一行（167→178→189→200…）。
    `parse_acks` 逐行匹配所以生产上不可达，但"字节级幂等"的宣称就此不成立。"""
    txt = ("## ⚔️ 跨文献对撞\n\n"
           "#### ⚔️ 结论冲突｜`[@a]` ↔ `[@b]` `#aaaaaaaa`\n\n正文\n")
    acks = {"aaaaaaaa": "第一行\n第二行"}
    cur, lens = txt, []
    for _ in range(6):
        cur, _n = L.fold_acked_blocks(cur, acks)
        lens.append(len(cur))
    assert len(set(lens)) == 1, "折叠不收敛：{}".format(lens)
    assert "第一行 第二行" in cur


def test_vault_note_fills_in_when_the_notes_copy_has_an_empty_one(tmp_path):
    """B7：notes 那份写了 `- ack: abc`（空说明）会**盖掉** vault 那份的
    `- ack: abc 详细理由`，折叠区显示"（未写说明）"。
    notes 优先的方向不变，但说明为空时用 vault 的。"""
    d, v = tmp_path / "topics", tmp_path / "vault"
    d.mkdir()
    v.mkdir()

    def _write(where, line):
        (where / "_lint.md").write_text(
            T.assemble('---\ntype: "lint"\n---', "正文",
                       T.DEFAULT_USER_ZONE + "\n" + line + "\n",
                       generator=L.LINT_GENERATOR), encoding="utf-8")

    _write(d, "- ack: abc")
    _write(v, "- ack: abc 详细理由")
    assert L.read_lint_acks(d, extra_dirs=[v]) == {"abc": "详细理由"}
    # 方向本身不变：两份都写了说明时仍取 notes 那份（权威产物）
    _write(d, "- ack: abc 札记库这份的说明")
    assert L.read_lint_acks(d, extra_dirs=[v]) == {"abc": "札记库这份的说明"}


def test_stale_delta_compares_what_the_anchor_limit_actually_listed():
    """B3 的连带坑（端到端跑出来的）：`anchor_limit` 收起来的那批不出现在正文里，
    delta 若拿**全量** sid 去比上一版**列出的**那些，被收起来的每一条每轮都被报成
    "本轮新增"——A1 那个"永久幽灵"换了个成因又回来了。"""
    claims = [_stale_at("p", "论断 {}".format(i), 2021, "anchor{:02d}".format(i))
              for i in range(6)]
    r1 = _render(stale_claims=claims, stale_anchor_limit=2, now=_T0,
                 params={"now_year": 2026})
    r2 = _render(stale_claims=claims, stale_anchor_limit=2, now=_T1,
                 params={"now_year": 2026}, previous=L.split_lint_sections(r1))
    block = _block(r2, "证据基础可能过时")
    assert "本轮新增 **0**" in block and "与上轮相同 2" in block and "已消失 0" in block
    assert "阈值前移" not in block          # 一条内容都没变，不该放烟花


def test_fresh_sections_do_not_ship_double_blank_lines(tmp_path):
    """A3 的守卫让陈旧/孤儿两节结转时原样返回——以前靠 `fold_acked_blocks` 顺手压掉的
    那两个连续空行（折叠区前面就是这个形状）现在会一直留在磁盘上。在源头压掉。"""
    cov = L.CoverageReport(n_pages=1, n_orphans_total=2, n_orphans_settled=2,
                           orphans=[{"citekey": "a2020", "year": 2020, "title": "甲"},
                                    {"citekey": "b2021", "year": 2021, "title": "乙"}],
                           thin_pages=[("p", 60, 60)], now_year=2026)
    s = _stale_at("p", "一条", 2021, "x2021")
    text = _render(coverage=cov, stale_claims=[s],
                   acks={"a2020": "看过了", s.sid: "也看过了"}, params={"now_year": 2026})
    assert "\n\n\n" not in text


# ---------------------------------------------------------------------------
# 归档问答（topics/qa/）与概念页的分界（B2）
# ---------------------------------------------------------------------------

def _qa_page(qa_dir, slug, body, extra_fm=""):
    """一页归档问答。**故意也带一个 `topic` 键**：那三处扫描除了文件名还要求
    frontmatter 有 `topic`，若只放正常的 qa 页，它们即便改成 rglob 也照样是绿的——
    测试就测不出分界。带上这个键，唯一还挡着它们的就是"扫描不进 qa 子目录"本身。"""
    qa_dir.mkdir(parents=True, exist_ok=True)
    fm = ('---\nqa: "{}"\ntopic: "{}"\ntitle: "问答"\nn_evidence: 1\n{}---'
          ).format(slug, slug, extra_fm)
    (qa_dir / "{}.md".format(slug)).write_text(
        T.assemble(fm, body, T.DEFAULT_USER_ZONE), encoding="utf-8")


def _topic_page(topics, slug, body):
    topics.mkdir(parents=True, exist_ok=True)
    (topics / "{}.md".format(slug)).write_text(
        T.assemble('---\ntopic: "{}"\ntitle: "概念页"\nn_evidence: 9\n---'.format(slug),
                   body, T.DEFAULT_USER_ZONE), encoding="utf-8")


def test_coverage_counts_qa_citations_but_qa_pages_are_not_concept_pages(tmp_path):
    """B2 的分界，一条测试钉两头。

    **要算上的**：`cited_citekeys`——只被问答引用过的论文不该被算成孤儿
    （缺口分析问的是"哪些论文连概念层的证据池都进不去"，被专门问过一次的显然不是）。

    **不许算上的**：陈旧论断 / 撞车归属 / 页数与 thin_pages——qa 页不是概念页，
    不进概念页索引、不参与日历兜底、不被概念页审计。`is_topic_page_file` 是按
    **文件名**判的，而 `qa-xxx.md` 不以 `_` 开头：一旦这三处也改成 rglob，
    问答页会被当成概念页参与全部三项。
    """
    topics = tmp_path / "topics"
    _topic_page(topics, "real", "- 概念页论断 [@covered2024]")
    _qa_page(topics / "qa", "qa-x", "- 问答论断 [@onlyInQa2024]")

    assert {"covered2024", "onlyInQa2024"} <= L.cited_citekeys(topics)

    index = {"papers": [{"citekey": "covered2024", "year": 2015},
                        {"citekey": "onlyInQa2024", "year": 2015}]}
    assert [s.claim.slug for s in L.find_stale_claims(topics, index, now_year=2026)] == ["real"]
    assert list(L.cited_by_page(topics)) == ["real"]
    rep = L.coverage_report(topics, index)
    assert rep.n_pages == 1
    assert [t[0] for t in rep.thin_pages] == ["real"]


def test_qa_index_page_is_not_counted_as_a_citation_source(tmp_path):
    """`topics/qa/INDEX.md` 是目录页；`_` 前缀留给派生产物（lint 报告自己引用自己
    那个坑）。放宽到 qa 子目录后这两道防线必须仍然生效。"""
    topics = tmp_path / "topics"
    qa = topics / "qa"
    qa.mkdir(parents=True)
    (qa / "INDEX.md").write_text("| [@fromIndex2024] |", encoding="utf-8")
    (qa / "_draft.md").write_text("[@fromDraft2024]", encoding="utf-8")
    _qa_page(qa, "qa-x", "- 真论断 [@real2024]")
    keys = L.cited_citekeys(topics)
    assert "real2024" in keys
    assert "fromIndex2024" not in keys and "fromDraft2024" not in keys
