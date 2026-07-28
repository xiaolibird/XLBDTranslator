# -*- coding: utf-8 -*-
"""月度 CSL 重建工具（scripts/repair_references.py）的纯逻辑部分。

盯住两件会静默出错的事：
1. arXiv 的 id 嵌在 DOI 里（10.48550/arXiv.2606.11570）——不提取就永远走不到 arXiv 分支，
   四篇预印本会退回索引兜底、作者被 md 的 `et al.` 截断。
2. Crossref → CSL 的字段映射（pages→page、journal→container-title、publication_date→issued），
   映错一个字段，pandoc 渲染出的参考文献就缺信息且不报错。
"""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_refs_cli", REPO / "scripts" / "repair_references.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


# ---------------- arXiv id 从 DOI 提取 ----------------

@pytest.mark.parametrize("doi,want", [
    ("10.48550/arXiv.2606.11570", "2606.11570"),
    ("10.48550/arxiv.2504.08919", "2504.08919"),          # 大小写不敏感
    ("10.48550/ARXIV.2606.23741", "2606.23741"),
    ("10.48550/arXiv.2606.10593v2", "2606.10593"),        # 版本后缀不算进 id
    ("10.48550/arXiv.0704.0001", "0704.0001"),            # 2015 前的 4 位序号
])
def test_arxiv_id_extracted_from_doi(doi, want):
    m = M.ARXIV_IN_DOI.match(doi)
    assert m and m.group(1) == want


@pytest.mark.parametrize("doi", [
    "10.48550/arXiv.2606.105938",     # 6 位序号不存在——宁可不匹配也不能截断成 2606.10593
    "10.48550/arXiv.2606.1059.7",
])
def test_malformed_arxiv_doi_not_silently_truncated(doi):
    """回归：无边界保护时会截出一个看似合法的 id，拿去查 arXiv 会静默取回错的论文。"""
    assert M.ARXIV_IN_DOI.match(doi) is None


@pytest.mark.parametrize("doi", [
    "10.1038/s41746-026-02861-6",     # 期刊 DOI 不能被误判成 arXiv
    "10.1109/icaisisas68969.2026.11567674",
    "10.48550/something-else",
    "", None,
])
def test_non_arxiv_doi_not_matched(doi):
    assert M.ARXIV_IN_DOI.match(doi or "") is None


# ---------------- Crossref → CSL 字段映射 ----------------

ENTRY = {"citekey": "gargi2026Pretransition", "title": "索引里的标题", "year": 2026}


def test_crossref_field_mapping():
    """pages→page、journal→container-title、publication_date→issued 三处最易映错。"""
    item = {
        "title": "Pre-transition nutrition dose and mortality",
        "authors": ["Yonatan Gargi", "Neriya Levran", "Dorit Stein"],
        "journal": "Clinical Nutrition ESPEN",
        "doi": "10.1016/j.clnesp.2026.103431",
        "url": "https://doi.org/10.1016/j.clnesp.2026.103431",
        "volume": "75", "issue": None, "pages": "103431",
        "publication_date": date(2026, 6, 12),
    }
    csl = M._csl_from_crossref(item, ENTRY)
    assert csl["id"] == "gargi2026Pretransition"
    assert csl["type"] == "article-journal"
    assert csl["title"] == "Pre-transition nutrition dose and mortality"   # 用 Crossref 的标题
    assert csl["container-title"] == "Clinical Nutrition ESPEN"
    assert csl["DOI"] == "10.1016/j.clnesp.2026.103431"
    assert csl["volume"] == "75"
    assert csl["page"] == "103431"          # 不是 "pages"
    assert "issue" not in csl               # None 不该落进 CSL
    assert csl["issued"] == {"date-parts": [[2026, 6, 12]]}


def test_author_name_splitting():
    """Crossref 给的是 'Given Family' 字符串，须拆成 CSL 的 family/given。"""
    csl = M._csl_from_crossref(
        {"authors": ["Giulia Fiscon", "Paola Paci", "Prince"]}, ENTRY)
    assert csl["author"] == [
        {"family": "Fiscon", "given": "Giulia"},
        {"family": "Paci", "given": "Paola"},
        {"family": "Prince"},                     # 单名不能造出空 given
    ]


def test_multiword_given_name_kept_whole():
    csl = M._csl_from_crossref({"authors": ["Ji Soo Kim"]}, ENTRY)
    assert csl["author"] == [{"family": "Kim", "given": "Ji Soo"}]


def test_falls_back_to_index_when_crossref_lacks_fields():
    """Crossref 无标题/无日期时，用索引的标题与年份兜底而不是留空。"""
    csl = M._csl_from_crossref({"authors": []}, ENTRY)
    assert csl["title"] == "索引里的标题"
    assert csl["issued"] == {"date-parts": [[2026]]}
    assert "author" not in csl                # 空作者列表不写空数组


def test_no_year_no_issued():
    csl = M._csl_from_crossref({}, {"citekey": "x", "title": "t"})
    assert "issued" not in csl


# ---------------- --pick 式的序号解析不在此脚本，改测 CLI 契约 ----------------

def _run(*args):
    import subprocess
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "repair_references.py"), *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"})


def test_cli_refuses_to_clobber_existing_csl(tmp_path):
    """已有的月度 CSL 由 write_notes 在有 PaperSegment 上下文时产出，信息更全，不得被覆盖。"""
    r = _run("--month", "2026-05", "--dry-run")
    assert r.returncode == 2
    assert "拒绝覆盖" in (r.stdout + r.stderr)


def test_cli_unknown_month_exits_1():
    r = _run("--month", "1999-01", "--dry-run")
    assert r.returncode == 1


def test_cli_missing_index_exits_2(tmp_path):
    r = _run("--month", "2026-06", "--notes-dir", str(tmp_path), "--dry-run")
    assert r.returncode == 2
