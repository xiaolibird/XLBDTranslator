# -*- coding: utf-8 -*-
"""ask_notes v3 第二阶段（C 理论模式召回扩展 / D 角色过滤可见化）的单元测试。

设计见 docs/decisions/ask_notes_v3_design_2026-10-04.md。全部用假向量库/假 embed/临时目录，
不联网、不调 LLM、不碰 output/。
"""
import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scholar import qa as Q                      # noqa: E402
from src.scholar import topics as T                  # noqa: E402
from src.scholar.embed_store import VectorStore      # noqa: E402

_spec = importlib.util.spec_from_file_location("ask_notes_script_v3r", ROOT / "scripts" / "ask_notes.py")
AN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AN)

THM = "Theorem 4.3: coverage holds at level 1-2alpha under MCAR."
PLAIN = "The empirical coverage is close to nominal on synthetic data."


class FakeEmbed:
    """问题向量恒为 [1, 0]；记录调用次数。"""

    def __init__(self):
        self.calls = 0

    def embed(self, texts, *a, **kw):
        self.calls += 1
        return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)


def _store(rows):
    """rows: (citekey, text, sim, role)；chunk 向量 [sim, sqrt(1-sim^2)] 使余弦恰为 sim。"""
    recs, vecs = [], []
    for i, (ck, text, sim, role) in enumerate(rows):
        recs.append({"id": "c{}".format(i), "citekey": ck, "text": text, "role": role,
                     "section": "关键结论", "note_file": "{}.md".format(ck), "note_line": 10 + i,
                     "level": "highlight", "year": 2024, "tier": None, "bucket": [],
                     "pages": None, "chapter": None})
        vecs.append([sim, math.sqrt(max(0.0, 1 - sim * sim))])
    return VectorStore({"model": "fake", "dim": "2"}, recs, np.array(vecs, dtype=np.float32))


def _spec_q(question, roles=(), max_evidence=28):
    return Q.build_qa_spec(question, roles=list(roles), max_evidence=max_evidence)


class TestTrigger:
    def test_plain_question_not_triggered(self):
        assert not Q.theory_triggered("不同医院的缺失率差异有多大？")

    def test_keyword_in_question_or_q(self):
        assert Q.theory_triggered("覆盖保证能做到什么程度？")
        assert Q.theory_triggered("缺失率差异", ["Theorem coverage"])

    def test_explicit_override(self):
        assert Q.theory_triggered("缺失率差异", theory=True)
        assert not Q.theory_triggered("覆盖保证能做到什么程度？", theory=False)


class TestTheoryBackfill:
    def _rows(self):
        return [("zaffran", PLAIN, 0.70, "citable"),                       # 正常召回命中
                ("zaffran", THM + " (a)", 0.50, "citable"),
                ("zaffran", THM + " (b)", 0.49, "citable"),
                ("zaffran", THM + " (c)", 0.48, "citable"),
                ("zaffran", THM + " (d)", 0.47, "citable"),                # 超每篇 3 句上限
                ("zaffran", THM + " (low)", 0.40, "citable"),              # 低于 0.45
                ("zaffran", "没有任何关键词的句子 cover", 0.52, "citable"),  # 非理论句
                ("other", PLAIN, 0.60, "citable")]

    def test_not_triggered_without_keyword(self):
        r = Q.recall_qa_evidence(_spec_q("两个医院的缺失率差异有多大"), _store(self._rows()),
                                 FakeEmbed())
        assert not r.theory_mode and r.theory_backfilled == 0
        assert not any(getattr(e, "theory_backfill", False) for e in r.evidences)

    def test_cap_threshold_marker_and_anchor(self):
        r = Q.recall_qa_evidence(_spec_q("覆盖保证能做到什么程度"), _store(self._rows()),
                                 FakeEmbed())
        bf = [e for e in r.evidences if getattr(e, "theory_backfill", False)]
        assert r.theory_mode and r.theory_backfilled == 3 and len(bf) == 3
        texts = {e.text for e in bf}
        assert texts == {THM + " (a)", THM + " (b)", THM + " (c)"}     # 前 3 高分，且 (d)/(low) 不在
        for e in bf:
            assert e.note_file == "zaffran.md" and e.note_line is not None
        assert r.theory_papers == 1      # 只有 zaffran 贡献了补捞句

    def test_threshold_is_effective(self):
        r = Q.recall_qa_evidence(_spec_q("覆盖保证能做到什么程度"), _store(self._rows()),
                                 FakeEmbed(), theory_min_sim=0.495)
        assert r.theory_backfilled == 1
        r2 = Q.recall_qa_evidence(_spec_q("覆盖保证能做到什么程度"), _store(self._rows()),
                                  FakeEmbed(), theory_min_sim=0.39)
        assert r2.theory_backfilled == 3   # 阈值放到 0.39 后候选 5 句，仍受每篇 3 句上限

    def test_low_ranked_paper_still_backfilled(self):
        """目标论文论文级排名在第 10 名，但定理句分数 >= 阈值：必须被补进（不按论文排名截）。"""
        rows = [("p{}".format(i), PLAIN, 0.9 - 0.01 * i, "citable") for i in range(9)]
        rows += [("target", PLAIN, 0.56, "citable"), ("target", THM + " (t)", 0.50, "citable")]
        r = Q.recall_qa_evidence(_spec_q("覆盖保证"), _store(rows), FakeEmbed())
        bf = {e.citekey for e in r.evidences if getattr(e, "theory_backfill", False)}
        assert "target" in bf

    def test_global_cap_and_ranking(self):
        rows = [("p{}".format(i), PLAIN, 0.9 - 0.01 * i, "citable") for i in range(9)]
        rows += [("p{}".format(i), THM + " (x)", 0.46 + 0.01 * i, "citable") for i in range(9)]
        r = Q.recall_qa_evidence(_spec_q("覆盖保证"), _store(rows), FakeEmbed(),
                                 theory_max_backfill=4)
        bf = sorted((e for e in r.evidences if getattr(e, "theory_backfill", False)),
                    key=lambda e: -e.score)
        assert r.theory_backfilled == 4 and len(bf) == 4
        assert {e.citekey for e in bf} == {"p8", "p7", "p6", "p5"}   # 句分最高的 4 条
        assert r.theory_papers == 4                                   # 实际贡献的论文数
        r2 = Q.recall_qa_evidence(_spec_q("覆盖保证"), _store(rows), FakeEmbed())
        assert r2.theory_backfilled == 9 and r2.theory_papers == 9    # 默认上限 12 未触顶

    def test_default_max_is_12(self):
        assert Q.DEFAULT_THEORY_MAX_BACKFILL == 12
        rows = [("p{}".format(i), PLAIN, 0.9 - 0.01 * i, "citable") for i in range(15)]
        rows += [("p{}".format(i), THM, 0.5, "citable") for i in range(15)]
        r = Q.recall_qa_evidence(_spec_q("覆盖保证"), _store(rows), FakeEmbed())
        assert r.theory_backfilled == 12

    def test_backfilled_survive_caps(self):
        """per_paper_cap=1 且 max_evidence=1 时，补捞句也必须进入最终证据。"""
        r = Q.recall_qa_evidence(_spec_q("覆盖保证能做到什么程度", max_evidence=1),
                                 _store(self._rows()), FakeEmbed(), per_paper_cap=1)
        bf = [e for e in r.evidences if getattr(e, "theory_backfill", False)]
        assert len(bf) == 3
        assert [e.ref for e in r.evidences] == ["E{}".format(i + 1) for i in range(len(r.evidences))]

    def test_respects_roles(self):
        rows = self._rows() + [("zaffran", THM + " (ref)", 0.6, "refutable")]
        r = Q.recall_qa_evidence(_spec_q("覆盖保证", roles=["citable"]), _store(rows), FakeEmbed())
        assert all(e.role == "citable" for e in r.evidences)

    def test_marker_rendered_in_table(self):
        r = Q.recall_qa_evidence(_spec_q("覆盖保证能做到什么程度"), _store(self._rows()),
                                 FakeEmbed())
        qa = {"answer": "", "points": [], "caveats": [], "gaps": []}
        md = Q.render_qa_block("覆盖保证能做到什么程度", qa, r.evidences)
        assert md.count(Q.THEORY_BACKFILL_TAG) == 3


class TestRoleFiltered:
    def _rows(self):
        return [("a", "可引：缺失率 46.42%", 0.8, "citable"),
                ("b", "可反驳：PaCO2 46.42% vs 69.01%", 0.75, "refutable"),
                ("c", "可反驳：没有数字的观点", 0.7, "refutable"),
                ("d", "方法：GCS 0.46% 对 27.57%", 0.65, "method"),
                ("e", "低于阈值但是 123", 0.3, "refutable")]

    def test_counts_with_roles(self):
        emb = FakeEmbed()
        r = Q.recall_qa_evidence(_spec_q("缺失率差异", roles=["citable"]), _store(self._rows()),
                                 emb)
        assert r.roles_counted and r.role_filtered_out == 3 and r.role_filtered_numeric == 2
        assert [e.citekey for e in r.evidences] == ["a"]
        assert emb.calls == 1          # 两次宽召回共用同一次 embed

    def test_no_roles_no_stats(self):
        r = Q.recall_qa_evidence(_spec_q("缺失率差异"), _store(self._rows()), FakeEmbed())
        assert not r.roles_counted and r.role_filtered_out == 0 and r.role_filtered_numeric == 0

    def test_legacy_retrieve_unchanged(self):
        picked, _near = Q.retrieve_qa_evidence(_spec_q("缺失率差异", roles=["citable"]),
                                               _store(self._rows()), FakeEmbed())
        assert [e.citekey for e in picked] == ["a"]


class TestFrontmatterAndCli:
    def test_frontmatter_fields(self, tmp_path):
        ev = [T.Evidence(ref="E1", citekey="a", text=PLAIN, role="citable", section="s",
                         note_file="a.md", note_line=1, score=0.9)]
        qa, rep = Q.validate_qa({"answer": "x", "points": [{"text": "经验覆盖率接近目标水平。",
                                                           "evidence": ["E1"]}],
                                 "caveats": [], "gaps": []}, ev)
        path, _ = Q.write_qa_page(tmp_path / "qa", "qa-x", "问", qa, ev, rep,
                                  recall_stats={"theory_mode": True, "theory_backfilled": 4,
                                                "role_filtered_out": 12,
                                                "role_filtered_numeric": 5})
        fm, _b = Q.split_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["theory_mode"] is True and fm["theory_backfilled"] == 4
        assert fm["role_filtered_out"] == 12 and fm["role_filtered_numeric"] == 5

    def test_help_lists_flags(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["ask_notes.py", "--help"])
        with pytest.raises(SystemExit):
            AN.main()
        out = capsys.readouterr().out
        assert ("--theory" in out and "--no-theory" in out and "--theory-min-sim" in out
                and "--theory-max" in out)

    def _dry(self, tmp_path, question, roles, rows, **kw):
        embed = FakeEmbed()
        spec_extra = dict(extra_queries=[], roles=roles, min_sim=0.55, max_evidence=28,
                          per_paper_cap=3)
        return AN._run_one(question, "qa-t", qa_dir=tmp_path, index={"papers": []}, titles={},
                           topic_specs=[], template="", store=_store(rows), embed_client=embed,
                           settings=None, topic_vecs=None, model=None, dry_run=True,
                           no_dedup_check=True, no_topic_check=True, **spec_extra, **kw)

    def test_prints_theory_and_role_lines(self, tmp_path, capsys):
        rows = [("a", PLAIN, 0.7, "citable"), ("a", THM, 0.5, "citable"),
                ("b", "反驳 46.42%", 0.7, "refutable")]
        self._dry(tmp_path, "覆盖保证能做到什么程度", ["citable"], rows)
        out = capsys.readouterr().out
        assert "📐 理论模式：从 1 篇补捞 1 条定理/命题句（阈值 0.45）" in out
        assert "⚠️ 角色过滤（citable）挡掉 1 条高分证据，其中 1 条含数字；问数字/事实时考虑 --role all" in out

    def test_silent_without_roles_or_theory(self, tmp_path, capsys):
        self._dry(tmp_path, "缺失率差异", [], [("a", PLAIN, 0.7, "citable")])
        out = capsys.readouterr().out
        assert "📐" not in out and "角色过滤" not in out


class TestStatementRegex:
    @pytest.mark.parametrize("t", ["Theorem 2.3 says", "see Prop. 2.6", "Prop 2.6", "定理1 给出",
                                   "命题 2.8", "Thm 4.3 holds", "Theorems 4.2", "Lemma 1", "假设 1"])
    def test_positive(self, t):
        assert Q._THEORY_STATEMENT_RE.search(t)

    @pytest.mark.parametrize("t", ["一条从未被声明的隐含假设", "保证是边缘意义上的", "Figure 3",
                                   "Theorem without number", "实验增益极小"])
    def test_negative(self, t):
        assert not Q._THEORY_STATEMENT_RE.search(t)

    def test_backfill_only_numbered(self):
        broad = [("a", "一条从未被声明的隐含假设：保证无偏", 0.60, "citable"),
                 ("a", PLAIN, 0.70, "citable"),
                 ("a", "Theorem 2.3: hardness under MNAR", 0.47, "citable")]
        r = Q.recall_qa_evidence(_spec_q("覆盖保证能做到什么程度"), _store(broad), FakeEmbed())
        bf = [e.text for e in r.evidences if getattr(e, "theory_backfill", False)]
        assert bf == ["Theorem 2.3: hardness under MNAR"]
