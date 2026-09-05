# -*- coding: utf-8 -*-
"""改键的告警面与修复面必须同源，且修复动作不许把页面打成 conflict。

第一版修法被对抗审核打回，两条 BLOCKER 都记在这里当回归：
  1. 裸替换生成块内的文本而不重算 BEGIN 行的 `h=`，会让 topics/qa/_lint 三条重建链
     一律判 tampered → **拒绝覆盖 → 页面永久冻结**。比原缺陷严重一个量级。
  2. 撞键消歧路径的 pair 是 `(基键 → 后缀键)`，而最早月的 keeper **仍持基键**
     （notes_index.py `for e in group[1:]`）。无条件把 `[@基键]` 改指 duplicate 就是猜，
     猜错的那半会静默指向另一篇论文。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import notes_index as ni                     # noqa: E402
from src.scholar.vault import (GEN_BEGIN, GEN_END,            # noqa: E402
                               VAULT_SCHEMA_VERSION, _gen_hash,
                               generated_block_tampered)


def _page_with_gen_block(body: str) -> str:
    b = body.rstrip("\n")
    return "---\ntitle: x\n---\n\n{}\n{}\n{}\n\n## 我的批注\n".format(
        GEN_BEGIN.format(VAULT_SCHEMA_VERSION, _gen_hash(b)), b, GEN_END)


def _mk(tmp_path):
    t = tmp_path / "topics"
    (t / "qa").mkdir(parents=True)
    return t


# ---------------- BLOCKER 1 回归：哈希必须重算 ----------------

def test_rename_rehashes_the_generated_block_so_pages_do_not_freeze(tmp_path):
    """核心回归：改完之后 `generated_block_tampered` 必须仍是 False。
    否则 build_topics / lint / ask_notes 三条重建链会一律判 conflict、拒绝覆盖，页面冻结。"""
    t = _mk(tmp_path)
    page = t / "mnar.md"
    page.write_text(_page_with_gen_block("- 结论 [@lin2025Addressingb]"), encoding="utf-8")
    assert generated_block_tampered(page.read_text(encoding="utf-8")) is False, "前提：改之前是干净的"

    got = ni.rename_citekeys_in_topic_pages(
        tmp_path, [("lin2025Addressingb", "lin2025Addressing")], live_citekeys={"lin2025Addressing"},
        apply=True)
    assert got["applied"] is True and len(got["pages"]) == 1
    txt = page.read_text(encoding="utf-8")
    assert "[@lin2025Addressing]" in txt and "lin2025Addressingb" not in txt
    assert generated_block_tampered(txt) is False, \
        "改完哈希必须重算——不然页面被永久冻结，比原缺陷严重得多"


def test_pages_without_sentinel_are_left_structurally_alone(tmp_path):
    """没有哨兵的页（纯生成或纯手写）照改，不许凭空插入 BEGIN 行。"""
    t = _mk(tmp_path)
    page = t / "plain.md"
    page.write_text("- 结论 [@aOld]\n", encoding="utf-8")
    ni.rename_citekeys_in_topic_pages(tmp_path, [("aOld", "aNew")], live_citekeys=set(), apply=True)
    txt = page.read_text(encoding="utf-8")
    assert txt == "- 结论 [@aNew]\n" and "BEGIN GENERATED" not in txt


# ---------------- BLOCKER 2 回归：撞键路径不许自动改 ----------------

def test_keys_still_held_by_a_keeper_are_never_auto_renamed(tmp_path):
    """撞键消歧：keeper 仍持基键，页面上的 `[@基键]` 指谁无从判断——工具不许猜。"""
    t = _mk(tmp_path)
    page = t / "p.md"
    page.write_text("- 证据 [@li2021Missing]\n", encoding="utf-8")
    got = ni.rename_citekeys_in_topic_pages(
        tmp_path, [("li2021Missing", "li2021Missingb")],
        live_citekeys={"li2021Missing", "li2021Missingb"}, apply=True)
    assert got["pages"] == [] and got["skipped_live"] == ["li2021Missing"]
    assert page.read_text(encoding="utf-8") == "- 证据 [@li2021Missing]\n", "一个字节都不许动"


def test_retired_rekey_pairs_splits_the_two_directions():
    """两条改键路方向相反，判据要能分开。"""
    # 陈旧行内键：后缀键 → 基键，旧键真的没了 → 可改
    assert ni.retired_rekey_pairs([("aB", "a")], {"a"}) == [("aB", "a")]
    # 撞键消歧：基键 → 后缀键，基键仍在 → 不可改
    assert ni.retired_rekey_pairs([("a", "aB")], {"a", "aB"}) == []
    assert ni.retired_rekey_pairs([("a", "a")], set()) == []


# ---------------- 作用面与判据同源 ----------------

def test_rename_covers_pages_semantic_routing_would_miss_including_qa(tmp_path):
    """一页引了旧键但该论文早已挤出它的证据集——重合成路由看不见它；
    `topics/qa/` 更是根本没有重合成通路。改名必须覆盖两者。"""
    t = _mk(tmp_path)
    (t / "in-evidence.md").write_text("- 结论一 [@kOld]\n", encoding="utf-8")
    (t / "out-of-evidence.md").write_text("- 已被 max_evidence 挤出 [@kOld]\n", encoding="utf-8")
    (t / "qa" / "qa-x.md").write_text("答：见 [@kOld]\n", encoding="utf-8")
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")],
                                            live_citekeys={"kNew"}, apply=True)
    assert {d["page"] for d in got["pages"]} == {
        "in-evidence.md", "out-of-evidence.md", "qa/qa-x.md"}


def test_lint_page_orphan_ids_respect_word_boundaries(tmp_path):
    """_lint.md 的孤儿 ID 是裸 citekey，要改；但近亲键（前缀关系）不许误伤。"""
    t = _mk(tmp_path)
    (t / "_lint.md").write_text("覆盖率缺口：smith2024, smith2024a, smith2024b\n", encoding="utf-8")
    ni.rename_citekeys_in_topic_pages(tmp_path, [("smith2024", "smith2024Base")],
                                      live_citekeys={"smith2024Base"}, apply=True)
    txt = (t / "_lint.md").read_text(encoding="utf-8")
    assert "smith2024Base," in txt and "smith2024a" in txt and "smith2024b" in txt
    assert "smith2024Basea" not in txt


def test_qa_bare_backtick_keys_are_not_citations(tmp_path):
    """qa 页「未纳入的近邻论文」的裸反引号键是刻意的非引用形态，扫描不扫它，改名也不许动。"""
    t = _mk(tmp_path)
    (t / "qa" / "qa-y.md").write_text("未纳入：`kOld`（相似度不足）\n", encoding="utf-8")
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")],
                                            live_citekeys={"kNew"}, apply=True)
    assert got["pages"] == []


def test_scan_and_rename_share_one_predicate():
    assert ni._page_cites_old_key("见 [@a2024X] 一节", "a2024X", False) is True
    assert ni._page_cites_old_key("裸键 a2024X 不是引用", "a2024X", False) is False
    assert ni._page_cites_old_key("孤儿 ID a2024X", "a2024X", True) is True
    assert ni._page_cites_old_key("近亲 a2024Xb", "a2024X", True) is False


# ---------------- 容错与纯净性 ----------------

def test_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """一页写不进去不许掀翻整个收尾流程（调用方后面还要重刷索引、同步向量库）。"""
    t = _mk(tmp_path)
    (t / "a.md").write_text("[@kOld]\n", encoding="utf-8")
    (t / "b.md").write_text("[@kOld]\n", encoding="utf-8")

    def boom(path, content):
        if path.name == "a.md":
            raise PermissionError("read-only")
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(ni, "_atomic_write", boom)
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")],
                                            live_citekeys={"kNew"}, apply=True)
    assert [e["page"] for e in got["errors"]] == ["a.md"]
    assert got["applied"] is False, "有失败就不许报告成完成"
    assert "[@kNew]" in (t / "b.md").read_text(encoding="utf-8"), "别的页照常改"


def test_dry_run_writes_nothing(tmp_path):
    t = _mk(tmp_path)
    page = t / "a.md"
    page.write_text("[@kOld]\n", encoding="utf-8")
    before = page.read_bytes()
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")],
                                            live_citekeys={"kNew"}, apply=False)
    assert got["pages"] and got["applied"] is False
    assert page.read_bytes() == before


def test_idempotent_and_noop_cases(tmp_path):
    t = _mk(tmp_path)
    page = t / "a.md"
    page.write_text("[@kOld]\n", encoding="utf-8")
    ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], {"kNew"}, apply=True)
    after = page.read_bytes()
    got2 = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], {"kNew"}, apply=True)
    assert got2["pages"] == [] and page.read_bytes() == after
    assert ni.rename_citekeys_in_topic_pages(tmp_path, [], set(), apply=True)["pages"] == []


# ---------------- 接线层：announce 不许再自动改 ----------------

def test_announce_no_longer_mutates_topics_and_points_at_a_working_command(tmp_path):
    """接线回归：`announce_rekey_side_effects` 恢复成**只读**（它此前一版会就地改名，
    把生成块哈希打脏）。它必须给出**真能修文本**的那条命令，而不只是重合成那条。"""
    topics = tmp_path / "topics"
    topics.mkdir(parents=True)
    page = topics / "p.md"
    page.write_text(_page_with_gen_block("- 结论 [@kOld]"), encoding="utf-8")
    before = page.read_bytes()
    entry = {"citekey": "x", "month": "2026-08", "note_file": "科研札记_2026-08_全文精读.md"}
    out = ni.announce_rekey_side_effects(tmp_path, [entry], rekey_map=[("kOld", "kNew")])
    assert page.read_bytes() == before, "announce 必须只读——改名是另一条显式命令的事"
    assert out["topics_pages"] and out["topics_pages"][0]["page"] == "p.md"
    assert "--fix-topic-citekeys" in (out.get("topics_fix_hint") or "")
    assert "build_topics" in (out.get("topics_refresh_hint") or "")
