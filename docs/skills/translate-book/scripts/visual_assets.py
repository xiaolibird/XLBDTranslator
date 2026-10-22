"""Embed only explicitly reviewed visual assets, never implicit scanned pages."""
import hashlib
import html
import json
import mimetypes
import shutil
from pathlib import Path


def prepare_assets(job, view):
    manifest = job / 'reviews/visual_assets.json'
    if not manifest.exists():
        return []
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('reviewed') is not True:
        raise ValueError('插图清单须经过审核')
    ids = {s['segment_id'] for s in view}
    assets, names = [], set()
    dest = job / 'reviewed/assets'
    dest.mkdir(parents=True, exist_ok=True)
    for entry in data.get('assets', []):
        item = dict(entry)
        path = (job / item['path']).resolve()
        if not path.is_relative_to(job.resolve()) or not path.is_file():
            raise ValueError('插图必须位于本书任务目录内')
        if item.get('kind') not in {'cover', 'illustration', 'diagram'} or not item.get('reason'):
            raise ValueError('插图须标明类型和核实理由，不能直接嵌入原文扫描页')
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256'):
            raise ValueError('插图已变化，需重新审核')
        if item['kind'] != 'cover' and item.get('segment_id') not in ids:
            raise ValueError('插图目标片段不存在')
        name = path.name
        if name in names:
            raise ValueError('插图文件名重复')
        names.add(name)
        shutil.copyfile(path, dest / name)
        item['render_path'] = 'assets/' + name
        assets.append(item)
    if sum(a['kind'] == 'cover' for a in assets) > 1:
        raise ValueError('只允许一张封面')
    for seg in view:
        seg['visual_assets'] = [a for a in assets if a['kind'] != 'cover' and a['segment_id'] == seg['segment_id']]
    return assets


def figure(item):
    caption = html.escape(item.get('caption', '原书插图'))
    return f'<figure><img src="{item["render_path"]}" alt="{caption}"/><figcaption>{caption}</figcaption></figure>\n\n'


def embed_epub(path, assets):
    if not assets:
        return 0
    import zipfile, posixpath, tempfile, os
    from lxml import etree
    with zipfile.ZipFile(path) as source:
        payloads = {i.filename: (i, source.read(i.filename)) for i in source.infolist()}
    container = etree.fromstring(payloads['META-INF/container.xml'][1])
    opf_name = container.xpath('//*[local-name()="rootfile"]')[0].get('full-path')
    opf = etree.fromstring(payloads[opf_name][1])
    manifest = opf.xpath('//*[local-name()="manifest"]')[0]
    metadata = opf.xpath('//*[local-name()="metadata"]')[0]
    namespace = etree.QName(manifest).namespace
    for index, item in enumerate(assets):
        name = item['render_path']
        uid = 'book_visual_' + str(index)
        attributes = {'id': uid, 'href': name, 'media-type': mimetypes.guess_type(name)[0]}
        if item['kind'] == 'cover':
            attributes['properties'] = 'cover-image'
            etree.SubElement(metadata, '{' + namespace + '}meta', name='cover', content=uid)
        etree.SubElement(manifest, '{' + namespace + '}item', **attributes)
        target = posixpath.join(posixpath.dirname(opf_name), name)
        payloads[target] = (zipfile.ZipInfo(target), (path.parent / name).read_bytes())
    payloads[opf_name] = (payloads[opf_name][0], etree.tostring(opf, xml_declaration=True, encoding='UTF-8'))
    fd, temp = tempfile.mkstemp(suffix='.epub', dir=path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp, 'w') as target:
            for info, content in payloads.values():
                target.writestr(info, content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    # Verify all embedded references resolve inside the ZIP, independent of ebooklib.
    import zipfile, posixpath
    from lxml import etree
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        count = 0
        for name in names:
            if not name.endswith('.xhtml'):
                continue
            root = etree.fromstring(z.read(name))
            for node in root.xpath('//*[local-name()="img"]'):
                src = node.get('src', '')
                if posixpath.normpath(posixpath.join(posixpath.dirname(name), src)) not in names:
                    raise ValueError('EPUB 图片未打包：' + src)
                count += 1
    return count
