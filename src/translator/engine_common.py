# -*- coding: utf-8 -*-
"""翻译引擎的跨后端共享层（自 engine.py 拆出，阶段 4a）。

只放 Gemini/OpenAI 两侧**都用**的 helper。铁律：本模块不得 import google.genai
——`_TRANSLATION_RESPONSE_SCHEMA` 是 types.Schema 实例，归 engine_gemini；放这里
会让共享层强依赖 genai SDK（PRD 审定）。
外部（测试/agent.py）一律经 `src.translator.engine` 门面 import，不直连本模块。
"""
from typing import Any, Dict, List

from ..utils.logger import get_logger

logger = get_logger(__name__)


def _reraise_if_provider_fatal(exc: Exception) -> None:
    """provider 级错误不能吞成 [Failed:] 字符串，必须上抛让回退链（FallbackTranslator）感知：
    - hard（欠费/认证失败/模型下线）：一次即切 provider；
    - soft（429/quota 配额限流）：交给回退链计数——未达阈值原样上抛由上层处理，
      连续达阈值切 provider 重放，与文本路径的传导语义完全对齐。
    若在这里吞掉 soft，不仅回退链收不到计数，"正常返回"还会把 _soft_failures
    清零，配额耗尽时视觉段会整本逐段写死为 [Failed:] 而兜底 provider 永不触发。"""
    from .fallback import classify_fatal
    if classify_fatal(exc) in ('hard', 'soft'):
        raise exc



_VISION_TRANSIENT_RETRIES = 2


GLOSSARY_WINDOW_SIZE = 8000


def _stop_by_settings(retry_state) -> bool:
    """tenacity stop 回调：读实例 settings.processing.max_retries。

    此前两处 @retry 硬编码 stop_after_attempt(3)，quality 预设声称的
    max_retries=5 从未生效。
    """
    try:
        self_obj = retry_state.args[0]
        limit = int(getattr(self_obj.settings.processing, 'max_retries', 3))
    except Exception:
        limit = 3
    return retry_state.attempt_number >= max(1, limit)



def _glossary_extraction_prompt(content_sample: str) -> str:
    """术语抽取 prompt 的唯一出处（Gemini 与 OpenAI 兼容路径共用）。

    此前两个 Translator 各持一份拷贝且已漂移（OpenAI 版丢了示例输出块），
    改一处不同步另一处。以带示例的版本为准。
    """
    return f"""
You are an expert linguist and terminologist.
Analyze the following pairs of original and translated text. Identify all key, recurring, or specialized terms (like names, places, philosophical concepts, technical jargon) and create a definitive glossary.

RULES:
1. The output MUST be a flat JSON object.
2. Keys are the original English terms.
3. Values are their corresponding Chinese translations found in the text.
4. Focus on nouns and proper nouns.
5. Be precise. The goal is to enforce consistency.

Example Output Format:
{{
    "Slavoj Žižek": "斯拉沃热·齐泽克",
    "the Real": "实在界",
    "Objet petit a": "客体小 a"
}}

Text to Analyze:
<text>
{content_sample}
</text>

Return ONLY the JSON object.
""".strip()



def _normalize_translation_list(parsed: Any) -> List[Dict[str, Any]]:
    """把翻译解析结果规整成 [{"id":..,"translation":..}] 列表。

    DeepSeek 的 response_format=json_object 语义是「顶层为对象」，模型可能把数组包成
    {"translations":[...]}/{"data":[...]} 或退化成 {id: 译文} 映射；直接遍历 dict 会拿到键、
    导致整批被误判缺失。这里统一解包，避免结构化输出反而打断解析。
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("translations", "data", "results", "papers", "items"):
            val = parsed.get(key)
            if isinstance(val, list):
                return val
        # 退化的 {id: 译文} 映射（值为字符串）
        if parsed and all(isinstance(v, str) for v in parsed.values()):
            out = []
            for k, v in parsed.items():
                out.append({"id": k, "translation": v})
            return out
    return []



def _iter_glossary_windows(pair_texts: List[str], window_size: int = GLOSSARY_WINDOW_SIZE):
    """把「原文/译文」配对字符串按字符预算切成多个窗口，保证覆盖全文。

    单条配对若本身超过窗口大小，则单独成窗（不再截断，交给模型自行处理）。
    """
    window: List[str] = []
    length = 0
    for pair in pair_texts:
        if window and length + len(pair) > window_size:
            yield "\n".join(window)
            window, length = [], 0
        window.append(pair)
        length += len(pair) + 1
    if window:
        yield "\n".join(window)


# ========================================================================
# Gemini 翻译客户端
# ========================================================================
