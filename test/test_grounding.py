# -*- coding: utf-8 -*-
"""数字接地防线接进生产链路的验收（P1 第一层，2026-08-27）。

`test_gen_bench.py` 证明的是**判据**抓得住失真；本文件证明的是这个判据真的接在了
`validate_synthesis` / `validate_qa` 上、计数真的会跳。一个永远为 0 的计数器和一个
永远报 100% 的 bench 是同一种病。

只记账不拦截是**有意的**：首跑全库 584 个数字 100% 接地，样本还不足以支撑"直接拒绝
落盘"——规则里任何一个没想到的合法写法都会静默吃掉真论断。观察一轮再定。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.scholar import qa as Q          # noqa: E402
from src.scholar import topics as T      # noqa: E402


def _ev(ref, citekey, text, title=None):
    return T.Evidence(ref=ref, citekey=citekey, text=text, role="citable",
                      section="方法与数据", note_file="科研札记_2025-01_全文精读.md",
                      note_line=10, score=0.9, title=title)


EVS = [
    _ev("E1", "aa2026Alpha", "影子变量区间中点平均绝对误差 0.06。", "Alpha 研究"),
    _ev("E2", "bb2026Beta", "队列共 17,775 例，AUC 为 0.488。", "Beta 研究"),
]


def _synth(claim_text, refs=("E1",)):
    return {"summary": "s",
            "sections": [{"heading": "H", "claims": [
                {"text": claim_text, "evidence": list(refs)}]}],
            "disputes": [], "gaps": []}


# --- topics 侧 -----------------------------------------------------------

def test_topics_counts_ungrounded_number():
    """证据是 0.06，论断写 0.6——计数必须跳。"""
    out, rep = T.validate_synthesis(_synth("该方法误差为 0.6。"), EVS)
    assert rep.ungrounded_numbers == 1
    assert rep.ungrounded_claims == 1
    assert rep.kept_claims == 1, "只记账不拦截：论断仍应保留"


def test_topics_catches_cross_paper_attribution():
    """0.488 是 E2 的数字，论断只引了 E1。"""
    _, rep = T.validate_synthesis(_synth("Alpha 报告 AUC 0.488。"), EVS)
    assert rep.ungrounded_numbers == 1


def test_topics_grounded_number_does_not_trip():
    _, rep = T.validate_synthesis(_synth("误差为 0.06。"), EVS)
    assert rep.ungrounded_numbers == 0
    assert rep.numbers_checked == 1


def test_topics_percent_form_does_not_trip():
    """证据 0.488、论断 48.8% 是同一事实。"""
    _, rep = T.validate_synthesis(_synth("AUC 为 48.8%。", refs=("E2",)), EVS)
    assert rep.ungrounded_numbers == 0


def test_topics_title_is_in_the_pool():
    """匹配池含**标题**——标题是文献自己的文字，里面的数字可引。"""
    evs = [_ev("E1", "x2019Key", "无数字的一句话。", "2019 年的队列研究")]
    _, rep = T.validate_synthesis(_synth("这是 2019 年的工作。"), evs)
    assert rep.ungrounded_numbers == 0


def test_citekey_year_is_NOT_in_the_pool():
    """citekey 里的发表年份**不**进池（2026-08-27 第一轮对抗审核后收紧）。

    曾经把 citekey / 年份 / 出处行都并进池子，理由是"年份常来自文献自身"。实测
    证伪：全库 9 页把池砍到只剩原句+标题，未接地数一个都没变（533 个接地数字全靠
    原句/标题硬接地，靠 citekey/年份/出处的是 0 个）。而代价可测——年份 token 让
    「共纳入 2025 例患者」对任何 2025 年的文献自动接地，假接地从 51/455 涨到
    122/455。零收益、可测代价，删掉。

    代价是这类论断现在会被报出来。那是**正确行为**：论断说「2023 年的建议」而所引
    证据原句里没有 2023，就该让人看一眼。
    """
    evs = [_ev("E1", "butler2023Noninterventional", "无数字的一句话。", "T")]
    _, rep = T.validate_synthesis(_synth("2023 年的建议。"), evs)
    assert rep.ungrounded_numbers == 1


def test_topics_meta_number_is_exempt():
    """"这 2 条证据……"说的是本页证据条数，来源是页面元信息。"""
    _, rep = T.validate_synthesis(_synth("2 条证据中仅一条提到该做法。"), EVS)
    assert rep.ungrounded_numbers == 0


def test_topics_derived_is_counted_separately():
    _, rep = T.validate_synthesis(_synth("两者相差约 8.7 倍。", refs=("E2",)), EVS)
    assert rep.numbers_derived == 1
    assert rep.ungrounded_numbers == 0


def test_topics_disputes_are_checked_too():
    """分歧两侧同样带数字、同样会失真。"""
    data = {"summary": "s", "sections": [], "gaps": [],
            "disputes": [{"issue": "争议", "position_a": "一方说误差 0.6。",
                          "evidence_a": ["E1"], "position_b": "另一方说 AUC 0.488。",
                          "evidence_b": ["E2"], "note": "n"}]}
    _, rep = T.validate_synthesis(data, EVS)
    assert rep.ungrounded_numbers == 1, "position_a 的 0.6 该被抓"


def test_topics_dropped_claim_is_not_checked():
    """整条被丢弃的论断不该再计入数字统计。"""
    _, rep = T.validate_synthesis(_synth("误差 0.6。", refs=("E99",)), EVS)
    assert rep.dropped_claims == 1
    assert rep.numbers_checked == 0


def test_frontmatter_carries_the_counters():
    spec = T.TopicSpec(slug="s", title="T", question="Q", queries=["q"])
    out, rep = T.validate_synthesis(_synth("误差为 0.6。"), EVS)
    fm = T.build_frontmatter(spec, out, EVS, rep, "2026-08-27T00:00:00")
    assert "ungrounded_numbers: 1" in fm
    assert "numbers_checked: 1" in fm


# --- qa 侧 ---------------------------------------------------------------

def _qa(text, refs=("E1",)):
    return {"answer": "a", "points": [{"text": text, "evidence": list(refs)}],
            "caveats": [], "gaps": []}


def test_qa_counts_ungrounded_number():
    _, rep = Q.validate_qa(_qa("该方法误差为 0.6。"), EVS)
    assert rep.ungrounded_numbers == 1
    assert rep.kept_claims == 1


def test_qa_grounded_number_does_not_trip():
    _, rep = Q.validate_qa(_qa("误差为 0.06。"), EVS)
    assert rep.ungrounded_numbers == 0


def test_qa_inline_backtranslated_ref_joins_the_pool():
    """正文里写 E2 的论断，回译后 E2 也该进匹配池——否则会被误判失真。

    qa 侧特有：`backtranslate_inline_refs` 会把正文自由文本里的 E2 变成 [@key]，
    但 `evidence` 字段里可能只列了 E1。池子不并起来就会假报。
    """
    _, rep = Q.validate_qa(_qa("如 E2 所示，AUC 为 0.488。", refs=("E1",)), EVS)
    assert rep.ungrounded_numbers == 0
