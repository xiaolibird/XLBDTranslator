import zipfile
from pathlib import Path

epub_path = Path('test.epub')

# 创建最小的 EPUB 结构
with zipfile.ZipFile(epub_path, 'w') as epub:
    # mimetype 文件（必须未压缩）
    epub.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    
    # META-INF/container.xml
    container_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''
    epub.writestr('META-INF/container.xml', container_xml)
    
    # OEBPS/content.opf
    content_opf = '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Document</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="uid">test123</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter1"/>
  </spine>
</package>'''
    epub.writestr('OEBPS/content.opf', content_opf)
    
    # OEBPS/chapter1.xhtml
    chapter1 = '''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
<h1>Test Chapter</h1>
<p>The Unified Memory Architecture (UMA) in M2 Pro chips allows high-bandwidth, low-latency access to data.</p>
<p>Generative AI models require significant computational resources for inference, especially during long-context processing.</p>
</body>
</html>'''
    epub.writestr('OEBPS/chapter1.xhtml', chapter1)

print(f'Created {epub_path}')
