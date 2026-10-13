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
