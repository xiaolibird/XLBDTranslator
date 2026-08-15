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


# ---------------- F. 精读故障批门控 ----------------

def _settings(tmp_path):
    from src.scholar.schema import ScholarSettings
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n",
        encoding="utf-8",
    )
    s = ScholarSettings.from_env_file(env_file)
    s.processing.notes_dir = tmp_path / "notes"
    s.processing.output_dir = tmp_path / "out"
    return s


def _mock_pipeline(monkeypatch, done):
    """把网络/LLM 依赖全 mock 掉，只留 run_ingest 的门控逻辑。done 为精读成功篇数。"""
    monkeypatch.setattr(ING, "enrich_segments", lambda segs, email, ts: (0, 0, 0))
    monkeypatch.setattr(ING, "resolve_citekeys",
                        lambda segs, base: {s.paper_id: None for s in segs})
    import src.scholar.closereading as closereading
    monkeypatch.setattr(closereading, "close_read_segments", lambda *a, **k: done)
    import src.scholar.llm_client as llm_client
    monkeypatch.setattr(llm_client, "LLMClient", lambda cfg: type('Mock', (), {'close': lambda self: None, 'call': lambda self, *a, **k: ''})())


def test_closeread_zero_success_raises_and_writes_nothing(tmp_path, monkeypatch):
    """精读 0/N 成功=LLM 通路故障批：必须 raise、不写终稿——否则降级札记被
    seen 去重永久固化（周度没有 --force 入口），只能人工翻库才发现。"""
    settings = _settings(tmp_path)
    _mock_pipeline(monkeypatch, done=0)
    import src.scholar.notes as notes
    written = []
    monkeypatch.setattr(notes, "write_notes",
                        lambda *a, **k: written.append(1))

    segs = [_seg(1, "A", doi="10.1/a"), _seg(2, "B", doi="10.1/b")]
    with pytest.raises(RuntimeError, match="全文精读 0/"):
        ING.run_ingest(segs, settings, "2026-07-27", top_n=5, close_read=True, seen=set())
    assert not written
    notes_dir = tmp_path / "notes"
    assert not notes_dir.exists() or not list(notes_dir.glob("*.md"))  # 无新 md 落盘


def test_closeread_partial_success_still_writes(tmp_path, monkeypatch):
    """done>=1 的部分成功照常写盘（保守阈值）：正常周不因个别论文精读失败而拦截。"""
    settings = _settings(tmp_path)
    _mock_pipeline(monkeypatch, done=1)
    import src.scholar.notes as notes
    monkeypatch.setattr(
        notes, "write_notes",
        lambda *a, **k: {"note_path": str(tmp_path / "n.md"), "docx_path": None})

    segs = [_seg(1, "A", doi="10.1/a"), _seg(2, "B", doi="10.1/b")]
    rep = ING.run_ingest(segs, settings, "2026-07-27", top_n=5, close_read=True, seen=set())
    assert rep["status"] == "ok" and rep["count"] == 2


# ---------------- G. 周札记覆盖保护（同 label 第二次入库不得整篇覆盖丢弃上一批） ----------------

def _seed_existing_note(notes_dir: Path, label: str, dedup_keys):
    """伪造一份"已存在"的周札记 md + sidecar（模拟上一批已入库的内容）。"""
    notes_dir.mkdir(parents=True, exist_ok=True)
    stem = "科研札记_{}_全文精读".format(label)
    (notes_dir / "{}.md".format(stem)).write_text("# 既有周札记\n", encoding="utf-8")
    sidecar = {"schema_version": 1, "papers": [
        {"citekey": "old{}".format(i), "dedup_key": k} for i, k in enumerate(dedup_keys)]}
    (notes_dir / "{}.index.json".format(stem)).write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")


def test_second_batch_dropping_existing_paper_is_rejected(tmp_path, monkeypatch):
    """回归：本批不含既有周札记里的全部论文时，拒绝整篇覆盖（否则上一批论文
    连同其 citekey/索引条目一起被抹掉），且不得实际调用 write_notes。"""
    settings = _settings(tmp_path)
    _mock_pipeline(monkeypatch, done=1)
    notes_dir = tmp_path / "notes"
    old_seg = _seg(1, "上批论文", doi="10.1/old")
    _seed_existing_note(notes_dir, "2026-07-27", [ING.dedup_key(old_seg.metadata)])

    import src.scholar.notes as notes
    written = []
    monkeypatch.setattr(notes, "write_notes", lambda *a, **k: written.append(1))

    new_segs = [_seg(2, "本批论文", doi="10.1/new")]  # 不含上批那篇
    with pytest.raises(RuntimeError, match="拒绝写入"):
        ING.run_ingest(new_segs, settings, "2026-07-27", top_n=5, close_read=True, seen=set())
    assert not written
    # 既有文件必须原样保留，不能被本次调用动过
    assert (notes_dir / "科研札记_2026-07-27_全文精读.md").read_text(encoding="utf-8") == "# 既有周札记\n"


def test_second_batch_covering_existing_paper_still_writes(tmp_path, monkeypatch):
    """本批（含新论文）完整覆盖既有周札记里的全部论文时，允许照常写入。"""
    settings = _settings(tmp_path)
    _mock_pipeline(monkeypatch, done=1)
    notes_dir = tmp_path / "notes"
    kept_seg = _seg(1, "上批论文", doi="10.1/old")
    _seed_existing_note(notes_dir, "2026-07-27", [ING.dedup_key(kept_seg.metadata)])

    import src.scholar.notes as notes
    monkeypatch.setattr(
        notes, "write_notes",
        lambda *a, **k: {"note_path": str(notes_dir / "n.md"), "docx_path": None})

    new_segs = [_seg(1, "上批论文", doi="10.1/old"), _seg(2, "本批论文", doi="10.1/new")]
    rep = ING.run_ingest(new_segs, settings, "2026-07-27", top_n=5, close_read=True, seen=set())
    assert rep["status"] == "ok" and rep["count"] == 2


def test_no_existing_note_writes_without_guard_interference(tmp_path, monkeypatch):
    """同名周札记不存在时，守卫不应误挡首次入库。"""
    settings = _settings(tmp_path)
    _mock_pipeline(monkeypatch, done=1)
    notes_dir = tmp_path / "notes"

    import src.scholar.notes as notes
    monkeypatch.setattr(
        notes, "write_notes",
        lambda *a, **k: {"note_path": str(notes_dir / "n.md"), "docx_path": None})

    segs = [_seg(1, "首批论文", doi="10.1/first")]
    rep = ING.run_ingest(segs, settings, "2026-08-03", top_n=5, close_read=True, seen=set())
    assert rep["status"] == "ok" and rep["count"] == 1


def test_existing_note_with_unreadable_sidecar_is_rejected(tmp_path, monkeypatch):
    """既有 md 存在但 sidecar 缺失/损坏：无法确认既有内容，必须保守拒绝而非
    静默当作"无既有内容"直接覆盖。"""
    settings = _settings(tmp_path)
    _mock_pipeline(monkeypatch, done=1)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    stem = "科研札记_2026-07-27_全文精读"
    (notes_dir / "{}.md".format(stem)).write_text("# 既有周札记\n", encoding="utf-8")
    (notes_dir / "{}.index.json".format(stem)).write_text("{ 坏 json", encoding="utf-8")

    import src.scholar.notes as notes
    written = []
    monkeypatch.setattr(notes, "write_notes", lambda *a, **k: written.append(1))

    segs = [_seg(2, "本批论文", doi="10.1/new")]
    with pytest.raises(RuntimeError, match="无法读取"):
        ING.run_ingest(segs, settings, "2026-07-27", top_n=5, close_read=True, seen=set())
    assert not written


def test_run_ingest_excludes_own_week_note_citekeys_from_existing_ckeys(tmp_path, monkeypatch):
    """回归 FIX-1（ingest 侧同源坑）：本批完整覆盖既有周札记（正常改写路径）时，
    existing_ckeys 不该包含这份周札记自己的旧 citekey——否则兜底键生成会把上一轮的
    base 键判成「库内已占用」而加消歧后缀，来回改名（citekey 抖动）。守卫链路虽已
    挡住大多数场景，这里防御式验证 write_notes 收到的 existing_citekeys 确实排除了
    本周文件、但仍含别的周文件的键。"""
    settings = _settings(tmp_path)
    _mock_pipeline(monkeypatch, done=1)
    notes_dir = tmp_path / "notes"
    kept_seg = _seg(1, "上批论文", doi="10.1/old")
    _seed_existing_note(notes_dir, "2026-07-27", [ING.dedup_key(kept_seg.metadata)])

    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "literature_index.json").write_text(json.dumps({"papers": [
        {"citekey": "wang2024Missing", "note_file": "科研札记_2026-07-27_全文精读.md"},
        {"citekey": "other2023Key", "note_file": "科研札记_2026-06-01_全文精读.md"},
    ]}, ensure_ascii=False), encoding="utf-8")

    import src.scholar.notes as notes
    captured = {}

    def fake_write_notes(segs, citekeys, **k):
        captured["existing_citekeys"] = set(k.get("existing_citekeys") or set())
        return {"note_path": str(notes_dir / "n.md"), "docx_path": None}

    monkeypatch.setattr(notes, "write_notes", fake_write_notes)

    new_segs = [_seg(1, "上批论文", doi="10.1/old"), _seg(2, "本批论文", doi="10.1/new")]
    rep = ING.run_ingest(new_segs, settings, "2026-07-27", top_n=5, close_read=True, seen=set())

    assert rep["status"] == "ok"
    assert "wang2024Missing" not in captured["existing_citekeys"]
    assert "other2023Key" in captured["existing_citekeys"]


# ---------------- R3-3：空窗周也必须刷索引 + 同步向量库 ----------------

def _r3_env(tmp_path):
    """最小可用 config：notes_dir 指向 tmp，绝不让早退路径碰到真实札记库。"""
    env_file = tmp_path / "scholar_r3.env"
    env_file.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n".format(tmp_path / "notes"),
        encoding="utf-8")
    return env_file


def _load_cli():
    import importlib.util
    spec_ = importlib.util.spec_from_file_location("ing_cli_r3", CLI)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return mod


def _cli_with_spy(tmp_path, monkeypatch, digest_segs, run_ingest_report=None):
    """把 CLI 的两个外部依赖钉死，只留下「早退时走不走 _refresh_index_and_vectors」这一个变量。"""
    mod = _load_cli()
    calls = []
    monkeypatch.setattr(mod, "_refresh_index_and_vectors", lambda s: calls.append(s))
    monkeypatch.setattr(mod.ing, "load_digest_segments",
                        lambda *a, **k: list(digest_segs))
    if run_ingest_report is not None:
        monkeypatch.setattr(mod.ing, "run_ingest", lambda *a, **k: run_ingest_report)
    return mod, calls


def test_empty_week_still_refreshes_index_and_vectors(tmp_path, monkeypatch):
    """R3-3：「本周无新论文」不蕴含「向量库不需要动」。

    周度 ingest 是向量库唯一的自动同步入口，而向量库会因为**入库之外**的原因变旧
    （改 citekey、改元数据、手工重建索引）。原先这条早退直接 return 1，跳过索引刷新与
    向量同步，于是只要连着几周没有新论文，陈旧状态就无限期存续——而日志上写着
    「无新论文」，读起来像一切正常。
    """
    from src.scholar import notes_index as NI
    seg = _seg(1, "Already Ingested", doi="10.1/dup")
    mod, calls = _cli_with_spy(tmp_path, monkeypatch, [seg])
    monkeypatch.setattr(NI, "load_seen_keys", lambda *a, **k: {ING.dedup_key(seg.metadata)})
    monkeypatch.setattr(mod.sys, "argv",
                        ["ingest", "--config", str(_r3_env(tmp_path)), "--auto"])

    rc = mod.main()
    assert rc == 1                       # 退出码语义不变（没有新论文可入）
    assert len(calls) == 1               # 但索引+向量同步照跑


def test_empty_after_run_ingest_still_refreshes_index_and_vectors(tmp_path, monkeypatch):
    """第二条早退（run_ingest 二次去重后为空）同理。"""
    from src.scholar import notes_index as NI
    seg = _seg(1, "New Paper", doi="10.1/new")
    mod, calls = _cli_with_spy(tmp_path, monkeypatch, [seg],
                               run_ingest_report={"status": "empty"})
    monkeypatch.setattr(NI, "load_seen_keys", lambda *a, **k: set())
    monkeypatch.setattr(mod.sys, "argv",
                        ["ingest", "--config", str(_r3_env(tmp_path)), "--auto"])

    assert mod.main() == 1
    assert len(calls) == 1


def test_read_only_entries_do_not_write_anything_on_empty_week(tmp_path, monkeypatch):
    """--list / --dry-run 是「只看不写」的入口，空窗时不得顺手刷索引写盘。"""
    from src.scholar import notes_index as NI
    seg = _seg(1, "Already Ingested", doi="10.1/dup")
    for flag in ("--list", "--dry-run"):
        mod, calls = _cli_with_spy(tmp_path, monkeypatch, [seg])
        monkeypatch.setattr(NI, "load_seen_keys", lambda *a, **k: {ING.dedup_key(seg.metadata)})
        monkeypatch.setattr(mod.sys, "argv",
                            ["ingest", "--config", str(_r3_env(tmp_path)), flag])
        assert mod.main() == 1
        assert calls == []


def test_no_index_flag_still_suppresses_the_refresh(tmp_path, monkeypatch):
    """--no-index 的语义是「收尾不刷索引」，早退路径也要一致遵守。"""
    from src.scholar import notes_index as NI
    seg = _seg(1, "Already Ingested", doi="10.1/dup")
    mod, calls = _cli_with_spy(tmp_path, monkeypatch, [seg])
    monkeypatch.setattr(NI, "load_seen_keys", lambda *a, **k: {ING.dedup_key(seg.metadata)})
    monkeypatch.setattr(mod.sys, "argv",
                        ["ingest", "--config", str(_r3_env(tmp_path)), "--auto", "--no-index"])
    assert mod.main() == 1
    assert calls == []
