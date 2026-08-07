#!/usr/bin/env python3
"""单独重试论文 14 TTE framework 的全文精读"""
import sys, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import ingest as ing
from src.scholar.paths import repo_path
from src.scholar.schema import ScholarSettings, FilterDecision
from src.utils.logger import get_logger

logger = get_logger("retry_paper14")

def main():
    cfg = Path("config/scholar.env")
    settings = ScholarSettings.from_env_file(cfg)
    settings.processing.notes_dir = repo_path(settings.processing.notes_dir)

    # Just one paper
    doi = "10.1038/s41746-026-02563-z"
    ids = ing.parse_identifiers(doi)
    logger.info("解析到 {} 个标识符".format(len(ids)))

    email = settings.processing.zotero_email or settings.processing.external_email or ""
    segs = ing.segments_from_identifiers(ids, email=email)
    logger.info("元数据解析 {}/{} 篇".format(len(segs), len(ids)))
    if not segs:
        logger.warning("零篇解析成功")
        return 1

    for seg in segs:
        seg.filter_decision = FilterDecision(
            paper_id=seg.paper_id,
            title=seg.metadata.title or "",
            verdict="included",
            decision="INCLUDE",
            stage="llm_judge",
            reason="npjDM 论文14 TTE framework 重试——上次SSL失败降级为摘要",
            one_line="TTE操作化框架：EHR数据生成机制对因果可识别性的约束与边界",
        )

    label = "npjDM-2026-08-supplement-retry14"
    print("\n单篇重试 (label={}):".format(label))
    print("\n".join(ing.describe(segs)))

    rep = ing.run_ingest(segs, settings, label, top_n=1, close_read=True)
    if rep["status"] == "empty":
        print("去重后无新论文，未写盘。")
        return 1

    print("\n" + "=" * 66)
    print("✅ {} 篇 → {}".format(rep["count"], rep["md"]))
    print("   全文精读 {} 篇".format(rep["full_text"]))
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
