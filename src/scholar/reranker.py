# -*- coding: utf-8 -*-
"""交叉编码器重排（bge-reranker-v2-m3），notes_search 的 post-retrieval 排序层。

试验依据（docs/decisions/rerank_hyde_experiment_2026-08.md，2026-08-29，87 case）：
hybrid 基线 66@1/0.8162 → hybrid+rerank 75@1/0.8642；中文换述档 16→21@1（dense 侧
18→25）。逐 case 差分赢 13 输 7，输的集中在短缩写 query 名次 1→2 的小滑动。

铁律（与 thresholds.py 同级）：
- reranker 分是交叉编码器 logit，与余弦是**两个不可比的量纲**——只能用于排序，
  绝不能拿去和 NOTES_SEARCH_MIN_SCORE / DIGEST_NEIGHBOR_MIN_SIM(0.62) 等任何
  min_sim 门槛比较。门槛过滤永远发生在重排之前、按余弦执行。
- 本模块只重排**已经检索出来的集合**，不改变集合成员（试验验证的就是纯重排口径）。

使用边界：本模块面向 **CLI 短进程**（notes_search 一次调用一次加载）。若将来被
workflow 等长驻进程引用须重新评估：KMP_DUPLICATE_LIB_OK 是进程级副作用且会传给
子进程；宿主若在调用前已 import torch，该 workaround 为时已晚。

环境备忘：模型 ~2.3GB 须已在 HF 缓存（HF 直连被墙，2026-08-29 经
HF_ENDPOINT=https://hf-mirror.com 下载）；conda 的 torch 与 numpy 各带一份 libomp，
不设 KMP_DUPLICATE_LIB_OK 会在 import 后直接 abort（计算走 MPS/纯推理，该 workaround
不影响打分正确性，方向性检查见 rerank_hyde_experiment_2026-08.md：相关对 +1.65/
离题对 −11.0；打桩回归在 test/test_notes_search_rerank.py，不载真模型）。
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
MAX_LENGTH = 512   # query+doc 联合截断；ab: chunk（title+判词+摘要[:800]）在此长度内
BATCH_SIZE = 32

_scorer: Optional[Tuple] = None      # (torch, tokenizer, model, device) 进程内单例
_load_error: Optional[str] = None    # 首次加载失败后短路，同进程不再重试


class RerankUnavailable(RuntimeError):
    """模型/依赖不可用。调用方必须降级回原排序，绝不能让重排层挡住检索主路径。"""


def _load() -> None:
    global _scorer, _load_error
    if _scorer is not None or _load_error is not None:
        return
    # 必须在 torch import 前设好：libomp 的重复加载检查发生在第二份初始化时
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    try:
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        # 必须两步走：先把 repo id 解析成**本地快照目录**，再从目录加载。
        # 直接 from_pretrained(repo_id, local_files_only=True) 不行——transformers
        # 4.57.3 的 _patch_mistral_regex→is_base_mistral 会无视 local_files_only 打
        # huggingface.co 的 API（上游 bug），在 HF 被墙的网络下每次 CLI 调用挂十几秒
        # 后失败。snapshot_download(local_files_only=True) 纯本地解析，缓存没有就
        # 立刻抛错走降级，绝不碰网络。
        local_dir = snapshot_download(RERANK_MODEL, local_files_only=True)
        tok = AutoTokenizer.from_pretrained(local_dir)
        model = AutoModelForSequenceClassification.from_pretrained(local_dir)
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _scorer = (torch, tok, model.to(device).eval(), device)
    except Exception as e:  # noqa: BLE001 —— 任何加载失败都走同一条降级路径
        _load_error = "{}: {}".format(type(e).__name__, str(e)[:200])


def available() -> bool:
    _load()
    return _scorer is not None


def load_error() -> Optional[str]:
    return _load_error


def rerank_scores(query: str, docs: List[str]) -> List[float]:
    """对 (query, doc) 逐对打分，返回与 docs 同长的分数列表（大者更相关）。

    抛 RerankUnavailable 时调用方降级；其余异常不在此吞——打分中途炸说明
    环境有真问题，静默降级会把它藏起来。
    """
    _load()
    if _scorer is None:
        raise RerankUnavailable(_load_error or "未知加载失败")
    torch, tok, model, device = _scorer
    scores: List[float] = []
    with torch.no_grad():
        for i in range(0, len(docs), BATCH_SIZE):
            pairs = [(query, d) for d in docs[i:i + BATCH_SIZE]]
            enc = tok(pairs, padding=True, truncation=True,
                      max_length=MAX_LENGTH, return_tensors="pt").to(device)
            scores.extend(model(**enc).logits.view(-1).float().cpu().tolist())
    if len(scores) != len(docs):
        # num_labels≠1 的错模型头会让 view(-1) 吐 2 倍分数，zip 静默错配——必须炸响
        raise RuntimeError("rerank 打分数量与文档数不符：{} vs {}（模型头疑似非单标签，"
                           "检查 HF 缓存快照）".format(len(scores), len(docs)))
    return scores
