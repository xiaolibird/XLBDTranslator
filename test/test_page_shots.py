# -*- coding: utf-8 -*-
"""原文截图（src/scholar/page_shots.py）。

每条用例都对应 2026-09-20 在真实论文上**亲眼看图**抓到的一个错，数字回执上全都看不出来：
  · Mitra 2023 札记标的是期刊页 13–23，PDF 只有 11 页——拿页码当下标会全错；
  · Scientific Reports 的表注在表**下方**：逐个取最近，Table 1 的表注会配到 Table 2 的表，
    还一路吞进 Discussion 正文（che2018 p.8）；
  · 正文里以「Table 2 presents…」开头的段落被当成表注（shi2025 p.10）；
  · 三线表没有竖线，默认表格检测检不出，间距启发式又把长表从分组横线处截断；
  · 「定位失败，见所在整页」——却没真的渲染整页。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

fitz = pytest.importorskip("fitz")
from src.scholar import page_shots as PS  # noqa: E402


# ---------------- 合成 PDF 的小工具 ----------------

def _grid(page, x0, y0, rows, cols, cw=70, rh=16, text="0.71"):
    for r in range(rows + 1):
        page.draw_line((x0, y0 + r * rh), (x0 + cols * cw, y0 + r * rh))
    for c in range(cols + 1):
        page.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + rows * rh))
    for r in range(rows):
        for c in range(cols):
            page.insert_text((x0 + c * cw + 6, y0 + r * rh + 12), "{}{}".format(text, r * cols + c), fontsize=8)
    return fitz.Rect(x0, y0, x0 + cols * cw, y0 + rows * rh)


def _prose(page, y, first="Discussion of the results follows here with many words.", n=5):
    for i in range(n):
        page.insert_text((60, y + i * 13),
                         (first if i == 0 else "More running prose text that belongs to the body of the paper."),
                         fontsize=10)


def _save(doc, tmp_path, name="p.pdf"):
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


# ---------------- 标签与页码解析 ----------------

def test_labels_cover_chinese_appendix_and_extended_data():
    got = PS.labels_in("主结果（表1、Table 2）与消融（Table A6，Supplementary Table S3），"
                       "见图3 与 Extended Data Fig 1；Algorithm 2 给出流程")
    assert got == ["Table 1", "Table 2", "Supplementary Table A6", "Supplementary Table S3",
                   "Fig. 3", "Extended Data Fig. 1", "Algorithm 2"]


def test_labels_ignore_words_that_merely_contain_biao_or_zero():
    assert PS.labels_in("本文代表1种思路，列表2项，发表3篇；算法0 不存在") == []


def test_parse_pages_single_range_and_garbage():
    assert PS.parse_pages("16") == [16]
    assert PS.parse_pages("15-16") == [15, 16]
    assert PS.parse_pages("17–18") == [17, 18]
    assert PS.parse_pages(None) == [] and PS.parse_pages("附录") == []
    assert PS.parse_pages("3-400") == [3, 400]          # 不是区间（跨度离谱）→ 不展开成 398 页


# ---------------- 页码口径 ----------------

def test_page_map_infers_journal_offset_from_footer(tmp_path):
    doc = fitz.open()
    for i in range(6):
        pg = doc.new_page()
        pg.insert_text((60, 300), "body text")
        pg.insert_text((500, 820), str(155 + i), fontsize=9)                 # 页脚印刷页码
        pg.insert_text((60, 820), "J Am Med Inform Assoc 2021", fontsize=9)  # 每页都有的年份不该赢
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        pm = PS.build_page_map(d, wanted=[156])
    assert (pm.how, pm.offset) == ("header", 154)
    assert pm.to_pdf(156) == 2
    assert pm.to_pdf(12) is None and pm.to_pdf(999) is None                  # 超界绝不就近凑一页


def test_page_map_defaults_to_pdf_order_without_evidence(tmp_path):
    doc = fitz.open()
    for _ in range(4):
        doc.new_page().insert_text((60, 300), "arXiv style page without printed numbers")
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        pm = PS.build_page_map(d, wanted=[3])
    assert pm.how == "pdf-order" and pm.to_pdf(3) == 3 and pm.to_pdf(5) is None


# ---------------- 表格 ----------------

def _two_tables_captions_below(tmp_path):
    doc = fitz.open()
    pg = doc.new_page()
    t1 = _grid(pg, 60, 60, 6, 5, text="1.")
    pg.insert_text((60, t1.y1 + 18), "Table 1. Mortality prediction AUC (mean and std).", fontsize=10)
    t2 = _grid(pg, 60, t1.y1 + 50, 4, 3, text="2.")
    pg.insert_text((60, t2.y1 + 18), "Table 2. Multi-task prediction AUC.", fontsize=10)
    _prose(pg, t2.y1 + 60)
    return _save(doc, tmp_path), t1, t2


def test_caption_below_table_pairs_with_its_own_table(tmp_path):
    pdf, t1, t2 = _two_tables_captions_below(tmp_path)
    with fitz.open(str(pdf)) as d:
        r1, how1 = PS.find_figure_bbox(d[0], "Table 1")
        r2, _ = PS.find_figure_bbox(d[0], "Table 2")
    assert how1 == "caption+table"
    assert r1.contains(t1) and not r1.intersects(t2 + (0, 6, 0, 0))          # 没抓到 Table 2 的表
    assert r2.contains(t2) and r2.y1 < t2.y1 + 60                            # 没吞进后面的正文


def test_body_paragraph_starting_with_table_is_not_a_caption(tmp_path):
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((60, 60), "Table 2", fontsize=10)
    pg.insert_text((60, 74), "Structure learning results of F1.", fontsize=10)
    t = _grid(pg, 60, 90, 5, 4)
    pg.insert_text((60, 400), "Table 2 presents the F1 value for six networks and we discuss it.", fontsize=10)
    pg.insert_text((60, 440), "Tables 2 and 3 summarize the F1 and SHD results.", fontsize=10)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        caps = PS._captions(d[0])
        r, how = PS.find_figure_bbox(d[0], "Table 2")
    assert len(caps) == 1 and caps[0][1].y0 < 80
    assert how == "caption+table" and r.contains(t) and r.y1 < 390


def test_three_line_table_is_found_whole_via_rules(tmp_path):
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((60, 60), "Table 3", fontsize=10)
    pg.insert_text((60, 74), "SHD on benchmark datasets.", fontsize=10)
    ys = [84, 100, 170, 240, 310]                                            # 顶线/表头线/分组线…/底线
    for y in ys:
        pg.draw_line((60, y), (500, y))
    for y in range(96, 306, 14):
        for c in range(6):
            pg.insert_text((66 + c * 72, y + 10), "0.{}".format(30 + c), fontsize=8)
    _prose(pg, 360)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, how = PS.find_figure_bbox(d[0], "Table 3")
    assert how in ("caption+table", "caption+rules")
    assert r.y0 <= 60 and r.y1 >= 308 and r.y1 < 355                         # 到底线为止，不在分组线处截断


# ---------------- 图 ----------------

def test_figure_crop_has_graphics_and_caption_but_not_next_section(tmp_path):
    doc = fitz.open()
    pg = doc.new_page()
    box = fitz.Rect(80, 80, 480, 300)
    pg.draw_rect(box, fill=(0.9, 0.5, 0.5))
    pg.insert_text((90, 100), "a", fontsize=10)
    pg.insert_text((80, 322), "Fig. 2 | Examples of structured missingness.", fontsize=9)
    _prose(pg, 380, first="For data that are MNAR, complete case analysis may introduce bias.")
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, how = PS.find_figure_bbox(d[0], "图2")                            # 中文标签也能配
    assert how == "caption+graphics"
    assert r.contains(box) and r.y1 >= 322 and r.y1 < 375


# ---------------- 面向调用方 ----------------

def test_shots_for_paper_names_fallback_and_idempotence(tmp_path):
    doc = fitz.open()
    for i in range(3):
        pg = doc.new_page()
        pg.insert_text((500, 820), str(13 + i), fontsize=9)
        pg.insert_text((60, 120), "Body of page {}".format(i + 1))
    pg = doc[1]
    pg.insert_text((60, 300), "Fig. 1 | A caption whose graphic we never drew.", fontsize=9)  # 有图注、无图形
    pdf = _save(doc, tmp_path)
    out = tmp_path / "shots"
    hl = [{"text": "高维下更难发现（Fig. 1）", "pages": "14"},
          {"text": "落不到本 PDF 内的页码", "pages": "99"},
          {"text": "提到一张不存在的表 Table 9", "pages": None}]
    r = PS.shots_for_paper(pdf, "mitra2023Learning", hl, out)
    assert r["_map"]["how"] == "header" and r["_map"]["offset"] == 12
    assert r["mitra2023Learning/p0014.jpg"] == {"kind": "page", "printed": 14, "pdf_page": 2}
    fb = r["mitra2023Learning/pdf0002.jpg"]                                  # 裁不准 → 真的渲染了整页
    assert fb["how"] == "figure-fallback" and fb["labels"] == ["Fig. 1"]
    assert (out / "mitra2023Learning/pdf0002.jpg").stat().st_size > 1000
    assert sorted(r["_missing"]) == ["Table 9", "p.99"]                      # 缺什么如实上报
    before = {p: p.stat().st_mtime_ns for p in out.rglob("*.jpg")}
    PS.shots_for_paper(pdf, "mitra2023Learning", hl, out)
    assert before == {p: p.stat().st_mtime_ns for p in out.rglob("*.jpg")}   # 幂等：不重渲染


def test_extended_data_label_is_strict_first_then_lenient(tmp_path):
    doc = fitz.open()
    pg = doc.new_page()
    t = _grid(pg, 60, 90, 4, 4)
    pg.insert_text((60, 70), "Extended Data Table 1 | Hyperparameter search spaces.", fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        assert PS.find_label_page(d, "Table 1") == 1                         # 全文无严格匹配 → 宽松命中
        r, how = PS.find_figure_bbox(d[0], "Table 1")
    assert r.contains(t) and how.endswith("(lenient)")                       # 且回执标明是宽松配的


# ---------------- 接进 md / docx 渲染链 ----------------

def _segment_with_figure(tmp_path):
    from src.scholar.schema import PaperSegment, PaperMetadata, CloseReading
    doc = fitz.open()
    pg = doc.new_page()
    pg.draw_rect(fitz.Rect(80, 80, 480, 300), fill=(0.9, 0.5, 0.5))
    pg.insert_text((80, 322), "Fig. 2 | Examples of structured missingness.", fontsize=9)
    pg.insert_text((500, 820), "16", fontsize=9)
    doc.new_page().insert_text((500, 820), "17", fontsize=9)
    pdf = _save(doc, tmp_path, "mitra.pdf")
    cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        {"heading": "图表与补充材料要点", "sentences": [
            {"text": "Fig. 2a：85 岁以下全部检查，单调模式。", "tag": "可引用证据", "page": "16"},
            {"text": "Fig. 2c 为 MNAR，结构不可观测。", "tag": "可引用证据", "page": "16"}]},
        {"heading": "局限与可质疑点", "sentences": [
            {"text": "正文与图2 的图注有张力。", "tag": "可反驳观点", "page": "17"}]}])
    meta = PaperMetadata(paper_id="pid1", title="Learning from data with structured missingness",
                         authors=["Robin Mitra"], doi="10.1038/s42256-022-00596-z")
    seg = PaperSegment(segment_id=1, paper_id="pid1", metadata=meta, close_reading=cr)
    return seg, pdf


def test_write_notes_embeds_each_figure_once_and_parser_is_unaffected(tmp_path):
    from src.scholar.notes import write_notes
    from src.scholar.notes_index import parse_note_md
    seg, pdf = _segment_with_figure(tmp_path)
    out = tmp_path / "notes"
    out.mkdir()
    res = write_notes([seg], {"pid1": "mitra2023Learning"}, out_dir=out, filename="科研札记_T_手动精读",
                      emit_docx=True, index_series="manual", pdf_paths={"pid1": str(pdf)})
    md = Path(res["note_path"]).read_text("utf-8")
    assert md.count("![Fig. 2 · 原文截图](assets/shots/mitra2023Learning/fig2.jpg)") == 1   # 三句提到，只嵌一次
    first = md.index("Fig. 2a：85 岁以下")
    assert first < md.index("![Fig. 2") < md.index("Fig. 2c 为 MNAR")                        # 紧跟第一次提到它的那句
    assert "[p.16](assets/shots/mitra2023Learning/p0016.jpg)" in md and "p0017.jpg" in md
    assert all((out / m).is_file() for m in
               ("assets/shots/mitra2023Learning/fig2.jpg", "assets/shots/mitra2023Learning/p0016.jpg"))
    entry = parse_note_md(Path(res["note_path"]))[0]
    assert len(entry["highlights"]) == 3                       # 图片行/原文页面节都不是证据句
    assert {h.get("pages") for h in entry["highlights"]} == {"16", "17"}
    from docx import Document
    d = Document(res["docx_path"])
    assert len(d.inline_shapes) == 1 and any("Fig. 2 · 原文截图" in p.text for p in d.paragraphs)


def test_write_notes_without_pdf_is_byte_identical_to_before(tmp_path):
    """没给 pdf_paths（自动月度链路、旧调用方）→ md 里不许多出任何一行。"""
    from src.scholar.notes import write_notes
    seg, pdf = _segment_with_figure(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    ra = write_notes([seg], {"pid1": "k"}, out_dir=a, filename="n", index_series="manual")
    rb = write_notes([seg], {"pid1": "k"}, out_dir=b, filename="n", index_series="manual",
                     pdf_paths={"pid1": str(tmp_path / "missing.pdf")})                      # PDF 不在 → 同样不动
    ma, mb = (Path(r["note_path"]).read_text("utf-8") for r in (ra, rb))
    assert "原文截图" not in ma and "原文页面" not in ma
    strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("generated"))
    assert strip(ma) == strip(mb) and not (b / "assets").exists()


# ---------------- vault 镜像 ----------------

def test_vault_mirrors_referenced_shots_and_rewrites_links(tmp_path):
    from src.scholar import vault as V
    notes, vault = tmp_path / "notes", tmp_path / "vault"
    src = notes / V.SHOT_DIR / "mitra2023Learning"
    src.mkdir(parents=True)
    (src / "fig2.jpg").write_bytes(b"jpg")
    (src / "unused.jpg").write_bytes(b"x")
    body = ["- 〔可引用证据〕Fig. 2a ……〔p.16〕",
            "  ![Fig. 2 · 原文截图](assets/shots/mitra2023Learning/fig2.jpg)",
            "页：[p.16](assets/shots/mitra2023Learning/p0016.jpg)"]          # 源文件缺失的那张
    out, n = V.mirror_shots(body, notes, vault)
    assert out[1] == "  ![Fig. 2 · 原文截图](../assets/shots/mitra2023Learning/fig2.jpg)"
    assert out[2].endswith("(../assets/shots/mitra2023Learning/p0016.jpg)") and out[0] == body[0]
    dst = vault / V.SHOT_DIR / "mitra2023Learning"
    assert n == 1 and (dst / "fig2.jpg").read_bytes() == b"jpg"
    assert not (dst / "unused.jpg").exists() and not (dst / "p0016.jpg").exists()      # 只搬被引用且存在的
    assert V.mirror_shots(body, notes, vault)[1] == 0                                  # 幂等
    assert V.mirror_shots(body, notes, tmp_path / "v2", dry_run=True)[1] == 0 and not (tmp_path / "v2").exists()


def test_caption_without_punctuation_followed_by_capitalised_title_word(tmp_path):
    """投稿版 PDF：「Figure 1 The flowchart of …」是图注；「Figure 1 the flowchart shows」是正文。"""
    doc = fitz.open()
    pg = doc.new_page()
    pg.draw_rect(fitz.Rect(80, 80, 480, 300), fill=(0.5, 0.7, 0.9))
    pg.insert_text((80, 322), "Figure 1 The flowchart of genetic sequencing strategy", fontsize=9)
    pg.insert_text((80, 500), "Figure 1 the flowchart shows how individuals were enrolled.", fontsize=10)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        caps = PS._captions(d[0])
        r, how = PS.find_figure_bbox(d[0], "Fig. 1")
    assert len(caps) == 1 and caps[0][1].y0 < 330
    assert how == "caption+graphics" and r.y1 < 480


def test_labels_supp_abbreviation_and_chinese_quantities():
    assert PS.labels_in("未发生肾衰者被删除（Supp Fig 20）；软件包见 Supp. Table 9") == \
        ["Supplementary Fig. 20", "Supplementary Table 9"]
    assert PS.labels_in("比较了算法 9 种：逻辑回归、XGBoost……；共表 3 个、图 2 张") == []
    assert PS.labels_in("见表 3 与图2a") == ["Table 3", "Fig. 2"]


def test_table_crop_stops_at_next_caption_and_short_heading(tmp_path):
    """serrano2026 p.7：Table 3 之后紧跟一行小节标题、一段正文和 Table 4，都不许裁进来。"""
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((60, 60), "Table 3. Percentage degradation in MAE.", fontsize=10)
    for y in (72, 90, 200):
        pg.draw_line((60, y), (500, y))
    for y in range(96, 196, 14):
        for c in range(5):
            pg.insert_text((66 + c * 86, y + 8), "-5{}.{}*".format(c, y % 7), fontsize=8)
    pg.insert_text((60, 226), "RQ1: Impact of Graph-Aware Mechanisms.", fontsize=10)
    _prose(pg, 244, first="Across configurations, graph-aware missingness markedly increases difficulty, and so on.", n=6)
    pg.insert_text((60, 360), "Table 4. Percentage Summary of Statistical Outcomes.", fontsize=10)
    t4 = _grid(pg, 60, 372, 3, 4)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, how = PS.find_figure_bbox(d[0], "Table 3")
    assert how.startswith("caption+")
    assert r.y0 <= 56 and 198 <= r.y1 < 216 and not r.intersects(t4)


def test_caption_only_region_falls_back_instead_of_returning_a_sliver(tmp_path):
    """he2024 p.8：无线表检不出时，别把一条孤零零的表注当成「这张表」。"""
    doc = fitz.open()
    pg = doc.new_page()
    for y in range(80, 160, 14):
        pg.insert_text((60, y), "NPHA   66.35   65.48   66.35   65.73", fontsize=8)      # 无表线的表体，在表注上方
    pg.insert_text((60, 180), "Table 3: Imputation accuracy w.r.t. different confidence levels.", fontsize=10)
    _prose(pg, 230)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, how = PS.find_figure_bbox(d[0], "Table 3")
    assert r is None or r.height > 60          # 要么老实失败（交给整页兜底），要么真带上了表体


def test_multi_block_caption_is_kept_whole_and_version_bump_rerenders(tmp_path):
    doc = fitz.open()
    pg = doc.new_page()
    pg.draw_rect(fitz.Rect(80, 80, 480, 300), fill=(0.5, 0.8, 0.8))
    pg.insert_text((80, 322), "Figure 6: Distribution of each patient's longest continuous", fontsize=9)
    pg.insert_text((80, 337), "stretch without a 4-month gap between visits.", fontsize=9)      # 续行是另一个文本块
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, _ = PS.find_figure_bbox(d[0], "Fig. 6")
    assert r.y1 >= 337
    out = tmp_path / "s"
    PS.shots_for_paper(pdf, "k", [{"text": "Fig. 6"}], out)
    f = out / "k/fig6.jpg"
    (out / "k/.v").write_text("0")                                   # 假装是旧算法出的图
    (out / "k/stale_wrong_crop.jpg").write_bytes(b"x")
    m0 = f.stat().st_mtime_ns
    PS.shots_for_paper(pdf, "k", [{"text": "Fig. 6"}], out)
    assert f.stat().st_mtime_ns > m0 and not (out / "k/stale_wrong_crop.jpg").exists()
    assert (out / "k/.v").read_text() == PS.SHOT_VERSION


def test_region_is_widened_to_touching_columns_but_not_to_the_next_text_column(tmp_path):
    """lin2004 Table 3：检测到的表比实际窄，右侧几列被切；但不许把相邻栏的正文拉进来。"""
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((60, 60), "Table 3. Estimates of AUCs", fontsize=10)
    t = _grid(pg, 60, 72, 4, 3)                                   # 检得出的部分：x 60–270
    for r in range(4):
        pg.insert_text((276, 72 + r * 16 + 12), "[1.89, 7.25]", fontsize=8)   # 紧贴其右、没有表线的一列
    for i in range(12):                                           # 相邻栏的正文，远高于表
        pg.insert_text((360, 60 + i * 13), "Neighbouring column prose that must stay out.", fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, _ = PS.find_figure_bbox(d[0], "Table 3")
    assert r.x1 >= 320 and r.x1 < 360 and r.contains(t)


def test_wraparound_prose_beside_figure_is_not_pulled_in(tmp_path):
    """cornelis2026 Fig.1：文字环绕排版，左栏正文与图形区纵向重叠，但不是图的一部分。"""
    doc = fitz.open()
    pg = doc.new_page()
    pg.draw_rect(fitz.Rect(300, 80, 540, 300), fill=(0.5, 0.3, 0.8))
    pg.insert_text((300, 322), "Figure 1: Existing graph benchmarks operate in the n >> p regime.", fontsize=9)
    pg.insert_textbox(fitz.Rect(60, 80, 290, 300),
                      "On the other hand, existing GNN inductive benchmarks, ranging from the recent one, "
                      "to established ones like OGB, and others, predominantly operate in the opposite regime. "
                      "As quantified in the figure, which visualizes major datasets, the landscape is defined. "
                      "Consequently, while these benchmarks have driven progress, they fail to reflect it.",
                      fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, how = PS.find_figure_bbox(d[0], "Fig. 1")
    assert how == "caption+graphics" and r.x0 > 250


# ---------------- 逐句原文出处 ----------------

def _fake_embed(texts):
    """确定性假向量：按关键词打到不同维度，既不连网络也能区分段落。"""
    import numpy as np
    keys = ["sexual", "性功能", "satellite", "卫星", "skip", "跳答"]
    pair = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2}
    out = np.full((len(texts), 4), 0.01)
    for i, t in enumerate(texts):
        for k, w in enumerate(keys):
            if w in t:
                out[i, pair[k]] += 1.0
        out[i, 3] += 0.05
    return out


def _three_paragraph_pdf(tmp_path):
    doc = fitz.open()
    pg = doc.new_page()
    paras = {100: "In a hypothetical study on sexual health, men might be reluctant to answer a question on loss of sexual function, so missingness is informative.",
             300: "Data obtained from remote sensing equipment via satellite may be missing observations at certain times due to transient satellite malfunction.",
             500: "A skip pattern occurs when a set of responses are only obtained if the response to an earlier question is Yes, leading to a skip."}
    for y, t in paras.items():
        pg.insert_textbox(fitz.Rect(60, y, 520, y + 120), t, fontsize=10)
    pg.insert_text((60, 700), "References", fontsize=11)
    pg.insert_textbox(fitz.Rect(60, 715, 520, 800), "1. Someone, A. A paper about sexual health surveys and satellite skip patterns. J. X 1, 1-2 (2020).", fontsize=9)
    pg.insert_text((500, 830), "19", fontsize=9)
    p0 = doc.new_page(0)                                   # 前面再垫一页：页码偏移至少两页一致才会被采信
    p0.insert_textbox(fitz.Rect(60, 100, 520, 200), "Title page with an abstract that is unrelated to any of the evidence sentences below.", fontsize=10)
    p0.insert_text((500, 830), "18", fontsize=9)
    return _save(doc, tmp_path, "src.pdf")


def test_each_sentence_gets_its_own_source_passage_and_own_words_are_skipped(tmp_path):
    pdf = _three_paragraph_pdf(tmp_path)
    sents = [{"text": "缺失本身可作信息：男性更不愿回答性功能问题。", "page": None, "section": "结果与效应量"},
             {"text": "批次失败：卫星瞬时故障导致某些时刻观测缺失。", "page": None, "section": "方法与数据"},
             {"text": "这与我的研究直接相关（性功能的例子可引）。", "page": None, "section": "对我研究的联想"}]
    out = tmp_path / "shots"
    got = PS.passages_for_paper(pdf, "mitra2023Learning", sents, out, _fake_embed)
    k0, k1, k2 = (PS.sentence_key(s["text"]) for s in sents)
    assert set(got) == {k0, k1}                      # 「对我研究的联想」是 agent 自己的话，原文里没有出处
    with fitz.open(str(pdf)) as d:
        wins = PS.passage_windows(d)
        assert not any("Someone, A." in w["text"] for w in wins)          # References 之后不作候选
        r = PS.locate_passages(d, sents[:2], _fake_embed)
    assert r[0]["rect"][1] < 250 < 290 < r[1]["rect"][1] < 450            # 各自落在自己的那一段
    assert (out / got[k0]["rel"]).stat().st_size > 800


def test_passage_cache_only_embeds_new_sentences(tmp_path):
    pdf = _three_paragraph_pdf(tmp_path)
    calls = []

    def counting(texts):
        calls.append(len(texts))
        return _fake_embed(texts)
    s1 = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量"}]
    out = tmp_path / "shots"
    PS.passages_for_paper(pdf, "k", s1, out, counting)
    n = len(calls)
    PS.passages_for_paper(pdf, "k", s1, out, counting)
    assert len(calls) == n                           # 句子没变：一次向量都不算
    PS.passages_for_paper(pdf, "k", s1 + [{"text": "跳答模式造成大块缺失。", "section": "方法与数据"}], out, counting)
    assert len(calls) > n and calls[-1] == 1         # 只为新增的那一句算查询向量


def test_write_notes_puts_source_crop_right_under_the_sentence(tmp_path):
    from src.scholar.notes import write_notes
    from src.scholar.notes_index import parse_note_md
    from src.scholar.schema import PaperSegment, PaperMetadata, CloseReading
    pdf = _three_paragraph_pdf(tmp_path)
    cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        {"heading": "结果与效应量", "sentences": [{"text": "男性更不愿回答性功能问题。", "tag": "可引用证据", "page": "19"},
                                         {"text": "卫星瞬时故障导致观测缺失。", "tag": "可引用证据", "page": "19"}]}])
    seg = PaperSegment(segment_id=1, paper_id="p1", close_reading=cr,
                       metadata=PaperMetadata(paper_id="p1", title="T", authors=["A B"], doi="10.1/t"))
    out = tmp_path / "notes"
    out.mkdir()
    res = write_notes([seg], {"p1": "mitra2023Learning"}, out_dir=out, filename="n", index_series="manual",
                      pdf_paths={"p1": str(pdf)}, embed_fn=_fake_embed)
    lines = Path(res["note_path"]).read_text("utf-8").splitlines()
    i = next(n for n, l in enumerate(lines) if "男性更不愿回答" in l)
    assert lines[i + 1].startswith("  ![原文出处 p.19](assets/shots/mitra2023Learning/q_")
    assert sum(l.startswith("  ![原文出处") for l in lines) == 2
    assert len(parse_note_md(Path(res["note_path"]))[0]["highlights"]) == 2       # 截图行不是证据句


def test_page_hint_inside_sentence_text_restricts_the_search(tmp_path):
    """shi2025：句子没有 page 字段，但正文写着「（§5.7, p.14）」——那就只在那一页找。"""
    doc = fitz.open()
    for i in range(3):
        pg = doc.new_page()
        pg.insert_textbox(fitz.Rect(60, 100, 520, 220),
                          "Missing data rate range settings are described here with satellite examples on page {}. "
                          "The remaining words pad this paragraph so that it is long enough to be a window.".format(13 + i), fontsize=10)
        pg.insert_text((500, 830), str(13 + i), fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        pm = PS.build_page_map(d, [14])
        r = PS.locate_passages(d, [{"text": "缺失率三档设置，卫星例子（§5.7, p.14）", "section": "实验方法"}], _fake_embed, pm)
    assert r[0] and r[0]["page"] == 2


def test_two_pdfs_under_one_citekey_do_not_wipe_each_other(tmp_path):
    """同一篇在两个月份桶里各读过一次、PDF 是两份文件：后一次不许删掉前一次的截图。"""
    a = _three_paragraph_pdf(tmp_path)
    b = tmp_path / "copy.pdf"
    b.write_bytes(a.read_bytes() + b"\n% another download\n")
    s = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量"}]
    out = tmp_path / "shots"
    ra = PS.passages_for_paper(a, "umPropagate", s, out, _fake_embed)
    rb = PS.passages_for_paper(b, "umPropagate", s, out, _fake_embed)
    (rel_a,), (rel_b,) = [v["rel"] for v in ra.values()], [v["rel"] for v in rb.values()]
    assert rel_a != rel_b and (out / rel_a).is_file() and (out / rel_b).is_file()
    assert PS.passages_for_paper(a, "umPropagate", s, out, _fake_embed) == ra          # 前一次的缓存仍然有效


def test_shot_version_bump_keeps_passage_crops_and_lost_crops_self_heal(tmp_path):
    """对抗审核 F1：整页图的版本清理曾把同目录的 q_*.jpg 一并删掉，而 passages 缓存仍记着命中——
    那一句的出处图静默消失、重跑多少次都不回来。两道防线各测一遍。"""
    pdf = _three_paragraph_pdf(tmp_path)
    s = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量", "page": "19"}]
    out = tmp_path / "shots"
    PS.shots_for_paper(pdf, "k", s, out)
    got = PS.passages_for_paper(pdf, "k", s, out, _fake_embed)
    (rel,) = [v["rel"] for v in got.values()]
    (out / "k/.v").write_text("0")                                   # 整页图算法升版
    PS.shots_for_paper(pdf, "k", s, out)
    assert (out / rel).is_file()                                     # 第一道：升版清理不碰逐句出处图
    assert PS.passages_for_paper(pdf, "k", s, out, _fake_embed) == got
    (out / rel).unlink()                                             # 第二道：图真丢了 → 下一轮重算补回
    again = PS.passages_for_paper(pdf, "k", s, out, _fake_embed)
    assert again == got and (out / rel).is_file()


def test_first_round_without_page_or_label_then_adding_one_keeps_passage_crops(tmp_path):
    """F1 的另一条触发路径：第一轮没有页码也没有图表标签 → shots_for_paper 早退、不写 .v；
    之后给句子补了页码，下一轮因「.v 不存在」触发清理。真库里有 7 个目录处于这个状态。"""
    pdf = _three_paragraph_pdf(tmp_path)
    s = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量"}]
    out = tmp_path / "shots"
    PS.shots_for_paper(pdf, "k", s, out)
    got = PS.passages_for_paper(pdf, "k", s, out, _fake_embed)
    assert got and not (out / "k/.v").exists()
    PS.shots_for_paper(pdf, "k", [dict(s[0], page="19")], out)
    assert (out / "k/.v").exists() and all((out / v["rel"]).is_file() for v in got.values())


def test_parse_pages_mixed_lists_and_non_main_pages():
    """对抗审核 F4：写法全部来自真库。"""
    assert PS.parse_pages("5, 7-8") == [5, 7, 8]                  # 此前 [5,6,7]
    assert PS.parse_pages("10-11,15") == [10, 11, 15]             # 此前丢 15
    assert PS.parse_pages("2, 10") == [2, 10] and PS.parse_pages("220,224") == [220, 224]
    assert PS.parse_pages("112-5") == [112, 113, 114, 115]
    assert PS.parse_pages("241-259") == [241, 259] and PS.parse_pages("183-187") == [183, 184, 185, 186, 187]
    for junk in ("Supp Fig 20", "Supp Table 3", "S3", "Supp", "§5.7", "附录 B"):
        assert PS.parse_pages(junk) == [], junk                   # 此前会去渲染正文第 20 / 3 / 5 页
    assert PS.parse_pages("§5.7, p.14") == [14]                   # 此前 [5,7,14]
    assert PS.parse_pages("p.14") == [14] and PS.parse_pages("pp. 3-5") == [3, 4, 5]
    assert PS.parse_pages("第4页") == [4] and PS.parse_pages("R71") == [71] and PS.parse_pages("e12") == [12]
    assert PS.parse_pages(7) == [7] and PS.parse_pages("0") == []


def test_vault_prunes_unreferenced_shots_but_keeps_what_any_note_on_disk_still_links(tmp_path):
    """对抗审核 F7：镜像只增不删，源侧改名后 vault 里的旧硬链接成了唯一副本（真库 15,278 个 / 531 MB）。"""
    from src.scholar import vault as V
    vault = tmp_path / "vault"
    d = vault / "assets/shots/k"
    d.mkdir(parents=True)
    for n in ("q_new.jpg", "q_old.jpg", "q_conflict.jpg"):
        (d / n).write_bytes(b"x")
    (vault / "assets/shots/gone").mkdir()
    (vault / "assets/shots/gone/p0001.jpg").write_bytes(b"x")
    (vault / "01-文献").mkdir()
    note = vault / "01-文献/k.md"                        # 冲突笔记：本轮没重写，还挂着旧图名
    note.write_text("- 句子\n  ![原文出处 p.3](../assets/shots/k/q_conflict.jpg)\n", encoding="utf-8")
    gone = V.prune_shots(vault, {("k", "q_new.jpg")}, [note])
    assert sorted(gone) == ["assets/shots/gone/p0001.jpg", "assets/shots/k/q_old.jpg"]
    assert (d / "q_new.jpg").exists() and (d / "q_conflict.jpg").exists()
    assert not (vault / "assets/shots/gone").exists()      # 空目录一并收走
    assert V.prune_shots(vault, {("k", "q_new.jpg")}, [note]) == []
    refs = set()                                           # mirror_shots 顺手把引用收集出来
    V.mirror_shots(["  ![x](assets/shots/k/q_new.jpg)"], tmp_path / "notes", vault, referenced=refs)
    assert refs == {("k", "q_new.jpg")}


def test_without_embedding_service_cached_passages_still_render_and_one_bad_paper_does_not_kill_the_month(tmp_path):
    """对抗审核 F5：(1) Ollama 没起时 finalize 曾把整月 md 重写成没有任何逐句图的版本；
    (2) 任何一篇抛异常都被当成「向量服务挂了」，本轮后面的论文全部没有出处图。"""
    from src.scholar.notes import _make_shots
    from src.scholar.schema import PaperSegment, PaperMetadata, CloseReading
    pdf = _three_paragraph_pdf(tmp_path)

    def seg(pid, text):
        cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
            {"heading": "结果与效应量", "sentences": [{"text": text, "tag": "可引用证据", "page": "19"}]}])
        return PaperSegment(segment_id=1, paper_id=pid, close_reading=cr,
                            metadata=PaperMetadata(paper_id=pid, title="T", authors=["A B"], doi="10.1/" + pid))
    segs = [seg("p1", "男性更不愿回答性功能问题。"), seg("p2", "卫星瞬时故障导致观测缺失。"), seg("p3", "跳答模式造成大块缺失。")]
    cks = {"p1": "k1", "p2": "k2", "p3": "k3"}
    pdfs = {k: str(pdf) for k in cks}
    out = tmp_path / "notes"
    first = _make_shots(segs, cks, pdfs, out, embed_fn=_fake_embed)
    assert all(len(first[p]["_passages"]) == 1 for p in cks)
    offline = _make_shots(segs, cks, pdfs, out, embed_fn=None)               # 向量服务没起
    assert {p: offline[p]["_passages"] for p in cks} == {p: first[p]["_passages"] for p in cks}

    def picky(texts):                                                        # 只对 p1 的句子确定性地炸
        if any("性功能" in t for t in texts):
            raise ValueError("input too long")
        return _fake_embed(texts)
    out2 = tmp_path / "notes2"
    got = _make_shots(segs, cks, pdfs, out2, embed_fn=picky)
    assert got["p1"]["_passages"] == {} and len(got["p2"]["_passages"]) == 1 and len(got["p3"]["_passages"]) == 1


def test_second_pdf_under_one_citekey_gets_its_own_page_and_figure_shots(tmp_path):
    """对抗审核 F3：27f34d3 只给逐句图加了指纹；整页图/图表图仍按 citekey 共用，后一份 PDF 静默
    复用前一份渲染的图（图刚渲染、永远比 PDF 新，陈旧判据不触发）。"""
    def make(name, color):
        doc = fitz.open()
        pg = doc.new_page()
        pg.draw_rect(fitz.Rect(80, 80, 480, 300), fill=color)
        pg.insert_text((80, 322), "Figure 1: A figure whose colour differs between the two versions.", fontsize=9)
        return _save(doc, tmp_path, name)
    a, b = make("a.pdf", (0.9, 0.1, 0.1)), make("b.pdf", (0.1, 0.1, 0.9))
    hl = [{"text": "Fig. 1 显示……", "page": "1"}]
    out = tmp_path / "s"
    ra = {k for k, v in PS.shots_for_paper(a, "choi2020Learning", hl, out).items() if not k.startswith("_")}
    before = {k: (out / k).read_bytes() for k in ra}
    rb = {k for k, v in PS.shots_for_paper(b, "choi2020Learning", hl, out).items() if not k.startswith("_")}
    assert ra == {"choi2020Learning/p0001.jpg", "choi2020Learning/fig1.jpg"}       # 第一份 PDF 占裸名
    assert not (ra & rb) and len(rb) == 2 and all((out / k).is_file() for k in rb)
    assert all((out / k).read_bytes() == v for k, v in before.items())              # 前一份的图没被动
    fig_b = next(k for k in rb if "fig1" in k)
    assert (out / fig_b).read_bytes() != before["choi2020Learning/fig1.jpg"]        # 后一份是自己渲染的
    (out / "choi2020Learning/.v").write_text("0")                                   # 第一份升版重渲染……
    PS.shots_for_paper(a, "choi2020Learning", hl, out)
    assert all((out / k).is_file() for k in rb)                                     # ……不许清掉第二份的图


def test_booktabs_table_with_zero_height_rules_is_cropped_whole(tmp_path):
    """LaTeX booktabs 的表线是画线命令、高度为 0：fitz 把零高矩形当空矩形，`reg |= r` 求并时直接
    忽略，三条线并出来还是第一条 → 被「区域过小」判失败退回整页（TabPFN-3.5 的 Table 2–5）。"""
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 80), "Table 2. Leaderboard using all 51 datasets.", fontsize=9)
    for y in (93, 122, 450):
        pg.draw_line((72, y), (570, y))
    for i in range(20):
        pg.insert_text((78, 140 + i * 15), "Model {}".format(i), fontsize=9)
        pg.insert_text((480, 140 + i * 15), "0.{}".format(40 + i), fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, how = PS.find_figure_bbox(d[0], "Table 2")
    assert r is not None and how == "caption+rules" and r.y0 < 80 and r.y1 >= 450 and r.x1 >= 560


def test_sentence_starting_with_a_figure_number_is_not_a_caption(tmp_path):
    """「Figure 9 places the family on…」是引用它的正文；图在下一页。动词列不全，按「编号后接小写词」判。"""
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_textbox(fitz.Rect(72, 100, 520, 200),
                      "Figure 9 places the model family on the improvability-against-time plane together with "
                      "the tuning trajectories of the classical baselines.", fontsize=10)
    p2 = doc.new_page()
    p2.draw_rect(fitz.Rect(80, 80, 480, 300), fill=(0.2, 0.6, 0.4))
    p2.insert_text((80, 322), "Figure 9. Pareto frontier on the benchmark.", fontsize=9)
    p2.insert_text((80, 400), "Algorithm 1 sample-batch(D)", fontsize=9)               # 小写函数名的算法标题是真图注
    p2.insert_text((80, 430), "Table 1 continued", fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        assert PS.find_label_page(d, "Fig. 9", hint=1) == 2
        kinds = {k[1] + k[2] for k, _ in PS._captions(d[1])}
    assert {"fig9", "algorithm1", "table1"} <= kinds


# ---------------- 逐句出处 v4：2026-09-21 对抗审核 A 的七类结构性缺陷 ----------------

def test_appendix_after_references_is_searchable_but_reference_entries_are_not(tmp_path):
    """References 之后不是全文终点：附录里的消融/补充表恰恰是证据句最爱引的地方。此前一遇到
    References 就彻底停止，出处在附录的句子要么漏配、要么被配到正文里的相似物。"""
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_textbox(fitz.Rect(60, 80, 520, 170), "Data obtained from remote sensing equipment via satellite may be missing "
                      "observations at certain times due to transient satellite malfunction in the main text.", fontsize=10)
    pg.insert_text((60, 200), "References", fontsize=12)
    pg.insert_textbox(fitz.Rect(60, 215, 520, 290), "[1] Someone, A. A paper about sexual health surveys and loss of sexual "
                      "function among men, with a long enough title to form a window. J. X 1, 1-2 (2020).", fontsize=9)
    pg.insert_text((60, 320), "A Additional results", fontsize=12)
    pg.insert_textbox(fitz.Rect(60, 335, 520, 430), "In the appendix study on sexual health, men were reluctant to answer a "
                      "question on loss of sexual function, so the missingness is informative there as well.", fontsize=10)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        wins = PS.passage_windows(d)
        r = PS.locate_passages(d, [{"text": "附录：男性更不愿回答性功能问题。", "section": "结果与效应量"}], _fake_embed)
    assert not any("Someone, A." in w["text"] for w in wins)
    assert any(w["appx"] and "appendix study" in w["text"] for w in wins)
    assert r[0] and r[0]["rect"][1] > 300                                # 落在附录那一段（含其标题行），不是 215–290 的参考文献条目


def test_page_hint_is_soft_a_one_page_slip_still_finds_the_source(tmp_path):
    """札记页码差一页很常见。此前页码是铁律：锁在错页里挑个最像的（yan2024 真出处 p.9、提示 8）。"""
    doc = fitz.open()
    texts = {13: "A skip pattern occurs when a set of responses are only obtained if the response to an earlier question is Yes.",
             14: "Another skip pattern paragraph that talks about a skip in the questionnaire and nothing else at all here.",
             15: "Data obtained via satellite may be missing observations at certain times due to transient satellite malfunction."}
    for n, t in texts.items():
        pg = doc.new_page()
        pg.insert_textbox(fitz.Rect(60, 100, 520, 220), t, fontsize=10)
        pg.insert_text((500, 830), str(n), fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        pm = PS.build_page_map(d, [14])
        r = PS.locate_passages(d, [{"text": "卫星瞬时故障导致观测缺失。", "page": "14", "section": "方法与数据"},
                                   {"text": "跳答模式造成大块缺失。", "page": "14", "section": "方法与数据"}], _fake_embed, pm)
    assert r[0]["page"] == 3 and r[0]["how"] == "page±1"
    assert r[1]["page"] == 2 and r[1]["how"] == "page"                  # 同页能过线就留在同页


def test_sentence_full_of_decimals_is_not_pinned_to_a_caption_that_has_none_of_them(tmp_path):
    """讲表内数字的句子此前常落到图注/表注上，满篇都是的数字（300/500/1000）还给它加分。现在：
    句子带 ≥3 个小数而窗口命中不到 2 个 → 要纯余弦 ≥0.70 才信；本文少见的数字命中才加分。"""
    import numpy as np
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_textbox(fitz.Rect(60, 80, 520, 150), "FIGURE 2 Empirical power CAPTION for n = 300, 500 and 1000 under the "
                      "alternative, with nominal levels as described in the main text of the simulation study.", fontsize=10)
    pg.insert_textbox(fitz.Rect(60, 300, 520, 370), "TABLEROW n = 300 rejection rates 0.012 0.048 0.097 0.151 at the nominal "
                      "levels reported for the first simulation scenario of the study in this table.", fontsize=10)
    pdf = _save(doc, tmp_path)

    def embed(texts):                                   # 图注的余弦 0.65，表格行只有 0.58
        out = []
        for t in texts:
            c = 0.65 if "CAPTION" in t else 0.58 if "TABLEROW" in t else 1.0
            out.append([c, (1 - c * c) ** 0.5, 0.0])
        return np.array(out)
    with fitz.open(str(pdf)) as d:
        r = PS.locate_passages(d, [{"text": "第一类错误：n=300 时分别为 0.012、0.048、0.097、0.151。", "section": "结果与效应量"}], embed)
    assert r[0] and r[0]["rect"][1] > 250 and r[0]["raw"] < 0.62 <= r[0]["score"]


def test_pdf_with_one_text_block_per_line_is_windowed_as_paragraphs():
    """逐行成块的 PDF：此前要么切不出窗口（mathur2026 全篇 12 个），要么截出来只有一行字没法核对。
    （MuPDF 会把自己生成的相邻行并成一块，造不出这种 PDF，所以直接喂碎块。）"""
    def blk(n, y, x0=60, x1=300, lines=1):
        return {"number": n, "lines": [(fitz.Rect(x0, y + i * 12, x1, y + i * 12 + 11), "line {} of block {}".format(i, n), 10.0)
                                       for i in range(lines)]}
    frag = [blk(i, 100 + i * 13) for i in range(6)]                       # 六个单行块，行距 2pt
    merged = PS._merge_fragments(frag)
    assert len(merged) == 1 and len(merged[0]["lines"]) == 6
    far = [blk(0, 100), blk(1, 140)]                                      # 隔了一个段落间距：不并
    assert len(PS._merge_fragments(far)) == 2
    other_col = [blk(0, 100), blk(1, 113, x0=320, x1=560)]                # 另一栏：不并
    assert len(PS._merge_fragments(other_col)) == 2
    normal = [blk(0, 100, lines=5), blk(1, 162, lines=5)]                 # 正常分段的 PDF 不受影响
    assert len(PS._merge_fragments(normal)) == 2


def test_no_source_is_decided_by_keywords_not_by_four_fixed_headings():
    assert PS.has_no_source({"section": "对本论文攻防的意义", "text": "这一条可以直接拿来反驳审稿人。"})
    assert PS.has_no_source({"section": "净影响", "text": "总体对我们有利。"})
    assert PS.has_no_source({"section": "实验方法", "text": "【原文未报告】随机种子、GPU 型号。"})
    assert not PS.has_no_source({"section": "局限与可质疑点", "text": "我的质疑九：每个标签独立挑选编码器。"})   # 事实前提在原文里
    assert not PS.has_no_source({"section": "结果与效应量", "text": "AUC 为 0.87。"})
    assert PS._cjk_len("谱分解图卷积模型与空间图卷积模型的核心思想汇总如下表所示") >= 60 > len("谱分解图卷积模型与空间图卷积模型的核心思想汇总如下表所示")


def test_passage_cache_invalidation_page_hint_model_and_offline_never_deletes(tmp_path, monkeypatch):
    pdf = _three_paragraph_pdf(tmp_path)
    out = tmp_path / "shots"
    calls = []

    class Client:
        def __init__(self, model):
            self.model = model

        def embed(self, texts):
            calls.append((self.model, len(texts)))
            return _fake_embed(texts)
    s = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量"}]
    got = PS.passages_for_paper(pdf, "k", s, out, Client("bge-m3").embed)
    n = len(calls)
    PS.passages_for_paper(pdf, "k", s, out, Client("bge-m3").embed)
    assert len(calls) == n                                               # 同模型同句同页码：全走缓存
    PS.passages_for_paper(pdf, "k", [dict(s[0], page="19")], out, Client("bge-m3").embed)
    assert len(calls) > n                                                # 只订正了页码锚：重算这一句
    n = len(calls)
    PS.passages_for_paper(pdf, "k", [dict(s[0], page="19")], out, Client("other-model").embed)
    assert len(calls) > n and calls[-1][0] == "other-model"             # 换了向量模型：整篇作废重算
    monkeypatch.setattr(PS, "PASSAGE_VERSION", "999")                    # 算法升版了，但向量服务没起
    offline = PS.passages_for_paper(pdf, "k", [dict(s[0], page="19")], out, None)
    assert len(offline) == 1 and all((out / v["rel"]).is_file() for v in offline.values())   # 旧图照出，一张不删


def test_docx_embeds_figure_shots_only_when_asked_and_md_always_does(tmp_path):
    """月度 docx 不内嵌截图（2026-08 一份 85 MB，把周备份从 56 MB 撑到 310 MB）；专题桶照旧内嵌。"""
    import zipfile
    from src.scholar.notes import write_notes
    seg, pdf = _segment_with_figure(tmp_path)
    sizes = {}
    for flag in (True, False):
        out = tmp_path / ("on" if flag else "off")
        out.mkdir()
        res = write_notes([seg], {seg.paper_id: "k2020Fig"}, out_dir=out, filename="n", index_series="manual",
                          pdf_paths={seg.paper_id: str(pdf)}, emit_docx=True, docx_shots=flag)
        with zipfile.ZipFile(res["docx_path"]) as z:
            sizes[flag] = sum(1 for n in z.namelist() if n.startswith("word/media/"))
        assert "assets/shots/k2020Fig/" in Path(res["note_path"]).read_text("utf-8")      # md 两种情况都带图
    assert sizes[True] >= 1 and sizes[False] == 0


def test_topic_bucket_rule():
    import importlib.util
    spec = importlib.util.spec_from_file_location("read_pdf_cli", Path(__file__).resolve().parents[1] / "scripts" / "read_pdf.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    assert rp._is_topic_bucket("2026-09-20-SM源头") and rp._is_topic_bucket("2026-07-17")
    assert not rp._is_topic_bucket("2026-09") and not rp._is_topic_bucket("")


def test_header_offset_is_ignored_when_notes_were_clearly_written_in_pdf_page_order(tmp_path):
    """对抗审核 F6：页眉印着 13–42，札记按 PDF 页序标了 3/14/20。此前 3→落空、14→第 2 页、20→第 8 页。"""
    doc = fitz.open()
    for i in range(30):
        pg = doc.new_page()
        pg.insert_text((60, 100), "body text page {}".format(i + 1), fontsize=10)
        pg.insert_text((500, 830), str(13 + i), fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        pm = PS.build_page_map(d, [3, 14, 20])
        assert pm.how.startswith("pdf-order") and [pm.to_pdf(p) for p in (3, 14, 20)] == [3, 14, 20]
        pm2 = PS.build_page_map(d, [14, 20, 41])                       # 41 > 30 页：只能是印刷页
        assert pm2.how == "header" and pm2.to_pdf(14) == 2
        pm3 = PS.build_page_map(d, [14, 20])                           # 两种口径都说得通：信页眉（原行为）
        assert pm3.how == "header"


def test_files_of_a_pdf_fingerprint_that_no_longer_exists_are_reaped_but_a_living_second_pdf_is_not(tmp_path):
    """对抗审核 F8：PDF 重新下载后指纹变了，旧标记名下的缓存与图此前永远没人删。"""
    import os
    a = _three_paragraph_pdf(tmp_path)
    b = tmp_path / "other_month_copy.pdf"
    b.write_bytes(a.read_bytes() + b"\n% another download\n")
    s = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量", "page": "19"}]
    out = tmp_path / "shots"
    for pdf in (a, b):
        PS.shots_for_paper(pdf, "k", s, out)
        PS.passages_for_paper(pdf, "k", s, out, _fake_embed)
    before = sorted(p.name for p in (out / "k").iterdir())
    tag_b = PS.pdf_tag(b)[1]
    PS.passages_for_paper(a, "k", s, out, _fake_embed)
    assert sorted(p.name for p in (out / "k").iterdir()) == before       # 两份都活着：谁也不动谁
    os.utime(b, ns=(1, 1))                                               # b 被「重新下载」：指纹变了
    PS.shots_for_paper(a, "k", s, out)
    left = [p.name for p in (out / "k").iterdir() if tag_b in p.name]
    assert left == []                                                    # 旧标记名下的 q 图、缓存、整页图全部回收
    assert (out / "k/p0019.jpg").is_file()                               # a 的裸名图不受影响


def test_side_by_side_booktabs_tables_each_caption_takes_the_rules_under_itself(tmp_path):
    """um25a p.25：Table 27 | Table 28 并排。只按纵向取最近的横线，会把左表的表注配给右表的线。"""
    doc = fitz.open()
    pg = doc.new_page()
    for x0, x1, name, ys in ((58, 287, "Table 27. Dataset statistics.", (89, 104, 170)),
                             (309, 539, "Table 28. Statistics of medical datasets.", (88, 103, 146))):
        pg.insert_text((x0 + 40, 76), name, fontsize=9)
        for y in ys:
            pg.draw_line((x0, y), (x1, y))
        for i in range(3):
            pg.insert_text((x0 + 4, 120 + i * 12), "row {} 1,234 5,678".format(i), fontsize=8)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        left, _ = PS.find_figure_bbox(d[0], "Table 27")
        right, _ = PS.find_figure_bbox(d[0], "Table 28")
    assert left.x1 < 300 and left.y1 >= 170                        # 左表：不含右表，且一直到自己的底线
    assert right.x0 > 295 and right.y1 < 165


def test_wordy_table_row_between_the_rules_is_not_a_prose_wall(tmp_path):
    """leon-tramontini26a Table 1：表注在表下方，表里一行的文字长得像正文，表被从那一行拦腰截断。"""
    doc = fitz.open()
    pg = doc.new_page()
    for y in (91, 103, 199):
        pg.draw_line((74, y), (538, y))
    pg.insert_textbox(fitz.Rect(74, 106, 538, 190),
                      "Beattie et al. (2024), using ada-002 (Greene et al., 2022) and GPT-4 (Achiam et al., 2023), "
                      "with a personified role, manually modified criteria definitions, and a JSON response format, "
                      "returning the criterion name, a list of medications, the rationale, and a binary prediction.", fontsize=8)
    pg.insert_text((72, 218), "Table 1: Break down of the methods by the models used for embedding.", fontsize=10)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        r, how = PS.find_figure_bbox(d[0], "Table 1")
    assert how == "caption+rules" and r.y0 <= 91 and r.y1 >= 218


def test_reference_category_cell_in_a_table_is_not_the_references_heading(tmp_path):
    """回归表里的参照组单元格「Reference」不是参考文献标题——当成标题的话后半篇正文全部丢掉。"""
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((60, 80), "Never smoker", fontsize=9)
    pg.insert_text((300, 80), "Reference", fontsize=9)
    pg.insert_textbox(fitz.Rect(60, 200, 520, 300), "Data obtained from remote sensing equipment via satellite may be missing "
                      "observations at certain times due to transient satellite malfunction after the table.", fontsize=10)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        assert any("satellite" in w["text"] for w in PS.passage_windows(d))


def test_number_bonus_cannot_lift_an_unrelated_window_over_the_line(tmp_path):
    """rossiUnreasonable：「缺失率 0.00、0.10…0.99」被配到 Label Propagation 的超参网格
    [0.1,…,0.95,0.99] 上——数字全中、内容无关（raw=0.539）。纯余弦有绝对下限。"""
    import numpy as np
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_textbox(fitz.Rect(60, 80, 520, 170), "GRIDSEARCH Label Propagation. We use the label propagation implementation "
                      "provided in the library. Since the method is sensitive to alpha, we perform a gridsearch over the "
                      "following values: 0.1, 0.2, 0.3, 0.4, 0.5, 0.9, 0.95, 0.99 for each dataset separately.", fontsize=10)
    pdf = _save(doc, tmp_path)

    def embed(texts):                                   # 唯一的窗口余弦 0.54：相关但不是出处
        return np.array([[0.54, 0.84, 0.0] if "GRIDSEARCH" in t else [1.0, 0.0, 0.0] for t in texts])
    sent = [{"text": "图3 和图6 沿横轴报告缺失率 0.1、0.2、0.3、0.4、0.5、0.9、0.95、0.99 下的准确率。", "section": "实验方法"}]
    with fitz.open(str(pdf)) as d:
        assert PS.locate_passages(d, sent, embed)[0] is None
        PS.PASSAGE_MIN_RAW, keep = 0.50, PS.PASSAGE_MIN_RAW      # 放开下限就会配上——证明拦住它的正是这条线
        try:
            assert PS.locate_passages(d, sent, embed)[0] is not None
        finally:
            PS.PASSAGE_MIN_RAW = keep


def test_lines_are_rebuilt_from_words_when_the_block_structure_is_useless(tmp_path):
    """kwaf284（AJE 投稿版）14 页有 6,990 个文本块、3,569 个词——每个词一个块，`_merge_fragments`
    的成对合并够不着，整篇只切出 12 个窗口、被窗口池守卫整篇跳过。MuPDF 自己生成的 PDF 会把同
    一行的词并回一个块，造不出那种结构，所以直接单测按词重建行这一步。"""
    doc = fitz.open()
    pg = doc.new_page()
    for row in range(6):
        x = 50
        for w in "Consider an analysis adjusted for".split():
            pg.insert_text((x, 100 + row * 14), w, fontsize=9); x += len(w) * 5 + 4
        x = 380                                                # 另一栏：与左栏的空隙远大于 12% 页宽
        for w in "In our hypothetical example receiving".split():
            pg.insert_text((x, 100 + row * 14), w, fontsize=9); x += len(w) * 5 + 4
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        lines = PS._lines_from_words(d[0])
    texts = [t for _, t, _ in lines]
    assert texts.count("Consider an analysis adjusted for") == 6
    assert texts.count("In our hypothetical example receiving") == 6
    assert not any("adjusted" in t and "hypothetical" in t for t in texts)      # 两栏不许并进同一行
    assert texts[:6] == ["Consider an analysis adjusted for"] * 6              # 同栏的行在列表里相邻


def test_window_pool_falls_back_to_words_only_when_the_block_path_is_degenerate(tmp_path, monkeypatch):
    """回退判据看**结果**（窗口池够不够），不看块数这类结构代理指标：块结构看着碎但能并回来的
    双栏中文综述走词路径反而会把左右栏交错在一起（吴博综述实测 216 → 1041 个交错窗口）。"""
    doc = fitz.open()
    for _ in range(5):                                         # 守卫只对 ≥4 页的 PDF 生效
        pg = doc.new_page()
        for k, y in enumerate((100, 300, 500)):                # 每页 3 段 → 块路径每页 3 个窗口，不算退化
            pg.insert_textbox(fitz.Rect(60, y, 520, y + 160),
                              "Paragraph {} about data obtained from remote sensing equipment via satellite that "
                              "may be missing observations at certain times due to malfunction. ".format(k) * 2, fontsize=10)
    pdf = _save(doc, tmp_path, "five.pdf")
    with fitz.open(str(pdf)) as d:
        seen = []
        real = PS._windows_from
        monkeypatch.setattr(PS, "_windows_from", lambda doc, from_words: (seen.append(from_words), real(doc, from_words))[1])
        assert PS.passage_windows(d) and seen == [False]        # 块路径够用：根本不去碰词路径
        seen.clear()
        monkeypatch.setattr(PS, "_page_blocks", lambda page: [])   # 块路径颗粒无收
        assert PS.passage_windows(d) and seen == [False, True]     # 回退到词路径并拿到窗口


def test_garbled_text_layer_is_reported_not_silently_zero(tmp_path, caplog):
    """MohanPearlJASA2019：ToUnicode 表坏了，抽出来是 `Ehftqd 6'"( onqsq"xr`（每个字母移了一位）。
    向量匹配自然全在阈值下（失败安全），但要让人看见原因，而不是静默零命中。不做自动纠偏——
    移位不均匀，猜错会产出自信的错图。"""
    doc = fitz.open()
    for _ in range(5):
        pg = doc.new_page()
        pg.insert_textbox(fitz.Rect(60, 100, 520, 700),
                          ("Ehftqd 6 onqsq xr mnsgdq dw lokd ne hcdmshﬀ akd ptdqx ats hm sghr b rd sgd qdbnudq "
                           "ahkhsx ne sgd hcdmshﬀ akd ptdqx hr mns fr q msddc ax sgd sgdnqdl bnmrhcdqdc gdqd. ") * 6,
                          fontsize=9)
    pdf = _save(doc, tmp_path)
    with fitz.open(str(pdf)) as d:
        assert PS.text_looks_garbled(d)
        assert PS.passage_windows(d) == []
    ok = fitz.open()
    for _ in range(5):
        ok.new_page().insert_textbox(fitz.Rect(60, 100, 520, 700),
                                     ("Figure 6 portrays another example of an identifiable query, but in this case the "
                                      "recoverability of the identifiable query is not guaranteed by the theorem here. ") * 6,
                                     fontsize=9)
    ok_pdf = _save(ok, tmp_path, "ok.pdf")
    with fitz.open(str(ok_pdf)) as d:
        assert not PS.text_looks_garbled(d) and PS.passage_windows(d)
