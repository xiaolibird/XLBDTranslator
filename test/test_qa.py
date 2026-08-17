# -*- coding: utf-8 -*-
"""src/scholar/qa.py 回归（不连 Ollama、不调 LLM、不联网）。

问答归档与概念页共用同一套防幻觉机制，但它自己的失败模式不一样：

1. **同一个问题堆出第二份答案**——slug 必须只由问题文本决定（不掺日期、对空白归一化），
   否则 `topics/qa/` 三个月后会变成一堆互相矛盾的历史快照，而这个模块存在的全部理由
   是让答案**积累**而不是**堆积**；
2. **自由文本里的证据编号没人管**——`answer` / `points[].text` / `gaps` 里模型顺手写的
   `E31` 不经过 `evidence` 字段的校验，越界了就是指向不存在证据的交叉引用，
   有效的也会在页面被摘去写稿后失去意义。必须回译成 `[@citekey]`；
3. **没有出处的论断混进有出处的论断里**——读者会默认它们同质，所以凑不出编号的整条丢弃；
4. **答不上来被写成看起来完整的答案**——`gaps` 是这一页最该被看到的部分。

外加与概念页共享的两条：哨兵不覆盖、编号回译。
"""
import datetime as _dt
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar import qa as Q                                           # noqa: E402
from src.scholar import topics as T                                       # noqa: E402
from src.scholar.embeddings import EmbeddingError                         # noqa: E402


def _ev(ref, citekey, text="证据句", role="citable", score=0.9, section="实验方法"):
    return T.Evidence(ref=ref, citekey=citekey, text=text, role=role, section=section,
                      note_file="札记.md", note_line=7, score=score, year=2024,
                      title="论文 {}".format(citekey))


EVS = [_ev("E1", "a2024X"), _ev("E2", "b2024Y"), _ev("E3", "c2024Z")]


# ---------------------------------------------------------------------------
# 假 embedding / 假向量库：查重与 gap 回查都要连 Ollama，测试里一律注入替身。
# 用**逐字文本 -> 向量**的显式映射（而不是关键词启发式），因为这些用例锁的正是
# "词面不像但语义像"的那一档——启发式替身会把被测行为悄悄替换成词面重合。
# ---------------------------------------------------------------------------

class FakeEmbed:
    def __init__(self, vecs, dim=4, fail=False):
        self.vecs = vecs
        self.dim = dim
        self.fail = fail
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise EmbeddingError("Ollama 没起来")
        out = []
        for t in texts:
            v = np.array(self.vecs.get(t, [0.0] * (self.dim - 1) + [1.0]), dtype=np.float32)
            n = float(np.linalg.norm(v)) or 1.0
            out.append(v / n)
        return np.array(out, dtype=np.float32)


class FakeStore:
    def __init__(self, records, vecs):
        self.records = records
        self.mat = np.array(vecs, dtype=np.float32)

    def search(self, vec, mask=None, top_k=50):
        scores = self.mat @ np.asarray(vec, dtype=np.float32)
        idx = sorted(range(len(scores)), key=lambda i: -scores[i])
        return [(i, float(scores[i])) for i in idx
                if (mask is None or mask[i])][:top_k]


# ---------------------------------------------------------------------------
# 1. slug：同一个问题必须落在同一页上
# ---------------------------------------------------------------------------

def test_same_question_always_yields_the_same_slug():
    """这是"活文档 vs 流水账"的分界。slug 若掺日期或对空白敏感，同一个问题会每天
    堆一份新答案，`topics/qa/` 三个月后就是一堆互相矛盾的历史快照。"""
    q = "MNAR 诊断在纵向 EHR 上到底能不能做？"
    assert Q.qa_slug(q) == Q.qa_slug(q)
    # 用户复制粘贴问题时极容易多带空格/换行，不能因此另开一页
    assert Q.qa_slug(q) == Q.qa_slug("  {}  ".format(q))
    assert Q.qa_slug(q) == Q.qa_slug(q.replace(" ", "\n"))


def test_different_questions_yield_different_slugs():
    assert Q.qa_slug("缺失机制可检验吗") != Q.qa_slug("缺失机制不可检验吗")


def test_slug_is_filesystem_and_wikilink_safe():
    """slug 同时当文件名与 wiki 链接用。纯中文问题就只剩哈希（靠 frontmatter 的
    title 在 INDEX 与 Obsidian 里显示问题全文），但绝不能出现空格/中文/标点。"""
    for q in ["MNAR 诊断在纵向 EHR 上到底能不能做？", "缺失机制真的可检验吗",
              "!!!???", "a/b\\c:d*e"]:
        s = Q.qa_slug(q)
        # 用**本模块自己**的 `_QA_SLUG_RE` 而不是 topics 的 `_SLUG_RE`：后者更宽松
        # （不要求 qa- 前缀），拿它来验就等于把本模块真正的约束整条放空。
        assert Q._QA_SLUG_RE.match(s), (q, s)
        assert s.startswith("qa-")


def test_ascii_words_make_the_slug_readable():
    """中英混排的技术问题几乎总有 ASCII 术语，拼进 slug 让文件名可读。"""
    assert Q.qa_slug("MNAR 诊断在纵向 EHR 上能做吗").startswith("qa-mnar-ehr-")


@pytest.mark.parametrize("explicit,want", [
    ("my-question", "qa-my-question"),
    ("qa-my-question", "qa-my-question"),      # 已带前缀不重复加
    ("MY-Question", "qa-my-question"),         # 大小写归一
])
def test_explicit_slug_is_normalized(explicit, want):
    assert Q.qa_slug("随便什么问题", explicit) == want


@pytest.mark.parametrize("bad", [
    # 非 ASCII 那一档
    "有中文", "带 空格", "带/斜杠", "带_下划线",
    # M1：**纯 ASCII** 的非法样本。首版四个参数每一个都含汉字，于是只测到了
    # "非 ASCII 被拒"——把 `_QA_SLUG_RE` 放行下划线与点的变异整条逃逸。
    "my_question", "my.question", "a b", "a/b", "a:b", "UPPER_CASE", "带$符号",
    "----",                       # 去掉首尾连字符后什么都不剩
    "q" * 200,                    # 300 字符的 slug 落盘时 OSError: File name too long
])
def test_illegal_explicit_slug_is_rejected_loudly(bad):
    """静默改写用户给的 slug 比报错更糟：他下次用同一个 slug 想更新那一页，
    却会因为改写规则不可预测而落到别处。"""
    with pytest.raises(T.TopicError):
        Q.qa_slug("问题", bad)


def test_explicit_slug_trailing_hyphens_are_trimmed():
    """`qa-abc-` 与 `qa-abc` 是同一页——尾随连字符在文件名里是纯噪音，
    但让它通过就会得到两页内容相同、名字差一个字符的归档。"""
    assert Q.qa_slug("问题", "abc-") == "qa-abc"
    assert Q.qa_slug("问题", "-abc") == "qa-abc"


def test_explicit_slug_length_is_capped():
    assert len(Q.qa_slug("问题", "a" * 64)) <= 67          # qa- 前缀另算
    with pytest.raises(T.TopicError):
        Q.qa_slug("问题", "a" * 200)


def test_auto_slug_hash_is_wide_enough_to_separate_questions():
    """M4：`sha1(...)[:8]` 被改成 `[:2]` 时没有任何用例会红。哈希宽度就是
    "两个不相干的问题不会共用一页"的全部保证——32 bit 时 n=1000 页碰撞约 0.012%，
    8 bit 时几十页就必撞，而撞上的后果是 C1 那种整页被覆盖。"""
    digest = Q.qa_slug("纯中文的一个问题").rsplit("-", 1)[-1]
    assert len(digest) == 8
    seen = {Q.qa_slug("第 {} 个互不相干的问题，内容完全不同".format(i)) for i in range(300)}
    assert len(seen) == 300


def test_empty_question_is_rejected():
    with pytest.raises(T.TopicError):
        Q.qa_slug("   ")


# ---------------------------------------------------------------------------
# 2. 自由文本里的证据编号回译（本模块特有的那条通道）
# ---------------------------------------------------------------------------

def test_inline_evidence_numbers_are_back_translated_to_citekeys():
    """实测模型确实会这么写（首次真实运行：「E1 的流程图明确写……」）。
    编号是本次召回的内部序号，页面被摘去写稿后就完全失去意义——回译成 `[@citekey]`
    才是真正可追溯、可粘进 pandoc 的引用。"""
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    out = Q.backtranslate_inline_refs("E1 的流程图明确写着需要外部数据", ev_map, rep)
    assert out == "[@a2024X] 的流程图明确写着需要外部数据"
    assert rep.invalid_refs == 0


def test_inline_numbers_glued_to_chinese_are_still_caught():
    """中文行文里「见E31描述」这种紧贴写法最常见。两侧都必须按**定界**判断——
    `\\b` 与 `\\w` 按 Unicode 判定、汉字算单词字符，用它们会判不到边界而整条漏过
    （lint.py 那三次教训：`\\w` → `\\b` → 矫枉过正成 ASCII-only）。"""
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    assert Q.backtranslate_inline_refs("见E2描述的方法", ev_map, rep) == "见[@b2024Y]描述的方法"


def test_out_of_range_inline_numbers_are_dropped_and_counted():
    """越界编号留在页面上就是一个指向不存在证据的交叉引用，而读者会以为自己没找到。"""
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    out = Q.backtranslate_inline_refs("E99 说了一件事", ev_map, rep)
    assert "E99" not in out and "@" not in out
    assert rep.invalid_refs == 1


def test_longer_numbers_are_not_truncated():
    """`E312` 绝不能被截成 `E31`——那会把一个越界引用悄悄变成一个**指向别篇论文**的
    有效引用，比留着更危险。

    这条保证来自 `\\d+` 的贪婪，不是来自正则右侧那个 `(?![0-9])`（变异测试实测：
    删掉那个前瞻行为完全不变）。所以这条测试锁的是**行为**，不是某一个字符——
    哪天有人把 `\\d+` 改成 `\\d`，它才是真正会响的那一条。"""
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    out = Q.backtranslate_inline_refs("E312 是个越界编号", ev_map, rep)
    assert "a2024X" not in out and "b2024Y" not in out and "c2024Z" not in out
    assert rep.invalid_refs == 1


@pytest.mark.parametrize("text", ["AE1 是个缩写", "MICE1 方法", "COVID19 队列"])
def test_letters_before_the_number_prevent_a_false_match(text):
    """左侧后顾断言才是真防线：没有它，`MICE1` 的词尾会被当成证据编号 E1，
    把一句正常行文改写成带引用的论断。"""
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    assert Q.backtranslate_inline_refs(text, ev_map, rep) == text
    assert rep.invalid_refs == 0


@pytest.mark.parametrize("text", ["MNAR 与 MAR 的区别", "MICE 插补", "AUROC 0.82", "E"])
def test_ordinary_prose_is_not_mangled(text):
    """回译不能误伤正常行文。这一条是 `\\b`→ASCII-only 那次矫枉过正的反向保险。"""
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    assert Q.backtranslate_inline_refs(text, ev_map, rep) == text
    assert rep.invalid_refs == 0


def test_validate_qa_applies_back_translation_to_all_free_text_fields():
    """三处自由文本（answer / points[].text / gaps）都要盖住——只盖一处等于没盖。"""
    data = {
        "answer": "综合 E1 与 E2 看，答案是折中的",
        "points": [{"text": "E3 给出了具体做法", "evidence": ["E3"]}],
        "gaps": ["缺 E99 那类的实证研究"],
    }
    qa, rep = Q.validate_qa(data, EVS)
    assert "[@a2024X]" in qa["answer"] and "[@b2024Y]" in qa["answer"]
    assert "[@c2024Z]" in qa["points"][0]["text"]
    assert "E99" not in qa["gaps"][0]
    assert rep.invalid_refs == 1


def test_clean_text_runs_before_back_translation():
    """顺序不能反：先回译会引入 `[@key]`，再剥 citekey 就把刚回译出来的也剥掉了。"""
    data = {"answer": "如 [@fake2020Ghost] 与 E1 所述", "points": [], "gaps": []}
    qa, rep = Q.validate_qa(data, EVS)
    assert "fake2020Ghost" not in qa["answer"]
    assert "[@a2024X]" in qa["answer"]          # 回译出来的那个必须活下来
    assert rep.stripped_cites == 1


# ---------------------------------------------------------------------------
# 3. 编号回译主防线（与概念页同约定）
# ---------------------------------------------------------------------------

def test_claims_without_valid_evidence_are_dropped():
    """一条没有出处的论断混在有出处的论断里，比少一条危险得多——读者会默认它们同质。"""
    data = {"answer": "", "points": [
        {"text": "有出处", "evidence": ["E1"]},
        {"text": "编号全越界", "evidence": ["E77", "E88"]},
        {"text": "压根没给编号", "evidence": []},
    ], "gaps": []}
    qa, rep = Q.validate_qa(data, EVS)
    assert [p["text"] for p in qa["points"]] == ["有出处"]
    assert rep.dropped_claims == 2
    assert rep.invalid_refs == 2
    assert rep.kept_claims == 1


def test_caveats_follow_the_same_rule_as_points():
    """注意事项是最可能被直接摘去写稿的一节，不能比论断松。"""
    data = {"answer": "", "points": [], "gaps": [],
            "caveats": [{"text": "有出处的限制", "evidence": ["E2"]},
                        {"text": "没出处的限制", "evidence": []}]}
    qa, rep = Q.validate_qa(data, EVS)
    assert [c["text"] for c in qa["caveats"]] == ["有出处的限制"]
    assert rep.dropped_claims == 1


def test_used_refs_counts_both_points_and_caveats():
    data = {"answer": "", "gaps": [],
            "points": [{"text": "p", "evidence": ["E1"]}],
            "caveats": [{"text": "c", "evidence": ["E2"]}]}
    qa, rep = Q.validate_qa(data, EVS)
    assert rep.used_refs == 2


def test_citekeys_are_deduped_within_one_claim():
    evs = [_ev("E1", "same2024"), _ev("E2", "same2024")]
    qa, _rep = Q.validate_qa(
        {"answer": "", "gaps": [], "points": [{"text": "t", "evidence": ["E1", "E2"]}]}, evs)
    assert qa["points"][0]["citekeys"] == ["same2024"]
    assert qa["points"][0]["refs"] == ["E1", "E2"]      # 编号仍是两条（证据表要标 ●）


def test_garbage_shapes_do_not_crash():
    for data in [{}, {"points": "不是列表"}, {"points": [None, 42, "x"]},
                 {"answer": None, "gaps": None}]:
        qa, _rep = Q.validate_qa(data, EVS)
        assert qa["points"] == [] and qa["caveats"] == []


# ---------------------------------------------------------------------------
# 4. prompt 组装：不许把 citekey 喂给模型
# ---------------------------------------------------------------------------

def test_prompt_never_exposes_citekeys():
    """编号回译的物理前提：模型看不到 citekey 就写不出 citekey。"""
    tpl = "问题：{{QUESTION}}\n证据：\n{{EVIDENCE_BLOCK}}\n上一版：{{PREVIOUS_ANSWER}}"
    text = Q.build_qa_prompt("我的问题", EVS, tpl)
    for e in EVS:
        assert e.citekey not in text
    assert "E1" in text and "我的问题" in text


def test_prompt_marks_absent_previous_answer_explicitly():
    """留空占位符会让模型把 `{{PREVIOUS_ANSWER}}` 本身当成内容。"""
    tpl = "{{QUESTION}}{{EVIDENCE_BLOCK}}{{PREVIOUS_ANSWER}}"
    assert "（无上一版" in Q.build_qa_prompt("q", EVS, tpl)


def test_prompt_template_requires_placeholders(tmp_path):
    bad = tmp_path / "p.md"
    bad.write_text("只有 {{QUESTION}}", encoding="utf-8")
    with pytest.raises(T.TopicError):
        Q.load_prompt_template(bad)
    with pytest.raises(T.TopicError):
        Q.load_prompt_template(tmp_path / "不存在.md")


def test_long_previous_answer_is_truncated_not_dropped():
    tpl = "{{QUESTION}}{{EVIDENCE_BLOCK}}{{PREVIOUS_ANSWER}}"
    text = Q.build_qa_prompt("q", EVS, tpl, previous_answer="甲" * 9000)
    assert "此处截断" in text and len(text) < 9000


# ---------------------------------------------------------------------------
# 5. 渲染与落盘
# ---------------------------------------------------------------------------

_QA = {"answer": "简短回答", "gaps": ["缺某类证据"],
       "points": [{"text": "论断一", "refs": ["E1"], "citekeys": ["a2024X"]}],
       "caveats": [{"text": "限制一", "refs": ["E2"], "citekeys": ["b2024Y"]}]}


def test_render_marks_unused_evidence():
    block = Q.render_qa_block("问题", _QA, EVS)
    assert "● **E1**" in block and "● **E2**" in block
    assert "○ **E3**" in block          # 召回了但没被引用


def test_gaps_render_before_the_evidence_table():
    """一个答案的边界在哪里，比答案本身更决定它能不能被直接写进稿子——
    gaps 排在证据表之后就等于沉底了。"""
    block = Q.render_qa_block("问题", _QA, EVS)
    assert block.index("本次召回没覆盖到的") < block.index("本页证据")


def test_write_creates_then_preserves_annotation(tmp_path):
    d = tmp_path / "qa"
    path, status = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    assert status == "new"
    txt = path.read_text(encoding="utf-8")
    assert "由 scripts/ask_notes.py 生成" in txt      # 哨兵要指向真正生成它的脚本
    assert "build_topics.py" not in txt

    path.write_text(txt.replace("## 我的批注", "## 我的批注\n\n第 3 条我核对过，不对"),
                    encoding="utf-8")
    _p, status2 = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    assert status2 in ("merged", "unchanged")
    assert "第 3 条我核对过" in path.read_text(encoding="utf-8")


def test_write_refuses_to_clobber_a_tampered_block(tmp_path):
    d = tmp_path / "qa"
    path, _ = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    txt = path.read_text(encoding="utf-8")
    path.write_text(txt.replace("论断一", "论断一（我在生成块里手写了批注）"), encoding="utf-8")
    _p, status = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    assert status == "conflict"
    assert "我在生成块里手写了批注" in path.read_text(encoding="utf-8")


def test_first_asked_at_survives_a_rerun(tmp_path):
    """同一个问题再问一次是原地更新，`generated_at` 要前进，但"我第一次什么时候问的"
    是这一页的历史，重新生成不该把它抹掉。"""
    d = tmp_path / "qa"
    t0 = datetime(2026, 1, 1, 9, 0)
    Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport(), now=t0)
    path, _ = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport(),
                              now=datetime(2026, 6, 1, 9, 0))
    fm, _body = __import__("src.scholar.vault", fromlist=["x"]).split_frontmatter(
        path.read_text(encoding="utf-8"))
    assert fm["first_asked_at"].startswith("2026-01-01")
    assert fm["generated_at"].startswith("2026-06-01")


def test_dry_run_writes_nothing(tmp_path):
    d = tmp_path / "qa"
    path, status = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport(),
                                   dry_run=True)
    assert status == "new" and not path.exists()


def test_frontmatter_preserves_user_keys_but_manages_its_own(tmp_path):
    fm = Q.build_qa_frontmatter("问题", "qa-x", _QA, EVS, T.ValidationReport(),
                                "2026-08-17T00:00:00",
                                preserved={"我的备注": "待复核", "type": "会被覆盖"})
    assert "待复核" in fm
    assert 'type: "qa"' in fm
    # 不能有 topic 键：那是"我是一页概念页"的唯一判据，误认会让它进概念页索引、
    # 被日历兜底强制重合成、被 --verify 当页面审计
    assert "\ntopic:" not in fm


# ---------------------------------------------------------------------------
# 6. 查重：问之前先看看是不是问过了
# ---------------------------------------------------------------------------

def _archive(d, slug, question, at="2026-08-17T09:00:00"):
    d.mkdir(parents=True, exist_ok=True)
    fm = ('---\nqa: "{}"\ntitle: "{}"\nquestion: "{}"\ntype: "qa"\n'
          'generated_at: "{}"\nfirst_asked_at: "{}"\nn_points: 3\nn_evidence: 20\n---'
          ).format(slug, question, question, at, at)
    (d / "{}.md".format(slug)).write_text(
        T.assemble(fm, "正文", T.DEFAULT_USER_ZONE, generator=Q.QA_GENERATOR),
        encoding="utf-8")


def test_similar_question_is_surfaced(tmp_path):
    """这条最直接地兑现"探索复合"——它拦住的正是「同一个问题被问第四次」。"""
    d = tmp_path / "qa"
    _archive(d, "qa-old", "MNAR 诊断在纵向 EHR 上能不能做")
    res = Q.find_similar_questions(d, "MNAR 诊断在纵向 EHR 上到底能不能做？")
    assert [h.slug for h, _s in res.hits] == ["qa-old"]
    assert res.hits[0][1] > 0.5


def test_unrelated_question_is_not_surfaced(tmp_path):
    d = tmp_path / "qa"
    _archive(d, "qa-old", "MNAR 诊断在纵向 EHR 上能不能做")
    assert Q.find_similar_questions(d, "图神经网络的过平滑问题怎么缓解").hits == []


def test_the_page_being_updated_is_excluded_from_its_own_dedup_check(tmp_path):
    """没有这条，每次更新一页都会提示"你问过一个 100% 相似的问题"，纯噪音。"""
    d = tmp_path / "qa"
    q = "MNAR 诊断在纵向 EHR 上能不能做"
    _archive(d, Q.qa_slug(q), q)
    assert Q.find_similar_questions(d, q, exclude_slug=Q.qa_slug(q)).hits == []


def test_dedup_check_survives_an_empty_or_broken_archive(tmp_path):
    d = tmp_path / "qa"
    d.mkdir(parents=True)
    assert Q.find_similar_questions(d, "任何问题").hits == []
    (d / "坏的.md").write_text("不是合法 frontmatter", encoding="utf-8")
    (d / "INDEX.md").write_text("| 目录 |", encoding="utf-8")
    (d / "_草稿.md").write_text('---\nqa: "混进来的"\n---\n', encoding="utf-8")
    assert Q.find_similar_questions(d, "任何问题").hits == []
    assert Q.list_qa_pages(d) == []


def test_list_qa_pages_sorts_newest_first(tmp_path):
    d = tmp_path / "qa"
    _archive(d, "qa-a", "问题甲", at="2026-01-01T00:00:00")
    _archive(d, "qa-b", "问题乙", at="2026-08-01T00:00:00")
    assert [p.slug for p in Q.list_qa_pages(d)] == ["qa-b", "qa-a"]


# ---------------------------------------------------------------------------
# 7. 目录页
# ---------------------------------------------------------------------------

def test_index_scans_disk_not_just_this_run(tmp_path):
    """单跑一个问题不该让其余问答从目录里失踪——文件还在但没人找得到，
    而这个模块存在的全部理由就是让答案找得到（同 render_topics_index 的教训）。"""
    d = tmp_path / "qa"
    _archive(d, "qa-a", "问题甲")
    _archive(d, "qa-b", "问题乙")
    idx = Q.render_qa_index(d)
    assert "问题甲" in idx and "问题乙" in idx
    assert "共 2 条" in idx


def test_index_states_the_vector_store_limitation(tmp_path):
    """"归档问答搜不到"这件事必须写在目录页上——用户的第一反应一定是去 notes_search 搜。"""
    d = tmp_path / "qa"
    _archive(d, "qa-a", "问题甲")
    assert "notes_search" in Q.render_qa_index(d)


def test_index_handles_an_empty_archive(tmp_path):
    d = tmp_path / "qa"
    d.mkdir(parents=True)
    assert "还没有归档过任何问答" in Q.render_qa_index(d)


def test_index_escapes_pipes_in_questions(tmp_path):
    """问题里带 `|` 会把 markdown 表格那一行整个拆坏。"""
    d = tmp_path / "qa"
    _archive(d, "qa-a", "A | B 哪个好")
    assert "A \\| B" in Q.render_qa_index(d)


# ---------------------------------------------------------------------------
# 8. vault 同步与自检
# ---------------------------------------------------------------------------

def test_vault_sync_converts_citekeys_to_wiki_links(tmp_path):
    notes, vault = tmp_path / "notes", tmp_path / "vault"
    d = notes / "topics" / "qa"
    d.mkdir(parents=True)
    Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    rep = Q.sync_qa_to_vault(notes, vault, {"a2024X": "a2024X", "b2024Y": "b2024Y"})
    assert rep["new"] == 1
    out = (vault / "02-主题" / Q.VAULT_QA_DIR / "qa-x.md").read_text(encoding="utf-8")
    assert "[[a2024X]]" in out
    assert "[@a2024X]" not in out
    assert "qa-page" in out


def test_vault_sync_leaves_unknown_citekeys_alone(tmp_path):
    """不在 vault 里的 citekey（只有摘要没精读的条目）保持 `[@key]` 原样——
    转成 wiki 链接只会得到一个点开是空白的死链。"""
    notes, vault = tmp_path / "notes", tmp_path / "vault"
    d = notes / "topics" / "qa"
    d.mkdir(parents=True)
    Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    Q.sync_qa_to_vault(notes, vault, {})
    out = (vault / "02-主题" / Q.VAULT_QA_DIR / "qa-x.md").read_text(encoding="utf-8")
    assert "[@a2024X]" in out and "[[a2024X]]" not in out


def test_vault_sync_does_not_prune(tmp_path):
    """一次问答归档之后就是历史：源文件被删不代表 vault 那份该跟着消失
    （那可能正是用户手动整理的结果）。这与概念页的规矩不同，是有意的。"""
    notes, vault = tmp_path / "notes", tmp_path / "vault"
    d = notes / "topics" / "qa"
    d.mkdir(parents=True)
    Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    Q.sync_qa_to_vault(notes, vault, {})
    (d / "qa-x.md").unlink()
    Q.sync_qa_to_vault(notes, vault, {})
    assert (vault / "02-主题" / Q.VAULT_QA_DIR / "qa-x.md").exists()


def test_vault_sync_is_a_noop_without_a_qa_dir(tmp_path):
    rep = Q.sync_qa_to_vault(tmp_path / "notes", tmp_path / "vault", {})
    assert rep["synced"] == 0 and rep["conflicts"] == []


def test_audit_finds_dead_keys_and_unanchored_quotes(tmp_path):
    """问答页比概念页更需要这个：概念页随新论文自动重合成，改过的 citekey 迟早被刷掉；
    问答页只在人再问一次时才更新，可能一直挂着半年前就改过名的键。"""
    d = tmp_path / "qa"
    Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    index = {"papers": [
        {"citekey": "a2024X", "highlights": [{"text": "证据句"}]},
        {"citekey": "b2024Y", "highlights": [{"text": "已经被改写过的别的句子"}]},
    ]}   # c2024Z 整条从索引里消失了 = 死键
    audits = Q.audit_qa_pages(d, index)
    assert len(audits) == 1
    a = audits[0]
    assert a.dead_keys == ["c2024Z"]
    assert [u.split("(")[1].rstrip(")") for u in a.unanchored] == ["b2024Y", "c2024Z"]
    assert not a.ok


def test_audit_is_clean_when_everything_still_anchors(tmp_path):
    d = tmp_path / "qa"
    # slug 必须与问题算出来的一致，否则会撞上 A1 那条「文件名与问题对不上」的不变式
    # ——那正是它该做的事，这里测的是"其余都干净时 ok 为真"。
    Q.write_qa_page(d, Q.qa_slug("问题"), "问题", _QA, EVS, T.ValidationReport())
    index = {"papers": [{"citekey": e.citekey, "highlights": [{"text": "证据句"}]}
                        for e in EVS]}
    a = Q.audit_qa_pages(d, index)[0]
    assert a.ok and a.n_evidence == 3
    assert not a.slug_mismatch


def test_duplicate_papers_are_not_alive_for_audit(tmp_path):
    """跨月重复条目不承担证据责任（同 embed_store/notes_query 的入选口径）。"""
    d = tmp_path / "qa"
    Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    index = {"papers": [{"citekey": "a2024X", "duplicate_of": "other",
                         "highlights": [{"text": "证据句"}]}]}
    assert "a2024X" in Q.audit_qa_pages(d, index)[0].dead_keys


# ---------------------------------------------------------------------------
# 9. 与概念页共存：qa 子目录不能污染既有扫描
# ---------------------------------------------------------------------------

def test_qa_subdir_is_invisible_to_topic_page_scans(tmp_path):
    """`topics/` 下的四处扫描都是非递归 `glob("*.md")`，qa/ 是子目录所以天然看不到。
    这条测试把这个前提钉死——哪天有人改成 rglob，概念页索引里会突然冒出一堆问答页，
    日历兜底还会去"重合成"它们。"""
    topics = tmp_path / "topics"
    qa = topics / "qa"
    qa.mkdir(parents=True)
    _archive(qa, "qa-a", "问题甲")
    (topics / "real.md").write_text(
        T.assemble('---\ntopic: "real"\ntitle: "真概念页"\nn_evidence: 5\n---',
                   "正文", T.DEFAULT_USER_ZONE), encoding="utf-8")
    spec = T.TopicSpec(slug="real", title="真概念页", question="q", queries=["q"])
    assert "qa-a" not in T.render_topics_index(topics, [spec])
    assert [a.slug for a in T.audit_topic_pages(topics, {"papers": []})] == ["real"]
    assert T.stale_topic_slugs(topics, [spec], max_age_days=0) == []


def test_lint_coverage_counts_qa_citations(tmp_path):
    """B2：**只被问答页引用过的论文不该被算成孤儿**。缺口分析问的是"哪些论文连
    概念层的证据池都进不去"——一篇被专门问过一次、还进了某页问答证据表的论文，
    显然已经被看见过了，把它列进"该考虑开新页"的名单是假信号。

    这与 `_lint.md` 自己引用自己那个坑不同：那是**报告**把自己列出的撤稿键算成覆盖，
    问答页是**真的人在用**这些论文。`_` 前缀那道防线仍然挡着报告。"""
    from src.scholar import lint as L
    topics = tmp_path / "topics"
    qa = topics / "qa"
    qa.mkdir(parents=True)
    (topics / "real.md").write_text(
        T.assemble('---\ntopic: "real"\n---', "- 论断 [@covered2024]", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    (topics / "_lint.md").write_text(
        T.assemble('---\n---', "- 撤稿 [@retracted2024]", T.DEFAULT_USER_ZONE),
        encoding="utf-8")
    Q.write_qa_page(qa, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    keys = L.cited_citekeys(topics)
    assert "covered2024" in keys
    assert "a2024X" in keys and "c2024Z" in keys        # qa 页的论断与证据表都算
    assert "retracted2024" not in keys                  # `_` 前缀那道防线仍然挡着报告
    # qa 目录页不是一页问答，别把它扫进来（它只有表格，没有 citekey，但判据要在）
    assert L.cited_citekeys(topics) == L.cited_citekeys(topics)


def test_archiving_a_qa_makes_the_vault_look_stale(tmp_path):
    """W5 的同型防线，换了个目录层级：归档一次问答必须能让 vault 陈旧判定为真。

    两边（`vault.write_vault` 写进 `_meta.json` 的 `source_topics_mtime` 与
    `sync_vault.topics_mtime` 读出来的）用的若是非递归 `glob("*.md")`，
    写 `topics/qa/xxx.md` 完全不改变这个时间戳——索引没变、`topics/*.md` 也没变，
    陈旧判定于是认为"已同步"，新归档的问答**永远到不了 Obsidian**。
    """
    import os
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import sync_vault as SV                                    # noqa: E402

    notes = tmp_path / "notes"
    topics = notes / "topics"
    topics.mkdir(parents=True)
    (topics / "real.md").write_text("x", encoding="utf-8")
    before = SV.topics_mtime(notes)
    assert before is not None

    qa = topics / "qa"
    qa.mkdir()
    p = qa / "qa-x.md"
    p.write_text("y", encoding="utf-8")
    os.utime(p, (before + 100, before + 100))       # 明确比既有概念页新
    after = SV.topics_mtime(notes)
    assert after is not None and after > before, "归档问答后 topics_mtime 必须前进"


def test_both_sides_of_the_staleness_check_use_the_same_scan(tmp_path):
    """口径一旦分叉，陈旧判定会永远为真或永远为假。这条把两边钉在一起。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import sync_vault as SV                                    # noqa: E402
    from src.scholar import vault as V                         # noqa: E402
    import inspect
    assert "rglob" in inspect.getsource(SV.topics_mtime)
    # write_vault 那侧的扫描语句
    src = inspect.getsource(V.write_vault)
    assert "topics_dir.rglob" in src


# ---------------------------------------------------------------------------
# 10. A1：「没答上来的」那一节不许对整个文献库下断言
# ---------------------------------------------------------------------------

def test_gap_section_never_claims_the_whole_library_is_empty():
    """本轮最严重的一条。首版那句「这些是**库里现在没有证据**的部分」是把**本次召回
    的召回率缺口**印成了对 2300+ 篇文献库的事实断言——而模型判断"库里有没有"的唯一
    依据就是这次召回的那几十条。实测产物据此写下「库内证据显示这类讨论几乎完全缺失」，
    而 `topics/missingness-causal.md` 整整一页 302 行都在讲那件事。

    **答错了用户还能核对，指错方向他不会去核对**——因为这一节按设计就没有出处可点。"""
    block = Q.render_qa_block("问题", _QA, EVS)
    assert "本次召回没覆盖到的" in block
    assert "库里现在没有证据" not in block
    assert "不等于库里没有" in block
    # 必须给出可执行的下一步，否则改口径只是换了个说法
    assert "topics/INDEX.md" in block and "notes_search" in block


def test_gap_recheck_finds_the_page_that_already_answers_it(tmp_path):
    """A1 的第二件：每条 gap 拿它自己的文本回查一次向量库与概念页。改口径只是不再
    说谎，回查才是真正拦住"指错方向"的那道——`chen2026Partial` 当时就躺在这一页
    **自己的证据表里当 E12**。"""
    gap = "缺少 MNAR 在观测数据上不可识别的系统讨论"
    other = "缺少差分隐私相关的实证研究"
    store = FakeStore(
        [{"id": "c1", "level": "highlight", "citekey": "chen2026Partial", "text": "可识别性判据"},
         {"id": "c2", "level": "paper", "citekey": "noise2024", "text": "标题级 chunk"}],
        [[1, 0, 0, 0], [1, 0, 0, 0]])
    specs = [T.TopicSpec(slug="missingness-causal", title="缺失与因果结构",
                         question="缺失机制的可识别性在什么条件下成立？", queries=["q"])]
    # FakeEmbed 的键必须是**剥完脚手架之后**的 query——回查用的是它，不是 gap 原句
    emb = FakeEmbed({Q.strip_gap_scaffold(gap): [1, 0, 0, 0],
                     Q.strip_gap_scaffold(other): [0, 0, 1, 0],
                     "缺失机制的可识别性在什么条件下成立？": [0.95, 0.31, 0, 0]})
    hits = Q.recheck_gaps([gap, other], store=store, embed_client=emb, topic_specs=specs)
    assert [h.key for h in hits[gap] if h.kind == "evidence"] == ["chen2026Partial"]
    assert [h.key for h in hits[gap] if h.kind == "topic"] == ["missingness-causal"]
    assert hits.get(other, []) == []          # 库里真没有的那条不许被硬安一个"其实有"
    # paper 级 chunk 不是句级证据，不能当"库里有"的凭据
    assert "noise2024" not in [h.key for h in hits[gap]]


def test_gap_recheck_costs_one_embedding_batch():
    """回查要便宜到"顺手就做"，否则下一个人会为了省时间把它关掉。
    一次批量 embedding（全部 gap + 全部概念页问题）而不是每条一次。"""
    emb = FakeEmbed({})
    Q.recheck_gaps(["甲", "乙", "丙"], store=None, embed_client=emb,
                   topic_specs=[T.TopicSpec(slug="s", title="t", question="q", queries=["q"])])
    assert emb.calls == 1


def test_gap_recheck_is_silent_without_an_embedding_client():
    """Ollama 不可用时不猜——不回查就是不回查，不许拿词面重合冒充。"""
    assert Q.recheck_gaps(["某个空白"], store=None, embed_client=None) == {}


def test_gap_hits_are_rendered_next_to_the_gap_they_contradict():
    qa = dict(_QA, gaps=["缺少可识别性的系统讨论"],
              gap_hits={"缺少可识别性的系统讨论": [
                  Q.GapHit(kind="topic", key="missingness-causal", score=0.71,
                           title="缺失与因果结构"),
                  Q.GapHit(kind="evidence", key="chen2026Partial", score=0.68,
                           title="Partial Identification under Missing Data",
                           snippet="影子变量区间中点平均绝对误差 0.06")]})
    block = Q.render_qa_block("问题", qa, EVS)
    assert "但库里可能有" in block
    assert "topics/missingness-causal.md" in block
    assert "[@chen2026Partial]" in block
    # 警告必须贴在那条 gap 后面，不能沉到别处
    assert block.index("缺少可识别性的系统讨论") < block.index("但库里可能有")
    # **标题与原句必须出现**：只印裸 citekey 时读者无从判断哪条值得点，
    # 而句级通道实测只有约四成相关，累积下来的结果是整节被跳过。
    assert "Partial Identification under Missing Data" in block
    assert "影子变量区间中点平均绝对误差 0.06" in block
    assert "缺失与因果结构" in block


def test_prompt_forbids_library_wide_absence_claims():
    """改口径要两边都改：页面上的措辞改了、prompt 还在鼓励模型写「库内缺失」的话，
    那句断言照样会被写进 gaps 正文里。"""
    text = Path("config/prompts/qa_synthesis_prompt.md").read_text(encoding="utf-8")
    assert "本次证据没有覆盖" in text
    assert "库内没有" in text and "不许" in text
    assert "不要复述" in text          # B9：caveats 复述 points/gaps


# ---------------------------------------------------------------------------
# 11. A2：归档页是"生成它那一刻的代码"的快照，要有机制发现这件事
# ---------------------------------------------------------------------------

def _page_with_residual_ref(d):
    """模拟一页"回译防线落地之前生成的"归档：正文里留着裸编号。"""
    qa = dict(_QA, points=[{"text": "E1 的流程图明确写着需要外部数据",
                            "refs": ["E1"], "citekeys": ["a2024X"]}])
    return Q.write_qa_page(d, "qa-old", "问题", qa, EVS, T.ValidationReport())


def test_verify_catches_evidence_numbers_left_in_the_generated_block(tmp_path):
    """线上那一页 19:01 生成、回译防线 19:06 才落地，于是 `E1的流程图明确写…`
    四处原封不动留在正文里，而 frontmatter 写着 `invalid_refs: 0`、`--verify` 报 ✅、
    退出码 0——**所有仪表全绿**。对照 `topics.PageAudit.bare_cites`（G1），qa 侧此前
    没有同型的那一条。"""
    d = tmp_path / "qa"
    path, _ = _page_with_residual_ref(d)
    a = Q.audit_qa_pages(d, {"papers": [{"citekey": e.citekey,
                                         "highlights": [{"text": "证据句"}]} for e in EVS]})[0]
    assert a.residual_refs == ["E1"]
    assert not a.ok, "残留编号必须让 --verify 变红，否则这一页永远没人会重跑"


def test_residual_ref_scan_stops_at_the_sentinel(tmp_path):
    """用户在「我的批注」里写「E1 那条我核对过」是完全正常的——生成器不碰用户区，
    审计也不该拿用户区的字去判生成块有问题。"""
    d = tmp_path / "qa"
    path, _ = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    txt = path.read_text(encoding="utf-8")
    path.write_text(txt.replace("## 我的批注", "## 我的批注\n\nE1 那条我核对过，对的"),
                    encoding="utf-8")
    a = Q.audit_qa_pages(d, {"papers": []})[0]
    assert a.residual_refs == []


def test_qa_pages_record_their_own_defence_version(tmp_path):
    """哨兵里写的是概念页的 `TOPIC_SCHEMA_VERSION`，而 qa 有自己的一套防线。
    半年后 `qa.py` 再加两层，`topics/qa/` 里哪几页是老防线产的，得能看出来。"""
    d = tmp_path / "qa"
    path, _ = Q.write_qa_page(d, "qa-x", "问题", _QA, EVS, T.ValidationReport())
    txt = path.read_text(encoding="utf-8")
    assert "BEGIN GENERATED v{} ".format(Q.QA_SCHEMA_VERSION) in txt
    a = Q.audit_qa_pages(d, {"papers": []})[0]
    assert a.schema_version == Q.QA_SCHEMA_VERSION
    assert not a.outdated


def test_an_old_schema_page_is_flagged_but_not_treated_as_tampered(tmp_path):
    """改哨兵版本号最容易做歪的地方：让既有页面全被判成 tampered，于是每一页
    重跑都是 conflict、谁也更新不了。`generated_block_tampered` 只认 `h=` 哈希，
    版本号变化不参与——这条把它钉死。"""
    from src.scholar import vault as V
    d = tmp_path / "qa"
    d.mkdir(parents=True)
    old = d / "qa-old.md"
    fm = ('---\nqa: "qa-old"\ntitle: "问题"\nquestion: "问题"\ntype: "qa"\n'
          'generated_at: "2026-01-01T00:00:00"\n---')
    old.write_text(T.assemble(fm, Q.render_qa_block("问题", _QA, EVS),
                              T.DEFAULT_USER_ZONE, generator=Q.QA_GENERATOR),
                   encoding="utf-8")   # T.assemble 写的是 TOPIC_SCHEMA_VERSION=1
    _fm, body = V.split_frontmatter(old.read_text(encoding="utf-8"))
    assert not V.generated_block_tampered(body)

    a = Q.audit_qa_pages(d, {"papers": []})[0]
    assert a.schema_version == 1 and a.outdated

    _p, status = Q.write_qa_page(d, "qa-old", "问题", _QA, EVS, T.ValidationReport())
    assert status in ("merged", "unchanged"), "版本号前进不该把既有页面变成 conflict"


# ---------------------------------------------------------------------------
# 12. A3：查重两头都不成立
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("EHR 上能不能做 MNAR 的敏感性分析", "EHR 上能不能做 MNAR 的敏感性分析？"),
    ("EHR 上能不能做", "EHR 上能不能做。"),
    ("什么是 MAR", "什么是MAR"),
    ("A 与 B，哪个好", "A 与 B, 哪个好"),
])
def test_punctuation_differences_do_not_fork_a_new_page(a, b):
    """`«…能不能做»` 与 `«…能不能做？»` 曾经是两页不同文件，查重会说「100% 像」，
    然后接一句"确认是新问题就继续，本次会另存一页"——用户被提示了，却被引去另存。"""
    assert Q.qa_slug(a) == Q.qa_slug(b)


def test_case_differences_do_not_fork_a_new_page():
    """自己那轮变异测试的逃逸补丁：`question_key` 去掉 `.lower()` 时没有任何用例会红。
    而 `MNAR` / `mnar` / `Mnar` 是同一个术语——技术问题里大小写混着打太常见了
    （显式 `--slug` 那一侧早就归一化大小写，自动 slug 这一侧反而没有）。"""
    assert Q.qa_slug("MNAR 能不能查") == Q.qa_slug("mnar 能不能查")
    assert Q.qa_slug("EHR 上的 MNAR") == Q.qa_slug("ehr 上的 Mnar")
    # 但归一化只在身份口径上做，展示口径必须原样保留用户打的大小写
    assert Q.normalize_question("MNAR 能不能查") == "MNAR 能不能查"


@pytest.mark.parametrize("z", ["​", "‌", "‍", "⁠", "﻿"])
def test_zero_width_characters_are_stripped_by_normalize_question(z):
    """B5：这条**首版锁错了函数**。它原来断言 `qa_slug` 相等，而当时 `question_key`
    的 `[\\W_]+` 本来就吃掉零宽字符——`_ZERO_WIDTH_RE` 对 slug 而言是死代码，
    把它整条删掉这个用例照样绿。

    `_ZERO_WIDTH_RE` 真正 load-bearing 的地方是**展示口径与检索 query**：
    `build_qa_spec` 拿 `normalize_question` 拼 query，带一个 U+200C 进去就会原样
    送给 Ollama。所以断言必须打在 `normalize_question` 上，且 5 个字符全覆盖
    （首版只测了 U+200B 一个）。

    A6 把 `_KEY_DROP_RE` 收窄成"只丢排版标点"之后，零宽字符**不再**被它吃掉，
    于是 `_ZERO_WIDTH_RE` 从死代码变成了身份口径唯一的那道防线——下面那条
    `question_key` 的断言因此也必须在。"""
    assert Q.normalize_question("MNAR{}能不能查".format(z)) == "MNAR能不能查"
    # 收窄 `_KEY_DROP_RE` 之后零宽字符只剩这一条通道，两头都要钉住
    assert Q.question_key("MNAR{}能不能查".format(z)) == Q.question_key("MNAR能不能查")
    assert Q.qa_slug("MNAR 能不能查") == Q.qa_slug("MNAR{}能不能查".format(z))


# ---------------------------------------------------------------------------
# 18. A6：身份键丢掉全部标点会把**语义相反**的问题并成同一页
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("p<0.05 时该怎么办？", "p>0.05 时该怎么办？"),
    ("样本量 >100 够吗", "样本量 <100 够吗"),
    ("C++ 和 C 有什么区别", "C# 和 C 有什么区别"),
    ("A→B 还是 B→A", "A←B 还是 B←A"),
    ("准确率 = 0.9 算好吗", "准确率 > 0.9 算好吗"),
    ("R^2 怎么解释", "R&2 怎么解释"),
])
def test_semantically_loaded_symbols_are_not_dropped_from_the_identity_key(a, b):
    """A6：`_KEY_DROP_RE = [\\W_]+` 把 `<` `>` `=` `+` `#` 全丢掉，于是
    «p<0.05 时该怎么办？» 与 «p>0.05 时该怎么办？» 的 key 都是 `p005时该怎么办`。

    后果不是"多一页"而是"**少一页 + 串题**"：`taken` 不触发（两个 key 相等），
    `previous_answer` 把甲的答案当"上一版"喂给乙的 prompt，而 prompt 铁律 11
    明写「把它当草稿修订」——正是 C1 那道防线要拦的场景，从身份键这一侧绕了回来。"""
    assert Q.question_key(a) != Q.question_key(b), (a, b)
    assert Q.qa_slug(a) != Q.qa_slug(b), (a, b)


def test_narrowing_the_drop_set_did_not_break_punctuation_tolerance():
    """收窄不能矫枉过正：A3 的目标（少打一个问号仍然合并）必须原样成立。
    这一条与上面那条是一对，缺任何一边都会让 `_KEY_DROP_RE` 滑到另一个极端。"""
    for a, b in [("能不能做？", "能不能做"), ("能不能做。", "能不能做"),
                 ("A 与 B，哪个好", "A 与 B, 哪个好"), ("什么是 MAR", "什么是MAR"),
                 ("「引用」怎么写", "引用怎么写"), ("能不能做——真的吗", "能不能做 真的吗"),
                 ("A（B）C", "A B C"), ("测试…结束", "测试结束")]:
        assert Q.question_key(a) == Q.question_key(b), (a, b)


def test_opposite_questions_do_not_share_a_page_via_previous_answer(tmp_path):
    """A6 的真正后果在这里：两个 key 相等时 `taken` 不触发，甲的整页答案被乙覆盖，
    而 `previous_answer` 还会把甲的答案当"上一版"喂进乙的 prompt。"""
    d = tmp_path / "qa"
    Q.write_qa_page(d, "qa-x", "p<0.05 时该怎么办？", _QA, EVS, T.ValidationReport())
    _p, status = Q.write_qa_page(d, "qa-x", "p>0.05 时该怎么办？", _QA, EVS,
                                 T.ValidationReport())
    assert status == "taken"
    assert Q.previous_answer(d, "qa-x", "p>0.05 时该怎么办？") == ""


def test_normalize_question_keeps_the_question_readable():
    """归一化只用于**身份判定**，不许把展示用的问题原文改坏——标题、frontmatter 的
    `question`、检索 query 用的都是它。"""
    q = "MNAR 诊断在纵向 EHR 上到底能不能做？"
    assert Q.normalize_question("  {}\n".format(q)) == q
    assert Q.normalize_question("MNAR​ 能不能查") == "MNAR 能不能查"


def test_embedding_dedup_catches_a_rephrased_question(tmp_path):
    """词面 Jaccard 漏的正是三个月后真正会发生的那种重问（实测 0.10~0.17 全漏），
    报的是完全不同的问题（0.42~0.67）。问答召回本来就要连 Ollama，多算一次几乎不加成本。"""
    d = tmp_path / "qa"
    old = "MNAR 诊断在纵向 EHR 上能不能做"
    new = "在纵向电子健康记录上能否判断非随机缺失"
    _archive(d, "qa-old", old)
    assert Q.question_similarity(old, new) < 0.35, "前提：这两句词面确实不像"
    emb = FakeEmbed({old: [1, 0, 0, 0], new: [0.97, 0.24, 0, 0]})
    res = Q.find_similar_questions(d, new, embed_client=emb)
    assert res.mode == "embedding" and not res.degraded
    assert [h.slug for h, _s in res.hits] == ["qa-old"]


def test_embedding_dedup_does_not_fire_on_opposite_questions(tmp_path):
    """词面档最糟的一例：`«缺失机制可检验吗»` vs `«缺失机制不可检验吗»` = 0.67，
    语义相反却高分。语义档要能把它压下去。"""
    d = tmp_path / "qa"
    _archive(d, "qa-old", "缺失机制可检验吗")
    emb = FakeEmbed({"缺失机制可检验吗": [1, 0, 0, 0], "缺失机制不可检验吗": [0.5, 0.87, 0, 0]})
    assert Q.find_similar_questions(d, "缺失机制不可检验吗", embed_client=emb).hits == []


def test_dedup_degrades_to_lexical_and_says_so(tmp_path):
    """降级本身不是问题，**静默降级**才是：用户会以为语义查重跑过了。"""
    d = tmp_path / "qa"
    _archive(d, "qa-old", "MNAR 诊断在纵向 EHR 上能不能做")
    res = Q.find_similar_questions(d, "MNAR 诊断在纵向 EHR 上到底能不能做",
                                   embed_client=FakeEmbed({}, fail=True))
    assert res.mode == "lexical"
    assert "Ollama" in res.degraded or "embedding" in res.degraded
    assert [h.slug for h, _s in res.hits] == ["qa-old"]      # 兜底仍要工作


def test_dedup_hint_is_an_executable_command():
    """`--verify` 的错误提示都知道要写 `--slug <slug>`，查重提示反而只说
    "确认是新问题就继续"——它没告诉用户**怎么合并到那一页**。"""
    cmd = Q.update_command("qa-mnar-ehr-1f424623", "MNAR 诊断在纵向 EHR 上能不能做？")
    assert "--slug qa-mnar-ehr-1f424623" in cmd and "ask_notes.py" in cmd


def test_update_command_prints_the_pages_own_question_not_a_new_one():
    """A2：首版印的是 `"<你的新问法>" --slug <slug>`，而 slug 占用检查用
    `question_key` 比对——**任何真正的"新问法"都是另一个键，必然被拒**：

        ❌ slug 'qa-mnar-ehr-1f424623' 已经属于另一个问题：«…»  EXIT=2

    代码保证这条"可直接粘的更新命令"会失败。改成印**那一页自己的问题**
    （错误文案里早就这么做了，是对的），命令才真的能跑。"""
    import inspect
    sig = inspect.signature(Q.update_command)
    assert sig.parameters["question"].default is inspect.Parameter.empty, (
        "question 不能有「新问法」这类默认值——那正是会被 taken 拒掉的那个")
    # 印出来的必须是那一页自己的问题，粘回去能通过 question_key 比对
    q = "MNAR 诊断在纵向 EHR 上能不能做？"
    cmd = Q.update_command("qa-x", q)
    assert q in cmd
    assert "新问法" not in cmd


@pytest.mark.parametrize("q", [
    '带"半角双引号"的问题',
    "带 `反引号 $(whoami)` 的问题",
    "带 $HOME 变量的问题",
    "带 \\ 反斜杠的问题",
])
def test_update_command_is_shell_safe(q):
    """B7：`'"{}" --slug {}'.format(...)` 不转义。问题含 `"` 时打出来的命令直接是坏的；
    含反引号或 `$(...)` 时粘进 shell 还会**命令替换**——这条命令是印给人复制粘贴的。"""
    import shlex
    cmd = Q.update_command("qa-x", q)
    parts = shlex.split(cmd)          # 坏引号会在这里抛 ValueError
    assert q in parts, (cmd, parts)
    assert "--slug" in parts and parts[parts.index("--slug") + 1] == "qa-x"


# ---------------------------------------------------------------------------
# 13. A4：与概念页的边界
# ---------------------------------------------------------------------------

def test_topic_page_answering_the_same_question_is_surfaced():
    """同一个问题，库里已经有一页更大（70 条证据）、100% 引用率、随新论文自动保鲜
    的答案，而 P4 首版没有任何一处告诉用户。"""
    specs = [T.TopicSpec(slug="mnar-diagnosis", title="MNAR 与缺失机制诊断",
                         question="判断 EHR 数据的缺失是否属于 MNAR，现有方法有哪些？",
                         queries=["q"]),
             T.TopicSpec(slug="fairness", title="公平性", question="公平性怎么度量？",
                         queries=["q"])]
    q = "MNAR 诊断在纵向 EHR 上到底能不能做"
    emb = FakeEmbed({q: [1, 0, 0, 0],
                     specs[0].question: [0.96, 0.28, 0, 0],
                     specs[1].question: [0, 1, 0, 0]})
    hits = Q.find_similar_topics(specs, q, emb)
    assert [s.slug for s, _sc in hits] == ["mnar-diagnosis"]


def test_related_topic_is_linked_at_the_top_of_the_page():
    block = Q.render_qa_block("问题", _QA, EVS, related_topics=["mnar-diagnosis"])
    assert "[[mnar-diagnosis]]" in block
    assert block.index("mnar-diagnosis") < block.index("## 依据")


def test_topic_matching_is_skipped_without_an_embedding_client():
    specs = [T.TopicSpec(slug="s", title="t", question="q", queries=["q"])]
    assert Q.find_similar_topics(specs, "任何问题", None) == []


def test_docs_carry_the_three_way_routing_table():
    """"什么时候不该用它"必须写在两份文档里——功能有价值，但对已有概念页覆盖的问题
    它不值那 90 秒。"""
    for p in ["docs/scholar_notes_AGENTS.md", "docs/skills/scholar-notes/SKILL.md"]:
        text = Path(p).read_text(encoding="utf-8")
        assert "notes_query.py" in text and "ask_notes.py" in text
        assert "自动保鲜" in text or "自动重合成" in text, p
        assert "sync_vault.py --vault-dir" in text, p        # B3：急用时怎么同步


# ---------------------------------------------------------------------------
# 14. C1：slug 被复用时不许静默覆盖另一个问题
# ---------------------------------------------------------------------------

def test_reusing_a_slug_for_another_question_is_refused(tmp_path):
    """`--verify` 的报错文案恰恰在教用户手打 `--slug`。实测：第二次 status=merged、
    图标 ✅、退出码 0，甲的整块答案被替换，`first_asked_at` 却继承自甲
    （看起来像连续历史）。"""
    d = tmp_path / "qa"
    Q.write_qa_page(d, "qa-x", "问题甲：MNAR 能不能诊断", _QA, EVS, T.ValidationReport())
    before = (d / "qa-x.md").read_text(encoding="utf-8")
    path, status = Q.write_qa_page(d, "qa-x", "问题乙：图神经网络怎么调参",
                                   _QA, EVS, T.ValidationReport())
    assert status == "taken"
    assert path.read_text(encoding="utf-8") == before, "甲那一页必须原样不动"


def test_slug_reuse_check_tolerates_punctuation_drift(tmp_path):
    """同一个问题少打一个问号不该被判成"别人的 slug"——身份口径必须与 slug 一致。"""
    d = tmp_path / "qa"
    Q.write_qa_page(d, "qa-x", "MNAR 能不能诊断？", _QA, EVS, T.ValidationReport())
    _p, status = Q.write_qa_page(d, "qa-x", "MNAR 能不能诊断", _QA, EVS, T.ValidationReport())
    assert status in ("merged", "unchanged")


def test_previous_answer_is_not_leaked_across_questions(tmp_path):
    """最坏的一条：prompt 铁律写着「有上一版答案时把它当草稿修订」，于是模型会去
    **融合两个不相干的问题**。任何早退都救不了 prompt 污染，这里必须自己拦。"""
    d = tmp_path / "qa"
    Q.write_qa_page(d, "qa-x", "问题甲：MNAR 能不能诊断", _QA, EVS, T.ValidationReport())
    assert Q.previous_answer(d, "qa-x", "问题甲：MNAR 能不能诊断")
    assert Q.previous_answer(d, "qa-x", "问题乙：图神经网络怎么调参") == ""


# ---------------------------------------------------------------------------
# 15. C2：answer 非空但论断全被丢弃时不许落盘
# ---------------------------------------------------------------------------

def test_answer_alone_is_not_archivable():
    """`answer` 被允许无编号的**前提**是"它是下面各条论断的概括"。points 与 caveats
    全没了，这个前提就不成立，剩下的是一句纯 LLM 断言 + 一张全 ○ 的证据表。"""
    qa, _rep = Q.validate_qa({"answer": "库里证据显示 A 优于 B",
                              "points": [{"text": "无出处", "evidence": []}],
                              "caveats": [], "gaps": []}, EVS)
    assert qa["answer"] and not qa["points"] and not qa["caveats"]
    assert not Q.is_archivable(qa)


def test_caveats_alone_are_archivable():
    """同一行还有**反向**的错：points 为空但 caveats 非空（有出处）时被判成
    "什么都没剩下"而退出 2，丢掉合法结果。"""
    qa, _rep = Q.validate_qa({"answer": "", "points": [],
                              "caveats": [{"text": "有出处的限制", "evidence": ["E2"]}],
                              "gaps": []}, EVS)
    assert Q.is_archivable(qa)


def test_points_alone_are_archivable():
    qa, _rep = Q.validate_qa({"answer": "", "gaps": [],
                              "points": [{"text": "有出处", "evidence": ["E1"]}]}, EVS)
    assert Q.is_archivable(qa)


# ---------------------------------------------------------------------------
# 16. B1：宽浅切片漏掉 load-bearing 的那一条
# ---------------------------------------------------------------------------

def _cands(n_papers, per_paper):
    out = []
    for p in range(n_papers):
        for j in range(per_paper):
            out.append(T.Evidence(ref="", citekey="p{:02d}".format(p), text="s{}{}".format(p, j),
                                  role="citable", section=None, note_file="n.md", note_line=1,
                                  score=0.9 - p * 0.01 - j * 0.001))
    return out


def _thin_pool(n_papers=33, n_evidence=41):
    """**真实候选池的形状**：33 篇 / 41 条 = 每篇平均 1.24 条。

    这是线上那个问题实测的候选池，也是既有两条 fixture 正好绕开的那一档：
    `_cands(40, 4)` 每篇都填满 cap（于是 `ceil()` 预截断刚好够用），
    `_cands(3, 2)` 低于截断阈值（走不到分支）。**只有"篇数多、每篇很薄"这一档
    能暴露预截断的空格子不回补**——实测 41 条候选被砍到 16 条。"""
    out = []
    for p in range(n_papers):
        n = 2 if p < (n_evidence - n_papers) else 1     # 前 8 篇 2 条，其余各 1 条
        for j in range(n):
            out.append(T.Evidence(ref="", citekey="p{:02d}".format(p), text="s{}{}".format(p, j),
                                  role="citable", section=None, note_file="n.md", note_line=1,
                                  score=0.9 - p * 0.001 - j * 0.0001))
    return out


def test_qa_retrieval_is_narrow_and_deep_not_one_sentence_from_forty_papers():
    """`select_evidence` 是轮次制：候选论文远超 max_evidence 时**第 0 轮就填满**，
    于是 per_paper_cap 在问答场景下永远不生效，产物必然是「40 篇各 1 句」。
    概念页要横扫全库，轮次制是对的；一个具体问题要的是把最相关那几篇挖深。"""
    cands = _cands(40, 4)
    wide = T.select_evidence(cands, 40, 4)
    assert len({e.citekey for e in wide}) == 40                 # 现状：40 篇各 1 句

    narrow = Q.select_qa_evidence(cands, max_evidence=28, per_paper_cap=3)
    n_papers = len({e.citekey for e in narrow})
    assert n_papers <= 10, "窄而深：论文数必须先被截断，否则轮次制会再次摊平"
    top = narrow[0].citekey
    assert sum(1 for e in narrow if e.citekey == top) >= 2, "最相关那篇必须挖到第 2 句"
    assert [e.ref for e in narrow] == ["E{}".format(i + 1) for i in range(len(narrow))]


def test_qa_retrieval_keeps_everything_when_the_pool_is_small():
    """候选论文数没超上限时不该做任何截断——窄而深是给"候选爆表"准备的。"""
    cands = _cands(3, 2)
    out = Q.select_qa_evidence(cands, max_evidence=28, per_paper_cap=3)
    assert len(out) == 6 and len({e.citekey for e in out}) == 3


def test_qa_defaults_are_narrower_than_the_concept_page_defaults():
    assert Q.DEFAULT_QA_MAX_EVIDENCE <= 30
    assert Q.DEFAULT_QA_PER_PAPER_CAP <= 3 < T.DEFAULT_PER_PAPER_CAP


def test_a_thin_pool_is_not_silently_underfilled():
    """A4【本轮最贵的一条】`max_papers = ceil(max_evidence / cap)` 是**预截断**：
    先砍到 10 篇，而留下的 10 篇根本填不满各自的 cap，空出来的 12 个格子不回补。

    真实数据实测：候选池 41 条 / 33 篇 → 新口径只剩 **16 条 / 10 篇**，
    旧口径 `select_evidence(28,3)` 给的是 28 条 / 28 篇。被砍掉的 18 篇里有三条
    直接回答那个问题、分数只比第 10 名低 0.006~0.010。

    单测测不出来是因为既有 fixture 每篇都填满 cap；`_thin_pool` 就是那个真实形状。"""
    pool = _thin_pool()
    assert len(pool) == 41 and len({e.citekey for e in pool}) == 33, "前提：33 篇 × 1.24 条"
    out = Q.select_qa_evidence(pool, max_evidence=28, per_paper_cap=3)
    assert len(out) == 28, "池子够深就必须填满 28 个格子，不许静默欠填（实测欠成 16）"
    assert len({e.citekey for e in out}) >= 20, "薄池子只能靠多铺论文填满"


def test_raising_per_paper_cap_never_returns_less_evidence():
    """`--per-paper-cap` 是 CLI 暴露的参数，帮助文案写"单篇最多贡献几条证据"，
    用户按字面理解调大以求"挖更深"。编排者实测得到的却是**单调递减**：
    cap 1 → 28 条 / 28 篇，cap 3 → 16 条 / 10 篇，cap 28 → **1 条 / 1 篇**。
    合成 prompt 拿到 1 条证据会写出什么不用测。"""
    pool = _thin_pool()
    counts = [len(Q.select_qa_evidence(pool, max_evidence=28, per_paper_cap=c))
              for c in (1, 2, 3, 5, 10, 28)]
    assert counts == sorted(counts), "调大 cap 不许让证据变少：{}".format(counts)
    assert min(counts) == 28, "薄池子在任何 cap 下都该填满：{}".format(counts)


def test_a_cap_at_or_above_max_evidence_degrades_to_the_old_behaviour():
    """`per_paper_cap >= max_evidence` 时截断这一层没有任何意义（一篇本来就装得下
    全部名额），必须等价于旧口径——否则 `--per-paper-cap 28` 会退化成 1 篇 1 条。"""
    pool = _thin_pool()
    assert (Q.select_qa_evidence(pool, max_evidence=28, per_paper_cap=28)
            == T.select_evidence(pool, 28, 28))


def test_a_nonpositive_cap_is_rejected_instead_of_silently_becoming_one():
    """B7：`max(1, per_paper_cap)` 把 `--per-paper-cap 0` 静默当成 1。
    用户打 0 多半是想"不限制"，拿到的却是最窄的那一档，而且没有任何提示。"""
    for bad in (0, -1, -3):
        with pytest.raises(T.TopicError):
            Q.select_qa_evidence(_thin_pool(), max_evidence=28, per_paper_cap=bad)


# ---------------------------------------------------------------------------
# 17. B4/B5/B6/B7/B8/B9：其余
# ---------------------------------------------------------------------------

def test_refs_back_translated_from_free_text_count_as_used():
    """B4：`answer` 里回译出的引用不计入 `used_refs`，证据表把它标 ○，而表头写着
    "未被引用的证据标 ○"。两个数字互相自洽，但都与页面事实不符。"""
    qa, rep = Q.validate_qa({"answer": "综合看，E3 是关键", "points": [], "gaps": [],
                             "caveats": [{"text": "限制", "evidence": ["E1"]}]}, EVS)
    assert "[@c2024Z]" in qa["answer"]
    assert rep.used_refs == 2                       # E1（caveat）+ E3（正文回译）
    block = Q.render_qa_block("问题", qa, EVS)
    assert "● **E3**" in block and "○ **E3**" not in block


def test_inline_refs_in_gaps_also_count_as_used():
    qa, rep = Q.validate_qa({"answer": "", "gaps": ["缺 E2 那一类的实证"],
                             "points": [{"text": "p", "evidence": ["E1"]}]}, EVS)
    assert rep.used_refs == 2
    assert "● **E2**" in Q.render_qa_block("问题", qa, EVS)


@pytest.mark.parametrize("prev", [
    _dt.date(2026, 1, 1),          # YAML 裸日期：在 Obsidian 属性面板里手改一下就是这个
    _dt.datetime(2026, 1, 1, 9),
    20260101,
])
def test_first_asked_at_survives_non_string_frontmatter(prev):
    """B5：实测只有带引号的字符串能保住，其余形态一律静默重置成"今天"，
    状态还是 `merged`、无任何提示。裸日期那行是最现实的一种。"""
    fm = Q.build_qa_frontmatter("问题", "qa-x", _QA, EVS, T.ValidationReport(),
                                "2026-08-17T00:00:00", preserved={"first_asked_at": prev})
    assert "2026-01-01" in fm or "20260101" in fm
    assert "first_asked_at: \"2026-08-17" not in fm


def test_empty_first_asked_at_is_reset():
    fm = Q.build_qa_frontmatter("问题", "qa-x", _QA, EVS, T.ValidationReport(),
                                "2026-08-17T00:00:00", preserved={"first_asked_at": ""})
    assert "2026-08-17" in fm


def test_bracketed_evidence_numbers_do_not_leave_nested_brackets():
    """B6：喂给模型的证据块首字段就是 `[E1]（可引用证据 · …）`，模仿这个形状的概率不低。
    实测 `'[E1] 说了'` → `'[[@a2024X]] 说了'`，再过 `to_wiki_links` →
    `'[[[a2024X|a2024X]]] …'`——不是死链但多一对字面方括号，粘进 pandoc 也多一层。"""
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    assert Q.backtranslate_inline_refs("[E1] 说了这件事", ev_map, rep) == "[@a2024X] 说了这件事"
    assert "[[" not in Q.backtranslate_inline_refs("见 [E2]", ev_map, rep)


@pytest.mark.parametrize("text,want_key", [
    ("(E1) 括号写法", "a2024X"),
    ("第E1条", "a2024X"),
    ("E01 前导零", "a2024X"),
])
def test_bracket_change_did_not_break_the_audited_boundaries(text, want_key):
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    assert want_key in Q.backtranslate_inline_refs(text, ev_map, rep)


@pytest.mark.parametrize("text", [
    "AE1 是个缩写", "MICE1 方法", "COVID19 队列",
    # M2：三个既有参数 E 前面**全是字母**，数字侧一个都没测——把左侧后顾断言
    # 从 `(?<![A-Za-z0-9])` 削成 `(?<![A-Za-z])` 的变异整条逃逸。
    "2.5E3 是科学计数法", "1E2 也是", "0E1", "浓度 3E4 mol",
    "E-1 不是编号", "E 单独一个字母",
])
def test_no_false_match_on_either_side(text):
    ev_map = {e.ref: e for e in EVS}
    rep = T.ValidationReport()
    assert Q.backtranslate_inline_refs(text, ev_map, rep) == text
    assert rep.invalid_refs == 0


@pytest.mark.parametrize("bad_text", ["", "   ", "[@fake2020Ghost]"])
def test_a_claim_with_no_prose_is_dropped(bad_text):
    """M3：`if not text or not refs` 被改成 `if not refs` 时没有任何用例会红——
    落盘的会是一条只有 `[@key]`、没有任何论断的 bullet。第三个参数是被
    `_clean_text` 剥成空串的那种（模型只写了一个自造引用当正文）。"""
    qa, rep = Q.validate_qa({"answer": "", "gaps": [],
                             "points": [{"text": bad_text, "evidence": ["E1"]}]}, EVS)
    assert qa["points"] == []
    assert rep.dropped_claims == 1


def test_evidence_row_regex_is_shared_with_topics():
    """B8：两份逐字拷贝，正是 `_render_frontmatter`（G4）栽过的形状。"""
    assert Q._EVIDENCE_ROW_RE is T._EVIDENCE_ROW_RE


def test_render_qa_block_has_no_dead_parameters():
    import inspect
    params = set(inspect.signature(Q.render_qa_block).parameters)
    assert "asked_at" not in params and "n_similar" not in params


def test_qa_module_has_no_unused_imports():
    import ast
    tree = ast.parse(Path("src/scholar/qa.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.value.id for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert not (imported - used), "未使用的导入：{}".format(sorted(imported - used))


def test_docstring_is_honest_about_the_vector_store_debt():
    """B9：把"不进向量库"写成经过权衡的独立取舍，但概念页本来也不在向量库里
    （`embed_store.py` 里一个 `topic` 字样都没有）——这是沿用 P1 的已知欠账，
    不是这一轮做的新决定。"""
    doc = Q.__doc__ or ""
    assert "欠账" in doc and "概念页" in doc


# ---------------------------------------------------------------------------
# 19. A1：改身份键把存量页孤立了，而 --verify 报绿
# ---------------------------------------------------------------------------

def test_a_page_written_under_the_old_digest_stays_on_the_automatic_path(tmp_path):
    """A1【上一轮自己造的】`qa_slug` 的 digest 从 `normalize_question`（展示口径）
    改到 `question_key`（身份口径）之后，**所有 v1 口径生成的 slug 全部作废**。
    CLI 实测：同一个问题、逐字节相同的文本，查重说「100% 像 qa-mnar-ehr-1f424623」，
    下一行 dry-run 却说归档路径会是 `qa-mnar-ehr-82bb128f`——磁盘那页从此是孤儿。

    修法同 `scholar-pub-date-precision` 里「regen 沿用键」：**旧文件存在就沿用旧 slug**。"""
    import hashlib
    d = tmp_path / "qa"
    q = "MNAR 诊断在纵向 EHR 上到底能不能做？"
    norm = Q.normalize_question(q)
    legacy_digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    new_slug = Q.qa_slug(q)
    legacy_slug = new_slug.rsplit("-", 1)[0] + "-" + legacy_digest
    assert legacy_slug != new_slug, "前提：两种口径确实给出不同的 slug"

    # 没有旧文件时走新口径（新问题不该被旧口径污染）
    assert Q.qa_slug(q, qa_dir=d) == new_slug
    # 旧文件存在就沿用它——存量页自动回到自动路径上
    _archive(d, legacy_slug, q)
    assert Q.qa_slug(q, qa_dir=d) == legacy_slug
    # 但新口径那页一旦存在就以它为准（不许把两页都当成"沿用"）
    _archive(d, new_slug, q)
    assert Q.qa_slug(q, qa_dir=d) == new_slug


def test_legacy_slug_fallback_only_fires_for_that_exact_question(tmp_path):
    """沿用旧键不能变成"随便撞上一页就认领"：旧文件里躺的必须是**同一个问题**。"""
    import hashlib
    d = tmp_path / "qa"
    q = "MNAR 诊断在纵向 EHR 上到底能不能做？"
    legacy = (Q.qa_slug(q).rsplit("-", 1)[0] + "-"
              + hashlib.sha1(Q.normalize_question(q).encode("utf-8")).hexdigest()[:8])
    _archive(d, legacy, "完全不相干的另一个问题：图神经网络怎么调参")
    assert Q.qa_slug(q, qa_dir=d) == Q.qa_slug(q), "旧文件上是别人的问题，不许认领"


def test_verify_reports_a_page_whose_filename_no_longer_matches_its_question(tmp_path):
    """A1 的第一件：`--verify` 检查了死键/失锚/残留编号/防线版本四件事，
    **唯独没检查「再问一次这个问题还找不找得回这一页」**——上一轮亲手造出来的
    孤儿页在所有仪表上都是绿的。1 页时代价是多一份重复，50 页时是整个归档翻倍且
    `--list` 并排列出两条一模一样的问题。

    ⚠️ 不变式是「**可达性**」而不是「文件名好不好看」。`qa_slug(…, qa_dir=)` 按
    **身份**扫目录，所以一页名字再怪，只要它是这个身份**唯一**的那一页，
    重问就能找回它——那就没有任何东西需要修，报它是纯噪音。
    真正的伤害是**同一个身份有两页**：那时扫描只会认领其中一页，另一页从此不可达，
    而这恰恰就是"整个归档翻倍"的机制。"""
    d = tmp_path / "qa"
    q = "MNAR 诊断在纵向 EHR 上到底能不能做？"

    # 名字很怪但**唯一**：可达，不报
    _archive(d, "qa-手写的名字与问题无关", q)
    lone = Q.audit_qa_pages(d, {"papers": []})[0]
    assert not lone.slug_mismatch, "唯一那页即使名字怪也是可达的，报它是噪音"
    assert lone.ok

    # 同一个身份出现第二页 → 必有一页不可达，必须报出来并给出它该有的名字
    _archive(d, Q.qa_slug(q), q, at="2026-08-18T09:00:00")
    audits = Q.audit_qa_pages(d, {"papers": []})
    stranded = [a for a in audits if a.slug_mismatch]
    assert len(stranded) == 1, "两页同身份时必须恰好报出那一页不可达的"
    assert not stranded[0].ok, "纳入 ok，否则这一页永远没人会去修"
    # 报出来的"该有的名字"必须是重问时真正会落到的那一页
    assert stranded[0].slug_mismatch == Q.qa_slug(q, qa_dir=d)


def test_a_lone_page_stays_reachable_whatever_its_filename(tmp_path):
    """上一条的正面：可达性不靠文件名，靠身份扫描。这一条锁住"别为了好看去改名"。"""
    d = tmp_path / "qa"
    q = "缺失机制到底可不可检验"
    _archive(d, "qa-随便起的", q)
    assert Q.qa_slug(q, qa_dir=d) == "qa-随便起的"
    assert Q.qa_slug(q + "？", qa_dir=d) == "qa-随便起的"      # 少打问号也找得回


def test_verify_does_not_flag_a_legacy_page_that_the_fallback_reclaimed(tmp_path):
    """两件修法必须一起成立：旧口径页面被 `qa_slug` 的回退认领之后，
    它**已经回到自动路径上**了，`--verify` 再报它就是纯噪音。"""
    import hashlib
    d = tmp_path / "qa"
    q = "MNAR 诊断在纵向 EHR 上到底能不能做？"
    legacy = (Q.qa_slug(q).rsplit("-", 1)[0] + "-"
              + hashlib.sha1(Q.normalize_question(q).encode("utf-8")).hexdigest()[:8])
    _archive(d, legacy, q)
    a = Q.audit_qa_pages(d, {"papers": []})[0]
    assert not a.slug_mismatch and a.ok


def test_the_digest_migration_is_documented_where_it_bit(tmp_path):
    """A1 的第三件：口径迁移这件事必须写在 `qa_slug` 旁边，否则下一个人改 digest
    时会把同一个坑再踩一遍（MEMORY 里 `scholar-identity-keys` 那条教训的复刻）。"""
    doc = Q.qa_slug.__doc__ or ""
    assert "2026-08-17" in doc and "身份" in doc and "旧" in doc


def test_a_migration_orphan_is_labelled_as_such_not_as_a_new_question():
    """A1：查重报出 ≥0.99 的旧页时，提示语不能是那句会把人径直引去另存的
    「确认是新问题就继续」——那正是把归档翻倍的动作。"""
    old = Q.ArchivedQA(slug="qa-old", question="同一个问题", title="同一个问题",
                       path=Path("/tmp/qa-old.md"))
    assert Q.is_migration_orphan([(old, 0.995)], "同一个问题")
    assert not Q.is_migration_orphan([(old, 0.995)], "另一个完全不同的问题")
    assert not Q.is_migration_orphan([(old, 0.80)], "同一个问题"), "只有近乎逐字相同才算"
    assert not Q.is_migration_orphan([], "同一个问题")


# ---------------------------------------------------------------------------
# 20. A3-3：阈值这个**数**本身必须被测试锁住
# ---------------------------------------------------------------------------
#
# 上一轮审计实测：把 `find_similar_topics` 的阈值改成 0.95、把
# `DEFAULT_SIMILAR_EMBED_THRESHOLD` 改成 0.95，**测试全绿（398 passed）**——
# 因为那两条测试的 `FakeEmbed` 造的余弦是 0.96/0.97，对 0.80 与 0.95 一视同仁。
# 1359 passed 对这两个数零证明力。下面每个阈值配一对「刚过 / 刚不过」的用例，
# 余弦造在阈值两侧 ±0.02 内。

def _vec_at(cos):
    """与 `[1,0,0,0]` 夹角余弦恰好等于 `cos` 的单位向量。"""
    return [cos, float(np.sqrt(max(0.0, 1.0 - cos * cos))), 0, 0]


@pytest.mark.parametrize("cos,should_fire", [
    (Q.DEFAULT_TOPIC_MATCH_THRESHOLD + 0.02, True),
    (Q.DEFAULT_TOPIC_MATCH_THRESHOLD - 0.02, False),
])
def test_topic_match_threshold_is_pinned_to_its_calibrated_value(cos, should_fire):
    """A3-1：`DEFAULT_TOPIC_MATCH_THRESHOLD` 从 0.80 降到 0.60 是有实测依据的
    （见常量旁的分档表：0.80 时正例只剩 4/17）。这一对用例锁的是**那个数**——
    把它改回 0.80 或改到 0.95，两条里必有一条红。"""
    specs = [T.TopicSpec(slug="s", title="t", question="概念页的问题", queries=["q"])]
    emb = FakeEmbed({"提问": [1, 0, 0, 0], "概念页的问题": _vec_at(cos)})
    assert bool(Q.find_similar_topics(specs, "提问", emb)) is should_fire


@pytest.mark.parametrize("cos,should_fire", [
    (Q.DEFAULT_SIMILAR_EMBED_THRESHOLD + 0.02, True),
    (Q.DEFAULT_SIMILAR_EMBED_THRESHOLD - 0.02, False),
])
def test_dedup_embed_threshold_is_pinned_to_its_calibrated_value(tmp_path, cos, should_fire):
    """A3-2：0.80 时同题只认 4/6——实测「在纵向电子健康记录上能否诊断非随机缺失？」
    对「MNAR 诊断在纵向 EHR 上到底能不能做？」只有 0.661，而那**正是
    `question_similarity` docstring 拿来论证"必须换 embedding"的旗舰案例**。
    换了 embedding 仍然漏，因为阈值没跟着改。"""
    d = tmp_path / "qa"
    _archive(d, "qa-old", "旧问题")
    emb = FakeEmbed({"新问题": [1, 0, 0, 0], "旧问题": _vec_at(cos)})
    res = Q.find_similar_questions(d, "新问题", embed_client=emb)
    assert bool(res.hits) is should_fire


def test_dedup_still_shows_the_top_hit_below_the_threshold(tmp_path):
    """A3-2 的第二件：真重问区间（0.661~0.997）与真不同角度（0.813）**重叠**，
    不存在能干净分开的阈值。所以没有任何一条过阈值时，仍要打印 top-1 + 分数
    作为「参考（未达提示阈值）」——这一节本来就是提示不是闸门。"""
    d = tmp_path / "qa"
    _archive(d, "qa-near", "沾点边的旧问题")
    _archive(d, "qa-far", "完全不相干的旧问题")
    emb = FakeEmbed({"新问题": [1, 0, 0, 0],
                     "沾点边的旧问题": _vec_at(Q.DEFAULT_SIMILAR_EMBED_THRESHOLD - 0.05),
                     "完全不相干的旧问题": _vec_at(0.10)})
    res = Q.find_similar_questions(d, "新问题", embed_client=emb)
    assert res.hits == []
    assert [p.slug for p, _s in res.below] == ["qa-near"], "未达阈值也要给一条参考"
    assert res.below[0][1] == pytest.approx(
        Q.DEFAULT_SIMILAR_EMBED_THRESHOLD - 0.05, abs=1e-4)


def test_below_threshold_reference_is_empty_once_something_fires(tmp_path):
    """过阈值时就别再印"参考"了——两份名单同时出现只会让人分不清哪条要紧。"""
    d = tmp_path / "qa"
    _archive(d, "qa-hit", "旧问题")
    emb = FakeEmbed({"新问题": [1, 0, 0, 0], "旧问题": _vec_at(0.97)})
    res = Q.find_similar_questions(d, "新问题", embed_client=emb)
    assert res.hits and res.below == []


def test_threshold_constants_carry_their_calibration_table():
    """P3 那次 `min_sim` 的教训：形状锁住了、数值没锁。这一条要求每个阈值旁边
    都留下**实测分档表**（格式对齐 `topics.DEFAULT_MIN_SIM`），
    否则下一个人只能靠拍脑袋调它。"""
    src = Path("src/scholar/qa.py").read_text(encoding="utf-8")
    for name in ("DEFAULT_TOPIC_MATCH_THRESHOLD", "DEFAULT_SIMILAR_EMBED_THRESHOLD",
                 "DEFAULT_GAP_EVIDENCE_MIN_SIM"):
        head = src[:src.index("\n{} =".format(name))]
        block = head[head.rindex("\n\n"):]
        assert "阈值 0." in block, "{} 缺实测分档表".format(name)
        assert "bge-m3" in block, "{} 的分档表没写清楚是拿什么模型标的".format(name)


# ---------------------------------------------------------------------------
# 21. A5：gap 回查的句级通道在"库里确实没有"的 gap 上假阳性 5/5
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gap,want", [
    ("本次证据没有专门系统讨论「MNAR 在观测数据上不可识别」这一根本问题。",
     "「MNAR 在观测数据上不可识别」这一根本问题"),
    ("本次这批证据里没有一条讨论影子变量在临床数据中的可得性。",
     "影子变量在临床数据中的可得性"),
    ("上面的证据没有覆盖跨 ICU 数据库外部验证时的性能衰减。",
     "跨 ICU 数据库外部验证时的性能衰减"),
    ("本次证据未涉及插补在什么条件下会引入偏倚。", "插补在什么条件下会引入偏倚"),
    ("本次这 28 条证据里没有一条对 MNAR 检验做过实证功效评估。",
     "对 MNAR 检验做过实证功效评估"),
    ("本次证据缺少注意力权重可解释性被质疑的实验条件。",
     "注意力权重可解释性被质疑的实验条件"),
])
def test_gap_scaffold_is_stripped_before_the_recheck_query(gap, want):
    """A5 第一件：gap 文本是**否定句**而 embedding 不编码否定。
    「本次证据没有…」这段共有脚手架 + 学术中文语域，对任意 highlight chunk 的余弦
    本来就在 0.6 左右——所以拿原句回查等于在问"哪些句子长得像这句否定句"，
    而不是"库里有没有 X"。实测 5 条构造成"库里绝对没有"的 gap 假阳性 **5/5**。"""
    assert Q.strip_gap_scaffold(gap) == want


@pytest.mark.parametrize("text", [
    "缺失机制的可检验性",                 # 「缺失」是领域词，不许被当成「缺少」啃掉
    "缺失指示符的建模方式",
    "未观测混杂的处理办法",               # 「未观测」同理
    "系统性偏差的来源",
])
def test_stripping_does_not_eat_domain_terms(text):
    """剥离器最容易做歪的地方：把 `缺失机制` 的 `缺` 当成否定词吃掉，
    留下 `失机制的可检验性` 去检索。这一条是那个方向的反向保险。"""
    assert Q.strip_gap_scaffold(text) == text


@pytest.mark.parametrize("degenerate", ["本次证据没有。", "没有", "  ", "未涉及。"])
def test_stripping_falls_back_when_nothing_meaningful_is_left(degenerate):
    """剥完只剩两三个字（甚至空）时必须回退到原文——拿空串去检索会命中一堆噪音，
    比不回查还糟。"""
    out = Q.strip_gap_scaffold(degenerate)
    assert out == degenerate.strip() or len(out) >= 4


def test_gap_recheck_uses_the_stripped_text_not_the_negation_sentence():
    """两件必须一起做：只提阈值仍然分不开（共有脚手架让任意 highlight 都有 ~0.6 余弦）。
    这一条把"回查用的是剥完的正向内容"钉死——注入的 FakeEmbed 只认剥离后的文本。"""
    gap = "本次证据没有覆盖影子变量的可得性。"
    stripped = Q.strip_gap_scaffold(gap)
    assert stripped != gap
    store = FakeStore([{"id": "c1", "level": "highlight", "citekey": "chen2026Partial"}],
                      [[1, 0, 0, 0]])
    # 只给剥离后的文本一个"命中"向量；原句给一个正交向量。回查若还用原句就命中不到。
    emb = FakeEmbed({stripped: [1, 0, 0, 0], gap: [0, 1, 0, 0]})
    hits = Q.recheck_gaps([gap], store=store, embed_client=emb)
    assert [h.key for h in hits[gap]] == ["chen2026Partial"]


def test_gap_evidence_channel_has_its_own_higher_threshold():
    """A5 第二件：句级通道的命中阈值提到实测分界。概念页通道保持 0.55 不动
    （审计原话"本轮唯一一个阈值取对了的地方"），两条通道从此不共用一个数。"""
    assert Q.DEFAULT_GAP_EVIDENCE_MIN_SIM > Q.DEFAULT_GAP_TOPIC_MIN_SIM
    assert Q.DEFAULT_GAP_TOPIC_MIN_SIM == T.DEFAULT_MIN_SIM


@pytest.mark.parametrize("cos,should_hit", [
    # ⚠️ 写死数字，不许写成 `DEFAULT_… ± 0.02`——余弦会跟着常量一起动，常量改成
    # 0.95 测试照样绿。另外两个阈值的用例都改过来了，**独独漏了这一个**
    # （第 3 轮验收逐条查出来的）。三个阈值同一个坑，第三次才补齐。
    (0.67, True),      # 0.65 之上
    (0.63, False),     # 0.65 之下
])
def test_gap_evidence_threshold_is_pinned_to_its_calibrated_value(cos, should_hit):
    """剥完脚手架之后重新标定的分界（见常量旁的分档表）。这一对用例锁的是那个数。"""
    gap = "本次证据没有覆盖影子变量的可得性。"
    stripped = Q.strip_gap_scaffold(gap)
    store = FakeStore([{"id": "c1", "level": "highlight", "citekey": "k2026A"}],
                      [_vec_at(cos)])
    emb = FakeEmbed({stripped: [1, 0, 0, 0]})
    hits = Q.recheck_gaps([gap], store=store, embed_client=emb)
    assert bool([h for h in hits[gap] if h.kind == "evidence"]) is should_hit


@pytest.mark.parametrize("cos,should_hit", [
    (Q.DEFAULT_GAP_TOPIC_MIN_SIM + 0.02, True),
    (Q.DEFAULT_GAP_TOPIC_MIN_SIM - 0.02, False),
])
def test_gap_topic_threshold_stays_at_the_calibrated_0_55(cos, should_hit):
    """概念页通道实测是干净的（真 gap 0.53~0.82 / 假 gap 0.24~0.50），
    这一对防止有人"顺手"把它跟着句级通道一起抬上去。"""
    gap = "本次证据没有覆盖影子变量的可得性。"
    stripped = Q.strip_gap_scaffold(gap)
    spec = T.TopicSpec(slug="shadow-variable", title="影子变量", question="概念页问题",
                       queries=["q"])
    emb = FakeEmbed({stripped: [1, 0, 0, 0], "概念页问题": _vec_at(cos)})
    hits = Q.recheck_gaps([gap], store=None, embed_client=emb, topic_specs=[spec])
    assert bool([h for h in hits[gap] if h.kind == "topic"]) is should_hit


def test_gap_recheck_looks_past_the_first_two_hits():
    """A5 第三件：实测 gap#2 的**第 3 名**命中是 `toye2025Benchmarking` 0.722，
    原句「全文 0 次出现 identifiability、ignorability、shadow variable、
    sensitivity analysis」**逐字回答了那条 gap**，也正是上一轮点名的漏网证据——
    被 `top_n=2` 扔了。"""
    gap = "本次证据没有覆盖影子变量的可得性。"
    stripped = Q.strip_gap_scaffold(gap)
    recs = [{"id": "c{}".format(i), "level": "highlight", "citekey": "k{}".format(i)}
            for i in range(5)]
    vecs = [_vec_at(0.90 - i * 0.02) for i in range(5)]        # 全部远高于阈值
    emb = FakeEmbed({stripped: [1, 0, 0, 0]})
    hits = Q.recheck_gaps([gap], store=FakeStore(recs, vecs), embed_client=emb)
    keys = [h.key for h in hits[gap] if h.kind == "evidence"]
    assert len(keys) >= 4, "top_n=2 会扔掉第 3 名，而第 3 名正是那条漏网证据"
    assert keys[:4] == ["k0", "k1", "k2", "k3"]


def test_gap_recheck_excludes_citekeys_already_on_this_page():
    """A5 第四件：线上那页 gap#2 留下的两条里，`zhang2026Newonset` 就是本页自己的
    E4/E18/E20——「库里可能有」指回同一页下面 30 行的东西，纯噪音。"""
    gap = "本次证据没有覆盖影子变量的可得性。"
    stripped = Q.strip_gap_scaffold(gap)
    store = FakeStore(
        [{"id": "c1", "level": "highlight", "citekey": "onpage2026"},
         {"id": "c2", "level": "highlight", "citekey": "elsewhere2026"}],
        [_vec_at(0.95), _vec_at(0.90)])
    emb = FakeEmbed({stripped: [1, 0, 0, 0]})
    hits = Q.recheck_gaps([gap], store=store, embed_client=emb,
                          exclude_citekeys={"onpage2026"})
    assert [h.key for h in hits[gap]] == ["elsewhere2026"]


def test_gap_recheck_survives_a_store_that_blows_up():
    """B7：`recheck_gaps` 只捕 `EmbeddingError`，`store.search` 没包在内。它位于
    LLM 调用**之后**——真抛了就是把已经付过费的整轮合成丢掉、什么都不落盘。"""
    class Boom:
        records = [{"id": "c1", "level": "highlight", "citekey": "k"}]

        def search(self, *a, **k):
            raise RuntimeError("向量库炸了")

    gap = "本次证据没有覆盖影子变量的可得性。"
    emb = FakeEmbed({Q.strip_gap_scaffold(gap): [1, 0, 0, 0]})
    assert Q.recheck_gaps([gap], store=Boom(), embed_client=emb) == {}


def test_topic_questions_are_embedded_once_per_run():
    """B7：一次运行里概念页的 8 个 question 被 embed **三遍**（查重一次、概念页比对
    一次、gap 回查一次）。算一次传下去。"""
    specs = [T.TopicSpec(slug="s", title="t", question="概念页问题", queries=["q"])]
    emb = FakeEmbed({"提问": [1, 0, 0, 0], "概念页问题": [0.97, 0.24, 0, 0]})
    hits = Q.find_similar_topics(specs, "提问", emb)
    assert hits and emb.calls == 1
    tvecs = Q.topic_vectors(specs, emb)          # 复用点：算一次，两处都能用
    emb2 = FakeEmbed({Q.strip_gap_scaffold("本次证据没有覆盖 X 的可得性。"): [1, 0, 0, 0]})
    Q.recheck_gaps(["本次证据没有覆盖 X 的可得性。"], store=None, embed_client=emb2,
                   topic_specs=specs, topic_vecs=tvecs)
    assert emb2.calls == 1, "传了预算好的概念页向量就不许再 embed 一遍"


# ---------------------------------------------------------------------------
# 22. B2/B6：可见性与陈旧轴
# ---------------------------------------------------------------------------

def test_papers_just_below_the_cut_stay_visible_on_the_page():
    """B2：旧版至少让漏网证据以 ○ 的身份躺在页面上（上一轮验收方就是这么逮到
    `little1988Test` 的）；窄而深之后 11 名以后的论文从页面上**彻底消失**，
    连被肉眼逮到的机会都没有。"""
    block = Q.render_qa_block("问题", _QA, EVS, nearby_papers=["near2026A", "near2026B"])
    assert "near2026A" in block and "near2026B" in block
    assert "本次未纳入" in block
    # 这一行里的 key **不是引用**：不许被 `_CITE_RE` 当成 citekey 算进 n_cites/死键
    assert "[@near2026A]" not in block


def test_nearby_papers_do_not_pollute_the_citation_audit(tmp_path):
    """B2 的配套：`--verify` 的死键检测是"粘进 pandoc 会渲染成 (key?)"的防线。
    「你可能还想看看」那一行的 key 从没被引用过，算进去就是假报警。"""
    d = tmp_path / "qa"
    Q.write_qa_page(d, Q.qa_slug("问题"), "问题", _QA, EVS, T.ValidationReport(),
                    nearby_papers=["ghost2026Never"])
    a = Q.audit_qa_pages(d, {"papers": [{"citekey": e.citekey,
                                         "highlights": [{"text": "证据句"}]}
                                        for e in EVS]})[0]
    assert "ghost2026Never" not in a.dead_keys
    assert a.ok


def test_nearby_papers_come_from_the_candidate_pool_below_the_cut():
    """名单要真的是"差一点就进来了"的那几篇，而不是随便几个 citekey。"""
    pool = _thin_pool()
    picked = Q.select_qa_evidence(pool, max_evidence=10, per_paper_cap=3)
    nearby = Q.nearby_papers(pool, picked, top_n=5)
    chosen = {e.citekey for e in picked}
    assert nearby and not (set(nearby) & chosen), "已入选的不该再列一遍"
    assert len(nearby) <= 5


def test_an_old_page_with_a_clean_local_scan_is_not_flagged_for_a_rerun(tmp_path):
    """B6：「防线版本」选错了陈旧轴。v3 一涨，100 页全标 ⚠️，其中绝大多数内容毫无问题，
    而重跑 100 页 × 90 秒 ≈ 2.5 小时且回退链只剩订阅那一路（会触顶）。
    **它会在不该响时全响。**凡是本地能扫出来的缺陷就不该靠"重跑一次 LLM"来修。"""
    d = tmp_path / "qa"
    d.mkdir(parents=True)
    q = "问题"
    fm = ('---\nqa: "{}"\ntitle: "问题"\nquestion: "问题"\ntype: "qa"\n'
          'generated_at: "2026-01-01T00:00:00"\n---').format(Q.qa_slug(q))
    (d / "{}.md".format(Q.qa_slug(q))).write_text(
        T.assemble(fm, Q.render_qa_block(q, _QA, EVS), T.DEFAULT_USER_ZONE,
                   generator=Q.QA_GENERATOR), encoding="utf-8")
    index = {"papers": [{"citekey": e.citekey, "highlights": [{"text": "证据句"}]}
                        for e in EVS]}
    a = Q.audit_qa_pages(d, index)[0]
    assert a.schema_version == 1 and a.outdated, "版本号本身照旧如实报告"
    assert a.ok
    assert not a.needs_rerun, "本地自检无异常的旧页不该被催着重跑"


def test_an_old_page_with_a_real_defect_still_asks_for_a_rerun(tmp_path):
    d = tmp_path / "qa"
    _page_with_residual_ref(d)                 # 残留编号 = 本地扫得出来的真缺陷
    a = Q.audit_qa_pages(d, {"papers": []})[0]
    assert not a.ok and a.residual_refs


def test_audit_carries_the_question_so_the_rerun_command_is_pasteable(tmp_path):
    """B6 第二件：`--verify` 给的重跑命令要人手填 `<那一页的问题>`，而问题就在
    frontmatter 里躺着。100 页时这一步会直接劝退。"""
    d = tmp_path / "qa"
    q = "MNAR 诊断在纵向 EHR 上到底能不能做？"
    _archive(d, Q.qa_slug(q), q)
    a = Q.audit_qa_pages(d, {"papers": []})[0]
    assert a.question == q
    assert q in Q.update_command(a.slug, a.question)


# ---------------------------------------------------------------------------
# 23. B3/B4：prompt 里两条互相打架的铁律
# ---------------------------------------------------------------------------

def test_prompt_tells_the_model_how_to_keep_an_old_claim():
    """B3：铁律 11「不要因为某条旧论断这次没检索到证据就删」与铁律 3「每条必须至少
    有一个证据编号」互相打架。模型照 11 保留的旧论断没有编号可挂 → `validate_qa`
    整条丢弃 → `dropped_claims` 上涨 → CLI 打 `⚑ 丢弃 N 条无出处论断`。
    而这个警告本来是"模型没按编号引用"的信号，会被正常的修订流程污染。"""
    text = Path("config/prompts/qa_synthesis_prompt.md").read_text(encoding="utf-8")
    i = text.index("有上一版答案时")
    tail = text[i:i + 400]
    assert "本次证据" in tail and ("挂不上" in tail or "删掉" in tail)


def test_prompt_forbids_answer_from_contradicting_its_own_gaps():
    """B4：线上那页 `answer` 说「……这是**统计上的不可判定性问题**」，而 gap#2 说
    「本次证据**没有专门系统讨论**「MNAR/NMAR 在观测数据上不可识别」这一根本问题」。
    **同一页，第一句和边界那一节互相打脸。**而 `answer` 按设计不带编号、
    `validate_qa` 也不检查一致性——这个豁免正好被用来说出全页最可引用、
    也最没出处的那句话。"""
    text = Path("config/prompts/qa_synthesis_prompt.md").read_text(encoding="utf-8")
    assert "answer" in text and "gaps" in text
    i = text.index("不许断言")
    assert "gaps" in text[i - 200:i + 200]


# ---------------------------------------------------------------------------
# 25. A3-3：把阈值这个**数**本身锁住
# ---------------------------------------------------------------------------
# 审计实测：把两个阈值改成 0.95，测试全绿——因为唯一那两条用例的 FakeEmbed 造的
# 余弦是 0.96/0.97，对 0.80 与 0.95 一视同仁。**形状锁住了，数值没锁。**
# 这与 P3 那次 `min_sim` 教训同形。下面四条把余弦造在阈值两侧 ±0.02 内。

def _cos_pair(cos):
    """构造两个夹角余弦恰为 `cos` 的单位向量（2 维即可）。"""
    import math
    return [1.0, 0.0, 0.0, 0.0], [cos, math.sqrt(max(0.0, 1 - cos * cos)), 0.0, 0.0]


@pytest.mark.parametrize("cos,should_hit", [
    # ⚠️ 这里的数字**必须写死**，不许写成 `DEFAULT_… ± 0.02`——那样余弦会跟着常量一起动，
    # 常量改成 0.95 测试照样绿，等于没锁（我第一版就是这么写的，变异测试当场逮到）。
    (0.62, True),      # 0.60 之上
    (0.58, False),     # 0.60 之下
])
def test_topic_match_threshold_is_pinned_to_its_number(cos, should_hit):
    """概念页比对的阈值。首版 0.80 实测让真命中 4/17——正例中位数只有 0.741，
    而反例地板在 0.46~0.55（bge-m3 对同语种中文提问的余弦下限本来就不是 0）。
    改成 0.60 之后这条用例保证它不会被人无声地改回去。"""
    qv, tv = _cos_pair(cos)
    specs = [T.TopicSpec(slug="s", title="t", question="概念页问题", queries=["q"])]
    emb = FakeEmbed({"提问": qv, "概念页问题": tv})
    hits = Q.find_similar_topics(specs, "提问", emb)
    assert bool(hits) is should_hit


@pytest.mark.parametrize("cos,should_hit", [
    (0.67, True),      # 0.65 之上（同上：写死，不许跟着常量动）
    (0.63, False),     # 0.65 之下
])
def test_dedup_threshold_is_pinned_to_its_number(tmp_path, cos, should_hit):
    """问答查重的阈值。首版 0.80 漏掉的正是 `question_similarity` docstring 拿来
    论证"必须换 embedding"的那个旗舰案例（换成 embedding 之后它拿 0.661，仍然漏）。"""
    d = tmp_path / "qa"
    _archive(d, "qa-old", "旧问题")
    qv, tv = _cos_pair(cos)
    emb = FakeEmbed({"新问题": qv, "旧问题": tv})
    res = Q.find_similar_questions(d, "新问题", embed_client=emb)
    assert bool(res.hits) is should_hit
    assert res.mode == "embedding" and not res.degraded


# ---------------------------------------------------------------------------
# 26. CLI 接线：这条链路没有 CLI 级测试（要 Ollama + 真向量库），
#     退而求其次锁住接线本身——变异实测这两处改坏时全套测试都是绿的。
# ---------------------------------------------------------------------------

def _main_src():
    import inspect
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import ask_notes                                          # noqa: E402
    return inspect.getsource(ask_notes.main)


def test_cli_passes_qa_dir_so_legacy_pages_stay_reachable():
    """`qa_slug` 的身份扫描只在给了 `qa_dir` 时才走。CLI 漏传 = 存量页全部变孤儿，
    而这正是上一轮亲手造出来的那个缺陷。"""
    assert "qa_slug(question, args.slug, qa_dir=qa_dir)" in _main_src()


def test_cli_topic_check_is_not_gated_by_the_dedup_switch():
    """B1：首版把概念页比对放在 `--no-dedup-check` 的 if 里，于是带上那个 flag 重跑
    一页时，生成块顶部的 `📎 相关概念页` 会被静默删掉，状态仍是 merged ✅。
    两件不同的事不许共用一个开关。"""
    src = _main_src()
    i = src.index("if not args.no_topic_check:")
    # 概念页那段必须在 dedup 的 if 之外：它自己那行的缩进是 8 空格（函数体一级），
    # 落在 dedup 块里就会是 12 空格。
    line_start = src.rindex("\n", 0, i) + 1
    assert src[line_start:i] == " " * 8, "概念页比对不许缩在 --no-dedup-check 块内"
    assert "topic_vecs = Q.topic_vectors(topic_specs, embed_client)" in src


# ---------------------------------------------------------------------------
# 27. 第 3 轮：把「算出来了但从不打印」这一类堵死
# ---------------------------------------------------------------------------

def test_verify_actually_prints_the_slug_mismatch():
    """F2：`slug_mismatch` 影响 `ok`、影响退出码，却**从不出现在输出里**
    （`grep -rn slug_mismatch scripts/` 曾经返回零行）。用户看到的是一行全 0 配一个 ❌，
    而给的药方"重问一次"会落到另一页上、孤儿页原地不动——**诊断和药方都是错的**。

    这一类"算了不印"没有任何既有测试能拦：`QAAudit` 的字段有测试，
    CLI 的输出没有。这条按源码结构锁住那个打印分支。"""
    import inspect
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import ask_notes                                          # noqa: E402
    src = inspect.getsource(ask_notes.main)
    assert "if a.slug_mismatch:" in src, "算出来了就必须印出来"
    i = src.index("if a.slug_mismatch:")
    tail = src[i:i + 500]
    assert "不可达" in tail, "要说清后果：重问会落到另一页"
    # ⚠️ **不许印 `mv`**：`slug_mismatch` 置位时目标文件必然已存在（两条返回路径给的
    # 都是磁盘上真实存在的文件），照做就是覆盖掉那页唯一可达的。这条断言是反向的——
    # 我第一版写的是 `assert "mv " in tail`，把一个破坏性命令锁进了测试里
    # （"测试跟着实现动"的活标本，第 3 轮审计逮到）。
    printed = "\n".join(ln for ln in tail.splitlines()
                        if not ln.lstrip().startswith("#"))
    assert "mv " not in printed, "目标必然已存在，mv 就是覆盖数据"
    assert "归并" in tail


def test_nearby_papers_carry_titles_too():
    """F4：这一行替代的是 v1 里带整句原文的 ○ 行——补偿不能比被牺牲的东西还便宜。
    三个不认识的裸 citekey 排在那里没有人会去查。"""
    block = Q.render_qa_block("问题", _QA, EVS, nearby_papers=["little1988Test"],
                              titles={"little1988Test": "A Test of Missing Completely at Random"})
    assert "little1988Test" in block
    assert "A Test of Missing Completely at Random" in block


def test_gap_hit_lines_survive_missing_titles():
    """索引里查不到标题（改过键、或刚入库还没进索引）时不许崩，也不许印出 `None`。"""
    lines = Q._gap_hit_lines([Q.GapHit(kind="evidence", key="k2024", score=0.7),
                              Q.GapHit(kind="topic", key="t", score=0.7)])
    joined = "\n".join(lines)
    assert "None" not in joined and "k2024" in joined and "t" in joined


# ---------------------------------------------------------------------------
# 28. 第 3 轮代码审计：剥离器的领域词、编码容错、填充侧的 title/snippet
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "报告偏倚的量化",          # `报告` 既是脚手架动词也是领域词词头
    "覆盖率的定义",            # `覆盖`
    "研究设计的三种类型",      # `研究`
    "包含缺失指示符的模型",    # `包含`
    "直接效应与间接效应的分解",  # `直接`（裸副词档）
    "充分统计量在缺失数据下的构造",
    "具体病种的亚组分析",
    "任何时点的插补策略",
])
def test_verb_and_adverb_tiers_do_not_eat_domain_terms(text):
    """第 2 轮只给「未」「系统」两个**有已知反例**的词加了动词前瞻，没把它上升成规则。
    剩下 27 个词是同一个形状——审计用真 bge-m3 + 真库量化：6/6 领域词被啃掉，
    其中 3/6 让回查 top1 换了一篇论文或跌破 0.65 阈值，**把结论整个翻转**。

    规则现在是两条：裸副词要后接副词或动词；**动词档只在前面已经剥掉过脚手架时才开火，
    且至多剥一次**（「没有给出覆盖率」里 `给出` 是脚手架、`覆盖` 不是）。"""
    assert Q.strip_gap_scaffold(text) == text


@pytest.mark.parametrize("gap,want", [
    ("本次证据没有给出覆盖率的定义", "覆盖率的定义"),          # 剥一个动词就停
    ("本次证据没有专门系统讨论可识别性条件", "可识别性条件"),   # 副词链 + 动词
    ("未涉及跨中心机制漂移的量化", "跨中心机制漂移的量化"),
])
def test_scaffold_still_stripped_after_the_guards(gap, want):
    """守卫不能把该剥的也挡住——这两条是同一枚硬币的两面。"""
    assert Q.strip_gap_scaffold(gap) == want


def test_a_non_utf8_file_does_not_take_down_the_whole_archive(tmp_path):
    """`UnicodeDecodeError` 是 `ValueError` 的子类，只捕 `OSError` 挡不住它。
    目录里内容是中文，有人用 GBK 存回来一个文件，就能让 `--list`/`--verify`/查重/
    身份扫描/INDEX 重建一起挂——而这几处的 docstring 都写着「绝不抛异常」。"""
    d = tmp_path / "qa"
    _archive(d, "qa-good", "正常的问题")
    (d / "qa-broken.md").write_bytes(b"---\nqa: \xff\xfe\x00broken\n---\n")
    assert [p.slug for p in Q.list_qa_pages(d)] == ["qa-good"]
    assert Q.audit_qa_pages(d, {"papers": []})[0].slug == "qa-good"
    assert Q.find_similar_questions(d, "图神经网络的过平滑怎么缓解").hits == []
    assert "正常的问题" in Q.render_qa_index(d)


def test_recheck_gaps_fills_in_titles_and_snippets(tmp_path):
    """第 3 轮加的 title/snippet **只在渲染层有测试**（手工构造 GapHit 再喂给
    render_qa_block），而填充它们的那三行在 `recheck_gaps` 里没有任何断言——
    把填充整个去掉，页面上标题原句消失，测试全绿（审计变异 N1/N2/N4 全部逃逸）。"""
    gap = "缺少可识别性的系统讨论"
    store = FakeStore(
        [{"id": "c1", "level": "highlight", "citekey": "chen2026Partial",
          "text": "可识别性判据的原句"}],
        [[1, 0, 0, 0]])
    specs = [T.TopicSpec(slug="missingness-causal", title="缺失与因果结构",
                         question="可识别性在什么条件下成立？", queries=["q"])]
    emb = FakeEmbed({Q.strip_gap_scaffold(gap): [1, 0, 0, 0],
                     "可识别性在什么条件下成立？": [0.95, 0.31, 0, 0]})
    hits = Q.recheck_gaps([gap], store=store, embed_client=emb, topic_specs=specs,
                          titles={"chen2026Partial": "Partial Identification"})
    ev = [h for h in hits[gap] if h.kind == "evidence"][0]
    tp = [h for h in hits[gap] if h.kind == "topic"][0]
    assert ev.title == "Partial Identification", "证据命中必须带标题"
    assert ev.snippet == "可识别性判据的原句", "必须带匹配到的那句原文"
    assert tp.title == "缺失与因果结构", "概念页命中也要带标题"
