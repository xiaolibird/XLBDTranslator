# -*- coding: utf-8 -*-
"""scripts/notes_query.py 离线回归（不读真实索引）。

锁三件容易悄悄退化的东西：
1. **role 模式必须句级真命中**——曾出现靠 one_line 里"未显式建模 MNAR"命中、
   却展示八条与 MNAR 无关的方法句，且因回退展示全量句子而排到真命中前面；
2. 排序契约：句级命中 > 纯标题命中，同层内真命中句多者靠前；
3. 索引损坏/结构诡异退出 2（与"无命中"的 1 区分开），不把 traceback 抛给用户。

导入方式沿用 test_search_pubs.py：pytest 时 repo root 在 sys.path，
scripts 作为隐式命名空间包可直接 import；INDEX_PATH 是模块级常量，monkeypatch 指向 tmp_path。
"""
import json

import pytest

import scripts.notes_query as nq


def _paper(citekey, *, title="", one_line="", highlights=(), tier="mid", **kw):
    e = {"citekey": citekey, "title": title, "title_zh": None, "one_line": one_line,
         "highlights": list(highlights), "priority_tier": tier, "duplicate_of": None,
         "year": 2025, "month": "2026-07", "series": "manual", "has_full_text_reading": True,
         "note_file": "n.md", "note_line": 1}
    e.update(kw)
    return e


def _hl(role, text, section="方法与数据"):
    return {"role": role, "tag": "x", "section": section, "text": text}


# ---------------- _match：role 模式的句级真命中 ----------------

def test_role_requires_real_sentence_hit():
    """one_line 提到 MNAR 但 method 句子都不含它 → 不入选（P0 回归）。"""
    e = _paper("fake2024X", one_line="未显式建模MNAR等缺失机制",
               highlights=[_hl("method", "图变换器抽就诊嵌入"),
                           _hl("method", "全连接图编码码-码关系")])
    assert nq._match(e, ["mnar"], "method") is None


def test_role_keeps_entry_with_real_hit():
    e = _paper("real2025Y", one_line="联邦因果发现",
               highlights=[_hl("method", "各源各自学出局部MNAR机制做IPW重加权"),
                           _hl("method", "无关句子")])
    hits, source = nq._match(e, ["mnar"], "method")
    assert source == "highlight"
    assert len(hits) == 1 and "IPW" in hits[0]["text"]


def test_no_role_allows_title_only_match_but_marks_source():
    """不传 role 时标题命中仍入选，但来源标为 title、不伪造句级证据。"""
    e = _paper("t2024Z", title="A study of MNAR mechanisms",
               highlights=[_hl("citable", "样本量 1000 例")])
    hits, source = nq._match(e, ["mnar"], None)
    assert source == "title" and hits == []


def test_terms_are_and_across_fields():
    e = _paper("a2024A", title="Missingness in EHR",
               highlights=[_hl("citable", "队列含 MNAR 结构")])
    assert nq._match(e, ["missingness", "mnar"], None) is not None   # 跨字段 AND 成立
    assert nq._match(e, ["missingness", "zzz"], None) is None


def test_role_filter_excludes_other_roles_sentences():
    """关键词只出现在 citable 句里时，--role method 不应命中。"""
    e = _paper("b2024B", highlights=[_hl("citable", "troponin 缺失最高"),
                                     _hl("method", "自注意力融合")])
    assert nq._match(e, ["troponin"], "method") is None
    assert nq._match(e, ["troponin"], "citable") is not None


def test_malformed_highlights_do_not_crash():
    e = _paper("c2024C", title="MNAR", highlights=["裸字符串", None, {"role": "method"}])
    assert nq._match(e, ["mnar"], None) is not None      # 脏项被过滤，不抛异常


# ---------------- 端到端：排序、退出码、输出格式 ----------------

@pytest.fixture
def fake_index(tmp_path, monkeypatch):
    def _write(papers, collisions=()):
        p = tmp_path / "literature_index.json"
        p.write_text(json.dumps({"schema_version": 3, "papers": papers,
                                 "citekey_collisions": list(collisions)}, ensure_ascii=False),
                     encoding="utf-8")
        monkeypatch.setattr(nq, "INDEX_PATH", p)
        return p
    return _write


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["notes_query.py"] + argv)
    code = nq.main()
    return code, capsys.readouterr()


def test_sort_real_hits_before_title_only(fake_index, monkeypatch, capsys):
    """纯标题命中排在句级命中之后（P0 回归：曾因展示句数把假命中顶到第一）。"""
    fake_index([
        _paper("titleOnly", title="MNAR everywhere",
               highlights=[_hl("citable", "无关句 1"), _hl("citable", "无关句 2")]),
        _paper("realHit", highlights=[_hl("citable", "MNAR 下 AUC 降 0.05")]),
    ])
    code, out = _run(monkeypatch, capsys, ["MNAR", "--cite"])
    assert code == 0
    assert out.out.strip() == "[@realHit; @titleOnly]"


def test_exit_codes_consistent_across_modes(fake_index, monkeypatch, capsys):
    fake_index([_paper("x2024X", title="something else")])
    for extra in ([], ["--cite"], ["--json"]):
        code, _ = _run(monkeypatch, capsys, ["zzznotexist"] + extra)
        assert code == 1, extra


def test_json_reports_total_and_truncation(fake_index, monkeypatch, capsys):
    fake_index([_paper("k{}".format(i), highlights=[_hl("citable", "MNAR 证据")])
                for i in range(5)])
    code, out = _run(monkeypatch, capsys, ["MNAR", "--limit", "2", "--json"])
    d = json.loads(out.out)
    assert code == 0 and d["total"] == 5 and d["shown"] == 2 and d["truncated"] == 3


def test_limit_zero_means_unlimited(fake_index, monkeypatch, capsys):
    fake_index([_paper("k{}".format(i), highlights=[_hl("citable", "MNAR")]) for i in range(3)])
    code, out = _run(monkeypatch, capsys, ["MNAR", "--limit", "0", "--json"])
    assert code == 0 and json.loads(out.out)["shown"] == 3


def test_duplicate_entries_filtered(fake_index, monkeypatch, capsys):
    fake_index([
        _paper("dup2024D", highlights=[_hl("citable", "MNAR")], duplicate_of="keeper@2020-01"),
        _paper("keep2024K", highlights=[_hl("citable", "MNAR")]),
    ])
    code, out = _run(monkeypatch, capsys, ["MNAR", "--cite"])
    assert code == 0 and "dup2024D" not in out.out and "keep2024K" in out.out


@pytest.mark.parametrize("raw", ['{"papers": [', "[]", '{"x":1}'])
def test_corrupt_index_exits_2(tmp_path, monkeypatch, capsys, raw):
    p = tmp_path / "literature_index.json"
    p.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(nq, "INDEX_PATH", p)
    code, out = _run(monkeypatch, capsys, ["MNAR"])
    assert code == 2 and "Traceback" not in out.err


def test_missing_index_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nq, "INDEX_PATH", tmp_path / "nope.json")
    code, out = _run(monkeypatch, capsys, ["MNAR"])
    assert code == 2 and "notes_index.py" in out.err     # 给出可操作提示


def test_role_choices_match_schema():
    """CLI 的 role 三值必须与 schema.TAG_TO_ROLE 的产出集合一致（防第二真相源漂移）。"""
    from src.scholar.schema import TAG_TO_ROLE
    assert set(nq.ROLE_HINT) == {r for r in TAG_TO_ROLE.values() if r}
