#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""literature_index.json → 《札记库入库时间线.xlsx》（三张表：时间线 / 按月汇总 / 明细）。

这张表最早是 2026-08-19 一次性用 openpyxl 生成后放在 ~/Downloads 的，仓库里没有生成脚本，
索引一变它就过期。现在改为**由索引派生、随索引更新**：
- 由 scripts/sync_vault.py 在每次被 launchd 触发（WatchPaths=literature_index.json）时顺带调用；
- 自带陈旧判定：xlsx 旁的 `.<文件名>.meta.json` 记录来源索引的 generated_at，相同就跳过；
- **零依赖**：launchd 用的 env002_reader 没装 openpyxl，这里用 zipfile + 手写 SpreadsheetML
  写最小 xlsx（表头加粗反白、冻结首行、明细表自动筛选、列宽），Excel / Numbers / openpyxl 都能读。

口径（与 08-19 的老表逐行对过：2021-01 = 16/0/4/12/1）：
- 时间线：一份札记文件一行。篇数 = 文件内全部条目（含被判重的）；手动全文精读 = has_full_text_reading 且
  reading_source 以 manual 开头（manual-pdf / manual-book）；脚本全文通读 = 其余全文精读；仅摘要 = 其余；
  重复(已合并) = duplicate_of 非空的条数（信息列，已计入篇数）。
- 按月汇总：按 month 前 7 位聚合，自动周报 / 手动精读 / 书籍精读 / 合计 / 其中全文精读（老表没有书籍列，
  2026-08-26 才有书籍系列，故新增一列而不是并进手动）。
- 明细：一条目一行，含被判重条目（末列「重复于」标出），按批次、优先级序号排。

用法：
    PYTHONPATH=. python3 scripts/export_timeline_xlsx.py                 # 默认写 ~/Desktop/Lab/Reading/札记库入库时间线.xlsx
    PYTHONPATH=. python3 scripts/export_timeline_xlsx.py --force         # 忽略陈旧判定
    PYTHONPATH=. python3 scripts/export_timeline_xlsx.py --out /tmp/x.xlsx
退出码：0 写了或已是最新 / 2 索引缺失或坏。
"""
import argparse
import json
import os
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path          # noqa: E402
from src.scholar.notes_index import INDEX_JSON   # noqa: E402

DEFAULT_OUT = Path.home() / "Desktop" / "Lab" / "Reading" / "札记库入库时间线.xlsx"
SERIES_ZH = {"auto": "自动周报", "manual": "手动精读", "book": "书籍精读"}
SERIES_SHORT = {"auto": "自动", "manual": "手动", "book": "书籍"}
LEGEND = [
    ("自动周报", "Google Alert → digest → 自动入库"),
    ("手动精读", "agent 亲读 PDF + 交叉核验"),
    ("书籍精读", "按章分诊后深读的教科书/专著"),
    ("手动全文精读 / 脚本全文通读", "两种全文精读；其余为仅摘要（未读全文）"),
    ("重复(已合并)", "已按 dedup_key 判为同一篇（信息列，已计入篇数）"),
]


# ---------------- 最小 xlsx 写入器 ----------------

def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_xml(ref: str, v: Any, style: int) -> str:
    if v is None or v == "":
        return '<c r="{}" s="{}"/>'.format(ref, style) if style else ""
    if isinstance(v, bool):
        return '<c r="{}" s="{}" t="b"><v>{}</v></c>'.format(ref, style, int(v))
    if isinstance(v, (int, float)):
        return '<c r="{}" s="{}"><v>{}</v></c>'.format(ref, style, v)
    text = escape(str(v)).replace("\r", "")
    # 非法 XML 控制字符会让整个文件打不开
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return '<c r="{}" s="{}" t="inlineStr"><is><t xml:space="preserve">{}</t></is></c>'.format(ref, style, text)


def _sheet_xml(rows: Sequence[Sequence[Any]], widths: Sequence[float],
               header_rows: int = 1, autofilter: bool = False) -> str:
    n_cols = max((len(r) for r in rows), default=0)
    cols = "".join('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w)
                   for i, w in enumerate(widths[:n_cols]))
    body = []
    for ri, row in enumerate(rows):
        style = 1 if ri < header_rows else 0
        cells = "".join(_cell_xml("{}{}".format(_col_letter(ci), ri + 1), v, style)
                        for ci, v in enumerate(row))
        body.append('<row r="{}">{}</row>'.format(ri + 1, cells))
    dim = "A1:{}{}".format(_col_letter(max(n_cols - 1, 0)), max(len(rows), 1))
    freeze = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="{0}" topLeftCell="A{1}" '
              'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>').format(header_rows, header_rows + 1)
    af = '<autoFilter ref="{}"/>'.format(dim) if autofilter and rows else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<dimension ref="{dim}"/>{freeze}<sheetFormatPr defaultRowHeight="15"/>'
            '{cols}<sheetData>{body}</sheetData>{af}</worksheet>').format(
        dim=dim, freeze=freeze, cols="<cols>{}</cols>".format(cols) if cols else "",
        body="".join(body), af=af)


_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
           '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
           '<fills count="3"><fill><patternFill patternType="none"/></fill>'
           '<fill><patternFill patternType="gray125"/></fill>'
           '<fill><patternFill patternType="solid"><fgColor rgb="FF44546A"/><bgColor indexed="64"/></patternFill></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def write_xlsx(path: Path, sheets: List[Dict[str, Any]]) -> None:
    """sheets: [{name, rows, widths, autofilter}]，原子写（tmp + replace）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join('<Override PartName="/xl/worksheets/sheet{}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i + 1) for i in range(len(sheets)))
                   + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                   '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                   '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                   '</Relationships>')
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        z.writestr("docProps/core.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                   'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                   '<dc:creator>XLBDTranslator export_timeline_xlsx</dc:creator>'
                   '<dcterms:created xsi:type="dcterms:W3CDTF">{0}</dcterms:created>'
                   '<dcterms:modified xsi:type="dcterms:W3CDTF">{0}</dcterms:modified></cp:coreProperties>'.format(now))
        z.writestr("docProps/app.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                   '<Application>XLBDTranslator</Application></Properties>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets>' + "".join('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(escape(s["name"]), i + 1, i + 1)
                                        for i, s in enumerate(sheets)) + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join('<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(i + 1) for i in range(len(sheets)))
                   + '<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'.format(len(sheets) + 1)
                   + '</Relationships>')
        z.writestr("xl/styles.xml", _STYLES)
        for i, s in enumerate(sheets):
            z.writestr("xl/worksheets/sheet{}.xml".format(i + 1),
                       _sheet_xml(s["rows"], s.get("widths") or [], autofilter=s.get("autofilter", False)))
    tmp.replace(path)


# ---------------- 从索引算三张表 ----------------

def _depth(p: Dict[str, Any]) -> str:
    """按 reading_source 而不是 series 分：手动精读文件里也可能混进脚本通读的条目
    （老表 2026-07 = 89 手动 + 1 脚本，就是这么分的），series 只决定「来源」列。"""
    if not p.get("has_full_text_reading"):
        return "仅摘要"
    return "手动全文精读" if (p.get("reading_source") or "").startswith("manual") else "脚本全文通读"


def _batch_key(label: str):
    # "2026-08" < "2026-08-03" < "2026-08-npjDM"：先按前 7 位，再按整串
    return (label[:7], label)


def build_sheets(index: Dict[str, Any]) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = list(index.get("papers", []))

    # 时间线：一份札记文件一行
    by_file: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in papers:
        by_file[p.get("note_file") or ""].append(p)
    tl_rows: List[List[Any]] = [["批次", "来源", "篇数", "手动全文精读", "脚本全文通读", "仅摘要", "重复(已合并)", "札记文件"]]
    file_rows = []
    for nf, es in by_file.items():
        label = str(es[0].get("month") or "")
        series = es[0].get("series") or "auto"
        c = Counter(_depth(p) for p in es)
        dup = sum(1 for p in es if p.get("duplicate_of"))
        file_rows.append((_batch_key(label), [label, SERIES_ZH.get(series, series), len(es),
                                              c.get("手动全文精读", 0), c.get("脚本全文通读", 0),
                                              c.get("仅摘要", 0), dup, nf]))
    for _, r in sorted(file_rows, key=lambda x: x[0]):
        tl_rows.append(r)
    tot = [sum(r[i] for r in tl_rows[1:]) for i in (2, 3, 4, 5, 6)]
    tl_rows.append(["合计", "", *tot, ""])
    tl_rows.append([])
    tl_rows.append(["图例"])
    for k, v in LEGEND:
        tl_rows.append([k, v])
    tl_rows.append(["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M"), "", "", "", "", "",
                    "来源索引 generated_at={}".format(index.get("generated_at"))])

    # 按月汇总
    mon: Dict[str, Counter] = defaultdict(Counter)
    for p in papers:
        m = str(p.get("month") or "")[:7]
        mon[m][p.get("series") or "auto"] += 1
        mon[m]["_all"] += 1
        if p.get("has_full_text_reading"):
            mon[m]["_full"] += 1
    ms_rows: List[List[Any]] = [["年月", "自动周报", "手动精读", "书籍精读", "合计", "其中全文精读"]]
    for m in sorted(mon):
        c = mon[m]
        ms_rows.append([m, c.get("auto", 0), c.get("manual", 0), c.get("book", 0), c["_all"], c["_full"]])
    ms_rows.append(["合计", *[sum(r[i] for r in ms_rows[1:]) for i in range(1, 6)]])

    # 明细
    det_rows: List[List[Any]] = [["批次", "来源", "精读程度", "citekey", "年份", "标题", "一句话用处",
                                  "维度", "角色", "期刊", "重复于"]]
    def _rank(p):
        r = p.get("priority_rank")
        return r if isinstance(r, int) else 10 ** 6
    for p in sorted(papers, key=lambda p: (_batch_key(str(p.get("month") or "")), _rank(p), p.get("citekey") or "")):
        bucket = p.get("bucket")
        if isinstance(bucket, list):
            bucket = "/".join(str(b) for b in bucket)
        det_rows.append([str(p.get("month") or ""), SERIES_SHORT.get(p.get("series") or "auto", p.get("series")),
                         _depth(p), p.get("citekey"), p.get("year"), p.get("title"), p.get("one_line"),
                         bucket or None, p.get("role"), p.get("journal"), p.get("duplicate_of")])

    return [
        {"name": "时间线", "rows": tl_rows, "widths": [34, 11, 8, 15, 15, 10, 14, 46]},
        {"name": "按月汇总", "rows": ms_rows, "widths": [12, 11, 11, 11, 9, 13]},
        {"name": "明细", "rows": det_rows, "widths": [30, 7, 14, 30, 7, 70, 40, 9, 15, 30, 34], "autofilter": True},
    ]


def _meta_path(out: Path) -> Path:
    return out.with_name(".{}.meta.json".format(out.name))


def export(index_path: Path, out: Path, force: bool = False) -> str:
    """返回 'written' / 'fresh' / 'missing' / 'broken'。"""
    if not index_path.exists():
        return "missing"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index.get("papers"), list):
            return "broken"
    except Exception:
        return "broken"
    stamp = index.get("generated_at")
    meta = _meta_path(out)
    if not force and out.exists() and meta.exists():
        try:
            if json.loads(meta.read_text(encoding="utf-8")).get("source_index_generated_at") == stamp:
                return "fresh"
        except Exception:
            pass
    out.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(out, build_sheets(index))
    meta.write_text(json.dumps({"source_index_generated_at": stamp, "papers": len(index["papers"]),
                                "written_at": datetime.now().isoformat(timespec="seconds")},
                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return "written"


def main() -> int:
    ap = argparse.ArgumentParser(description="literature_index.json → 札记库入库时间线.xlsx")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--force", action="store_true", help="忽略陈旧判定，强制重写")
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    r = export(repo_path(args.notes_dir) / INDEX_JSON, out, force=args.force)
    if r == "written":
        print("✅ 已写 {}（{} 字节）".format(out, os.path.getsize(out)))
        return 0
    if r == "fresh":
        print("与索引一致，跳过：{}".format(out))
        return 0
    print("⛔ 索引{}：{}".format("缺失" if r == "missing" else "损坏", args.notes_dir), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
