"""Reading-view curation and validated cross-file EPUB anchors (no network)."""
import copy
import json
import posixpath
import re
import zipfile
from pathlib import Path


def read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def build_view(job, raw):
    policy = read(job / "reviews/render_policy.json", {})
    ids = {s["segment_id"] for s in raw}
    navigation = policy.get("source_navigation", [])
    if any(type(x.get("segment_id")) is not int or x["segment_id"] not in ids or not x.get("reason") for x in navigation):
        raise ValueError("导航替换必须有有效段号及核实理由")
    omitted = {x["segment_id"] for x in navigation}
    index = read(job / "reviews/index.json", {})
    index_ids = index.get("segment_ids", [])
    if index:
        if index.get("reviewed") is not True or not index_ids or len(set(index_ids)) != len(index_ids) or not set(index_ids) <= ids:
            raise ValueError("索引区域需要有效段号及审核记录")
        for entry in index.get("entries", []):
            if not isinstance(entry.get("label"), str) or not entry["label"].strip() or type(entry.get("depth")) is not int or not 0 <= entry["depth"] <= 8:
                raise ValueError("无效索引词目")
            if not set(entry.get("targets", [])) <= ids - omitted - set(index_ids):
                raise ValueError("索引目标无效或会被替换")
        if not index.get("entries"):
            raise ValueError("索引词目不能为空")
        omitted.update(index_ids[1:])
    result = []
    for source in raw:
        if source["segment_id"] in omitted:
            continue
        s = copy.deepcopy(source)
        sid = s["segment_id"]
        s["toc_level"] = int(policy.get("toc_levels", {}).get(str(sid), s.get("toc_level", 1)))
        s["compact"] = sid in policy.get("compact_segment_ids", [])
        if index and sid == index_ids[0]:
            s["index_entries"] = index["entries"]
            parts = ["索引链接指向对应译文片段的起始处，跨片段词目请查阅相邻内容。"]
            for e in index["entries"]:
                refs = "、".join(f"[定位{i + 1}](#seg{target})" for i, target in enumerate(e.get("targets", [])))
                parts.append("　" * e["depth"] + e["label"] + ("　" + refs if refs else ""))
            s["translated_text"] = "\n\n".join(parts)
        elif s.get("is_new_chapter"):
            lines = s["translated_text"].splitlines()
            norm = lambda v: re.sub(r"[\W_]+", "", re.sub(r"^#+\s*", "", v))
            target = norm(s.get("chapter_title", ""))
            # Source books often put a chapter number and subtitle on separate
            # lines. Remove only an exact combined duplicate, never body prose.
            prefix = ""
            for count, (i, line) in enumerate((x for x in enumerate(lines) if x[1].strip())):
                if count >= 3:
                    break
                prefix += norm(line)
                if target and prefix == target:
                    lines = lines[i + 1:]
                    break
                if not target.startswith(prefix):
                    break
            s["translated_text"] = "\n".join(lines)
        result.append(s)
    return result


def render_markdown(renderer, segments, meta):
    text = renderer.templates["document_title"].format(translated_title=meta["title"], original_title=meta["title"])
    if meta.get("author"):
        text += meta["author"] + " 著\n\n"
    for seg in segments:
        chunk = renderer.render_segment(seg, "normal")
        anchor = f'<a id="seg{seg.segment_id}"></a>'
        if seg.is_new_chapter and seg.chapter_title:
            chunk = re.sub(r"(?m)^(#{1,6} [^\n]+)", lambda m: m.group(1) + " " + anchor, chunk, count=1)
        else:
            chunk = anchor + "\n\n" + chunk
        text += chunk
    return text


def relink_epub(path):
    from lxml import etree
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        data = {info.filename: z.read(info.filename) for info in infos}
    roots, owners = {}, {}
    for name, content in data.items():
        if name.endswith(".xhtml"):
            root = etree.fromstring(content)
            roots[name] = root
            for el in root.xpath("//*[@id]"):
                anchor = el.get("id", "")
                if re.fullmatch(r"seg\d+", anchor):
                    if anchor in owners:
                        raise ValueError("重复 EPUB 锚点：" + anchor)
                    owners[anchor] = name
    count = 0
    for name, root in roots.items():
        for a in root.xpath('//*[local-name()="a"][@href]'):
            href = a.get("href", "")
            if re.fullmatch(r"#seg\d+", href):
                if href[1:] not in owners:
                    raise ValueError("EPUB 索引目标缺失：" + href)
                a.set("href", posixpath.relpath(owners[href[1:]], posixpath.dirname(name)) + href)
                count += 1
        data[name] = etree.tostring(root, encoding="utf-8", xml_declaration=True)
    temp = path.with_suffix(".tmp.epub")
    with zipfile.ZipFile(temp, "w") as z:
        for info in infos:
            z.writestr(info, data[info.filename])
    with zipfile.ZipFile(temp) as z:
        if z.testzip() is not None:
            raise ValueError("EPUB ZIP 完整性检查失败")
    temp.replace(path)
    return count
