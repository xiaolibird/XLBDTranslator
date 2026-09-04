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
    # 本例要验的正是「真发出去」这条契约，先摘掉 pytest 静默闸（2026-08-24 加，
    # 防止 best-effort 路径的测试噪音推进用户通知中心，见 notify 的 docstring）。
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
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

    def fake_run_month(y, m, settings, seen, existing_ckeys, args, existing_owners=None):
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

    def fake_run_month(y, m, settings, seen, existing_ckeys, args, existing_owners=None):
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

    def fake_run_month(y, m, settings, seen, existing_ckeys, args, existing_owners=None):
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


def test_refresh_topics_called_once_with_merged_citekeys_across_months(tmp_path, monkeypatch):
    """Y5（第 6 轮运行时复审）：backfill_notes.py::main() 收尾"多月 citekey 合并 ->
    单次调用 _refresh_topics_for_keys"这段是 W7 的成果（--since 2023-01 --until
    2026-05 的 41 个月此前逐月各起一次 build_topics.py 子进程，撞了订阅限流）。
    但现有测试里 5 个直调 bn.main() 的用例全部带 --no-index（main() 里
    `if not args.no_index:` 直接把这整段收尾跳过，见 backfill_notes.py），没有一个
    真正跑到这段代码——本用例不带 --no-index，验证 _refresh_topics_for_keys 只被
    调用一次，且收到的是两个月 citekey 的并集，不是逐月各调一次。"""
    import scripts.backfill_notes as bn
    import src.scholar.notes_index as notes_index_mod
    import src.scholar.embed_store as embed_store_mod

    monkeypatch.chdir(tmp_path)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True)
    md_june = notes_dir / "科研札记_2026-06_全文精读.md"
    md_july = notes_dir / "科研札记_2026-07_全文精读.md"

    def fake_run_month(y, m, settings, seen, existing_ckeys, args, existing_owners=None):
        label = "{:04d}-{:02d}".format(y, m)
        md = {"2026-06": md_june, "2026-07": md_july}[label]
        return {"month": label, "status": "ok", "md": str(md)}

    monkeypatch.setattr(bn, "run_month", fake_run_month)

    # 收尾索引刷新（update_index/write_outputs）与向量库同步（sync_store）都是本用例
    # 要验证的"citekey 合并"逻辑之外的独立 best-effort 分支——短路掉避免真的解析
    # md/去连 Ollama，同时喂给下游一份可控的 index_data，让 new_citekeys_from_notes
    # 能按 note_file 精确匹配到两个月各自的新 citekey。
    fake_index = {
        "generated_at": "2026-08-17T00:00:00",
        "papers": [
            {"citekey": "june2026Key", "note_file": md_june.name},
            {"citekey": "july2026Key", "note_file": md_july.name},
        ],
    }
    monkeypatch.setattr(notes_index_mod, "update_index", lambda nd: fake_index)
    monkeypatch.setattr(notes_index_mod, "write_outputs", lambda idx, nd: None)
    monkeypatch.setattr(
        embed_store_mod, "sync_store",
        lambda db_path, idx, client: type(
            "S", (), {"embedded": 0, "deleted": 0, "meta_refreshed": 0})())

    calls = []
    monkeypatch.setattr(
        bn, "_refresh_topics_for_keys",
        lambda notes_dir, citekeys, **kw: calls.append(list(citekeys)) or True)

    monkeypatch.setattr(sys, "argv",
                        ["backfill_notes.py", "--since", "2026-06", "--until", "2026-07",
                         "--config", str(_env_file(tmp_path))])   # 不带 --no-index/--no-topics

    bn.main()   # topics_ok=True（fake 返回 True）→ 不抛 SystemExit

    assert len(calls) == 1, "必须只调用一次，不是逐月各调一次（W7 修复前的行为）"
    assert sorted(calls[0]) == ["july2026Key", "june2026Key"]


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


def test_run_month_half_state_raises_instead_of_skip(tmp_path):
    """写盘中途被杀留下的半态不得记 skipped 静默退 0——须抛 RuntimeError 走 error
    路径（notify + 退非零），提示 --force 重跑完整重建。半态签名按旧写序
    （md → references → sidecar，裸 open('w') 先截断）推导：md 空、references
    缺/坏、sidecar 在但坏。但 sidecar「缺失」不算半态——存量有 43 个 sidecar
    机制出现之前写成的月份（md+references 齐全、无 sidecar）是合法完成态，
    误报会让范围重跑在每个老月份上炸 error 并诱导 --force 重烧整月 LLM。"""
    from src.scholar.schema import ScholarSettings
    import scripts.backfill_notes as bn

    settings = ScholarSettings.from_env_file(_env_file(tmp_path))
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    settings.processing.notes_dir = notes_dir
    args = argparse.Namespace(force=False, no_close_read=True, top_n=0,
                              summary=False, batch_size=15)

    md = notes_dir / "科研札记_2026-06_全文精读.md"
    refs = notes_dir / "科研札记_2026-06_全文精读.references.json"
    sidecar = notes_dir / "科研札记_2026-06_全文精读.index.json"

    # 半态 1：杀在 md 写盘中——md 空（旧版裸 open('w') 刚截断就被杀）
    md.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="md 为空"):
        bn.run_month(2026, 6, settings, set(), set(), args)

    # 半态 2：杀在 md 与 references 之间——md 在、references 缺失
    md.write_text("# 半截札记", encoding="utf-8")
    with pytest.raises(RuntimeError, match="references"):
        bn.run_month(2026, 6, settings, set(), set(), args)

    # 半态 3：杀在 references 写盘中——半截 JSON
    refs.write_text('[{"id": "a2026Key",', encoding="utf-8")
    with pytest.raises(RuntimeError, match="references"):
        bn.run_month(2026, 6, settings, set(), set(), args)

    # 合法老月份：md+references 齐全、无 sidecar（sidecar 机制之前的存量）→ 正常跳过，
    # 绝不能误报半态
    refs.write_text('[{"id": "a2026Key", "type": "article-journal"}]', encoding="utf-8")
    res = bn.run_month(2026, 6, settings, set(), set(), args)
    assert res == {"month": "2026-06", "status": "skipped"}

    # 半态 4：杀在 sidecar 写盘中——sidecar 在但是半截 JSON
    sidecar.write_text('{"schema_version": 1, "papers": [', encoding="utf-8")
    with pytest.raises(RuntimeError, match="sidecar"):
        bn.run_month(2026, 6, settings, set(), set(), args)

    # 三件套完好（sidecar 可读）→ 正常跳过
    sidecar.write_text('{"schema_version": 1, "papers": []}', encoding="utf-8")
    res = bn.run_month(2026, 6, settings, set(), set(), args)
    assert res == {"month": "2026-06", "status": "skipped"}


# ---------------------------------------------------------------------------
# P3 第 1 轮 B3：知识层 lint 触发器与它在 main() 里的接线
#
# 此前唯一不带 --no-index 直调 main() 的用例既没 mock 也没断言过 _run_knowledge_lint，
# 之所以没出事纯粹是因为 tmp_path 不在 output/ 下、被 notes_dir_is_production 短路了
# ——**这条接线路径从未被自动化测试真正走过**（与第 5 轮 X7 完全同型的缺口）。
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, rc, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def _fake_lint(monkeypatch, proc, calls):
    """让 _run_knowledge_lint 真的走完（生产短路解除），但子进程是假的。"""
    import subprocess
    import src.scholar.topics as T
    monkeypatch.setattr(T, "notes_dir_is_production", lambda d: True)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, *a, **k: (calls.append(cmd), proc)[1])


def _capture_notifications(monkeypatch):
    """直接替 backfill_notes 里的 notify 符号，**不要**去 patch notify 模块的
    subprocess.run——那和 _fake_lint patch 的是同一个 subprocess.run，两者会互相覆盖。"""
    import scripts.backfill_notes as bn
    calls = []
    monkeypatch.setattr(bn, "notify", lambda title, body="": calls.append((title, body)))
    return calls


def test_lint_retraction_hit_notifies_but_does_not_fail_the_backfill(tmp_path, monkeypatch):
    """退出码 1 = "lint 干活干成了并且发现了撤稿"，不是失败。要用最高音量喊人，
    但 lint_ok 仍为 True——回填本身完成了，退出码不该变。"""
    import scripts.backfill_notes as bn
    notes = _capture_notifications(monkeypatch)
    runs = []
    _fake_lint(monkeypatch, _Proc(1, "🚨 已撤稿仍在库：[@bad2024] 某某"), runs)

    assert bn._run_knowledge_lint(tmp_path / "notes") is True
    assert runs and "lint_notes.py" in " ".join(runs[0])
    assert "--skip-contradictions" in runs[0]          # 月度链路不花钱跑对撞
    assert len(notes) == 1
    assert "撤稿" in notes[0][0]


def test_lint_tool_failure_marks_lint_not_ok(tmp_path, monkeypatch):
    import scripts.backfill_notes as bn
    notes = _capture_notifications(monkeypatch)
    _fake_lint(monkeypatch, _Proc(2, "", "索引损坏"), [])
    assert bn._run_knowledge_lint(tmp_path / "notes") is False
    assert len(notes) == 1
    assert "未跑成" in notes[0][1]


def test_lint_finding_and_tool_failure_are_both_reported(tmp_path, monkeypatch):
    """B2：`alert` 与 `ok` 是两个独立字段，此前那对 if/elif 是互斥的——
    "既发现了撤稿、报告又没写进磁盘"时，撤稿警报会被工具故障那条分支吃掉。"""
    import scripts.backfill_notes as bn
    notes = _capture_notifications(monkeypatch)
    _fake_lint(monkeypatch,
               _Proc(2, "🚨 已撤稿仍在库：[@bad2024] 某某\n⚠️ 报告落盘冲突", "落盘冲突"), [])
    assert bn._run_knowledge_lint(tmp_path / "notes") is False
    bodies = [t + b for t, b in notes]
    assert len(notes) == 2
    assert any("撤稿" in b for b in bodies)
    assert any("未跑成" in b for b in bodies)


def _main_with_index(tmp_path, monkeypatch, extra_argv):
    """跑一遍 main()（不带 --no-index），把索引刷新/向量同步都换成假的。"""
    import scripts.backfill_notes as bn
    import src.scholar.notes_index as ni
    import src.scholar.embed_store as es
    import src.scholar.embeddings as emb
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ni, "update_index", lambda d: {"papers": []})
    monkeypatch.setattr(ni, "write_outputs", lambda data, d: None)
    monkeypatch.setattr(emb, "resolve_embedding_base_url", lambda cfg: "http://x")
    monkeypatch.setattr(emb, "EmbeddingClient",
                        lambda **kw: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(es, "sync_store", lambda *a, **k: es.SyncStats())
    monkeypatch.setattr(bn, "run_month",
                        lambda y, m, *a, **k: {"month": "{:04d}-{:02d}".format(y, m),
                                               "status": "ok"})
    argv = ["backfill_notes.py", "--since", "2026-06", "--until", "2026-06",
            "--config", str(_env_file(tmp_path))] + extra_argv
    monkeypatch.setattr(sys, "argv", argv)
    return bn


def test_main_exits_3_when_the_lint_run_itself_failed(tmp_path, monkeypatch):
    """派生检查没跟上 → 退出码 3（对齐概念页那一档），不是回填失败的 1。"""
    _capture_notifications(monkeypatch)
    runs = []
    bn = _main_with_index(tmp_path, monkeypatch, [])
    _fake_lint(monkeypatch, _Proc(2, "", "索引损坏"), runs)
    with pytest.raises(SystemExit) as ei:
        bn.main()
    assert ei.value.code == 3
    assert len(runs) == 1


def test_main_retraction_alert_does_not_change_the_exit_code(tmp_path, monkeypatch):
    _capture_notifications(monkeypatch)
    runs = []
    bn = _main_with_index(tmp_path, monkeypatch, [])
    _fake_lint(monkeypatch, _Proc(1, "🚨 已撤稿仍在库：[@bad2024] 某某"), runs)
    bn.main()             # 不抛 SystemExit → 退出码 0
    assert len(runs) == 1


def test_no_lint_flag_never_spawns_the_subprocess(tmp_path, monkeypatch):
    _capture_notifications(monkeypatch)
    runs = []
    bn = _main_with_index(tmp_path, monkeypatch, ["--no-lint"])
    _fake_lint(monkeypatch, _Proc(1, "🚨 已撤稿仍在库：[@bad2024] 某某"), runs)
    bn.main()
    assert runs == []


def test_no_index_also_skips_lint(tmp_path, monkeypatch):
    """索引都没刷新，lint 读到的是旧库，结论没意义。"""
    _capture_notifications(monkeypatch)
    runs = []
    bn = _main_with_index(tmp_path, monkeypatch, ["--no-index"])
    _fake_lint(monkeypatch, _Proc(1, "🚨 已撤稿仍在库：[@bad2024] 某某"), runs)
    bn.main()
    assert runs == []


def test_lint_trigger_refuses_to_touch_a_non_production_notes_dir(tmp_path, monkeypatch):
    """安全前提：tmp_path 不在 output/ 下时一律不发子进程（read_pdf.py 的同类触发器
    实测踩过，测试套件真的起了一个指向生产 config 的子进程，挂了 13 分钟）。"""
    import subprocess
    import scripts.backfill_notes as bn
    runs = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: runs.append(cmd))
    assert bn._run_knowledge_lint(tmp_path / "notes") is True
    assert runs == []


def test_run_month_report_carries_sidecar_ok(tmp_path, monkeypatch):
    """变异 R31b：run_month 回执不透传 `sidecar_ok` → main() 里那条 error+notify 永远不触发，
    sidecar 写失败（阅读深度量尺永久丢失）在过夜回填里重新变回静默。"""
    import argparse
    import scripts.backfill_notes as bn
    from src.scholar.schema import DigestOutput, PaperSegment, PaperMetadata, FilterDecision
    from src.scholar.settings import ScholarSettings
    settings = ScholarSettings.from_env_file(_env_file(tmp_path))
    settings.processing.notes_dir = tmp_path / "notes"
    settings.processing.output_dir = tmp_path / "out"

    def _paper(sid, title):
        return PaperSegment(
            segment_id=sid, paper_id="paper_{}".format(sid), priority_score=0.9,
            filter_decision=FilterDecision(paper_id="paper_{}".format(sid), title=title,
                                           verdict="included", decision="INCLUDE",
                                           stage="llm_judge", reason="r", confidence=0.9),
            metadata=PaperMetadata(paper_id="paper_{}".format(sid), title=title))

    digest = DigestOutput(digest_id="d", segments=[_paper(1, "EHR missingness study")],
                          undecided_segments=[])

    class FakeWF:
        def __init__(self, s):
            self.date_range = None

        def execute(self):
            return digest

    monkeypatch.setattr(bn, "ScholarWorkflow", FakeWF)
    monkeypatch.setattr(bn, "enrich_segments", lambda segs, email, ts: (0, 0, 0))
    monkeypatch.setattr(bn, "resolve_citekeys", lambda segs, base: {s.paper_id: None for s in segs})
    import src.scholar.closereading as closereading
    monkeypatch.setattr(closereading, "close_read_segments", lambda *a, **k: 1)
    import src.scholar.llm_client as llm_client
    monkeypatch.setattr(llm_client, "LLMClient",
                        lambda cfg: type('M', (), {'close': lambda self: None,
                                                   'call': lambda self, *a, **k: ''})())
    import src.scholar.notes as notes
    args = argparse.Namespace(force=False, no_close_read=False, top_n=1, summary=False, batch_size=15)

    monkeypatch.setattr(notes, "write_notes", lambda *a, **k: {
        "note_path": str(tmp_path / "n.md"), "docx_path": None, "sidecar_ok": False,
        "sidecar_error": "ValueError: boom"})
    r = bn.run_month(2026, 6, settings, set(), set(), args)
    assert r["status"] == "ok" and r["sidecar_ok"] is False

    monkeypatch.setattr(notes, "write_notes", lambda *a, **k: {
        "note_path": str(tmp_path / "n2.md"), "docx_path": None, "sidecar_ok": True,
        "index_sidecar": str(tmp_path / "n2.index.json")})
    r = bn.run_month(2026, 7, settings, set(), set(), args)
    assert r["sidecar_ok"] is True
    # 老形态回执（无该键）默认视为成功，不误报
    monkeypatch.setattr(notes, "write_notes", lambda *a, **k: {
        "note_path": str(tmp_path / "n3.md"), "docx_path": None})
    assert bn.run_month(2026, 8, settings, set(), set(), args)["sidecar_ok"] is True
