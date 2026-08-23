# -*- coding: utf-8 -*-
"""本地 Ollama embedding 客户端（札记库语义检索用）。

刻意不接 LLMClient 的多提供商回退链：embedding 换模型=向量库全废（维度/语义空间
都不兼容），静默回退到另一个模型会产出混合模型的坏索引且没有报错信号。这里反其
道而行——任何异常直接抛 EmbeddingError，调用方（notes_embed.py/notes_search.py）
自行决定报错退出，不吞不猜。
"""
import time
from typing import List, Optional

import httpx
import numpy as np

from ..utils.logger import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 3
_RETRYABLE = (httpx.TimeoutException, httpx.TransportError)

# llama.cpp 对 embedding 请求的单次批处理上限（token）。**不传就是 2048**，
# 与模型自身的上下文长度无关：`ollama show bge-m3` 报的 8192 是模型能力，
# 运行时真正卡脖子的是 num_batch。实测（2026-08-23）不传时 2872 中文字符 = 2048
# token 处即报 `input length exceeds the context length`，传 8192 后同一条 4176
# token 的文本全量吃进。默认 truncate=true 会把超出部分**静默丢弃**——向量与全文
# 语义不符且没有任何信号，正是本模块 docstring 拒绝的那类坏索引。
_NUM_BATCH = 8192


class EmbeddingError(RuntimeError):
    """Ollama 不可达 / 模型未 pull / 返回维度异常。message 带可操作命令。"""


def resolve_embedding_base_url(llm_settings) -> str:
    """embedding_base_url 显式配置时优先；否则从 ollama_base_url 派生。

    ollama_base_url 默认是 OpenAI 兼容端点（形如 `http://host:11434/v1`），而
    embedding 走 Ollama 原生 `/api/*` 路径，不带 `/v1` 前缀，故去掉尾部 `/v1`。
    """
    explicit = getattr(llm_settings, "embedding_base_url", None)
    if explicit:
        return explicit.rstrip("/")
    base = getattr(llm_settings, "ollama_base_url", None) or "http://localhost:11434/v1"
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _status_error_message(exc: httpx.HTTPStatusError, batch: List[str]) -> str:
    """把 Ollama 的错误状态翻译成可操作信息。

    truncate=false 下超长文本会拿到 400 `input length exceeds the context length`，
    而一批 64 条里只要有一条超长就整批失败——只报状态码等于让人从 64 条里瞎猜，
    所以这里必须点名最长的那条（入库侧的超长嫌疑犯只会是句级 highlight，它没有
    长度上限，abstract 有 ABSTRACT_CLIP=800 兜着）。
    """
    body = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            body = resp.text[:300]
        except Exception:
            body = ""
    if resp is not None and resp.status_code == 400 and "context length" in body:
        longest = max(batch, key=len) if batch else ""
        return (
            "文本超出 embedding 模型输入上限（本批 {} 条，最长 {} 字符）：\n"
            "  {}…\n"
            "本客户端刻意不静默截断（truncate=false）：截断会产出与全文语义不符的向量且没有任何信号。\n"
            "请缩短该文本（如给 highlight 设长度上限），或确认 Ollama 支持 options.num_batch={}。"
        ).format(len(batch), len(longest), longest[:60].replace("\n", " "), _NUM_BATCH)
    return "Ollama embedding 返回错误状态：{}\n{}".format(exc, body)


class EmbeddingClient:
    """封装 Ollama 原生 `/api/embed`（支持批量 input 数组）。

    只做一件事：文本 -> L2 归一化的 float32 向量。没有回退、没有多 provider。
    """

    def __init__(self, base_url: str = "http://localhost:11434",
                 model: str = "bge-m3", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None
        self._probed = False
        self._probe_dim = 0

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------

    def probe(self) -> dict:
        """确认 Ollama 起着、模型已 pull，再试嵌一条拿真实维度。

        返回 {"model": str, "dim": int}。任何失败都抛 EmbeddingError，
        message 里带下一步该敲的命令（不是让用户自己去猜为什么连不上）。
        成功过则直接返回缓存结果（同一 client 实例只探活一次）。
        """
        if self._probed:
            return {"model": self.model, "dim": self._probe_dim}
        try:
            resp = self.client.get("{}/api/tags".format(self.base_url))
            resp.raise_for_status()
        except httpx.TransportError as e:
            raise EmbeddingError(
                "连不上 Ollama（{}）：{}\n请先启动 Ollama（如 `ollama serve` 或打开 Ollama.app）".format(
                    self.base_url, e)) from e
        except httpx.HTTPStatusError as e:
            raise EmbeddingError(
                "Ollama 返回异常状态：{}\n{}".format(e, self.base_url)) from e

        try:
            names = {m.get("name", "").split(":")[0] for m in resp.json().get("models", [])}
        except Exception as e:
            raise EmbeddingError("解析 Ollama /api/tags 响应失败：{}".format(e)) from e

        model_base = self.model.split(":")[0]
        if model_base not in names:
            raise EmbeddingError(
                "模型 '{}' 未在本地 Ollama 找到（已装：{}）\n请先执行：ollama pull {}".format(
                    self.model, ", ".join(sorted(names)) or "（空）", self.model))

        vec = self.embed(["probe"])
        dim = int(vec.shape[1])
        if dim <= 0:
            raise EmbeddingError("模型 '{}' 返回了异常维度：{}".format(self.model, dim))
        self._probed = True
        self._probe_dim = dim
        return {"model": self.model, "dim": dim}

    def embed(self, texts: List[str], batch_size: int = 64) -> np.ndarray:
        """批量嵌入，返回 (n, dim) float32，每行已 L2 归一化。

        超时/连接错误重试 3 次指数退避（1s/2s/4s）；其余错误（4xx/5xx/JSON 解析
        失败）不重试，直接抛——重试对确定性错误没有意义，只会拖慢失败反馈。
        """
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        rows: List[np.ndarray] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            rows.append(self._embed_batch(batch))
        try:
            mat = np.vstack(rows).astype(np.float32)
        except ValueError as e:
            # 批间维度不一致（服务端模型热切换等罕见情况）——调用方契约是只捕 EmbeddingError
            raise EmbeddingError("embedding 批间维度不一致：{}".format(e)) from e
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # 全零向量（异常输入）避免除零，原样保留
        return mat / norms

    def _embed_batch(self, batch: List[str]) -> np.ndarray:
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.client.post(
                    "{}/api/embed".format(self.base_url),
                    json={
                        "model": self.model,
                        "input": batch,
                        # 超长直接报错，不静默截断（见 _NUM_BATCH 注释与 _status_error_message）
                        "truncate": False,
                        "options": {"num_batch": _NUM_BATCH},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings")
                if not embeddings or len(embeddings) != len(batch):
                    raise EmbeddingError(
                        "Ollama /api/embed 返回数量与输入不符（期望 {}，收到 {}）".format(
                            len(batch), len(embeddings) if embeddings else 0))
                return np.array(embeddings, dtype=np.float32)
            except _RETRYABLE as e:
                last_exc = e
                if attempt < _MAX_RETRIES - 1:
                    wait = 1.0 * (2 ** attempt)
                    logger.warning("⚠️ embedding 请求超时/断连，{}s 后重试（{}/{}）：{}".format(
                        wait, attempt + 1, _MAX_RETRIES, e))
                    time.sleep(wait)
                    continue
                raise EmbeddingError(
                    "Ollama embedding 请求重试 {} 次仍失败：{}".format(_MAX_RETRIES, e)) from e
            except httpx.HTTPStatusError as e:
                raise EmbeddingError(_status_error_message(e, batch)) from e
            except EmbeddingError:
                raise
            except Exception as e:
                raise EmbeddingError("Ollama embedding 调用失败（{}）：{}".format(
                    type(e).__name__, e)) from e
        if last_exc:
            raise EmbeddingError("Ollama embedding 请求失败：{}".format(last_exc)) from last_exc
        raise EmbeddingError("Ollama embedding 请求失败：未知原因")
