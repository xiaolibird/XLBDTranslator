# -*- coding: utf-8 -*-
"""src/scholar/topics.py 关键纯函数回归（不连 Ollama、不调 LLM）。

概念页是综述体裁，最容易滋生"读起来天衣无缝但引用是编的"。这份测试锁住四道防线：

1. **编号回译**：LLM 只能引用 E1..En，越界编号剔除、剔空的论断整条丢弃——
   页面上的 citekey 因此不可能凭空出现；
2. **裸引用剥离**：论断正文里模型自写的 `[@key]` 必须被剥掉（它绕开了编号校验，
   是唯一可能编造 citekey 的通道，且会被 pandoc 当真引用去挂书目）；
3. **哨兵不覆盖**：用户批注区被删或生成块被手改时返回 conflict，绝不覆盖；
4. **证据配额**：单篇论文不能吃满整页（概念页的价值在横跨多篇）。

外加配置加载的硬失败（slug 重复/非法会静默毁掉一页，必须抛）。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar import topics as T                                       # noqa: E402


def _ev(ref, citekey, text="证据句", role="citable", score=0.9, bucket_boost=0.0):
    return T.Evidence(ref=ref, citekey=citekey, text=text, role=role,
                      section="方法与数据", note_file="科研札记_2025-01_全文精读.md",
                      note_line=10, score=score, bucket_boost=bucket_boost)


def _evidences(n=3):
    return [_ev("E{}".format(i), "a{}Key".format(i)) for i in range(1, n + 1)]


# ---------------- 防线 1：编号回译 ----------------

def test_valid_refs_translate_to_citekeys():
    """合法编号回译成对应 citekey，且顺序去重。"""
    evs = _evidences(3)
    data = {"sections": [{"heading": "要点", "claims": [
        {"text": "掩码嵌入在 MIMIC-IV 上提升 AUROC 0.02", "evidence": ["E1", "E3", "E1"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    claim = out["sections"][0]["claims"][0]
    assert claim["citekeys"] == ["a1Key", "a3Key"]
    assert rep.kept_claims == 1 and rep.dropped_claims == 0 and rep.invalid_refs == 0


def test_out_of_range_ref_dropped():
    """越界编号（召回只有 3 条却引用 E99）剔除并计数，不得混进页面。"""
    evs = _evidences(3)
    data = {"sections": [{"heading": "要点", "claims": [
        {"text": "有据可依的论断", "evidence": ["E2", "E99"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    assert out["sections"][0]["claims"][0]["citekeys"] == ["a2Key"]
    assert rep.invalid_refs == 1


def test_claim_with_only_invalid_refs_is_dropped_entirely():
    """编号全部无效 = 这条论断无从追溯，整条丢弃而不是留个没引用的句子。"""
    evs = _evidences(2)
    data = {"sections": [{"heading": "要点", "claims": [
        {"text": "凭空捏造的论断", "evidence": ["E42"]},
        {"text": "真有证据的论断", "evidence": ["E1"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    texts = [c["text"] for c in out["sections"][0]["claims"]]
    assert texts == ["真有证据的论断"]
    assert rep.dropped_claims == 1 and rep.invalid_refs == 1


def test_claim_without_evidence_field_dropped():
    """没给 evidence 的论断一律丢弃（prompt 铁律 3 的兜底）。"""
    evs = _evidences(2)
    data = {"sections": [{"heading": "要点", "claims": [
        {"text": "多项研究表明该方法有效"},
        {"text": "带证据的", "evidence": ["E1"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    assert len(out["sections"][0]["claims"]) == 1
    assert rep.dropped_claims == 1


def test_ref_format_normalized():
    """e3 / E03 与 E3 视为同一条——大小写与前导零不该让证据白白丢掉。"""
    evs = _evidences(3)
    data = {"sections": [{"heading": "h", "claims": [
        {"text": "x", "evidence": ["e3", "E03"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    assert out["sections"][0]["claims"][0]["citekeys"] == ["a3Key"]
    assert rep.invalid_refs == 0


def test_empty_sections_after_validation():
    """全部论断被丢弃时 sections 为空——调用方据此判 failed，不落一页空壳。"""
    evs = _evidences(1)
    data = {"sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E9"]}]}]}
    out, rep = T.validate_synthesis(data, evs)
    assert out["sections"] == []
    assert rep.kept_claims == 0


# ---------------- 防线 2：裸引用剥离 ----------------

def test_bare_citekey_in_text_stripped():
    """模型在正文里自写的 [@key] 必须剥掉：它没经过编号校验，且会被 pandoc 当真引用。"""
    evs = _evidences(2)
    data = {"sections": [{"heading": "h", "claims": [
        {"text": "掩码嵌入有效 [@fabricated2099Fake]，在多库上复现",
         "evidence": ["E1"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    claim = out["sections"][0]["claims"][0]
    assert "fabricated2099Fake" not in claim["text"]
    assert "[@" not in claim["text"]
    assert claim["citekeys"] == ["a1Key"]        # 引用只来自编号回译
    assert rep.stripped_cites == 1


def test_bare_citekey_stripped_in_summary_and_gaps():
    """总述与缺口同样要清洗——它们不带编号，是裸引用最容易漏网的地方。"""
    evs = _evidences(1)
    data = {
        "summary": "总体上 [@ghost2020Nope] 指出……",
        "sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E1"]}]}],
        "gaps": ["缺少 [@another2021Ghost] 那类外部验证"],
    }
    out, rep = T.validate_synthesis(data, evs)
    assert "[@" not in out["summary"]
    assert "[@" not in out["gaps"][0]
    assert rep.stripped_cites == 2


def test_bare_citekey_without_brackets_stripped_and_counted():
    """G1：pandoc-citeproc 同样认不带方括号的裸 @key（作者年份型行文引用，如
    "如 @smith2020 所述"），是合法引用语法。此前 _BARE_CITE_RE 只挡 `[@key]` 方括号
    形式，这种写法会原样保留、逃过剥离与计数，也逃过 audit_topic_pages 的死键检测。"""
    evs = _evidences(2)
    data = {"sections": [{"heading": "h", "claims": [
        {"text": "如 @fabricated2099Ghost 所述，效果显著", "evidence": ["E1"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    claim = out["sections"][0]["claims"][0]
    assert "fabricated2099Ghost" not in claim["text"]
    assert "@" not in claim["text"]
    assert claim["citekeys"] == ["a1Key"]        # 引用只来自编号回译，不是正文里的裸引用
    assert rep.stripped_cites == 1


@pytest.mark.parametrize("text,leaked", [
    ("研究表明@sneaky2020Ghost能降低误差", "sneaky2020Ghost"),   # 中文紧贴：最自然的中文写法
    ("如@li2021Fake所述", "li2021Fake"),
    ("第3种方法@x2020Y最好", "x2020Y"),                          # 数字紧贴
    ("（见@paren2020Key）", "paren2020Key"),                     # 中文括号内
    ("如 @spaced2099Ghost 所述", "spaced2099Ghost"),             # 带空格（原本唯一测过的写法）
])
def test_bare_citekey_stripped_regardless_of_what_precedes_it(text, leaked):
    """裸 @key 紧贴在汉字/数字后面时同样必须剥掉。

    回归：后顾断言原本写的是 `(?<![\\w.\\-])`，而 Python 3 的 `\\w` 按 Unicode 匹配、
    **汉字也算单词字符**，于是「研究表明@key」这种中文行文里最自然的写法被判成邮箱局部
    整条放行，audit 的 bare_cites 也一并看不见，`--verify` 给出全绿。这比没有防线更危险。
    当时的测试只覆盖了"如 @xxx 所述"（前后带空格）——恰好是唯一能通过的写法，是假绿。
    """
    evs = _evidences(1)
    data = {"sections": [{"heading": "h", "claims": [{"text": text, "evidence": ["E1"]}]}]}
    out, rep = T.validate_synthesis(data, evs)
    cleaned = out["sections"][0]["claims"][0]["text"]
    assert leaked not in cleaned
    assert "@" not in cleaned
    assert rep.stripped_cites == 1
    # 只删引用本身，不许连带吞掉后面的正文：citekey 不含汉字，若匹配体用"排除空白与
    # 中文标点"的宽字符类，会一路吃到句末标点，页面上只剩半句话。
    tail = text.split(leaked, 1)[1].lstrip("] ")
    assert not tail or tail in cleaned


def test_email_not_mistaken_for_bare_citekey():
    """邮箱不能被误伤——收紧后顾断言时最容易顺手打破的一条。"""
    evs = _evidences(1)
    data = {"sections": [{"heading": "h", "claims": [
        {"text": "数据申请联系 user@example.com 获取", "evidence": ["E1"]}]}]}
    out, rep = T.validate_synthesis(data, evs)
    assert "user@example.com" in out["sections"][0]["claims"][0]["text"]
    assert rep.stripped_cites == 0


def test_email_like_text_not_stripped_as_bare_citation():
    """G1：@ 前紧邻字母/数字/./- 时判定为邮箱局部，不剥离——结构性收紧的代价是必须
    排除这种最常见的合法 @ 用法，不能误伤。"""
    evs = _evidences(1)
    data = {"sections": [{"heading": "h", "claims": [
        {"text": "联系作者 user@example.com 获取原始数据", "evidence": ["E1"]},
    ]}]}
    out, rep = T.validate_synthesis(data, evs)
    claim = out["sections"][0]["claims"][0]
    assert "user@example.com" in claim["text"]
    assert rep.stripped_cites == 0


def test_bare_cite_only_regex_does_not_flag_compact_bracket_form():
    """audit_topic_pages 扫描裸引用前，要先挖掉合法的 [@key] 方括号引用（含
    [@k1; @k2] 紧凑多键写法），剩下文本里残留的 @token 才是真正的裸引用——否则
    紧凑写法里的第二个 @key 会被误判成一条裸引用（它明明在合法方括号内部）。
    生成器自身不产出这种紧凑写法，但用户手写批注区可能会用。"""
    text = "一些论断 [@k1Key; @k2Key] 结尾"
    stripped = T._BRACKET_CITE_RE.sub("", text)
    assert T._BARE_CITE_ONLY_RE.findall(stripped) == []


def test_rendered_page_only_contains_recalled_citekeys():
    """端到端：渲染出的页面里出现的每个 [@key] 都必须来自召回集合。"""
    import re
    evs = _evidences(3)
    data = {
        "summary": "总述 [@sneaky2020X]",
        "sections": [{"heading": "h", "claims": [
            {"text": "论断一 [@sneaky2021Y]", "evidence": ["E1"]},
            {"text": "论断二", "evidence": ["E2", "E77"]},
        ]}],
        "disputes": [{"issue": "争议", "position_a": "A 说", "evidence_a": ["E1"],
                      "position_b": "B 说", "evidence_b": ["E3"]}],
        "gaps": ["缺口"],
    }
    out, _ = T.validate_synthesis(data, evs)
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    md = T.render_generated_block(spec, out, evs)
    recalled = {e.citekey for e in evs}
    for key in re.findall(r"\[@([^\]]+)\]", md):
        assert key in recalled, "页面出现了召回集合之外的 citekey: {}".format(key)


# ---------------- 分歧：两侧都要有证据 ----------------

def test_dispute_needs_both_sides():
    """只有一侧有证据的"分歧"其实是普通论断，丢弃——否则页面会把单方观点渲染成争议。"""
    evs = _evidences(2)
    data = {"sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E1"]}]}],
            "disputes": [
                {"issue": "争议1", "position_a": "A", "evidence_a": ["E1"],
                 "position_b": "B", "evidence_b": []},
                {"issue": "争议2", "position_a": "A", "evidence_a": ["E1"],
                 "position_b": "B", "evidence_b": ["E2"]},
            ]}
    out, rep = T.validate_synthesis(data, evs)
    assert [d["issue"] for d in out["disputes"]] == ["争议2"]
    assert rep.dropped_disputes == 1


def test_dispute_dropped_when_both_sides_cite_identical_citekey_set():
    """回归 F5：分歧两侧若回译到完全相同的一批文献，不是分歧，是同一批文献被复述成
    "一方/另一方"。实测 shadow-variable.md 踩过：两侧同引 aerts2021Quality 一篇论文的
    两条批注（联想 vs 局限），LLM 自己在 note 里都写"两者其实是同一现象的两面"。

    部分重叠必须放行——同一篇论文完全可能既支持又限定，只拦两侧集合**完全相同**的情形。
    """
    evs = [
        _ev("E1", "sameKey", text="对我研究的联想批注"),
        _ev("E2", "sameKey", text="局限与可质疑点批注"),
        _ev("E3", "otherKey", text="另一篇论文的证据"),
    ]
    data = {
        "sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E3"]}]}],
        "disputes": [
            {"issue": "假分歧：左右手互搏", "position_a": "A", "evidence_a": ["E1"],
             "position_b": "B", "evidence_b": ["E2"]},                          # {sameKey} == {sameKey} -> 丢弃
            {"issue": "真分歧：不同文献", "position_a": "A", "evidence_a": ["E1"],
             "position_b": "B", "evidence_b": ["E3"]},                          # {sameKey} != {otherKey} -> 保留
            {"issue": "部分重叠也保留", "position_a": "A", "evidence_a": ["E1"],
             "position_b": "B", "evidence_b": ["E2", "E3"]},                    # {sameKey} != {sameKey,otherKey} -> 保留
        ],
    }
    out, rep = T.validate_synthesis(data, evs)
    assert [d["issue"] for d in out["disputes"]] == ["真分歧：不同文献", "部分重叠也保留"]
    assert rep.dropped_disputes == 1


# ---------------- 防线 3：哨兵不覆盖 ----------------

def _page(gen="正文", user="\n## 我的批注\n\n手写内容\n"):
    fm = "---\ntopic: t\n---"
    return T.assemble(fm, gen, user)


def test_merge_preserves_user_zone():
    """重建保留用户批注区。"""
    old = _page(gen="旧正文")
    new, status = T.merge_topic_page("---\ntopic: t\n---", "新正文", old)
    assert status == "merged"
    assert "手写内容" in new and "新正文" in new and "旧正文" not in new


def test_merge_conflict_when_sentinel_removed():
    """END 哨兵被删 -> conflict 且返回 None，调用方不得覆盖（否则用户手写内容蒸发）。"""
    broken = _page().replace(T.GEN_END, "")
    content, status = T.merge_topic_page("---\ntopic: t\n---", "新正文", broken)
    assert status == "conflict" and content is None


def test_merge_conflict_when_generated_block_tampered():
    """用户在生成块内部写了批注（哈希对不上）-> conflict，不静默覆盖掉它。"""
    old = _page(gen="正文第一行")
    tampered = old.replace("正文第一行", "正文第一行\n\n<!-- 我顺手写的批注 -->")
    content, status = T.merge_topic_page("---\ntopic: t\n---", "新正文", tampered)
    assert status == "conflict" and content is None


def test_new_page_when_absent():
    content, status = T.merge_topic_page("---\ntopic: t\n---", "正文", None)
    assert status == "new"
    assert T.USER_HEADING in content and T.GEN_END in content


def test_assemble_is_idempotent():
    """重建两次内容一致——否则 write_if_changed 每次都判有变更，mtime 白抖、vault 白重建。"""
    fm = "---\ntopic: t\n---"
    once, _ = T.merge_topic_page(fm, "正文", None)
    twice, status = T.merge_topic_page(fm, "正文", once)
    assert status == "merged"
    assert once == twice


# ---------------- G2：read_existing 只信任 FileNotFoundError ----------------

def test_read_existing_missing_file_returns_none(tmp_path):
    """真正不存在的文件仍然是 (None, None, "")——对照组，确认 G2 的修法没有误伤
    这条正常路径。"""
    existing, fm, prev = T.read_existing(tmp_path / "nope.md")
    assert existing is None and fm is None and prev == ""


def test_read_existing_propagates_permission_error(tmp_path):
    """G2：已存在但读不出来的文件（权限被改/磁盘抖动等瞬时读失败）必须让异常向上
    传播，绝不能被吞成"文件不存在"——旧代码用 `except OSError` 把两者一视同仁，
    导致 merge_topic_page 据此走"新建"分支，用空白 DEFAULT_USER_ZONE 重建整页，
    用户几个月的批注被一次 IO 抖动静默清空（编排者实测复现）。"""
    p = tmp_path / "existing.md"
    p.write_text("几个月的手写批注", encoding="utf-8")
    p.chmod(0o000)
    try:
        with pytest.raises(OSError):
            T.read_existing(p)
    finally:
        p.chmod(0o644)   # 恢复权限，避免 tmp_path 自动清理时因权限问题报错


# ---------------- 防线 4：证据配额 ----------------

def test_per_paper_cap_enforced():
    """单篇论文最多贡献 cap 条，不能吃满整页。"""
    cands = [_ev("", "hog2024Big", text="第{}句".format(i), score=0.99 - i * 0.001)
             for i in range(20)]
    cands += [_ev("", "other{}Key".format(i), text="别家", score=0.8) for i in range(5)]
    picked = T.select_evidence(cands, max_evidence=12, per_paper_cap=3)
    from collections import Counter
    counts = Counter(e.citekey for e in picked)
    assert counts["hog2024Big"] == 3
    assert len(counts) == 6            # 独占者 + 5 篇其它


def test_select_evidence_renumbers_sequentially():
    """定稿后编号必须是连续的 E1..En（回译表以此为准，跳号会让映射错位）。"""
    cands = [_ev("", "k{}Key".format(i), score=0.9 - i * 0.01) for i in range(5)]
    picked = T.select_evidence(cands, max_evidence=3)
    assert [e.ref for e in picked] == ["E1", "E2", "E3"]
    assert picked[0].score >= picked[1].score >= picked[2].score


def test_select_evidence_respects_max():
    cands = [_ev("", "k{}Key".format(i), score=0.9) for i in range(50)]
    assert len(T.select_evidence(cands, max_evidence=10)) == 10
    assert T.select_evidence(cands, max_evidence=0) == []


def test_select_evidence_bucket_boost_breaks_near_ties():
    """回归 F1：bucket_boost 只影响排序名次，不改写 Evidence.score 本身。

    2026-08-16 前 bucket 是硬过滤（不匹配就出局）；改成软加权后命中给 +0.03——
    够反超同档内一个原始分数略高但没打偏好标签的证据，但重编号后展示的 score
    仍是原始余弦，不能让加成污染了"这条证据到底多相关"这件事本身。
    """
    boosted = _ev("", "boostedKey", score=0.58, bucket_boost=0.03)
    plain = _ev("", "plainKey", score=0.60)
    picked = T.select_evidence([boosted, plain], max_evidence=2)
    assert [e.citekey for e in picked] == ["boostedKey", "plainKey"]
    assert picked[0].score == 0.58     # 排到第一但展示的分数没被加成污染
    assert picked[0].bucket_boost == 0.03


# ---------------- 召回：bucket 软加权 / exclude_sections 硬过滤 ----------------

class _FakeEmbedClient:
    """只实现 retrieve_evidence 用到的 embed() 接口，每次调用返回同一个固定向量，
    这样 store 里每条记录与 query 的余弦相似度就完全由记录向量本身决定，可控。"""
    def embed(self, texts):
        import numpy as np
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]


def _fake_highlight_record(citekey, score, *, bucket=None, section="结果"):
    """构造一条 2 维单位向量记录：与固定 query 向量 [1, 0] 的点积精确等于 score。"""
    other = float((1 - score ** 2) ** 0.5)
    record = {
        "id": "h:{}:citable:x:0".format(citekey), "level": "highlight",
        "citekey": citekey, "role": "citable", "section": section,
        "month": None, "series": None, "tier": None, "bucket": bucket or [],
        "year": 2024, "has_full_text": True, "note_file": "x.md",
        "note_line": 1, "text": "{} 的证据句".format(citekey),
    }
    return record, [score, other]


def _fake_store(records_and_vecs):
    import numpy as np
    from src.scholar.embed_store import VectorStore
    records = [r for r, _ in records_and_vecs]
    mat = np.array([v for _, v in records_and_vecs], dtype=np.float32)
    return VectorStore(meta={}, records=records, mat=mat)


def test_retrieve_evidence_bucket_is_soft_not_hard_filter():
    """回归 F1（地基级 bug）：全库审计发现 37% 的句级证据 bucket 为空（manual 系列
    220 篇 100% 全空），硬过滤会把这批证据整体排除在 7/8 个概念页之外。

    这里验证改成软加权后的两个契约：(1) bucket 为空的证据不再被 mask 挡在门外；
    (2) bucket 命中偏好维度的证据能在排序中反超一个原始相似度略高但没打标签的证据，
    但不改变谁能不能过 min_sim 门槛（min_sim 用的是加成前的原始分）。
    """
    # aKey 没打 bucket 标签、原始相似度更高；bKey 打了偏好维度 A、原始相似度略低
    rec_a = _fake_highlight_record("aKey", 0.60, bucket=[])
    rec_b = _fake_highlight_record("bKey", 0.58, bucket=["A"])
    store = _fake_store([rec_a, rec_b])
    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["query"],
                       buckets=["A"], exclude_sections=[])

    evs = T.retrieve_evidence(spec, store, _FakeEmbedClient(), min_sim=0.5, bucket_bonus=0.03)

    assert {e.citekey for e in evs} == {"aKey", "bKey"}   # 空 bucket 没有被硬过滤掉
    assert evs[0].citekey == "bKey"                        # 0.58+0.03=0.61 > 0.60，排到第一
    assert evs[0].score == pytest.approx(0.58)              # 展示分数仍是原始余弦


def test_retrieve_evidence_relative_threshold_trims_strong_query_tail():
    """P1a 相对判据：eff_min = max(floor, α×top1)。强 query（top1 高）时边缘尾巴
    被抬高的门槛剪掉；α=0 关闭后退回纯绝对阈值（尾巴保留）——floor 语义不变。"""
    rec_hi = _fake_highlight_record("strongKey", 0.90)
    rec_lo = _fake_highlight_record("tailKey", 0.56)     # 过 floor 0.55，但 0.85×0.90=0.765 之下
    store = _fake_store([rec_hi, rec_lo])
    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["query"],
                       buckets=[], exclude_sections=[])

    evs = T.retrieve_evidence(spec, store, _FakeEmbedClient(), min_sim=0.55)
    assert [e.citekey for e in evs] == ["strongKey"]      # 尾巴被相对门槛剪掉

    evs_off = T.retrieve_evidence(spec, store, _FakeEmbedClient(), min_sim=0.55,
                                  relative_alpha=0.0)
    assert {e.citekey for e in evs_off} == {"strongKey", "tailKey"}   # 关闭后保留


def test_retrieve_evidence_relative_threshold_keeps_cold_query_floor():
    """冷门 query（top1 低）：α×top1 < floor 时门槛退回 floor，个位数召回是诚实的
    库覆盖信号，不得被相对判据进一步收紧。"""
    rec_a = _fake_highlight_record("coldA", 0.60)
    rec_b = _fake_highlight_record("coldB", 0.56)         # 0.85×0.60=0.51 < floor
    store = _fake_store([rec_a, rec_b])
    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["query"],
                       buckets=[], exclude_sections=[])
    evs = T.retrieve_evidence(spec, store, _FakeEmbedClient(), min_sim=0.55)
    assert {e.citekey for e in evs} == {"coldA", "coldB"}


def test_retrieve_evidence_bucket_bonus_does_not_affect_min_sim_gate():
    """加成只用于排序，不改变 min_sim 判定——一条 bucket 命中但原始相似度不足的证据，
    不能靠加成"买"到过门槛的资格（回归 F1 修法里"先按原始余弦过 min_sim，再加成排序"）。
    """
    rec = _fake_highlight_record("belowKey", 0.52, bucket=["A"])   # 0.52+0.03=0.55，仍够不着 min_sim
    store = _fake_store([rec])
    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["query"],
                       buckets=["A"], exclude_sections=[])

    evs = T.retrieve_evidence(spec, store, _FakeEmbedClient(), min_sim=0.55, bucket_bonus=0.03)

    assert evs == []


def test_retrieve_evidence_excludes_default_sections():
    """回归 F4：「对我研究的联想」是精读者自己的推测性批注，不是文献内容，默认硬排除——
    哪怕它跟 query 的语义相似度很高，也不该混进证据池被综述当成"已验证结论"引用。
    """
    rec_a = _fake_highlight_record("aKey", 0.90, section="对我研究的联想")
    rec_b = _fake_highlight_record("bKey", 0.60, section="结果")
    store = _fake_store([rec_a, rec_b])
    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["query"])   # 用默认 exclude_sections

    evs = T.retrieve_evidence(spec, store, _FakeEmbedClient(), min_sim=0.5)

    assert {e.citekey for e in evs} == {"bKey"}   # aKey 相似度更高，但因 section 被硬排除


def test_exclude_sections_matches_annotated_variants():
    """排除按**前缀**匹配，不能是精确相等。

    回归：库里同一类分节存在带括注的变体，实测三种写法——「对我研究的联想」2179 条、
    「对我研究的联想（研究者自身引申，非论文原创结论）」3 条、
    「对我研究的联想(MNAR / MA-GCT / 缺失机制 / 生存分析)」1 条。精确匹配只挡住第一种，
    2026-08-17 实测 mnar-diagnosis 页的 E53 就是靠变体名混进去并被引用的——而这些变体
    恰恰因为写得更详细、更像结论，危害比原版更大。
    """
    recs = [
        _fake_highlight_record("aKey", 0.95, section="对我研究的联想（研究者自身引申，非论文原创结论）"),
        _fake_highlight_record("bKey", 0.94, section="对我研究的联想(MNAR / MA-GCT / 缺失机制 / 生存分析)"),
        _fake_highlight_record("cKey", 0.60, section="结果"),
    ]
    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["query"])

    evs = T.retrieve_evidence(spec, _fake_store(recs), _FakeEmbedClient(), min_sim=0.5)

    assert {e.citekey for e in evs} == {"cKey"}


# ---------------- 证据块只给编号，不给 citekey ----------------

def test_evidence_block_hides_citekeys():
    """喂给 LLM 的证据块里绝不能出现 citekey——模型看不到就写不出，这是防线 1 的前提。"""
    evs = _evidences(3)
    block = T.render_evidence_block(evs)
    for e in evs:
        assert e.citekey not in block
        assert "[{}]".format(e.ref) in block


def test_build_prompt_fills_placeholders():
    spec = T.TopicSpec(slug="t", title="标题", question="核心问题？", queries=["q"])
    tpl = ("T={{TOPIC_TITLE}} Q={{TOPIC_QUESTION}} R={{RESEARCH_INTERESTS}} "
           "E={{EVIDENCE_BLOCK}} P={{PREVIOUS_PAGE}}")
    out = T.build_prompt(spec, _evidences(2), tpl, "我的研究", "")
    assert "{{" not in out
    assert "标题" in out and "核心问题？" in out and "我的研究" in out
    assert "无上一版本" in out


# ---------------- 解析容错 ----------------

def test_parse_strips_code_fence():
    assert T.parse_synthesis('```json\n{"summary": "x"}\n```')["summary"] == "x"


def test_parse_recovers_from_leading_prose():
    """模型偶尔在 JSON 前面写一句话——截取首尾花括号救回来，别整页作废。"""
    raw = '好的，这是结果：\n{"summary": "x", "sections": []}\n希望有帮助'
    assert T.parse_synthesis(raw)["summary"] == "x"


def test_parse_rejects_non_json():
    with pytest.raises(T.TopicError):
        T.parse_synthesis("我无法完成这个任务。")
    with pytest.raises(T.TopicError):
        T.parse_synthesis("")


def test_parse_rejects_json_array():
    """顶层是数组时下游 .get 会 AttributeError，提前判死。"""
    with pytest.raises(T.TopicError):
        T.parse_synthesis('[{"summary": "x"}]')


# ---------------- 配置加载：错误必须硬失败 ----------------

def _write_yaml(tmp_path, body):
    p = tmp_path / "topics.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_specs_ok(tmp_path):
    p = _write_yaml(tmp_path, """
topics:
  - slug: mnar-diagnosis
    title: MNAR 诊断
    question: 怎么判断？
    queries: [MNAR, 缺失机制]
    buckets: [A, C]
    max_evidence: 30
""")
    specs = T.load_topic_specs(p)
    assert len(specs) == 1
    s = specs[0]
    assert s.slug == "mnar-diagnosis" and s.buckets == ["A", "C"] and s.max_evidence == 30
    assert s.exclude_sections == T.DEFAULT_EXCLUDE_SECTIONS   # 没写这个键就用默认排除


def test_exclude_sections_can_be_overridden_per_page(tmp_path):
    """F4：缺省排除「对我研究的联想」，但显式给了这个键（哪怕是空列表）就完全按用户
    写的来——允许个别页主动要联想式内容，而不是全局一刀切。"""
    p = _write_yaml(tmp_path, """
topics:
  - {slug: default-page, title: A, question: "q?", queries: [x]}
  - {slug: no-exclude-page, title: B, question: "q?", queries: [y], exclude_sections: []}
  - {slug: custom-page, title: C, question: "q?", queries: [z], exclude_sections: ["局限与可质疑点"]}
""")
    specs = {s.slug: s for s in T.load_topic_specs(p)}
    assert specs["default-page"].exclude_sections == T.DEFAULT_EXCLUDE_SECTIONS
    assert specs["no-exclude-page"].exclude_sections == []
    assert specs["custom-page"].exclude_sections == ["局限与可质疑点"]


def test_duplicate_slug_rejected(tmp_path):
    """重复 slug 会让后一页静默覆盖前一页——必须报错而不是少生成一页。"""
    p = _write_yaml(tmp_path, """
topics:
  - {slug: dup, title: A, question: "q?", queries: [x]}
  - {slug: dup, title: B, question: "q?", queries: [y]}
""")
    with pytest.raises(T.TopicError, match="重复"):
        T.load_topic_specs(p)


def test_bad_slug_rejected(tmp_path):
    """slug 直接做文件名与 wiki 链接，非 ASCII/含空格会在 Obsidian 和 shell 两侧各炸一次。"""
    p = _write_yaml(tmp_path, """
topics:
  - {slug: "缺失机制", title: A, question: "q?", queries: [x]}
""")
    with pytest.raises(T.TopicError, match="slug"):
        T.load_topic_specs(p)


def test_missing_queries_rejected(tmp_path):
    p = _write_yaml(tmp_path, """
topics:
  - {slug: ok-slug, title: A, question: "q?", queries: []}
""")
    with pytest.raises(T.TopicError, match="queries"):
        T.load_topic_specs(p)


def test_bad_role_rejected(tmp_path):
    """roles 打错字（citeable）会静默过滤掉全部证据，页面变空——提前判死。"""
    p = _write_yaml(tmp_path, """
topics:
  - {slug: ok-slug, title: A, question: "q?", queries: [x], roles: [citeable]}
""")
    with pytest.raises(T.TopicError, match="roles"):
        T.load_topic_specs(p)


def test_missing_config_file(tmp_path):
    with pytest.raises(T.TopicError):
        T.load_topic_specs(tmp_path / "nope.yaml")


def test_repo_topics_config_is_valid():
    """仓库自带的 config/topics.yaml 必须始终可加载（改坏了这里立刻红）。"""
    repo_cfg = Path(__file__).resolve().parents[1] / "config" / "topics.yaml"
    specs = T.load_topic_specs(repo_cfg)
    assert len(specs) >= 5
    assert len({s.slug for s in specs}) == len(specs)


def test_repo_prompt_template_is_valid():
    repo_prompt = Path(__file__).resolve().parents[1] / "config" / "prompts" / "topic_synthesis_prompt.md"
    tpl = T.load_prompt_template(repo_prompt)
    assert "{{EVIDENCE_BLOCK}}" in tpl


# ---------------- 渲染 ----------------

def test_render_marks_unused_evidence():
    """未被引用的证据标 ○：引用率低是 queries 跑偏的信号，得让人看见。"""
    evs = _evidences(3)
    data = {"sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E1"]}]}]}
    out, _ = T.validate_synthesis(data, evs)
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    md = T.render_generated_block(spec, out, evs)
    assert "● **E1**" in md
    assert "○ **E2**" in md and "○ **E3**" in md


def test_render_marks_dispute_only_evidence_as_used():
    """回归 F2：只在「⚔️ 分歧与冲突」里出现的证据必须标 ●，不能因为渲染层只扫了
    sections 而误标 ○。validate_synthesis 的 report.used_refs 其实一直把 disputes
    算进去了（_refs_to_keys 对 evidence_a/evidence_b 都调用过），漏的只是
    render_generated_block 自己算的 used_refs 集合——两处口径必须统一。
    """
    evs = _evidences(3)
    data = {
        "sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E1"]}]}],
        "disputes": [{"issue": "争议", "position_a": "A 说", "evidence_a": ["E2"],
                      "position_b": "B 说", "evidence_b": ["E3"]}],
    }
    out, rep = T.validate_synthesis(data, evs)
    assert out["disputes"][0]["refs_a"] == ["E2"]      # 原始编号被保留，不只是回译后的 citekey
    assert out["disputes"][0]["refs_b"] == ["E3"]
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    md = T.render_generated_block(spec, out, evs)
    assert "● **E1**" in md
    assert "● **E2**" in md              # 只在分歧一方出现，之前会被误标 ○
    assert "● **E3**" in md              # 只在分歧另一方出现，之前会被误标 ○
    assert "○ **E2**" not in md and "○ **E3**" not in md   # 三条证据全被用上，一条都不该是空心
    assert rep.used_refs == 3            # frontmatter 的 evidence_used 与页面 ● 计数口径一致


def test_frontmatter_reports_validation_counters():
    """校验战果进 frontmatter：invalid_refs 突然变大 = 模型开始乱编，得能一眼看见。"""
    evs = _evidences(2)
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    data = {"sections": [{"heading": "h", "claims": [
        {"text": "x [@ghost2020Z]", "evidence": ["E1", "E88"]}]}]}
    out, rep = T.validate_synthesis(data, evs)
    fm = T.build_frontmatter(spec, out, evs, rep, "2026-08-16T10:00:00")
    assert "invalid_refs: 1" in fm
    assert "stripped_cites: 1" in fm
    assert "n_claims: 1" in fm


# ---------------- 目录页：按磁盘现状重建 ----------------

def _topic_page(dir_, slug, title="标题", n_ev=10, n_papers=5, n_claims=7, n_disputes=1):
    dir_.mkdir(parents=True, exist_ok=True)
    fm = ('---\ntopic: "{}"\ntitle: "{}"\nn_evidence: {}\nn_papers: {}\n'
          'n_claims: {}\nn_disputes: {}\n---'.format(slug, title, n_ev, n_papers,
                                                     n_claims, n_disputes))
    (dir_ / "{}.md".format(slug)).write_text(
        T.assemble(fm, "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")


def test_index_lists_all_pages_on_disk(tmp_path):
    """`--topic X` 单跑一页时，目录页必须仍列出全部已存在的页。

    回归：目录页原本只写本次运行的结果，单跑一页会让其余页从目录里整体消失
    （文件还在，但没人找得到）。
    """
    d = tmp_path / "topics"
    _topic_page(d, "aaa", "第一页")
    _topic_page(d, "bbb", "第二页")
    specs = [T.TopicSpec(slug=s, title=t, question="问题？", queries=["q"])
             for s, t in (("aaa", "第一页"), ("bbb", "第二页"))]
    md = T.render_topics_index(d, specs)
    assert "第一页" in md and "第二页" in md
    assert "(aaa.md)" in md and "(bbb.md)" in md


def test_index_flags_retired_pages(tmp_path):
    """页面还在磁盘上但已从 topics.yaml 移除时，标注出来而不是假装它是当前主题。"""
    d = tmp_path / "topics"
    _topic_page(d, "current", "在用")
    _topic_page(d, "dropped", "已下线")
    specs = [T.TopicSpec(slug="current", title="在用", question="问题？", queries=["q"])]
    md = T.render_topics_index(d, specs)
    assert "已下线" in md
    assert "dropped" in md and "已不在" in md


def test_index_shows_zero_disputes_not_dash(tmp_path):
    """G7：n_disputes=0 是合法值（真的没有分歧），`fm.get("n_disputes") or "—"` 的
    真值判断会把它误显示成"—"（看起来像缺失/未知）——fixture 默认 n_disputes=1，
    此前从没覆盖过 0 的情形。"""
    d = tmp_path / "topics"
    _topic_page(d, "zero-disputes", n_disputes=0)
    specs = [T.TopicSpec(slug="zero-disputes", title="标题", question="问题？", queries=["q"])]
    md = T.render_topics_index(d, specs)
    row = next(ln for ln in md.splitlines() if "zero-disputes.md" in ln)
    assert row.rstrip().endswith("| 0 |")


def test_index_skips_itself_and_bad_files(tmp_path):
    """INDEX.md 自身与无 frontmatter 的杂文件不该出现在目录里。"""
    d = tmp_path / "topics"
    _topic_page(d, "good")
    (d / "INDEX.md").write_text("# 旧目录\n", encoding="utf-8")
    (d / "notes.md").write_text("随手记的东西，没有 frontmatter\n", encoding="utf-8")
    md = T.render_topics_index(d, [])
    assert "(good.md)" in md
    assert "INDEX.md)" not in md and "notes.md)" not in md


# ---------------- vault 同步：wiki 链接与死链防护 ----------------

def test_to_wiki_links_converts_known_keys():
    """已进 vault 的 citekey 转成 wiki 链接——这是概念页连进关系图的唯一途径。"""
    out = T.to_wiki_links("论断 [@a2024Key] 与 [@b2025Other]",
                          {"a2024Key": "a2024Key", "b2025Other": "b2025Other-2"})
    assert "[[a2024Key]]" in out
    assert "[[b2025Other-2|b2025Other]]" in out      # 撞名后缀要走别名形式


def test_to_wiki_links_keeps_unknown_as_plain_cite():
    """不在 vault 里的论文（如仅摘要未精读）保持 [@key] 原样，别造死链。"""
    out = T.to_wiki_links("论断 [@ghost2020NotInVault]", {"a2024Key": "a2024Key"})
    assert out == "论断 [@ghost2020NotInVault]"


def test_sync_topics_to_vault_roundtrip(tmp_path):
    """端到端：源页 -> vault 页（转链接、建用户区），重跑保留批注且幂等。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    (src / "demo.md").write_text(
        T.assemble('---\ntopic: "demo"\ntitle: "演示"\n---',
                   "# 演示\n\n- 论断 [@a2024Key] 与 [@ghost2020Nope]", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    (src / "INDEX.md").write_text("# 索引\n", encoding="utf-8")

    rep = T.sync_topics_to_vault(notes_dir, vault_dir, {"a2024Key": "a2024Key"})
    dst = vault_dir / T.VAULT_TOPICS_DIR / "demo.md"
    assert rep["new"] == 1 and dst.exists()
    body = dst.read_text(encoding="utf-8")
    assert "[[a2024Key]]" in body                 # 已进 vault 的转链接
    assert "[@ghost2020Nope]" in body             # 未进 vault 的保持原样
    assert not (vault_dir / T.VAULT_TOPICS_DIR / "INDEX.md").exists()   # INDEX 不同步

    # 用户在 vault 侧写批注后重跑：批注保留，内容未变
    dst.write_text(body.replace(T.USER_HEADING, T.USER_HEADING + "\n\n我的想法"), encoding="utf-8")
    rep2 = T.sync_topics_to_vault(notes_dir, vault_dir, {"a2024Key": "a2024Key"})
    assert "我的想法" in dst.read_text(encoding="utf-8")
    assert rep2["unchanged"] == 1 and rep2["conflicts"] == []


def test_sync_skips_non_topic_files(tmp_path):
    """用户随手放进 topics/ 的笔记（无 frontmatter）不该被当概念页同步进 vault。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    (src / "随手记.md").write_text("我自己写的东西，没有 frontmatter\n", encoding="utf-8")
    rep = T.sync_topics_to_vault(notes_dir, vault_dir, {})
    assert rep["synced"] == 0
    assert not (vault_dir / T.VAULT_TOPICS_DIR / "随手记.md").exists()


def test_sync_prunes_only_clean_retired_pages(tmp_path):
    """topics.yaml 删掉某页后：无批注的 vault 页清理，有批注的保留（绝不替用户删）。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    (notes_dir / T.TOPICS_DIRNAME).mkdir(parents=True)
    out_dir = vault_dir / T.VAULT_TOPICS_DIR
    out_dir.mkdir(parents=True)
    (out_dir / "clean.md").write_text(
        T.assemble("---\ntopic: clean\n---", "旧正文", T.DEFAULT_USER_ZONE), encoding="utf-8")
    (out_dir / "annotated.md").write_text(
        T.assemble("---\ntopic: annotated\n---", "旧正文",
                   T.DEFAULT_USER_ZONE + "\n重要笔记"), encoding="utf-8")

    rep = T.sync_topics_to_vault(notes_dir, vault_dir, {})
    assert rep["pruned"] == ["clean.md"]
    assert (out_dir / "annotated.md").exists()
    assert "重要笔记" in (out_dir / "annotated.md").read_text(encoding="utf-8")


def test_sync_skips_prune_when_source_read_fails(tmp_path):
    """G3：源文件本轮读失败（权限被改/IO 抖动）不等于"已从 topics.yaml 下线"——
    审计实测复现：chmod 0 模拟瞬时读失败，内容完好的源文件被误判成"已下线"，
    vault 侧同名文件被 unlink。修法：本轮只要出现这种"读不出/解析不出"的情形，
    整体跳过 prune 并在 report 里说明原因，vault 侧旧页原样保留。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src_dir = notes_dir / T.TOPICS_DIRNAME
    src_dir.mkdir(parents=True)
    unreadable = src_dir / "mnar-diagnosis.md"
    unreadable.write_text(
        T.assemble('---\ntopic: "mnar-diagnosis"\n---', "正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    out_dir = vault_dir / T.VAULT_TOPICS_DIR
    out_dir.mkdir(parents=True)
    vault_page = out_dir / "mnar-diagnosis.md"
    vault_page.write_text(
        T.assemble('---\ntopic: "mnar-diagnosis"\n---', "旧正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")

    unreadable.chmod(0o000)
    try:
        rep = T.sync_topics_to_vault(notes_dir, vault_dir, {})
    finally:
        unreadable.chmod(0o644)

    assert vault_page.exists()             # 内容完好的旧页没有被误删
    assert rep["pruned"] == []
    assert rep["prune_skipped_reason"]     # 跳过原因被记进 report，供 build_vault.py 打印


def test_sync_no_topics_dir_is_noop(tmp_path):
    """没有 topics 目录时空转——不能让未启用概念页的用户跑 build_vault 报错。"""
    rep = T.sync_topics_to_vault(tmp_path / "notes", tmp_path / "vault", {})
    assert rep["synced"] == 0 and rep["conflicts"] == []
    assert not (tmp_path / "vault").exists()


def test_frontmatter_preserves_user_keys():
    """用户在 frontmatter 自加的键不被生成器抹掉（同 vault 约定）。"""
    evs = _evidences(1)
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    data = {"sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E1"]}]}]}
    out, rep = T.validate_synthesis(data, evs)
    fm = T.build_frontmatter(spec, out, evs, rep, "2026-08-16T10:00:00",
                             preserved={"my_note": "别删我", "topic": "会被覆盖"})
    assert "my_note" in fm
    assert '"t"' in fm          # topic 仍由生成器覆盖


# ---------------- G4：frontmatter 键名消毒（复用 vault._SAFE_KEY/_YAML_WORDS/_y） ----------------

def test_frontmatter_sanitizes_unsafe_preserved_key_names():
    """G4：用户在 Obsidian 属性面板/文本编辑器里自加的键名是任意字符串——含 `: ` `#`
    或空串的键裸拼会写出语法错误的 YAML，下次 split_frontmatter 解析失败，该页永久
    卡在 conflict（审计实测：用户加了个「备注: 待复核」这种键就踩上）。复用
    vault._SAFE_KEY/_YAML_WORDS/_y 消毒后必须仍能被正常解析回去，且键名本身
    （而不是被 YAML 1.1 解析成 bool/null 的裸词）原样保留。"""
    evs = _evidences(1)
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])
    data = {"sections": [{"heading": "h", "claims": [{"text": "x", "evidence": ["E1"]}]}]}
    out, rep = T.validate_synthesis(data, evs)
    fm = T.build_frontmatter(spec, out, evs, rep, "2026-08-16T10:00:00",
                             preserved={"备注: 待复核": "危险键名", "": "空串键名",
                                       "yes": "YAML保留词键名"})
    from src.scholar.vault import split_frontmatter
    parsed, _ = split_frontmatter(fm)
    assert parsed is not None                          # 必须仍是合法 YAML，不能解析失败
    assert parsed.get("备注: 待复核") == "危险键名"
    assert parsed.get("") == "空串键名"
    assert parsed.get("yes") == "YAML保留词键名"         # 没被 YAML 1.1 解析成 bool 键 True


def test_sync_topics_to_vault_sanitizes_unsafe_frontmatter_keys(tmp_path):
    """G4：sync_topics_to_vault 内部的 frontmatter 序列化与 build_frontmatter 共用
    同一个 _render_frontmatter（此前是两份各自演化的拷贝），键名消毒同样要生效——
    源文件 frontmatter 里任何一个不安全的键名，重新序列化到 vault 侧时都不能写出
    语法错误的 YAML。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    (src / "demo.md").write_text(
        T.assemble('---\ntopic: "demo"\n"备注: 待复核": "危险键名"\n---',
                   "正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")

    rep = T.sync_topics_to_vault(notes_dir, vault_dir, {})
    assert rep["conflicts"] == []
    dst = vault_dir / T.VAULT_TOPICS_DIR / "demo.md"
    from src.scholar.vault import split_frontmatter
    parsed, _ = split_frontmatter(dst.read_text(encoding="utf-8"))
    assert parsed is not None
    assert parsed.get("备注: 待复核") == "危险键名"


# ---------------- G5：synthesize_topic 统一包装 llm.call 异常 ----------------

class _FakeLLMRaisesRuntimeError:
    """模拟 llm_client.py 回退链整条耗尽时的行为：抛 RuntimeError，不是 TopicError。"""
    def call(self, *a, **kw):
        raise RuntimeError("所有 LLM provider 均不可用（回退链已耗尽）")


def test_synthesize_topic_wraps_llm_exceptions_as_topic_error():
    """G5：llm.call 抛出的任何异常都必须被包成 TopicError——尤其是回退链耗尽时的
    RuntimeError（本项目实际踩过：订阅限流/备用 provider 地区不可用），否则
    build_topic 里"保留召回统计再上抛"的 except TopicError 分支会被跳过，落到 CLI
    的泛 except，构造出的 TopicResult 没有 n_evidence/n_papers。"""
    with pytest.raises(T.TopicError):
        T.synthesize_topic(_FakeLLMRaisesRuntimeError(), "prompt")


# ---------------- G6：GEN_BEGIN/GEN_END 与 vault.py 的哨兵格式保持兼容 ----------------

def test_gen_end_reused_from_vault():
    """G6：GEN_END 不再在 topics.py 里重新声明一份字面量，直接从 vault 导入——两边
    测的是同一个对象，不会出现"改一边忘了改另一边"的漂移（此前纯靠人工保持一致，
    没有测试强制）。"""
    from src.scholar import vault as V
    assert T.GEN_END is V.GEN_END


@pytest.mark.parametrize("generator", [T.DEFAULT_GENERATOR, "scripts/lint_notes.py",
                                       "某个带 · 中点和 -- 破折号的名字"])
def test_gen_begin_matches_vault_regex(generator):
    """G6：GEN_BEGIN 因文案里写了自己的生成脚本名，继续独立声明是对的，但格式必须
    仍能被 vault.GEN_BEGIN_RE 认出——否则 extract_user_zone/generated_block_tampered
    （本模块从 vault 导入、内部认 vault 自己那份正则）会在 topics.py 生成的页面上
    失效，每次重建都误判成 conflict。

    脚本名 2026-08-17 起是参数（topics/ 下不止一种产物：概念页由 build_topics.py 写、
    知识层 lint 报告由 lint_notes.py 写），所以要把"换个脚本名照样认得出"锁住。"""
    from src.scholar.vault import GEN_BEGIN_RE
    line = T.GEN_BEGIN.format(T.TOPIC_SCHEMA_VERSION, "abcd1234", generator)
    assert GEN_BEGIN_RE.match(line.strip())


def test_assemble_records_generator_and_stays_parseable():
    """哨兵里的脚本名要如实反映谁写的这份文件（人按提示去重跑时不能被指到一个根本
    不生成它的命令上），同时不能因此破坏用户区抽取与篡改检测。"""
    from src.scholar.vault import extract_user_zone, split_frontmatter
    text = T.assemble("---\ntype: lint\n---", "正文", T.DEFAULT_USER_ZONE,
                      generator="scripts/lint_notes.py")
    assert "由 scripts/lint_notes.py 生成" in text
    assert "build_topics.py" not in text
    from src.scholar.vault import generated_block_tampered
    _fm, body = split_frontmatter(text)
    assert extract_user_zone(body) is not None
    assert not generated_block_tampered(body)


# ---------------- G1：audit_topic_pages 发现已生成页面里残留的裸引用 ----------------

def test_audit_detects_bare_citation_left_in_generated_page(tmp_path):
    """--verify 必须能发现已生成页面里残留的裸引用（历史遗留场景：2026-08-17 前的
    旧代码只挡方括号形式，那版代码生成的页面里若混进裸 @key，audit_topic_pages 此前
    完全看不见——这是 G1 的核心防线补丁，兜底覆盖"重跑之前"的旧页）。"""
    d = tmp_path / "topics"
    d.mkdir()
    fm = '---\ntopic: "demo"\ntitle: "标题"\n---'
    gen = "# 标题\n\n- 一条论断，如 @sneaky2020Ghost 所述"
    (d / "demo.md").write_text(T.assemble(fm, gen, T.DEFAULT_USER_ZONE), encoding="utf-8")

    audits = T.audit_topic_pages(d, {"papers": [], "generated_at": ""})

    assert len(audits) == 1
    a = audits[0]
    assert a.bare_cites == ["@sneaky2020Ghost"]
    assert a.ok is False           # bare_cites 非空必须让 ok 判负，--verify 才会报错退出


def test_audit_detects_bare_citation_glued_to_chinese(tmp_path):
    """紧贴汉字的裸引用同样要被 audit 抓到（与 _clean_text 侧同一个 Unicode `\\w` 坑：
    审计侧漏判意味着已经混进旧页面的编造引用永远不会被 --verify 报出来）。"""
    d = tmp_path / "topics"
    d.mkdir()
    fm = '---\ntopic: "demo"\ntitle: "标题"\n---'
    (d / "demo.md").write_text(
        T.assemble(fm, "# 标题\n\n- 研究表明@sneaky2020Ghost能降低误差", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    a = T.audit_topic_pages(d, {"papers": [], "generated_at": ""})[0]
    assert a.bare_cites == ["@sneaky2020Ghost"]
    assert a.ok is False


def test_audit_ignores_at_mentions_in_user_zone(tmp_path):
    """用户批注区里的 @提及 不算问题。

    「问下 @导师 这个说法要不要强调」是完全正常的私人笔记；全文件扫描会把生成块
    完全健康的页面报成红色。--verify 的价值全在于"报警了就一定有事"，噪音会毁掉它。
    """
    d = tmp_path / "topics"
    d.mkdir()
    fm = '---\ntopic: "demo"\ntitle: "标题"\n---'
    (d / "demo.md").write_text(
        T.assemble(fm, "# 标题\n\n- 一条干净的论断",
                   T.DEFAULT_USER_ZONE + "\n问下 @导师 这个说法要不要强调\n"),
        encoding="utf-8")
    a = T.audit_topic_pages(d, {"papers": [], "generated_at": ""})[0]
    assert a.bare_cites == []
    assert a.ok is True


# ---------------- G8：Evidence.text 在唯一构造点统一换行归一化 ----------------

def test_retrieve_evidence_normalizes_embedded_newlines():
    """G8：换行归一化必须在 Evidence 的唯一构造点（retrieve_evidence）统一处理，
    与 render_generated_block 的展示口径（那里对 e.text 做过 .replace("\\n"," ")）
    保持一致——否则 render_evidence_block（喂给 LLM）与 audit_topic_pages 的
    hl_by_key（失锚比对基准）会用带换行的原始文本，与页面上已折叠成单行的引文
    永远逐字对不上，把完全正常的证据误报成"失锚"。"""
    rec, vec = _fake_highlight_record("aKey", 0.9)
    rec["text"] = "第一行\n第二行  "
    store = _fake_store([(rec, vec)])
    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["query"])

    evs = T.retrieve_evidence(spec, store, _FakeEmbedClient(), min_sim=0.5)

    assert len(evs) == 1
    assert evs[0].text == "第一行 第二行"


# ---------------- 合成输出格式跑偏：重试一次 ----------------

class _FlakyLLM:
    """第一次返回元描述（非 JSON），第二次才给合法 JSON。"""

    def __init__(self, bad_first=True):
        self.calls = []
        self.bad_first = bad_first

    def call(self, prompt, **kw):
        self.calls.append(prompt)
        if self.bad_first and len(self.calls) == 1:
            return "已完成综述合成，输出为合法 JSON。要点：6 个小节覆盖……"
        return '{"summary": "s", "sections": []}'


def test_synthesize_retries_once_on_non_json_output():
    """claude-agent 没有原生 json_mode，实测会偶发输出「已完成合成，要点：…」这类
    元描述（整段没有花括号，首尾花括号兜底也救不回来）。这属于格式跑偏而非内容问题，
    重跑整页要重新召回+重新合成，代价远大于追加一句格式指令再试一次。"""
    llm = _FlakyLLM()
    out = T.synthesize_topic(llm, "原始 prompt")
    assert out["summary"] == "s"
    assert len(llm.calls) == 2
    assert "原始 prompt" in llm.calls[1]          # 重试仍带原始内容
    assert "第一个字符" in llm.calls[1]            # 追加了更严厉的格式指令


def test_synthesize_gives_up_after_retries():
    """重试仍不合规就如实失败，不无限重试烧额度。"""
    class _AlwaysBad:
        def __init__(self):
            self.calls = 0

        def call(self, prompt, **kw):
            self.calls += 1
            return "我无法完成这个任务。"

    llm = _AlwaysBad()
    with pytest.raises(T.TopicError):
        T.synthesize_topic(llm, "p")
    assert llm.calls == 2                          # 原始 + 重试一次，不再多


def test_synthesize_does_not_retry_when_first_output_is_valid():
    """首次就合法时不得多调一次（重试只针对格式跑偏，别平白翻倍成本）。"""
    llm = _FlakyLLM(bad_first=False)
    T.synthesize_topic(llm, "p")
    assert len(llm.calls) == 1


# ---------------- P2：新论文影响了哪些概念页（living document 的路由器） ----------------

def test_new_citekeys_from_note_picks_only_that_note():
    """只取该札记带进来的 keeper；跨月重复条目不算（它们的证据早已进过概念页）。"""
    index = {"papers": [
        {"citekey": "new1Key", "note_file": "科研札记_2026-08-17_全文精读.md", "duplicate_of": None},
        {"citekey": "new2Key", "note_file": "科研札记_2026-08-17_全文精读.md", "duplicate_of": None},
        {"citekey": "dupKey", "note_file": "科研札记_2026-08-17_全文精读.md", "duplicate_of": "old"},
        {"citekey": "oldKey", "note_file": "科研札记_2026-07_全文精读.md", "duplicate_of": None},
    ]}
    got = T.new_citekeys_from_note(index, "/abs/path/科研札记_2026-08-17_全文精读.md")
    assert got == {"new1Key", "new2Key"}


def test_affected_topics_only_flags_pages_the_paper_actually_entered():
    """判据是"新论文有没有真的挤进证据集"，不是"有点相关"。

    max_evidence/per_paper_cap 截断之后，排在名额之外的证据对页面内容毫无影响，
    为它跑一次 LLM 合成纯属浪费——一轮全量 8 页要 20+ 分钟且会撞订阅限流。
    """
    hot = _fake_highlight_record("newKey", 0.90)
    cold = _fake_highlight_record("oldKey", 0.80)
    store = _fake_store([hot, cold])
    # 只给 1 个名额：分数更高的 newKey 进得去
    spec_in = T.TopicSpec(slug="in", title="t", question="q?", queries=["q"], max_evidence=1)
    hits = T.affected_topics([spec_in], store, _FakeEmbedClient(), ["newKey"], min_sim=0.5)
    assert hits == [("in", ["newKey"])]

    # 同样 1 个名额，但新论文分数更低 → 挤不进去 → 该页不必重合成
    cold2 = _fake_highlight_record("newKey", 0.60)
    hot2 = _fake_highlight_record("oldKey", 0.95)
    store2 = _fake_store([hot2, cold2])
    assert T.affected_topics([spec_in], store2, _FakeEmbedClient(), ["newKey"], min_sim=0.5) == []


def test_affected_topics_empty_input_is_noop():
    """没有新论文时不该跑任何召回（空窗周每周都会走到这条路径）。"""
    class _Boom:
        def embed(self, texts):
            raise AssertionError("不该调用 embed")

    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["q"])
    assert T.affected_topics([spec], None, _Boom(), []) == []


def test_affected_topics_treats_recall_failure_as_affected():
    """单页召回失败时保守视为受影响：漏判（该更新的没更新）比误判（多跑一页）更难发现。"""
    class _BadEmbed:
        def embed(self, texts):
            raise RuntimeError("Ollama 挂了")

    spec = T.TopicSpec(slug="t", title="t", question="q?", queries=["q"])
    store = _fake_store([_fake_highlight_record("k", 0.9)])
    hits = T.affected_topics([spec], store, _BadEmbed(), ["newKey"], min_sim=0.5)
    assert hits == [("t", [])]


# ---------------------------------------------------------------------------
# 第 4 轮对抗审核回归：W1（lost update）
# ---------------------------------------------------------------------------

def test_build_topic_does_not_lose_annotation_written_during_synthesis(tmp_path):
    """W1（最严重·真实丢数据）：synthesize_topic 实测一页要 3-4 分钟，用户完全可能在
    这期间在 Obsidian 里编辑「我的批注」区。若落盘前仍用 LLM 调用**前**那份快照去
    合并，这段时间新写的批注会被静默覆盖——状态照样是正常的 merged，没有任何报错
    信号（编排者用仓库真实代码复现：旧批注还在、合成期间新写的批注丢失）。

    这里用"LLM.call() 内部同步改文件"精确模拟"合成还没返回、用户已经在编辑"这段
    时间窗口，不需要真线程/真等待。
    """
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir()
    path = topics_dir / "t.md"
    path.write_text(
        T.assemble('---\ntopic: t\n---', "旧正文", T.DEFAULT_USER_ZONE + "\n旧批注\n"),
        encoding="utf-8")

    rec = _fake_highlight_record("aKey", 0.9)
    store = _fake_store([rec])
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])

    class _LLMEditsFileDuringCall:
        """在 call() 内部同步改文件，代表"用户在合成耗时期间写了新批注"。"""
        def call(self, prompt, **kw):
            cur = path.read_text(encoding="utf-8")
            path.write_text(cur.replace("旧批注", "旧批注\n\n合成期间新写的批注"),
                            encoding="utf-8")
            return ('{"sections": [{"heading": "h", '
                    '"claims": [{"text": "新论断", "evidence": ["E1"]}]}]}')

    index = {"papers": [{"citekey": "aKey", "title": "T"}]}
    result = T.build_topic(spec, store=store, embed_client=_FakeEmbedClient(),
                           llm=_LLMEditsFileDuringCall(), index=index,
                           topics_dir=topics_dir, template="模板", min_sim=0.5)

    assert result.status == "merged"
    final = path.read_text(encoding="utf-8")
    assert "合成期间新写的批注" in final     # W1：这条不能丢——回归前会丢
    assert "旧批注" in final                 # 原有批注也还在
    assert "新论断" in final                 # 这次合成的新正文也确实写进去了


def test_build_topic_does_not_lose_frontmatter_key_added_during_synthesis(tmp_path):
    """第 5 轮对抗审核回归：X1（必修·仍在丢用户数据）——上一条测试锁的是正文「我的
    批注」区，这条锁 frontmatter 分支。W1 修复时 `build_frontmatter` 的 `preserved=`
    仍然传的是 970 行、LLM 调用**前**读到的旧 frontmatter；用户在合成期间（实测
    3-4 分钟）在 Obsidian Properties 面板新增的自定义键会被同一种方式静默覆盖——
    状态照样是正常的 merged，没有任何报错信号，是 W1 的同一根因在另一个分支的原样
    复现（复审 agent 用仓库真实代码复现：my_old_key survived=True，合成期间新加的
    my_new_key survived=False）。"""
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir()
    path = topics_dir / "t.md"
    path.write_text(
        T.assemble('---\ntopic: t\nmy_old_key: "kept from before"\n---',
                   "旧正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")

    rec = _fake_highlight_record("aKey", 0.9)
    store = _fake_store([rec])
    spec = T.TopicSpec(slug="t", title="标题", question="问题？", queries=["q"])

    class _LLMAddsFrontmatterKeyDuringCall:
        """在 call() 内部同步往 frontmatter 插一个新键，代表"用户在合成耗时期间在
        Obsidian Properties 面板新增了一个自定义键"（同上一条测试对正文批注的模拟，
        只是这次改的是 frontmatter 而不是正文）。"""
        def call(self, prompt, **kw):
            cur = path.read_text(encoding="utf-8")
            new = cur.replace(
                'my_old_key: "kept from before"',
                'my_old_key: "kept from before"\n'
                'my_new_key_added_during_synthesis: "added while synthesizing"')
            path.write_text(new, encoding="utf-8")
            return ('{"sections": [{"heading": "h", '
                    '"claims": [{"text": "新论断", "evidence": ["E1"]}]}]}')

    index = {"papers": [{"citekey": "aKey", "title": "T"}]}
    result = T.build_topic(spec, store=store, embed_client=_FakeEmbedClient(),
                           llm=_LLMAddsFrontmatterKeyDuringCall(), index=index,
                           topics_dir=topics_dir, template="模板", min_sim=0.5)

    assert result.status == "merged"
    from src.scholar.vault import split_frontmatter
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    assert fm.get("my_old_key") == "kept from before"                 # 老键本就该留住
    assert fm.get("my_new_key_added_during_synthesis") == \
        "added while synthesizing"      # X1：这条不能丢——回归前会丢


# ---------------------------------------------------------------------------
# 第 4 轮对抗审核回归：W2（路由前必须确认向量库不陈旧）
# ---------------------------------------------------------------------------

class _FakeFreshnessStore:
    """只实现 routing_is_stale 用到的两个方法，不需要真的向量矩阵/records。"""
    def __init__(self, warn, lag):
        self._warn = warn
        self._lag = lag

    def freshness_warning(self, index_generated_at):
        return self._warn

    def freshness_lag_seconds(self, index_generated_at):
        return self._lag


def test_routing_is_stale_false_when_store_is_current():
    assert T.routing_is_stale(_FakeFreshnessStore(None, None), "2026-08-17T09:30:00") is False


def test_routing_is_stale_true_when_lag_exceeds_threshold():
    """W2：sync_store 在 ingest 里失败被吞掉后，向量库的 source_generated_at 会停留在
    旧值——freshness_warning 判定旧、freshness_lag_seconds 量出差多少秒。差值远超
    ROUTING_STALE_SECONDS 时必须视为陈旧，调用方据此拒绝相信"召回不到 = 不影响任何页"
    的路由结论（回归前：这种情形下路由永远判定 0 命中、退出码 0，新论文的证据永久
    错过任何概念页，且没有机制回头重跑漏判批次）。"""
    store = _FakeFreshnessStore("⚠️ 向量库落后", T.ROUTING_STALE_SECONDS * 10)
    assert T.routing_is_stale(store, "2026-08-17T09:30:00") is True


def test_routing_is_stale_true_when_lag_unparseable():
    """lag 算不出幅度（时间戳格式异常）时从严当超阈值处理，与 workflow.py 对同一情形
    的既有处理口径一致——不能因为"量不出差多少"就放行。"""
    store = _FakeFreshnessStore("⚠️ 向量库落后", None)
    assert T.routing_is_stale(store, "2026-08-17T09:30:00") is True


def test_routing_is_stale_false_when_lag_within_threshold():
    """阈值语境是"这批论文的 embedding 来不来得及生成"，远小于 STALE_DEGRADE_SECONDS
    （24h，检索类只读场景的容忍度）——落后 1 秒不该触发拒绝路由。"""
    store = _FakeFreshnessStore("⚠️ 向量库落后", 1.0)
    assert T.routing_is_stale(store, "2026-08-17T09:30:00") is False


# ---------------------------------------------------------------------------
# 第 4 轮对抗审核回归：W3（build_topics.py 子进程结果解读，失败不能被三重吞掉）
# ---------------------------------------------------------------------------

def test_summarize_build_topics_run_ok_on_zero_exit():
    out = T.summarize_build_topics_run("▶ mnar-diagnosis\n  ✅ merged", "", 0)
    assert out.ok is True


def test_summarize_build_topics_run_captures_failed_pages_and_exit_code_meaning():
    """W3：此前失败时 stdout 只截尾部几行、stderr 完全不读、退出码在通知文案里被拍扁
    成"退出码 N"——生产事故里失败页的身份在任何持久化日志里都找不到，运行时只能靠
    重放 --dry-run 反推。这里验证三件事一次性收进 detail：哪几页失败（❌ 开头的行）、
    退出码对应什么故障、stderr 内容（W2 的向量库陈旧拒绝路由信息就打在这里）。"""
    stdout = "\n".join([
        "▶ mnar-diagnosis", "  ✅ merged",
        "▶ shadow-variable", "  ❌ shadow-variable",
        "▶ cross-site-transportability", "  ❌ cross-site-transportability",
    ])
    out = T.summarize_build_topics_run(stdout, "⚠️ 向量库落后于当前索引", 1)
    assert out.ok is False
    assert "❌ shadow-variable" in out.detail
    assert "❌ cross-site-transportability" in out.detail
    assert "向量库落后" in out.detail
    assert "有概念页失败或冲突" in out.detail       # 退出码 1 的人话解释


def test_summarize_build_topics_run_captures_conflict_pages_too():
    """第 5 轮对抗审核回归：X2——build_topics.py::_fmt 的 icon 表里 conflict 用的是
    `⚠️`，不是 `❌`；此前这里只匹配 `❌` 开头的行，conflict（哨兵缺失/生成块被手改，
    是三种非正常状态里最需要人工介入的一种，比 LLM 抽风更需要被点名）在摘要里完全
    隐形——退出码与 ok 判定都对，但 notify 弹窗和日志里看不出到底哪一页冲突了，还是
    得重放 --dry-run 反推，这正是本函数存在的意义。复审 agent 用"1 页 merged + 1 页
    conflict"的真实 stdout 实测复现过这个漏报。"""
    stdout = "\n".join([
        "▶ mnar-diagnosis", "  ✅ merged",
        "▶ shadow-variable", "  ⚠️ shadow-variable",
    ])
    out = T.summarize_build_topics_run(stdout, "", 1)
    assert out.ok is False
    assert "⚠️ shadow-variable" in out.detail
    assert "✅ mnar-diagnosis" not in out.detail   # 正常页不该混进"失败/冲突页"列表


def test_summarize_build_topics_run_maps_ollama_exit_code():
    """退出码 3=Ollama 没起，是可操作的诊断，不该被拍扁成"退出码 3"让人猜。"""
    out = T.summarize_build_topics_run("", "", 3)
    assert out.ok is False
    assert "Ollama" in out.detail


def test_summarize_build_topics_run_unknown_exit_code_does_not_crash():
    out = T.summarize_build_topics_run("some output", "", 99)
    assert out.ok is False
    assert "未知退出码" in out.detail


# ---------------------------------------------------------------------------
# 第 6 轮运行时复审回归：Y4（诊断信息优先于失败页枚举，避免挤出 notify 的 300 字符）
# ---------------------------------------------------------------------------

def test_summarize_build_topics_run_puts_stderr_before_flagged_pages():
    """Y4：三处调用方的 notify 弹窗把 detail 统一截到 300 字符，stderr 诊断（如 W2
    的向量库陈旧警告）必须排在失败页枚举之前，否则页数一多就会把 stderr 挤出弹窗。"""
    out = T.summarize_build_topics_run(
        "▶ a\n  ❌ a", "⚠️ 向量库落后于当前索引", 1)
    stderr_pos = out.detail.index("向量库落后")
    pages_pos = out.detail.index("失败/冲突页")
    assert stderr_pos < pages_pos


def test_summarize_build_topics_run_truncates_long_flagged_list():
    """8 页全灭这类极端场景下，失败页列表本身就可能接近/超过 300 字符——截成
    "前 N 页 + 共 M 页"，不让它自己把 stderr 挤没（也让日志里的一行摘要保持可扫）。
    不写死具体截断条数（那是实现细节），只验证"确实截断了、保留的是前缀、
    总数没丢"。"""
    slugs = ["topic-{:02d}".format(i) for i in range(8)]
    stdout = "\n".join("  ❌ {}".format(s) for s in slugs)
    out = T.summarize_build_topics_run(stdout, "", 1)
    shown = [s for s in slugs if s in out.detail]
    assert 0 < len(shown) < len(slugs)      # 确实截断了：展示了一部分，不是全部 8 页
    assert shown == slugs[:len(shown)]      # 展示的是最前面若干页（保序），不是随机子集
    assert "共 8 页" in out.detail           # 总数没丢，只是列不下


def test_summarize_build_topics_run_short_flagged_list_not_truncated():
    """回归基线：页数不多时不该被误截——W3/X2 的既有断言（子串匹配）碰巧不会暴露
    截断逻辑的越界错误，这里显式确认"未超限时原样全列"。"""
    out = T.summarize_build_topics_run(
        "\n".join(["  ❌ a", "  ⚠️ b"]), "", 1)
    assert "❌ a" in out.detail and "⚠️ b" in out.detail
    assert "共" not in out.detail          # 没有触发截断，不该出现"共 N 页"字样


# ---------------------------------------------------------------------------
# 第 4 轮对抗审核回归：W7（月度回填收尾只路由 + 合成一次，不逐月各起子进程）
# ---------------------------------------------------------------------------

def test_new_citekeys_from_notes_unions_across_months():
    """W7：--since 2023-01 --until 2026-05 的 41 个月实测因为逐月各调一次
    build_topics.py 撞了订阅限流。改法是先把多个月份的新 citekey 收集成并集，
    整次回填只路由 + 合成一次——这个函数就是"收集"这一步。"""
    index = {"papers": [
        {"citekey": "jan1", "note_file": "科研札记_2026-01_全文精读.md", "duplicate_of": None},
        {"citekey": "jan2", "note_file": "科研札记_2026-01_全文精读.md", "duplicate_of": None},
        {"citekey": "feb1", "note_file": "科研札记_2026-02_全文精读.md", "duplicate_of": None},
        {"citekey": "dup", "note_file": "科研札记_2026-02_全文精读.md", "duplicate_of": "old"},
    ]}
    got = T.new_citekeys_from_notes(index, [
        "/abs/科研札记_2026-01_全文精读.md", "/abs/科研札记_2026-02_全文精读.md"])
    assert got == {"jan1", "jan2", "feb1"}


def test_new_citekeys_from_notes_empty_list_is_noop():
    assert T.new_citekeys_from_notes({"papers": []}, []) == set()


# ---------------------------------------------------------------------------
# 第 4 轮对抗审核回归：W8（证据池打满后，失败页不能只靠"新论文恰好命中"才有重试机会）
# ---------------------------------------------------------------------------

def test_stale_topic_slugs_flags_pages_past_threshold(tmp_path):
    """一次性合成失败后，若之后再没有新论文恰好挤进它的证据集，页面会永远定格在
    旧版本——这里验证"超过日历阈值"这条不依赖路由命中的兜底能识别出这类页面。"""
    d = tmp_path / "topics"
    d.mkdir()
    old_fm = '---\ntopic: "old"\ntitle: "旧页"\ngenerated_at: "2026-06-01T00:00:00"\n---'
    fresh_fm = '---\ntopic: "fresh"\ntitle: "新页"\ngenerated_at: "2026-08-16T00:00:00"\n---'
    (d / "old.md").write_text(T.assemble(old_fm, "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")
    (d / "fresh.md").write_text(T.assemble(fresh_fm, "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")
    specs = [T.TopicSpec(slug="old", title="旧页", question="q?", queries=["q"]),
             T.TopicSpec(slug="fresh", title="新页", question="q?", queries=["q"])]

    got = T.stale_topic_slugs(d, specs, max_age_days=30, now=datetime(2026, 8, 17))
    assert got == ["old"]


def test_stale_topic_slugs_ignores_retired_pages(tmp_path):
    """已从 topics.yaml 移除的页不该被强制拉回来重跑——它就该定格在最后一次生成的
    内容上（同 render_topics_index 对退役页的处理）。"""
    d = tmp_path / "topics"
    d.mkdir()
    fm = '---\ntopic: "retired"\ntitle: "退役页"\ngenerated_at: "2020-01-01T00:00:00"\n---'
    (d / "retired.md").write_text(T.assemble(fm, "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")
    got = T.stale_topic_slugs(d, [], max_age_days=30, now=datetime(2026, 8, 17))
    assert got == []


def test_stale_topic_slugs_tolerates_missing_generated_at(tmp_path):
    """generated_at 解析不出（缺失/格式异常）不强制重跑——那类问题该由 --verify 的
    死键/失锚检测发现，不该让格式问题本身触发一次没有意义的重跑。"""
    d = tmp_path / "topics"
    d.mkdir()
    fm = '---\ntopic: "weird"\ntitle: "怪页"\n---'   # 没有 generated_at
    (d / "weird.md").write_text(T.assemble(fm, "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")
    specs = [T.TopicSpec(slug="weird", title="怪页", question="q?", queries=["q"])]
    got = T.stale_topic_slugs(d, specs, max_age_days=30, now=datetime(2026, 8, 17))
    assert got == []


def _write_stale_page(d, filename, slug, generated_at):
    fm = '---\ntopic: "{}"\ntitle: "t"\ngenerated_at: "{}"\n---'.format(slug, generated_at)
    (d / filename).write_text(T.assemble(fm, "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")


def test_stale_topic_slugs_sorted_oldest_first(tmp_path):
    """第 5 轮对抗审核回归：X3 兜底要"优先补最久没更新的页"，前提是返回顺序本身就是
    按陈旧程度排的——不能是磁盘 glob 的文件名顺序（那与陈旧程度无关）。这里故意让
    文件名字母序与 generated_at 时间序相反，只有真按时间排序才能过。"""
    d = tmp_path / "topics"
    d.mkdir()
    # 文件名字母序：aaa < mmm < zzz；但按 generated_at，zzz 最旧、aaa 最新。
    _write_stale_page(d, "aaa.md", "aaa", "2026-07-01T00:00:00")
    _write_stale_page(d, "mmm.md", "mmm", "2026-06-01T00:00:00")
    _write_stale_page(d, "zzz.md", "zzz", "2026-05-01T00:00:00")
    specs = [T.TopicSpec(slug=s, title="t", question="q?", queries=["q"])
            for s in ("aaa", "mmm", "zzz")]

    got = T.stale_topic_slugs(d, specs, max_age_days=30, now=datetime(2026, 8, 17))
    assert got == ["zzz", "mmm", "aaa"], "必须按 generated_at 从旧到新排，不是文件名序"


def test_stale_topic_slugs_max_count_caps_to_oldest(tmp_path):
    """X3（必修·会撞限流）：W8 兜底此前是无条件全量拉回，同一批上线的页容易在同一个
    日历阈值上同时过期（复审 agent 用仓库真实 config/topics.yaml 复现：8 页全部命中），
    单次运行会连续跑数十分钟 LLM 合成，撞订阅限流。max_count 必须只放行"最旧的 N 个"，
    且这个"最旧"是按 generated_at 判的，不是恰好排在文件名/glob 顺序前面的 N 个
    ——否则退化成没有优先级的随机限流，补课顺序无法收敛。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_stale_page(d, "aaa.md", "aaa", "2026-07-01T00:00:00")   # 最新（仍超期）
    _write_stale_page(d, "mmm.md", "mmm", "2026-06-01T00:00:00")   # 中间
    _write_stale_page(d, "zzz.md", "zzz", "2026-05-01T00:00:00")   # 最旧
    specs = [T.TopicSpec(slug=s, title="t", question="q?", queries=["q"])
            for s in ("aaa", "mmm", "zzz")]

    got = T.stale_topic_slugs(d, specs, max_age_days=30, now=datetime(2026, 8, 17),
                              max_count=2)
    assert got == ["zzz", "mmm"], "必须是最旧的两个，不能是文件名序里靠前的两个"


def test_stale_topic_slugs_max_count_none_or_zero_is_unlimited(tmp_path):
    """max_count 缺省（None）与 build_topics.py `--stale-max-per-run<=0` 传达的"不限"
    是同一语义，不该意外截断——回归写清楚这条边界，避免以后有人把 0 当"选 0 个"改坏。"""
    d = tmp_path / "topics"
    d.mkdir()
    _write_stale_page(d, "aaa.md", "aaa", "2026-07-01T00:00:00")
    _write_stale_page(d, "zzz.md", "zzz", "2026-05-01T00:00:00")
    specs = [T.TopicSpec(slug=s, title="t", question="q?", queries=["q"]) for s in ("aaa", "zzz")]

    assert T.stale_topic_slugs(d, specs, max_age_days=30, now=datetime(2026, 8, 17)) == \
        ["zzz", "aaa"]
    assert T.stale_topic_slugs(d, specs, max_age_days=30, now=datetime(2026, 8, 17),
                               max_count=None) == ["zzz", "aaa"]


# ---------------------------------------------------------------------------
# 第 6 轮运行时复审回归：Y1（W2 向量库陈旧判断不该连带挡住 W8 日历兜底）
# ---------------------------------------------------------------------------

def test_plan_synthesis_targets_uses_routing_hits_when_routing_ok():
    plan = T.plan_synthesis_targets(
        [("a", ["k1"]), ("b", ["k2"])], [], routing_ok=True)
    assert plan.want_slugs == ["a", "b"]
    assert plan.stale_selected == [] and plan.stale_held_back == []


def test_plan_synthesis_targets_calendar_catchup_survives_stale_vector_store():
    """Y1 核心回归：向量库陈旧（routing_ok=False，见 routing_is_stale）时，路由命中
    必须被视为不可信而忽略，但日历兜底（stale_all，不碰向量库）必须照常生效——这正
    是第 6 轮运行时复审揪出的交互 bug：build_topics.py 此前的顺序会让 W2 的向量库
    陈旧判断把 W8 兜底一起挡住，而 W8 存在的全部理由就是给"因故障错过更新的页"一条
    不依赖路由的补课通道。"""
    plan = T.plan_synthesis_targets(
        [("a", ["k1"])], ["b", "c"], routing_ok=False)
    assert "a" not in plan.want_slugs          # 路由命中不可信，不采纳
    assert plan.want_slugs == ["b", "c"]        # 日历兜底照常执行
    assert plan.stale_selected == ["b", "c"]


def test_plan_synthesis_targets_stale_excludes_slugs_already_routed():
    """已被路由命中的 slug 不该在 stale_selected 里重复出现——否则展示文案与
    --stale-max-per-run 的名额会把它重复计一次。"""
    plan = T.plan_synthesis_targets(
        [("a", ["k1"])], ["a", "b"], routing_ok=True)
    assert plan.want_slugs == ["a", "b"]
    assert plan.stale_selected == ["b"]


def test_plan_synthesis_targets_truncates_stale_by_max_per_run():
    """X3 的既有语义原样保留：截断前必须已经是按陈旧程度排好的（调用方负责），
    这里只验证"排前面的留下、排后面的进 held_back"。"""
    plan = T.plan_synthesis_targets(
        [], ["a", "b", "c"], routing_ok=True, stale_max_per_run=1)
    assert plan.stale_selected == ["a"]
    assert plan.stale_held_back == ["b", "c"]
    assert plan.want_slugs == ["a"]


def test_plan_synthesis_targets_unlimited_when_max_per_run_none_or_zero():
    """None（未指定）与 <=0（build_topics.py `--stale-max-per-run<=0` 传达的"不限"）
    是同一语义，写法对齐 T.stale_topic_slugs 对 max_count 的既有回归。"""
    assert T.plan_synthesis_targets(
        [], ["a", "b"], routing_ok=True).stale_selected == ["a", "b"]
    assert T.plan_synthesis_targets(
        [], ["a", "b"], routing_ok=True, stale_max_per_run=0).stale_selected == ["a", "b"]


# ---------------------------------------------------------------------------
# 第 6 轮运行时复审回归：Y2（跨触发失败冷却，别让重放回退链烧配额）
# ---------------------------------------------------------------------------

def test_filter_retry_cooldown_allows_all_when_state_file_missing(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    allowed, cooling = T.filter_retry_cooldown(d, ["a", "b"])
    assert allowed == ["a", "b"] and cooling == []


def test_filter_retry_cooldown_allows_all_when_topics_dir_missing(tmp_path):
    """topics_dir 本身都还不存在（全新仓库，从没成功生成过一页）时更要退化成无
    冷却——不能因为目录不存在就报错，见 T._load_retry_state 顶部注释。"""
    d = tmp_path / "never-created"
    allowed, cooling = T.filter_retry_cooldown(d, ["a"])
    assert allowed == ["a"] and cooling == []


def test_filter_retry_cooldown_survives_corrupted_json(tmp_path):
    """状态文件损坏（非法 JSON，如写入中途被杀）必须退化成"无冷却"，不能抛异常——
    这是纯运行状态，不该因为自己坏了就连带阻断正常合成。"""
    d = tmp_path / "topics"
    d.mkdir()
    (d / T.RETRY_STATE_FILENAME).write_text("{not valid json", encoding="utf-8")
    allowed, cooling = T.filter_retry_cooldown(d, ["a"])
    assert allowed == ["a"] and cooling == []


def test_filter_retry_cooldown_survives_non_utf8_bytes(tmp_path):
    """非法 UTF-8 是"损坏"的另一种表现形式，UnicodeDecodeError 是 ValueError 子类、
    不是 OSError，只挡 OSError 会漏掉这一类损坏。"""
    d = tmp_path / "topics"
    d.mkdir()
    (d / T.RETRY_STATE_FILENAME).write_bytes(b"\xff\xfe\x00bad")
    allowed, cooling = T.filter_retry_cooldown(d, ["a"])
    assert allowed == ["a"] and cooling == []


def test_filter_retry_cooldown_survives_wrong_top_level_shape(tmp_path):
    """内容是合法 JSON 但顶层不是对象（如变成了列表）同样要退化成无冷却，而不是
    把列表元素误当 slug 处理。"""
    d = tmp_path / "topics"
    d.mkdir()
    (d / T.RETRY_STATE_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
    allowed, cooling = T.filter_retry_cooldown(d, ["a"])
    assert allowed == ["a"] and cooling == []


def test_record_failure_then_filter_blocks_within_cooldown_window(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    t0 = datetime(2026, 8, 17, 9, 0, 0)
    T.record_topic_failure(d, "shadow-variable", now=t0)

    allowed, cooling = T.filter_retry_cooldown(
        d, ["shadow-variable", "mnar-diagnosis"], now=t0 + timedelta(minutes=30))
    assert allowed == ["mnar-diagnosis"]
    assert len(cooling) == 1 and cooling[0][0] == "shadow-variable"
    assert 0 < cooling[0][1] <= 1.0     # 第 1 次失败冷却 1 小时，30 分钟后还剩不到 1 小时


def test_filter_retry_cooldown_allows_after_window_expires(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    t0 = datetime(2026, 8, 17, 9, 0, 0)
    T.record_topic_failure(d, "shadow-variable", now=t0)

    allowed, cooling = T.filter_retry_cooldown(
        d, ["shadow-variable"], now=t0 + timedelta(hours=1, minutes=1))
    assert allowed == ["shadow-variable"] and cooling == []


def test_record_topic_failure_escalates_cooldown_with_consecutive_failures(tmp_path):
    """连续第 2 次失败要退避到 4 小时档，不是重新计一次 1 小时——否则冷却机制对
    "持续性故障"（订阅欠费这类）形同虚设，每小时都会再被拉去重放一次注定失败的
    回退链。"""
    d = tmp_path / "topics"
    d.mkdir()
    t0 = datetime(2026, 8, 17, 9, 0, 0)
    T.record_topic_failure(d, "shadow-variable", now=t0)
    t1 = t0 + timedelta(hours=1, minutes=1)      # 过了第 1 档的 1h
    T.record_topic_failure(d, "shadow-variable", now=t1)   # 第 2 次失败：升到 4h 档

    # 若冷却是固定 1 小时（未按连续失败次数升档），此刻（第 2 次失败后 1 小时）
    # 早该放行；升档后仍应在冷却中。
    allowed, cooling = T.filter_retry_cooldown(
        d, ["shadow-variable"], now=t1 + timedelta(hours=1))
    assert allowed == []
    assert cooling[0][0] == "shadow-variable"


def test_record_topic_success_clears_cooldown(tmp_path):
    d = tmp_path / "topics"
    d.mkdir()
    t0 = datetime(2026, 8, 17, 9, 0, 0)
    T.record_topic_failure(d, "shadow-variable", now=t0)
    T.record_topic_success(d, "shadow-variable")

    allowed, cooling = T.filter_retry_cooldown(
        d, ["shadow-variable"], now=t0 + timedelta(minutes=1))
    assert allowed == ["shadow-variable"] and cooling == []


def test_record_topic_success_on_slug_without_history_is_noop(tmp_path):
    """成功页此前从没失败过（状态里没有它的记录）时，清除操作不该报错。"""
    d = tmp_path / "topics"
    d.mkdir()
    T.record_topic_success(d, "never-failed")     # 不抛异常即通过
    allowed, cooling = T.filter_retry_cooldown(d, ["never-failed"])
    assert allowed == ["never-failed"] and cooling == []


def test_retry_state_file_not_picked_up_as_topic_page(tmp_path):
    """冷却状态文件与真实概念页共存于同一个 topics_dir 时，stale_topic_slugs/
    audit_topic_pages 的 glob("*.md") 不该被它污染——它连 .md 后缀都不是，双重保险
    （见 T.RETRY_STATE_FILENAME 顶部注释）。"""
    d = tmp_path / "topics"
    d.mkdir()
    T.record_topic_failure(d, "ghost-page")        # 落一份 .retry_state.json
    fm = '---\ntopic: "real"\ntitle: "真实页"\ngenerated_at: "2020-01-01T00:00:00"\n---'
    (d / "real.md").write_text(T.assemble(fm, "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")
    specs = [T.TopicSpec(slug="real", title="真实页", question="q?", queries=["q"])]

    got = T.stale_topic_slugs(d, specs, max_age_days=30, now=datetime(2026, 8, 17))
    assert got == ["real"]

    audits = T.audit_topic_pages(d, {"papers": []})
    assert [a.slug for a in audits] == ["real"]


# ---------------------------------------------------------------------------
# 第 4 轮对抗审核回归：B1（退役 topic 在 vault 侧也要有醒目标记）
# ---------------------------------------------------------------------------

def test_sync_topics_to_vault_marks_retired_pages_when_specs_given(tmp_path):
    """B1：退役页在 vault 侧此前与仍在更新的页面长得一模一样，唯一提示只写在
    notes_dir/topics/INDEX.md 里，vault 重度用户几乎不会翻那个文件。给了 specs 时，
    不在其中的 slug 在 vault 侧打 retired: true + 退役 tag。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    (src / "current.md").write_text(
        T.assemble('---\ntopic: "current"\ntitle: "在用"\n---', "正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    (src / "dropped.md").write_text(
        T.assemble('---\ntopic: "dropped"\ntitle: "已下线"\n---', "正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    specs = [T.TopicSpec(slug="current", title="在用", question="q?", queries=["q"])]

    T.sync_topics_to_vault(notes_dir, vault_dir, {}, specs=specs)

    from src.scholar.vault import split_frontmatter
    current_fm, _ = split_frontmatter(
        (vault_dir / T.VAULT_TOPICS_DIR / "current.md").read_text(encoding="utf-8"))
    dropped_fm, _ = split_frontmatter(
        (vault_dir / T.VAULT_TOPICS_DIR / "dropped.md").read_text(encoding="utf-8"))
    assert not current_fm.get("retired")
    assert dropped_fm.get("retired") is True
    assert "札记/概念页-退役" in (dropped_fm.get("tags") or [])


def test_sync_topics_to_vault_skips_retired_marking_without_specs(tmp_path):
    """specs 不给（默认 None）时行为与此前完全一致——当前唯一调用方 build_vault.py
    还没接这个参数，不能因为这个可选功能改变现有调用方的行为。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    (src / "dropped.md").write_text(
        T.assemble('---\ntopic: "dropped"\ntitle: "已下线"\n---', "正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    T.sync_topics_to_vault(notes_dir, vault_dir, {})
    from src.scholar.vault import split_frontmatter
    fm, _ = split_frontmatter(
        (vault_dir / T.VAULT_TOPICS_DIR / "dropped.md").read_text(encoding="utf-8"))
    assert "retired" not in fm


# ---------------------------------------------------------------------------
# 事故复现回归：best-effort 触发器绝不能对着测试用的 tmp_path 发起真实子进程
# ---------------------------------------------------------------------------

def test_notes_dir_is_production_rejects_tmp_path(tmp_path):
    """事故实测：test_read_pdf_cli.py 直调 read_pdf._rebuild_month(tmp_path, ...) 时，
    W6 新增的 best-effort 触发器曾拿 tmp_path 下的 note 文件名去匹配**生产**索引，
    撞上生产库里同名的当月文件，真的起了一个指向生产 config 的 build_topics.py 子
    进程并挂起 13 分钟（pytest 临时目录一定不在仓库 output/ 下，这里直接用 tmp_path
    模拟同样的场景）。"""
    assert T.notes_dir_is_production(tmp_path) is False
    assert T.notes_dir_is_production(tmp_path / "notes") is False


def test_notes_dir_is_production_accepts_repo_output_subdir():
    """真正的生产 notes_dir（仓库 output/ 下）必须放行，否则三条入库脚本在生产环境
    里也会把 P2 触发器整个跳过。"""
    from src.scholar.paths import repo_path
    assert T.notes_dir_is_production(repo_path("output/scholar_notes")) is True
    assert T.notes_dir_is_production(repo_path("output")) is True


# ---------------------------------------------------------------------------
# 第 6 轮运行时复审回归：Y3（read_pdf.py 的 best-effort 触发器也要 notify）
#
# 这几个用例测的是 scripts/read_pdf.py::_sync_topics_best_effort，按本仓库既有
# 分工本该放进 test_read_pdf_cli.py（该文件已有同款 importlib 装载 read_pdf.py 的
# 先例，见其 `_load()`）——但第 6 轮对抗审核这一轮的改动白名单只允许触碰
# test_topics.py，这里借用同一套装载方式，把它落在本文件末尾单独一节。
# ---------------------------------------------------------------------------

def _load_read_pdf_cli():
    import importlib.util
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "read_pdf_cli_y3", repo / "scripts" / "read_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sync_topics_best_effort_notifies_on_timeout(monkeypatch):
    """Y3：2026-08-17 运行时复审现场证伪"人盯着终端"假设——`ps aux` 实际看到真正在
    跑这条路径的是自动权限模式的 Claude Code 会话，不是人盯着终端，warning 级日志
    完全可能被会话自己的输出吞掉。子进程超时也要 notify，不能只 logger.warning。"""
    import subprocess
    M = _load_read_pdf_cli()
    calls = []
    monkeypatch.setattr("src.utils.notify.notify",
                        lambda title, text: calls.append((title, text)))
    monkeypatch.setattr("src.scholar.topics.notes_dir_is_production", lambda _d: True)

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="build_topics.py", timeout=2400)
    monkeypatch.setattr("subprocess.run", _boom)

    M._sync_topics_best_effort(Path("/fake/notes"), "note.md")

    assert len(calls) == 1
    assert calls[0][0] == "Scholar 手动精读"
    assert "超时" in calls[0][1]


def test_sync_topics_best_effort_notifies_on_subprocess_exception(monkeypatch):
    M = _load_read_pdf_cli()
    calls = []
    monkeypatch.setattr("src.utils.notify.notify",
                        lambda title, text: calls.append((title, text)))
    monkeypatch.setattr("src.scholar.topics.notes_dir_is_production", lambda _d: True)

    def _boom(*a, **k):
        raise OSError("boom")
    monkeypatch.setattr("subprocess.run", _boom)

    M._sync_topics_best_effort(Path("/fake/notes"), "note.md")

    assert len(calls) == 1
    assert calls[0][0] == "Scholar 手动精读"
    assert "异常" in calls[0][1]


def test_sync_topics_best_effort_notifies_when_outcome_not_ok(monkeypatch):
    """子进程本身正常退出，但概念页有页面失败/冲突（summarize_build_topics_run
    判定 ok=False）——同样要 notify，不是只有子进程崩溃才算数。"""
    import subprocess
    M = _load_read_pdf_cli()
    calls = []
    monkeypatch.setattr("src.utils.notify.notify",
                        lambda title, text: calls.append((title, text)))
    monkeypatch.setattr("src.scholar.topics.notes_dir_is_production", lambda _d: True)

    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="▶ mnar-diagnosis\n  ❌ mnar-diagnosis", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: completed)

    M._sync_topics_best_effort(Path("/fake/notes"), "note.md")

    assert len(calls) == 1
    assert calls[0][0] == "Scholar 手动精读"
    assert "未更新成功" in calls[0][1]


def test_sync_topics_best_effort_does_not_notify_on_success(monkeypatch):
    """不多打扰人：概念页全部成功时不该弹窗——Y3 修法原文强调"警告不会多到扰民，
    只在概念页合成真失败时才弹"。"""
    import subprocess
    M = _load_read_pdf_cli()
    calls = []
    monkeypatch.setattr("src.utils.notify.notify",
                        lambda title, text: calls.append((title, text)))
    monkeypatch.setattr("src.scholar.topics.notes_dir_is_production", lambda _d: True)

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="▶ mnar-diagnosis\n  ✅ merged", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: completed)

    M._sync_topics_best_effort(Path("/fake/notes"), "note.md")

    assert calls == []


def test_sync_topics_best_effort_skips_notify_when_notes_dir_not_production(monkeypatch):
    """notes_dir 不在生产目录下（测试隔离场景）时整个触发器直接跳过——不该发子
    进程、更不该 notify（同 W6/事故复现回归对 notes_dir_is_production 的既有约束）。"""
    M = _load_read_pdf_cli()
    calls = []
    monkeypatch.setattr("src.utils.notify.notify",
                        lambda title, text: calls.append((title, text)))

    def _boom(*a, **k):
        raise AssertionError("不该发起子进程")
    monkeypatch.setattr("subprocess.run", _boom)

    M._sync_topics_best_effort(Path("/fake/notes"), "note.md")   # 真实判定：非生产目录

    assert calls == []


# ---------------------------------------------------------------------------
# P3 第 1 轮 B4：sync_topics_to_vault 的 lint 分支（此前零测试）
#
# `is_lint` / `cssclasses=["lint-report"]` / 退役豁免 / prune 保护四件事都是人工
# 验证过的，但没有测试网住——改一次 `is_page or is_lint` 的判断顺序就可能让整份
# 知识层 lint 报告悄悄从 vault 里消失（Obsidian 才是人真正读它的地方）。
# ---------------------------------------------------------------------------

def _lint_src(src_dir, body="- `[@a2024Key]` 某篇撤稿论文 与 [@ghost2020Nope]"):
    (src_dir / "_lint.md").write_text(
        T.assemble('---\ntitle: "知识层 lint 报告"\ntype: "lint"\n---',
                   body, T.DEFAULT_USER_ZONE, generator="scripts/lint_notes.py"),
        encoding="utf-8")


def test_sync_carries_the_lint_report_into_vault_with_wiki_links(tmp_path):
    """报告只落在 output/scholar_notes/topics/ 等于没人看，里面逐条列出的 citekey
    也点不开。转成 wiki 链接后，"这篇撤稿论文正在给哪几页当地基"才真的能点过去。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    _lint_src(src)
    rep = T.sync_topics_to_vault(notes_dir, vault_dir, {"a2024Key": "a2024Key"})
    dst = vault_dir / T.VAULT_TOPICS_DIR / "_lint.md"
    assert dst.exists() and rep["new"] == 1
    body = dst.read_text(encoding="utf-8")
    assert "[[a2024Key]]" in body
    assert "[@ghost2020Nope]" in body          # 不在 vault 的保持原样，别造死链
    from src.scholar.vault import split_frontmatter
    fm, _ = split_frontmatter(body)
    assert fm["cssclasses"] == ["lint-report"]   # 概念页那边是 ["topic-page"]
    assert "topic" not in fm                     # 报告不是概念页，别混进概念页身份


def test_lint_report_is_never_marked_retired_but_a_real_dropped_page_is(tmp_path):
    """lint 报告永远不在 topics.yaml 里。不豁免它就会每轮被打上 retired: true +
    「已退役」标签，读起来像一份作废文件——实际上它是每轮重新生成的最新报告。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    _lint_src(src)
    (src / "dropped.md").write_text(
        T.assemble('---\ntopic: "dropped"\ntitle: "已下线"\n---', "正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    (src / "current.md").write_text(
        T.assemble('---\ntopic: "current"\ntitle: "在用"\n---', "正文", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    specs = [T.TopicSpec(slug="current", title="在用", question="q?", queries=["q"])]
    T.sync_topics_to_vault(notes_dir, vault_dir, {}, specs=specs)

    from src.scholar.vault import split_frontmatter
    out = vault_dir / T.VAULT_TOPICS_DIR

    def _fm(name):
        return split_frontmatter((out / name).read_text(encoding="utf-8"))[0]

    assert "retired" not in _fm("_lint.md")                  # 豁免
    assert "札记/概念页-退役" not in (_fm("_lint.md").get("tags") or [])
    assert _fm("dropped.md").get("retired") is True          # 对照组：真的退役页要打
    assert "retired" not in _fm("current.md")


def test_vault_lint_copy_is_pruned_after_the_source_is_deleted(tmp_path):
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    _lint_src(src)
    T.sync_topics_to_vault(notes_dir, vault_dir, {})
    dst = vault_dir / T.VAULT_TOPICS_DIR / "_lint.md"
    assert dst.exists()

    (src / "_lint.md").unlink()
    rep = T.sync_topics_to_vault(notes_dir, vault_dir, {})
    assert rep["pruned"] == ["_lint.md"]
    assert not dst.exists()


def test_annotated_vault_lint_copy_is_never_deleted(tmp_path):
    """同落选笔记的既有规矩：写过东西的只报告不删，绝不替用户删。
    这份报告尤其容易被写批注——"这条我核对过，不是真冲突"正是它的用法。"""
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    src = notes_dir / T.TOPICS_DIRNAME
    src.mkdir(parents=True)
    _lint_src(src)
    T.sync_topics_to_vault(notes_dir, vault_dir, {})
    dst = vault_dir / T.VAULT_TOPICS_DIR / "_lint.md"
    dst.write_text(dst.read_text(encoding="utf-8").replace(
        T.USER_HEADING, T.USER_HEADING + "\n\n- ack: ab12cd34 这条我看过了"), encoding="utf-8")

    (src / "_lint.md").unlink()
    rep = T.sync_topics_to_vault(notes_dir, vault_dir, {})
    assert rep["pruned"] == []
    assert dst.exists() and "ack: ab12cd34" in dst.read_text(encoding="utf-8")
    assert any("_lint.md" in s for s in rep["skipped"])
