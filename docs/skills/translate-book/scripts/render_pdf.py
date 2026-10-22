#!/usr/bin/env python3
"""Render the same reviewed reading view as EPUB, using a ReportLab runtime."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("job")
    p.add_argument("--font"); p.add_argument("--bold-font"); p.add_argument("--fallback-font")
    p.add_argument("--font-index", type=int, default=6); p.add_argument("--bold-font-index", type=int, default=1)
    args = p.parse_args()
    job = Path(args.job).resolve()
    load = lambda path: json.loads(path.read_text(encoding="utf-8"))
    meta = load(job / "book.json")
    raw = load(job / "reviewed/render_segments.json")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_CENTER
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, Paragraph as BaseParagraph, Spacer, PageBreak, Image, KeepTogether
    from reportlab.platypus.tableofcontents import TableOfContents
    import reportlab.platypus.paragraph as paragraph_module
    import reportlab.lib.textsplit as textsplit_module
    paragraph_module.ALL_CANNOT_START += "，；：！？）》】〕〉”’％…"
    textsplit_module.ALL_CANNOT_START += "，；：！？）》】〕〉”’％…"
    default_font = Path("/System/Library/Fonts/Supplemental/Songti.ttc")
    font = Path(args.font) if args.font else default_font
    if not font.is_file():
        raise ValueError("没有中文字体；用 --font 提供可嵌入的 TrueType 字体或字体集合")
    normal_index = args.font_index if font.suffix.lower() == ".ttc" else 0
    bold = Path(args.bold_font) if args.bold_font else font
    bold_index = args.bold_font_index if bold.suffix.lower() == ".ttc" else 0
    pdfmetrics.registerFont(TTFont("Book", str(font), subfontIndex=normal_index))
    pdfmetrics.registerFont(TTFont("BookBold", str(bold), subfontIndex=bold_index))
    pdfmetrics.registerFontFamily("Book", normal="Book", bold="BookBold", italic="Book", boldItalic="BookBold")
    chars = set(meta["title"] + meta.get("author", "") + "目录中文阅读版 著索引定位页码片段")
    for s in raw:
        for asset in s.get('visual_assets', []):
            chars.update(asset.get('caption', ''))
        chars.update(s.get("translated_text", "")); chars.update(s.get("chapter_title", ""))
        if meta["retain_original"]:
            chars.update(s.get("original_text", ""))
        for e in s.get("index_entries", []):
            chars.update(e["label"])
    primary = pdfmetrics.getFont("Book").face.charToGlyph
    missing = {c for c in chars if not c.isspace() and ord(c) not in primary}
    fallback_chars = set()
    if missing:
        fallback = Path(args.fallback_font) if args.fallback_font else Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf")
        if fallback.is_file():
            pdfmetrics.registerFont(TTFont("Fallback", str(fallback)))
            pdfmetrics.registerFontFamily("Fallback", normal="Fallback", bold="Fallback", italic="Fallback", boldItalic="Fallback")
            covered = pdfmetrics.getFont("Fallback").face.charToGlyph
            fallback_chars = {c for c in missing if ord(c) in covered}
        if missing - fallback_chars:
            raise ValueError("字体缺字：" + "".join(sorted(missing - fallback_chars)))

    def markup(text):
        text = html.escape(text)
        if fallback_chars:
            pattern = "[" + re.escape("".join(fallback_chars)) + "]+"
            text = re.sub(pattern, lambda m: '<font name="Fallback">' + m.group() + "</font>", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
        return re.sub(r"【译注[：:].*?】", lambda m: '<font size="9.5" color="#4a4a4a">' + m.group() + "</font>", text)

    class Paragraph(BaseParagraph):
        def split(self, *a, **kw):
            parts = super().split(*a, **kw)
            if parts and hasattr(self, "segment_id"):
                parts[0].segment_id = self.segment_id
            return parts

    width, height, margin = 160 * mm, 240 * mm, 21 * mm
    cover_size = (min(32, (width - 2 * margin - 4) /
                      max(1, pdfmetrics.stringWidth(meta["title"], "BookBold", 1)))
                  if len(meta["title"]) <= 16 else 26)
    styles = {
        "body": ParagraphStyle("body", fontName="Book", fontSize=11, leading=19.5, wordWrap="CJK", alignment=TA_JUSTIFY, firstLineIndent=22, spaceAfter=5, allowWidows=0, allowOrphans=0),
        "note": ParagraphStyle("note", fontName="Book", fontSize=9.5, leading=16.5, wordWrap="CJK", alignment=TA_LEFT, spaceAfter=6),
        "heading": ParagraphStyle("heading", fontName="BookBold", fontSize=19, leading=29, wordWrap="CJK", spaceBefore=12, spaceAfter=24, keepWithNext=True),
        "sub": ParagraphStyle("sub", fontName="BookBold", fontSize=13, leading=23, wordWrap="CJK", spaceBefore=10, spaceAfter=12, keepWithNext=True),
        "cover": ParagraphStyle("cover", fontName="BookBold", fontSize=cover_size, leading=44, alignment=TA_CENTER, wordWrap="CJK", spaceAfter=18),
        "center": ParagraphStyle("center", fontName="Book", fontSize=13, leading=25, alignment=TA_CENTER, wordWrap="CJK", spaceAfter=16),
    }
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", meta["title"]).strip() or "中文译本"
    output = job / "reviewed" / (title + ".pdf")

    class Book(BaseDocTemplate):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.current_headers = {}

        def beforeDocument(self):
            self.header = ""
            self.previous_headers = dict(self.current_headers)
            self.current_headers = {}
            self.segment_pages = {}
            self.headings = []

        def afterFlowable(self, flow):
            if hasattr(flow, "segment_id") and flow.segment_id not in self.segment_pages:
                self.segment_pages[flow.segment_id] = self.page
                self.canv.bookmarkPage("seg" + str(flow.segment_id))
            if hasattr(flow, "book_heading"):
                label, level, key = flow.book_heading
                self.header = label
                self.current_headers[self.page] = label
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(label, key, level=0, closed=False)
                self.notify("TOCEntry", (level, label, self.page, key))
                self.headings.append([label, self.page])

    asset_report = load(job / 'reviewed/quality_report.json')
    covers = [a for a in asset_report.get('visual_assets', []) if a['kind'] == 'cover']

    def page(canvas, doc):
        if doc.page <= (2 if covers else 1):
            return
        canvas.saveState(); canvas.setFillColor(HexColor("#747474")); canvas.setFont("Book", 8)
        canvas.drawString(margin, height - 13 * mm, meta["title"][:18])
        header = doc.previous_headers.get(doc.page, doc.header) or "目录"
        canvas.drawRightString(width - margin, height - 13 * mm, header if len(header) <= 22 else header[:21] + "…")
        canvas.setStrokeColor(HexColor("#d6d6d6")); canvas.setLineWidth(.35)
        canvas.line(margin, height - 15 * mm, width - margin, height - 15 * mm)
        canvas.setFont("Book", 9); canvas.drawCentredString(width / 2, 12 * mm, str(doc.page)); canvas.restoreState()

    def build(target_pages):
        doc = Book(str(output), pagesize=(width, height), leftMargin=margin, rightMargin=margin, topMargin=23 * mm, bottomMargin=23 * mm,
                   title=meta["title"], author=meta.get("author", ""), pageCompression=1)
        doc.addPageTemplates(PageTemplate(id="book", frames=[Frame(margin, 23 * mm, width - 2 * margin, height - 46 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)], onPage=page))
        story = [Spacer(1, 43 * mm), Paragraph(markup(meta["title"]), styles["cover"]), Spacer(1, 14 * mm), Paragraph(markup(meta.get("author", "")) + " 著", styles["center"]), Spacer(1, 38 * mm), Paragraph("中文阅读版", styles["center"]), PageBreak(), Paragraph("目录", styles["heading"])]
        if covers:
            image = Image(str(job / 'reviewed' / covers[0]['render_path']))
            image._restrictSize(width - 2 * margin, height - 48 * mm)
            story = [image, PageBreak()] + story
        toc = TableOfContents(); toc.dotsMinLevel = 0
        toc.levelStyles = [ParagraphStyle("TOC" + str(i), fontName="Book", fontSize=10.5, leading=19, wordWrap="CJK", leftIndent=i * 12, rightIndent=18, spaceBefore=5) for i in range(6)]
        story.extend([toc, PageBreak()])
        for seg in raw:
            sid, attached = seg["segment_id"], False

            def append(text, style):
                nonlocal attached
                flow = Paragraph(text, style)
                if not attached:
                    flow.segment_id = sid; attached = True
                story.append(flow)
                return flow

            if seg.get("is_new_chapter"):
                if not isinstance(story[-1], PageBreak):
                    story.append(PageBreak())
                label = seg.get("chapter_title") or meta["title"]
                flow = append(markup(label), styles["heading"])
                flow.book_heading = (label, min(5, max(0, seg.get("toc_level", 1) - 1)), "chapter" + str(sid))
            if seg.get("index_entries"):
                append("索引依据原书链接定位到对应译文片段起始页。页码可点击；跨页词目请查阅相邻页。", styles["note"])
                entries = seg["index_entries"]
                for i, e in enumerate(entries):
                    parent = i + 1 < len(entries) and entries[i + 1]["depth"] > e["depth"]
                    label = markup(e["label"])
                    if parent:
                        label = "<b>" + label + "</b>"
                    refs = "、".join('<link href="#seg' + str(t) + '">' + str(target_pages.get(t, 0)) + "</link>" for t in e.get("targets", []))
                    style = ParagraphStyle("index", fontName="Book", fontSize=9.5, leading=16.5, wordWrap="CJK", leftIndent=e["depth"] * 12, spaceAfter=2, keepWithNext=parent)
                    append(label + ("　" + refs if refs else ""), style)
                continue
            for asset in seg.get('visual_assets', []):
                image = Image(str(job / 'reviewed' / asset['render_path']))
                image._restrictSize(width - 2 * margin, (55 if asset['kind'] == 'illustration' else 105) * mm)
                caption = Paragraph(markup(asset.get('caption', '原书插图')), styles['note'])
                story.append(KeepTogether([image, Spacer(1, 3 * mm), caption, Spacer(1, 4 * mm)]))
            texts = [(seg.get("translated_text", ""), False)]
            if meta["retain_original"]:
                texts.append((seg.get("original_text", ""), True))
            for content, original in texts:
                for para in re.split(r"\n\s*\n", content):
                    para = para.strip()
                    if not para or re.fullmatch(r"[-*_]{3,}", para):
                        continue
                    if para.startswith("#"):
                        append(markup(re.sub(r"^#+\s*", "", para)), styles["sub"])
                        continue
                    note = original or seg.get("compact") or bool(re.match(r"^(?:\[\^?\d+\]|\d+[.．、]|【(?:译注|原注))", para))
                    para = re.sub(r"^>\s*", "", para, flags=re.M)
                    append("<br/>".join(markup(line) for line in para.splitlines()), styles["note" if note else "body"])
        doc.multiBuild(story)
        return doc

    targets = {t for s in raw for e in s.get("index_entries", []) for t in e.get("targets", [])}
    pages = {}
    for _ in range(4):
        doc = build(pages)
        if not targets or all(pages.get(t) == doc.segment_pages.get(t) for t in targets):
            break
        pages = dict(doc.segment_pages)
    else:
        raise ValueError("索引页码排版未收敛；不能交付未核准的页码")
    if not targets <= doc.segment_pages.keys():
        raise ValueError("部分索引目标没有 PDF 锚点")
    report_path = job / "reviewed/quality_report.json"
    report = load(report_path)
    report.update({"pdf_generated": True, "pdf_pages": doc.page, "pdf_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "font_missing_characters": 0,
                   "visual_review": "pending: inspect latest rendered PNG pages"})
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (job / "reviewed/pdf_layout.json").write_text(json.dumps({"headings": doc.headings, "segment_pages": doc.segment_pages}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output), doc.page, "pages; visual inspection still required")


if __name__ == "__main__":
    main()
