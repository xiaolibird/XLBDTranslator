# -*- coding: utf-8 -*-
"""给「抓不到全文」的篇目补抓 PDF：把 backfill_deepread 账本里的 failed 逐条过多路通道。

**为什么不是改 fulltext.resolve_oa_pdf 而单独成脚本**：精读主链路的取全文只走两条
（Unpaywall 的 pdf_url、Europe PMC 的 JATS 全文 XML），这两条都断时就判 no_output。
实测 expand 批头 29 篇挂了 24 篇，逐条查下来**大部分并非真的拿不到**，而是主链路没试
这几条路：

- `europepmc.org/articles/<PMCID>?pdf=render`——EPMC 的**渲染版 PDF**，与 `?format=xml`
  的全文接口是两套东西：JATS 全文 404 不代表 PDF 没有（geva2021 就是 XML 404 而
  PDF 200/280KB）。这条也是已知能绕开 medRxiv/PMC 403 的通道。
- Semantic Scholar 的 `openAccessPdf`、OpenAlex 的 `best_oa_location`——两家各自聚合
  了机构库/预印本/作者主页副本，Unpaywall 漏的它们常有。
- arXiv 标题检索——ML 系会议论文（ACM/IEEE 版被 Cloudflare 挡死）几乎都有 arXiv 副本。

**刻意不做的事**：不碰任何绕过出版商反爬的手段。Cloudflare 403（MDPI/OUP/ACM/
science.org 实测）一律判为「拿不到」写进清单交给人，脚本只走公开 API 与开放副本。

**为什么要校验而不是拿到 200 就存**：反爬站点回的 403 页面也是 200 字节的 HTML，
存成 .pdf 会在下游 `_pdf_text_with_stats` 处炸成空文本，然后被当成「精读质量差」
误诊。这里按 `%PDF` magic + 体积 + 可解析页数三道闸卡死，宁可判失败也不留坏文件。

用法：
    PYTHONPATH=. python scripts/fetch_missing_pdfs.py --ledger expand
    PYTHONPATH=. python scripts/fetch_missing_pdfs.py --ledger expand --limit 10
    # 补完接回精读：
    PYTHONPATH=. python scripts/backfill_deepread.py run --expand --decision INCLUDE \
        --tier high --only-failed --pdf-dir <out> --apply

产出：`--out` 目录下的 `<citekey>.pdf`，外加同目录 `_fetch_report.json`（每篇走通了哪条
路 / 为什么没走通），后者就是交给人去手动下的清单来源。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.settings import load_scholar_settings          # noqa: E402
from src.scholar.embed_store import INDEX_NAME                   # noqa: E402
from src.scholar.notes_index import keepers_by_citekey           # noqa: E402
from src.scholar.fulltext import (                             # noqa: E402
    ipv4_client, arxiv_pdf_url, polite_get, rewrite_pmc_url, validate_pdf_bytes,
    route_arxiv_title, route_epmc_render,
    route_openalex as _route_openalex, route_s2 as _route_s2,
)
from src.scholar.schema import PaperMetadata                    # noqa: E402
from src.scholar.crossref import crossref_lookup                 # noqa: E402
from src.utils.logger import get_logger                          # noqa: E402

logger = get_logger("fetch_missing_pdfs")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# 客户端只带 UA：`Accept: application/pdf` 是**下载**时才该带的，挂在全局会让
# EPMC/OpenAlex 这些 JSON 接口回 502/403（实测两家都因此挂过）。下载时按需覆盖。
HEADERS = {"User-Agent": UA}
PDF_ACCEPT = {"Accept": "application/pdf,*/*;q=0.8", "Referer": "https://www.google.com/"}

OPENALEX_MAILTO = ""     # 在 main() 里按配置填；OpenAlex 的礼貌池要它，否则易被限流

# 体积/页数/magic 三闸的常量与实现都在 src/scholar/fulltext（validate_pdf_bytes）——
# 这里不再自持一份，免得两边漂移（2026-09-04 实测漂过一次：那边放宽到 1KB，这边还卡 20KB）。


# ---------------- 校验 ----------------

def _meta_from_entry(entry: dict) -> PaperMetadata:
    """索引条目 → PaperMetadata，好让 fulltext 的通道函数直接吃（它们的入参是 meta）。"""
    return PaperMetadata(
        paper_id=str(entry.get("citekey") or "x"),
        title=entry.get("title") or "",
        doi=(entry.get("doi") or "") or None,
        arxiv_id=(entry.get("arxiv_id") or "") or None,
        pmid=(entry.get("pmid") or "") or None,
    )


def validate_pdf(blob: bytes) -> Tuple[bool, str]:
    """三道闸——**直接委托** `fulltext.validate_pdf_bytes`。

    2026-09-04 教训：本脚本原来自持一份副本（20KB 体积闸 + `%PDF` 必须在偏移 0），
    fulltext 那份在对抗审计后放宽成「1KB + magic 在前 1024 字节内」，副本没跟改——
    于是同一个合法短 PDF 主链路收、补抓脚本丢。凡两处同义的判据，一律留一份。
    """
    return validate_pdf_bytes(blob)


def try_download(client, url: str) -> Tuple[Optional[bytes], str]:
    try:
        r = polite_get(client, url, follow_redirects=True, timeout=45.0, headers=PDF_ACCEPT)
    except Exception as e:                        # noqa: BLE001
        return None, "请求异常：{}".format(e)
    if r.status_code != 200:
        return None, "HTTP {}".format(r.status_code)
    ok, why = validate_pdf(r.content)
    return (r.content if ok else None), why


# ---------------- 各路通道：全部委托 src/scholar/fulltext ----------------
#
# 这些通道 2026-09-04 已从本脚本下沉进 `fulltext`（主链路 `resolve_oa_pdf(extra_routes=True)` 用同一套），
# 并在对抗审计后修过两处：arXiv 标题检索改**双向**词面重合（单向重合对短标题恒为 1.0，会把
# 《A Survey of …》这类长综述当成本篇拉回来）、实词 <4 不检索。本脚本只保留「已有 arxiv_id 就直连」
# 这一条独有分支，其余一律调过去——别再复制一份。

def route_arxiv(entry: dict, client) -> List[Tuple[str, str]]:
    ax = (entry.get("arxiv_id") or "").strip()
    if ax:
        return [("arxiv-id", arxiv_pdf_url(ax))]
    return route_arxiv_title(_meta_from_entry(entry), client)


def route_epmc(entry: dict, client) -> List[Tuple[str, str]]:
    return route_epmc_render(_meta_from_entry(entry), client)


def route_s2(entry: dict, client) -> List[Tuple[str, str]]:
    return _route_s2(_meta_from_entry(entry), client)


def route_openalex(entry: dict, client) -> List[Tuple[str, str]]:
    return _route_openalex(_meta_from_entry(entry), client, email=OPENALEX_MAILTO)


def rewrite_url(url: str) -> str:
    """NCBI PMC → Europe PMC 换宿主，委托 `fulltext.rewrite_pmc_url`（同一份实现）。"""
    return rewrite_pmc_url(url)


def backfill_doi(entry: dict, client) -> Optional[str]:
    """无 DOI 的篇目先按标题问一次 Crossref——三条 DOI 依赖的通道全靠它开门。

    早期 Scholar 快讯条目常常连 DOI 都没有（本批 24 篇失败里占 11 篇），不补的话
    EPMC/S2/OpenAlex 三条路直接无从下手。

    **只在内存里用，绝不写回索引**：DOI 是身份键的一部分，改它必须连带扫一遍派生物
    （citekey、向量库、书目），那是另一件事，不能由一个补抓脚本顺手做掉。
    """
    if entry.get("doi") or entry.get("arxiv_id"):
        return None
    hit = crossref_lookup(entry.get("title") or "", email=OPENALEX_MAILTO, client=client)
    doi = (hit or {}).get("doi") or ""
    if doi:
        print("     Crossref 补到 DOI：{}".format(doi), flush=True)
    return doi or None


ROUTES = (("arXiv", route_arxiv), ("EuropePMC", route_epmc),
          ("SemanticScholar", route_s2), ("OpenAlex", route_openalex))


# ---------------- 主流程 ----------------

def load_keeper_index(notes_dir: Path) -> Dict[str, dict]:
    """读索引并取 **keeper 视图**（citekey → keeper 条目）。

    绝不能写成 `{e["citekey"]: e for e in papers}`：同一 citekey 可以有多条（跨月重复），
    天真字典取到的是**最后一条**、往往是 duplicate，而 duplicate 的 doi/arxiv 可能与 keeper 不同
    ——补抓就会拿错标识符去问 API（生产索引实测 54 个键取到 duplicate，其中 10 个标识符不同）。
    见 docs/bugs/2026-09-04-index-keeper-view-missing.md。
    """
    index = json.loads((Path(notes_dir) / INDEX_NAME).read_text(encoding="utf-8"))
    return keepers_by_citekey(index)


def load_failed(notes_dir: Path, ledger: str) -> List[str]:
    """账本里**真的缺 PDF**的篇目。

    `reason` 以 `llm_unavailable` 开头的一律剔除：那批是「全文/摘要**已经到手**、只是模型侧挂了
    （额度耗尽 / 429 限流 / 鉴权）」，给它们补抓 PDF 是白跑一趟公开 API，还会把它们混进
    `_fetch_report.json` 的人工下载清单里（2026-09-04 第 4 轮审计 CONFIRMED；那天有 35 篇
    正是这样进的清单——见 docs/bugs/2026-09-04-quota-failure-looks-like-no-fulltext.md）。
    额度恢复后它们该走的是 `backfill_deepread run --only-failed`，不是本脚本。
    """
    name = ("backfill_expand_progress.json" if ledger == "expand"
            else "backfill_deepread_progress.json")
    p = notes_dir / name
    if not p.exists():
        raise SystemExit("账本不存在：{}".format(p))
    failed = json.loads(p.read_text(encoding="utf-8")).get("failed") or {}
    out, skipped = [], 0
    for ck, v in failed.items():
        if str((v or {}).get("reason") or "").startswith("llm_unavailable"):
            skipped += 1
            continue
        out.append(ck)
    if skipped:
        print("跳过 {} 篇「模型侧挂了、全文其实已到手」的条目（额度恢复后跑 "
              "backfill_deepread run --only-failed，不是本脚本）".format(skipped), flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="给抓不到全文的篇目补抓 PDF（只走公开通道）")
    ap.add_argument("--ledger", choices=["expand", "deepread"], default="expand")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--config", default="")
    args = ap.parse_args()

    settings = (load_scholar_settings(args.config, patch_gemini=False)
                if args.config else load_scholar_settings(patch_gemini=False))
    notes_dir = Path(settings.processing.notes_dir)
    global OPENALEX_MAILTO
    OPENALEX_MAILTO = (settings.processing.external_email
                       or settings.processing.zotero_email or "")
    by_ck = load_keeper_index(notes_dir)

    from datetime import date as _date
    out_dir = Path(args.out).expanduser() if args.out else (
        Path.home() / "Desktop/Lab/Reading/{}-补抓全文".format(_date.today().isoformat()))
    out_dir.mkdir(parents=True, exist_ok=True)

    cks = load_failed(notes_dir, args.ledger)
    if args.limit:
        cks = cks[: args.limit]
    print("待补抓 {} 篇 → {}".format(len(cks), out_dir), flush=True)

    report: Dict[str, dict] = {}
    rp = out_dir / "_fetch_report.json"
    if rp.exists():
        try:
            report = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            report = {}

    ok = 0
    with ipv4_client(timeout=30.0, headers=HEADERS) as client:
        for i, ck in enumerate(cks, start=1):
            dst = out_dir / "{}.pdf".format(ck)
            if dst.exists():
                print("[{}/{}] {} … 已有文件，跳过".format(i, len(cks), ck), flush=True)
                ok += 1
                continue
            entry = dict(by_ck.get(ck) or {})
            print("[{}/{}] {} … {}".format(i, len(cks), ck, (entry.get("title") or "")[:48]),
                  flush=True)
            found_doi = backfill_doi(entry, client)
            if found_doi:
                entry["doi"] = found_doi
            tried: List[dict] = []
            got = None
            for label, fn in ROUTES:
                for tag, url in ((t, rewrite_url(u)) for t, u in fn(entry, client)):
                    blob, why = try_download(client, url)
                    tried.append({"route": label, "tag": tag, "url": url, "why": why})
                    print("     {:<16} {}".format(label, why), flush=True)
                    if blob:
                        dst.write_bytes(blob)
                        got = {"route": label, "url": url, "detail": why}
                        break
                    time.sleep(0.5)               # 别把公开 API 打崩
                if got:
                    break
            report[ck] = {"title": entry.get("title"), "doi": entry.get("doi"),
                          "doi_from_crossref": found_doi,
                          "url": entry.get("url"), "journal": entry.get("journal"),
                          "got": got, "tried": tried}
            rp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(1.5)      # 篇间节流：限流是按窗口计的，单篇内退避挡不住整批的量
            if got:
                ok += 1
                print("   ✅ {} ← {}".format(got["detail"], got["route"]), flush=True)
            else:
                print("   ❌ 四条路都没走通，进人工清单", flush=True)

    print("\n补抓完成：{} / {} 篇拿到 PDF。报告：{}".format(ok, len(cks), rp))
    print("接回精读：backfill_deepread.py run --expand --decision INCLUDE --tier high "
          "--only-failed --pdf-dir {} --apply".format(out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
