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
        tmp_path, [("lin2025Addressingb", "lin2025Addressing")], apply=True)
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
    ni.rename_citekeys_in_topic_pages(tmp_path, [("aOld", "aNew")], apply=True)
    txt = page.read_text(encoding="utf-8")
    assert txt == "- 结论 [@aNew]\n" and "BEGIN GENERATED" not in txt


# ---------------- BLOCKER 2 回归：撞键路径不许自动改 ----------------

def test_collision_path_never_auto_renames_because_caller_does_not_declare_retired(tmp_path):
    """撞键消歧：keeper 仍持基键，页面上的 `[@基键]` 指谁无从判断——工具不许猜。

    判据**不能**从索引事后推断：两条路的旧键都还在 `papers[]` 里（压测 BLOCKER 2 实证，
    按索引猜会把 100% 的 pair 全滤掉、功能变成死的）。只有产生 pair 的操作知道方向，
    故由调用方用 `old_keys_retired` 声明，默认 False = 不改。
    """
    topics = tmp_path / "topics"
    topics.mkdir(parents=True)
    page = topics / "p.md"
    page.write_text("- 证据 [@li2021Missing]\n", encoding="utf-8")
    entry = {"citekey": "x", "month": "2026-08", "note_file": "科研札记_2026-08_全文精读.md"}
    out = ni.announce_rekey_side_effects(tmp_path, [entry],
                                         rekey_map=[("li2021Missing", "li2021Missingb")])
    assert out["topics_pages"], "仍要告警"
    assert out["topics_renamed"] == [], "但一个字节都不许改"
    assert page.read_text(encoding="utf-8") == "- 证据 [@li2021Missing]\n"


def test_chained_or_cyclic_rekey_maps_are_rejected(tmp_path):
    """`A→B` 与 `B→C` 同时出现时按顺序替换会把 `[@A]` 变成 `[@C]`，环还会让两条引用塌成一条
    且不可逆（压测实证）。当场拒绝，不许静默改错。"""
    _mk(tmp_path)
    import pytest
    with pytest.raises(ValueError, match="级联/环"):
        ni.rename_citekeys_in_topic_pages(tmp_path, [("A", "B"), ("B", "C")], apply=True)
    with pytest.raises(ValueError, match="级联/环"):
        ni.rename_citekeys_in_topic_pages(tmp_path, [("A", "B"), ("B", "A")], apply=True)
    with pytest.raises(ValueError, match="多个新键"):
        ni.rename_citekeys_in_topic_pages(tmp_path, [("A", "B"), ("A", "C")], apply=True)


def test_pages_already_tampered_are_skipped_not_rehashed(tmp_path):
    """本来就与 h= 对不上的页（有人手改过生成块）必须跳过——无条件重算会洗掉这个冲突信号，
    下次 build_topics 就直接覆盖掉人写的东西，而那正是哨兵存在的唯一理由（压测 M1）。"""
    t = _mk(tmp_path)
    page = t / "tampered.md"
    page.write_text(_page_with_gen_block("- 结论 [@kOld]").replace("[@kOld]", "[@kOld] 我手加的"),
                    encoding="utf-8")
    assert generated_block_tampered(page.read_text(encoding="utf-8")) is True, "前提：这页本就脏"
    before = page.read_bytes()
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], apply=True)
    assert got["pages"] == [] and got["skipped_tampered"] == ["tampered.md"]
    assert page.read_bytes() == before, "一个字节都不许动"


def test_new_key_containing_regex_backreference_is_not_interpreted(tmp_path):
    """_lint.md 走 re.sub，新键里的 `\\1` / `\\g<0>` 不许被当反向引用（压测 M4：
    裸字符串会让 re.error 直接逃出函数，或静默替换成整段匹配）。"""
    t = _mk(tmp_path)
    (t / "_lint.md").write_text("孤儿 kOld\n", encoding="utf-8")
    ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", r"a\1b")], apply=True)
    assert r"a\1b" in (t / "_lint.md").read_text(encoding="utf-8")


# ---------------- 作用面与判据同源 ----------------

def test_rename_covers_pages_semantic_routing_would_miss_including_qa(tmp_path):
    """一页引了旧键但该论文早已挤出它的证据集——重合成路由看不见它；
    `topics/qa/` 更是根本没有重合成通路。改名必须覆盖两者。"""
    t = _mk(tmp_path)
    (t / "in-evidence.md").write_text("- 结论一 [@kOld]\n", encoding="utf-8")
    (t / "out-of-evidence.md").write_text("- 已被 max_evidence 挤出 [@kOld]\n", encoding="utf-8")
    (t / "qa" / "qa-x.md").write_text("答：见 [@kOld]\n", encoding="utf-8")
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], apply=True)
    assert {d["page"] for d in got["pages"]} == {
        "in-evidence.md", "out-of-evidence.md", "qa/qa-x.md"}


def test_lint_page_orphan_ids_respect_word_boundaries(tmp_path):
    """_lint.md 的孤儿 ID 是裸 citekey，要改；但近亲键（前缀关系）不许误伤。"""
    t = _mk(tmp_path)
    (t / "_lint.md").write_text("覆盖率缺口：smith2024, smith2024a, smith2024b\n", encoding="utf-8")
    ni.rename_citekeys_in_topic_pages(tmp_path, [("smith2024", "smith2024Base")], apply=True)
    txt = (t / "_lint.md").read_text(encoding="utf-8")
    assert "smith2024Base," in txt and "smith2024a" in txt and "smith2024b" in txt
    assert "smith2024Basea" not in txt


def test_qa_bare_backtick_keys_are_not_citations(tmp_path):
    """qa 页「未纳入的近邻论文」的裸反引号键是刻意的非引用形态，扫描不扫它，改名也不许动。"""
    t = _mk(tmp_path)
    (t / "qa" / "qa-y.md").write_text("未纳入：`kOld`（相似度不足）\n", encoding="utf-8")
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], apply=True)
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
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], apply=True)
    assert [e["page"] for e in got["errors"]] == ["a.md"]
    assert got["applied"] is False, "有失败就不许报告成完成"
    assert "[@kNew]" in (t / "b.md").read_text(encoding="utf-8"), "别的页照常改"


def test_dry_run_writes_nothing(tmp_path):
    t = _mk(tmp_path)
    page = t / "a.md"
    page.write_text("[@kOld]\n", encoding="utf-8")
    before = page.read_bytes()
    got = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], apply=False)
    assert got["pages"] and got["applied"] is False
    assert page.read_bytes() == before


def test_idempotent_and_noop_cases(tmp_path):
    t = _mk(tmp_path)
    page = t / "a.md"
    page.write_text("[@kOld]\n", encoding="utf-8")
    ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], apply=True)
    after = page.read_bytes()
    got2 = ni.rename_citekeys_in_topic_pages(tmp_path, [("kOld", "kNew")], apply=True)
    assert got2["pages"] == [] and page.read_bytes() == after
    assert ni.rename_citekeys_in_topic_pages(tmp_path, [], apply=True)["pages"] == []


# ---------------- 接线层：announce 不许再自动改 ----------------

def test_announce_renames_only_when_caller_declares_keys_retired(tmp_path):
    """接线回归：`announce_rekey_side_effects` 恢复成**只读**（它此前一版会就地改名，
    把生成块哈希打脏）。它必须给出**真能修文本**的那条命令，而不只是重合成那条。"""
    topics = tmp_path / "topics"
    topics.mkdir(parents=True)
    page = topics / "p.md"
    page.write_text(_page_with_gen_block("- 结论 [@kOld]"), encoding="utf-8")
    before = page.read_bytes()
    entry = {"citekey": "x", "month": "2026-08", "note_file": "科研札记_2026-08_全文精读.md"}
    # 不声明 retired：只读、只告警
    out = ni.announce_rekey_side_effects(tmp_path, [entry], rekey_map=[("kOld", "kNew")])
    assert page.read_bytes() == before and out["topics_renamed"] == []
    assert out["topics_pages"] and out["topics_pages"][0]["page"] == "p.md"
    # 声明 retired（fix_inline_citekeys 那条路）：真的改，且改完不 tampered
    out2 = ni.announce_rekey_side_effects(tmp_path, [entry], rekey_map=[("kOld", "kNew")],
                                          old_keys_retired=True)
    txt = page.read_text(encoding="utf-8")
    assert out2["topics_renamed"] and "[@kNew]" in txt and "kOld" not in txt
    assert generated_block_tampered(txt) is False, "改完不许把页面打成 conflict"
