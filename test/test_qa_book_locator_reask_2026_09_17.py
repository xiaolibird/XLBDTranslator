# -*- coding: utf-8 -*-
"""2026-09-17 三项修复的回归：书籍证据页码定位符 / `--reask` 重问可复现 / 撤稿检查
覆盖问答页（不连 Ollama、不调 LLM、不联网）。

三件事分开锁，因为坏法不一样：

1. **书籍证据丢页码**——`topics.Evidence`/`qa` 渲染此前只带得动 citekey，书籍句级
   证据（章节 + 原书页码）挂在 `embed_store` 层就有，却从没传到 `[@citekey]` 引用
   里。加了定位符之后，全仓凡是用正则解析 `[@key]` 的地方都要重新核过一遍带
   `, p. N` 后缀的引用还认不认得出来——这正是本文件里覆盖面最大的一段。
2. **重问不可复现**——`ask_notes.py` 的检索参数（`-q`/`--role`/`--min-sim`/
   `--max-evidence`/`--per-paper-cap`）此前不落盘，半年后想重问同一页得自己回忆
   当年怎么问的。现在随页存档，`--reask`/`--reask-all` 读回来原样重问，命令行
   显式给的覆盖存档值。
3. **撤稿检查漏问答页**——`lint_checks.cited_by_page` 只扫概念页（有意为之，见
   该函数 docstring），撤稿论文若只被问答页引用会被漏报。加一张平行的问答页
   引用表，撤稿节据此单独列出。
"""
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar import lint as L                                         # noqa: E402
from src.scholar import notes_index as ni                                 # noqa: E402
from src.scholar import page_parse as PP                                  # noqa: E402
from src.scholar import qa as Q                                           # noqa: E402
from src.scholar import topics as T                                       # noqa: E402
from src.scholar.grounding import check_numbers, build_pool               # noqa: E402
from src.scholar.vault import split_frontmatter                           # noqa: E402
import scripts.ask_notes as AN                                            # noqa: E402
import scripts.backfill_scholar_metadata as BSM                           # noqa: E402
import scripts.gen_bench as GB                                            # noqa: E402


# ---------------------------------------------------------------------------
# 共用夹具
# ---------------------------------------------------------------------------

def _paper_ev(ref, citekey, text="论文证据句", score=0.9, role="citable"):
    """普通论文证据：pages/chapter 恒为 None，与改动前逐字节一致。"""
    return T.Evidence(ref=ref, citekey=citekey, text=text, role=role,
                      section="实验方法", note_file="科研札记_2026-08.md",
                      note_line=10, score=score, year=2024, title="论文 {}".format(citekey))


def _book_ev(ref, citekey, pages, text="书籍证据句", score=0.9, role="citable",
            chapter="Ch.7 · pp.241-259 · Nonignorable models"):
    """书籍句级证据：带页码定位符（与真实索引里 book_notes 写入的形态一致）。"""
    return T.Evidence(ref=ref, citekey=citekey, text=text, role=role,
                      section=chapter + " · 方法与数据", note_file="科研札记_书籍精读.md",
                      # 书籍全书共用同一个入口行 note_line——见「note_line 调查」相关测试，
                      # 这里故意取一个与其它证据相同的值，模拟真实索引的观测现象。
                      note_line=20, score=score, year=2020,
                      title="Statistical Analysis with Missing Data",
                      pages=pages, chapter=chapter)


def _spec():
    return T.TopicSpec(slug="demo", title="缺失数据能不能处理", question="缺失数据能不能处理？",
                       queries=["缺失数据"])


def _synthesis_data(claim_text, refs):
    return {"summary": "总述", "sections": [{"heading": "小节",
            "claims": [{"text": claim_text, "evidence": list(refs)}]}], "disputes": [], "gaps": []}


def _qa_data(point_text, refs, *, answer="简短回答"):
    return {"answer": answer, "points": [{"text": point_text, "evidence": list(refs)}],
           "caveats": [], "gaps": []}


# ===========================================================================
# 一、书籍证据页码定位符：渲染
# ===========================================================================

class TestLocatorRendering:
    def test_single_page_locator_appears_in_topic_claim(self):
        ev = [_book_ev("E1", "little2020rubin", "247")]
        synthesis, report = T.validate_synthesis(
            _synthesis_data("因子化似然把单调缺失拆成完全数据问题", ["E1"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "[@little2020rubin, p. 247]" in block

    def test_paper_evidence_has_no_locator_suffix(self):
        """论文证据完全向后兼容：不带定位符，逐字节等于改动前的渲染。"""
        ev = [_paper_ev("E1", "a2024X")]
        synthesis, report = T.validate_synthesis(
            _synthesis_data("论断一", ["E1"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "[@a2024X]" in block
        assert "[@a2024X," not in block

    def test_same_book_different_pages_render_as_two_brackets(self):
        """同一论断引同一本书的两页：逐页各开一个方括号，两个页码都要出现——
        这是本轮明确选定的多页定位符格式。"""
        ev = [_book_ev("E1", "little2020rubin", "12", text="第一句"),
             _book_ev("E2", "little2020rubin", "40", text="第二句")]
        synthesis, report = T.validate_synthesis(
            _synthesis_data("两处都支持这个论断", ["E1", "E2"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "[@little2020rubin, p. 12]" in block
        assert "[@little2020rubin, p. 40]" in block
        # 是 pandoc 能渲染的合法形式：两个独立的方括号引用，不是把两页硬塞进一个方括号
        assert "pp. 12, 40" not in block

    def test_mixed_paper_and_book_citation_in_one_claim(self):
        ev = [_paper_ev("E1", "smith2024paper"), _book_ev("E2", "little2020rubin", "5")]
        synthesis, report = T.validate_synthesis(
            _synthesis_data("论文与书籍共同支持", ["E1", "E2"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "[@smith2024paper]" in block
        assert "[@little2020rubin, p. 5]" in block

    def test_page_range_locator_rendered_verbatim(self):
        """区间页码（"241-259"）原样透传，不拆成起止两个数字，也不改写成 pp. 形式——
        与 scripts/notes_query.py::_cite_one 已经在用的既有约定一致。"""
        ev = [_book_ev("E1", "little2020rubin", "241-259")]
        synthesis, report = T.validate_synthesis(
            _synthesis_data("整章讨论敏感性分析", ["E1"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "[@little2020rubin, p. 241-259]" in block

    def test_repeated_ref_to_same_page_dedups_to_one_bracket(self):
        """同一 citekey 同一页码被两个不同 E 编号指向（同一句被拆成两条证据的极端情形）
        仍只应渲染一次——去重键是 (citekey, pages)，不是 citekey 单独。"""
        ev = [_book_ev("E1", "k", "12"), _book_ev("E2", "k", "12")]
        text = T._cite_str(["E1", "E2"], {e.ref: e for e in ev})
        assert text == "[@k, p. 12]"

    def test_dispute_sides_render_locators(self):
        ev = [_book_ev("E1", "a2020Book", "10", text="甲方观点句"),
             _book_ev("E2", "b2020Book", "20", text="乙方观点句")]
        data = {"summary": "", "sections": [], "gaps": [], "disputes": [
            {"issue": "分歧点", "position_a": "甲方立场", "evidence_a": ["E1"],
             "position_b": "乙方立场", "evidence_b": ["E2"], "note": ""}]}
        synthesis, report = T.validate_synthesis(data, ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "[@a2020Book, p. 10]" in block
        assert "[@b2020Book, p. 20]" in block

    def test_qa_points_and_caveats_render_locators(self):
        ev = [_book_ev("E1", "little2020rubin", "88")]
        qa, report = Q.validate_qa(_qa_data("依据这一条", ["E1"]), ev)
        block = Q.render_qa_block("问题", qa, ev)
        assert "[@little2020rubin, p. 88]" in block

    def test_qa_backtranslate_inline_ref_adds_locator(self):
        """模型在自由文本里顺手写的证据编号（"E1 的流程图..."）回译时，若那条证据
        是书籍句级证据，同样要带上页码定位符——不能只有 evidence 字段那条通道有。"""
        ev = [_book_ev("E1", "little2020rubin", "99")]
        ev_map = {e.ref: e for e in ev}
        report = T.ValidationReport()
        out = Q.backtranslate_inline_refs("E1 的表格明确写着这一点", ev_map, report)
        assert "[@little2020rubin, p. 99]" in out

    def test_topic_evidence_table_row_shows_page_number(self):
        """证据表行里必须显示句级页码（不是只有正文引用才带）；backtick 包着的
        `[@key]` 本身不带定位符——那是展示用的代码格式，不是真 pandoc 引用。"""
        ev = [_book_ev("E1", "little2020rubin", "153")]
        synthesis, report = T.validate_synthesis(
            _synthesis_data("论断", ["E1"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "`[@little2020rubin]`" in block          # 反引号里不带定位符
        assert "p.153" in block                          # 但这一行别处显示了页码

    def test_qa_evidence_table_row_shows_page_number(self):
        ev = [_book_ev("E1", "little2020rubin", "376")]
        qa, report = Q.validate_qa(_qa_data("依据", ["E1"]), ev)
        block = Q.render_qa_block("问题", qa, ev)
        assert "`[@little2020rubin]`" in block
        assert "p.376" in block

    def test_topic_evidence_table_falls_back_to_chapter_when_pages_missing(self):
        """N1（2026-09-17 R1 对抗审核）：`pages`/`chapter` 在 `embed_store.Chunk`
        里来源独立（`pages` 只认显式字段，`chapter` 还能从 `section` 派生），
        真实库现状总是两者一起出现，但不代表以后不会出现"只标章节没标页码"的
        精读句。此时证据表行退而显示 chapter；**正文引用不受影响**——pandoc
        的 locator 语法只认页码，"Ch.3" 不是合法 citeproc locator。"""
        ev = [T.Evidence(ref="E1", citekey="k2020Book", text="证据句", role="citable",
                         section=None, note_file="书.md", note_line=20, score=0.9,
                         pages=None, chapter="Ch.3 · pp.55-70 · Some Title")]
        synthesis, report = T.validate_synthesis(_synthesis_data("论断", ["E1"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        assert "Ch.3 · pp.55-70 · Some Title" in block
        assert "[@k2020Book]" in block
        assert "[@k2020Book, " not in block     # 正文引用没有定位符，更不会塞进 chapter

    def test_qa_evidence_table_falls_back_to_chapter_when_pages_missing(self):
        ev = [T.Evidence(ref="E1", citekey="k2020Book", text="证据句", role="citable",
                         section=None, note_file="书.md", note_line=20, score=0.9,
                         pages=None, chapter="Ch.3 · pp.55-70 · Some Title")]
        qa, report = Q.validate_qa(_qa_data("依据", ["E1"]), ev)
        block = Q.render_qa_block("问题", qa, ev)
        assert "Ch.3 · pp.55-70 · Some Title" in block
        assert "[@k2020Book]" in block
        assert "[@k2020Book, " not in block

    def test_evidence_table_loc_ends_with_pages_not_double_appended_with_chapter(self):
        """pages 存在时 `elif e.chapter:` 不该再触发——用 `row.endswith` 直接锁
        `loc`（`{meta}{loc}` 里 `loc` 恰是整行的**末尾**）：若有人把 `elif` 改回
        独立的 `if`，pages 后面会多出一段 `· <chapter>`，这里就会红。

        不用"chapter 原文没出现在行里"这种断言——`_book_ev` 的 `section` 本来就
        由 `chapter` 拼出来（`chapter + " · 方法与数据"`），经 `meta` 那条路已经
        合法地出现在行里一次，与这里要锁的"loc 会不会被二次追加"是两件事。
        """
        ev = [_book_ev("E1", "little2020rubin", "153")]
        synthesis, report = T.validate_synthesis(_synthesis_data("论断", ["E1"]), ev)
        block = T.render_generated_block(_spec(), synthesis, ev)
        row = next(ln for ln in block.splitlines() if "**E1**" in ln)
        assert row.endswith("· p.153")

    def test_qa_evidence_table_loc_ends_with_pages_not_double_appended_with_chapter(self):
        """MAJOR-1（2026-09-17 R2 对抗审核）：上一条镜像测试只锁了 `topics.py` 那份
        实现，`qa.py` 独立维护一份 `render_qa_block`（模块 docstring 明写了这条
        例外——两侧渲染逐字同形但各自实现，改一边记得改另一边）。审核员实测把
        `qa.py` 的 `elif getattr(e, "chapter", None):` 改回独立的 `if`，全仓 2492
        条测试没有一条红——因为这条镜像用例此前根本不存在。断言方式同
        `topics.py` 那条：`row.endswith("· p.NNN")` 直接锁 `loc` 是整行末尾，
        pages 存在时 chapter 不该再被追加。"""
        ev = [_book_ev("E1", "little2020rubin", "153")]
        qa, report = Q.validate_qa(_qa_data("依据", ["E1"]), ev)
        block = Q.render_qa_block("问题", qa, ev)
        row = next(ln for ln in block.splitlines() if "**E1**" in ln)
        assert row.endswith("· p.153")

    def test_qa_cite_str_paper_evidence_has_no_locator(self):
        """M3（2026-09-17 R1 对抗审核）：`qa.py` 的 `_cite_str` 是独立维护的一份
        （不是从 `topics.py` 导入——模块 docstring 明写了这条例外），`topics.py`
        侧的同型负例（`test_paper_evidence_has_no_locator_suffix`）盖不到这一份。
        审核员实测把这里的 `if pages:` 变异成恒真，既有测试全绿——`_cite_str`
        是渲染定位符的唯一权威实现，exact-match 锁死输出，不给"多加了点什么"
        留任何空间。"""
        ev = [_paper_ev("E1", "a2024X")]
        assert Q._cite_str(["E1"], {e.ref: e for e in ev}) == "[@a2024X]"

    def test_qa_evidence_table_row_has_no_page_suffix_for_paper_evidence(self):
        """同上，证据表那一行（`loc` 拼接处，`if getattr(e, "pages", None):`）。
        `_paper_ev` 夹具的 role 标签/分节/note_file 都不含 "p." 子串，所以这行
        assertion 精确命中"这一行有没有被多塞进页码后缀"这件事，不会被无关内容
        污染——mutant 把守卫改成恒真时，这里会多出 " · p.None"。"""
        ev = [_paper_ev("E1", "a2024X")]
        qa, report = Q.validate_qa(_qa_data("论断", ["E1"]), ev)
        block = Q.render_qa_block("问题", qa, ev)
        row = next(ln for ln in block.splitlines() if "**E1**" in ln)
        assert row == ("- ● **E1** `[@a2024X]` 🟪可引用证据 · 实验方法 · "
                       "科研札记_2026-08.md:10")
        assert "p." not in row

    def test_evidence_carries_pages_end_to_end_through_retrieve_evidence(self):
        """从向量库检索到 Evidence 对象的完整链路：store 记录里的 pages/chapter 必须
        透传到 Evidence，而不是在 retrieve_evidence/select_evidence 两道重建里弄丢
        （select_evidence 重编号时若漏列字段，值会静默回落到 None）。"""
        from src.scholar.embed_store import VectorStore

        class _FakeEmbed:
            def embed(self, texts):
                return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]

        record = {
            "id": "h:little2020rubin:citable:x:0", "level": "highlight",
            "citekey": "little2020rubin", "role": "citable", "section": "方法与数据",
            "month": None, "series": "book", "tier": "high", "bucket": [],
            "year": 2020, "has_full_text": True, "note_file": "书.md", "note_line": 20,
            "text": "因子化似然的证据句", "pages": "247",
            "chapter": "Ch.7 · pp.241-259 · Nonignorable models",
        }
        store = VectorStore(meta={}, records=[record],
                            mat=np.array([[1.0, 0.0]], dtype=np.float32))
        evs = T.retrieve_evidence(_spec(), store, _FakeEmbed(), min_sim=0.1,
                                  per_paper_cap=4)
        assert len(evs) == 1
        assert evs[0].pages == "247"
        assert evs[0].chapter == "Ch.7 · pp.241-259 · Nonignorable models"
        # select_evidence 单独回归：重编号（定稿证据的唯一构造点）不能弄丢新字段
        picked = T.select_evidence(list(evs), max_evidence=1, per_paper_cap=1)
        assert picked[0].pages == "247"

    def test_flatten_optional_locator_preserves_none_and_empty(self):
        assert T._flatten_optional_locator(None) is None
        assert T._flatten_optional_locator("") is None

    def test_flatten_optional_locator_strips_embedded_newlines(self):
        assert T._flatten_optional_locator("Ch.3 ·\npp.55-70") == "Ch.3 · pp.55-70"

    def test_chapter_with_embedded_newline_does_not_break_evidence_row_regex(self):
        """MINOR-6（2026-09-17 R2 对抗审核）：`pages`/`chapter` 此前没有像 `text`
        （368 行）那样过 `flatten_linebreaks`。`chapter` 含内嵌换行时（书籍精读
        的分节描述跨行很常见），证据表那一行会被硬生生断成两个物理行，
        `_EVIDENCE_ROW_RE`（单行正则）匹配 0 条——这条证据从 `--verify`/
        `audit_topic_pages` 的统计里**静默消失**：citekey 仍在页面上、pandoc
        仍能正常渲染，看起来一切正常，只是再也没有任何自检能发现它。"""
        from src.scholar.embed_store import VectorStore

        class _FakeEmbed:
            def embed(self, texts):
                return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]

        record = {
            "id": "h:k2020Book:citable:x:0", "level": "highlight",
            "citekey": "k2020Book", "role": "citable", "section": "方法与数据",
            "month": None, "series": "book", "tier": "high", "bucket": [],
            "year": 2020, "has_full_text": True, "note_file": "书.md", "note_line": 20,
            "text": "证据句", "pages": "12",
            "chapter": "Ch.3 · pp.55-70 ·\nSome Title",     # 内嵌换行
        }
        store = VectorStore(meta={}, records=[record],
                            mat=np.array([[1.0, 0.0]], dtype=np.float32))
        evs = T.retrieve_evidence(_spec(), store, _FakeEmbed(), min_sim=0.1, per_paper_cap=4)
        assert len(evs) == 1
        assert "\n" not in (evs[0].chapter or "")

        synthesis, report = T.validate_synthesis(_synthesis_data("论断", ["E1"]), evs)
        block = T.render_generated_block(_spec(), synthesis, evs)
        rows = T._EVIDENCE_ROW_RE.findall(block)
        assert len(rows) == 1, "chapter 里的内嵌换行把证据表行断成了两半"


# ===========================================================================
# 二、全链路对带定位符引用的识别
# ===========================================================================

class TestLocatorAwareParsing:
    def test_topics_cite_re_extracts_bare_citekey(self):
        assert T._CITE_RE.findall("看 [@little2020rubin, p. 247] 这句") == ["little2020rubin"]
        assert T._CITE_RE.findall("看 [@a2024X] 这句") == ["a2024X"]

    def test_grounding_check_numbers_does_not_leak_locator_page_number(self):
        """回归核心：论断正文若原样带着 `[@key, p. 247]`（页面解析出来的旧形态），
        `check_numbers` 必须把整个定位符连同页码一起剥掉——不然 247 会被当成论断
        自己提到的数字，去匹配池里找不到就报一个与转述失真无关的假 ungrounded。"""
        claim = "因子化似然把单调缺失拆成完全数据问题 [@little2020rubin, p. 247]"
        pool = build_pool("因子化似然把单调缺失拆成完全数据问题。", "标题")
        chk = check_numbers(claim, pool)
        assert chk.total == 0 and chk.ungrounded == []

    def test_grounding_still_catches_a_real_ungrounded_number_next_to_a_locator(self):
        """上一条测试证明定位符本身不产生假阳性；这一条证明剥离没有"顺手"把真正
        该抓的数字也剥掉——两者必须同时成立才说明这条正则改得对。"""
        claim = "样本量 999 例 [@little2020rubin, p. 247]"
        pool = build_pool("样本量 100 例。", "标题")
        chk = check_numbers(claim, pool)
        assert chk.total == 1 and "999" in chk.ungrounded

    def test_page_parse_claim_citekeys_ignore_locator(self, tmp_path):
        page = tmp_path / "demo.md"
        page.write_text(
            "---\ntopic: demo\n---\n\n"
            "{}\n## 小节\n\n"
            "- 因子化似然把单调缺失拆成完全数据问题 [@little2020rubin, p. 247]\n\n"
            "{}\n\n## 我的批注\n".format(
                T.GEN_BEGIN.format(1, "12345678", "test"), T.GEN_END),
            encoding="utf-8")
        parsed = PP.parse_page(page)
        assert len(parsed.claims) == 1
        assert parsed.claims[0].citekeys == ["little2020rubin"]

    def test_page_parse_claim_citekeys_dedup_same_book_two_page_locators(self, tmp_path):
        """N3（2026-09-17 R1 对抗审核）：一条论断引同一本书的两页（多页定位符场景
        下的常见形状）此前 `citekeys` 从不去重，会拿到 `[k, k]`——`entail_audit.py`
        展示证据时截 `citekeys[:3]`，重复项占掉本该展示别的文献的位置。去重保序：
        重复的 citekey 只留第一次出现的位置，不同 citekey 的相对顺序不变。"""
        page = tmp_path / "demo.md"
        page.write_text(
            "---\ntopic: demo\n---\n\n"
            "{}\n## 小节\n\n"
            "- 两页都支持这个论断 [@little2020rubin, p. 12] [@other2021Key] "
            "[@little2020rubin, p. 40]\n\n"
            "{}\n\n## 我的批注\n".format(
                T.GEN_BEGIN.format(1, "12345678", "test"), T.GEN_END),
            encoding="utf-8")
        parsed = PP.parse_page(page)
        assert len(parsed.claims) == 1
        assert parsed.claims[0].citekeys == ["little2020rubin", "other2021Key"]

    def test_gen_bench_cite_re_extracts_and_strips_locator(self):
        assert GB._CITE_RE.findall("[@little2020rubin, p. 247]") == ["little2020rubin"]
        stripped = GB._CITE_RE.sub("", "结论 [@little2020rubin, p. 247] 结尾")
        assert "247" not in stripped and "little2020rubin" not in stripped

    def test_topic_dead_key_audit_ignores_locator_but_still_finds_real_dead_keys(self, tmp_path):
        """audit_topic_pages 的死键检测：带定位符的活键不能被误报为死键；
        不带定位符的死键必须仍然被抓到——两件事都要验证，免得"修好一个坏了一个"。"""
        topics_dir = tmp_path / "topics"
        topics_dir.mkdir()
        body = ("- 活键带页码 [@alive2020Book, p. 12]\n"
               "- 死键 [@dead2019Paper]\n")
        page = topics_dir / "demo.md"
        page.write_text("---\ntopic: demo\n---\n\n{}\n{}\n{}\n\n## 我的批注\n".format(
            T.GEN_BEGIN.format(1, "abcdef12", "test"), body, T.GEN_END), encoding="utf-8")
        index = {"papers": [{"citekey": "alive2020Book"}], "generated_at": "2026-01-01T00:00:00"}
        audits = T.audit_topic_pages(topics_dir, index)
        assert len(audits) == 1
        assert audits[0].dead_keys == ["dead2019Paper"]

    def test_lint_cited_citekeys_ignores_locator(self, tmp_path):
        topics_dir = tmp_path / "topics"
        (topics_dir / "qa").mkdir(parents=True)
        (topics_dir / "p.md").write_text(
            "---\ntopic: demo\n---\n\n- 论断 [@little2020rubin, p. 247]\n",
            encoding="utf-8")
        assert L.cited_citekeys(topics_dir) == {"little2020rubin"}

    def test_find_dangling_citations_recognizes_locator_bearing_citekeys(self, tmp_path):
        """`notes_index.find_dangling_citations` 是改键收尾判定"是否 finalized"的
        安全检查（`backfill_scholar_metadata.run_rekeys`）；原字符类遇到定位符的逗号
        就整段判不出 citekey，会让书籍证据的悬空引用永远查不出来——不是"报告少一行"，
        是"明明还有悬空引用却被判定为改键已完成"。"""
        (tmp_path / "topics" / "qa").mkdir(parents=True)
        (tmp_path / "topics" / "p.md").write_text(
            "见 [@aLive, p. 12] 与 [@bGone, p. 5-9]\n", encoding="utf-8")
        idx = {"papers": [{"citekey": "aLive"}]}
        got = ni.find_dangling_citations(tmp_path, idx)
        assert got == [{"page": "p.md", "key": "bGone"}]

    def test_keys_in_tampered_topic_pages_recognizes_locator_bearing_citekeys(self, tmp_path):
        """`backfill_scholar_metadata.keys_in_tampered_topic_pages`：手改页里的键要从
        改键计划里排除，不然改键后这页会留下一个悬空引用（改名工具本来就跳过手改页）。
        同样的定位符逗号问题：原字符类 `[^\\]\\s;,]` 遇到逗号整段判不出 citekey。"""
        topics_dir = tmp_path / "topics"
        topics_dir.mkdir()
        page = topics_dir / "tampered.md"
        body = "- 结论 [@oldKey, p. 247]"
        page.write_text("{}\n{}\n{}\n\n## 我的批注\n".format(
            T.GEN_BEGIN.format(1, "12345678", "test"), body, T.GEN_END), encoding="utf-8")
        # 内容与哈希对不上 = 手改过
        got = BSM.keys_in_tampered_topic_pages(tmp_path)
        assert got == {"oldKey": "tampered.md"}


# ===========================================================================
# 三、改键保留定位符（notes_index.py）
# ===========================================================================

class TestRenamePreservesLocator:
    def _mk(self, tmp_path):
        t = tmp_path / "topics"
        (t / "qa").mkdir(parents=True)
        return t

    def test_rename_preserves_single_page_locator(self, tmp_path):
        t = self._mk(tmp_path)
        page = t / "demo.md"
        page.write_text("- 结论 [@oldKey, p. 247]\n", encoding="utf-8")
        got = ni.rename_citekeys_in_topic_pages(tmp_path, [("oldKey", "newKey")], apply=True)
        assert got["applied"] is True
        txt = page.read_text(encoding="utf-8")
        assert txt == "- 结论 [@newKey, p. 247]\n"

    def test_rename_preserves_locator_in_qa_page(self, tmp_path):
        t = self._mk(tmp_path)
        page = t / "qa" / "qa-x.md"
        page.write_text("答：见 [@oldKey, p. 241-259]\n", encoding="utf-8")
        ni.rename_citekeys_in_topic_pages(tmp_path, [("oldKey", "newKey")], apply=True)
        assert page.read_text(encoding="utf-8") == "答：见 [@newKey, p. 241-259]\n"

    def test_rename_handles_mixed_locator_and_plain_citations_on_one_page(self, tmp_path):
        t = self._mk(tmp_path)
        page = t / "demo.md"
        page.write_text("- 一 [@oldKey]\n- 二 [@oldKey, p. 12]\n- 三 [@oldKey, p. 40]\n",
                        encoding="utf-8")
        ni.rename_citekeys_in_topic_pages(tmp_path, [("oldKey", "newKey")], apply=True)
        txt = page.read_text(encoding="utf-8")
        assert txt == "- 一 [@newKey]\n- 二 [@newKey, p. 12]\n- 三 [@newKey, p. 40]\n"

    def test_page_cites_old_key_detects_locator_bearing_citation(self):
        assert ni._page_cites_old_key("- 结论 [@oldKey, p. 247]", "oldKey", False) is True
        assert ni._page_cites_old_key("- 结论 [@somethingElse, p. 247]", "oldKey", False) is False

    def test_rename_does_not_false_positive_on_prefixed_citekey(self, tmp_path):
        """`old="k"` 不能把 `[@k2024X, p. 5]` 也当成 k 的引用改掉——这条测试同时验证
        detect 与 replace 两条路径都守住了这个边界，不只是 detect 那一半。"""
        t = self._mk(tmp_path)
        page = t / "demo.md"
        page.write_text("- 结论 [@k2024X, p. 5]\n", encoding="utf-8")
        got = ni.rename_citekeys_in_topic_pages(tmp_path, [("k", "kRenamed")], apply=True)
        assert got["pages"] == []
        assert page.read_text(encoding="utf-8") == "- 结论 [@k2024X, p. 5]\n"


# ===========================================================================
# 四、wiki 链接转换保留定位符
# ===========================================================================

class TestToWikiLinksLocator:
    def test_locator_preserved_in_link_display_text(self):
        out = T.to_wiki_links("见 [@little2020rubin, p. 247]",
                              {"little2020rubin": "Little Rubin 2020"})
        assert out == "见 [[Little Rubin 2020|little2020rubin, p. 247]]"

    def test_unmapped_citekey_with_locator_left_unchanged(self):
        """不在 vault filenames 里的 citekey（论文在库里但没进 vault）保持原样，
        **含定位符的整段**都不能被吞掉或改写成半个死链。"""
        out = T.to_wiki_links("见 [@notInVault, p. 12]", {})
        assert out == "见 [@notInVault, p. 12]"

    def test_plain_citation_without_locator_still_renders_bare_form(self):
        """回归：fname == key 时渲染成不带管道符的 `[[key]]`——这条既有行为不能被
        定位符功能带崩。"""
        out = T.to_wiki_links("见 [@k]", {"k": "k"})
        assert out == "见 [[k]]"

    def test_locator_forces_piped_form_even_when_fname_equals_key(self):
        out = T.to_wiki_links("见 [@k, p. 9]", {"k": "k"})
        assert out == "见 [[k|k, p. 9]]"


# ===========================================================================
# 五、frontmatter 检索参数往返
# ===========================================================================

class TestFrontmatterRoundTrip:
    def test_render_frontmatter_empty_list_round_trips_to_empty_list(self):
        """`_render_frontmatter` 的既有 bug：空列表此前写成裸 `key:`，YAML 读回来是
        `None` 不是 `[]`。qa 页的 ask_query/ask_role 两个可能为空的列表字段第一次
        真正触发这条路径，必须先补上，不然「旧页无存档参数」判定会被污染。"""
        text = T._render_frontmatter({"ask_query": [], "ask_role": ["citable"]})
        fm, _body = split_frontmatter(text + "\n\n正文\n")
        assert fm["ask_query"] == []
        assert fm["ask_role"] == ["citable"]

    def test_ask_params_round_trip_with_chinese_quotes_colons_hash_and_newlines(self):
        """中文、引号、冒号、`#`、换行——这几个字符任一个转义错了都会写出语法错误的
        YAML（`_render_frontmatter` 头部注释点名过这个坑），必须整体往返无损。"""
        tricky_queries = [
            "缺失机制：可检验性",             # 冒号
            '带"引号"的检索词',                # 中文引号内的英文双引号
            "标签 #话题 后缀",                 # #
            "第一行\n第二行",                  # 换行
        ]
        fm_text = Q.build_qa_frontmatter(
            "问题", "qa-x", {"answer": "", "points": [], "caveats": [], "gaps": []},
            [], T.ValidationReport(), "2026-09-17T00:00:00",
            extra_queries=tricky_queries, roles=["citable", "refutable"],
            min_sim=0.62, max_evidence=17, per_paper_cap=2)
        fm, _body = split_frontmatter(fm_text + "\n\n正文\n")
        assert fm["ask_query"] == tricky_queries
        assert fm["ask_role"] == ["citable", "refutable"]
        assert fm["ask_min_sim"] == 0.62
        assert fm["ask_max_evidence"] == 17
        assert fm["ask_per_paper_cap"] == 2

        archived = Q.read_archived_ask_params(fm)
        assert archived.has_params is True
        assert archived.extra_queries == tricky_queries
        assert archived.roles == ["citable", "refutable"]
        assert archived.min_sim == 0.62
        assert archived.max_evidence == 17
        assert archived.per_paper_cap == 2

    def test_legacy_page_without_new_kwargs_has_no_archived_params(self):
        """不传五个新参数（旧调用点 / 模拟 2026-09-17 之前生成的页面）时，
        has_params 必须是 False——这是「旧页兼容」判定的唯一依据。"""
        fm_text = Q.build_qa_frontmatter(
            "问题", "qa-x", {"answer": "", "points": [], "caveats": [], "gaps": []},
            [], T.ValidationReport(), "2026-01-01T00:00:00")
        fm, _body = split_frontmatter(fm_text + "\n\n正文\n")
        archived = Q.read_archived_ask_params(fm)
        assert archived.has_params is False
        assert archived.extra_queries == [] and archived.roles == []
        assert archived.min_sim is None and archived.max_evidence is None

    def test_read_archived_ask_params_tolerates_corrupted_frontmatter(self):
        """手改 frontmatter 把数值字段改成非法值（bool/字符串）不该让重问整个崩溃，
        退化成"当没存过"更安全。"""
        assert Q.read_archived_ask_params(None).has_params is False
        assert Q.read_archived_ask_params({"ask_max_evidence": True}).has_params is False
        assert Q.read_archived_ask_params({"ask_max_evidence": "十"}).has_params is False
        bad = Q.read_archived_ask_params({"ask_max_evidence": 10, "ask_min_sim": True,
                                          "ask_query": "不是列表"})
        assert bad.has_params is True
        assert bad.min_sim is None            # bool 字段单独判非法，不牵连别的字段
        assert bad.extra_queries == []


def _capture_warnings():
    """loguru 不走标准 logging，caplog 抓不到——挂一个临时 sink 收字符串
    （同 test_embed_store.py 的既有模式）。"""
    from loguru import logger as _lg
    buf = []
    sink_id = _lg.add(lambda m: buf.append(str(m)), level="WARNING")
    return buf, (lambda: _lg.remove(sink_id))


class TestArchivedMinSimNanInfRangeSafety:
    """M2（2026-09-17 R1 对抗审核）：YAML 认得 `.nan`/`.inf` 字面量，手改或损坏的
    frontmatter 完全可能带出 `ask_min_sim: .nan`。改动前 `_fm_num` 只挡"不是
    数字"（`isinstance` 判断），NaN/Inf 本身是合法的 Python float，会原样放行；
    `retrieve_evidence` 里 `eff_min = max(min_sim, ...)` 一旦有 NaN 参与，
    `score < eff_min` 按 IEEE 754 语义恒假，相似度下限整个失效且没有任何提示。
    """

    def test_fm_num_rejects_nan_and_warns(self):
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"k": float("nan")}, "k", float)
        finally:
            stop()
        assert got is None
        assert any("NaN/Inf" in m for m in buf)

    def test_fm_num_rejects_inf_and_neg_inf(self):
        assert Q._fm_num({"k": float("inf")}, "k", float) is None
        assert Q._fm_num({"k": float("-inf")}, "k", float) is None

    def test_fm_num_rejects_out_of_range_when_bounds_given(self):
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"k": 1.5}, "k", float, min_val=-1.0, max_val=1.0)
        finally:
            stop()
        assert got is None
        assert any("超出合法范围" in m for m in buf)
        assert Q._fm_num({"k": -2.0}, "k", float, min_val=-1.0, max_val=1.0) is None

    def test_fm_num_accepts_inclusive_boundary_values(self):
        assert Q._fm_num({"k": -1.0}, "k", float, min_val=-1.0, max_val=1.0) == -1.0
        assert Q._fm_num({"k": 1.0}, "k", float, min_val=-1.0, max_val=1.0) == 1.0

    def test_fm_num_without_bounds_still_rejects_nan_inf_for_max_evidence_style_fields(self):
        """`ask_max_evidence`/`ask_per_paper_cap` 不传 min_val/max_val（那两个的
        "正数"约束在 `_validate_numeric_params` 硬拒绝，这里只挡"读出来就不像话"
        的值）——但 NaN/Inf 检查是无条件的，不依赖有没有传界。"""
        assert Q._fm_num({"k": float("nan")}, "k", int) is None

    @pytest.mark.parametrize("bad_min_sim", [float("nan"), float("inf"), float("-inf"),
                                             1.5, -2.0])
    def test_read_archived_ask_params_degrades_nan_inf_out_of_range_min_sim_to_none(
            self, bad_min_sim):
        """端到端回归：`read_archived_ask_params` 拿到一份 `ask_min_sim` 非法的
        frontmatter dict，`archived.min_sim` 必须是 `None`（退化成"没存过"），
        **绝不能是 NaN/Inf/越界值本身**——这是本条 finding 要拦的确切后果。"""
        fm = {"ask_max_evidence": 20, "ask_min_sim": bad_min_sim,
             "ask_per_paper_cap": 3, "ask_query": [], "ask_role": []}
        archived = Q.read_archived_ask_params(fm)
        assert archived.has_params is True     # 哨兵字段（ask_max_evidence）仍合法
        assert archived.min_sim is None
        assert archived.max_evidence == 20     # 不牵连其它字段

    def test_yaml_dot_nan_literal_round_trips_to_none_not_to_nan(self):
        """PyYAML 原生认得 `.nan`/`.inf` 字面量（`split_frontmatter` 靠
        `yaml.safe_load`）——直接从**原始 YAML 文本**（模拟手改 frontmatter）
        往回读，而不是构造 Python dict，逐字复现真实故障路径。"""
        raw = ("---\nqa: qa-x\nask_max_evidence: 20\nask_min_sim: .nan\n"
              "ask_per_paper_cap: 3\nask_query: []\nask_role: []\n---\n\n正文\n")
        fm, _body = split_frontmatter(raw)
        assert fm is not None
        import math as _math
        assert _math.isnan(fm["ask_min_sim"])   # 前提：YAML 确实解析成了 float('nan')
        archived = Q.read_archived_ask_params(fm)
        assert archived.min_sim is None         # 但 read_archived_ask_params 挡住了它


class TestArchivedIntegerFieldSanity:
    """MAJOR-2 + MINOR-4（2026-09-17 R2 对抗审核）：`ask_max_evidence`/
    `ask_per_paper_cap` 的两类"读出来就不像话"——非整数（`int()` 静默截断）与
    超出宽松上界——`_fm_num` 都要能识别并退化成 `None`+warning，不能悄悄接受。
    """

    def test_fm_num_rejects_non_integer_float_for_int_cast(self):
        """MAJOR-2 核心回归：`3.7` 此前会被 `int(3.7)` 静默截成 `3`——既不退化
        `None` 也不 warning，直接违反"写了但是垃圾必须能区分"的契约。"""
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"k": 3.7}, "k", int)
        finally:
            stop()
        assert got is None
        assert any("不是整数" in m for m in buf)

    def test_fm_num_accepts_integer_valued_float_for_int_cast(self):
        """`3.0` 是"值本身就是整数"，YAML 里写 `ask_max_evidence: 20` 这种整数
        字面量在 Python 侧也可能以 float 形式出现（视 YAML 加载器细节），不该
        被上一条的修复连带误伤——`(3.0).is_integer()` 为真，必须正常接受为 `3`。
        """
        assert Q._fm_num({"k": 3.0}, "k", int) == 3

    def test_fm_num_string_digit_rejected_by_existing_type_gate(self):
        """`"3"`（字符串）——**这不是本轮修复的新行为**，是既有的类型校验
        （`not isinstance(v, (int, float))`）本来就会挡住；这条测试把它显式锁定
        下来，避免以后有人以为"字符串数字"需要额外特殊处理。"""
        assert Q._fm_num({"k": "3"}, "k", int) is None

    def test_fm_num_bool_rejected_for_int_cast(self):
        """bool 是 int 的子类，必须被最前面那道 `isinstance(v, bool)` 排除掉，
        不会走到新加的整数值校验那一步（这里显式验证两道防线没有互相干扰）。"""
        assert Q._fm_num({"k": True}, "k", int) is None

    def test_read_archived_ask_params_non_integer_marker_field_degrades_whole_page(self):
        """端到端：`ask_max_evidence` 本身**同时也是** `has_params` 的哨兵字段
        （`ASK_PARAMS_MARKER_KEY`）——`3.7` 这种典型手误（多打一个小数点）让
        哨兵检测本身就判不出"有没有存过参数"，`read_archived_ask_params` 据此
        整页退化成"没存过"（`ArchivedAskParams()` 全默认），不只是这一个字段
        变 `None`。这比"只影响一个字段"更保守，但一致：哨兵都不可信了，没有
        理由继续相信同一份 frontmatter 里别的字段。"""
        fm = {"ask_max_evidence": 3.7, "ask_min_sim": 0.55, "ask_per_paper_cap": 3,
             "ask_query": [], "ask_role": []}
        archived = Q.read_archived_ask_params(fm)
        assert archived.has_params is False
        assert archived.max_evidence is None
        assert archived.min_sim is None
        assert archived.per_paper_cap is None

    def test_read_archived_ask_params_degrades_non_integer_per_paper_cap_only(self):
        """同一件事换一个不兼任哨兵的字段验证"只影响这一个字段"：`ask_max_evidence`
        这里是合法整数（哨兵检测通过，`has_params=True`），`ask_per_paper_cap: 3.7`
        单独作废，不牵连 `max_evidence`/`min_sim`。"""
        fm = {"ask_max_evidence": 20, "ask_min_sim": 0.55, "ask_per_paper_cap": 3.7,
             "ask_query": [], "ask_role": []}
        archived = Q.read_archived_ask_params(fm)
        assert archived.has_params is True
        assert archived.per_paper_cap is None
        assert archived.max_evidence == 20
        assert archived.min_sim == 0.55

    def test_fm_num_rejects_value_above_upper_bound(self):
        """MINOR-4：`max_val=ASK_NUMERIC_UPPER_BOUND`（500）之上的存档值退化
        成 `None`——`DEFAULT_QA_MAX_EVIDENCE` 是 28，2800 这种量级只可能是手误
        多打了两个零或数据损坏。"""
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"k": 501}, "k", int, max_val=Q.ASK_NUMERIC_UPPER_BOUND)
        finally:
            stop()
        assert got is None
        assert any("超出合法范围" in m for m in buf)
        assert Q._fm_num({"k": 500}, "k", int, max_val=Q.ASK_NUMERIC_UPPER_BOUND) == 500

    def test_read_archived_ask_params_degrades_over_bound_per_paper_cap_to_none(self):
        fm = {"ask_max_evidence": 20, "ask_min_sim": 0.55, "ask_per_paper_cap": 2800,
             "ask_query": [], "ask_role": []}
        archived = Q.read_archived_ask_params(fm)
        assert archived.per_paper_cap is None
        assert archived.max_evidence == 20       # 不牵连其它字段


class TestFmNumWarningDiscipline:
    """MAJOR-R3-1（2026-09-17 R3 对抗审核）：`_fm_num` 四类拒绝分支此前只有
    "类型完全不对"（bool/字符串/dict……）这一支静默返回、不打 warning，与
    "键压根不存在"走同一条路径——而 `ask_max_evidence` 兼任 `has_params` 哨兵，
    它一坏，其余四个字段哪怕本身合法也整页被当成没存过，且此前零日志、
    `_load_reask_target` 还会打印一句断言"2026-09-17 之前的产物"的事实错误。
    """

    def test_key_missing_is_silent(self):
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({}, "k", int)
        finally:
            stop()
        assert got is None and buf == []

    def test_key_present_as_none_is_silent(self):
        """`ask_min_sim: null`（`build_qa_frontmatter` 在调用方没传对应参数时
        就是这么写的）与"键不存在"同义——都是"明确表示没有值"，不是数据损坏。"""
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"k": None}, "k", int)
        finally:
            stop()
        assert got is None and buf == []

    def test_key_present_wrong_type_bool_warns(self):
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"k": True}, "k", int)
        finally:
            stop()
        assert got is None
        assert any("类型不对" in m and "k" in m for m in buf)

    def test_key_present_wrong_type_string_warns_with_key_and_type(self):
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"ask_max_evidence": "十"}, "ask_max_evidence", int)
        finally:
            stop()
        assert got is None
        assert any("ask_max_evidence" in m and "类型不对" in m and "str" in m for m in buf)

    def test_key_present_wrong_type_dict_warns(self):
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_num({"k": {"a": 1}}, "k", int)
        finally:
            stop()
        assert got is None
        assert any("类型不对" in m for m in buf)

    def test_read_archived_ask_params_marker_missing_is_silent(self):
        """键完全没写（老页面的常态）——不该有任何 warning。"""
        buf, stop = _capture_warnings()
        try:
            archived = Q.read_archived_ask_params({"question": "x"})
        finally:
            stop()
        assert archived.has_params is False
        assert buf == []

    @pytest.mark.parametrize("bad_marker", [True, "十", {"a": 1}])
    def test_read_archived_ask_params_marker_present_but_invalid_warns_page_level(
            self, bad_marker):
        """哨兵字段**存在但非法**：既要有 `_fm_num` 自己那条"这个键类型不对"，
        也要有 `read_archived_ask_params` 追加的"整页检索参数按未存档处理"——
        后者是这条 finding 要补的，解释"为什么其它合法字段也没了"。"""
        fm = {"ask_max_evidence": bad_marker, "ask_min_sim": 0.55,
             "ask_per_paper_cap": 3, "ask_query": ["foo"], "ask_role": []}
        buf, stop = _capture_warnings()
        try:
            archived = Q.read_archived_ask_params(fm)
        finally:
            stop()
        assert archived.has_params is False
        assert archived.min_sim is None            # 本身合法，但被哨兵坏掉连累
        assert any("整页检索参数按未存档处理" in m for m in buf)

    def test_load_reask_target_message_does_not_assert_a_date_for_corrupted_marker(
            self, tmp_path, capsys):
        """`_load_reask_target` 的提示文案不能断言"2026-09-17 之前的产物"——
        哨兵字段被手改坏时，页面完全可能是本轮功能上线**之后**生成的。"""
        d = tmp_path / "qa"
        d.mkdir()
        path = d / "qa-corrupted.md"
        path.write_text(
            "---\nqa: qa-corrupted\nquestion: \"坏掉的问题\"\n"
            "ask_max_evidence: true\n---\n\n正文\n", encoding="utf-8")
        target, err = AN._load_reask_target(d, "qa-corrupted", _Args())
        assert err is None
        out = capsys.readouterr().out
        assert "未读到有效的检索参数" in out
        assert "2026-09-17" not in out


class TestFmStrListDropsNonStringItems:
    """MINOR-5（2026-09-17 R2 对抗审核）：`ask_query`/`ask_role` 列表里混进非
    字符串项（`None`/dict/数字）此前被 `str(x)` 无差别强转，`None` 会变成
    字面量检索词 `"None"` 混进 `-q` 参数。"""

    def test_none_item_is_dropped_not_stringified(self):
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_str_list(["foo", None])
        finally:
            stop()
        assert got == ["foo"]
        assert "None" not in got                # 关键：不能出现字面量字符串 "None"
        assert any("不是字符串" in m for m in buf)

    def test_dict_item_is_dropped_not_stringified(self):
        got = Q._fm_str_list(["foo", {"a": 1}])
        assert got == ["foo"]

    def test_number_item_is_dropped_not_stringified(self):
        got = Q._fm_str_list(["foo", 42])
        assert got == ["foo"]

    def test_empty_string_still_silently_dropped_no_warning(self):
        """空白字符串是普通格式噪音，不是"垃圾"——不该打 warning，这条区分
        "类型错误"与"内容为空"两种不同性质的丢弃。"""
        buf, stop = _capture_warnings()
        try:
            got = Q._fm_str_list(["foo", "", "   "])
        finally:
            stop()
        assert got == ["foo"]
        assert buf == []

    def test_non_list_input_returns_empty_unchanged(self):
        assert Q._fm_str_list("not-a-list") == []
        assert Q._fm_str_list(None) == []

    def test_read_archived_ask_params_end_to_end_drops_none_from_query_list(self):
        fm = {"ask_max_evidence": 20, "ask_min_sim": 0.55, "ask_per_paper_cap": 3,
             "ask_query": ["informative missingness", None], "ask_role": []}
        archived = Q.read_archived_ask_params(fm)
        assert archived.extra_queries == ["informative missingness"]


# ===========================================================================
# 六、write_qa_page：用户批注区保留 + 参数一起写入
# ===========================================================================

def _EV3():
    return [_paper_ev("E1", "a2024X"), _paper_ev("E2", "b2024Y")]


_QA_SIMPLE = {"answer": "简短回答", "points": [{"text": "论断一", "refs": ["E1"],
             "citekeys": ["a2024X"]}], "caveats": [], "gaps": []}


class TestWriteQaPagePreservesUserZone:
    def test_user_annotation_survives_a_rerun_that_also_carries_ask_params(self, tmp_path):
        d = tmp_path / "qa"
        ev = _EV3()
        path, status = Q.write_qa_page(d, "qa-x", "问题", _QA_SIMPLE, ev, T.ValidationReport(),
                                       extra_queries=["informative missingness"],
                                       roles=["citable"], min_sim=0.6, max_evidence=20,
                                       per_paper_cap=3)
        assert status == "new"
        txt = path.read_text(encoding="utf-8")
        path.write_text(txt.replace("## 我的批注", "## 我的批注\n\n第 1 条我核对过，没问题"),
                        encoding="utf-8")

        _p, status2 = Q.write_qa_page(d, "qa-x", "问题", _QA_SIMPLE, ev, T.ValidationReport(),
                                      extra_queries=["informative missingness"],
                                      roles=["citable"], min_sim=0.6, max_evidence=20,
                                      per_paper_cap=3)
        assert status2 in ("merged", "unchanged")
        final = path.read_text(encoding="utf-8")
        assert "第 1 条我核对过，没问题" in final
        fm, _body = split_frontmatter(final)
        assert fm["ask_query"] == ["informative missingness"]
        assert fm["ask_max_evidence"] == 20


# ===========================================================================
# 七、--reask 合并语义（scripts/ask_notes.py 的纯函数部分）
# ===========================================================================

class _Args:
    """`_resolve_ask_params` 只读这五个属性，构造一个最小 stand-in 代替
    `argparse.Namespace`（避免每条用例都手搭一整套 CLI 解析）。"""
    def __init__(self, query=None, role=None, min_sim=AN._UNSET,
                max_evidence=AN._UNSET, per_paper_cap=AN._UNSET):
        self.query = query
        self.role = role
        self.min_sim = min_sim
        self.max_evidence = max_evidence
        self.per_paper_cap = per_paper_cap


def _archived(**kw):
    base = dict(extra_queries=["存档词"], roles=["method"], min_sim=0.7,
               max_evidence=40, per_paper_cap=5, has_params=True)
    base.update(kw)
    return Q.ArchivedAskParams(**base)


class TestResolveAskParams:
    def test_no_cli_override_falls_back_to_archived(self):
        eq, roles, min_sim, max_ev, cap = AN._resolve_ask_params(_Args(), _archived())
        assert (eq, roles, min_sim, max_ev, cap) == (["存档词"], ["method"], 0.7, 40, 5)

    def test_explicit_cli_replaces_not_merges_archived_query_and_role(self):
        """「覆盖」是替换不是并集——命令行给了 -q/--role 就整体顶掉存档值。"""
        args = _Args(query=["新词"], role=["citable", "refutable"])
        eq, roles, _s, _e, _c = AN._resolve_ask_params(args, _archived())
        assert eq == ["新词"]                  # 不是 ["存档词", "新词"]
        assert roles == ["citable", "refutable"]

    def test_explicit_cli_numeric_overrides_archived_numeric(self):
        args = _Args(min_sim=0.5, max_evidence=10, per_paper_cap=2)
        _eq, _r, min_sim, max_ev, cap = AN._resolve_ask_params(args, _archived())
        assert (min_sim, max_ev, cap) == (0.5, 10, 2)

    def test_no_archive_and_no_cli_falls_back_to_code_defaults(self):
        """全新提问（`has_params=False`）与旧页重问共用同一条兜底：退到代码默认值。"""
        eq, roles, min_sim, max_ev, cap = AN._resolve_ask_params(
            _Args(), Q.ArchivedAskParams())
        assert eq == [] and roles == []
        assert min_sim == T.DEFAULT_MIN_SIM
        assert max_ev == Q.DEFAULT_QA_MAX_EVIDENCE
        assert cap == Q.DEFAULT_QA_PER_PAPER_CAP

    def test_archived_has_params_but_individual_field_missing_uses_code_default(self):
        """存档页整体 has_params=True，但某个数值字段本身是 None（如手改坏了）——
        只有那一个字段退回代码默认值，不牵连其它字段。"""
        archived = _archived(min_sim=None)
        _eq, _r, min_sim, max_ev, _c = AN._resolve_ask_params(_Args(), archived)
        assert min_sim == T.DEFAULT_MIN_SIM
        assert max_ev == 40                    # 别的字段仍然沿用存档值

    def test_validate_numeric_params_rejects_non_positive(self):
        assert AN._validate_numeric_params(0.5, 10, 0) is not None
        assert AN._validate_numeric_params(0.5, 0, 3) is not None
        assert AN._validate_numeric_params(0.5, 10, 3) is None

    @pytest.mark.parametrize("bad_min_sim", [float("nan"), float("inf"), float("-inf"),
                                             1.5, -2.0])
    def test_validate_numeric_params_rejects_bad_min_sim(self, bad_min_sim):
        """M2：min_sim 与 max_evidence/per_paper_cap **同等对待**——硬拒绝，
        不是静默退化。这是"这一次真的要用的值"落地前的最后一道闸，不管它是
        命令行敲的还是从存档合并过来的。"""
        assert AN._validate_numeric_params(bad_min_sim, 10, 3) is not None

    def test_validate_numeric_params_accepts_inclusive_min_sim_boundary(self):
        assert AN._validate_numeric_params(-1.0, 10, 3) is None
        assert AN._validate_numeric_params(1.0, 10, 3) is None
        assert AN._validate_numeric_params(0.55, 10, 3) is None

    def test_validate_numeric_params_rejects_above_upper_bound(self):
        """MINOR-4：CLI 侧硬拒绝，上界与 `qa.ASK_NUMERIC_UPPER_BOUND` 同一个常量
        （不是各写一份魔数）——`--max-evidence 2800`/`--per-paper-cap 2800`
        这种量级只可能是手误或想表达"不限制"却打错了。"""
        assert AN._validate_numeric_params(0.5, Q.ASK_NUMERIC_UPPER_BOUND + 1, 3) is not None
        assert AN._validate_numeric_params(0.5, 10, Q.ASK_NUMERIC_UPPER_BOUND + 1) is not None

    def test_validate_numeric_params_accepts_upper_bound_inclusive(self):
        assert AN._validate_numeric_params(
            0.5, Q.ASK_NUMERIC_UPPER_BOUND, Q.ASK_NUMERIC_UPPER_BOUND) is None


# ===========================================================================
# 八、--reask：读回存档页
# ===========================================================================

class TestLoadReaskTarget:
    def test_reads_archived_question_and_params(self, tmp_path):
        d = tmp_path / "qa"
        Q.write_qa_page(d, "qa-x", "MNAR 能不能诊断？", _QA_SIMPLE, _EV3(),
                        T.ValidationReport(), extra_queries=["missingness"],
                        roles=["citable"], min_sim=0.58, max_evidence=25, per_paper_cap=4)
        target, err = AN._load_reask_target(d, "qa-x", _Args())
        assert err is None
        question, slug, eq, roles, min_sim, max_ev, cap = target
        assert question == "MNAR 能不能诊断？"
        assert slug == "qa-x"
        assert (eq, roles, min_sim, max_ev, cap) == (["missingness"], ["citable"], 0.58, 25, 4)

    def test_cli_override_applies_during_reask(self, tmp_path):
        d = tmp_path / "qa"
        Q.write_qa_page(d, "qa-x", "问题", _QA_SIMPLE, _EV3(), T.ValidationReport(),
                        max_evidence=25)
        target, err = AN._load_reask_target(d, "qa-x", _Args(max_evidence=99))
        assert err is None
        assert target[5] == 99                 # max_evidence 位

    def test_legacy_page_reports_no_archived_params(self, tmp_path, capsys):
        d = tmp_path / "qa"
        Q.write_qa_page(d, "qa-old", "旧问题", _QA_SIMPLE, _EV3(), T.ValidationReport())
        target, err = AN._load_reask_target(d, "qa-old", _Args())
        assert err is None
        question, slug, eq, roles, min_sim, max_ev, cap = target
        assert question == "旧问题"
        # 旧页没有存档参数，退回代码默认值（同全新提问）
        assert eq == [] and roles == []
        assert max_ev == Q.DEFAULT_QA_MAX_EVIDENCE
        assert "旧页" in capsys.readouterr().out

    def test_missing_page_returns_error(self, tmp_path):
        d = tmp_path / "qa"
        d.mkdir()
        target, err = AN._load_reask_target(d, "qa-does-not-exist", _Args())
        assert target is None and "没有已归档页面" in err

    def test_reask_by_recomputed_slug_falls_back_to_legacy_digest_filename(self, tmp_path):
        """MINOR-1（2026-09-17 R2 对抗审核）：真实场景——`qa-mnar-ehr-1f424623.md`
        是 2026-08-17 之前的旧 digest 口径文件名，从未被重命名过。用户拿"按今天
        算法重算"的新 slug 去 `--reask`，此前直接报"没有已归档页面"退 2，即便
        那个问题明明已经归档过。修复后：反查到身份相同的旧文件，用真实 slug
        继续、并提示映射关系。"""
        d = tmp_path / "qa"
        question = "MNAR 诊断在纵向 EHR 上到底能不能做？"
        legacy_slug = "qa-legacy-1f424623"        # 模拟"旧 digest 口径"的任意旧文件名
        Q.write_qa_page(d, legacy_slug, question, _QA_SIMPLE, _EV3(), T.ValidationReport())
        recomputed_slug = Q.qa_slug(question)       # "今天的算法"会给出的新 slug
        assert recomputed_slug != legacy_slug       # 前提：两者确实不同

        target, err = AN._load_reask_target(d, recomputed_slug, _Args())
        assert err is None
        q_text, slug, _eq, _roles, _ms, _me, _cap = target
        assert q_text == question
        assert slug == legacy_slug                  # 用真实文件名，不是用户输入的那个

    def test_reask_slug_with_no_matching_legacy_page_still_errors(self, tmp_path):
        """反查找不到身份相同的页时，不能假装成功——继续报错，文案提示先 --list。"""
        d = tmp_path / "qa"
        Q.write_qa_page(d, "qa-x", "问题甲", _QA_SIMPLE, _EV3(), T.ValidationReport())
        target, err = AN._load_reask_target(d, "qa-completely-unrelated-slug", _Args())
        assert target is None
        assert "没有已归档页面" in err and "--list" in err

    def test_reask_direct_hit_does_not_trigger_legacy_fallback_scan(self, tmp_path, monkeypatch):
        """slug 直接命中真实文件时不该走反查扫描那条路（性能与正确性的双重
        保证——用 monkeypatch 确认 `list_qa_pages` 压根没被调用）。"""
        d = tmp_path / "qa"
        Q.write_qa_page(d, "qa-x", "问题甲", _QA_SIMPLE, _EV3(), T.ValidationReport())
        called = []
        real_list = Q.list_qa_pages

        def _spy(*a, **kw):
            called.append(1)
            return real_list(*a, **kw)
        monkeypatch.setattr(Q, "list_qa_pages", _spy)
        target, err = AN._load_reask_target(d, "qa-x", _Args())
        assert err is None and target[1] == "qa-x"
        assert not called

    def test_illegal_slug_returns_error(self, tmp_path):
        d = tmp_path / "qa"
        d.mkdir()
        target, err = AN._load_reask_target(d, "带 空格", _Args())
        assert target is None and err

    def test_page_with_missing_question_key_returns_error(self, tmp_path):
        d = tmp_path / "qa"
        d.mkdir()
        (d / "qa-broken.md").write_text("---\nqa: qa-broken\n---\n\n正文\n", encoding="utf-8")
        target, err = AN._load_reask_target(d, "qa-broken", _Args())
        assert target is None and "question" in err


# ===========================================================================
# 九、--reask-all：计划 / --yes 门槛 / 批量汇总
# ===========================================================================

class TestBatchSummary:
    def test_groups_outcomes_by_status_and_lists_slugs(self, capsys):
        outcomes = [
            AN.AskOutcome(slug="qa-a", question="Q1", status="new"),
            AN.AskOutcome(slug="qa-b", question="Q2", status="merged"),
            AN.AskOutcome(slug="qa-c", question="Q3", status="failed", detail="LLM 挂了"),
            AN.AskOutcome(slug="qa-d", question="Q4", status="conflict"),
        ]
        AN._print_batch_summary(outcomes)
        out = capsys.readouterr().out
        assert "共 4 页" in out
        assert "qa-a" in out and "new" in out
        assert "qa-c" in out and "LLM 挂了" in out
        assert "qa-d" in out and "未覆盖" in out


def _cli_env(tmp_path, *, extra_lines=""):
    notes = tmp_path / "notes"
    (notes / "topics" / "qa").mkdir(parents=True)
    (notes / "literature_index.json").write_text(
        '{"papers": [], "generated_at": "2026-09-17T00:00:00"}', encoding="utf-8")
    env = tmp_path / "scholar_test.env"
    env.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\nLLM__GEMINI_API_KEY=FAKE\nLLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n{}".format(notes, extra_lines), encoding="utf-8")
    return notes, env


class _FakeStore:
    model = "bge-m3"

    def freshness_warning(self, _idx_at):
        return None


class _FakeEmbedClient:
    def __init__(self, base_url=None, model=None):
        pass

    def probe(self):
        return {"model": "bge-m3"}

    def embed(self, texts):
        return np.zeros((len(texts), 4), dtype=np.float32)

    def close(self):
        pass


class TestReaskAllCli:
    """走 `AN.main()` 真正的入口（`sys.argv` + `capsys`，同 test_lint.py 里
    `_run_cli` 的既有模式）。VectorStore/EmbeddingClient 换成最小假实现——
    这几条用例要测的是 `main()` 自己的编排逻辑（计划怎么打印、--yes 门槛、
    单页失败怎么不中断其余页），不是真的召回/合成。
    """

    def test_single_question_reports_param_error_before_touching_ollama(
            self, tmp_path, monkeypatch, capsys):
        """MINOR-R3-2（2026-09-17 R3 对抗审核）：`_validate_numeric_params` 是纯
        算术，不依赖网络，必须排在 `VectorStore.load()`/`EmbeddingClient` 之前。
        "Ollama 不可用 + 参数越界"同时发生时，用户必须先看到参数错误（退出码
        2，改一下命令行就能自己修）——而不是网络故障（退出码 3，看起来像是
        环境问题，实际上命令本身就是错的）。用两个"一被调用就断言失败"的假
        实现直接证明 `VectorStore.load`/`EmbeddingClient` 在参数校验失败后
        根本没被碰过，不只是"退出码对了、但内部顺序其实没变"这种巧合。
        """
        _notes, env = _cli_env(tmp_path)

        def _boom_store(*a, **kw):
            raise AssertionError("参数校验失败后不该还去加载向量库")

        class _BoomEmbedClient:
            def __init__(self, *a, **kw):
                raise AssertionError("参数校验失败后不该还去构造 EmbeddingClient")

        monkeypatch.setattr(AN, "VectorStore",
                            type("X", (), {"load": staticmethod(_boom_store)}))
        monkeypatch.setattr(AN, "EmbeddingClient", _BoomEmbedClient)
        monkeypatch.setattr(sys, "argv", [
            "ask_notes.py", "--config", str(env), "随便什么问题", "--min-sim=-inf"])
        rc = AN.main()
        err = capsys.readouterr().err
        assert rc == 2
        assert "min_sim" in err and "NaN/Inf" in err

    def test_reask_all_prevalidates_all_targets_before_touching_ollama(
            self, tmp_path, monkeypatch, capsys):
        """批量路径的镜像版本：两个 `--reask` 目标**都**在预校验阶段被筛掉——
        一个在磁盘上不存在（`_load_reask_target` 直接报错），另一个存在，但这
        次命令行显式给了 `--min-sim=-inf`：`_resolve_ask_params` 里命令行显式
        值对**批量里的每一页都生效**（一次调用只有一份 `args`，不是逐页各读
        各的命令行），所以这一页会在预校验循环里被 `_validate_numeric_params`
        拦下来。两个目标全灭，`prevalidated_targets` 因此为空，
        `VectorStore.load`/`EmbeddingClient` 应该**一次都不会被调用**。

        没有直接拿"archived 字段本身越界"（比如把 `ask_max_evidence` 存成
        99999）来构造失败目标：那种输入会先被 `qa._fm_num` 的哨兵校验判定成
        "整页参数没存过"（MAJOR-R3-1），退化成"用默认参数重问"而不是报错，
        反而会让这一页**变成合法目标**、破坏本用例想搭的"两个都失败"前提。
        用会断言失败的假实现直接证明"真的没碰网络"，而不是只看退出码——退出码
        对了也可能是侥幸（万一它们先被调用了、只是恰好没报错）。"""
        notes, env = _cli_env(tmp_path)
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-exists", "存在的问题", _QA_SIMPLE,
                        _EV3(), T.ValidationReport())

        def _boom(*a, **kw):
            raise AssertionError("预校验全部失败时不该碰网络资源")
        monkeypatch.setattr(AN, "VectorStore", type("X", (), {"load": staticmethod(_boom)}))
        monkeypatch.setattr(AN, "EmbeddingClient", _boom)
        monkeypatch.setattr(sys, "argv", [
            "ask_notes.py", "--config", str(env), "--reask", "qa-exists",
            "--reask", "qa-does-not-exist", "--min-sim=-inf"])
        rc = AN.main()
        err = capsys.readouterr().err
        assert "qa-exists" in err and "min_sim" in err and "NaN/Inf" in err
        assert "qa-does-not-exist" in err and "没有已归档页面" in err
        assert rc == 2

    def test_reask_all_without_yes_only_prints_plan_and_never_touches_vector_store(
            self, tmp_path, monkeypatch, capsys):
        notes, env = _cli_env(tmp_path)
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-a", "问题甲", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-b", "问题乙", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())

        def _boom(*a, **kw):
            raise AssertionError("不该在只打印计划这一步就去加载向量库")
        monkeypatch.setattr(AN, "VectorStore", type("X", (), {"load": staticmethod(_boom)}))
        monkeypatch.setattr(sys, "argv", ["ask_notes.py", "--config", str(env), "--reask-all"])
        rc = AN.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "计划重问 2 页" in out
        assert "qa-a" in out and "qa-b" in out
        assert "--yes" in out

    def test_reask_all_with_yes_proceeds_past_the_gate(self, tmp_path, monkeypatch, capsys):
        """给了 --yes 之后不该再打印"加 --yes"的提示——即使后面因为向量库不可用
        （这里让 VectorStore.load 抛错）而提前退出，也证明它确实越过了那道门槛。"""
        notes, env = _cli_env(tmp_path)
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-a", "问题甲", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        from src.scholar.embed_store import VectorStoreError

        def _boom(*a, **kw):
            raise VectorStoreError("测试用：假装向量库不可用")
        monkeypatch.setattr(AN, "VectorStore", type("X", (), {"load": staticmethod(_boom)}))
        monkeypatch.setattr(sys, "argv",
                            ["ask_notes.py", "--config", str(env), "--reask-all", "--yes"])
        rc = AN.main()
        out = capsys.readouterr().out
        assert rc == 2
        assert "加 --yes" not in out
        assert "计划重问 1 页" in out            # 计划仍然打印了，只是没有停在那里

    def test_reask_all_no_pages_is_a_clean_noop(self, tmp_path, monkeypatch, capsys):
        notes, env = _cli_env(tmp_path)
        monkeypatch.setattr(sys, "argv", ["ask_notes.py", "--config", str(env), "--reask-all"])
        assert AN.main() == 0
        assert "无事可做" in capsys.readouterr().out

    def test_reask_all_batch_continues_after_one_page_fails_and_prints_summary(
            self, tmp_path, monkeypatch, capsys):
        """单页失败不中断其余页——核心批量回归。`_run_one` 换成一个假实现：
        `qa-bad` 抛异常、`qa-good` 正常返回；两页都必须被处理到，汇总里两页
        都要点名，退出码必须体现"本轮有失败"。"""
        notes, env = _cli_env(tmp_path)
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-bad", "坏问题", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-good", "好问题", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        monkeypatch.setattr(AN, "VectorStore",
                            type("X", (), {"load": staticmethod(lambda p: _FakeStore())}))
        monkeypatch.setattr(AN, "EmbeddingClient", _FakeEmbedClient)

        calls = []

        def _fake_run_one(question, slug, **kw):
            calls.append(slug)
            if slug == "qa-bad":
                raise RuntimeError("模拟合成阶段炸了")
            return AN.AskOutcome(slug=slug, question=question, status="new")
        monkeypatch.setattr(AN, "_run_one", _fake_run_one)

        monkeypatch.setattr(sys, "argv", [
            "ask_notes.py", "--config", str(env), "--reask-all", "--yes",
            "--topics-config", str(tmp_path / "no-such-topics.yaml")])
        rc = AN.main()
        out = capsys.readouterr().out
        assert set(calls) == {"qa-bad", "qa-good"}, "两页都必须被处理到，一个都不能被跳过"
        assert "重问汇总" in out
        assert "qa-good" in out and "new" in out
        assert "模拟合成阶段炸了" in out
        assert rc == 2                          # 有失败页、没有冲突页 → 退 2

    def test_reask_all_batch_exit_code_accounts_for_taken_status(
            self, tmp_path, monkeypatch, capsys):
        """M1（2026-09-17 R1 对抗审核）：批量路径末尾的退出码判定漏了 `taken`。
        单问题路径把 `taken` 与 `failed` 同等对待（都退 2，见 `main()` 靠后的
        `if o.status in ("taken", "failed"): return 2`）——slug 被另一个问题占用
        是真实故障，不比"合成失败"轻。此前批量路径只查 `failed`，`taken` 页存在
        时退出码仍是 0，会被下游 launchd/编排脚本当成"全部成功"，而汇总里其实
        打着 ❌。这条测试直接对应审核员的探针
        `test_r1_taken_exitcode_probe.py::test_probe_taken_status_during_reask_all_exit_code`
        （断言方向相反：探针锁的是"错误行为"，这里锁的是修完之后的正确行为）。
        """
        notes, env = _cli_env(tmp_path)
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-taken", "问题甲", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-good", "问题乙", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        monkeypatch.setattr(AN, "VectorStore",
                            type("X", (), {"load": staticmethod(lambda p: _FakeStore())}))
        monkeypatch.setattr(AN, "EmbeddingClient", _FakeEmbedClient)

        def _fake_run_one(question, slug, **kw):
            if slug == "qa-taken":
                return AN.AskOutcome(slug=slug, question=question, status="taken",
                                     detail="占用中")
            return AN.AskOutcome(slug=slug, question=question, status="new")
        monkeypatch.setattr(AN, "_run_one", _fake_run_one)

        monkeypatch.setattr(sys, "argv", [
            "ask_notes.py", "--config", str(env), "--reask-all", "--yes",
            "--topics-config", str(tmp_path / "no-such-topics.yaml")])
        rc = AN.main()
        out = capsys.readouterr().out
        assert "qa-taken" in out
        assert rc == 2, "taken 页存在，批量退出码必须体现失败——不能是 0"

    def test_reask_all_conflict_beats_failed_in_exit_code_priority(
            self, tmp_path, monkeypatch, capsys):
        """NIT-R3-2（2026-09-17 R3 对抗审核）：批量末尾的退出码判定是"先查
        conflict、再查 failed/taken"（见 `main()` 靠后的
        `if any(... == "conflict"): return 1` 排在
        `if any(... in ("failed", "taken")): return 2` 前面）——同一批里**同时**
        出现 conflict 与 failed/taken 时，必须退 1 不是退 2。这条优先级本身这一
        轮没改动，但此前没有测试真的把两种结局凑在同一批里过（M1/`taken`那条
        只单独测过 taken，没跟 conflict 同批出现过），补上防止以后重排 if 顺序
        时没人发现。"""
        notes, env = _cli_env(tmp_path)
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-conflict", "问题甲", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-bad", "问题乙", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        monkeypatch.setattr(AN, "VectorStore",
                            type("X", (), {"load": staticmethod(lambda p: _FakeStore())}))
        monkeypatch.setattr(AN, "EmbeddingClient", _FakeEmbedClient)

        def _fake_run_one(question, slug, **kw):
            if slug == "qa-conflict":
                return AN.AskOutcome(slug=slug, question=question, status="conflict",
                                     detail="哈希不匹配")
            return AN.AskOutcome(slug=slug, question=question, status="failed",
                                 detail="模拟合成失败")
        monkeypatch.setattr(AN, "_run_one", _fake_run_one)

        monkeypatch.setattr(sys, "argv", [
            "ask_notes.py", "--config", str(env), "--reask-all", "--yes",
            "--topics-config", str(tmp_path / "no-such-topics.yaml")])
        rc = AN.main()
        out = capsys.readouterr().out
        assert "qa-conflict" in out and "qa-bad" in out
        assert rc == 1, "conflict 与 failed/taken 同批出现时，conflict 优先级更高，必须退 1"

    def test_reask_dry_run_bypasses_yes_gate(self, tmp_path, monkeypatch, capsys):
        """`--dry-run` 不调 LLM、没有成本，配合 `--reask-all` 时不该被 --yes 门槛拦住。"""
        notes, env = _cli_env(tmp_path)
        Q.write_qa_page(Q.qa_dir_for(notes), "qa-a", "问题甲", _QA_SIMPLE, _EV3(),
                        T.ValidationReport())
        monkeypatch.setattr(AN, "VectorStore",
                            type("X", (), {"load": staticmethod(lambda p: _FakeStore())}))
        monkeypatch.setattr(AN, "EmbeddingClient", _FakeEmbedClient)

        calls = []

        def _fake_run_one(question, slug, *, dry_run, **kw):
            calls.append((slug, dry_run))
            return AN.AskOutcome(slug=slug, question=question, status="dry_run")
        monkeypatch.setattr(AN, "_run_one", _fake_run_one)

        monkeypatch.setattr(sys, "argv", [
            "ask_notes.py", "--config", str(env), "--reask-all", "--dry-run",
            "--topics-config", str(tmp_path / "no-such-topics.yaml")])
        rc = AN.main()
        out = capsys.readouterr().out
        assert "加 --yes" not in out
        assert calls == [("qa-a", True)]
        assert rc == 0

    def test_reask_and_reask_all_are_mutually_exclusive(self, tmp_path, monkeypatch, capsys):
        _notes, env = _cli_env(tmp_path)
        monkeypatch.setattr(sys, "argv", ["ask_notes.py", "--config", str(env),
                                          "--reask", "qa-a", "--reask-all"])
        with pytest.raises(SystemExit) as exc:
            AN.main()
        assert exc.value.code == 2
        assert "不能同时使用" in capsys.readouterr().err

    def test_reask_rejects_a_positional_question(self, tmp_path, monkeypatch, capsys):
        _notes, env = _cli_env(tmp_path)
        monkeypatch.setattr(sys, "argv", ["ask_notes.py", "--config", str(env),
                                          "另一个新问题", "--reask", "qa-a"])
        with pytest.raises(SystemExit):
            AN.main()
        assert "位置参数问题" in capsys.readouterr().err


# ===========================================================================
# 十、撤稿检查覆盖问答页
# ===========================================================================

def _render(**kw):
    base = dict(verdicts=[], candidates=[], verdict_report=L.VerdictReport(),
               retraction=L.RetractionScan(), stale_claims=[], coverage=L.CoverageReport())
    base.update(kw)
    return L.render_lint_report(**base)


def _hit_scan(citekey="bad2024"):
    return L.RetractionScan(n_papers=1, n_with_doi=1, n_resolved=1, hits=[
        L.RetractionHit(citekey=citekey, doi="10.1/x", title="某被撤稿研究",
                        signal="openalex-flag")])


def _block(text, heading):
    for b in text.split("\n## "):
        if heading in b.splitlines()[0]:
            return b
    raise AssertionError("报告里没有 {} 这一节".format(heading))


class TestRetractionSectionCoversQaPages:
    def test_cited_by_qa_page_scans_only_qa_dir(self, tmp_path):
        """NIT-1（2026-09-17 R2 对抗审核订正）：首版这条测试在 `topics/` 顶层放的
        文件用的是 `topic:` 键——`cited_by_qa_page` 内部本来就有 `fm.get("qa")`
        字段过滤，那个文件不管放在哪一层都会被过滤掉，测试锁住的其实是"字段
        过滤对不对"而不是"目录边界对不对"（哪怕把扫描换成递归 `rglob`，这条
        测试照样绿）。改成在顶层放一个**带 `qa:` 键**的畸形文件（字段过滤会
        放行，只有目录边界能挡住它），才是真正在测"只扫 `topics/qa/`，不递归
        到 `topics/` 其它地方"。"""
        topics_dir = tmp_path / "topics"
        (topics_dir / "qa").mkdir(parents=True)
        (topics_dir / "misplaced-qa.md").write_text(
            "---\nqa: qa-misplaced\n---\n\n- 依据 [@shouldNotAppear]\n", encoding="utf-8")
        (topics_dir / "qa" / "qa-x.md").write_text(
            "---\nqa: qa-x\n---\n\n- 依据 [@bad2024]\n", encoding="utf-8")
        got = L.cited_by_qa_page(topics_dir)
        assert got == {"qa-x": ["bad2024"]}
        assert "qa-misplaced" not in got

    def test_cited_by_qa_page_recognizes_locator_bearing_citation(self, tmp_path):
        """撤稿论文若是本书籍精读并被问答页以带页码的形式引用，一样要被抓到——
        这是「书籍页码定位符」与「撤稿检查覆盖问答页」两项改动唯一的交叉点。"""
        topics_dir = tmp_path / "topics"
        (topics_dir / "qa").mkdir(parents=True)
        (topics_dir / "qa" / "qa-x.md").write_text(
            "---\nqa: qa-x\n---\n\n- 依据 [@bad2024, p. 12]\n", encoding="utf-8")
        assert L.cited_by_qa_page(topics_dir) == {"qa-x": ["bad2024"]}

    def test_retraction_section_lists_qa_pages_under_their_own_line(self):
        text = _render(retraction=_hit_scan(),
                       cited_by_page={"mnar-diagnosis": ["bad2024"]},
                       cited_by_qa_page={"qa-x": ["bad2024"], "qa-y": ["ok2024"]})
        block = _block(text, "撤稿检查")
        assert "已被概念页引用" in block and "mnar-diagnosis" in block
        assert "已被问答页引用" in block and "qa-x" in block
        assert "qa-y" not in block             # qa-y 引的是另一篇，不该被点名

    def test_not_cited_anywhere_only_when_both_maps_are_empty(self):
        with_qa = _render(retraction=_hit_scan(), cited_by_page={},
                          cited_by_qa_page={"qa-x": ["bad2024"]})
        assert "未被任何概念页或问答页引用" not in _block(with_qa, "撤稿检查")

        neither = _render(retraction=_hit_scan(), cited_by_page={}, cited_by_qa_page={})
        assert "未被任何概念页或问答页引用" in _block(neither, "撤稿检查")

    def test_acknowledged_hit_does_not_force_the_not_cited_line(self):
        """已标记撤稿的命中即使两个映射都为空，也不该硬凑一句"未被引用"——
        原有语义（`elif not h.acknowledged`）必须原样保留。"""
        scan = L.RetractionScan(n_papers=1, n_with_doi=1, n_resolved=1, hits=[
            L.RetractionHit(citekey="bad2024", doi="10.1/x", title="x",
                            signal="openalex-flag", acknowledged=True)])
        block = _block(_render(retraction=scan, cited_by_page={}, cited_by_qa_page={}),
                      "撤稿检查")
        assert "未被任何概念页或问答页引用" not in block

    def test_reask_command_hint_is_printed_for_qa_pages(self):
        text = _render(retraction=_hit_scan(), cited_by_qa_page={"qa-x": ["bad2024"]})
        block = _block(text, "撤稿检查")
        assert "ask_notes.py --reask qa-x" in block

    def test_carried_retraction_banner_mentions_both_page_kinds(self):
        """N8 的延伸：结转来的两行「已被概念页/问答页引用」都是快照，两句提示
        必须一起出现，不能改完之后 banner 只提了概念页那一半。"""
        prev = _render(retraction=_hit_scan(), now=datetime(2026, 8, 1, 9, 0, 0),
                       cited_by_page={"mnar-diagnosis": ["bad2024"]},
                       cited_by_qa_page={"qa-x": ["bad2024"]})
        cur = _render(retraction=L.RetractionScan(skipped=True),
                      now=datetime(2026, 8, 17, 9, 0, 0),
                      previous=L.split_lint_sections(prev))
        block = _block(cur, "撤稿检查")
        assert "已被概念页引用" in block and "已被问答页引用" in block
        assert "快照" in block

    def test_lint_checks_cited_by_page_semantics_unchanged(self, tmp_path):
        """不改变 cited_by_page 的现有语义：qa/ 子目录仍然不计入它的结果
        （它现有的唯一消费方——撤稿节的「已被概念页引用」——不该被这次改动悄悄
        扩大成"概念页∪问答页"）。"""
        topics_dir = tmp_path / "topics"
        (topics_dir / "qa").mkdir(parents=True)
        (topics_dir / "p.md").write_text(
            "---\ntopic: demo\n---\n\n- 论断 [@bad2024]\n", encoding="utf-8")
        (topics_dir / "qa" / "qa-x.md").write_text(
            "---\nqa: qa-x\n---\n\n- 依据 [@bad2024]\n", encoding="utf-8")
        assert L.cited_by_page(topics_dir) == {"demo": ["bad2024"]}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
