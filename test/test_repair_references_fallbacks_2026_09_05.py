# -*- coding: utf-8 -*-
"""`repair_references` 的双向死锁（2026-09-05 对抗审计 Lens C）。

一次 Crossref 抖动 → 该月 references.json 落成索引兜底（缺卷/期/页）
→ 下次跑同一命令在**联网之前**就撞上「已存在，拒绝覆盖」直接退 2
→ 唯一逃生门 `--force` 又被台账明文禁止（整月重建、索引兜底会覆盖既有更权威字段）
= 修复通道被自己的安全闸和自己的纪律双向堵死。

修法：`--repair-fallbacks` 只碰索引兜底形态的条目、只升不降，其余字节不动。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar.paths import REPO_ROOT                          # noqa: E402

spec = importlib.util.spec_from_file_location("_rr", REPO_ROOT / "scripts" / "repair_references.py")
RR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RR)


class _A:
    email = ""
    dry_run = False


def test_is_index_fallback_only_flags_entries_missing_both_volume_and_page():
    assert RR._is_index_fallback({"DOI": "10.1/a"}) is True
    assert RR._is_index_fallback({"DOI": "10.1/a", "volume": "12"}) is False
    assert RR._is_index_fallback({"DOI": "10.1/a", "page": "e0266516"}) is False


def test_crossref_or_unreachable_separates_404_from_429():
    """404 = 源答了确实没有；429/5xx/异常 = 没答上。crossref_by_doi 把两者都吞成 None，
    于是一次抖动写下的兜底事后再也分辨不出该不该补。"""
    class _R:
        def __init__(self, code, payload=None):
            self.status_code, self._p = code, payload or {}

        def json(self):
            return self._p

    class _C:
        def __init__(self, r):
            self.r = r

        def get(self, url, **kw):
            return self.r
    got, ok = RR._crossref_or_unreachable("10.1/a", "", _C(_R(404)))
    assert got is None and ok is True, "404 是源答了：确实没有"
    got, ok = RR._crossref_or_unreachable("10.1/a", "", _C(_R(429)))
    assert got is None and ok is False, "429 是没答上"


def test_repair_fallbacks_upgrades_only_and_never_downgrades(tmp_path, monkeypatch):
    """核心回归：兜底条目被补全，而**已经很权威的条目一个字节不动**。"""
    f = tmp_path / "科研札记_2026-06_全文精读.references.json"
    original = [
        {"id": "a2024X", "DOI": "10.1/a", "title": "A"},                       # 兜底：待修
        {"id": "b2024Y", "DOI": "10.1/b", "title": "B", "volume": "9",
         "page": "1-10", "container-title": "权威刊名"},                        # 已权威：不许动
        {"id": "c2024Z", "DOI": "10.48550/arXiv.2401.1", "title": "C"},        # arXiv：跳过
    ]
    f.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    def fake(doi, email, client):
        if doi == "10.1/a":
            return {"doi": doi, "title": "A", "volume": "12", "page": "e0266516"}, True
        raise AssertionError("不该问已权威条目或 arXiv 条目：{}".format(doi))
    monkeypatch.setattr(RR, "_crossref_or_unreachable", fake)
    monkeypatch.setattr(RR, "_csl_from_crossref",
                        lambda got, old: {"volume": got["volume"], "page": got["page"],
                                          "container-title": ""})   # 空值不许覆盖旧值
    import src.scholar.fulltext as FT
    monkeypatch.setattr(FT, "ipv4_client",
                        lambda **k: type("C", (), {"__enter__": lambda s: None,
                                                   "__exit__": lambda *a: False})())
    assert RR._repair_fallbacks(f, _A()) == 0
    out = json.loads(f.read_text(encoding="utf-8"))
    assert out[0]["volume"] == "12" and out[0]["page"] == "e0266516"
    assert out[1] == original[1], "已权威条目必须一字节不动"
    assert out[2] == original[2], "arXiv 条目不该被碰"
    assert "container-title" not in out[0], "新值为空不许写进去（只升不降）"
    assert (tmp_path / "科研札记_2026-06_全文精读.references.json.bak").exists(), "改前必须备份"


def test_repair_fallbacks_does_not_write_when_source_is_unreachable(tmp_path, monkeypatch):
    """源没答上时不许写盘，也不许把「没问成」记成「源里也没有」。"""
    f = tmp_path / "x.references.json"
    original = [{"id": "a", "DOI": "10.1/a", "title": "A"}]
    f.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(RR, "_crossref_or_unreachable", lambda *a: (None, False))
    import src.scholar.fulltext as FT
    monkeypatch.setattr(FT, "ipv4_client",
                        lambda **k: type("C", (), {"__enter__": lambda s: None,
                                                   "__exit__": lambda *a: False})())
    assert RR._repair_fallbacks(f, _A()) == 0
    assert json.loads(f.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "x.references.json.bak").exists(), "没改就不该留备份"


def test_repair_fallbacks_dry_run_touches_nothing(tmp_path, monkeypatch):
    f = tmp_path / "y.references.json"
    original = [{"id": "a", "DOI": "10.1/a", "title": "A"}]
    f.write_text(json.dumps(original), encoding="utf-8")
    before = f.read_bytes()
    monkeypatch.setattr(RR, "_crossref_or_unreachable",
                        lambda *a: ({"doi": "10.1/a", "volume": "9"}, True))
    monkeypatch.setattr(RR, "_csl_from_crossref", lambda got, old: {"volume": "9"})
    import src.scholar.fulltext as FT
    monkeypatch.setattr(FT, "ipv4_client",
                        lambda **k: type("C", (), {"__enter__": lambda s: None,
                                                   "__exit__": lambda *a: False})())

    class _D(_A):
        dry_run = True
    assert RR._repair_fallbacks(f, _D()) == 0
    assert f.read_bytes() == before, "dry-run 必须零写入"
