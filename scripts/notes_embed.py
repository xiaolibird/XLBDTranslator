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
from src.scholar.embed_store import (                                     # noqa: E402
    DB_NAME, INDEX_NAME, ABSTRACTS_NAME, sync_store, read_store_meta, SCHEMA_VERSION,
    VectorStoreError, load_abstracts,
)
from src.scholar.notes_index import load_index_file                       # noqa: E402
from src.utils.logger import get_logger                                   # noqa: E402

logger = get_logger("notes_embed")

def _load_index(index_path: Path):
    """读索引（单点实现在 notes_index.load_index_file，别再各写各的）。"""
    data, err = load_index_file(index_path)
    if err:
        print(err, file=sys.stderr)
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
        n_ab = conn.execute("SELECT COUNT(*) FROM chunks WHERE level='abstract'").fetchone()[0]
        n_hl = conn.execute("SELECT COUNT(*) FROM chunks WHERE level='highlight'").fetchone()[0]
    finally:
        conn.close()

    print("向量库：{}".format(db_path))
    print("  schema_version = {}（代码期望 {}）".format(meta.get("schema_version"), SCHEMA_VERSION))
    print("  model = {}  dim = {}  normalized = {}".format(
        meta.get("model"), meta.get("dim"), meta.get("normalized")))
    print("  built_at = {}".format(meta.get("built_at")))
    print("  paper chunks = {}  abstract chunks = {}  highlight chunks = {}  合计 = {}".format(
        n_paper, n_ab, n_hl, n_paper + n_ab + n_hl))
    # 喂厚生效的直接观测：sidecar 条数应≈abstract chunks（无摘要条目不产 ab:）。
    # sidecar 损坏时 stats 仍是只读报告，只提示不改退出码（sync 才会硬拒）。
    if not (db_path.parent / ABSTRACTS_NAME).exists():
        print("  {} 不存在（瘦库模式，未回填摘要）".format(ABSTRACTS_NAME))
    else:
        try:
            abstracts = load_abstracts(db_path.parent)
        except VectorStoreError as e:
            print("  ⚠️ {} 存在但读不出，下次同步会被拒绝：{}".format(ABSTRACTS_NAME, e))
        else:
            print("  {} = {} 条摘要".format(ABSTRACTS_NAME, len(abstracts)))
    src = meta.get("source_generated_at") or ""
    print("  source_generated_at = {}".format(src or "（无）"))
    if index_generated_at:
        if src and src < index_generated_at:
            print("  ⚠️ 落后于当前索引（索引 generated_at = {}），建议跑增量同步".format(
                index_generated_at))
        else:
            print("  ✅ 与当前索引新鲜度一致（索引 generated_at = {}）".format(index_generated_at))
    return 0


def _sqlite_error_exit(e) -> int:
    """把 sqlite 异常翻成可操作的诊断并返回退出码 2（正式路径与 --dry-run 共用）。

    OperationalError 不只有"被锁"：磁盘写满、卷只读、加列后未迁移 schema
    （CREATE TABLE IF NOT EXISTS 不会 ALTER 已有表，SELECT 新列抛 no such column）
    全是它。一律说成"被锁"会让唯一的读者（cron_embed.err.log）照着"等一会儿再重试"白等。
    DatabaseError（库损坏/根本不是 SQLite 文件）此前完全不接，会裸抛 traceback 并以
    退出码 1 收场，与本脚本 docstring 承诺的 0/2/3 契约不符（1 在别处是"无命中"语义）。
    """
    msg = str(e)
    if isinstance(e, sqlite3.OperationalError):
        if "locked" in msg or "busy" in msg:
            print("❌ 向量库被并发写锁定（可能另一进程正在同步）：{}".format(e), file=sys.stderr)
        else:
            print("❌ 向量库 SQLite 操作失败（**不是**并发锁；常见成因：磁盘写满、卷只读、"
                  "schema 与代码不符需 --full 重建）：{}".format(e), file=sys.stderr)
    else:
        print("❌ 向量库文件损坏或不是 SQLite 库：{}\n"
              "请删除后重建：PYTHONPATH=. python scripts/notes_embed.py --full".format(e),
              file=sys.stderr)
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="同步札记库语义向量（literature_index.json -> SQLite）")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--full", action="store_true", help="全量重建（忽略库内已有内容）")
    ap.add_argument("--dry-run", action="store_true", help="只打印同步计划，不连 Ollama、不动库")
    ap.add_argument("--stats", action="store_true", help="只读库报告统计，不连 Ollama")
    args = ap.parse_args()

    cfg = repo_path(args.config)
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
            # 并发避让不是「库坏了」，措辞要分开——两者都走 VectorStoreError/退出码 2，
            # 但一个是保护生效（库完好），一个是真损坏，统一叫「异常」会让人以为库废了。
            print("{}：{}".format("⚠️ 同步避让" if "并发写入避让" in str(e)
                                  else "❌ 向量库/索引异常", e), file=sys.stderr)
            return 2
        except sqlite3.DatabaseError as e:
            # 与正式路径同款：dry-run 同样要开 sqlite 连接做 diff，坏库/坏 schema
            # 在这里裸抛 traceback 会以退出码 1 收场，与 docstring 的 0/2/3 契约不符。
            return _sqlite_error_exit(e)
        print("同步计划（干跑，未连接 Ollama）：")
        print("  期望 chunk 总数 = {}".format(stats.total))
        print("  待嵌 = {}（paper {} + abstract {} + highlight {}）".format(
            stats.embedded, stats.embedded_paper, stats.embedded_abstract, stats.embedded_highlight))
        print("  待删 = {}（paper {} + abstract {} + highlight {}）".format(
            stats.deleted, stats.deleted_paper, stats.deleted_abstract, stats.deleted_highlight))
        print("  hash 未变（不重嵌）= {}，其中元数据有变将 UPDATE = {}".format(
            stats.meta_refreshed, stats.meta_updated))
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
        print("{}：{}".format("⚠️ 同步避让" if "并发写入避让" in str(e)
                              else "❌ 向量库/索引异常", e), file=sys.stderr)
        return 2
    except EmbeddingError as e:
        print("❌ Ollama embedding 调用失败：{}".format(e), file=sys.stderr)
        return 3
    except sqlite3.DatabaseError as e:
        # OperationalError 是它的子类，一并接住（分流在 _sqlite_error_exit 里）
        return _sqlite_error_exit(e)
    finally:
        client.close()

    print("✅ 同步完成：期望 {} 条 | 新嵌 {}（paper {} + abstract {} + highlight {}） | "
          "删除 {}（paper {} + abstract {} + highlight {}） | hash 未变 {}（元数据 UPDATE {}） | model={}".format(
              stats.total, stats.embedded, stats.embedded_paper, stats.embedded_abstract,
              stats.embedded_highlight,
              stats.deleted, stats.deleted_paper, stats.deleted_abstract, stats.deleted_highlight,
              stats.meta_refreshed, stats.meta_updated, stats.model))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
