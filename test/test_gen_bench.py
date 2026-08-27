# -*- coding: utf-8 -*-
"""生成侧 bench 的有效性锚（2026-08-27）。

首跑全库 9 页拿到 100% 接地。**一个永远报满分的指标等于没有指标**——本文件的存在
就是为了证伪这件事：往页面里注入已知失真，bench 必须报出来；注入合法写法，bench
必须不误报。

这条纪律是有来历的：rag_bench 的 acronym case 集就是因为"9 条 case 全大写、照样
满分"而漏掉了小写缩写失明，同一个坑连踩两次（见 oss_alignment_audit_2026-08.md）。
先证明能抓，满分才有意义。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import gen_bench as GB  # noqa: E402
from src.scholar.grounding import numbers_match  # noqa: E402


def _page(claims: str, evidence: str, fm: str = "") -> str:
    return (
        "---\ntopic: \"t\"\n{}---\n\n"
        "<!-- BEGIN GENERATED v1 h=deadbeef -->\n"
        "# 标题\n\n{}\n\n"
        "## 本页证据（2 条 · 2 篇）\n\n{}\n"
        "<!-- END GENERATED -->\n\n"
        "## 我的批注\n\n- 用户自己写的 0.999 不该被评测\n"
    ).format(fm, claims, evidence)


def _ev(ref: int, ck: str, title: str, quote: str) -> str:
    return ("- ● **E{}** `[@{}]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2026-01.md:1\n"
            "  <small>{}</small>\n  > {}\n").format(ref, ck, title, quote)


def _audit(tmp_path, claims, evidence, fm=""):
    p = tmp_path / "t.md"
    p.write_text(_page(claims, evidence, fm), encoding="utf-8")
    return GB.audit_page(p)


EV_TWO = (_ev(1, "aa2026Alpha", "Alpha 研究", "影子变量区间中点平均绝对误差 0.06。")
          + _ev(2, "bb2026Beta", "Beta 研究", "队列共 17,775 例，AUC 为 0.488。"))


# --- 必须抓到的失真 -------------------------------------------------------

def test_catches_wrong_magnitude(tmp_path):
    """抄错量级：证据是 0.06，论断写成 0.6。"""
    r = _audit(tmp_path, "- 该方法误差为 0.6。 [@aa2026Alpha]", EV_TWO)
    assert r["numeric_rate"] < 1.0
    assert "0.6" in r["details"][0]["ungrounded"]


def test_catches_cross_paper_attribution(tmp_path):
    """张冠李戴：0.488 是 Beta 的数字，论断挂在 Alpha 头上。"""
    r = _audit(tmp_path, "- Alpha 报告 AUC 0.488。 [@aa2026Alpha]", EV_TWO)
    assert r["numeric_rate"] < 1.0
    assert "0.488" in r["details"][0]["ungrounded"]


def test_catches_invented_number(tmp_path):
    """凭空捏造：证据里根本没有 93.7 这个数。"""
    r = _audit(tmp_path, "- 准确率达到 93.7%。 [@bb2026Beta]", EV_TWO)
    assert r["numeric_rate"] < 1.0


# --- 必须不误报的合法写法 -------------------------------------------------

def test_grounded_number_passes(tmp_path):
    r = _audit(tmp_path, "- 误差为 0.06。 [@aa2026Alpha]", EV_TWO)
    assert r["numeric_rate"] == 1.0
    assert not r["details"]


def test_percent_form_is_equivalent(tmp_path):
    """证据写 0.488、论断写 48.8% 是同一个事实，不该判失真。"""
    r = _audit(tmp_path, "- AUC 为 48.8%。 [@bb2026Beta]", EV_TWO)
    assert r["numeric_rate"] == 1.0


def test_thousands_separator_is_equivalent(tmp_path):
    r = _audit(tmp_path, "- 队列共 17775 例。 [@bb2026Beta]", EV_TWO)
    assert r["numeric_rate"] == 1.0


def test_evidence_ref_is_not_a_number(tmp_path):
    """E2 是证据编号，不是数字事实。"""
    r = _audit(tmp_path, "- 如 E2 所述，误差 0.06。 [@aa2026Alpha]", EV_TWO)
    assert r["numeric_total"] == 1
    assert r["numeric_rate"] == 1.0


def test_latin_suffixed_number_is_skipped(tmp_path):
    """4CE 是联盟名（Consortium for Clinical Characterization of COVID-19 by EHR），
    里面的 4 不是数量——曾被当作未接地数字报出来。

    ⚠️ 断言必须锁住**抽出了几个数字**。原写法是
    `assert "4" not in r["details"][0][...] if r["details"] else True`，它解析成
    `assert (A if C else B)`——details 一旦为空就退化成 `assert True`，实现坏了也能过。
    """
    r = _audit(tmp_path, "- 覆盖 15 个 4CE 站点，误差 0.06。 [@aa2026Alpha]", EV_TWO)
    assert r["numeric_total"] == 2, "应只抽出 15 与 0.06，4CE 的 4 不算数字"
    assert all("4" != u for d in r["details"] for u in d["ungrounded"])


def test_chinese_comma_is_not_a_thousands_separator(tmp_path):
    """NFKC 会把中文逗号归一成半角；`241，与` 不能被读成千分位数字 `241,`。"""
    r = _audit(tmp_path, "- 相加为 241，与正文不一致。 [@bb2026Beta]", EV_TWO)
    nums = [u for d in r["details"] for u in d["ungrounded"]]
    assert "241," not in nums
    assert "241" in nums, "241 本身应被当成一个数字并判未接地"


def test_user_zone_inside_gen_block_is_not_evaluated(tmp_path):
    """批注区是用户自己写的，把它算进来等于拿用户的话去考模型。

    ⚠️ 这一条原来是假阳性：批注写在 `GEN_END` **之后**，早被 `if not in_gen` 挡住，
    `zone == "user"` 那条分支**从来没被执行过**——变异测试里把整个批注区排除逻辑
    删掉，49 个测试照样全绿。要真测它，批注标题必须出现在生成块**内部**。
    """
    from src.scholar.page_parse import parse_page
    p = tmp_path / "u.md"
    p.write_text(
        "---\ntopic: \"t\"\n---\n\n"
        "<!-- BEGIN GENERATED v1 h=aa -->\n"
        "# T\n\n- 真论断，误差 0.06。 [@aa2026Alpha]\n\n"
        "## 我的批注\n\n- 用户写的 99.9 与 [@zz2026Mine] 不该被收\n\n"
        "<!-- END GENERATED -->\n",
        encoding="utf-8")
    page = parse_page(p)
    assert len(page.claims) == 1
    assert "zz2026Mine" not in page.claims[0].citekeys


def test_meta_number_is_exempt(tmp_path):
    """"这 60 条证据" 说的是页面元信息，不来自任何一条证据。"""
    r = _audit(tmp_path, "- 60 条证据中仅一条提到该做法。 [@aa2026Alpha]", EV_TWO,
               fm="n_evidence: 60\n")
    assert r["numeric_rate"] == 1.0


# --- 派生量单列一档，不混进失真 -------------------------------------------

def test_derived_ratio_is_classified_not_counted_as_failure(tmp_path):
    """论断在证据数字上做算术得出的比值，按定义不在原句里，不是失真。"""
    r = _audit(tmp_path, "- 两者相差约 8.7 倍。 [@bb2026Beta]", EV_TWO)
    assert r["numeric_derived"] == 1
    assert r["numeric_rate"] is None or r["numeric_rate"] == 1.0
    assert not r["details"][0]["ungrounded"]


def test_derived_still_visible_in_details(tmp_path):
    """派生量不静默豁免——算错的派生数字正是该抓的，必须留在明细里供抽查。"""
    r = _audit(tmp_path, "- 两者相差约 8.7 倍。 [@bb2026Beta]", EV_TWO)
    assert "8.7" in r["details"][0]["derived"]


# --- 解析回归：qa 页那个真 bug -------------------------------------------

def test_repeated_citekey_keeps_quotes_on_the_right_paper(tmp_path):
    """同一 citekey 多条证据时，续行必须归给当前证据行而非"最后插入的 key"。

    这是真实 bug 的回归锚：qa 页 28 条证据只有 15 篇，`list(evidence)[-1]` 把原句
    挂到了别的文献名下，整页词面覆盖掉到 0.227、接地率假跌到 43%。
    """
    ev = (_ev(1, "aa2026Alpha", "Alpha", "第一条：误差 0.06。")
          + _ev(2, "bb2026Beta", "Beta", "无关内容。")
          + _ev(3, "aa2026Alpha", "Alpha", "第三条：样本 4242 例。"))
    r = _audit(tmp_path, "- Alpha 的样本为 4242 例。 [@aa2026Alpha]", ev)
    assert r["numeric_rate"] == 1.0, "重复 citekey 的第二条原句丢了"


@pytest.mark.parametrize("a,b,want", [
    # 百分号互换：两个方向 + 论断省掉 % 的写法
    ("0.488", "48.8%", True),
    ("48.8%", "0.488", True),
    ("48.8", "48.8%", True),
    ("17,775", "17775", True),
    ("０.０６", "0.06", True),          # 全角
    # ⚠️ 两方都不带 % 时，永远不许百倍换算。旧实现给每个数字无条件展开 ×100/÷100
    # 再求交集，于是「共 5 个中心」被证据「p = 0.05」判为接地。
    ("5", "0.05", False),
    ("0.05", "5", False),
    # 子串不是匹配：0.85 ≠ 0.853，48 ≠ 1948
    ("0.85", "0.853", False),
    ("48", "1948", False),
])
def test_numbers_match_semantics(a, b, want):
    assert numbers_match(a, b) is want


# --- 2026-08-27 对抗审核：给存活的变异体补锚 -----------------------------

def test_line_number_does_not_ground_a_number(tmp_path):
    """出处行号（`札记.md:944`）是札记文件的行号，不是论文内容，不许进匹配池。

    线上真实发生过：qa 页那条「值超出 3 个 IQR」的 3，全库唯一只靠 `.md:NNN` 接地。
    """
    ev = ("- ● **E1** `[@aa2026Alpha]` 🟩x · y · 科研札记_2026-01.md:944\n"
          "  <small>Alpha</small>\n  > 一句没有数字的原句。\n")
    r = _audit(tmp_path, "- 共纳入 944 例患者。 [@aa2026Alpha]", ev)
    assert r["numeric_rate"] == 0.0, "944 只出现在出处行号里，不该判接地"


def test_pool_is_quote_and_title_only(tmp_path):
    """匹配池**只有原句 + 标题**（2026-08-27 第一轮对抗审核后收紧）。

    ⚠️ 这条测试原来断言的是相反的事（"citekey/年份/出处进池是有意放宽"），把一个
    零收益的风险敞口钉死了。实测：把池砍到只剩原句+标题，全库未接地数一个没变；
    而 citekey 年份、出处文件名里的月份都在制造可测的假接地
    （`科研札记_2023-01.md` 的 `01` → 值 1，让「只有 1 个中心报告」自动接地）。
    """
    from src.scholar.page_parse import EvidenceRow
    row = EvidenceRow(ref="E1", citekey="butler2023Noninterventional",
                      title="2019 年的队列研究", quote="正文。",
                      meta="· 科研札记_2021-05.md:12")
    pool = row.pool
    assert "正文。" in pool and "2019" in pool, "原句与标题要在池里"
    assert "butler2023Noninterventional" not in pool, "citekey 不进池"
    assert "2021" not in pool and "05" not in pool, "出处的年月不进池"
    assert ".md:12" not in pool, "行号不进池"


def test_chinese_numerals_are_not_extracted():
    """本层**只查阿拉伯数字**，中文数字一概不抽（含带量词的）。

    曾经加过中文数字归一，两轮对抗审核实测后删除：全库 550 个数字里只救回 1 个，
    却把最常见捏造值「1」的假接地面积扩大 81%（「一致」「十分」「两者」「进一步」
    里的字被当成数字注入池）。这条测试把「不抽」钉住，免得下次又凭直觉加回来。

    代价是已记账的：论断「超出 3 个 IQR」+ 证据「值在三个 IQR 之外」会被报出来。
    那是**可接受的假失真**——报出来人看一眼就能判，比反向的假接地安全。
    """
    from src.scholar.grounding import pool_numbers
    assert pool_numbers("该差异十分显著，两者一致，进一步分析") == set()
    assert pool_numbers("值在三个 IQR 之外") == set()
    assert pool_numbers("覆盖十家中心") == set()


def test_percent_conversion_requires_exactly_one_percent_sign(tmp_path):
    """两方都带 % 时不许百倍换算——否则 `0.5%` 抄成 `50%` 完全逃逸。"""
    from src.scholar.grounding import numbers_match
    assert numbers_match("50%", "0.5%") is False
    assert numbers_match("0.488", "48.8%") is True


def test_numbers_adjacent_to_chinese_are_extracted():
    """紧邻中文的数字必须抽得出——中文语料里这是**常态**而不是边界情况。

    ⚠️ 这条是为了守住一类**整片失效**：前置断言一度写成 `(?<![^\\W\\d_])`
    （Unicode 字母类），而 CJK 也是 Unicode 字母，于是「共60条」「缺失率60%」这种
    没有空格分隔的写法被整片挡掉。当时 64 条单元测试**全绿**，因为它们的样例
    全都在数字两侧留了空格——直到重跑 gen_bench 才发现全库数字总数 542→389、
    接地率 99.5%→76.3%。

    教训有两条：(1) 测试样例必须覆盖**没有空格**的中文写法；(2) 改判据后必须
    立刻重跑 gen_bench，不能只看单元测试绿。
    """
    from src.scholar.grounding import pool_numbers
    assert pool_numbers("共60条证据，其中3条无效") == {"60", "3"}
    assert pool_numbers("缺失率60%，样本量17,775例") == {"60%", "17,775"}
    assert pool_numbers("第2版共收录1,234篇") == {"2", "1,234"}


def test_canary_corpus_exact_multiset():
    """**金丝雀语料**：一段覆盖全部抽取风险点的文本，断言产出 token 精确相等。

    为什么要"精确相等"而不是"含有"：判据整体变严导致的大面积漏抽，在每一条
    单点「某 token 在/不在结果里」的断言下**都像合理收紧**，所以能穿过任意多条
    那种测试。这一类缺陷已经溜过去两次：

      1. `[^\W\d_]`（Unicode 字母类）—— CJK 被当字母，紧邻中文的数字整片消失，
         64 条单元测试全绿，靠重跑 gen_bench 才发现（542→389）。
      2. `[A-Za-zÀ-ɏ]`（连续码位区段）—— 区段里含 `×`(U+00D7)/`÷`(U+00F7)，
         `3×3` 的第二个操作数被吃掉。**这一版已经发货过**，而事后补的
         「紧邻中文」测试也抓不到它，因为样例里没有 ×。

    实测判别力（第二轮对抗审核验证）：Unicode 字母类版产出 7 个、纯 ASCII 版
    15 个（多出 `202` 碎片）、`À-ɏ` 版 12 个（丢掉两个 × 操作数）——三个坏版本
    全红，只有正确的字符类通过。
    """
    from src.scholar.grounding import _NUM_RE, norm
    text = ("该队列纳入17,775例患者，其中60%为男性；模型AUC为0.488，训练48h。"
            "设计3×3提示矩阵，缺失率60-70%，覆盖15个4CE站点的COVID-19病例，"
            "证据来自smajlović2026Secure与куксенко2024Аналіз，系数β0未报告。")
    got = [m.group(0).strip() for m in _NUM_RE.finditer(norm(text))]
    assert got == ["17,775", "60%", "0.488", "48", "3", "3", "60", "70%", "15"], got


def test_number_extraction_yield_is_not_silently_gutted():
    """整片失效的下界守卫：一段典型中文段落必须抽出全部 6 个数字。

    单点样例挡不住"判据整体变严导致大面积漏抽"这种事故——它每一条单独看都像
    合理收紧。这条用一段密集的真实风格文本兜住产出量。
    """
    from src.scholar.grounding import pool_numbers
    text = ("该队列纳入17,775例患者，其中60%为男性，中位年龄65岁；"
            "模型AUC为0.488，训练用了48h，外部验证队列另有1,234例。")
    assert pool_numbers(text) == {"17,775", "60%", "65", "0.488", "48", "1,234"}


def test_unicode_author_name_does_not_leak_a_number(tmp_path):
    """非 ASCII 作者名的 citekey 不该漏出截断的数字。

    `smajlović2026Secure`：ASCII 前置断言挡不住 `ć`，`2026` 进入匹配后又被后置
    `(?![A-Z])` 截断成 `202` 塞进池子（真实页面上出现 3 次）。
    """
    from src.scholar.grounding import pool_numbers
    assert pool_numbers("smajlović2026Secure") == set()
    assert pool_numbers("müller2021Data") == set()


def test_derived_that_is_literally_grounded_stays_in_the_denominator(tmp_path):
    """派生标记不该让一个**逐字就在证据里**的数字被移出分母静默豁免。"""
    from src.scholar.grounding import check_numbers
    chk = check_numbers("绝对提升 2.12 个百分点。", "基线 96.94%，提升到 99.06%，即 2.12%。")
    assert chk.grounded == 1
    assert "2.12" not in chk.derived


def test_bench_and_production_share_one_judge():
    """判据必须唯一：bench 与生产链路（validate_*）都走 src.scholar.grounding。

    两边各写一套的话，bench 报 100% 而生产链路报别的，谁也不知道该信哪个。
    """
    from src.scholar import grounding, topics
    assert GB.check_numbers is grounding.check_numbers
    assert topics.check_numbers is grounding.check_numbers
