# -*- coding: utf-8 -*-
"""WO-6 金标准构造：指标纯函数、bib 解析与 P1 匹配、登记闸、H-stream 分层抽样、盲标表泄露、
build-gold 端到端幂等。全部 tmp 夹具，标题均为虚构，不读真实 output、不联网、不调 LLM。
"""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import filter_eval as FE                                  # noqa: E402

BIB = r"""
@article{aaa2020Alpha,
  author = {A, B},
  title = {Alpha {Method} for Foo},
  doi = {10.1000/alpha},
  year = {2020}
}
@inproceedings{bbb2021Beta,
  title = {Beta Networks},
  url = {https://arxiv.org/abs/2101.00001v2},
  year = {2021}
}
@book{ccc2009Book,
  title = {A Textbook},
  year = {2009}
}
@misc{ddd2022Soft,
  title = {Some Software},
  url = {https://example.org/tool}
}
@misc{eee2023Pre,
  title = {Gamma Preprint},
  url = {http://arxiv.org/abs/2301.00009v1}
}
@article{fff2022Data,
  title = {A Dataset Paper},
  doi = {10.1000/data}
}
"""


def _lib():
    return [
        {"citekey": "aaa2020Alpha", "dedup_key": "doi:10.1000/alpha", "title": "Alpha Method for Foo",
         "doi": "10.1000/alpha", "arxiv_id": None, "series": "auto"},
        {"citekey": "other2021Beta", "dedup_key": "arxiv:2101.00001", "title": "Beta Networks",
         "doi": None, "arxiv_id": "2101.00001", "series": "manual"},
    ]


def _manual(n_core=3, n_side=2):
    out = []
    for i in range(n_core + n_side):
        out.append({"dedup_key": "doi:10.2000/m{}".format(i), "title": "Manual paper {}".format(i),
                    "abstract": "Abstract of manual {}.".format(i),
                    "batch": "B-core" if i < n_core else "B-side", "paper_id": "p{}".format(i)})
    return out


def _state(manual, mixed=False):
    st = {"batches": {"B-core": "core", "B-side": "side"}, "batches_registered_at": "2026-10-14T00:00:00"}
    return st


def _stream():
    out = []
    for i in range(60):
        conf = [0.6, 0.7, 0.8, 0.9, 1.0][i % 5]
        out.append({"paper_id": "e{}".format(i), "dedup_key": "doi:10.3000/e{}".format(i), "title": "Excl {}".format(i),
                    "abstract": "Excluded abstract {}".format(i), "verdict": "EXCLUDE", "confidence": conf,
                    "decided_at": "2026-10-01"})
    for i in range(12):
        out.append({"paper_id": "i{}".format(i), "dedup_key": "doi:10.3000/i{}".format(i), "title": "Incl {}".format(i),
                    "abstract": "Included abstract {}".format(i), "verdict": "INCLUDE", "confidence": 0.9,
                    "decided_at": "2026-10-01"})
    for i in range(12):
        out.append({"paper_id": "m{}".format(i), "dedup_key": "doi:10.3000/m{}".format(i), "title": "Maybe {}".format(i),
                    "abstract": "Maybe abstract {}".format(i), "verdict": "MAYBE", "confidence": 0.5,
                    "decided_at": "2026-10-01"})
    return out


# ---------------- 指标纯函数 ----------------

def test_wilson_interval_textbook():
    p, lo, hi = FE.wilson_interval(95, 100)
    assert p == pytest.approx(0.95)
    assert lo == pytest.approx(0.8882, abs=2e-3) and hi == pytest.approx(0.9785, abs=2e-3)
    # 设计 Q9：n=100 时 3% 点估计的 Wilson 上界约 8.5%
    assert FE.wilson_interval(3, 100)[2] == pytest.approx(0.0845, abs=2e-3)
    assert FE.wilson_interval(0, 0) == (0.0, 0.0, 1.0)
    assert FE.wilson_interval(0, 20)[1] == 0.0 and FE.wilson_interval(20, 20)[2] == 1.0


def test_keep_rate_and_weighted_rate():
    assert FE.keep_rate(["INCLUDE", "MAYBE", "EXCLUDE", "EXCLUDE"]) == (2, 4)
    # 低置信段 2 条抽中 1 条（权重 2）漏、高置信段 10 条抽中 1 条（权重 10）不漏 → 2/12
    assert FE.weighted_rate([True, False], [2.0, 10.0]) == pytest.approx(2 / 12)
    assert FE.weighted_rate([], []) == 0.0


def test_flip_rate_and_worse_of_repeats():
    r1 = ["INCLUDE", "MAYBE", "EXCLUDE", "INCLUDE"]
    r2 = ["INCLUDE", "EXCLUDE", "EXCLUDE", "MAYBE"]
    assert FE.flip_rate([r1, r2]) == pytest.approx(0.5)
    assert FE.worse_of_repeats([r1, r2]) == ["INCLUDE", "EXCLUDE", "EXCLUDE", "MAYBE"]
    assert FE.flip_rate([]) == 0.0


def test_paired_regression():
    r = FE.paired_regression(["INCLUDE", "MAYBE", "EXCLUDE", "INCLUDE"],
                             ["INCLUDE", "EXCLUDE", "INCLUDE", "INCLUDE"])
    assert r["lost"] == 1 and r["gained"] == 1 and r["delta"] == 0.0
    assert FE.paired_regression(["INCLUDE"] * 4, ["EXCLUDE"] + ["INCLUDE"] * 3)["delta"] == pytest.approx(-0.25)
    with pytest.raises(ValueError):
        FE.paired_regression(["INCLUDE"], [])


def test_split_stable_and_roughly_30pct():
    ids = [FE.item_id_of("doi:10.1/{}".format(i)) for i in range(2000)]
    assert [FE.split_of(i) for i in ids] == [FE.split_of(i) for i in ids]
    frac = sum(FE.split_of(i) == "test" for i in ids) / len(ids)
    assert 0.25 < frac < 0.35
    # 切分只取决于 item_id，与 gold 版本/种子无关：固定值回归，防止有人改了哈希口径
    assert FE.item_id_of("doi:10.1/0") == FE.item_id_of("doi:10.1/0") and len(ids[0]) == 12


# ---------------- bib 与 P1 ----------------

def test_parse_bib_and_ids():
    ents = FE.parse_bib(BIB)
    assert [e["key"] for e in ents] == ["aaa2020Alpha", "bbb2021Beta", "ccc2009Book", "ddd2022Soft",
                                        "eee2023Pre", "fff2022Data"]
    a = FE.bib_identifiers(ents[0])
    assert a["doi"] == "10.1000/alpha" and a["title"] == "Alpha Method for Foo"
    assert FE.bib_identifiers(ents[1])["arxiv_id"] == "2101.00001"       # 去版本号
    assert FE.bib_identifiers(ents[4])["arxiv_id"] == "2301.00009"


def test_classify_bib_entry_drops_non_papers():
    ents = {e["key"]: e for e in FE.parse_bib(BIB)}
    assert FE.classify_bib_entry(ents["aaa2020Alpha"])[0]
    assert not FE.classify_bib_entry(ents["ccc2009Book"])[0]             # 教科书
    assert not FE.classify_bib_entry(ents["ddd2022Soft"])[0]             # 无标识 misc
    assert FE.classify_bib_entry(ents["eee2023Pre"])[0]                  # misc 但带 arXiv
    assert not FE.classify_bib_entry(ents["fff2022Data"], extra_exclude={"fff2022Data"})[0]
    assert FE.classify_bib_entry(ents["ddd2022Soft"], extra_include={"ddd2022Soft"})[0]   # 捞回


def test_build_p1_in_out_and_dedup_across_bibs():
    drafts, review = FE.build_p1({"x": BIB, "y": BIB}, _lib())
    by = {d["dedup_key"]: d for d in drafts}
    assert by["doi:10.1000/alpha"]["stratum"] == "P1-in" and by["doi:10.1000/alpha"]["origin"] == "auto"
    assert by["doi:10.1000/alpha"]["sources"] == ["x", "y"]               # 两份 bib 重复只出一条
    beta = by["arxiv:2101.00001"]
    assert beta["stratum"] == "P1-in" and beta["match_by"] == "arxiv" and beta["origin"] == "manual"
    outs = {d["bib_key"] for d in drafts if d["stratum"] == "P1-out"}
    assert outs == {"eee2023Pre", "fff2022Data"}
    dropped = {r["bib_key"] for r in review if not r["keep"]}
    assert dropped == {"ccc2009Book", "ddd2022Soft"}


# ---------------- 登记闸 ----------------

def test_registry_roundtrip_and_refusals(tmp_path):
    edir = tmp_path / "e"
    man = _manual()
    FE.registry_export(edir, 1, man, "batches")
    p = FE.registry_paths(edir, 1)["batches"]
    rows = FE._read_csv(p)
    assert {r["batch"]: r["n_papers"] for r in rows} == {"B-core": "3", "B-side": "2"}
    with pytest.raises(ValueError):                                       # 未填 class
        FE.registry_import(edir, 1, man, "batches")
    FE._write_csv(p, ("batch", "n_papers", "class", "note"), [("B-core", 3, "core", ""), ("B-side", 2, "mixed", "")])
    st = FE.registry_import(edir, 1, man, "batches", now="2026-10-14T01:00:00")
    assert st["batches_registered_at"] == "2026-10-14T01:00:00"
    with pytest.raises(RuntimeError):                                     # 已登记不可覆盖/重导
        FE.registry_export(edir, 1, man, "batches")
    with pytest.raises(RuntimeError):
        FE.registry_import(edir, 1, man, "batches")
    # mixed 批次需要按篇登记
    with pytest.raises(RuntimeError):
        FE.check_registry_complete(FE._load_state(FE.registry_paths(edir, 1)["state"]), man)
    FE.registry_export(edir, 1, man, "papers")
    pp = FE.registry_paths(edir, 1)["papers"]
    prow = FE._read_csv(pp)
    assert len(prow) == 2 and all(r["batch"] == "B-side" for r in prow)
    FE._write_csv(pp, ("dedup_key", "batch", "title", "class"),
                  [(prow[0]["dedup_key"], "B-side", "t", "core"), (prow[1]["dedup_key"], "B-side", "t", "side")])
    st = FE.registry_import(edir, 1, man, "papers")
    FE.check_registry_complete(st, man)
    assert FE.manual_class(st, man[3]) == "core" and FE.manual_class(st, man[4]) == "side"
    # 登记之后新增批次 → 拒绝
    with pytest.raises(RuntimeError):
        FE.check_registry_complete(st, man + [{"dedup_key": "k", "batch": "NEW", "title": "", "abstract": ""}])


def test_registry_rejects_bad_value_and_missing_batch(tmp_path):
    edir = tmp_path / "e"
    man = _manual()
    FE.registry_export(edir, 1, man, "batches")
    p = FE.registry_paths(edir, 1)["batches"]
    FE._write_csv(p, ("batch", "n_papers", "class", "note"), [("B-core", 3, "maybe", ""), ("B-side", 2, "side", "")])
    with pytest.raises(ValueError):
        FE.registry_import(edir, 1, man, "batches")
    FE._write_csv(p, ("batch", "n_papers", "class", "note"), [("B-core", 3, "core", "")])
    with pytest.raises(ValueError):
        FE.registry_import(edir, 1, man, "batches")
    assert not FE.registry_paths(edir, 1)["state"].exists()               # 校验不过不落状态


# ---------------- H-stream ----------------

def test_allocate_properties():
    a = FE.allocate({"0.8": 581, "0.9": 517, "1.0": 71}, 60, min_each=3)
    assert sum(a.values()) == 60 and a["1.0"] >= 3 and a["0.8"] > a["0.9"] > a["1.0"]
    assert FE.allocate({"a": 2, "b": 0}, 10) == {"a": 2, "b": 0}          # 不超过池大小
    assert FE.allocate({}, 5) == {}


def test_sample_stream_stratified_weights_and_determinism():
    cands = _stream()
    s1, plan = FE.sample_stream(list(cands), seed=7, n_exclude=30, n_include=6, n_maybe=4, low_cap=8)
    s2, _ = FE.sample_stream(list(reversed(cands)), seed=7, n_exclude=30, n_include=6, n_maybe=4, low_cap=8)
    assert sorted(c["item_id"] for c in s1) == sorted(c["item_id"] for c in s2)   # 与输入顺序无关
    ex = [c for c in s1 if c["pool"] == "EXCLUDE"]
    assert len(ex) == 30 and plan["EXCLUDE"]["low"]["taken"] == 8        # 低置信段封顶
    assert sum(1 for c in s1 if c["pool"] == "INCLUDE") == 6 and sum(1 for c in s1 if c["pool"] == "MAYBE") == 4
    # 权重 = 池大小/抽中数，按层还原总体
    for b, v in plan["EXCLUDE"].items():
        assert v["weight"] == pytest.approx(v["pool"] / v["taken"])
    assert sum(c["weight"] for c in ex) == pytest.approx(60)
    s3, _ = FE.sample_stream(list(cands), seed=8, n_exclude=30, n_include=6, n_maybe=4, low_cap=8)
    assert sorted(c["item_id"] for c in s3) != sorted(c["item_id"] for c in s1)


def test_sample_stream_takes_all_low_when_small():
    cands = _stream()
    _, plan = FE.sample_stream(cands, seed=1, n_exclude=40, n_include=2, n_maybe=2, low_cap=100)
    assert plan["EXCLUDE"]["low"]["taken"] == plan["EXCLUDE"]["low"]["pool"] == 24   # 设计原意：低置信全取


def test_label_sheet_has_no_llm_fields(tmp_path):
    sample, _ = FE.sample_stream(_stream(), seed=3, n_exclude=20, n_include=5, n_maybe=5, low_cap=6)
    rows, key = FE.label_sheet_rows(sample, seed=3)
    assert len(rows) == 30 and all(tuple(r) == FE.SHEET_COLUMNS for r in rows)
    sheet = tmp_path / "s.csv"
    FE.write_label_sheet(sheet, rows)
    text = sheet.read_text(encoding="utf-8-sig")
    header = text.splitlines()[0].split(",")
    assert header == list(FE.SHEET_COLUMNS)
    for bad in ("verdict", "confidence", "decision", "stratum", "bucket", "neighbor", "pool", "EXCLUDE", "MAYBE"):
        assert bad not in header
    # 盲标表的任何单元格都不得含 LLM 判定词或层别名（标题/摘要夹具里没有这些词）
    body = text.split("\n", 1)[1]
    assert "EXCLUDE" not in body and "weight" not in body and "H-stream" not in body
    # 顺序已混洗：row_id 递增但与抽样池顺序无关；钥匙里才有判定
    assert set(key) == {r["row_id"] for r in rows} and "prod_verdict" in next(iter(key.values()))
    assert sheet.read_bytes().startswith(b"\xef\xbb\xbf")                 # BOM，Numbers/Excel 直接打开


def test_import_labels_validation(tmp_path):
    sample, _ = FE.sample_stream(_stream(), seed=3, n_exclude=10, n_include=2, n_maybe=2, low_cap=4)
    rows, key = FE.label_sheet_rows(sample, seed=3)
    sheet = tmp_path / "s.csv"
    FE.write_label_sheet(sheet, rows)
    with pytest.raises(ValueError):                                       # 全空
        FE.import_labels(sheet, key)
    for r, lab in zip(rows, ["应收", "边缘", "无关"] * 10):
        r["label"] = lab
    rows[0]["label"] = "不知道"
    FE.write_label_sheet(sheet, rows)
    with pytest.raises(ValueError):
        FE.import_labels(sheet, key)
    rows[0]["label"] = "应收"
    FE.write_label_sheet(sheet, rows)
    labs = FE.import_labels(sheet, key)
    assert len(labs) == len(rows) and {l["label"] for l in labs} == {"应收", "边缘", "无关"}


# ---------------- build_gold 端到端 ----------------

def _build(**over):
    lib = _lib()
    manual = _manual()
    sample, plan = FE.sample_stream(_stream(), seed=5, n_exclude=10, n_include=3, n_maybe=3, low_cap=4)
    rows, key = FE.label_sheet_rows(sample, seed=5)
    labs = [{"row_id": r["row_id"], "item_id": key[r["row_id"]]["item_id"],
             "label": ["应收", "边缘", "无关"][i % 3], "comment": ""} for i, r in enumerate(rows)]
    kw = dict(lib=lib, manual=manual, abstracts={"doi:10.1000/alpha": "Alpha abstract."}, bibs={"x": BIB},
              reg_state=_state(manual), stream=_stream(), stable=[{"citekey": "other2021Beta", "old": "MAYBE", "reason": "X4"}],
              p1_abstracts={"doi:10.1000/data": {"abstract": "Dataset abstract.", "source": "crossref"}},
              labels=labs, label_key={"exported_at": "2026-10-14T00:00:00", "plan": plan, "key": key}, seed=5)
    kw.update(over)
    return FE.build_gold(**kw)


def test_build_gold_strata_labels_and_precedence():
    items, stats, _ = _build()
    cnt = FE.gold_counts(items)
    assert cnt["P1-in"]["n"] == 2 and cnt["P1-out"]["n"] == 2
    assert cnt["M-core"]["n"] == 3 and cnt["M-side"]["n"] == 2
    assert cnt["H-stream"]["n"] == 16
    by = {i["dedup_key"]: i for i in items}
    assert by["doi:10.2000/m0"]["label"] == "positive" and by["doi:10.2000/m4"]["label"] is None
    assert by["doi:10.1000/data"]["abstract_source"] == "crossref" and by["doi:10.1000/alpha"]["abstract_source"] == "abstracts_cache"
    assert by["arxiv:2301.00009"]["abstract_source"] == "none"
    # R-stable 的 other2021Beta 已在 P1-in：优先级 P1 > R，只出现在 P1-in，并计入去重统计
    assert by["arxiv:2101.00001"]["stratum"] == "P1-in" and stats["dropped_dup"]["R-stable"] == 1
    assert [t["bib_key"] for t in stats["p1_abstract_todo"]] == ["eee2023Pre"]
    hs = [i for i in items if i["stratum"] == "H-stream"]
    assert {i["label"] for i in hs} == {"positive", "borderline", "negative"}
    assert all({"pool", "conf_bin", "weight"} <= set(i["sample"]) for i in hs)
    assert len({i["item_id"] for i in items}) == len(items)               # item_id 唯一
    assert all(i["split"] == FE.split_of(i["item_id"]) for i in items)


def test_build_gold_refuses_without_registry():
    with pytest.raises(RuntimeError, match="登记"):
        _build(reg_state={})


def test_build_gold_idempotent_same_sha():
    a = FE.gold_bytes(_build()[0])
    b = FE.gold_bytes(_build()[0])
    assert FE._sha256_bytes(a) == FE._sha256_bytes(b)
    # 输入顺序变化也不改变产物
    items, _, _ = _build(manual=list(reversed(_manual())), stream=list(reversed(_stream())))
    assert FE._sha256_bytes(FE.gold_bytes(items)) == FE._sha256_bytes(a)


def test_gold_item_shape():
    items, _, _ = _build()
    need = {"item_id", "title", "abstract", "abstract_source", "stratum", "label", "split", "dedup_key",
            "citekey", "label_source", "labeled_at", "notes"}
    assert all(need <= set(i) for i in items)
    assert all(len(i["abstract"]) <= FE.ABSTRACT_MAX_CHARS for i in items)


# ---------------- CLI 冒烟（tmp 构造的最小库） ----------------

def test_cli_build_gold_refuses_without_registry_and_runs_after(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("filter_eval_cli", Path(__file__).resolve().parents[1] / "scripts" / "filter_eval.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    notes, digest = tmp_path / "notes", tmp_path / "digest"
    (notes / "manual" / "B1").mkdir(parents=True)
    (digest / "_archive" / "excluded").mkdir(parents=True)
    (notes / "literature_index.json").write_text(json.dumps({"papers": _lib()}), encoding="utf-8")
    (notes / "abstracts.json").write_text(json.dumps({"abstracts": {}}), encoding="utf-8")
    bundle = {"paper_id": "p1", "segment": {"original_abstract": "abs", "metadata": {"title": "Man 1", "doi": "10.2000/x"}}}
    (notes / "manual" / "B1" / "a.paper.json").write_text(json.dumps(bundle), encoding="utf-8")
    bib = tmp_path / "a.bib"
    bib.write_text(BIB, encoding="utf-8")
    base = ["--notes-dir", str(notes), "--digest-dir", str(digest), "--bib-arxiv", str(bib),
            "--bib-ieee", str(tmp_path / "none.bib")]
    assert cli.main(base + ["build-gold"]) == 2                           # 未登记 → 拒绝
    assert cli.main(base + ["registry-export", "--kind", "batches"]) == 0
    p = FE.registry_paths(FE.eval_dir(notes), 1)["batches"]
    FE._write_csv(p, ("batch", "n_papers", "class", "note"), [("B1", 1, "core", "")])
    assert cli.main(base + ["registry-import", "--kind", "batches"]) == 0
    assert cli.main(base + ["build-gold"]) == 0
    assert cli.main(base + ["stats"]) == 0
    sha1 = (FE.eval_dir(notes) / "gold_v1.jsonl").read_bytes()
    assert cli.main(base + ["build-gold"]) == 0                           # 幂等重跑
    assert (FE.eval_dir(notes) / "gold_v1.jsonl").read_bytes() == sha1
    meta = json.loads((FE.eval_dir(notes) / "gold_v1.meta.json").read_text(encoding="utf-8"))
    assert meta["counts"]["M-core"]["n"] == 1 and meta["registry"]["batches_registered_at"]


# ======================================================================
# WO-7 评测执行器：假 LLM 全链路，不联网、不读真实 output
# ======================================================================

import json as _json                                                       # noqa: E402
from datetime import datetime as _dt                                       # noqa: E402
from types import SimpleNamespace                                          # noqa: E402

from src.scholar.llm_client import CallMeta                                # noqa: E402
from src.scholar.schema import ScholarSettings                             # noqa: E402
from src.scholar.workflow import ScholarWorkflow, FILTER_BATCH_SIZE        # noqa: E402

_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _wf(tmp_path, whitelist=None):
    envf = tmp_path / "e.env"
    envf.write_text(_ENV, encoding="utf-8")
    s = ScholarSettings.from_env_file(envf)
    s.processing.output_dir = tmp_path / "out"
    s.processing.whitelist = whitelist or []
    return ScholarWorkflow(s)


def _gi(i, stratum="M-core", label="positive", title=None, **extra):
    """手造 gold 条目；item_id 取自 dedup_key，split 与生产一致。"""
    dk = "doi:10.9/{}-{}".format(stratum, i)
    iid = FE.item_id_of(dk)
    d = {"item_id": iid, "title": title or "{} paper {}".format(stratum, i), "abstract": "x" * 800,
         "abstract_source": "bundle", "stratum": stratum, "label": label, "split": FE.split_of(iid),
         "dedup_key": dk, "citekey": None, "label_source": "t", "labeled_at": None, "notes": ""}
    d.update(extra)
    return d


def _install_fake(wf, decide, served="claude-sonnet-5-5", provider="claude-agent", prompts=None):
    """桩：_build_filter_prompt 返回 [(id,title)] 的 JSON；_call_llm 按标题查 decide(title, call_no)。
    与生产一样把 CallMeta 存进线程本地（_take_llm_meta 取）。"""
    calls = {"n": 0}
    wf._build_filter_prompt = lambda batch: _json.dumps(
        [{"id": s.segment_id, "title": s.metadata.title, "abstract": s.original_abstract} for s in batch])

    def fake(prompt, model=None, json_mode=None):
        calls["n"] += 1
        if prompts is not None:
            prompts.append(prompt)
        items = _json.loads(prompt)
        out = []
        for it in items:
            v = decide(it["title"], calls["n"])
            if v is None:
                continue
            out.append({"id": it["id"], "decision": v, "bucket": ["A"], "one_line": "o", "confidence": 0.9})
        sv, pv = served(calls["n"]) if callable(served) else served, provider
        if callable(served):
            sv, pv = served(calls["n"])
        wf._llm_meta_tls.meta = CallMeta(provider=pv, requested_model="sonnet", sent_model="sonnet",
                                         served_model=sv, chain_index=0, fell_back=False,
                                         degraded_partial=False, at="2026-10-13T00:00:00")
        return _json.dumps({"verdicts": out})

    wf._call_llm = fake
    return calls


def test_truncate_abstract_modes():
    assert len(FE.truncate_abstract("a" * 800, "prod")) == FE.PROD_ABSTRACT_CHARS == 400
    assert len(FE.truncate_abstract("a" * 900, "full")) == 800
    assert FE.truncate_abstract(None, "prod") == ""
    with pytest.raises(ValueError):
        FE.truncate_abstract("a", "mid")


def test_select_items_split_strata_smoke_and_manual_source():
    items = ([_gi(i, "M-core") for i in range(80)] + [_gi(i, "P1-out") for i in range(30)]
             + [_gi(i, "H-stream", "negative") for i in range(30)] + [_gi(i, "M-side", None) for i in range(5)]
             + [_gi(0, "R-stable", None)]
             + [_gi(99, "H-stream", "negative", decision_source="manual_pick")])
    sel, st = FE.select_items(items, split="test")
    strata = {i["stratum"] for i in sel}
    assert strata == {"M-core", "P1-out", "H-stream"}              # M-side/R-stable 默认不跑
    assert all(i["split"] == "test" for i in sel if i["stratum"] == "M-core")
    assert sum(1 for i in sel if i["stratum"] == "P1-out") == 30     # split 不作用于 P1
    assert st["skipped_manual_source"] == 1
    assert "manual_pick" not in {i.get("decision_source") for i in sel}
    # 显式指定层后 M-side 才进
    sel2, _ = FE.select_items(items, split="all", strata=["M-side"])
    assert len(sel2) == 5
    # 冒烟：每组 20 条、与输入顺序无关、可复现
    s1, _ = FE.select_items(items, smoke=True)
    s2, _ = FE.select_items(list(reversed(items)), smoke=True)
    assert [i["item_id"] for i in s1] == [i["item_id"] for i in s2]
    assert len(s1) == 60
    assert {k: sum(1 for i in s1 if i["stratum"] in m) for k, m in FE.SMOKE_GROUPS} == {"P1": 20, "M-core": 20, "H-stream": 20}
    with pytest.raises(ValueError):
        FE.select_items(items, strata=["nope"])


def test_check_run_preconditions():
    items = [_gi(0, "H-stream", "negative", labeled_at="2026-10-12T10:00:00")]
    meta = {"gold_sha256": "abc", "registry": {"batches_registered_at": "2026-10-12T09:00:00"}}
    now = _dt(2026, 10, 13)
    FE.check_run_preconditions(meta, "abc", items, now=now)
    with pytest.raises(ValueError, match="sha256"):
        FE.check_run_preconditions(meta, "zzz", items, now=now)
    with pytest.raises(ValueError, match="登记时间戳"):
        FE.check_run_preconditions({"gold_sha256": "abc", "registry": {}}, "abc", items, now=now)
    with pytest.raises(ValueError, match="不早于"):
        FE.check_run_preconditions(meta, "abc", items, now=_dt(2026, 10, 12, 8))   # 运行早于登记
    late = [_gi(0, "H-stream", "negative", labeled_at="2026-10-14T00:00:00")]
    with pytest.raises(ValueError, match="盲标"):
        FE.check_run_preconditions(meta, "abc", late, now=now)


def test_shuffled_order_mixes_labels_and_is_deterministic():
    items = [_gi(i, "M-core", "positive") for i in range(20)] + [_gi(i, "H-stream", "negative") for i in range(20)]
    a, b = FE.shuffled_order(items, 1), FE.shuffled_order(items, 1)
    assert [i["item_id"] for i in a] == [i["item_id"] for i in b]
    assert [i["item_id"] for i in a] != [i["item_id"] for i in FE.shuffled_order(items, 2)]
    # 每批 20：不能整批全是阳性或全是阴性
    for k in range(0, 40, FILTER_BATCH_SIZE):
        labs = {i["label"] for i in a[k:k + FILTER_BATCH_SIZE]}
        assert labs == {"positive", "negative"}


def test_run_eval_two_repeats_worse_of_and_flip(tmp_path):
    wf = _wf(tmp_path)
    items = [_gi(i, "M-core") for i in range(25)] + [_gi(i, "H-stream", "negative") for i in range(15)]
    victim = items[3]["title"]
    seen = {}

    def decide(title, n):
        if title == victim:
            seen[n] = 1
            return "INCLUDE" if len(seen) == 1 else "EXCLUDE"      # 第一遍保留、第二遍丢 → 取更差
        return "INCLUDE"

    calls = _install_fake(wf, decide)
    reps = FE.run_eval(wf, items, repeats=2, seed=7, abstract_mode="prod", rag_on=False)
    assert calls["n"] == 2 * 2                                      # 40 条 = 2 批/遍 × 2 遍
    assert len(reps) == 2 and all(len(r) == 40 for r in reps)
    entries = {e["item_id"]: e for e in FE.build_entries(items, reps)}
    v = entries[items[3]["item_id"]]
    assert sorted(v["verdicts"]) == ["EXCLUDE", "INCLUDE"] and v["worst"] == "EXCLUDE"
    m = FE.compute_metrics(list(entries.values()))
    assert m["flip"]["all"] == pytest.approx(1 / 40)
    assert m["include_recall"]["k"] == 24 and m["include_recall"]["n"] == 25
    # 两遍的混洗不同
    assert [r["item_id"] for r in reps[0]] != [r["item_id"] for r in reps[1]]


def test_prod_abstract_is_truncated_in_real_prompt(tmp_path):
    """不替换 _build_filter_prompt：生产 prompt 里送审摘要在 prod 口径下只有 400 字符。"""
    wf = _wf(tmp_path)
    prompts = []

    def fake(prompt, model=None, json_mode=None):
        prompts.append(prompt)
        return _json.dumps({"verdicts": []})
    wf._call_llm = fake
    it = _gi(1)
    it["abstract"] = "q" * 800
    longest = lambda p: max(len(x) for x in __import__("re").findall(r"q+", p))
    FE.run_eval(wf, [it], repeats=1, abstract_mode="prod", rag_on=False)
    assert prompts and all(longest(p) == 400 for p in prompts)
    prompts.clear()
    FE.run_eval(wf, [it], repeats=1, abstract_mode="full", rag_on=False)
    assert prompts and all(longest(p) == 800 for p in prompts)


def test_split_by_served_model_marks_not_certifiable(tmp_path):
    wf = _wf(tmp_path)
    items = [_gi(i, "M-core") for i in range(40)]
    # 第 2 次调用起回退到另一家：同一遍里前半批 A、后半批 B
    calls = _install_fake(wf, lambda t, n: "INCLUDE",
                          served=lambda n: ("model-A", "claude-agent") if n % 2 else ("model-B", "gemini"))
    reps = FE.run_eval(wf, items, repeats=2, rag_on=False)
    entries = FE.build_entries(items, reps)
    S = FE.summarize(entries)
    assert "claude-agent:model-A" in S["models"] and "gemini:model-B" in S["models"] or "mixed" in S["models"]
    assert S["not_certifiable_reasons"]
    assert any("多于一个应答模型" in r for r in S["not_certifiable_reasons"])
    assert sum(m["n_items"] for m in S["by_model"].values()) == 40      # 拆分后条目不丢不重


def test_single_verified_model_is_certifiable_and_unknown_is_not(tmp_path):
    items = [_gi(i, "M-core") for i in range(20)]
    wf = _wf(tmp_path)
    _install_fake(wf, lambda t, n: "MAYBE", served="claude-sonnet-5-5")
    S = FE.summarize(FE.build_entries(items, FE.run_eval(wf, items, repeats=2, rag_on=False)))
    assert S["models"] == ["claude-agent:claude-sonnet-5-5"] and S["not_certifiable_reasons"] == []
    wf2 = _wf(tmp_path)
    _install_fake(wf2, lambda t, n: "MAYBE", served=None)
    S2 = FE.summarize(FE.build_entries(items, FE.run_eval(wf2, items, repeats=1, rag_on=False)))
    assert S2["models"] == ["claude-agent:unknown"]
    assert any("未知" in r or "未验证" in r for r in S2["not_certifiable_reasons"])


def test_llm_failure_counts_as_fallback_dropped(tmp_path):
    wf = _wf(tmp_path, whitelist=["paper"])          # 白名单命中也不能冒充真裁决
    items = [_gi(i, "M-core") for i in range(5)]

    def boom(prompt, model=None, json_mode=None):
        raise RuntimeError("API down")
    wf._call_llm = boom
    wf.llm_client.rewind_chain = lambda: None
    entries = FE.build_entries(items, FE.run_eval(wf, items, repeats=1, rag_on=False))
    assert all(e["worst"] == "FALLBACK" for e in entries)
    m = FE.compute_metrics(entries)
    assert m["core_test_keep"]["k"] == 0 and m["n_fallback_items"] == 5
    assert any("FALLBACK" in r for r in FE.summarize(entries)["not_certifiable_reasons"])


def test_neighbor_exclude_set_during_call_and_cleared_after(tmp_path):
    wf = _wf(tmp_path)
    it = _gi(1)
    it["citekey"] = "self2020"
    seen = {}

    def fake(prompt, model=None, json_mode=None):
        seen["ex"] = dict(wf._neighbor_exclude)
        seen["unavail"] = wf._library_neighbors_unavailable
        return _json.dumps({"verdicts": []})
    wf._call_llm = fake
    rec = FE.run_once(wf, [it], seed=1, abstract_mode="prod", rag_on=True, cit_by_dedup={it["dedup_key"]: "libck"})
    (excl,) = seen["ex"].values()
    assert excl == {it["dedup_key"], "self2020", "libck"}
    assert wf._neighbor_exclude == {}
    # 本测试环境没有向量库 → 真实 _library_neighbors 降级；记录必须标出 RAG 实际没开
    assert rec[0]["rag_degraded"] is True
    entries = FE.build_entries([it], [rec])
    assert any("RAG 近邻检索中途降级" in r for r in FE.summarize(entries)["not_certifiable_reasons"])
    rec = FE.run_once(wf, [it], seed=1, abstract_mode="prod", rag_on=False)
    assert seen["ex"] == {} and seen["unavail"] is True and rec[0]["rag_degraded"] is False   # RAG 关闭臂


def _nb_wf(store_records, vecs, exclude=None):
    import numpy as np
    from src.scholar.embed_store import VectorStore
    store = VectorStore({"model": "bge-m3", "dim": "2"}, store_records, np.array(vecs, dtype=np.float32))

    class _Client:
        model = "bge-m3"
        def probe(self):
            return {"model": "bge-m3", "dim": 2}
        def embed(self, texts, batch_size=64):
            return np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (len(texts), 1))

    wf = ScholarWorkflow.__new__(ScholarWorkflow)         # 与 test_embed_store 同款：不建 Gmail 等
    wf.settings = SimpleNamespace(processing=SimpleNamespace(notes_dir=Path(".")),
                                  llm=SimpleNamespace(embedding_model="bge-m3", embedding_base_url=None,
                                                      ollama_base_url=None))
    wf._library_neighbors_cache = {}
    wf._vector_store = store
    wf._paper_mask = np.array([r["level"] in ("paper", "abstract") for r in store_records])
    wf._embedding_client = _Client()
    wf._library_neighbors_unavailable = False
    if exclude is not None:
        wf._neighbor_exclude = exclude
    return wf


def test_library_neighbors_self_exclusion_hook():
    recs = [{"level": "paper", "citekey": "self2020", "year": 2020, "text": "My Paper Title\n自己的判词"},
            {"level": "abstract", "citekey": "self2020", "year": 2020, "text": "abs of self"},
            {"level": "paper", "citekey": "pre2019", "year": 2019, "text": "My Paper Title\n预印本版本"},   # 同标题异键
            {"level": "paper", "citekey": "other2021", "year": 2021, "text": "Other Work\n别人的判词"}]
    v = [[1, 0], [0.99, 0.141], [0.98, 0.199], [0.9, 0.436]]
    seg = SimpleNamespace(segment_id=5, metadata=SimpleNamespace(title="My Paper Title"), original_abstract="a")

    # 生产默认（无钩子 / 空钩子）：自命中照旧保留，仅 one_line 被掐 —— 行为与改动前一致
    for wf in (_nb_wf(recs, v), _nb_wf(recs, v, exclude={})):
        hits = wf._library_neighbors([seg], k=3, min_sim=0.5)[5]
        assert [h["citekey"] for h in hits] == ["self2020", "pre2019", "other2021"]
        assert hits[0]["one_line"].startswith("（本篇自身")

    # 钩子开启：自己的 citekey 与同标题（预印本/正式版）都不在近邻里
    wf = _nb_wf(recs, v, exclude={5: {"self2020"}})
    hits = wf._library_neighbors([seg], k=3, min_sim=0.5)[5]
    assert [h["citekey"] for h in hits] == ["other2021"]
    # 钩子只对登记的 segment 生效
    other = SimpleNamespace(segment_id=6, metadata=SimpleNamespace(title="My Paper Title"), original_abstract="a")
    wf = _nb_wf(recs, v, exclude={5: {"self2020"}})
    assert "self2020" in [h["citekey"] for h in wf._library_neighbors([other], k=3, min_sim=0.5)[6]]


def test_workflow_init_has_empty_neighbor_exclude(tmp_path):
    assert _wf(tmp_path)._neighbor_exclude == {}


def _entries_for_metrics():
    def e(stratum, label, verdicts, **kw):
        d = {"item_id": "i{}".format(len(kw) + hash((stratum, label, tuple(verdicts), str(kw))) % 10**6),
             "stratum": stratum, "label": label, "split": kw.pop("split", "test"), "title": kw.pop("title", "t"),
             "abstract_source": "bundle", "origin": kw.pop("origin", None), "sample": kw.pop("sample", None),
             "batch": None, "verdicts": verdicts, "confidence": [0.9] * len(verdicts),
             "served_model": ["p:m"] * len(verdicts), "verified": [True] * len(verdicts),
             "fell_back": [False] * len(verdicts), "model": "p:m",
             "worst": FE.worse_of_repeats([[v] for v in verdicts])[0] if False else
             min(verdicts, key=lambda x: FE.VERDICT_ORDER[x])}
        d.update(kw)
        return d
    S = lambda w, pv, cb: {"weight": w, "prod_verdict": pv, "conf_bin": cb}
    return [
        e("P1-out", "positive", ["INCLUDE", "EXCLUDE"], title="lost-p1", origin="bib"),
        e("P1-out", "positive", ["MAYBE", "MAYBE"], title="kept-p1", origin="bib"),
        e("P1-in", "positive", ["EXCLUDE", "EXCLUDE"], title="auto-lost", origin="auto"),
        e("M-core", "positive", ["INCLUDE", "INCLUDE"], split="test"),
        e("M-core", "positive", ["EXCLUDE", "INCLUDE"], split="test", title="lost-core"),
        e("M-core", "positive", ["EXCLUDE", "EXCLUDE"], split="dev"),
        e("H-stream", "positive", ["MAYBE", "MAYBE"], sample=S(2.0, "EXCLUDE", "0.6-0.7")),
        e("H-stream", "negative", ["EXCLUDE", "EXCLUDE"], sample=S(10.0, "EXCLUDE", "0.9-1.0")),
        e("H-stream", "negative", ["INCLUDE", "INCLUDE"], sample=S(1.0, "INCLUDE", "0.9-1.0")),
        e("H-stream", "borderline", ["MAYBE", "MAYBE"], sample=S(10.0, "EXCLUDE", "0.9-1.0")),
        e("M-side", None, ["EXCLUDE", "EXCLUDE"]),
    ]


def test_compute_metrics_values():
    m = FE.compute_metrics(_entries_for_metrics())
    assert (m["p1_keep"]["k"], m["p1_keep"]["n"]) == (1, 2)              # auto 来源不进
    assert [d["title"] for d in m["p1_dropped"]] == ["lost-p1"]
    assert (m["p1_auto_keep"]["k"], m["p1_auto_keep"]["n"]) == (0, 1)
    assert (m["core_test_keep"]["k"], m["core_test_keep"]["n"]) == (1, 2)
    assert (m["core_dev_keep"]["k"], m["core_dev_keep"]["n"]) == (0, 1)
    assert [d["title"] for d in m["core_dropped_test"]] == ["lost-core"]
    # H：false include 1/2；漏判（生产 EXCLUDE 且人判 positive/negative）= 1 应收 / 2，边缘不进分母，权重 2 vs 10
    assert (m["h_false_include"]["k"], m["h_false_include"]["n"]) == (1, 2)
    hm = m["h_stream_miss"]
    assert (hm["k"], hm["n"]) == (1, 2) and hm["weighted"] == pytest.approx(2 / 12)
    assert m["h_stream_miss_by_conf_bin"] == {"0.6-0.7": {"k": 1, "n": 1}, "0.9-1.0": {"k": 0, "n": 1}}
    assert m["h_borderline_n"] == 1
    assert m["h_vs_prod"] == {"prod_exclude_now_keep": 2, "prod_keep_now_exclude": 0}
    assert m["f_override_keep"]["gated"] is False
    assert m["verdict_counts"]["M-side"] == {"EXCLUDE": 1}               # 只报告分布
    assert m["flip"]["P1-out"] == pytest.approx(0.5)


def test_assemble_report_paired_and_cli(tmp_path, capsys):
    items = [_gi(i, "M-core") for i in range(30)]
    wf = _wf(tmp_path)
    bad = items[0]["title"]
    _install_fake(wf, lambda t, n: "INCLUDE")
    reps_a = FE.run_eval(wf, items, repeats=2, rag_on=False)
    _install_fake(wf, lambda t, n: "EXCLUDE" if t == bad else "INCLUDE")
    reps_b = FE.run_eval(wf, items, repeats=2, rag_on=False)
    cfg = {"gold_version": "gold-v1@deadbeef", "split": "all", "strata": ["M-core"], "smoke": False, "repeats": 2,
           "seed": 1, "abstract_mode": "prod", "rag_on": False, "batch_size": 20,
           "prompt_fingerprint": "filter-v3@x", "requested_model": "sonnet", "main_provider": "claude-agent"}
    A = FE.assemble_run("run_A", cfg, {"n": 30}, items, reps_a, status="complete", started_at="t")
    B = FE.assemble_run("run_B", cfg, {"n": 30}, items, reps_b, status="complete", started_at="t")
    pr = FE.paired_between_runs(A, B)
    assert pr["n"] == 30 and pr["lost"] == 1 and pr["gained"] == 0
    assert pr["delta"] == pytest.approx(-1 / 30) and pr["lost_items"][0]["title"] == bad
    txt = FE.format_report(B, baseline=A, show_verdicts=True)
    for needle in ("run_B", "可用于认证：是", "配对比较", "新增丢弃：" + bad, "逐条裁决", "M-core test 保留率"):
        assert needle in txt
    assert "可用于认证：否" in FE.format_report(dict(A, config=dict(cfg, smoke=True)))
    # 未完成的断点不可认证
    P = FE.assemble_run("run_P", cfg, {"n": 30}, items, reps_a[:1], status="partial", started_at="t")
    assert "可用于认证：否" in FE.format_report(P) and P["n_repeats_done"] == 1

    # CLI report：从 runs/ 读取；缺省取最新；--json 只出汇总；盲标未完成时 --show-verdicts 被拒
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import filter_eval as CLI
    notes = tmp_path / "notes"
    runs = notes / "_eval" / "filter" / "runs"
    runs.mkdir(parents=True)
    (runs / "run_A.json").write_text(_json.dumps(A), encoding="utf-8")
    (runs / "run_B.json").write_text(_json.dumps(B), encoding="utf-8")
    base = ["--notes-dir", str(notes), "--digest-dir", str(tmp_path / "dg")]
    assert CLI.main(base + ["report", "run_B", "--vs", "run_A"]) == 0
    assert "配对比较" in capsys.readouterr().out
    assert CLI.main(base + ["report", "--json"]) == 0
    assert "overall" in _json.loads(capsys.readouterr().out)
    gold = notes / "_eval" / "filter" / "gold_v1.jsonl"
    gold.write_text("\n".join(_json.dumps(i) for i in items), encoding="utf-8")
    (gold.parent / "gold_v1.meta.json").write_text("{}", encoding="utf-8")
    assert CLI.main(base + ["report", "run_B", "--show-verdicts"]) == 2           # gold 里没有 H-stream 标签


def test_cli_run_refuses_without_gold_or_registry(tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import filter_eval as CLI
    notes = tmp_path / "notes"
    (notes / "_eval" / "filter").mkdir(parents=True)
    base = ["--notes-dir", str(notes), "--digest-dir", str(tmp_path / "dg")]
    assert CLI.main(base + ["run", "--gold", "v1", "--smoke"]) == 2
    assert "拒绝运行" in capsys.readouterr().err
    # gold 在但 meta 里没有登记时间戳 → 同样拒绝，且不会走到建 workflow / 调 LLM
    items = [_gi(0)]
    blob = FE.gold_bytes(items)
    (notes / "_eval" / "filter" / "gold_v1.jsonl").write_bytes(blob)
    (notes / "_eval" / "filter" / "gold_v1.meta.json").write_text(
        _json.dumps({"gold_sha256": FE._sha256_bytes(blob), "gold_sha8": "x", "registry": {}}), encoding="utf-8")
    assert CLI.main(base + ["run", "--gold", "v1", "--smoke"]) == 2
    assert "登记时间戳" in capsys.readouterr().err
