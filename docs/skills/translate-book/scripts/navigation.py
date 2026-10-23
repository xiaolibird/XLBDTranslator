"""Reading-view curation and validated cross-file EPUB anchors (no network)."""
import copy
import hashlib
import json
import posixpath
import re
import zipfile
from pathlib import Path


def read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def split_internal_headings(segments, plan):
    """Split only verified standalone translated headings in the reading view."""
    next_id = max((s["segment_id"] for s in segments), default=-1) + 1
    by_id = {r["segment_id"]: r for r in plan}
    result = []
    for source in segments:
        rule = by_id.get(source["segment_id"])
        if not rule:
            result.append(source)
            continue
        text = source.get("translated_text", "")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != rule["translation_sha256"]:
            raise ValueError("段内章目计划已过期：" + str(source["segment_id"]))
        matches = []
        for h in rule["headings"]:
            pattern = r"(?m)^[^\S\n]*(?:\#{1,6}[^\S\n]*)?(?:\*\*)?" + re.escape(h["text"]) + r"(?:\*\*)?[^\S\n]*$"
            found = list(re.finditer(pattern, text))
            if len(found) != 1:
                raise ValueError("段内标题必须唯一完整命中：" + h["text"])
            matches.append((found[0].start(), found[0].end(), h))
        matches.sort(key=lambda x: x[0])
        boundaries = [(0, None, 0)]
        for start, end, heading in matches:
            if start == 0:
                boundaries[0] = (0, heading, end)
            else:
                boundaries.append((start, heading, end))
        for i, (start, heading, end) in enumerate(boundaries):
            stop = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
            segment = copy.deepcopy(source)
            if i:
                segment["source_segment_id"] = source["segment_id"]
                segment["segment_id"] = next_id
                next_id += 1
            # The authoritative original remains in reviewed/structure.json.
            # Reading-only chunks must not duplicate the full source segment.
            segment["original_text"] = ""
            segment["translated_text"] = text[end if heading else start:stop].strip()
            segment["is_new_chapter"] = bool(heading) or (i == 0 and bool(source.get("is_new_chapter")) and not rule.get("prefix_is_continuation", False))
            if heading:
                segment["chapter_title"] = heading["title"]
                segment["toc_level"] = heading.get("toc_level", 1)
            result.append(segment)
    return result


def build_view(job, raw):
    policy = read(job / "reviews/render_policy.json", {})
    ids = {s["segment_id"] for s in raw}
    chapter_metadata = {}
    for update in policy.get('chapter_metadata_updates', []):
        sid = update.get('segment_id')
        if type(sid) is not int or sid not in ids or sid in chapter_metadata:
            raise ValueError('章目元数据段号无效或重复')
        if set(update) - {'segment_id', 'is_new_chapter', 'chapter_title', 'toc_level'}:
            raise ValueError('章目元数据只能更新标题、层级与章首标记')
        if type(update.get('is_new_chapter')) is not bool or not isinstance(update.get('chapter_title'), str) or type(update.get('toc_level')) is not int or not 1 <= update['toc_level'] <= 6:
            raise ValueError('章目元数据格式无效')
        if any(s['segment_id'] == sid and (s.get('editorial_omission') or s.get('edition_notice')) for s in raw):
            raise ValueError('不能用章目更新覆盖删节或版本说明')
        chapter_metadata[sid] = update
    navigation = policy.get("source_navigation", [])
    if any(type(x.get("segment_id")) is not int or x["segment_id"] not in ids or not x.get("reason") for x in navigation):
        raise ValueError("导航替换必须有有效段号及核实理由")
    omitted = {x["segment_id"] for x in navigation}
    if omitted & {s['segment_id'] for s in raw if s.get('editorial_omission') or s.get('edition_notice')}:
        raise ValueError('不能用导航替换隐藏删节位置或版本说明')
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
    text_edits = {record["segment_id"]: record for record in policy.get("reading_text_edits", [])}
    for source in raw:
        if source["segment_id"] in omitted:
            continue
        s = copy.deepcopy(source)
        sid = s["segment_id"]
        s.update(chapter_metadata.get(sid, {}))
        if sid in text_edits:
            edit_plan = text_edits[sid]
            text = s.get("translated_text", "")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != edit_plan["translation_sha256"]:
                raise ValueError("阅读层文字清理已过期：" + str(sid))
            last_start = len(text) + 1
            for edit in sorted(edit_plan["edits"], key=lambda e: e["start"], reverse=True):
                start, end = edit["start"], edit["end"]
                if not edit.get("reason") or not 0 <= start <= end <= len(text) or end > last_start or text[start:end] != edit["expected"]:
                    raise ValueError("无效或重叠的阅读层清理：" + str(sid))
                text = text[:start] + edit.get("replacement", "") + text[end:]
                last_start = start
            s["translated_text"] = text
        s["toc_level"] = int(policy.get("toc_levels", {}).get(str(sid), s.get("toc_level", 1)))
        s["compact"] = sid in policy.get("compact_segment_ids", [])
        if index and sid == index_ids[0]:
            s["is_new_chapter"] = True
            s["chapter_title"] = index.get("title") or "索引"
            s["index_entries"] = index["entries"]
            s["index_reader_note"] = index.get("note_for_reader") or "索引链接指向对应译文片段的起始处，跨片段词目请查阅相邻内容。"
            parts = [s["index_reader_note"]]
            for e in index["entries"]:
                refs = "、".join(f"[定位{i + 1}](#seg{target})" for i, target in enumerate(e.get("targets", [])))
                parts.append("　" * e["depth"] + e["label"] + ("　" + refs if refs else ""))
            s["translated_text"] = "\n\n".join(parts)
        elif s.get("is_new_chapter") and sid not in {item["segment_id"] for item in policy.get("in_segment_headings", [])}:
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
    order = policy.get("reading_order")
    if order is not None:
        if len(order) != len(ids) or set(order) != ids:
            raise ValueError("阅读顺序必须恰好包含所有源片段，不能借重排删掉正文")
        rank = {sid: i for i, sid in enumerate(order)}
        result.sort(key=lambda segment: rank[segment["segment_id"]])
    result = split_internal_headings(result, policy.get("in_segment_headings", []))
    # Keep one visible notice per continuous omitted scene, with all source IDs
    # retained in its provenance. This is editorial omission, not navigation.
    grouped, omission_groups = [], {}
    for segment in result:
        if not segment.get('editorial_omission'):
            grouped.append(segment)
            continue
        start = segment.get('omission_group_start_id', segment['segment_id'])
        if start == segment['segment_id']:
            segment['omitted_source_segment_ids'] = [segment['segment_id']]
            omission_groups[start] = segment
            grouped.append(segment)
        else:
            if start not in omission_groups:
                raise ValueError('连续删节缺少起始提示')
            omission_groups[start]['omitted_source_segment_ids'].append(segment['segment_id'])
    result = grouped
    view_order = policy.get("reading_view_order")
    if view_order is not None:
        if len(view_order) != len(result) or set(view_order) != {segment["segment_id"] for segment in result}:
            raise ValueError("阅读视图顺序必须恰好保留所有阅读片段")
        rank = {sid: i for i, sid in enumerate(view_order)}
        result.sort(key=lambda segment: rank[segment["segment_id"]])
    return result


def render_markdown(renderer, segments, meta):
    text = renderer.templates["document_title"].format(translated_title=meta["title"], original_title=meta["title"])
    if meta.get("author"):
        text += meta["author"] + " " + meta.get("author_role", "著") + "\n\n"
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
