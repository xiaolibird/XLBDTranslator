# -*- coding: utf-8 -*-
"""札记库向量库同步 CLI（核心逻辑在 src/scholar/embed_store.py + embeddings.py）。

  python scripts/notes_embed.py                # 增量同步（默认）
  python scripts/notes_embed.py --dry-run       # 只打印待嵌/待删/元数据刷新计划，不连 Ollama
  python scripts/notes_embed.py --full          # 全量重建（模型/维度变了用这个）
  python scripts/notes_embed.py --stats         # 只读库报告 model/built_at/新鲜度，不连 Ollama

首建（--full）耗时估算 2-6 分钟（batch 64，bge-m3 短文本约 30-100 条/s）。
退出码：0 成功 / 2 literature_index.json 缺失或损坏 / 3 Ollama 不可用。
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                   # noqa: E402
from src.scholar.schema import ScholarSettings                            # noqa: E402
from src.scholar.embeddings import (                                      # noqa: E402
    EmbeddingClient, EmbeddingError, resolve_embedding_base_url,
)
from src.scholar.embed_store import sync_store, read_store_meta, SCHEMA_VERSION, VectorStoreError  # noqa: E402
from src.utils.logger import get_logger                                   # noqa: E402

logger = get_logger("notes_embed")

DB_NAME = "embeddings.sqlite3"
INDEX_NAME = "literature_index.json"


def _load_index(index_path: Path):
    if not index_path.exists():
        print("找不到索引：{}\n先跑：PYTHONPATH=. python scripts/notes_index.py".format(index_path),
              file=sys.stderr)
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print("索引解析失败（{}）：{}".format(type(exc).__name__, index_path), file=sys.stderr)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("papers"), list):
        print("索引结构异常（缺 papers 数组）：{}".format(index_path), file=sys.stderr)
        return None
    return data


def _print_stats(db_path: Path, index_path: Path):
    meta = read_store_meta(db_path)
    if not meta:
        print("向量库尚不存在或为空：{}\n先跑：PYTHONPATH=. python scripts/notes_embed.py --full".format(
            db_path))
        return 2
    index = _load_index(index_path)
    index_generated_at = index.get("generated_at") if index else None

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        n_paper = conn.execute("SELECT COUNT(*) FROM chunks WHERE level='paper'").fetchone()[0]
        n_hl = conn.execute("SELECT COUNT(*) FROM chunks WHERE level='highlight'").fetchone()[0]
    finally:
        conn.close()

    print("向量库：{}".format(db_path))
    print("  schema_version = {}（代码期望 {}）".format(meta.get("schema_version"), SCHEMA_VERSION))
    print("  model = {}  dim = {}  normalized = {}".format(
        meta.get("model"), meta.get("dim"), meta.get("normalized")))
    print("  built_at = {}".format(meta.get("built_at")))
    print("  paper chunks = {}  highlight chunks = {}  合计 = {}".format(n_paper, n_hl, n_paper + n_hl))
    src = meta.get("source_generated_at") or ""
    print("  source_generated_at = {}".format(src or "（无）"))
    if index_generated_at:
        if src and src < index_generated_at:
            print("  ⚠️ 落后于当前索引（索引 generated_at = {}），建议跑增量同步".format(
                index_generated_at))
        else:
            print("  ✅ 与当前索引新鲜度一致（索引 generated_at = {}）".format(index_generated_at))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="同步札记库语义向量（literature_index.json -> SQLite）")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--full", action="store_true", help="全量重建（忽略库内已有内容）")
    ap.add_argument("--dry-run", action="store_true", help="只打印同步计划，不连 Ollama、不动库")
    ap.add_argument("--stats", action="store_true", help="只读库报告统计，不连 Ollama")
    args = ap.parse_args()

    cfg = Path(args.config)
    if cfg.exists():
        settings = ScholarSettings.from_env_file(cfg)
    else:
        settings = ScholarSettings()  # 无配置文件时吃默认值/环境变量，别硬失败
    notes_dir = repo_path(settings.processing.notes_dir)
    index_path = notes_dir / INDEX_NAME
    db_path = notes_dir / DB_NAME

    if args.stats:
        return _print_stats(db_path, index_path)

    index = _load_index(index_path)
    if index is None:
        return 2

    client = EmbeddingClient(
        base_url=resolve_embedding_base_url(settings.llm),
        model=settings.llm.embedding_model,
    )

    if args.dry_run:
        try:
            stats = sync_store(db_path, index, client, dry_run=True, full=args.full)
        except VectorStoreError as e:
            print("❌ 向量库/索引异常：{}".format(e), file=sys.stderr)
            return 2
        print("同步计划（干跑，未连接 Ollama）：")
        print("  期望 chunk 总数 = {}".format(stats.total))
        print("  待嵌 = {}（paper {} + highlight {}）".format(
            stats.embedded, stats.embedded_paper, stats.embedded_highlight))
        print("  待删 = {}（paper {} + highlight {}）".format(
            stats.deleted, stats.deleted_paper, stats.deleted_highlight))
        print("  仅元数据刷新（不重嵌）= {}".format(stats.meta_refreshed))
        print("  model = {}".format(stats.model))
        return 0

    try:
        info = client.probe()
    except EmbeddingError as e:
        print("❌ Ollama embedding 不可用：{}".format(e), file=sys.stderr)
        return 3
    logger.info("✅ embedding 探活成功：model={} dim={}".format(info["model"], info["dim"]))

    def _progress(done, total):
        print("  嵌入进度：{}/{}".format(done, total))

    try:
        stats = sync_store(db_path, index, client, full=args.full, progress_cb=_progress)
    except VectorStoreError as e:
        print("❌ 向量库/索引异常：{}".format(e), file=sys.stderr)
        return 2
    except EmbeddingError as e:
        print("❌ Ollama embedding 调用失败：{}".format(e), file=sys.stderr)
        return 3
    except sqlite3.OperationalError as e:
        # 并发写者持锁跨 embed 时，本进程默认 5s 超时拿到 database is locked
        print("❌ 向量库被并发写锁定（可能另一进程正在同步）：{}".format(e), file=sys.stderr)
        return 2
    finally:
        client.close()

    print("✅ 同步完成：期望 {} 条 | 新嵌 {}（paper {} + highlight {}） | "
          "删除 {}（paper {} + highlight {}） | 元数据刷新 {} | model={}".format(
              stats.total, stats.embedded, stats.embedded_paper, stats.embedded_highlight,
              stats.deleted, stats.deleted_paper, stats.deleted_highlight,
              stats.meta_refreshed, stats.model))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
