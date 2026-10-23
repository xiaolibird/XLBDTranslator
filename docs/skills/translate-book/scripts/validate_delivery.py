#!/usr/bin/env python3
"""Validate final PDF/EPUB against the curated reading view; no network calls."""
import json,re,zipfile,posixpath,hashlib,sys
from pathlib import Path
from urllib.parse import urlsplit,unquote
from lxml import etree
import fitz
if len(sys.argv) != 2:
 raise SystemExit('Usage: validate_delivery.py JOB_DIRECTORY')
j=Path(sys.argv[1]).expanduser().resolve();meta=json.loads((j/'book.json').read_text());view=json.loads((j/'reviewed/render_segments.json').read_text());pdfs=list((j/'reviewed').glob('*.pdf'));epubs=list((j/'reviewed').glob('*.epub'));
if len(pdfs)!=1 or len(epubs)!=1:raise SystemExit('Expected exactly one final PDF and EPUB in reviewed/')
pdf=pdfs[0];epub=epubs[0];errors=[]
han=lambda t:''.join(re.findall('[\u3400-\u9fff]',t));alltext=[];roots={};images=[];owners={}
with zipfile.ZipFile(epub) as z:
 names=set(z.namelist());bad=z.testzip();
 if bad:errors.append('ZIP corruption '+bad)
 for n in names:
  if n.endswith('.xhtml'):
   r=etree.fromstring(z.read(n));roots[n]=r;alltext.extend(r.xpath('//*[local-name()="body"]//text()'))
   for tag in r.xpath('//*[@id]'):
    k=tag.get('id');owners.setdefault(k,[]).append(n)
 for n,r in roots.items():
  for a in r.xpath('//*[@href or @src]'):
   href=a.get('src') or a.get('href');u=urlsplit(href)
   if u.scheme or u.netloc:continue
   target=posixpath.normpath(posixpath.join(posixpath.dirname(n),unquote(u.path))) if u.path else n
   if target not in names:errors.append('Missing local target '+n+':'+href)
   if u.fragment:
    ids=set(roots[target].xpath('//@id | //@name | //@xml:id',namespaces={'xml':'http://www.w3.org/XML/1998/namespace'})) if target in roots else set()
    if unquote(u.fragment) not in ids:errors.append('Broken anchor '+n+':'+href)
   if a.get('src') and a.tag.endswith('img'):images.append(target)
 et='\n'.join(alltext);eh=han(et)
 for s in view:
  if 'seg'+str(s['segment_id']) not in owners:errors.append('Missing reading anchor '+str(s['segment_id']))
 with fitz.open(pdf) as d:
  pt='\n'.join(p.get_text() for p in d)
  # Running heads interrupt sparse Chinese bibliography text across pages.
  # This renderer reserves the outer 50 pt for running heads and folios.
  bodytext='\n'.join(p.get_text(clip=fitz.Rect(0,50,p.rect.width,p.rect.height-50)) for p in d)
  ph=han(bodytext);out=[]
  for i,p in enumerate(d):
   rect=p.rect
   if any(w[0]<-1 or w[1]<-1 or w[2]>rect.width+1 or w[3]>rect.height+1 for w in p.get_text('words')):out.append(i+1)
  pdfpages=len(d);bookmarks=len(d.get_toc())
 checks=[]
 for s in view:
  if s.get('index_entries'):continue
  t=han(s['translated_text'])
  if len(t)<30:continue
  wins=[t[int((len(t)-25)*k/5):int((len(t)-25)*k/5)+25] for k in range(6)];e=sum(w in eh for w in wins);p=sum(w in ph for w in wins);checks.append({'segment_id':s['segment_id'],'epub_windows':e,'pdf_windows':p})
  if e<6:errors.append('EPUB text window loss candidate '+str(s['segment_id'])+' '+str(e)+'/6')
  if p<3:errors.append('PDF text window loss candidate '+str(s['segment_id'])+' '+str(p)+'/6')
 if '\ufffd' in et+pt:errors.append('Replacement character')
 if re.search(r'\[Failed|Translation Failed|翻译被截断',et+pt,re.I):errors.append('Failure marker')
 if out:errors.append('Outside-page text '+str(out))
 edition_path=j/'reviews/abridgement.json'
 if edition_path.exists():
  edition=json.loads(edition_path.read_text());by_id={sid:s for s in view for sid in s.get('omitted_source_segment_ids',[s['segment_id']])}
  for omitted in edition['omissions']:
   s=by_id.get(omitted['segment_id'],{})
   expected=next((x['notice'] for x in edition['omissions'] if x['segment_id']==omitted.get('group_start_segment_id',omitted['segment_id'])),None)
   if not s.get('editorial_omission') or s.get('original_text') or s.get('translated_text')!=expected:errors.append('Unsafe or absent omission placeholder '+str(omitted['segment_id']))
   notice=han(expected or '')
   if notice not in eh or notice not in ph:errors.append('Omission notice missing from reading file '+str(omitted['segment_id']))
  if han(edition['reader_notice']) not in eh or han(edition['reader_notice']) not in ph:errors.append('Abridged edition reader notice missing')
  for s in view:
   if s.get('visual_omission_notice'):
    notice=han(s['visual_omission_notice'])
    if notice not in eh or notice not in ph:errors.append('Visual omission notice missing '+str(s['segment_id']))
 assets=json.loads((j/'reviewed/quality_report.json').read_text()).get('visual_assets',[])
 for a in assets:
  name=Path(a['render_path']).name
  matches=[n for n in names if Path(n).name==name]
  if len(matches)!=1 or hashlib.sha256(z.read(matches[0])).hexdigest()!=a['sha256']:errors.append('EPUB asset mismatch '+name)
 if edition_path.exists():
  excluded={a['sha256'] for a in edition.get('excluded_assets',[])}
  if any(a['sha256'] in excluded for a in assets):errors.append('Excluded visual asset retained')
  if any(hashlib.sha256(z.read(n)).hexdigest() in excluded for n in set(images)):errors.append('Excluded visual asset embedded')
 result={'title':meta['title'],'pdf_pages':pdfpages,'pdf_bookmarks':bookmarks,'epub_xhtml':len(roots),'epub_image_references':len(images),'reading_segments':len(view),'text_window_checks':checks,'outside_page_text':out,'errors':errors,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'epub_sha256':hashlib.sha256(epub.read_bytes()).hexdigest(),'scope':'正文六个分布窗口及全部内链/图片哈希检查；视觉检查另行记录，非全文逐句校对。'}
 (j/'reviewed/delivery_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(j.name,'pages',pdfpages,'errors',errors[:20],'errorcount',len(errors))

if errors:
 raise SystemExit(1)
