# -*- coding: utf-8 -*-
"""札记库向量库：literature_index.json → chunk 抽取 → SQLite 落盘 → 内存检索。

分三层：
  chunks_from_index()  纯函数，索引 dict -> chunk 列表，不碰网络/磁盘
  sync_store()         chunk 列表 vs 库内内容哈希 diff -> 增量 embed -> 事务写库
  VectorStore           把整张表读进内存做暴力余弦检索（万条量级，matmul 比建索引划算）

向量库是索引的纯派生物：随时可 --full 全量重建，损坏/删除的最坏后果只是语义
检索暂时不可用，不影响 notes_index.py / notes_query.py 等既有链路。
"""
import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

SCHEMA_VERSION = 2  # v2: highlight chunk id 掺入 role+序号，修复同文本覆盖丢数据（老库需 --full 重建）

_CREATE_META_SQL = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
_CREATE_CHUNKS_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, level TEXT NOT NULL, citekey TEXT NOT NULL,
  role TEXT, tag TEXT, section TEXT, month TEXT, series TEXT, tier TEXT,
  bucket TEXT, year INTEGER, has_full_text INTEGER NOT NULL DEFAULT 0,
  note_file TEXT, note_line INTEGER, text TEXT NOT NULL,
  text_hash TEXT NOT NULL, vec BLOB NOT NULL
)
"""
_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_chunks_citekey ON chunks(citekey)"

_CHUNK_COLUMNS = (
    "id", "level", "citekey", "role", "tag", "section", "month", "series",
    "tier", "bucket", "year", "has_full_text", "note_file", "note_line",
    "text", "text_hash", "vec",
)


class VectorStoreError(RuntimeError):
    """库文件缺失 / schema 版本不兼容 / 结构异常。调用方（notes_search.py 等）
    据此退出码 2 并给可操作提示，不把 traceback 抛给用户。"""


# ---------------------------------------------------------------------------
# Chunk 抽取（纯函数）
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    id: str
    level: str          # "paper" | "highlight"
    citekey: str
    text: str
    role: Optional[str]
    tag: Optional[str]
    section: Optional[str]
    month: Optional[str]
    series: Optional[str]
    tier: Optional[str]
    bucket: str          # 逗号 join（源数据是 list）
    year: Optional[int]
    has_full_text: int   # 0/1
    note_file: Optional[str]
    note_line: Optional[int]


def chunks_from_index(index: dict) -> List[Chunk]:
    """literature_index.json -> 两级 chunk 列表。

    只取 duplicate_of 为空且有 citekey 的 keeper（与 notes_query.py 的入选口径一致）。
    paper 级：id=`p:<citekey>`，文本=title+"\\n"+one_line（one_line 空则仅 title）。
    highlight 级：id=`h:<citekey>:<role>:<sha1(text)[:12]>:<seq>`——内容寻址（句子编辑/citekey
    改名会自然让旧 id 消失、新 id 出现，sync_store 的 diff 据此"删旧嵌新"）；掺入 role+序号避免
    同篇同文本不同 role（如一句同时标 citable/refutable）碰撞覆盖丢数据。
    """
    out: List[Chunk] = []
    for e in index.get("papers") or []:
        if not isinstance(e, dict):
            continue
        citekey = e.get("citekey")
        if e.get("duplicate_of") or not citekey:
            continue
        if citekey.startswith("MISSING-KEY-") or e.get("citekey_source") == "missing":
            # MISSING-KEY 占位键不对应真实文献（notes_index.is_missing_citekey 契约），
            # 嵌进向量库会让 notes_search --cite 输出死引用，与 all_references 剔除口径一致。
            continue

        bucket_raw = e.get("bucket") or []
        bucket = ",".join(str(b) for b in bucket_raw) if isinstance(bucket_raw, list) else str(bucket_raw)
        has_full_text = 1 if e.get("has_full_text_reading") else 0
        tier = e.get("priority_tier")
        month = e.get("month")
        series = e.get("series")
        year = e.get("year")
        note_file = e.get("note_file")
        note_line = e.get("note_line")

        title = (e.get("title") or "").strip()
        one_line = (e.get("one_line") or "").strip()
        paper_text = "{}\n{}".format(title, one_line) if one_line else title
        out.append(Chunk(
            id="p:{}".format(citekey), level="paper", citekey=citekey, text=paper_text,
            role=None, tag=None, section=None, month=month, series=series, tier=tier,
            bucket=bucket, year=year, has_full_text=has_full_text,
            note_file=note_file, note_line=note_line,
        ))

        seen_hl: Dict[str, int] = {}
        for h in (e.get("highlights") or []):
            if not isinstance(h, dict):
                continue
            text = (h.get("text") or "").strip()
            if not text:
                continue
            # 内容寻址 id 掺入 role + 出现序号：同篇同文本被标不同 role（citable/refutable）
            # 或同 role 重复出现时，各自独立成 chunk，避免静默覆盖丢数据。
            role = h.get("role")
            hash12 = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            base = "h:{}:{}:{}".format(citekey, role or "", hash12)
            seq = seen_hl.get(base, 0)
            seen_hl[base] = seq + 1
            hid = "{}:{}".format(base, seq)
            out.append(Chunk(
                id=hid, level="highlight", citekey=citekey, text=text,
                role=role, tag=h.get("tag"), section=h.get("section"),
                month=month, series=series, tier=tier, bucket=bucket, year=year,
                has_full_text=has_full_text, note_file=note_file, note_line=note_line,
            ))
    return out


def _text_hash(model: str, text: str) -> str:
    """模型名织入哈希：换 embedding 模型 = 全体 chunk 自动判过期，不需要额外的
    "模型是否变了"分支判断——diff 逻辑自然覆盖。"""
    raw = "{}\x00{}".format(model, text).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 落盘同步
# ---------------------------------------------------------------------------

@dataclass
class SyncStats:
    total: int = 0
    embedded: int = 0
    embedded_paper: int = 0
    embedded_highlight: int = 0
    deleted: int = 0
    deleted_paper: int = 0
    deleted_highlight: int = 0
    meta_refreshed: int = 0
    model: str = ""
    dry_run: bool = False
    full: bool = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # WAL：长 sync 写事务 commit 时读方不再被短暂互斥（busy_timeout=0 默认下防 SQLITE_BUSY）
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass  # 某些只读文件系统不支持 WAL，退化到默认 journal 模式
    conn.execute(_CREATE_META_SQL)
    conn.execute(_CREATE_CHUNKS_SQL)
    conn.execute(_CREATE_INDEX_SQL)


def _write_meta(conn: sqlite3.Connection, meta: Dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(meta.items()),
    )


def _read_meta_dict(conn: sqlite3.Connection) -> Dict[str, str]:
    try:
        return {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.DatabaseError:
        return {}


def sync_store(db_path, index: dict, client, *, full: bool = False,
               dry_run: bool = False, batch_size: int = 64,
               progress_cb: Optional[Callable[[int, int], None]] = None) -> SyncStats:
    """index -> chunk 期望集，与库内 (id, text_hash) diff，增量 embed 后单事务落盘。

    - full=True：忽略库内已有内容，全部重嵌；写入临时文件成功后再 rename 替换
      （中途失败旧库原样保留，不会留半成品）。
    - full=False：按 id 找库内旧 text_hash；hash 不同或 id 不存在 -> 待嵌；
      hash 相同但其它元数据变了 -> 只 UPDATE 不重嵌（meta_refreshed 计数）；
      期望集里没有的旧 id -> 待删。text_hash 已经把模型名织入，换模型时旧
      hash 自然全部对不上新 hash，无需单独判断"模型变了"。
    - dry_run=True：只计算计划、不建连接、不调 client.embed/probe，返回统计后即停。
    - progress_cb(done, total)：每嵌完一批（batch_size 条）回调一次，供 CLI 打进度行。
    """
    db_path = Path(db_path)
    chunks = chunks_from_index(index)
    if not chunks:
        # 索引为空（重建中途产物/误配置）时 --full 会把现有库整体替换成 0 行，不可逆丢失。
        # 宁缺毋滥：空期望集一律拒绝落库，提示先核对索引。
        raise VectorStoreError(
            "索引没有产出任何 chunk（papers 为空或全被 duplicate_of 过滤）。"
            "拒绝执行，避免清空现有向量库。")
    model = client.model
    expected: Dict[str, Tuple[Chunk, str]] = {
        c.id: (c, _text_hash(model, c.text)) for c in chunks
    }

    existing_hash: Dict[str, str] = {}
    existing_level: Dict[str, str] = {}
    db_exists = db_path.exists()
    if db_exists and full:
        # --full 前核对索引是否骤缩（配合过期/部分生成的索引会把好库原子替换成小库）
        try:
            conn = sqlite3.connect(str(db_path))
            old_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        except sqlite3.DatabaseError:
            old_count = 0
        finally:
            conn.close()
        if old_count and len(chunks) < old_count * 0.5:
            raise VectorStoreError(
                "索引 chunk 数 {} 不到现有库 {} 的一半，疑似索引过期/部分生成。"
                "拒绝 --full 替换，避免好库被缩水。确认无误请手动处理。".format(
                    len(chunks), old_count))
    if db_exists and not full:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            meta = _read_meta_dict(conn)
            old_model = meta.get("model", "")
            # 只比基础名（split(":")[0]）：`bge-m3:latest` 与 `bge-m3` 是同一模型，
            # 整串比较会让带 tag 的配置每次触发虚假重建
            if old_model and old_model.split(":")[0] != model.split(":")[0]:
                # 换 embedding 模型后跑增量：text_hash 织入模型名 → 全部旧 hash 对不上 →
                # 静默全量重嵌。给明确提示，避免用户在不知情下等几十分钟到几小时。
                raise VectorStoreError(
                    "向量库 embedding 模型是 '{}'，配置改为 '{}'：增量同步会重嵌全部 {} 条。"
                    "建议先 `notes_embed.py --full` 一次性重建（迁移命令）。"
                    .format(old_model, model, len(chunks)))
            for cid, h, lvl in conn.execute("SELECT id, text_hash, level FROM chunks"):
                existing_hash[cid] = h
                existing_level[cid] = lvl
        finally:
            conn.close()

    to_embed_ids: List[str] = []
    meta_refresh_ids: List[str] = []
    for cid, (c, h) in expected.items():
        old = existing_hash.get(cid)
        if old is None or old != h:
            to_embed_ids.append(cid)
        else:
            meta_refresh_ids.append(cid)
    to_delete_ids = [] if full else [cid for cid in existing_hash if cid not in expected]

    stats = SyncStats(
        total=len(expected),
        embedded=len(to_embed_ids),
        embedded_paper=sum(1 for cid in to_embed_ids if expected[cid][0].level == "paper"),
        embedded_highlight=sum(1 for cid in to_embed_ids if expected[cid][0].level == "highlight"),
        deleted=len(to_delete_ids),
        deleted_paper=sum(1 for cid in to_delete_ids if existing_level.get(cid) == "paper"),
        deleted_highlight=sum(1 for cid in to_delete_ids if existing_level.get(cid) == "highlight"),
        meta_refreshed=len(meta_refresh_ids),
        model=model, dry_run=dry_run, full=full,
    )
    if dry_run:
        return stats

    tmp_path: Optional[Path] = None
    write_path = db_path
    if full:
        tmp_path = db_path.with_name(db_path.name + ".tmp-{}".format(os.getpid()))
        # 清掉历史 SIGKILL 残留的同类临时库（旧 pid），避免永久留盘累积
        for stale in db_path.parent.glob(db_path.name + ".tmp-*"):
            if stale != tmp_path:
                try:
                    stale.unlink()
                except OSError:
                    pass
        write_path = tmp_path

    # isolation_level=None：autocommit，显式管理事务（BEGIN IMMEDIATE / COMMIT / ROLLBACK）。
    # 增量写全程持排他写锁（WAL 下读者读旧快照不受阻），消除 diff 读与写之间的 TOCTOU gap：
    # 任何并发 sync_store/--full 要么在我们 BEGIN 前完成（被快照比对抓到）、要么在我们持锁
    # 期间等待——不会出现"基于过期快照的 lost update"。
    conn = sqlite3.connect(str(write_path), isolation_level=None)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()

        if not full:
            cur.execute("BEGIN IMMEDIATE")
            # diff 读（上面独立 conn）与写（本 conn）之间的并发修改，这里全量比对必然抓到。
            current = dict(cur.execute("SELECT id, text_hash FROM chunks"))
            if current != existing_hash:
                diff_ids = [cid for cid in set(current) | set(existing_hash)
                            if current.get(cid) != existing_hash.get(cid)]
                raise VectorStoreError(
                    "向量库在 diff 快照后被并发修改（{} 处）。本次同步已放弃，"
                    "请重试（重跑 notes_embed.py）。".format(len(diff_ids)))

        dim: Optional[int] = None
        if to_embed_ids:
            upsert_sql = (
                "INSERT INTO chunks ({cols}) VALUES ({qs}) "
                "ON CONFLICT(id) DO UPDATE SET {updates}".format(
                    cols=", ".join(_CHUNK_COLUMNS),
                    qs=", ".join("?" for _ in _CHUNK_COLUMNS),
                    updates=", ".join(
                        "{0}=excluded.{0}".format(col) for col in _CHUNK_COLUMNS if col != "id"),
                )
            )
            total = len(to_embed_ids)
            done = 0
            for i in range(0, total, batch_size):
                batch_ids = to_embed_ids[i:i + batch_size]
                texts = [expected[cid][0].text for cid in batch_ids]
                vecs = client.embed(texts, batch_size=batch_size)
                if dim is None and vecs.size:
                    dim = int(vecs.shape[1])
                for cid, vec in zip(batch_ids, vecs):
                    c, h = expected[cid]
                    cur.execute(upsert_sql, (
                        c.id, c.level, c.citekey, c.role, c.tag, c.section, c.month, c.series,
                        c.tier, c.bucket, c.year, c.has_full_text, c.note_file, c.note_line,
                        c.text, h, vec.astype(np.float32).tobytes(),
                    ))
                done += len(batch_ids)
                if progress_cb:
                    progress_cb(done, total)
            if dim is None:
                dim = 0

        if meta_refresh_ids:
            cur.executemany(
                "UPDATE chunks SET level=?, citekey=?, role=?, tag=?, section=?, month=?, "
                "series=?, tier=?, bucket=?, year=?, has_full_text=?, note_file=?, note_line=? "
                "WHERE id=?",
                [
                    (c.level, c.citekey, c.role, c.tag, c.section, c.month, c.series,
                     c.tier, c.bucket, c.year, c.has_full_text, c.note_file, c.note_line, c.id)
                    for c, _ in (expected[cid] for cid in meta_refresh_ids)
                ],
            )

        if to_delete_ids:
            cur.executemany("DELETE FROM chunks WHERE id=?", [(cid,) for cid in to_delete_ids])

        if dim is None:
            row = cur.execute("SELECT LENGTH(vec) FROM chunks LIMIT 1").fetchone()
            dim = (row[0] // 4) if row and row[0] else 0

        _write_meta(conn, {
            "schema_version": str(SCHEMA_VERSION),
            "model": model,
            "dim": str(dim),
            "normalized": "l2",
            "built_at": datetime.now().isoformat(),
            "source_generated_at": index.get("generated_at") or "",
        })
        if not full:
            # --full 走 tmp+os.replace 换 inode；本连接在 COMMIT 前校验 db_path 仍是同一
            # 文件。若并发 --full 已替换，本次增量写到旧 inode 会被静默丢弃——检测到即
            # 放弃，报可操作错误（下轮 diff 自愈，避免"同步完成"假成功）。
            if os.path.exists(str(db_path)) and not os.path.samefile(str(db_path), str(write_path)):
                raise VectorStoreError(
                    "向量库文件被并发 --full 替换（inode 已换）。本次增量已放弃，请重试。")
            conn.execute("COMMIT")
    except BaseException:
        # --full 失败时清理临时库，避免 .tmp-<pid> 残留磁盘
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
        if not full:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()

    if tmp_path is not None:
        os.replace(str(tmp_path), str(db_path))

    return stats


def read_store_meta(db_path) -> Dict[str, str]:
    """只读 meta 表（供 --stats 用，不加载整表向量）。库不存在返回空 dict。"""
    db_path = Path(db_path)
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        return _read_meta_dict(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 内存检索
# ---------------------------------------------------------------------------

class VectorStore:
    """整表读进内存：~1 万条 × 1024 维 float32 ≈ 41MB，暴力余弦（matmul）足够快，
    不值得为这个量级引 faiss/chromadb。"""

    def __init__(self, meta: Dict[str, str], records: List[dict], mat: np.ndarray):
        self.meta = meta
        self.records = records          # 与 mat 行一一对应的 dict 列表
        self.mat = mat                  # (n, dim) float32，已 L2 归一
        self.model = meta.get("model", "")
        self.dim = int(meta.get("dim") or 0)

    @classmethod
    def load(cls, db_path) -> "VectorStore":
        db_path = Path(db_path)
        if not db_path.exists():
            raise VectorStoreError(
                "向量库不存在：{}\n先跑：PYTHONPATH=. python scripts/notes_embed.py".format(db_path))
        conn = sqlite3.connect(str(db_path))
        try:
            # 包进读事务：meta 与 rows 两次读取必须在同一事务里（一致快照），否则并发写者
            # commit 会撕裂（读到旧 meta + 新 rows，spurious dim-mismatch）。WAL 下读事务
            # 不阻塞写者，纯快照。
            conn.execute("BEGIN")
            meta = _read_meta_dict(conn)
            if not meta:
                raise VectorStoreError(
                    "向量库为空/未初始化：{}\n跑：PYTHONPATH=. python scripts/notes_embed.py --full".format(
                        db_path))
            schema_version = int(meta.get("schema_version") or 0)
            if schema_version != SCHEMA_VERSION:
                raise VectorStoreError(
                    "向量库 schema 版本不兼容（库={}，代码期望={}），需要 --full 重建".format(
                        schema_version, SCHEMA_VERSION))
            dim = int(meta.get("dim") or 0)
            cols = ("id", "level", "citekey", "role", "tag", "section", "month", "series",
                    "tier", "bucket", "year", "has_full_text", "note_file", "note_line",
                    "text", "vec")
            rows = conn.execute("SELECT {} FROM chunks".format(", ".join(cols))).fetchall()
            conn.execute("COMMIT")
        except sqlite3.OperationalError as e:
            raise VectorStoreError(
                "读取向量库失败（可能正被并发写/磁盘异常）：{}\n稍后重试或 `--full` 重建".format(e)
            ) from e
        finally:
            conn.close()

        n = len(rows)
        if n and not dim:
            raise VectorStoreError(
                "向量库 meta.dim=0 但有 {} 行 chunk，库损坏。请 `notes_embed.py --full` 重建。"
                .format(n))
        mat = np.zeros((n, dim), dtype=np.float32) if dim else np.zeros((n, 0), dtype=np.float32)
        records: List[dict] = []
        for i, row in enumerate(rows):
            (cid, level, citekey, role, tag, section, month, series, tier, bucket, year,
             has_full_text, note_file, note_line, text, vec_blob) = row
            if dim:
                if len(vec_blob) != dim * 4:
                    raise VectorStoreError(
                        "chunk '{}' 向量维度 {} 与 meta.dim {} 不符（{} 字节 ≠ {}*4）。"
                        "库可能混入旧模型残留，请 `notes_embed.py --full` 重建。"
                        .format(cid, len(vec_blob) // 4, dim, len(vec_blob), dim))
                mat[i] = np.frombuffer(vec_blob, dtype=np.float32)
            records.append({
                "id": cid, "level": level, "citekey": citekey, "role": role, "tag": tag,
                "section": section, "month": month, "series": series, "tier": tier,
                "bucket": bucket.split(",") if bucket else [], "year": year,
                "has_full_text": bool(has_full_text), "note_file": note_file,
                "note_line": note_line, "text": text,
            })
        return cls(meta=meta, records=records, mat=mat)

    def __len__(self) -> int:
        return len(self.records)

    def search(self, query_vec: np.ndarray, mask: Optional[np.ndarray] = None,
               top_k: int = 50) -> List[Tuple[int, float]]:
        """query_vec 需已 L2 归一。mask 为布尔数组（True=参与检索），先过滤再算分——
        窄过滤不会因为"top-k 后再筛"而空手。返回 [(row_idx, cosine_score), ...] 降序。"""
        if len(self.records) == 0:
            return []
        scores = self.mat @ query_vec.astype(np.float32)
        if mask is not None:
            scores = np.where(mask, scores, -np.inf)
        k = min(top_k, len(scores)) if top_k > 0 else len(scores)
        idx = np.argpartition(-scores, k - 1)[:k] if k < len(scores) else np.arange(len(scores))
        idx = idx[np.argsort(-scores[idx])]
        return [(int(i), float(scores[i])) for i in idx if np.isfinite(scores[i])]

    def freshness_warning(self, index_generated_at: Optional[str]) -> Optional[str]:
        """向量库落后当前索引时给一句提醒（不阻塞，stderr 打印用）。"""
        src = self.meta.get("source_generated_at") or ""
        if not src or not index_generated_at:
            return None
        if src < index_generated_at:
            return (
                "⚠️ 向量库落后于当前索引（库快照 {} vs 索引 {}），"
                "可能漏掉最新入库内容，建议跑 notes_embed.py 增量同步".format(
                    src, index_generated_at))
        return None
