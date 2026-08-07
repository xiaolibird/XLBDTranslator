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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows fallback

from ..utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 2  # v2: highlight chunk id 掺入 role+序号，修复同文本覆盖丢数据（老库需 --full 重建）

DB_NAME = "embeddings.sqlite3"  # notes_dir 下的库文件名，全部调用方从这里取，别再各自硬编码


def model_matches(store_model: Optional[str], want_model: Optional[str]) -> bool:
    """embedding 模型一致性判定：只比基础名（split(":")[0]）——`bge-m3:latest` 与
    `bge-m3` 是同一模型，整串比较会让带 tag 的配置触发虚假的"模型不一致"。"""
    return (store_model or "").split(":")[0] == (want_model or "").split(":")[0]

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

        # 压掉内嵌换行：paper 文本用 "\n" 分隔 title/one_line，下游 split("\n",1) 还原，
        # 多行标题（邮件解析产物）会把标题后半误当 one_line
        title = (e.get("title") or "").replace("\n", " ").strip()
        one_line = (e.get("one_line") or "").replace("\n", " ").strip()
        paper_text = "{}\n{}".format(title, one_line) if one_line else title
        if not paper_text:
            # title 与 one_line 都空：嵌空串只会得到无意义向量占据检索名额，跳过
            logger.warning("citekey '{}' 的 title/one_line 均为空，跳过 paper 级 chunk", citekey)
            continue
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
    # WAL：写事务 commit 时读方不再被短暂互斥（读快照不阻塞，超出默认 5s busy timeout 也不炸读方）
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
    - dry_run=True：只算计划不落库、不调 client.embed/probe（db 已存在时仍会建只读连接做 diff）。
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

    # --full 模式持排他文件锁：防止两个进程同时全量重建互相覆盖（各自 embed 完再
    # os.replace，后完成的把先完成的静默抹掉）。增量模式不抢锁——WAL + BEGIN IMMEDIATE
    # 已经提供足够隔离。
    lock_fd = None
    lock_path = None
    if full:
        lock_path = db_path.with_suffix(db_path.suffix + ".lock")
        if fcntl is not None:
            try:
                lock_fd = open(str(lock_path), "w")
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError) as e:
                if lock_fd:
                    lock_fd.close()
                raise VectorStoreError(
                    "无法获取向量库排他锁（{}）：另一个 --full 正在运行，稍后重试。"
                    "（flock 随进程退出自动释放，不存在需要手动清理的残留；"
                    "删除锁文件反而会让两个 --full 并行互相覆盖）".format(e))
        else:
            # Windows fallback：用文件存在性作为互斥信号
            try:
                lock_path.touch(exist_ok=False)
            except FileExistsError:
                # 进程崩溃后锁文件残留会导致永久无法重建——检查 mtime 清理残留
                age = time.time() - lock_path.stat().st_mtime
                if age > 3600:  # 超过 1 小时视为上次崩溃残留
                    logger.warning("锁文件 {} 已残留 {:.0f} 分钟，疑似上次进程崩溃，自动清理",
                                   lock_path, age / 60)
                    lock_path.unlink()
                    try:
                        lock_path.touch(exist_ok=False)
                    except FileExistsError:
                        # 两进程同时判定残留并清理，对方抢先重建了锁
                        raise VectorStoreError(
                            "无法获取排他锁 {}：清理残留后被并发 --full 抢占，稍后重试。".format(lock_path))
                else:
                    raise VectorStoreError(
                        "无法获取排他锁 {}：已有另一个 --full 进程在运行。".format(lock_path))

    try:
        existing_hash: Dict[str, str] = {}
        existing_level: Dict[str, str] = {}
        db_exists = db_path.exists()
        if db_exists and full:
            # --full 前核对索引是否骤缩（配合过期/部分生成的索引会把好库原子替换成小库）
            conn = None  # connect() 本身抛错（路径是目录/权限拒绝）时 conn 保持 None
            try:
                conn = sqlite3.connect(str(db_path))
                old_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            except sqlite3.DatabaseError:
                old_count = 0
            finally:
                if conn is not None:
                    conn.close()
            if old_count and len(chunks) < old_count * 0.5:
                raise VectorStoreError(
                    "索引 chunk 数 {} 不到现有库 {} 的一半，疑似索引过期/部分生成。"
                    "拒绝 --full 替换，避免好库被缩水。确认无误请手动处理。".format(
                        len(chunks), old_count))
        if db_exists and not full:
            # connect() 在 try 外：它自己抛错时直接向上传播，不存在 close 未定义对象的问题
            conn = sqlite3.connect(str(db_path))
            try:
                _ensure_schema(conn)
                meta = _read_meta_dict(conn)
                old_model = meta.get("model", "")
                if old_model and not model_matches(old_model, model):
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

        if not full and existing_hash and len(chunks) < len(existing_hash) * 0.5:
            # 与 --full 同款骤缩防护：索引重建中途产物只剩零头时，增量会把差集全删。
            # 库虽可自愈但整库重嵌是小时级，且无人值守链路正走增量——一样拒绝。
            raise VectorStoreError(
                "索引 chunk 数 {} 不到现有库 {} 的一半，疑似索引过期/部分生成。"
                "拒绝增量同步（将删除 {} 条）。确认索引无误后重跑；确属大幅删减，"
                "请删除库文件后 --full 重建。".format(
                    len(chunks), len(existing_hash),
                    sum(1 for cid in existing_hash if cid not in expected)))

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

        # embed 全部放在写事务之外：写锁持有时长从"embed 总耗时"（分钟级网络调用）降到
        # 纯写入（毫秒级）。并发安全不靠锁住 embed 期间——靠 BEGIN IMMEDIATE 后的快照复核：
        # 期间库被并发修改则整体放弃报错重试，而不是拿着锁让并发写方 database is locked。
        # 全量向量驻留内存的代价可接受：1 万 chunk × 1024 维 float32 ≈ 41MB。
        vec_by_id: Dict[str, bytes] = {}
        dim: Optional[int] = None
        if to_embed_ids:
            total = len(to_embed_ids)
            done = 0
            for i in range(0, total, batch_size):
                batch_ids = to_embed_ids[i:i + batch_size]
                texts = [expected[cid][0].text for cid in batch_ids]
                vecs = client.embed(texts, batch_size=batch_size)
                if dim is None and vecs.size:
                    dim = int(vecs.shape[1])
                for cid, vec in zip(batch_ids, vecs):
                    vec_by_id[cid] = vec.astype(np.float32).tobytes()
                done += len(batch_ids)
                if progress_cb:
                    progress_cb(done, total)
            if dim is None:
                dim = 0

        # isolation_level=None：autocommit，显式管理事务（BEGIN IMMEDIATE / COMMIT / ROLLBACK）。
        conn = sqlite3.connect(str(write_path), isolation_level=None)
        sync_ok = False
        pre_stat = None
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            if not full:
                # 记录连接期文件身份：_ensure_schema 已建表落盘，文件此刻必然存在。
                # COMMIT 前重新 stat 比对 (st_ino, st_dev)——并发 --full 的 os.replace
                # 换 inode 后，本连接的写会落在已 unlink 的旧 inode 上被静默丢弃。
                # （不能用 os.path.samefile(db_path, write_path)：增量下两者是同一路径，
                # 按路径 stat 两次永远相等，检测不到 fd 指向的旧 inode 已被换。）
                st = os.stat(str(db_path))
                pre_stat = (st.st_ino, st.st_dev)
                cur.execute("BEGIN IMMEDIATE")
                # diff 读（上面独立 conn）与本事务之间的并发修改，这里全量比对必然抓到，
                # 不会出现"基于过期快照的 lost update"。
                current = dict(cur.execute("SELECT id, text_hash FROM chunks"))
                if current != existing_hash:
                    diff_ids = [cid for cid in set(current) | set(existing_hash)
                                if current.get(cid) != existing_hash.get(cid)]
                    raise VectorStoreError(
                        "向量库在 diff 快照后被并发修改（{} 处）。本次同步已放弃，"
                        "请重试（重跑 notes_embed.py）。".format(len(diff_ids)))

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
                cur.executemany(upsert_sql, (
                    (c.id, c.level, c.citekey, c.role, c.tag, c.section, c.month, c.series,
                     c.tier, c.bucket, c.year, c.has_full_text, c.note_file, c.note_line,
                     c.text, h, vec_by_id[cid])
                    for cid, (c, h) in ((cid, expected[cid]) for cid in to_embed_ids)
                ))

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
                # 已知窗口（接受）：反向时序——增量先 COMMIT、--full 用更早的索引快照后
                # replace，会把这次增量结果短暂回退；下一轮增量 diff 自动补齐。
                # stat 比对与 COMMIT 之间仍有极窄竞态窗口，接受（--full 锁已排除双 full）。
                try:
                    st = os.stat(str(db_path))
                except FileNotFoundError:
                    raise VectorStoreError(
                        "向量库文件在同步期间消失（被删除或替换后清理）。本次增量已放弃，请重试。")
                if (st.st_ino, st.st_dev) != pre_stat:
                    raise VectorStoreError(
                        "向量库文件被并发 --full 替换（inode 已换）。本次增量已放弃，请重试。")
                conn.execute("COMMIT")
            sync_ok = True
        except BaseException:
            if not full:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()
            # tmp 清理必须在 conn.close() 之后：Windows 上删打开中的文件抛 PermissionError，
            # 且异常路径上它会**替换**真正的失败原因向上传播（掩盖 embed 维度错/磁盘满等）
            if not sync_ok and tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        if tmp_path is not None:
            # WAL sidecar 按**文件名**配对：旧库残留的 -wal 会在换上的新库上被错误回放
            # （SQLite 官方列明的损坏源）。替换前对旧库 checkpoint 清 WAL，替换后删残留。
            if db_path.exists():
                try:
                    old_conn = sqlite3.connect(str(db_path))
                    try:
                        old_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        old_conn.close()
                except sqlite3.DatabaseError:
                    pass
            os.replace(str(tmp_path), str(db_path))
            for sfx in ("-wal", "-shm"):
                try:
                    os.unlink(str(db_path) + sfx)
                except OSError:
                    pass

        return stats
    finally:
        # 释放 --full 排他锁
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass
        elif lock_path is not None:
            # Windows fallback: 删除锁文件
            try:
                lock_path.unlink()
            except OSError:
                pass


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

    MAX_IN_MEMORY_CHUNKS = 50000

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
            try:
                meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    meta = {}  # 真·未初始化
                else:
                    raise  # database is locked / 磁盘错误 → 交给外层"稍后重试"分支，别误报成"未初始化"
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
            # 先 COUNT 预警，超过阈值再 fetchall 加载全表（避免静默 OOM）
            row_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            if row_count > cls.MAX_IN_MEMORY_CHUNKS:
                logger.warning(
                    "向量库行数 {} 超过内存阈值 {}，建议迁移到 faiss 或分批加载；"
                    "当前仍全量加载，若 OOM 请先清理旧库或缩小索引范围",
                    row_count, cls.MAX_IN_MEMORY_CHUNKS)
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
