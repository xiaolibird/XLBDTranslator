# -*- coding: utf-8 -*-
"""export_timeline_xlsx：零依赖 xlsx 写入器 + 三张表口径 + 陈旧判定。用 zipfile+XML 回读，不装 openpyxl。"""
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from scripts import export_timeline_xlsx as ex

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _paper(ck, month, series="auto", full=False, dup=None, rank=1, **kw):
    d = {"citekey": ck, "month": month, "series": series, "note_file": "科研札记_{}_{}.md".format(
        month, "手动精读" if series == "manual" else "全文精读"),
         "has_full_text_reading": full, "duplicate_of": dup, "priority_rank": rank,
         "title": "T " + ck, "year": 2026, "bucket": ["A", "D"], "role": "CITE_SUPPORT"}
    d.update(kw)
    return d


def _index():
    return {"generated_at": "2026-09-01T10:00:00", "papers": [
        _paper("a1", "2026-08-03", full=True),
        _paper("a2", "2026-08-03"),
        _paper("a3", "2026-08-03", dup="doi:x@2026-07"),
        _paper("m1", "2026-08", "manual", full=True, rank=2, one_line="x & <y>", reading_source="manual-pdf"),
        _paper("b1", "2026-08-26-Book", "book", full=True, reading_source="manual-book"),
        _paper("m2", "2026-08", "manual", full=True, rank=3, reading_source="local-pdf"),   # 手动文件里的脚本通读
    ]}


def _read(path):
    z = zipfile.ZipFile(path)
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    names = [s.get("name") for s in wb.find("m:sheets", NS)]
    out = {}
    for i, name in enumerate(names):
        root = ET.fromstring(z.read("xl/worksheets/sheet{}.xml".format(i + 1)))
        rows = []
        for r in root.find("m:sheetData", NS).findall("m:row", NS):
            vals = []
            for c in r.findall("m:c", NS):
                if c.get("t") == "inlineStr":
                    vals.append("".join(t.text or "" for t in c.iter("{%s}t" % NS["m"])))
                else:
                    v = c.find("m:v", NS)
                    vals.append(None if v is None else float(v.text))
            rows.append(vals)
        out[name] = (root, rows)
    return out


def test_sheets_and_counts(tmp_path):
    out = tmp_path / "t.xlsx"
    ex.write_xlsx(out, ex.build_sheets(_index()))
    sheets = _read(out)
    assert list(sheets) == ["时间线", "按月汇总", "明细"]
    tl = sheets["时间线"][1]
    assert tl[0][:8] == ["批次", "来源", "篇数", "手动全文精读", "脚本全文通读", "仅摘要", "重复(已合并)", "札记文件"]
    row = next(r for r in tl if r and r[0] == "2026-08-03")
    # 篇数含被判重的；仅摘要 = 篇数 - 两种全文；重复是信息列
    assert row[1:7] == ["自动周报", 3, 0, 1, 2, 1]
    manual = next(r for r in tl if r and r[0] == "2026-08")
    assert manual[1:7] == ["手动精读", 2, 1, 1, 0, 0]
    ms = sheets["按月汇总"][1]
    assert ms[0] == ["年月", "自动周报", "手动精读", "书籍精读", "合计", "其中全文精读"]
    assert ms[1] == ["2026-08", 3, 2, 1, 6, 4]
    det = sheets["明细"][1]
    assert det[0][-1] == "重复于" and len(det) == 7
    dup_row = next(r for r in det if r[3] == "a3")
    assert dup_row[2] == "仅摘要" and dup_row[-1] == "doi:x@2026-07"
    esc = next(r for r in det if r[3] == "m1")
    assert esc[6] == "x & <y>" and esc[7] == "A/D"          # XML 转义回读无损；bucket 列表拼成 A/D


def test_header_style_freeze_and_filter(tmp_path):
    out = tmp_path / "t.xlsx"
    ex.write_xlsx(out, ex.build_sheets(_index()))
    sheets = _read(out)
    root_det = sheets["明细"][0]
    assert root_det.find("m:autoFilter", NS) is not None
    assert root_det.find("m:sheetViews/m:sheetView/m:pane", NS).get("state") == "frozen"
    first = root_det.find("m:sheetData/m:row/m:c", NS)
    assert first.get("s") == "1"                                # 表头用样式 1（加粗反白）
    assert sheets["时间线"][0].find("m:autoFilter", NS) is None
    z = zipfile.ZipFile(out)
    assert b'fontId="1" fillId="2"' in z.read("xl/styles.xml")


def test_export_stale_check(tmp_path, monkeypatch):
    idx = tmp_path / "literature_index.json"
    idx.write_text(json.dumps(_index(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "Reading" / "时间线.xlsx"
    assert ex.export(idx, out) == "written" and out.exists()
    assert ex._meta_path(out).exists()
    assert ex.export(idx, out) == "fresh"                       # 索引没变不重写
    d = json.loads(idx.read_text(encoding="utf-8")); d["generated_at"] = "2026-09-02T00:00:00"
    idx.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert ex.export(idx, out) == "written"
    assert ex.export(idx, out, force=True) == "written"
    assert ex.export(tmp_path / "nope.json", out) == "missing"
    idx.write_text("{not json", encoding="utf-8")
    assert ex.export(idx, out) == "broken"


def test_control_chars_do_not_break_xml(tmp_path):
    d = _index()
    d["papers"][0]["title"] = "bad \x01 title\tok"
    out = tmp_path / "t.xlsx"
    ex.write_xlsx(out, ex.build_sheets(d))
    det = _read(out)["明细"][1]
    assert any("bad  title\tok" == r[5] for r in det)


# ---- sync_vault 挂点：索引一变（launchd WatchPaths）就顺带刷 xlsx；失败只通知不挡 vault ----

def test_sync_vault_hook_calls_exporter_and_notifies_on_failure(monkeypatch, tmp_path):
    import subprocess
    import scripts.sync_vault as sv
    calls, sent, logs = [], [], []
    seq = {"n": 0}
    monkeypatch.setattr(sv, "notify", lambda title, text: sent.append((title, text)))
    monkeypatch.setattr(sv, "log", lambda msg: logs.append(msg))

    def fake_run(argv, **kw):
        calls.append(argv)
        seq["n"] += 1
        rc = 0 if seq["n"] <= 2 else 2          # 第 3 次子进程调用模拟导出失败
        return subprocess.CompletedProcess(argv, rc, stdout="✅ 已写 x.xlsx\n" if rc == 0 else "",
                                           stderr="" if rc == 0 else "⛔ 索引损坏\n")
    monkeypatch.setattr(sv.subprocess, "run", fake_run)
    # 非真实札记库且未显式给 --out：不导出（防测试/临时目录覆盖桌面上的真表）
    assert sv.export_timeline(str(tmp_path)) == 0 and calls == []
    assert sv.export_timeline(str(tmp_path), out=str(tmp_path / "x.xlsx")) == 0
    assert str(sv.EXPORT_TIMELINE) in calls[0] and "--out" in calls[0]
    calls.clear()
    assert sv.export_timeline("output/scholar_notes") == 0
    assert str(sv.EXPORT_TIMELINE) in calls[0] and "--notes-dir" in calls[0] and "--out" not in calls[0]
    assert sent == [] and any("已写 x.xlsx" in m for m in logs)
    assert sv.export_timeline("output/scholar_notes") == 2
    assert len(sent) == 1 and "索引损坏" in sent[0][1]

    def boom(argv, **kw):
        raise OSError("no python")
    monkeypatch.setattr(sv.subprocess, "run", boom)
    assert sv.export_timeline("output/scholar_notes") == 1          # 异常也不抛，vault 照常
    assert len(sent) == 2
