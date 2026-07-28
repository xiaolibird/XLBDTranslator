# -*- coding: utf-8 -*-
"""仓库根锚点：把配置/CLI 里的**相对**路径固定到仓库根，而不是随进程 cwd 漂。

为什么需要：`notes_dir` 默认值 `output/scholar_notes` 是相对的，语义上是「仓库里的那个目录」，
但 `Path(...)` 按 cwd 解析。一旦某条命令里 `cd` 过（复合命令中的 cd 会留在后续调用里），
同一个配置就会指向 `<别处>/output/scholar_notes`——札记静默写去空目录、索引读回空、
下一步"重建"再把正确产物覆盖成空。这类故障不报错、只丢数据，实际已踩过两次。

用法：只在 **CLI 边界** 调用（脚本读完 args/配置后立刻锚定一次），库函数仍按传入路径办事，
这样 tmp_path 之类的绝对路径与显式相对路径调用不受影响。
"""
from pathlib import Path

# src/scholar/paths.py → parents[2] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_path(p) -> Path:
    """相对路径 → 挂到仓库根下；绝对路径（含 `~`）原样展开返回。"""
    q = Path(p).expanduser()
    return q if q.is_absolute() else (REPO_ROOT / q)
