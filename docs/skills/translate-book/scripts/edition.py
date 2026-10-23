"""Explicit, hash-bound editorial omissions; never certify them as complete text."""
import copy
import hashlib
import json


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def publication_segments(job, meta, raw):
    path = job / 'reviews/abridgement.json'
    restriction = job / 'reviews/delivery_restriction.json'
    blocked = json.loads(restriction.read_text()) if restriction.exists() else {}
    if not path.exists():
        if blocked.get('restricted_source_segment_ids'):
            raise ValueError('存在禁止整书导出的内容；须先审核并授权明确标记的删节版')
        return raw
    plan = json.loads(path.read_text())
    if not (plan.get('reviewed') is True and plan.get('user_approved') is True
            and plan.get('approval_text') and plan.get('reviewer')
            and plan.get('source_sha256') == meta['source_sha256']):
        raise ValueError('删节方案缺少用户授权、审核或匹配的源文件哈希')
    by_id = {s['segment_id']: s for s in raw}
    omissions = {}
    for item in plan.get('omissions', []):
        sid = item.get('segment_id')
        if type(sid) is not int or sid not in by_id or sid in omissions:
            raise ValueError('删节段号无效或重复')
        source = by_id[sid]
        if (item.get('source_sha256') != sha(source['original_text'])
                or item.get('translation_sha256') != sha(source.get('translated_text', ''))):
            raise ValueError('删节边界已过期：' + str(sid))
        if not item.get('reason') or not item.get('notice', '').startswith('【编者删节：'):
            raise ValueError('删节须保留明确可见的省略说明')
        omissions[sid] = item
    if not omissions or not set(blocked.get('restricted_source_segment_ids', [])) <= omissions.keys():
        raise ValueError('删节方案未覆盖所有已确认限制段落')
    intro = plan.get('reader_notice', '')
    before = plan.get('notice_before_segment_id')
    if not intro or '删节' not in intro or before not in by_id:
        raise ValueError('删节版须有可见的版本说明与位置')
    out = []
    for source in raw:
        sid = source['segment_id']
        if sid == before:
            out.append({'segment_id': max(by_id) + 1, 'original_text': '',
                        'translated_text': intro, 'is_new_chapter': True,
                        'chapter_title': '删节阅读版说明', 'toc_level': 1,
                        'content_type': 'text', 'image_path': None, 'page_index': 0,
                        'edition_notice': True})
        segment = copy.deepcopy(source)
        if sid in omissions:
            item = omissions[sid]
            segment.update(original_text='', translated_text=item['notice'],
                           content_type='text', image_path=None,
                           editorial_omission=True, omission_reason=item['reason'])
            group_start = item.get('group_start_segment_id', sid)
            if group_start not in omissions or group_start > sid:
                raise ValueError('删节组起点无效')
            segment['omission_group_start_id'] = group_start
            if item.get('chapter_title'):
                segment['chapter_title'] = item['chapter_title']
                segment['is_new_chapter'] = bool(item.get('is_new_chapter'))
                segment['toc_level'] = int(item.get('toc_level', 2))
            # No former inline image/text payload may pass through a placeholder.
            segment.pop('visual_assets', None)
        out.append(segment)
    return out


def edition_scope(raw):
    omitted = [s['segment_id'] for s in raw if s.get('editorial_omission')]
    return {'edition': 'abridged' if omitted else 'complete_reading_edition',
            'intentionally_omitted_segment_ids': omitted,
            'completeness_scope': '仅检查删节后保留正文；省略段不视作完整译文' if omitted else '当前审校正文'}
