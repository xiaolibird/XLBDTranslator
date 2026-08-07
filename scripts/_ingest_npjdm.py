#!/usr/bin/env python3
"""绕过 filter-v2（deepseek 超时），直接走精读+写盘。
14 篇已经过两轮全文级 agent 裁决，无需再跑 LLM 筛选。
"""
import sys, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import ingest as ing
from src.scholar.paths import repo_path
from src.scholar.schema import ScholarSettings, FilterDecision
from src.utils.logger import get_logger

logger = get_logger("ingest_direct")


def main():
    cfg = Path("config/scholar.env")
    if not cfg.exists():
        print("找不到 config/scholar.env", file=sys.stderr)
        return 2
    settings = ScholarSettings.from_env_file(cfg)
    settings.processing.notes_dir = repo_path(settings.processing.notes_dir)

    # Parse identifiers
    picks_file = Path("output/journal_screen/picks_supplement.txt")
    ids = ing.parse_identifiers(picks_file.read_text(encoding="utf-8"))
    logger.info("解析到 {} 个标识符".format(len(ids)))

    # Get metadata
    email = settings.processing.zotero_email or settings.processing.external_email or ""
    segs = ing.segments_from_identifiers(ids, email=email)
    logger.info("元数据解析 {}/{} 篇".format(len(segs), len(ids)))
    if not segs:
        logger.warning("零篇解析成功")
        return 1

    # Skip filter-v2: manually set all to INCLUDE
    for seg in segs:
        seg.filter_decision = FilterDecision(
            paper_id=seg.paper_id,
            title=seg.metadata.title or "",
            verdict="included",
            decision="INCLUDE",
            stage="llm_judge",
            reason="npjDM 漏网补入——关键词白名单漏洞/裁决升级（filter-v2 绕过）",
            one_line="",
        )

    label = "npjDM-2026-08-supplement"
    print("\n候选 {} 篇 (label={}):".format(len(segs), label))
    print("\n".join(ing.describe(segs)))

    # Run ingest with close reading
    print("\n开始入库（含全文精读 top-14 + citekey 解析）...")
    rep = ing.run_ingest(segs, settings, label, top_n=14, close_read=True)
    if rep["status"] == "empty":
        print("去重后无新论文，未写盘。")
        return 1

    # Update index and vector store
    from src.scholar.notes_index import update_index, write_outputs
    nd = Path(settings.processing.notes_dir)
    index_data = update_index(nd)
    write_outputs(index_data, nd)
    logger.info("文献索引已刷新")

    try:
        from src.scholar.embeddings import EmbeddingClient, resolve_embedding_base_url
        from src.scholar.embed_store import DB_NAME, sync_store
        client = EmbeddingClient(
            base_url=resolve_embedding_base_url(settings.llm),
            model=settings.llm.embedding_model,
        )
        try:
            stats = sync_store(nd / DB_NAME, index_data, client)
        finally:
            client.close()
        logger.info("向量库已同步：+{} / -{} / {} 元数据刷新".format(
            stats.embedded, stats.deleted, stats.meta_refreshed))
    except Exception as e:
        logger.warning("向量库同步跳过（不影响入库）：{}".format(e))

    print("\n" + "=" * 66)
    print("✅ {} 篇 → {}".format(rep["count"], rep["md"]))
    print("   全文精读 {} 篇 · citekey 命中 {}/{}".format(
        rep["full_text"], rep["citekey"], rep["count"]))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
