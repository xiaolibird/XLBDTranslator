# -*- coding: utf-8 -*-
"""按月回填回归测试：显式日期区间(PubMed mindate/maxdate、arXiv submittedDate) + 月份解析
+ 无人值守失败可见化（错误月退非零+通知、seen 键写盘后才并入）。"""
import argparse
import json
import sys
from datetime import date

import httpx
import pytest

from src.scholar.academic_search import AcademicSearchClient


def _client_with(handler):
    c = AcademicSearchClient(email="x@y.com")
    c._client = httpx.Client(transport=httpx.MockTransport(handler), headers=c._client.headers)
    return c


_ARXIV_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry><id>http://arxiv.org/abs/2203.00001v1</id><title>MNAR paper</title>
    <summary>x</summary><published>2022-03-15T00:00:00Z</published>
    <author><name>A B</name></author></entry>
</feed>"""


def test_pubmed_date_range_uses_mindate_maxdate():
    captured = {}

    def handler(request):
        if "esearch" in request.url.path:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, text="<PubmedArticleSet></PubmedArticleSet>")

    c = _client_with(handler)
    c.search_pubmed("MNAR", max_results=10, date_range=(date(2022, 3, 1), date(2022, 3, 31)))
    assert captured.get("mindate") == "2022/03/01"
    assert captured.get("maxdate") == "2022/03/31"
    assert captured.get("datetype") == "pdat"
    assert "reldate" not in captured  # 区间模式不用相对天数
    c.close()


def test_arxiv_date_range_injects_submitteddate():
    captured = {}

    def handler(request):
        captured["search_query"] = dict(request.url.params).get("search_query", "")
        return httpx.Response(200, text=_ARXIV_XML)

    c = _client_with(handler)
    items = c.search_arxiv("all:MNAR", max_results=10,
                           date_range=(date(2022, 3, 1), date(2022, 3, 31)))
    assert "submittedDate:[20220301" in captured["search_query"]
    assert "20220331" in captured["search_query"]
    assert len(items) == 1 and items[0]["title"] == "MNAR paper"
    c.close()


def test_month_range_parsing():
    from scholar_main import _parse_month_range
    assert _parse_month_range(argparse.Namespace(month="2022-03", since=None, until=None)) == \
        (date(2022, 3, 1), date(2022, 3, 31))
    # 12 月跨年边界
    assert _parse_month_range(argparse.Namespace(month="2022-12", since=None, until=None)) == \
        (date(2022, 12, 1), date(2022, 12, 31))
    # since/until
    assert _parse_month_range(argparse.Namespace(month=None, since="2026-05-01", until="2026-06-15")) == \
        (date(2026, 5, 1), date(2026, 6, 15))
    # 无参数
    assert _parse_month_range(argparse.Namespace(month=None, since=None, until=None)) is None


def test_run_month_undecided_not_in_notes(tmp_path, monkeypatch):
    """digest 含 undecided 时，月度札记与精读的入参 segs 不含该论文——
    LLM 裁决失败回退的未决论文只该出现在待复核小节，不该混进 write_notes/close_read。"""
    from src.scholar.schema import (DigestOutput, PaperMetadata, PaperSegment,
                                    ScholarSettings)
    import scripts.backfill_notes as bn

    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n",
        encoding="utf-8",
    )
    settings = ScholarSettings.from_env_file(env_file)
    settings.processing.notes_dir = tmp_path / "notes"
    settings.processing.output_dir = tmp_path / "out"

    def make_paper(sid, title):
        return PaperSegment(
            segment_id=sid, paper_id="paper_{}".format(sid),
            metadata=PaperMetadata(paper_id="paper_{}".format(sid), title=title),
        )

    digest = DigestOutput(
        digest_id="test_digest",
        segments=[make_paper(1, "EHR missingness study")],
        undecided_segments=[make_paper(2, "Quasar spectroscopy")],
    )

    class FakeWF:
        def __init__(self, settings):
            self.date_range = None

        def execute(self):
            return digest

    captured = {}
    monkeypatch.setattr(bn, "ScholarWorkflow", FakeWF)
    monkeypatch.setattr(bn, "enrich_segments", lambda segs, email, ts: (0, 0, 0))
    monkeypatch.setattr(bn, "resolve_citekeys",
                        lambda segs, base: {s.paper_id: None for s in segs})

    import src.scholar.closereading as closereading

    def fake_close_read(segs, *a, **k):
        captured["close_read"] = [s.paper_id for s in segs]
        return 1    # 真实现返回 int 成功篇数；0 会触发"LLM 故障批"门控

    monkeypatch.setattr(closereading, "close_read_segments", fake_close_read)

    import src.scholar.llm_client as llm_client
    monkeypatch.setattr(llm_client, "LLMClient", lambda cfg: type('Mock', (), {'close': lambda self: None, 'call': lambda self, *a, **k: ''})())

    import src.scholar.notes as notes

    def fake_write_notes(segs, citekeys, **k):
        captured["write_notes"] = [s.paper_id for s in segs]
        return {"note_path": str(tmp_path / "note.md"), "docx_path": None}

    monkeypatch.setattr(notes, "write_notes", fake_write_notes)

    args = argparse.Namespace(force=False, no_close_read=False, top_n=1,
                              summary=False, batch_size=15)
    res = bn.run_month(2026, 6, settings, set(), set(), args)

    assert res["status"] == "ok"
    assert captured["write_notes"] == ["paper_1"]
    assert captured["close_read"] == ["paper_1"]
    assert "paper_2" not in captured["write_notes"]
    assert "paper_2" not in captured["close_read"]


# ---------------- 无人值守失败可见化 ----------------

def _env_file(tmp_path):
    p = tmp_path / "scholar_test.env"
    p.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n".format(tmp_path / "notes"),
        encoding="utf-8",
    )
    return p


def _main_argv(env_file):
    return ["backfill_notes.py", "--since", "2026-06", "--until", "2026-06",
            "--no-index", "--config", str(env_file)]


def test_main_month_error_exits_1_and_notifies(tmp_path, monkeypatch):
    """launchd 无人值守：某月失败必须退出码 1 + osascript 系统通知，不能静默退 0。"""
    import scripts.backfill_notes as bn

    monkeypatch.chdir(tmp_path)  # progress 文件写进 tmp，别污染仓库 output/
    calls = []
    import src.utils.notify as notify_mod
    monkeypatch.setattr(notify_mod.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(bn, "run_month",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM 故障")))
    monkeypatch.setattr(sys, "argv", _main_argv(_env_file(tmp_path)))

    with pytest.raises(SystemExit) as ei:
        bn.main()
    assert ei.value.code == 1
    assert calls and calls[0][0] == "osascript"
    assert "2026-06" in calls[0][2]  # 通知正文列出失败月份


def test_main_stale_progress_error_does_not_fail_run(tmp_path, monkeypatch):
    """progress 文件跨运行续用：历史遗留的 error 条目（month 不在本次 run_months）
    不该让这次全部成功的运行误报非零退出/弹通知。"""
    import scripts.backfill_notes as bn

    monkeypatch.chdir(tmp_path)
    prog_dir = tmp_path / "output" / "scholar_notes"
    prog_dir.mkdir(parents=True)
    (prog_dir / "backfill_progress_2026-06_2026-06.json").write_text(
        json.dumps({"results": [{"month": "2026-05", "status": "error", "error": "旧的"}]}),
        encoding="utf-8")
    calls = []
    import src.utils.notify as notify_mod
    monkeypatch.setattr(notify_mod.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(bn, "run_month",
                        lambda y, m, *a, **k: {"month": "{:04d}-{:02d}".format(y, m),
                                               "status": "ok"})
    monkeypatch.setattr(sys, "argv", _main_argv(_env_file(tmp_path)))

    bn.main()   # 不抛 SystemExit → 退出码 0
    assert not calls  # 成功不弹通知


def test_failed_month_keys_not_in_seen_for_later_months(tmp_path, monkeypatch):
    """某月写盘失败后，该月论文的 dedup_key 不得残留在 seen 里——
    否则同一篇在同轮后续月份会被幽灵键静默 dedup 掉（实际从未落盘）。"""
    from src.scholar.schema import (DigestOutput, PaperMetadata, PaperSegment,
                                    ScholarSettings)
    import scripts.backfill_notes as bn

    settings = ScholarSettings.from_env_file(_env_file(tmp_path))
    settings.processing.notes_dir = tmp_path / "notes"
    settings.processing.output_dir = tmp_path / "out"

    def make_digest():
        meta = PaperMetadata(paper_id="paper_1", title="Cross month preprint",
                             doi="10.1/x")
        return DigestOutput(digest_id="d", segments=[
            PaperSegment(segment_id=1, paper_id="paper_1", metadata=meta)])

    class FakeWF:
        def __init__(self, settings):
            self.date_range = None

        def execute(self):
            return make_digest()

    monkeypatch.setattr(bn, "ScholarWorkflow", FakeWF)
    monkeypatch.setattr(bn, "enrich_segments", lambda segs, email, ts: (0, 0, 0))
    monkeypatch.setattr(bn, "resolve_citekeys",
                        lambda segs, base: {s.paper_id: None for s in segs})
    import src.scholar.closereading as closereading
    monkeypatch.setattr(closereading, "close_read_segments", lambda *a, **k: 1)
    import src.scholar.llm_client as llm_client
    monkeypatch.setattr(llm_client, "LLMClient", lambda cfg: type('Mock', (), {'close': lambda self: None, 'call': lambda self, *a, **k: ''})())

    import src.scholar.notes as notes
    state = {"fail": True, "written": []}

    def fake_write_notes(segs, citekeys, **k):
        if state["fail"]:
            raise IOError("磁盘写失败")
        state["written"] = [s.paper_id for s in segs]
        return {"note_path": str(tmp_path / "note.md"), "docx_path": None}

    monkeypatch.setattr(notes, "write_notes", fake_write_notes)

    args = argparse.Namespace(force=False, no_close_read=False, top_n=1,
                              summary=False, batch_size=15)
    seen: set = set()
    with pytest.raises(IOError):
        bn.run_month(2026, 5, settings, seen, set(), args)
    assert seen == set()  # 失败月的键不并入

    state["fail"] = False
    res = bn.run_month(2026, 6, settings, seen, set(), args)
    assert res["status"] == "ok"
    assert state["written"] == ["paper_1"]  # 同一篇在后续月份未被幽灵键 dedup 掉
    assert seen  # 落盘成功后键才并入


def test_force_excludes_own_month_citekeys_from_existing_ckeys(tmp_path, monkeypatch):
    """--force 重跑某月时，existing_ckeys 不应包含该月自己在索引里的旧 citekey——
    否则兜底键生成会把上一轮的 base 键判成「库内已占用」而加消歧后缀，下一轮又
    因为后缀键才是「已占用」而改回原键，来回改名（citekey 抖动）。"""
    import scripts.backfill_notes as bn

    monkeypatch.chdir(tmp_path)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "literature_index.json").write_text(json.dumps({
        "papers": [
            {"citekey": "wang2024Missing", "note_file": "科研札记_2026-06_全文精读.md"},
            {"citekey": "other2023Key", "note_file": "科研札记_2026-05_全文精读.md"},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n".format(notes_dir),
        encoding="utf-8",
    )

    captured = {}

    def fake_run_month(y, m, settings, seen, existing_ckeys, args):
        captured["existing_ckeys"] = set(existing_ckeys)
        return {"month": "{:04d}-{:02d}".format(y, m), "status": "ok"}

    monkeypatch.setattr(bn, "run_month", fake_run_month)
    monkeypatch.setattr(sys, "argv",
                        ["backfill_notes.py", "--since", "2026-06", "--until", "2026-06",
                         "--no-index", "--force", "--config", str(env_file)])

    bn.main()

    assert "wang2024Missing" not in captured["existing_ckeys"]  # 本月自己的旧键已排除
    assert "other2023Key" in captured["existing_ckeys"]         # 别的月份键仍算「已占用」


def test_no_force_keeps_own_month_citekeys_in_existing_ckeys(tmp_path, monkeypatch):
    """不带 --force 的正常跑（新月份）不该排除任何 note_file——existing_ckeys 就该是
    索引里的全量键，这样新论文才能避开库内已有的 citekey 撞名。"""
    import scripts.backfill_notes as bn

    monkeypatch.chdir(tmp_path)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "literature_index.json").write_text(json.dumps({
        "papers": [
            {"citekey": "wang2024Missing", "note_file": "科研札记_2026-06_全文精读.md"},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n".format(notes_dir),
        encoding="utf-8",
    )

    captured = {}

    def fake_run_month(y, m, settings, seen, existing_ckeys, args):
        captured["existing_ckeys"] = set(existing_ckeys)
        return {"month": "{:04d}-{:02d}".format(y, m), "status": "ok"}

    monkeypatch.setattr(bn, "run_month", fake_run_month)
    monkeypatch.setattr(sys, "argv",
                        ["backfill_notes.py", "--since", "2026-07", "--until", "2026-07",
                         "--no-index", "--config", str(env_file)])

    bn.main()

    assert "wang2024Missing" in captured["existing_ckeys"]


def test_sidecar_citekeys_merged_into_existing_ckeys_across_months(tmp_path, monkeypatch):
    """多月同进程内跑：本月新生成的兜底 citekey（sidecar）必须在下一个月调用
    run_month 前就并进 existing_ckeys——否则同一进程内连续两个月遇到同样的
    作者+年份 base 会各自算出同一个兜底键（existing_ckeys 只在循环开始前从
    literature_index.json 算过一次快照，主索引要等全部月份跑完才由 update_index
    刷新，看不到本月刚写盘的键）。"""
    import scripts.backfill_notes as bn

    monkeypatch.chdir(tmp_path)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "literature_index.json").write_text(
        json.dumps({"papers": []}), encoding="utf-8")

    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/creds.json\n"
        "GMAIL__TOKEN_PATH=fake/token.json\n"
        "LLM__PROVIDER=gemini\n"
        "LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST\n"
        "LLM__MODEL=fake-model\n"
        "PROCESSING__NOTES_DIR={}\n".format(notes_dir),
        encoding="utf-8",
    )

    # 第一个月的 sidecar：write_notes 顺手写出的 {slug}.index.json，含一个新生成的兜底键
    sidecar_1 = notes_dir / "科研札记_2026-06_全文精读.index.json"
    sidecar_1.write_text(json.dumps({"papers": [{"citekey": "wang2026Deep"}]},
                                    ensure_ascii=False), encoding="utf-8")

    captured = []

    def fake_run_month(y, m, settings, seen, existing_ckeys, args):
        label = "{:04d}-{:02d}".format(y, m)
        captured.append((label, set(existing_ckeys)))
        if label == "2026-06":
            return {"month": label, "status": "ok", "index_sidecar": str(sidecar_1)}
        return {"month": label, "status": "ok"}

    monkeypatch.setattr(bn, "run_month", fake_run_month)
    monkeypatch.setattr(sys, "argv",
                        ["backfill_notes.py", "--since", "2026-06", "--until", "2026-07",
                         "--no-index", "--config", str(env_file)])

    bn.main()

    assert captured[0][0] == "2026-06"
    assert "wang2026Deep" not in captured[0][1]    # 6 月开跑前索引里还没有这个键
    assert captured[1][0] == "2026-07"
    assert "wang2026Deep" in captured[1][1]         # 7 月开跑前已并回，避免撞名


# ---------------- top-N 精读候选随 priority_score 变化（R2 回归） ----------------


def test_close_read_top_n_follows_priority_score_not_input_order(monkeypatch):
    """R2 回归：月度回填此前 priority_score 恒为 0.0，close_read_segments 的稳定排序
    退化为输入（邮件）顺序。修复后 execute() 会先算规则优先级+加成，这里验证
    close_read_segments 确实按 priority_score 选 top-N——含 THREAT/MUST_ENGAGE
    加成的后位论文要能进 top-N，而不是照抄输入顺序的前 N 篇。"""
    from src.scholar.schema import PaperMetadata, PaperSegment
    import src.scholar.closereading as cr

    def seg(sid, pid, score):
        return PaperSegment(
            segment_id=sid, paper_id=pid, priority_score=score,
            metadata=PaperMetadata(paper_id=pid, title=pid),
            original_abstract="abs")

    # 输入顺序 a→threat，priority_score 与之相反（升序）；
    # 末位 threat 的 2.0 = 规则基分 0.2 + THREAT/MUST_ENGAGE 加成 1.8（加成已在 workflow 侧写入）
    segs = [seg(1, "a", 0.1), seg(2, "b", 0.2), seg(3, "c", 0.3),
            seg(4, "d", 0.4), seg(5, "threat", 2.0)]

    read_order = []

    # **kw 吸收 close_read_segments 无条件透传的 deep/max_chars/max_chunks：
    # 少了它替身会在调用处 TypeError，被 close_read_segments 的 except 吞成 cr=None
    def fake_close_read_segment(s, ri, llm, email="", model=None,
                                scratch_dir=None, oa=None, **kw):
        read_order.append(s.paper_id)
        return None

    monkeypatch.setattr(cr, "close_read_segment", fake_close_read_segment)

    # prefer_full_text=False：跳过 OA 预解析（不发网络），chosen = 按 priority_score 的前 N
    cr.close_read_segments(segs, "ri", llm=None, top_n=2, prefer_full_text=False)

    assert read_order == ["threat", "d"]                   # 顺序随 priority_score 变化
    assert read_order != [s.paper_id for s in segs[:2]]    # 不再等于输入顺序前 N 篇
