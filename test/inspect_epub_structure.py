import sys
import urllib.parse

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

if len(sys.argv) < 2:
    print("用法: python inspect_epub_structure.py <path/to/book.epub>")
    sys.exit(1)
epub_path = sys.argv[1]

try:
    book = epub.read_epub(epub_path)
except Exception as e:
    print(f"Error reading epub: {e}")
    sys.exit(1)

def get_toc_entries(toc, level=0):
    entries = []
    for item in toc:
        if isinstance(item, tuple):
            entries.append((item[0].title, item[0].href, level))
            if len(item) > 1 and isinstance(item[1], list):
                entries.extend(get_toc_entries(item[1], level + 1))
        elif isinstance(item, epub.Link):
            entries.append((item.title, item.href, level))
    return entries

toc = get_toc_entries(book.toc)
print('EPUB TOC Structure:')
for title, href, level in toc:
    print(f"{'  ' * level}- {title} ({href})")

print('\nEPUB Spine (Order of documents):')
for item in book.spine:
    # item is (idref, linear)
    item_ref = item[0]
    doc = book.get_item_with_id(item_ref)
    if doc:
        print(f'- {doc.file_name} (ID: {doc.id})')
