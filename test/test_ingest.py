# -*- coding: utf-8 -*-
"""周/按需入库（src/scholar/ingest.py + scripts/ingest_notes.py）。

盯住三件容易静默出错的事：
1. **无裁决的 digest 产物不得入库**——目录里躺着 4700+ 条早期未经筛选的抽取结果，
   放进来等于把 Scholar 告警原样倒进札记库。
2. **--pick 的序号必须对应去重后的清单**，否则「入第 3 篇」入的是别的论文。
3. 周 label 的命名要被 notes_index 的 NOTE_MD_RE 认、被 vault 的 month_key 折回月份，
   否则周文件进不了索引/图谱。
"""
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import ingest as ING              # noqa: E402
from src.scholar.schema import (DigestOutput, DigestStatus, FilterDecision,  # noqa: E402
                                PaperField, PaperMetadata, PaperSegment)

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "scripts" / "ingest_notes.py"


# ---------------- 造数据 ----------------

def _seg(sid, title, doi=None, decided=True, verdict="included", score=0.5, bucket=("G",)):
    meta = PaperMetadata(paper_id="p{}".format(sid), title=title, authors=["A B"],
                         doi=doi, field=PaperField.OTHER, source_type="gmail")
    seg = PaperSegment(segment_id=sid, paper_id=meta.paper_id, original_abstract="abs",
                       metadata=meta, status=DigestStatus.COMPLETED, priority_score=score)
    if decided:
        seg.filter_decision = FilterDecision(
            paper_id=meta.paper_id, title=title, verdict=verdict,
            decision="INCLUDE" if verdict == "included" else "EXCLUDE",
            stage="llm_judge", reason="r", one_line="一句话", bucket=list(bucket))
    return seg


def _write_digest(dirp: Path, stamp: str, segs):
    out = DigestOutput(digest_id=stamp, title="t", total_emails=1, emails_processed=[],
                       total_papers=len(segs), segments=list(segs),
                       status=DigestStatus.COMPLETED)
    p = dirp / "digest_{}.json".format(stamp)
    out.save_to_file(p)
    return p


@pytest.fixture
def digest_dir(tmp_path):
    d = tmp_path / "digests"
    d.mkdir()
    return d


# ---------------- A. 周历 ----------------

@pytest.mark.parametrize("day,monday", [
    ("2026-07-27", "2026-07-27"),      # 周一本身
    ("2026-07-29", "2026-07-27"),      # 周三
    ("2026-08-02", "2026-07-27"),      # 周日（ISO 周末，仍属上周一）
    ("2026-08-03", "2026-08-03"),      # 下周一
])
def test_week_monday(day, monday):
    d = datetime.strptime(day, "%Y-%m-%d").date()
    assert ING.week_label(d) == monday


def test_week_label_is_accepted_by_notes_index_and_vault():
    """周文件名必须被索引认、被 vault 折回月份，否则周札记进不了索引/图谱。"""
    from src.scholar.notes_index import NOTE_MD_RE
    from src.scholar.vault import month_key
    label = ING.week_label(date(2026, 7, 29))
    m = NOTE_MD_RE.match("科研札记_{}_全文精读.md".format(label))
    assert m and m.group(1) == "2026-07-27"
    assert month_key({"month": "2026-07-27"}) == "2026-07"


@pytest.mark.parametrize("label,ok", [
    ("2026-07", True),                    # 月度回填
    ("2026-07-27", True),                 # 周札记 / 日期专题批次
    ("2026-07-27-HuiyingLiang", True),    # 作者语料通读批次
    ("2026-07-27-审稿人A", True),          # 批次名允许中文
    ("2026-07-27_HuiyingLiang", False),   # 下划线是系列分隔符，不能出现在批次名里
    ("2026年7月", False),
])
def test_batch_label_is_accepted_by_notes_index(label, ok):
    """带批次名的专题札记也必须进索引——否则精读了却在文献库里检索不到。"""
    from src.scholar.notes_index import NOTE_MD_RE
    from src.scholar.vault import month_key
    m = NOTE_MD_RE.match("科研札记_{}_全文精读.md".format(label))
    assert bool(m) is ok
    if ok:
        assert m.group(1) == label
        assert month_key({"month": label}) == "2026-07"   # 图谱仍折回同一个月度页


# ---------------- B. digest 复用 ----------------

def test_undecided_segments_are_never_ingested(digest_dir):
    """回归：早期 digest 产物没有 filter_decision，那是**未经筛选**的原始抽取结果。"""
    _write_digest(digest_dir, "20260727_090000",
                  [_seg(1, "有裁决"), _seg(2, "无裁决", decided=False)])
    segs = ING.load_digest_segments(digest_dir)
    assert [s.metadata.title for s in segs] == ["有裁决"]


def test_excluded_segments_are_not_ingested(digest_dir):
    _write_digest(digest_dir, "20260727_090000",
                  [_seg(1, "入选"), _seg(2, "排除", verdict="excluded")])
    assert [s.metadata.title for s in ING.load_digest_segments(digest_dir)] == ["入选"]


def test_dedup_across_runs_in_same_week(digest_dir):
    """同周多次重跑 + 相邻周 8 天窗口重叠 → 同一篇会出现多次，只能收一条。"""
    _write_digest(digest_dir, "20260727_090000", [_seg(1, "同一篇", doi="10.1/x")])
    _write_digest(digest_dir, "20260727_140000", [_seg(9, "同一篇", doi="10.1/x")])
    assert len(ING.load_digest_segments(digest_dir)) == 1


def test_window_filters_by_run_timestamp(digest_dir):
    _write_digest(digest_dir, "20260720_090000", [_seg(1, "上周")])
    _write_digest(digest_dir, "20260727_090000", [_seg(2, "本周")])
    got = ING.load_digest_segments(digest_dir, since=date(2026, 7, 27), until=date(2026, 8, 2))
    assert [s.metadata.title for s in got] == ["本周"]


def test_sidecar_files_are_not_parsed_as_runs(digest_dir):
    """digest_xxx_excluded.json / _stats.json 与正片同前缀，误当 run 读会把排除项捞回来。"""
    _write_digest(digest_dir, "20260727_090000", [_seg(1, "正片")])
    (digest_dir / "digest_20260727_090000_excluded.json").write_text(
        json.dumps({"papers": []}), encoding="utf-8")
    (digest_dir / "digest_20260727_090000_stats.json").write_text("{}", encoding="utf-8")
    assert len(ING.digest_runs(digest_dir)) == 1


def test_corrupt_digest_does_not_abort_the_batch(digest_dir):
    _write_digest(digest_dir, "20260727_090000", [_seg(1, "好的")])
    (digest_dir / "digest_20260727_100000.json").write_text("{ 坏 json", encoding="utf-8")
    assert [s.metadata.title for s in ING.load_digest_segments(digest_dir)] == ["好的"]


def test_sorted_by_priority_desc(digest_dir):
    _write_digest(digest_dir, "20260727_090000",
                  [_seg(1, "低", score=0.1), _seg(2, "高", score=0.9), _seg(3, "中", score=0.5)])
    assert [s.metadata.title for s in ING.load_digest_segments(digest_dir)] == ["高", "中", "低"]


# ---------------- C. --pick 解析 ----------------

def _parse_pick(spec, n):
    import importlib.util
    spec_ = importlib.util.spec_from_file_location("ing_cli", CLI)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return mod.parse_pick(spec, n)


@pytest.mark.parametrize("spec,want", [
    ("2,3,5", [1, 2, 4]),
    ("1-3", [0, 1, 2]),
    ("1-2,5", [0, 1, 4]),
    ("3,3,3", [2]),                 # 去重
    (" 2 , 4 ", [1, 3]),
])
def test_parse_pick(spec, want):
    assert _parse_pick(spec, 5) == want


@pytest.mark.parametrize("spec", ["0", "6", "3-9", "4-2", "abc", "1-"])
def test_parse_pick_rejects_bad_input(spec):
    """越界必须报错而不是静默截断——少入库比乱入库更难被发现。"""
    with pytest.raises(ValueError):
        _parse_pick(spec, 5)


# ---------------- D. 标识符解析 ----------------

@pytest.mark.parametrize("text,want", [
    ("10.1/a\n10.2/b", ["10.1/a", "10.2/b"]),
    ("# 注释\n\n10.1/a\n", ["10.1/a"]),
    ("  10.1/a  ", ["10.1/a"]),
    ("10.1/a#page=2", ["10.1/a#page=2"]),      # 行内 # 不是注释（DOI 里合法）
])
def test_parse_identifiers(text, want):
    assert ING.parse_identifiers(text) == want


@pytest.mark.parametrize("s", ["2504.08919", "arXiv:2504.08919", "ARXIV:2504.08919v2"])
def test_arxiv_pattern(s):
    assert ING.ARXIV_RE.match(s).group(1) == "2504.08919"


@pytest.mark.parametrize("s,want", [
    ("10.1038/s41746-025-01234-5", "10.1038/s41746-025-01234-5"),
    ("https://doi.org/10.1001/jama.2025.9876", "10.1001/jama.2025.9876"),
])
def test_doi_pattern(s, want):
    assert ING.DOI_RE.search(s).group(1) == want


def test_plain_title_is_not_mistaken_for_id():
    t = "Learning Meta-Features for AutoML"
    assert not ING.ARXIV_RE.match(t) and not ING.DOI_RE.search(t)


# ---------------- E. CLI 契约 ----------------

def _run(*args, cwd=REPO):
    return subprocess.run([sys.executable, str(CLI), *args], cwd=str(cwd),
                          capture_output=True, text=True,
                          env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"})


def test_cli_missing_config_exits_2(tmp_path):
    r = _run("--config", str(tmp_path / "nope.env"), "--list")
    assert r.returncode == 2


def test_cli_bad_date_exits_2():
    r = _run("--week", "2026/07/27", "--list")
    assert r.returncode == 2


def test_cli_pick_and_auto_are_mutually_exclusive():
    r = _run("--pick", "1", "--auto")
    assert r.returncode == 2 and "互斥" in (r.stderr + r.stdout)


def test_cli_empty_window_exits_1(tmp_path):
    r = _run("--digest-dir", str(tmp_path), "--list")
    assert r.returncode == 1
