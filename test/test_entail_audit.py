# -*- coding: utf-8 -*-
"""蕴含审计的离线验收（P1 第二层，2026-08-27）。

这个脚本要花钱，所以**所有能离线验的都必须离线验**：页面解析、送审条目构造、
LLM 返回的处理（漏判/自造编号/非法 verdict/摘录造假）、批次失败记账、熔断。
真实 LLM 判定质量的标定是另一回事，记在
docs/decisions/entail_audit_calibration_2026-08.md。

最要紧的一条：**失败批次不能看起来像"判过且没问题"**。
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import entail_audit as EA          # noqa: E402
from src.scholar.page_parse import parse_page   # noqa: E402


# --- 页面解析 -------------------------------------------------------------

def _write_page(tmp_path, claims_md, evidence_md, fm="n_evidence: 2\n"):
    p = tmp_path / "pg.md"
    p.write_text(
        "---\ntopic: \"pg\"\n{}---\n\n"
        "<!-- BEGIN GENERATED v1 h=aa -->\n"
        "# T\n\n## 小节甲\n\n{}\n\n"
        "## 本页证据（2 条 · 2 篇）\n\n{}\n"
        "<!-- END GENERATED -->\n\n"
        "## 我的批注\n\n- 用户写的 [@zz2026Mine] 不该被送审\n".format(
            fm, claims_md, evidence_md),
        encoding="utf-8")
    return p


EV_MD = (
    "- ● **E1** `[@aa2026Alpha]` 🟩方法论借鉴 · 方法与数据 · 札记.md:1\n"
    "  <small>Alpha 研究</small>\n  > 误差为 0.06。\n"
    "- ● **E2** `[@bb2026Beta]` 🟦可反驳观点 · 局限 · 札记.md:2\n"
    "  <small>Beta 研究</small>\n  > 该方法在该队列中可能相关。\n"
)


def test_parses_claims_evidence_and_section(tmp_path):
    p = _write_page(tmp_path, "- 一条论断。 [@aa2026Alpha]", EV_MD)
    page = parse_page(p)
    assert len(page.claims) == 1
    assert page.claims[0].section == "小节甲"
    assert page.claims[0].citekeys == ["aa2026Alpha"]
    assert page.evidence["E1"].quote == "误差为 0.06。"
    assert page.evidence["E1"].title == "Alpha 研究"


def test_user_zone_inside_gen_block_is_not_collected(tmp_path):
    """批注区是用户自己写的，送去给模型评判等于拿用户的话考模型。

    ⚠️ 原写法把批注放在 `GEN_END` 之后，早被 `if not in_gen` 挡住，`zone == "user"`
    分支从未执行——删掉整个批注区排除逻辑，测试照样全绿。批注标题必须落在生成块
    **内部**才真的测到这条。
    """
    p = tmp_path / "u.md"
    p.write_text(
        "---\ntopic: \"pg\"\n---\n\n"
        "<!-- BEGIN GENERATED v1 h=aa -->\n"
        "# T\n\n- 真论断。 [@aa2026Alpha]\n\n"
        "## 我的批注\n\n- 用户写的 [@zz2026Mine] 不该被收\n\n"
        "<!-- END GENERATED -->\n",
        encoding="utf-8")
    page = parse_page(p)
    assert len(page.claims) == 1
    assert all("zz2026Mine" not in c.citekeys for c in page.claims)


def test_verdict_case_is_normalized():
    """LLM 常返回 "Unsupported" / "OVERREACH"，不归一就会静默变成 supported。"""
    v, rep = _run(_items(1), [{"verdicts": [
        {"id": "C1", "verdict": "Unsupported", "note": "n",
         "quote": "该方法在该队列中可能相关"}]}])
    assert v[0].verdict == "unsupported"
    assert rep.bad_verdicts == 0


def test_illegal_verdict_is_recorded_not_silently_dropped():
    """兜底成 supported 可以，但必须记账——否则就是"缺席看起来像判过且没问题"。"""
    _, rep = _run(_items(1), [{"verdicts": [{"id": "C1", "verdict": "很可疑"}]}])
    assert rep.bad_verdicts == 1
    assert any("非法" in e for e in rep.errors)


def test_collect_items_skips_claims_without_evidence(tmp_path):
    """引用解析不出证据行的论断要跳过——变异测试里放开它曾全绿。"""
    p = _write_page(tmp_path, "- 引了不存在的文献。 [@nope2026Ghost]", EV_MD)
    assert EA.collect_items([p], only_qualitative=False, limit=0) == []


def test_pool_merges_all_rows_of_one_citekey(tmp_path):
    """一个 citekey 可能有多条证据，匹配池要全并进来。"""
    ev = EV_MD + ("- ● **E3** `[@aa2026Alpha]` 🟩x · y · 札记.md:3\n"
                  "  <small>Alpha 研究</small>\n  > 第三条：样本 4242 例。\n")
    p = _write_page(tmp_path, "- x [@aa2026Alpha]", ev)
    page = parse_page(p)
    assert "4242" in page.pool_for(["aa2026Alpha"])
    assert "0.06" in page.pool_for(["aa2026Alpha"])


def test_only_qualitative_skips_numeric_claims(tmp_path):
    """含数字的论断已被第一层覆盖，重复送审是浪费钱。"""
    p = _write_page(tmp_path, "- 误差 0.06 很小。 [@aa2026Alpha]\n"
                              "- 该方法可能相关。 [@bb2026Beta]", EV_MD)
    assert len(EA.collect_items([p], only_qualitative=False, limit=0)) == 2
    items = EA.collect_items([p], only_qualitative=True, limit=0)
    assert len(items) == 1
    assert "可能相关" in items[0].claim


def test_limit_caps_items(tmp_path):
    p = _write_page(tmp_path, "- a [@aa2026Alpha]\n- b [@bb2026Beta]", EV_MD)
    assert len(EA.collect_items([p], False, limit=1)) == 1


# --- 摘录核验 -------------------------------------------------------------

def test_verify_quote_accepts_real_excerpt():
    assert EA._verify_quote("误差为 0.06", "E1（aa）：误差为 0.06。")


def test_verify_quote_rejects_fabricated_excerpt():
    """LLM 说它摘的、和它真摘的是两回事。"""
    assert not EA._verify_quote("作者明确否认了该结论", "E1（aa）：误差为 0.06。")


def test_verify_quote_rejects_too_short():
    """太短的"摘录"没有证明力。"""
    assert not EA._verify_quote("误差", "E1（aa）：误差为 0.06。")


# --- LLM 返回的处理 -------------------------------------------------------

class FakeLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def call(self, prompt, **kw):
        self.calls += 1
        p = self.payloads.pop(0)
        if isinstance(p, Exception):
            raise p
        return json.dumps(p, ensure_ascii=False)


def _items(n, quote="该方法在该队列中可能相关"):
    # raw_quotes 是摘录回验用的池（纯证据内容），evidence 是送 LLM 的展示串。
    # 两者必须分开——否则模型把展示串的表头 `E1（bb2026Beta）：` 抄回来就能
    # 骗过唯一的防伪关。
    return [EA.Item(id="C{}".format(i), page="pg", section="s",
                    claim="论断 {}".format(i),
                    evidence=["E1（bb2026Beta）：{}。".format(quote)],
                    raw_quotes=["{}。".format(quote)],
                    citekeys=["bb2026Beta"]) for i in range(1, n + 1)]


def _run(items, payloads, batch_size=8):
    llm = FakeLLM(payloads)
    return EA.adjudicate(items, "T {{PAIR_BLOCK}}", llm,
                         batch_size=batch_size, model=None, max_tokens=100)


def test_scaffold_cannot_pass_as_a_quote():
    """模型把喂给它的表头原样抄回来，不算摘录。

    ⚠️ 真实存在过的洞：`Item.pool` 曾用 `" ".join(self.evidence)`，而 evidence 每条
    形如 `E1（citekey）：正文` —— 前缀是脚本自己拼的装饰。于是
    `_verify_quote("E1（li2026Criticrag）：", pool)` 返回 True，一条**凭空捏造的
    报警**就通过了「LLM 说它摘的、和它真摘的是两回事」这道唯一的防伪关。
    """
    v, rep = _run(_items(1), [{"verdicts": [
        {"id": "C1", "verdict": "contradicted", "note": "n",
         "quote": "E1（bb2026Beta）："}]}])
    assert rep.unverified == 1
    assert v[0].verdict == "supported"


def test_quote_with_linebreak_still_verifies():
    """真摘录带换行不该被判成编造——验不过的后果是降级成 supported。"""
    assert EA._verify_quote("该方法在\n该队列中可能相关",
                            "该方法在该队列中可能相关。")


def test_fabricated_quote_is_downgraded():
    """摘录对不上证据 = 判断没有依据，降级而不是照单全收。"""
    v, rep = _run(_items(1), [{"verdicts": [
        {"id": "C1", "verdict": "contradicted", "note": "n",
         "quote": "这段话原文里根本没有"}]}])
    assert rep.unverified == 1
    assert v[0].verdict == "supported"
    assert rep.counts.get("contradicted", 0) == 0


def test_verified_quote_keeps_verdict():
    v, rep = _run(_items(1), [{"verdicts": [
        {"id": "C1", "verdict": "overreach", "note": "n",
         "quote": "该方法在该队列中可能相关"}]}])
    assert v[0].verdict == "overreach"
    assert rep.unverified == 0


def test_missing_verdicts_are_recorded():
    """LLM 漏判的条目必须记账——沉默不等于 supported。"""
    _, rep = _run(_items(3), [{"verdicts": [{"id": "C1", "verdict": "supported"}]}])
    assert rep.missing == 2
    assert any("漏判" in e for e in rep.errors)


def test_invented_id_is_dropped():
    _, rep = _run(_items(1), [{"verdicts": [
        {"id": "C1", "verdict": "supported"},
        {"id": "C99", "verdict": "contradicted", "note": "x", "quote": "y"}]}])
    assert sum(rep.counts.values()) == 1


def test_duplicate_id_is_dropped():
    _, rep = _run(_items(1), [{"verdicts": [
        {"id": "C1", "verdict": "supported"},
        {"id": "C1", "verdict": "contradicted", "note": "x", "quote": "y"}]}])
    assert sum(rep.counts.values()) == 1


def test_illegal_verdict_falls_back_to_supported():
    v, _ = _run(_items(1), [{"verdicts": [{"id": "C1", "verdict": "很可疑"}]}])
    assert v[0].verdict == "supported"


def test_failed_batch_is_recorded_not_silently_passed():
    """**最要紧的一条**：失败批次不能看起来像"判过且没问题"。"""
    _, rep = _run(_items(8), [RuntimeError("boom")], batch_size=8)
    assert rep.batches_failed == 1
    assert sum(rep.counts.values()) == 0
    assert any("失败" in e for e in rep.errors)


def test_two_consecutive_failures_trip_the_breaker():
    """回退链整条耗尽后，后面每批都会以同一个错误再失败，继续跑只是烧时间。"""
    items = _items(40)
    llm = FakeLLM([RuntimeError("a"), RuntimeError("b")] +
                  [{"verdicts": []}] * 10)
    _, rep = EA.adjudicate(items, "T {{PAIR_BLOCK}}", llm,
                           batch_size=8, model=None, max_tokens=100)
    assert llm.calls == 2, "熔断后不该再调 LLM"
    assert rep.batches_failed == rep.n_batches
    assert any("中止" in e for e in rep.errors)


def test_one_failure_does_not_stop_the_run():
    """单批失败不带走整轮（同 build_topics.py 的原则）。"""
    items = _items(16)
    _, rep = _run(items, [RuntimeError("x"),
                          {"verdicts": [{"id": "C9", "verdict": "supported"}]}],
                  batch_size=8)
    assert rep.batches_failed == 1
    assert sum(rep.counts.values()) == 1
