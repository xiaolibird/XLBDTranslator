"""Behavioral checks for the book skill; no model/network calls."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "docs/skills/translate-book/scripts"
spec = importlib.util.spec_from_file_location("translate_book_skill", SCRIPTS / "book.py")
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


def test_strict_json_never_salvages_a_quoted_prefix():
    complete = '{"translations":[{"id":3,"translation":"前句\\\"引语\\\"后句"}]}'
    assert skill.require_json(complete) == complete
    for broken in ['{"translation":"前句"引语"后句"}', '{"translation":"半段', '{"translation":"\\ufffd"}']:
        with pytest.raises(ValueError):
            skill.require_json(broken)


@pytest.fixture
def job(tmp_path, monkeypatch):
    from ebooklib import epub
    monkeypatch.syspath_prepend(str(SCRIPTS))
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "main.py").write_text("")
    actual_repo = SCRIPTS.parents[3]
    (repo / "config/modes.json").write_text((actual_repo / "config/modes.json").read_text())
    (repo / "config/config.env").write_text("API__TRANSLATOR_PROVIDER=deepseek\nAPI__OPENAI_API_KEY=unused-test-value\nPROCESSING__TRANSLATION_MODE=5\nLOGGING__LOG_LEVEL=INFO\n")
    source = tmp_path / "input.epub"
    book = epub.EpubBook()
    book.set_identifier("test-book"); book.set_title("Fixture"); book.set_language("en")
    chapters = []
    for i in range(3):
        c = epub.EpubHtml(title="Chapter " + str(i), file_name=f"ch{i}.xhtml", lang="en")
        c.content = f"<h1>Chapter {i}</h1><p>A source paragraph {i} with a quotation and an ending.</p>"
        book.add_item(c); chapters.append(c)
    book.toc = chapters; book.spine = chapters; book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
    epub.write_epub(str(source), book)
    monkeypatch.setenv("FILES__DOCUMENT_PATH", str(source))
    args = argparse.Namespace(repo=str(repo), source=str(source), mode="5", slug="fixture", output_root=str(tmp_path / "artifacts"), title="阅读样本", author="测试作者", bilingual=False, instructions=None, vision_mode="off")
    skill.prepare(args)
    j = tmp_path / "artifacts/fixture"
    _, meta, cache = skill.state(j)
    raw = skill.load(cache / "structure_map.json")
    for s in raw:
        s["chapter_title"] = "第" + str(s["segment_id"]) + "章"
        s["translated_text"] = "这是完整的中文段落，包含“引语”及希腊字母ἐ。\n\n【译注：必要的简短注解。】"
    skill.save(cache / "structure_map.json", raw)
    return j, cache, raw


def test_stale_or_duplicate_corrections_are_rejected(job):
    j, _, raw = job
    s = raw[0]
    patch = {"segment_id": s["segment_id"], "original_translation_sha256": "stale", "corrected_text": "修正译文", "reason": "实质误译"}
    skill.save(j / "reviews/a_corrections.json", {"corrections": [patch]})
    with pytest.raises(ValueError, match="过期"):
        skill.reviewed(j)
    patch["original_translation_sha256"] = skill.digest(s["translated_text"])
    skill.save(j / "reviews/a_corrections.json", {"corrections": [patch]})
    skill.save(j / "reviews/b_corrections.json", {"corrections": [patch]})
    with pytest.raises(ValueError, match="重复"):
        skill.reviewed(j)


def test_integrity_decision_is_bound_to_current_text(job):
    j, _, raw = job
    s = raw[0]
    s["original_text"] = "source " * 200
    report = skill.audit_data(j, raw)
    assert s["segment_id"] in report["unresolved_candidates"]
    skill.save(j / "reviews/integrity.json", {"integrity_reviews": [{"segment_id": s["segment_id"], "translation_sha256": skill.digest(s["translated_text"]), "complete": True, "reason": "已核对句子和段尾"}]})
    assert not skill.audit_data(j, raw)["unresolved_candidates"]
    s["translated_text"] += "变化"
    assert skill.audit_data(j, raw)["unresolved_candidates"]


def test_source_page_images_do_not_leak_into_monolingual_epub(job):
    j, cache, raw = job
    raw[0]["content_type"] = "image"
    raw[0]["image_path"] = "/private/source-page-with-foreign-text.png"
    skill.save(cache / "structure_map.json", raw)
    args = argparse.Namespace(job=str(j), epub_only=True, pdf_python=None, font=None, bold_font=None, fallback_font=None, font_index=6, bold_font_index=1)
    skill.render(args)
    md = (j / "reviewed/阅读样本.md").read_text()
    assert "source-page-with-foreign-text" not in md
    assert "这是完整的中文段落" in md


def test_split_chapter_title_is_removed_only_on_exact_match(job):
    from navigation import build_view
    j, _, raw = job
    raw[0].update(is_new_chapter=True, chapter_title="第一章　三角欲望",
                  translated_text="第一章\n\n三角欲望\n\n不可删除的正文。")
    raw[1].update(is_new_chapter=True, chapter_title="第二章　不同的标题",
                  translated_text="第二章\n\n这句正文不等于标题。")
    view = build_view(j, raw)
    assert view[0]["translated_text"].strip() == "不可删除的正文。"
    assert view[1]["translated_text"] == raw[1]["translated_text"]


def test_both_reading_formats_and_index_targets(job):
    j, _, raw = job
    ids = [s["segment_id"] for s in raw]
    skill.save(j / "reviews/index.json", {"reviewed": True, "segment_ids": [ids[-1]], "source_locator_count": 2,
                                          "entries": [{"label": "示例词目", "depth": 0, "targets": ids[:2]}]})
    args = argparse.Namespace(job=str(j), epub_only=True, pdf_python=None, font=None, bold_font=None, fallback_font=None, font_index=6, bold_font_index=1)
    skill.render(args)
    final = j / "reviewed"
    md = (final / "阅读样本.md").read_text()
    assert "A source paragraph" not in md
    assert "希腊字母ἐ" in md and "译注" in md
    assert skill.load(final / "quality_report.json")["epub_links_verified"] == 2
    pdf_python = os.environ.get("BOOK_SKILL_PDF_PYTHON")
    if pdf_python:
        subprocess.run([pdf_python, str(SCRIPTS / "render_pdf.py"), str(j)], check=True)
        report = skill.load(final / "quality_report.json")
        assert report["pdf_generated"] and report["pdf_pages"] > 0
        # The final PDF keeps index links and named targets, not merely rendered text.
        check = "from pypdf import PdfReader;import sys;r=PdfReader(sys.argv[1]);assert sum(len(p.get('/Annots',[])) for p in r.pages)>=2;assert all('[Failed' not in p.extract_text() for p in r.pages)"
        subprocess.run([pdf_python, "-c", check, str(final / "阅读样本.pdf")], check=True)


def test_curated_images_are_embedded_and_hash_protected(job):
    from PIL import Image
    import zipfile
    from visual_assets import prepare_assets
    j, _, raw = job
    assets = j / 'assets'
    assets.mkdir()
    image = assets / 'figure.png'
    Image.new('RGB', (300, 180), 'white').save(image)
    entry = {'path': 'assets/figure.png', 'sha256': __import__('hashlib').sha256(image.read_bytes()).hexdigest(),
             'kind': 'diagram', 'reason': '独立图表，不含扫描正文', 'segment_id': raw[0]['segment_id'], 'caption': '图：三层关系。'}
    skill.save(j / 'reviews/visual_assets.json', {'reviewed': True, 'assets': [entry]})
    args = argparse.Namespace(job=str(j), epub_only=True, pdf_python=None, font=None, bold_font=None, fallback_font=None, font_index=6, bold_font_index=1)
    skill.render(args)
    final = j / 'reviewed'
    assert (final / 'assets/figure.png').read_bytes() == image.read_bytes()
    with zipfile.ZipFile(final / '阅读样本.epub') as z:
        assert any(name.endswith('/assets/figure.png') for name in z.namelist())
    assert skill.load(final / 'quality_report.json')['epub_image_references_verified'] == 1
    assert skill.load(final / 'render_segments.json')[0]['visual_assets'][0]['caption'] == '图：三层关系。'
    pdf_python = os.environ.get('BOOK_SKILL_PDF_PYTHON')
    if pdf_python:
        subprocess.run([pdf_python, str(SCRIPTS / 'render_pdf.py'), str(j)], check=True)
        check = "from pypdf import PdfReader;import sys;r=PdfReader(sys.argv[1]);assert sum(len(p.images) for p in r.pages)==1"
        subprocess.run([pdf_python, '-c', check, str(final / '阅读样本.pdf')], check=True)
    image.write_bytes(b'changed')
    with pytest.raises(ValueError, match='变化'):
        prepare_assets(j, raw)
