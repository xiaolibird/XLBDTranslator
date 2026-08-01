"""JSON 文本预处理工具（跨 translator/scholar 共用）"""
import re


def strip_code_fences(text: str) -> str:
    """剥掉 LLM 响应外层的 markdown 代码围栏（```json ... ```）。

    此前 scholar/closereading.py 与 scholar/pdf_ingest.py 各持一份逐字相同的
    `_strip_json`，改一处不同步另一处；收敛为唯一出处。
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()
