# -*- coding: utf-8 -*-
"""ask_notes v3 第一阶段（A1 数字接地徽标 / A2 理论类原句并排 / B 蕴含审计）的单元测试。

设计见 docs/decisions/ask_notes_v3_design_2026-10-04.md。全部用假 LLM / 临时目录，
不联网、不碰 output/。C、D 两层不在本阶段。
"""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scholar import qa as Q          # noqa: E402
from src.scholar import topics as T      # noqa: E402

_spec = importlib.util.spec_from_file_location("ask_notes_script_v3", ROOT / "scripts" / "ask_notes.py")
AN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AN)

THEOREM = "Theorem 4.3: under MCAR with Y independent of M given X, coverage holds at level 1-2alpha."
EXPER = "In Fig. 3a the empirical coverage of the method is close to the nominal level on synthetic data."
NUMBER = "在 MIMIC 上 AUROC 为 0.82，缺失率 46.42%。"


def _ev(ref, citekey, text, section="实验方法", role="citable"):
    return T.Evidence(ref=ref, citekey=citekey, text=text, role=role, section=section,
                      note_file="札记.md", note_line=7, score=0.9, year=2024,
                      title="论文 {}".format(citekey))


EVS = [_ev("E1", "zaffran2024Predictive", THEOREM),
       _ev("E2", "b2024Y", EXPER),
       _ev("E3", "c2024Z", NUMBER)]


def _validate(points, caveats=()):
    return Q.validate_qa({"answer": "总述", "points": points, "caveats": list(caveats),
                          "gaps": []}, EVS)


class FakeLLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def call(self, prompt, model=None, max_tokens=0, temperature=0.0, json_mode=False):
        self.calls.append({"prompt": prompt, "model": model})
        if self.exc:
            raise self.exc
        r = self.reply
        if callable(r):
            r = r(prompt)
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


TPL = Q.load_audit_template(ROOT / Q.DEFAULT_AUDIT_PROMPT)


def _audit_reply(*pairs):
    return {"audits": [{"id": i, "verdict": v, "reason": r} for i, v, r in pairs]}


def _theory_qa():
    """P1 理论类（引 E1）、P2 实证类（引 E2，无关键词）、C1 引 E3 的数字论断。"""
    qa, rep = _validate(
        [{"text": "该方法在 MCAR 下可证覆盖水平为 1-α。", "evidence": ["E1"]},
         {"text": "经验覆盖率接近目标水平。", "evidence": ["E2"], "kind": "实证"}],
        [{"text": "AUROC 为 0.82。", "evidence": ["E3"]}])
    return qa, rep


# ---------------------------------------------------------------------------
# A1
# ---------------------------------------------------------------------------

class TestA1UngroundedBadge:
    def test_ungrounded_number_listed_and_rendered(self):
        qa, _ = _validate([{"text": "AUROC 达到 0.91。", "evidence": ["E3"]}])
        assert qa["points"][0]["ungrounded"] == ["0.91"]
        out = Q.render_qa_block("问", qa, EVS)
        assert "⚠️ 数字未在所引证据中找到：0.91" in out

    def test_all_grounded_renders_no_badge(self):
        qa, _ = _validate([{"text": "AUROC 为 0.82。", "evidence": ["E3"]}])
        assert qa["points"][0]["ungrounded"] == []
        assert "数字未在所引证据中找到" not in Q.render_qa_block("问", qa, EVS)

    def test_report_accounting_unchanged(self):
        _qa, rep = _validate([{"text": "AUROC 达到 0.91。", "evidence": ["E3"]}])
        assert rep.ungrounded_numbers == 1 and rep.ungrounded_claims == 1

    def test_plain_claim_line_is_byte_identical_to_v2(self):
        qa, _ = _validate([{"text": "经验覆盖率接近目标水平。", "evidence": ["E2"]}])
        out = Q.render_qa_block("问", qa, EVS)
        assert "- 经验覆盖率接近目标水平。 [@b2024Y]\n" in out


# ---------------------------------------------------------------------------
# A2
# ---------------------------------------------------------------------------

class TestA2TheoryClaims:
    def test_keyword_fallback_without_kind(self):
        qa, _ = _validate([{"text": "覆盖水平为 1-α。", "evidence": ["E2"]}])
        assert qa["points"][0]["kind"] == "" and qa["points"][0]["theory"] is True

    def test_keyword_in_evidence_sentence_counts(self):
        qa, _ = _validate([{"text": "某种覆盖结论。", "evidence": ["E1"]}])
        assert qa["points"][0]["theory"] is True

    def test_llm_kind_theory_without_keywords(self):
        qa, _ = _validate([{"text": "某种结论。", "evidence": ["E2"], "kind": "理论"}])
        assert qa["points"][0]["theory"] is True

    def test_empirical_not_theory_and_invalid_kind_normalized(self):
        qa, _ = _validate([{"text": "经验覆盖率接近目标水平。", "evidence": ["E2"],
                            "kind": "胡写"}])
        assert qa["points"][0]["kind"] == "" and qa["points"][0]["theory"] is False

    def test_theory_renders_original_sentence_empirical_does_not(self):
        qa, _ = _theory_qa()
        out = Q.render_qa_block("问", qa, EVS)
        assert "📌 原句（理论类论断，请对比方向与限定条件）" in out
        assert "> - [@zaffran2024Predictive] " + THEOREM in out
        assert out.count("📌 原句") == 1            # 实证类不贴
        assert "> - [@b2024Y] " + EXPER not in out

    def test_quoted_original_not_counted_as_residual_ref(self, tmp_path):
        ev = [_ev("E1", "k2024A", "Theorem 1 (see E1 and e2 in Table E3): bound holds.")]
        qa, rep = Q.validate_qa({"points": [{"text": "上界成立。", "evidence": ["E1"]}],
                                 "caveats": [], "gaps": [], "answer": ""}, ev)
        qa_dir = tmp_path / "qa"
        Q.write_qa_page(qa_dir, "qa-x", "问", qa, ev, rep, now=datetime(2026, 10, 4))
        idx = {"papers": [{"citekey": "k2024A",
                           "highlights": [{"text": ev[0].text}]}]}
        (a,) = Q.audit_qa_pages(qa_dir, idx)
        assert a.residual_refs == [] and a.ok


# ---------------------------------------------------------------------------
# B
# ---------------------------------------------------------------------------

class TestBAudit:
    def test_three_verdicts_rendered(self):
        qa, rep = _theory_qa()
        llm = FakeLLM(_audit_reply(("P1", "不忠实", "水平 1-2α 被写成 1-α"),
                                   ("C1", "无法判断", "原句未给出 AUROC 条件")))
        info = Q.run_audit(llm, qa, EVS, TPL, question="问", mode="all", report=rep)
        assert info["status"] == "ok" and info["n_audited"] == 2
        assert info["n_unfaithful"] == 1 and info["n_uncertain"] == 1
        out = Q.render_qa_block("问", qa, EVS)
        assert "- ❌ 该方法在 MCAR 下可证覆盖水平为 1-α。" in out
        assert "> ❌ 审计：水平 1-2α 被写成 1-α" in out
        assert "> ❔ 审计：原句未给出 AUROC 条件" in out
        assert "> ⚠️ 本页有 1 条论断审计判为不忠实（已标 ❌），answer 可能受其影响。" in out
        # 忠实：不加标记（P2 没返回判定也不应有任何审计行）
        assert out.count("审计：") == 2

    def test_faithful_has_no_marker(self):
        qa, _ = _theory_qa()
        llm = FakeLLM(_audit_reply(("P1", "忠实", "一致")))
        Q.run_audit(llm, qa, EVS, TPL, mode="theory")
        out = Q.render_qa_block("问", qa, EVS)
        assert "❌" not in out and "❔" not in out

    def test_theory_mode_only_audits_theory_claims(self):
        qa, _ = _theory_qa()
        llm = FakeLLM(_audit_reply(("P1", "忠实", "一致")))
        info = Q.run_audit(llm, qa, EVS, TPL, mode="theory")
        p = llm.calls[0]["prompt"]
        assert "### [P1]" in p and "### [P2]" not in p and "### [C1]" not in p
        assert info["n_audited"] == 1

    def test_all_mode_audits_everything_and_model_passed(self):
        qa, _ = _theory_qa()
        llm = FakeLLM(_audit_reply(("P1", "忠实", "x"), ("P2", "忠实", "x"), ("C1", "忠实", "x")))
        info = Q.run_audit(llm, qa, EVS, TPL, mode="all", model="m-audit")
        assert info["n_audited"] == 3 and llm.calls[0]["model"] == "m-audit"
        # 审计 prompt 里不泄露 citekey
        assert "zaffran2024Predictive" not in llm.calls[0]["prompt"]

    def test_off_makes_no_call(self):
        qa, _ = _theory_qa()
        llm = FakeLLM(_audit_reply())
        info = Q.run_audit(llm, qa, EVS, TPL, mode="off")
        assert info["status"] == "off" and llm.calls == []

    def test_no_theory_claims_makes_no_call(self):
        qa, _ = _validate([{"text": "经验覆盖率接近目标水平。", "evidence": ["E2"]}])
        llm = FakeLLM(_audit_reply())
        info = Q.run_audit(llm, qa, EVS, TPL, mode="theory")
        assert info["status"] == "ok" and info["n_audited"] == 0 and llm.calls == []

    def test_strict_drops_unfaithful_and_lists_them(self):
        qa, rep = _theory_qa()
        llm = FakeLLM(_audit_reply(("P1", "不忠实", "水平不符")))
        info = Q.run_audit(llm, qa, EVS, TPL, mode="theory", strict=True, report=rep)
        assert [p["text"] for p in qa["points"]] == ["经验覆盖率接近目标水平。"]
        assert rep.audit_dropped == 1 and info["dropped"][0]["id"] == "P1"
        out = Q.render_qa_block("问", qa, EVS)
        assert "## 审计记录" in out and "该方法在 MCAR 下可证覆盖水平为 1-α。" in out
        assert "水平不符" in out and "`--strict` 已丢弃" in out

    def test_llm_exception_degrades_not_blocks(self):
        qa, _ = _theory_qa()
        info = Q.run_audit(FakeLLM(exc=RuntimeError("所有 provider 不可用")), qa, EVS, TPL,
                           mode="theory")
        assert info["status"] == "failed" and "不可用" in info["reason"]
        assert Q.is_archivable(qa) and len(qa["points"]) == 2
        out = Q.render_qa_block("问", qa, EVS)
        assert "本页未经蕴含审计（原因：" in out and "不可用" in out

    def test_unparseable_output_degrades_after_retry(self):
        qa, _ = _theory_qa()
        llm = FakeLLM("已完成审计，没有问题")
        info = Q.run_audit(llm, qa, EVS, TPL, mode="theory")
        assert info["status"] == "failed" and len(llm.calls) == 2
        assert "本页未经蕴含审计" in Q.render_qa_block("问", qa, EVS)

    def test_garbage_verdicts_degrade(self):
        qa, _ = _theory_qa()
        llm = FakeLLM({"audits": [{"id": "P1", "verdict": "也许", "reason": "x"}]})
        assert Q.run_audit(llm, qa, EVS, TPL, mode="theory")["status"] == "failed"

    def test_reason_is_sanitized(self):
        qa, _ = _theory_qa()
        llm = FakeLLM(_audit_reply(("P1", "不忠实", "E2 说的是 [@evil2020X] 实验")))
        Q.run_audit(llm, qa, EVS, TPL, mode="theory")
        reason = qa["points"][0]["audit"]["reason"]
        assert "E2" not in reason and "evil2020X" not in reason

    def test_prompt_contains_three_fewshots(self):
        txt = (ROOT / "config/prompts/qa_audit_prompt.md").read_text(encoding="utf-8")
        assert "Proposition 2.6" in txt                      # Prop 2.6 方向反转
        assert "图 3a" in txt and "1−2α" in txt               # Fig.3a 被写成理论保证
        assert "GCS" in txt and "0.46%" in txt and "27.57%" in txt   # 忠实例
        assert txt.count("`不忠实`") >= 2 and "`忠实`" in txt

    def test_synthesis_prompt_has_kind_and_theory_rule(self):
        txt = (ROOT / "config/prompts/qa_synthesis_prompt.md").read_text(encoding="utf-8")
        assert txt.count('"kind"') >= 2 and "1−2α" in txt and "1−α" in txt


# ---------------------------------------------------------------------------
# frontmatter / schema / --verify 兼容
# ---------------------------------------------------------------------------

class TestFrontmatterAndVerify:
    def test_schema_version_is_current(self):
        # v3 → v4：可信度加固 WO-5（设计 4.3.5 明文要求涨版本）；v3 的字段仍在 v4 里
        assert Q.QA_SCHEMA_VERSION == 4

    def test_frontmatter_new_fields(self, tmp_path):
        qa, rep = _theory_qa()
        qa["points"][0]["ungrounded"] = ["9.9"]
        Q.run_audit(FakeLLM(_audit_reply(("P1", "不忠实", "x"), ("C1", "无法判断", "y"))),
                    qa, EVS, TPL, mode="all", report=rep)
        qa_dir = tmp_path / "qa"
        path, status = Q.write_qa_page(qa_dir, "qa-x", "问", qa, EVS, rep,
                                       now=datetime(2026, 10, 4))
        assert status == "new"
        fm, _ = Q.split_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["qa_schema"] == Q.QA_SCHEMA_VERSION and fm["audit_status"] == "ok"
        assert fm["audited_claims"] == 2 and fm["unfaithful_claims"] == 1
        assert fm["uncertain_claims"] == 1 and fm["theory_claims"] == 1
        assert fm["ungrounded_claims_listed"] == 1

    def test_failed_status_recorded(self, tmp_path):
        qa, rep = _theory_qa()
        Q.run_audit(FakeLLM(exc=RuntimeError("x")), qa, EVS, TPL, mode="theory")
        path, _ = Q.write_qa_page(tmp_path / "qa", "qa-x", "问", qa, EVS, rep)
        fm, _b = Q.split_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["audit_status"] == "failed"

    def test_verify_passes_on_v2_page(self, tmp_path):
        qa, rep = Q.validate_qa({"points": [{"text": "经验覆盖率接近目标水平。",
                                             "evidence": ["E2"]}],
                                 "caveats": [], "gaps": [], "answer": "总述"}, EVS)
        qa_dir = tmp_path / "qa"
        path, _ = Q.write_qa_page(qa_dir, "qa-x", "问", qa, EVS, rep)
        txt = path.read_text(encoding="utf-8")
        # 退回 v2 形态：没有 audit 字段、哨兵与 qa_schema 是 2
        txt = "\n".join(ln for ln in txt.splitlines()
                        if not ln.split(":")[0] in (
                            "audit_status", "audited_claims", "unfaithful_claims",
                            "uncertain_claims", "theory_claims", "ungrounded_claims_listed"))
        txt = txt.replace("qa_schema: {}".format(Q.QA_SCHEMA_VERSION), "qa_schema: 2").replace(
            "GENERATED v{}".format(Q.QA_SCHEMA_VERSION), "GENERATED v2")
        path.write_text(txt, encoding="utf-8")
        idx = {"papers": [{"citekey": e.citekey, "highlights": [{"text": e.text}]}
                          for e in EVS]}
        (a,) = Q.audit_qa_pages(qa_dir, idx)
        assert a.ok and a.schema_version == 2 and a.outdated
        assert a.audit_status == "" and a.unaudited


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def test_help_lists_new_flags(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["ask_notes.py", "--help"])
        with pytest.raises(SystemExit):
            AN.main()
        out = capsys.readouterr().out
        assert "--audit {theory,all,off}" in out and "--audit-model" in out and "--strict" in out


# ---------------------------------------------------------------------------
# 上一版页面喂回 prompt 前要剥掉 v3 标注
# ---------------------------------------------------------------------------

_V3_PREV = """> ⚠️ 本页未经蕴含审计（原因：x）。理论类论断请自行对比下方原句。

> ⚠️ 本页有 1 条论断审计判为不忠实（已标 ❌），answer 可能受其影响。

> 总述一句。

## 依据

- ❌ 该方法可证覆盖水平为 1-α。 [@zaffran2024Predictive]
  > ⚠️ 数字未在所引证据中找到：9.9（回原句核对）
  > ❌ 审计：水平应为 1-2α
  > 📌 原句（理论类论断，请对比方向与限定条件）：
  > - [@zaffran2024Predictive] Theorem 4.3 ...
- 经验覆盖率接近目标水平。 [@b2024Y]

## 本页证据（1 条 · 1 篇）

- ● **E1** `[@b2024Y]` 实验
  > 证据原句保留

## 审计记录（`--strict` 丢弃的不忠实论断）

- ❌ 被丢的论断（P3）
  > 审计：理由
"""


class TestPreviousAnswerStrip:
    def test_strip_v3_elements(self):
        out = Q._strip_v3_annotations(_V3_PREV)
        for bad in ("数字未在所引证据", "📌", "Theorem 4.3", "审计记录", "被丢的论断",
                    "本页未经蕴含审计", "本页有 1 条论断审计", "- ❌ "):
            assert bad not in out
        assert "- 该方法可证覆盖水平为 1-α。 [@zaffran2024Predictive]（上一版审计判为不忠实：水平应为 1-2α）" in out
        assert "- 经验覆盖率接近目标水平。 [@b2024Y]" in out
        assert "> 总述一句。" in out
        assert "  > 证据原句保留" in out         # 证据表原样保留

    def test_v2_text_unchanged(self):
        v2 = ("> 总述\n\n## 依据\n\n- 论断 [@a2024X]\n\n## 本页证据（1 条 · 1 篇）\n\n"
              "- ● **E1** `[@a2024X]` 实验\n  > 原句\n")
        assert Q._strip_v3_annotations(v2) == v2

    def test_previous_answer_applies_strip(self, tmp_path):
        qa, rep = _theory_qa()
        Q.run_audit(FakeLLM(_audit_reply(("P1", "不忠实", "水平不符"))), qa, EVS, TPL,
                    mode="theory")
        Q.write_qa_page(tmp_path / "qa", "qa-x", "问", qa, EVS, rep)
        prev = Q.previous_answer(tmp_path / "qa", "qa-x", "问")
        assert "📌" not in prev and "- ❌ " not in prev and "上一版审计判为不忠实：水平不符" in prev

    def test_validate_qa_strips_leading_marks(self):
        qa, _ = _validate([{"text": "❌ 经验覆盖率接近目标水平。", "evidence": ["E2"]},
                           {"text": "⚠️ 📌 AUROC 为 0.82。", "evidence": ["E3"]}])
        assert [p["text"] for p in qa["points"]] == ["经验覆盖率接近目标水平。", "AUROC 为 0.82。"]
