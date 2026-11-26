# -*- coding: utf-8 -*-
"""二阶合成知识（topics/ 概念页与 topics/qa/ 问答页）的独立小向量库。

设计原则：
- 独立存放在 `synthesis_embeddings.sqlite3`，与一阶文献主库 `embeddings.sqlite3`
  物理隔离，不经过 `embed_store.sync_store`，绝不触碰其全集差集删除闸；
- 仅用于 `notes_search` 顶部导航提示与 JSON `related_syntheses` 键，**绝不**混入
  一阶 `results` 或 `--cite` 可粘贴引用串，防止二阶综述循环自引；
- 几十上百页全量重建仅需 1–2 秒，按页面文件 sha256 做增量 diff。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..utils.logger import get_logger
from .embeddings import EmbeddingClient, EmbeddingError, resolve_embedding_base_url
from .embed_store import model_matches
from .vault import GEN_BEGIN_RE, GEN_END, split_frontmatter

logger = get_logger(__name__)

SYNTHESIS_DB_NAME = "synthesis_embeddings.sqlite3"
SYNTHESIS_SCHEMA_VERSION = 1
_TEXT_CLIP = 800

_CREATE_SYN_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
_CREATE_SYN_PAGES = """
CREATE TABLE IF NOT EXISTS pages (
  path TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  vec BLOB NOT NULL
)
"""


@dataclass
class SynthesisPage:
    path: str        # 相对 notes_dir 的路径，如 topics/mnar-diagnosis.md
    kind: str        # "topic" 或 "qa"
    slug: str
    title: str
    text: str
    text_hash: str


def _is_synthesis_md(p: Path) -> bool:
    name = p.name
    return name.endswith(".md") and name != "INDEX.md" and not name.startswith("_")


def _extract_synthesis_text(raw: str, fm: dict) -> str:
    """提取页面核心检索文本：title + question + 生成块前 800 字（剥表与批注）。"""
    title = str(fm.get("title") or "").strip()
    question = str(fm.get("question") or "").strip()

    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            body = parts[2]
    m = GEN_BEGIN_RE.search(body)
    if m:
        body = body[m.end():]
    end_idx = body.find(GEN_END)
    if end_idx != -1:
        body = body[:end_idx]

    # 截断到「## 本页证据」之前，只留摘要/依据/缺口等核心论断
    for marker in ("\n## 本页证据", "\n## 本次召回没覆盖到的"):
        idx = body.find(marker)
        if idx != -1:
            body = body[:idx]
            break

    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("|") or s.startswith("<!--"):
            continue
        lines.append(s)
    excerpt = "\n".join(lines)[:_TEXT_CLIP]
    chunks = [x for x in (title, question if question != title else "", excerpt) if x]
    return "\n".join(chunks)


def collect_synthesis_pages(notes_dir: Path, model: str = "") -> List[SynthesisPage]:
    notes_dir = Path(notes_dir)
    topics_dir = notes_dir / "topics"
    if not topics_dir.exists():
        return []

    pages: List[SynthesisPage] = []
    scan_dirs = [
        ("topic", topics_dir),
        ("qa", topics_dir / "qa"),
    ]
    for kind, d in scan_dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            if not _is_synthesis_md(p):
                continue
            try:
                raw = p.read_text(encoding="utf-8")
                fm, _ = split_frontmatter(raw)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(fm, dict):
                continue
            # 证据不足存根页不入库（只在 ok 或未标旧页时入库）
            ev_status = fm.get("evidence_status")
            if ev_status is not None and ev_status != "ok":
                continue
            slug = str(fm.get("topic") or fm.get("qa") or p.stem)
            title = str(fm.get("title") or slug)
            text = _extract_synthesis_text(raw, fm)
            if not text.strip():
                continue
            rel = str(p.relative_to(notes_dir)).replace("\\", "/")
            h = hashlib.sha256((model + "\0" + text).encode("utf-8")).hexdigest()[:16]
            pages.append(SynthesisPage(path=rel, kind=kind, slug=slug, title=title, text=text, text_hash=h))
    return pages


def sync_synthesis_store(
    notes_dir: Path,
    client,
    *,
    full: bool = False,
) -> Dict[str, int]:
    notes_dir = Path(notes_dir)
    db_path = notes_dir / SYNTHESIS_DB_NAME
    model = getattr(client, "model", "bge-m3") or "bge-m3"
    pages = collect_synthesis_pages(notes_dir, model=model)
    expected = {pg.path: pg for pg in pages}

    existing_hash: Dict[str, str] = {}
    if db_path.exists() and not full:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(_CREATE_SYN_META)
            conn.execute(_CREATE_SYN_PAGES)
            meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
            old_model = meta.get("model", "")
            if not old_model or model_matches(old_model, model):
                existing_hash = dict(conn.execute("SELECT path, text_hash FROM pages"))
        finally:
            conn.close()

    to_embed = [pg for path, pg in expected.items() if existing_hash.get(path) != pg.text_hash]
    to_delete = [path for path in existing_hash if path not in expected]

    vec_by_path: Dict[str, bytes] = {}
    dim = 0
    if to_embed:
        vecs = client.embed([pg.text for pg in to_embed], batch_size=32)
        if vecs.size:
            dim = int(vecs.shape[1])
        for pg, v in zip(to_embed, vecs):
            vec_by_path[pg.path] = v.astype(np.float32).tobytes()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_CREATE_SYN_META)
        conn.execute(_CREATE_SYN_PAGES)
        if full:
            conn.execute("DELETE FROM pages")
        if to_embed:
            conn.executemany(
                "INSERT INTO pages (path, kind, slug, title, text_hash, vec) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "kind=excluded.kind, slug=excluded.slug, title=excluded.title, "
                "text_hash=excluded.text_hash, vec=excluded.vec",
                [(pg.path, pg.kind, pg.slug, pg.title, pg.text_hash, vec_by_path[pg.path]) for pg in to_embed],
            )
        if to_delete:
            conn.executemany("DELETE FROM pages WHERE path=?", [(p,) for p in to_delete])
        if not dim:
            row = conn.execute("SELECT LENGTH(vec) FROM pages LIMIT 1").fetchone()
            dim = (row[0] // 4) if row and row[0] else 0
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("schema_version", str(SYNTHESIS_SCHEMA_VERSION)),
                ("model", model),
                ("dim", str(dim)),
                ("built_at", datetime.now().isoformat()),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {"total": len(expected), "embedded": len(to_embed), "deleted": len(to_delete)}


def sync_synthesis_best_effort(notes_dir: Path, settings) -> None:
    try:
        client = EmbeddingClient(
            base_url=resolve_embedding_base_url(settings.llm),
            model=settings.llm.embedding_model,
        )
        try:
            res = sync_synthesis_store(notes_dir, client)
            logger.info(
                "📎 二阶合成向量库同步完成：{} 页（新嵌 {}，删 {}）",
                res["total"], res["embedded"], res["deleted"],
            )
        finally:
            client.close()
    except (EmbeddingError, Exception) as e:  # noqa: BLE001
        logger.warning("二阶合成向量库同步跳过（不影响页面归档）：{}", e)


class SynthesisStore:
    def __init__(self, meta: Dict[str, str], pages: List[dict], mat: np.ndarray):
        self.meta = meta
        self.pages = pages
        self.mat = mat
        self.model = meta.get("model", "")

    @classmethod
    def load(cls, db_path: Path) -> Optional["SynthesisStore"]:
        db_path = Path(db_path)
        if not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
                dim = int(meta.get("dim") or 0)
                rows = conn.execute("SELECT path, kind, slug, title, vec FROM pages").fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return None

        n = len(rows)
        if n == 0 or dim <= 0:
            return cls(meta=meta, pages=[], mat=np.zeros((0, 0), dtype=np.float32))
        mat = np.zeros((n, dim), dtype=np.float32)
        pages: List[dict] = []
        for i, (path, kind, slug, title, blob) in enumerate(rows):
            if len(blob) != dim * 4:
                return None
            mat[i] = np.frombuffer(blob, dtype=np.float32)
            pages.append({"path": path, "kind": kind, "slug": slug, "title": title})
        return cls(meta=meta, pages=pages, mat=mat)

    def search(self, query_vec: np.ndarray, *, min_sim: float = 0.55, top_k: int = 2) -> List[dict]:
        if not self.pages or self.mat.size == 0:
            return []
        scores = self.mat @ query_vec.astype(np.float32)
        order = np.argsort(-scores)
        out: List[dict] = []
        for idx in order[:top_k]:
            s = float(scores[idx])
            if s < min_sim:
                break
            p = dict(self.pages[int(idx)])
            p["score"] = round(s, 4)
            out.append(p)
        return out
