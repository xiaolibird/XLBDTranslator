# -*- coding: utf-8 -*-
"""Crossref 元数据增强回归测试：解析 / 标题相似度门槛 / 增强覆盖。全程 mock，不发网络。"""
import httpx

from src.scholar.schema import PaperMetadata
from src.scholar import crossref


_WORK = {
    "DOI": "10.1016/j.landig.2026.101043",
    "title": ["Deception in clinical large language models: an under-recognised safety risk"],
    "author": [
        {"given": "Aakash", "family": "Reddy"},
        {"given": "David T", "family": "Zhu"},
    ],
    "container-title": ["The Lancet Digital Health"],
    "volume": "8", "issue": "2", "page": "e101043",
    "issued": {"date-parts": [[2026, 2, 1]]},
    "URL": "https://doi.org/10.1016/j.landig.2026.101043",
}


def test_title_similarity():
    assert crossref.title_similarity("A B C", "A B C") == 1.0
    assert crossref.title_similarity("A B C D", "X Y Z") == 0.0
    assert 0 < crossref.title_similarity("causal EHR missingness", "EHR missingness graph") < 1


def test_parse_crossref_work():
    p = crossref.parse_crossref_work(_WORK)
    assert p["doi"] == "10.1016/j.landig.2026.101043"
    assert p["authors"] == ["Aakash Reddy", "David T Zhu"]
    assert p["journal"] == "The Lancet Digital Health"
    assert p["publication_date"].year == 2026 and p["publication_date"].month == 2


def test_best_match_accepts_exact_rejects_low():
    title = "Deception in clinical large language models: an under-recognised safety risk"
    # 完全匹配 → 命中
    assert crossref.best_match([_WORK], title) is not None
    # 无关论文（低相似度）→ 拒绝，返回 None（不污染）
    wrong = dict(_WORK, title=["Game Theory Approach to Identifying Deception"],
                 author=[{"given": "Tyler", "family": "Di Maggio"}])
    assert crossref.best_match([wrong], title) is None


def test_crossref_lookup_via_mock():
    def handler(request):
        assert "api.crossref.org" in str(request.url)
        assert "mailto=" in str(request.url)
        return httpx.Response(200, json={"message": {"items": [_WORK]}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    hit = crossref.crossref_lookup(
        "Deception in clinical large language models: an under-recognised safety risk",
        email="x@y.com", client=client)
    assert hit and hit["doi"] == "10.1016/j.landig.2026.101043"


def test_enrich_metadata_cleans_dirty_authors():
    # 模拟 Scholar 邮件解析出的脏作者 + 缺 DOI
    meta = PaperMetadata(
        paper_id="p1",
        title="Deception in clinical large language models: an under-recognised safety risk",
        authors=["recognised safety riskA Reddy", "DT Zhu"], doi=None)

    def handler(request):
        return httpx.Response(200, json={"message": {"items": [_WORK]}})
    client = httpx.Client(transport=httpx.MockTransport(handler))

    ok = crossref.enrich_metadata(meta, email="x@y.com", client=client)
    assert ok is True
    assert meta.authors == ["Aakash Reddy", "David T Zhu"]  # 脏作者被规范覆盖
    assert meta.doi == "10.1016/j.landig.2026.101043"       # 补上 DOI
    assert meta.journal == "The Lancet Digital Health"


def test_enrich_metadata_no_match_keeps_original():
    """标题对不上时保留原始元数据（宁可漏增强也不误配）。"""
    meta = PaperMetadata(paper_id="p2", title="Totally Unrelated Preprint About Nothing",
                         authors=["Correct Author"], doi=None)

    def handler(request):
        return httpx.Response(200, json={"message": {"items": [_WORK]}})  # 返回无关论文
    client = httpx.Client(transport=httpx.MockTransport(handler))

    ok = crossref.enrich_metadata(meta, email="x@y.com", client=client)
    assert ok is False
    assert meta.authors == ["Correct Author"]  # 未被覆盖
    assert meta.doi is None


def test_crossref_lookup_network_error_returns_none():
    def handler(request):
        raise httpx.ConnectError("boom")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert crossref.crossref_lookup("whatever title here", email="x@y.com", client=client) is None


def test_parse_crossref_work_records_true_date_precision():
    """Crossref 的 date-parts 长度就是真实精度：补出来的月日不能被当成确切出版日。

    date 对象仍补 1（下游 citekey 取年与索引 year 都依赖它非空），
    精度另存 date_precision，只在 CSL/Zotero 产出层截断。
    """
    from src.scholar.crossref import parse_crossref_work
    def work(dp):
        return {"title": ["T"], "author": [{"family": "X", "given": "Y"}],
                "issued": {"date-parts": [dp]}, "DOI": "10.1/x"}

    y = parse_crossref_work(work([2026]))
    assert y["date_precision"] == "year"
    assert y["publication_date"].month == 1 and y["publication_date"].day == 1

    m = parse_crossref_work(work([2026, 5]))
    assert m["date_precision"] == "month"
    assert m["publication_date"].day == 1          # 占位，但精度已如实记录

    d = parse_crossref_work(work([2026, 5, 5]))
    assert d["date_precision"] == "day"


# ---------------- e-locator 期刊：article-number 回退（docs/bugs/2026-09-04-crossref-article-number-lost.md） ----------------

def _work(**kw):
    base = {"title": ["Testing Covariates Effects on Bivariate Reference Regions"],
            "DOI": "10.1002/sim.10308", "container-title": ["Statistics in Medicine"],
            "volume": "44", "issue": "3-4", "issued": {"date-parts": [[2025, 1, 24]]}}
    base.update(kw)
    return base


def test_parse_crossref_work_falls_back_to_article_number():
    from src.scholar.crossref import parse_crossref_work
    assert parse_crossref_work(_work(page=None, **{"article-number": "e10308"}))["pages"] == "e10308"
    # 不一定是纯数字（NAR 的 gkag386），别做 int 校验
    assert parse_crossref_work(_work(**{"article-number": "gkag386"}))["pages"] == "gkag386"


def test_parse_crossref_work_page_wins_over_article_number_and_both_empty_is_none():
    from src.scholar.crossref import parse_crossref_work
    assert parse_crossref_work(_work(page="123-130", **{"article-number": "e1"}))["pages"] == "123-130"
    assert parse_crossref_work(_work(page="", **{"article-number": ""}))["pages"] is None
    assert parse_crossref_work(_work())["pages"] is None


def test_parse_crossref_work_tolerates_non_string_page():
    from src.scholar.crossref import parse_crossref_work
    assert parse_crossref_work(_work(page=7))["pages"] == "7"
    assert parse_crossref_work(_work(**{"article-number": 294}))["pages"] == "294"


def test_repair_references_csl_carries_article_number_as_page():
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    from src.scholar.crossref import parse_crossref_work
    spec = importlib.util.spec_from_file_location("repair_refs_t", REPO_ROOT / "scripts" / "repair_references.py")
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)
    item = parse_crossref_work(_work(**{"article-number": "e10308"}))
    csl = rr._csl_from_crossref(item, {"citekey": "ladobaleato2025Testing"})
    assert csl["page"] == "e10308" and csl["id"] == "ladobaleato2025Testing"
