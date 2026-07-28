# -*- coding: utf-8 -*-
"""read_pdf CLI 的两处「防人祸」输出，以及相对路径的仓库根锚定。

这两件事都不是逻辑正确性问题，是**信息可见性**问题——今天各栽了一次：
  · 只读到 31 页 PDF 的第 12 页就断言草稿引用的附录表格是编造的（其实每个数都对）；
  · 21 篇一批 ingest，「索引里已有同文」的提示夹在第 3 篇的输出中间，被后面 18 篇刷没了，
    结果重读了一篇 2026-06 已精读过的论文。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load():
    spec = importlib.util.spec_from_file_location("read_pdf_cli", REPO / "scripts" / "read_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


# ---------------- 亲读窗口提示 ----------------

def test_read_plan_states_total_and_last_window():
    s = M._read_plan(31)
    assert "31 页" in s and "1-20" in s and "21-31" in s


def test_read_plan_unknown_pages_says_so_loudly():
    """页数读不出时不能装作没事——必须明说未知，否则等于默认「读了开头就够」。"""
    s = M._read_plan(None)
    assert "未知" in s and "1-20" not in s


# ---------------- 末尾汇总块 ----------------

def _row(**kw):
    base = {"title": "Some Paper Title", "duplicate": None, "meta_source": "crossref-doi",
            "authors_n": 3, "skipped": None}
    base.update(kw)
    return base


def test_attention_block_surfaces_duplicate_at_the_end(capsys):
    outs = [_row(title="P{}".format(i)) for i in range(20)]
    outs.insert(3, _row(title="Already Read Paper", duplicate={
        "note_file": "科研札记_2026-06_手动精读.md", "month": "2026-06", "citekey": "old2026Key"}))
    M._print_attention(outs, [])
    out = capsys.readouterr().out
    assert "需要注意（1 项）" in out
    assert "Already Read Paper" in out and "old2026Key" in out
    # 汇总块必须是最后一段，不能又被后续输出盖掉
    assert out.strip().splitlines()[-1].lstrip().startswith("→")


def test_attention_block_flags_thin_metadata():
    """pdf-only / 零作者 → citekey 退化成 anon*，bibliography 缺卷期页。库里已积了 95 个 anon*。"""
    outs = [_row(title="No Authors", meta_source="pdf-only", authors_n=0)]
    M._print_attention(outs, [])


@pytest.mark.parametrize("row,expect", [
    (_row(meta_source="pdf-only", authors_n=0), True),
    (_row(meta_source="pdf-llm", authors_n=0), True),      # 有来源但没作者，一样是 anon*
    (_row(meta_source="crossref-doi", authors_n=5), False),
    (_row(meta_source="pdf-only", authors_n=0, skipped="final"), False),  # 跳过的不重复提醒
])
def test_thin_metadata_predicate(row, expect, capsys):
    M._print_attention([row], [])
    assert ("元数据不全" in capsys.readouterr().out) is expect


def test_attention_block_silent_when_nothing_to_report(capsys):
    """没事就别印——每批都刷一个空的「需要注意」框，几次之后就没人看了。"""
    M._print_attention([_row(), _row()], [])
    assert capsys.readouterr().out == ""


def test_attention_block_lists_failures(capsys):
    M._print_attention([], [("broken.pdf", "PDF 抽不出文本（可能是扫描件/加密）")])
    out = capsys.readouterr().out
    assert "broken.pdf" in out and "需要注意（1 项）" in out


# ---------------- 仓库根锚定 ----------------

def test_anchor_pins_relative_path_to_repo_root(tmp_path, monkeypatch):
    """回归：cwd 漂了也不能改变 `output/scholar_notes` 指向何处。

    实际故障是 `output/scholar_notes` 解析成了 `output/scholar_notes/output/scholar_notes`——
    札记写进空目录、索引读回空，不报错、只丢数据。
    """
    from src.scholar.paths import repo_path, REPO_ROOT
    monkeypatch.chdir(tmp_path)
    assert repo_path("output/scholar_notes") == REPO_ROOT / "output" / "scholar_notes"


def test_anchor_leaves_absolute_paths_alone(tmp_path):
    from src.scholar.paths import repo_path
    assert repo_path(tmp_path) == tmp_path
    assert repo_path(str(tmp_path / "x")) == tmp_path / "x"


def test_anchor_expands_home():
    from src.scholar.paths import repo_path
    got = repo_path("~/Documents/ScholarVault")
    assert got.is_absolute() and "~" not in str(got)


def test_repo_root_is_the_repo():
    from src.scholar.paths import REPO_ROOT
    assert (REPO_ROOT / "src" / "scholar" / "pdf_ingest.py").exists()
    assert (REPO_ROOT / "scripts" / "read_pdf.py").exists()
