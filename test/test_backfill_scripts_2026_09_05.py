# -*- coding: utf-8 -*-
"""两个存量处置脚本的纯逻辑（不触网、不写生产目录）：

- scripts/backfill_article_numbers.py：只碰「缺 page 且带非 arXiv DOI 的期刊条目」，只改 page 一个键；
- scripts/inventory_missing_ids.py：三分类 + 建议放弃的判据。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.backfill_article_numbers as BA          # noqa: E402
import scripts.inventory_missing_ids as INV             # noqa: E402


# ---------------- backfill_article_numbers ----------------

def test_is_candidate_only_pageless_journal_articles_with_real_doi():
    assert BA.is_candidate({"type": "article-journal", "DOI": "10.1/x"})
    assert BA.is_candidate({"type": "article-journal", "DOI": "10.1/x", "page": "  "})
    assert not BA.is_candidate({"type": "article-journal", "DOI": "10.1/x", "page": "e10308"})
    assert not BA.is_candidate({"type": "article-journal", "DOI": "10.48550/arXiv.2301.00001"})
    assert not BA.is_candidate({"type": "paper-conference", "DOI": "10.1/x"})
    assert not BA.is_candidate({"type": "article-journal"})
    assert not BA.is_candidate("garbage")


def test_plan_and_apply_updates_touch_only_page():
    items = [
        {"id": "a", "type": "article-journal", "DOI": "10.1/a", "title": "A"},
        {"id": "b", "type": "article-journal", "DOI": "10.1/b", "title": "B"},
        {"id": "c", "type": "article-journal", "DOI": "10.1/c", "page": "12-19"},
        {"id": "d", "type": "book", "DOI": "10.1/d"},
    ]
    table = {"10.1/a": "e10308", "10.1/b": None}
    updates, n_cand, n_none, n_unchecked, n_unreach = BA.plan_updates(items, lambda doi: table.get(doi))
    assert updates == [(0, "e10308")] and n_cand == 2 and n_none == 1 and n_unchecked == 0
    before = json.dumps(items[1:], sort_keys=True)
    BA.apply_updates(items, updates)
    assert items[0]["page"] == "e10308" and set(items[0]) == {"id", "type", "DOI", "title", "page"}
    assert json.dumps(items[1:], sort_keys=True) == before, "其余条目一个字节不动"


def test_load_supports_array_and_wrapped_shapes(tmp_path):
    arr = tmp_path / "a.references.json"
    arr.write_text(json.dumps([{"id": "x"}]), encoding="utf-8")
    items, wrapper = BA._load(arr)
    assert items == [{"id": "x"}] and wrapper is None
    wrapped = tmp_path / "b.references.json"
    wrapped.write_text(json.dumps({"references": [{"id": "y"}]}), encoding="utf-8")
    items2, wrapper2 = BA._load(wrapped)
    assert items2 == [{"id": "y"}] and wrapper2[1] == "references"
    bad = tmp_path / "c.references.json"
    bad.write_text("42", encoding="utf-8")
    with pytest.raises(ValueError):
        BA._load(bad)


def test_atomic_write_replaces_file(tmp_path):
    p = tmp_path / "r.references.json"
    p.write_text("[]", encoding="utf-8")
    BA._atomic_write(p, [{"id": "z", "page": "1"}])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"id": "z", "page": "1"}]
    assert not (tmp_path / "r.references.json.tmp").exists()


# ---------------- inventory_missing_ids ----------------

def test_family_name_and_first_author_match():
    assert INV.family_name("Xiaoli Bird") == INV.family_name("Bird, Xiaoli") == INV.family_name("X. Bird")
    assert INV.first_author_matches(["Xiaoli Bird", "Other"], ["Bird, X."]) is True
    assert INV.first_author_matches(["Xiaoli Bird"], ["Someone Else"]) is False
    assert INV.first_author_matches([], ["Bird"]) is None
    assert INV.first_author_matches(["Bird"], []) is None


def test_year_ok_tolerates_one_year():
    assert INV.year_ok(2024, 2025) is True and INV.year_ok(2024, 2026) is False
    assert INV.year_ok(None, 2024) is None and INV.year_ok("x", 2024) is None


def _entry(**kw):
    e = {"citekey": "wang2024Federated", "title": "Federated learning for missing data in electronic health records",
         "year": 2024, "authors": ["Wei Wang", "Li Zhang"], "decision": "INCLUDE", "dedup_key": "title:federated..."}
    e.update(kw)
    return e


def _hit(sim, **kw):
    h = {"source": "crossref", "title": "Federated learning for missing data in electronic health records",
         "doi": "10.1000/abc", "year": 2024, "authors": ["Wei Wang", "Li Zhang"], "sim": sim}
    h.update(kw)
    return h


def test_classify_fillable_requires_title_year_and_author():
    cat, best, drop = INV.classify(_entry(), [_hit(0.98)])
    assert cat == "fillable" and best["doi"] == "10.1000/abc" and drop == ""
    # 年份差 2 → ambiguous
    assert INV.classify(_entry(), [_hit(0.98, year=2022)])[0] == "ambiguous"
    # 首作者对不上且相似度不够高 → ambiguous
    assert INV.classify(_entry(), [_hit(0.93, authors=["Someone Else"])])[0] == "ambiguous"
    # 首作者对不上但标题几乎逐字相同 → 仍 fillable（元数据首作者本就常错）
    assert INV.classify(_entry(), [_hit(0.995, authors=["Someone Else"])])[0] == "fillable"
    # 命中没有 DOI → 等于没命中
    assert INV.classify(_entry(), [_hit(0.99, doi=None)])[0] == "no_hit"
    assert INV.classify(_entry(), [])[0] == "no_hit"


def test_classify_flags_drop_candidates_but_keeps_the_hit():
    cat, best, drop = INV.classify(_entry(dedup_key="pmlr:v238/wang24a"), [_hit(0.98)])
    assert cat == "fillable" and "pmlr" in drop
    cat2, _, drop2 = INV.classify(_entry(decision="MAYBE"), [_hit(0.93)])
    assert cat2 == "fillable" and "MAYBE" in drop2
    cat3, best3, drop3 = INV.classify(_entry(title="Missing Data Imputation"), [_hit(0.99)])
    assert cat3 == "ambiguous" and best3 is None and "实词不足" in drop3


def test_select_targets_filters_identified_and_books():
    index = {"papers": [
        {"citekey": "a", "title": "T a", "dedup_key": "title:a", "month": "2026-01", "duplicate_of": None},
        {"citekey": "b", "title": "T b", "doi": "10.1/b", "dedup_key": "doi:10.1/b", "month": "2026-01", "duplicate_of": None},
        {"citekey": "c", "title": "T c", "arxiv_id": "2401.1", "dedup_key": "arxiv:2401.1", "month": "2026-01", "duplicate_of": None},
        {"citekey": "d", "title": "T d", "dedup_key": "isbn:1", "entry_type": "book", "month": "2026-01", "duplicate_of": None},
        {"citekey": "e", "title": "T e", "dedup_key": "title:e", "month": "2026-02", "duplicate_of": "title:a@2026-01"},
    ]}
    got = [e["citekey"] for e in INV.select_targets(index)]
    assert got == ["a"]


def test_render_md_lists_each_category():
    recs = [
        {"citekey": "k1", "category": "fillable", "decision": "INCLUDE", "year": 2024,
         "best": {"source": "crossref", "sim": 0.98, "doi": "10.1/a", "year": 2024, "authors": ["A"]},
         "authors": ["A"], "drop_reason": ""},
        {"citekey": "k2", "category": "ambiguous", "year": 2020,
         "best": {"source": "openalex", "sim": 0.9, "doi": "10.1/b", "year": 2017, "authors": ["B"]},
         "authors": ["Z"], "drop_reason": ""},
        {"citekey": "k3", "category": "no_hit", "best": None, "authors": [], "drop_reason": "裁决为 MAYBE"},
    ]
    md = INV.render_md(recs, "20260905T000000")
    assert "fillable 1 / ambiguous 1 / no_hit 1" in md
    assert "| k1 |" in md and "| k2 |" in md and "k3" in md
    assert "改身份键" in md


# ---------------- 第 1 轮审计补的守卫 ----------------

def test_classify_two_sources_disagreeing_on_doi_is_ambiguous():
    """R1-3.4：两源命中不同 DOI 是「要人看」的最强信号，不能按相似度取第一个当 fillable。"""
    cat, best, note = INV.classify(_entry(), [_hit(0.99), _hit(0.98, source="openalex", doi="10.48550/arxiv.2401.1")])
    assert cat == "ambiguous" and "两源 DOI 不一致" in note and best is not None
    # 同一 DOI（大小写/前缀不同）不算分歧
    cat2, _, _ = INV.classify(_entry(), [_hit(0.99), _hit(0.98, source="openalex", doi="https://doi.org/10.1000/ABC")])
    assert cat2 == "fillable"


def test_classify_maybe_only_dropped_when_similarity_is_not_high():
    """R1-3.6：MAYBE 裁决**且相似度不够高**才建议放弃；标题逐字命中的 MAYBE 留给人判断。"""
    cat, _, drop = INV.classify(_entry(decision="MAYBE"), [_hit(0.99)])
    assert cat == "fillable" and drop == ""
    cat2, _, drop2 = INV.classify(_entry(decision="MAYBE"), [_hit(0.93)])
    assert cat2 == "fillable" and "MAYBE" in drop2 and "0.93" in drop2
    cat3, _, drop3 = INV.classify(_entry(decision="MAYBE"), [])
    assert cat3 == "no_hit" and "MAYBE" in drop3


def test_select_targets_excludes_retracted_isbn_and_pmid_keys():
    """R1-3.7：撤稿不进清单；isbn:/pmid: 键按 dedup_key 前缀认（索引里没有 pmid 字段）。"""
    base = {"title": "Some long enough title words here", "month": "2026-01", "duplicate_of": None}
    index = {"papers": [
        dict(base, citekey="a", dedup_key="title:a"),
        dict(base, citekey="r", dedup_key="title:r", flags=[__import__("src.scholar.notes_index", fromlist=["RETRACTED_FLAG"]).RETRACTED_FLAG]),
        dict(base, citekey="i", dedup_key="isbn:123"),
        dict(base, citekey="p", dedup_key="pmid:999"),
    ]}
    got = [e["citekey"] for e in INV.select_targets(index)]
    assert "a" in got and "i" not in got and "p" not in got
    assert "r" not in got, "撤稿条目不该进盘点清单"


def test_plan_updates_counts_unchecked_separately():
    """R1-3.3：--limit 用完的候选是「未回查」，不是「源里也没有」。"""
    items = [{"id": k, "type": "article-journal", "DOI": "10.1/" + k} for k in "abcd"]
    budget = [2]

    def lookup(doi):
        if budget[0] <= 0:
            return BA.UNCHECKED
        budget[0] -= 1
        return "e1" if doi.endswith("a") else None
    updates, n_cand, n_none, n_unchecked, n_unreach = BA.plan_updates(items, lookup)
    assert n_cand == 4 and updates == [(0, "e1")] and n_none == 1 and n_unchecked == 2


def test_make_lookup_reports_unchecked_when_budget_spent():
    """R2-3.13：main() 里真正跑的 lookup 闭包此前无测试——把 `return UNCHECKED` 改回 `return None`
    没有任何用例能拦，`--limit 20` 试跑又会把「未回查」计成「源里也没有」。抽出 make_lookup 直测。"""
    budget = [1]
    calls = []

    def fetch(doi):
        calls.append(doi)
        return {"pages": "e10308"}
    lookup = BA.make_lookup(fetch, budget, {}, delay=0)
    assert lookup("10.1/a") == "e10308"          # 第 1 次：真回查、耗掉额度
    assert lookup("10.1/b") is BA.UNCHECKED      # 额度用完：未回查，不是 None
    assert calls == ["10.1/a"]
    assert lookup("10.1/a") == "e10308"          # 已在缓存：即便额度 0 也直接命中、不再触网


def test_make_lookup_caches_none_but_not_unchecked():
    """源里没有定位号（None）要缓存住，别重复回查同一条；未回查（UNCHECKED）绝不进缓存。"""
    seen = []
    budget = [2]

    def fetch(doi):
        seen.append(doi)
        return {}                                # 无 page → None
    lookup = BA.make_lookup(fetch, budget, {}, delay=0)
    assert lookup("10.1/a") is None and lookup("10.1/a") is None
    assert seen == ["10.1/a"], "None 命中缓存，同 DOI 不再回查"
    # fetch 抛异常 → **UNREACHABLE**，不是 None。
    # 2026-09-05 订正：这里原本断言的是 `is None`，等于把「没问成 = 源里也没有」这个缺陷
    # 钉成了契约（对抗审计 Lens C）。异常是「没答上」，必须与「问了确实没有」分开。
    def boom(doi):
        raise ConnectionError("x")
    l2 = BA.make_lookup(boom, [1], {}, delay=0)
    assert l2("10.1/z") is BA.UNREACHABLE


# ---------------- 源没答上 ≠ 源里没有（2026-09-05 生产实跑发现） ----------------

def test_classify_incomplete_when_a_source_never_answered():
    """OpenAlex 改成按日额度计费后，额度耗尽的那半批若判 no_hit，等于把「压根没问成」
    沉淀成「问过了确实没有」——与本批 R3-A1（5xx 被当成功）是同一个病。"""
    assert INV.classify(_entry(), [], None)[0] == "no_hit", "两源都答了、都没命中 → no_hit"
    cat, best, _ = INV.classify(_entry(), [], ["openalex"])
    assert cat == "incomplete" and best is None, "有源没答上 → incomplete，不是 no_hit"
    # 已经有像样命中时，缺一源不影响定性（结论已经立得住）
    assert INV.classify(_entry(), [_hit(0.98)], ["openalex"])[0] == "fillable"


def test_answered_reads_sources_ok_and_degrades_safely_on_legacy_reports():
    """续跑判据：新报告读 sources_ok；旧报告没这个字段时退化成「出过命中的源即算答过」——
    只会把「答了但没命中」的源多问一次（安全方向），绝不把没问成的当成结论。"""
    assert INV._answered({"sources_ok": ["crossref"]}) == {"crossref"}
    assert INV._answered({"sources_ok": []}) == set(), "空列表是「两源都没答上」，不是缺字段"
    legacy = {"hits": [{"source": "openalex", "doi": "10.1/x"}]}
    assert INV._answered(legacy) == {"openalex"}, "旧报告：出过命中必然答过"
    assert INV._answered({"hits": []}) == set(), "旧报告无命中 → 无从判断，全部重问"
    # 2026-09-06 起 ALL_SOURCES 多了 zotero_ts（本机 translation-server 直链源），
    # 所以旧报告要重问的不只 crossref。断言改成「答过的不再问、没答过的都要问」，
    # 不再把源的个数钉死。
    assert INV.ALL_SOURCES - INV._answered(legacy) == INV.ALL_SOURCES - {"openalex"}
    assert "crossref" in INV.ALL_SOURCES - INV._answered(legacy)


def test_render_md_calls_out_incomplete_and_how_to_resume():
    recs = [{"citekey": "a1", "category": "incomplete", "best": None, "sources_failed": ["openalex"],
             "year": 2024, "authors": [], "drop_reason": ""},
            {"citekey": "b2", "category": "no_hit", "best": None, "sources_failed": [],
             "year": 2024, "authors": [], "drop_reason": ""}]
    md = INV.render_md(recs, "20260905T000000")
    assert "incomplete 1" in md and "--resume-from" in md
    assert "a1（缺 openalex）" in md
    assert "b2" in md.split("## no_hit")[1].split("##")[0], "no_hit 名单里只该有真答过的"


def test_query_crossref_separates_http_failure_from_no_match():
    """crossref_lookup 把 HTTP 失败与「查了没有」一律吞成 None，正是本脚本要分的那两态——
    所以 query_crossref 自己发请求自己看状态码。429 必须记「没答上」，不能记成「源里没有」。"""
    class _R:
        def __init__(self, code, payload=None):
            self.status_code, self._p = code, payload or {}

        def json(self):
            return self._p

    class _C:
        def __init__(self, resp):
            self.resp, self.calls = resp, []

        def get(self, url, **kw):
            self.calls.append(url)
            return self.resp

    title = "Federated learning for missing data in electronic health records"
    # 429（额度/限流）→ 没答上
    hit, ok = INV.query_crossref(title, "", _C(_R(429)))
    assert hit is None and ok is False, "429 是没答上，不是源里没有"
    # 200 但零结果 → 答上了、确实没有
    hit2, ok2 = INV.query_crossref(title, "", _C(_R(200, {"message": {"items": []}})))
    assert hit2 is None and ok2 is True
    # 200 且命中
    item = {"title": [title], "DOI": "10.1000/abc",
            "author": [{"given": "Wei", "family": "Wang"}],
            "issued": {"date-parts": [[2024, 5, 1]]}}
    hit3, ok3 = INV.query_crossref(title, "", _C(_R(200, {"message": {"items": [item]}})))
    assert ok3 is True and hit3 and hit3["doi"] == "10.1000/abc" and hit3["source"] == "crossref"


def test_resume_set_excludes_titles_we_deliberately_never_ask_about(tmp_path, monkeypatch, capsys):
    """实词不足的篇目是**故意不问**（问了也不可信），不是「没问成」。若把它们算进重查集，
    每次续跑都会把它们列进「待重查」却永远问不成，计数永远消不掉。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    (nd / "literature_index.json").write_text(json.dumps({"entries": [
        {"citekey": "short1", "title": "Jimeng Sun", "dedup_key": "title:jimeng",
         "decision": "INCLUDE", "year": 2021, "month": "2021-01"},
        {"citekey": "long1", "dedup_key": "title:federated",
         "title": "Federated learning for missing data in electronic health records",
         "decision": "INCLUDE", "year": 2024, "month": "2024-01"},
    ]}), encoding="utf-8")
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"records": [
        {"citekey": "short1", "title": "Jimeng Sun", "category": "ambiguous",
         "hits": [], "sources_ok": [], "sources_failed": []},
        {"citekey": "long1", "category": "incomplete",
         "title": "Federated learning for missing data in electronic health records",
         "hits": [], "sources_ok": ["crossref"], "sources_failed": ["openalex"]},
    ]}), encoding="utf-8")

    monkeypatch.setattr(INV, "repo_path", lambda x: (nd if str(x).endswith("notes") else tmp_path / str(x)))
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd),
                                      "--resume-from", str(prev), "--dry-run"])
    assert INV.main() == 0
    out = capsys.readouterr().out
    assert "待重查 1 条" in out, "只有 long1 该重查；short1 是故意不问"
    assert "缺 openalex" in out


def test_lookup_separates_unreachable_source_from_source_has_none():
    """crossref_by_doi 把 404/非200/超时一律吞成 None，与「源里确实没有」同形——
    于是 HTTP 故障会被计进「源里也没有 N 条」，而这个数会被人抄进台账当结论
    （2026-09-05 对抗审计 Lens C 实证：台账「187 条源里本就没有」就是这么来的）。"""
    # fetch 抛异常 → UNREACHABLE，且**不写缓存**（本轮后面还能再问）
    cache = {}
    def boom(doi):
        raise ConnectionError("429")
    lk = BA.make_lookup(boom, [5], cache, delay=0)
    assert lk("10.1/a") is BA.UNREACHABLE
    assert "10.1/a" not in cache, "没答上不许进缓存，否则本轮内永远问不成"
    # fetch 自报 UNREACHABLE（生产里 _fetch 看状态码后返回它）
    lk2 = BA.make_lookup(lambda d: BA.UNREACHABLE, [5], {}, delay=0)
    assert lk2("10.1/b") is BA.UNREACHABLE
    # 源答了、确实没有 → None，照旧缓存
    c3 = {}
    lk3 = BA.make_lookup(lambda d: {}, [5], c3, delay=0)
    assert lk3("10.1/c") is None and c3["10.1/c"] is None


def test_plan_updates_counts_unreachable_separately_from_none():
    """三种「没补上」必须分三个计数：源里没有 / --limit 没轮到 / 源没答上。"""
    items = [{"type": "article-journal", "DOI": "10.1/a", "id": "a"},
             {"type": "article-journal", "DOI": "10.1/b", "id": "b"},
             {"type": "article-journal", "DOI": "10.1/c", "id": "c"},
             {"type": "article-journal", "DOI": "10.1/d", "id": "d"}]
    table = {"10.1/a": "281", "10.1/b": None,
             "10.1/c": BA.UNREACHABLE, "10.1/d": BA.UNCHECKED}
    upd, n_cand, n_none, n_unchecked, n_unreach = BA.plan_updates(items, lambda d: table[d])
    assert n_cand == 4 and [p for _, p in upd] == ["281"]
    assert (n_none, n_unchecked, n_unreach) == (1, 1, 1), "三种口径不许互相吞并"


def test_extract_url_identifier_reads_ids_the_entry_already_carries():
    """条目自己的 url 里常写着标识——抽出来零网络、零额度，而且是直链不是模糊匹配。"""
    f = INV.extract_url_identifier
    assert f("https://pubmed.ncbi.nlm.nih.gov/35309007/") == ("pmid", "35309007")
    assert f("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8378618/") == ("pmc", "PMC8378618")
    assert f("https://dl.acm.org/doi/10.1145/3459930.3469547") == ("doi", "10.1145/3459930.3469547")
    assert f("https://doi.org/10.1038/s41591-021-01506-3") == ("doi", "10.1038/s41591-021-01506-3")
    assert f("https://arxiv.org/abs/2401.12345") == ("arxiv", "2401.12345")
    # Scholar 跳转 / 聚合站：没有标识可抽，必须返回 None 而不是瞎猜
    assert f("https://scholar.google.com/scholar_url?url=xxx") is None
    assert f("https://search.proquest.com/docview/123") is None
    assert f("") is None and f(None) is None


def test_direct_link_hits_use_a_lower_similarity_floor():
    """直链的相似度只承担**确认**职责，标题检索要承担**识别**职责，门槛本就不同。
    实测 31 条真结果：min 0.75 / 中位 1.00 / 0.70 以下为零（2026-09-06）。
    用 0.92 那档会把 9 条正确的直链命中全判成 ambiguous。"""
    entry = {"title": "Fluid balance and acute lung injury in children and young adults",
             "year": 2026, "authors": ["Seo"], "decision": "INCLUDE", "dedup_key": "title:x"}
    ts_hit = {"source": INV.TS_SOURCE, "title": entry["title"], "doi": "10.1007/s00431-026-07285-8",
              "year": 2026, "authors": ["Seo"], "sim": 0.85}
    cat, best, _ = INV.classify(entry, [ts_hit])
    assert cat == "fillable" and best["doi"].startswith("10.1007")
    # 同样 0.85 但来自标题检索 → 只能是 ambiguous（识别职责没达标，0.85 < SIM_FILLABLE 0.92）
    assert INV.classify(entry, [dict(ts_hit, source="crossref")])[0] == "ambiguous"
    # 更低的 0.79：标题检索连候选门槛（SIM_CANDIDATE 0.80）都进不去 → no_hit；
    # 直链同分仍可用（门槛 0.70），这正是分档的意义
    assert INV.classify(entry, [dict(ts_hit, source="crossref", sim=0.79)])[0] == "no_hit"
    assert INV.classify(entry, [dict(ts_hit, sim=0.79)])[0] == "fillable"
    # 直链也不是免检：年份对不上照样降级交人看
    assert INV.classify(dict(entry, year=2019), [ts_hit])[0] == "ambiguous"


def test_confirmed_pmid_counts_as_a_usable_identifier():
    """TS 用 url 里的 PMID 解出正确标题后，那个 PMID **就是**可用身份（dedup_key 有 pmid: 档）。
    只认 DOI 会把已确认身份的条目报成 no_hit——「确认了却说没找到」是记账错误。"""
    entry = {"title": "Recurrent neural network based time-series modeling",
             "year": 2021, "authors": ["Nayebi"], "decision": "INCLUDE", "dedup_key": "title:x"}
    hit = {"source": INV.TS_SOURCE, "title": entry["title"], "doi": None, "pmid": "35309007",
           "year": 2021, "authors": ["Nayebi"], "sim": 1.0}
    cat, best, _ = INV.classify(entry, [hit])
    assert cat == "fillable" and best["pmid"] == "35309007"
    # 没有任何标识的命中仍然不算
    assert INV.classify(entry, [dict(hit, pmid=None)])[0] == "no_hit"


def test_resume_carries_forward_previously_answered_sources(tmp_path, monkeypatch, capsys):
    """上一轮答上的源必须结转进新报告的 sources_ok，否则下次续跑会把它们当没问过重问——
    `--resume-from` 的全部意义就是「只补问没答上的那一源」
    （2026-09-07 实测：不结转会让 crossref 对全部 301 条重问一遍）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    (nd / "literature_index.json").write_text(json.dumps({"papers": [
        {"citekey": "a2024X", "dedup_key": "title:fed",
         "title": "Federated learning for missing data in electronic health records",
         "decision": "INCLUDE", "year": 2024, "month": "2024-01"}]}), encoding="utf-8")
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"records": [
        {"citekey": "a2024X", "category": "incomplete", "hits": [],
         "title": "Federated learning for missing data in electronic health records",
         "sources_ok": ["crossref"], "sources_failed": ["openalex", "zotero_ts"]}]}),
        encoding="utf-8")
    monkeypatch.setattr(INV, "repo_path", lambda x: nd if str(x).endswith("notes") else tmp_path / str(x))
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd),
                                      "--resume-from", str(prev), "--dry-run"])
    assert INV.main() == 0
    out = capsys.readouterr().out
    assert "待重查 1 条" in out and "crossref" not in out.split("待重查")[1][:60], \
        "crossref 已答过，不该再出现在待重查的缺源清单里"
