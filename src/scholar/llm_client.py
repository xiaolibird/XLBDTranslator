# -*- coding: utf-8 -*-
"""
独立的多提供商 LLM 客户端（从 ScholarWorkflow._create_llm_client/_call_llm 抽出）。

供 workflow 与全文精读（closereading）共用：给一个 prompt 字符串，返回文本；
可选结构化 JSON 输出、覆盖 model / max_tokens / temperature（精读读全文需要更大 max_tokens）。
提供商：gemini（google-genai）与 openai-compatible（deepseek 等）。凭据缺失时回退复用
config/config.env 的 OpenAI 兼容凭据（与原 workflow 行为一致）。
"""
import time
from pathlib import Path
from typing import Optional

import httpx

from ..utils.logger import get_logger

logger = get_logger(__name__)


class _RetryableHTTP(Exception):
    """内部：可重试的 HTTP 状态（429/5xx）。"""


class LLMClient:
    """封装 LLM 连接与调用。只依赖 LLMSettings，无 workflow 状态。"""

    def __init__(self, llm_settings):
        self.settings = llm_settings  # scholar.schema.LLMSettings
        self._conn = None

    @property
    def conn(self) -> dict:
        if self._conn is None:
            self._conn = self._create()
        return self._conn

    def _create(self) -> dict:
        provider = self.settings.provider.lower()

        if provider == 'gemini':
            from google import genai
            client = genai.Client(api_key=self.settings.api_key)
            return {'client': client, 'model': self.settings.model, 'provider': 'gemini'}

        # OpenAI 兼容（deepseek / openai-compatible）
        import httpx
        api_key = self.settings.openai_api_key
        base_url = self.settings.base_url
        if not api_key or not base_url:
            try:
                from ..core.schema import Settings as CoreSettings
                core = CoreSettings.from_env_file(Path('config/config.env'))
                api_key = api_key or core.api.openai_api_key
                base_url = base_url or core.api.openai_base_url
                logger.info("复用主配置 config/config.env 的 OpenAI 兼容凭据")
            except Exception as e:
                logger.debug("读取主配置失败: {}".format(e))

        if provider == 'deepseek' and not base_url:
            base_url = 'https://api.deepseek.com/v1'
        if not api_key:
            raise ValueError(
                "未找到 OpenAI 兼容 API 密钥：请在 config/scholar.env 设置 "
                "LLM__OPENAI_API_KEY，或在 config/config.env 设置 API__OPENAI_API_KEY")
        if not base_url:
            raise ValueError("未找到 API 地址：请设置 LLM__BASE_URL 或 API__OPENAI_BASE_URL")

        return {
            'client': httpx.Client(timeout=120.0),
            'base_url': base_url.rstrip('/'),
            'api_key': api_key,
            'model': self.settings.model,
            'provider': 'openai-compatible',
        }

    def call(self, prompt: str, model: Optional[str] = None,
             max_tokens: Optional[int] = None, temperature: Optional[float] = None,
             json_mode: bool = False, max_retries: int = 4) -> str:
        """调用 LLM，返回文本。

        model/max_tokens/temperature 缺省时用 settings；json_mode=True 请求结构化 JSON。
        对限流(429)/服务端(5xx)/超时/连接错误做指数退避重试（并发多月回填时保证稳健）。
        """
        conn = self.conn
        provider = conn.get('provider')
        temp = self.settings.temperature if temperature is None else temperature
        mtok = self.settings.max_output_tokens if max_tokens is None else max_tokens

        last_exc = None
        for attempt in range(max_retries):
            try:
                if provider == 'gemini':
                    from google.genai import types
                    cfg = dict(temperature=temp, max_output_tokens=mtok)
                    if json_mode:
                        cfg['response_mime_type'] = 'application/json'
                    response = conn['client'].models.generate_content(
                        model=model or conn['model'],
                        contents=prompt,
                        config=types.GenerateContentConfig(**cfg),
                    )
                    return response.text

                # OpenAI 兼容
                payload = {
                    "model": model or conn['model'],
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temp,
                    "max_tokens": mtok,
                }
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
                resp = conn['client'].post(
                    "{}/chat/completions".format(conn['base_url']),
                    headers={"Authorization": "Bearer {}".format(conn['api_key']),
                             "Content-Type": "application/json"},
                    json=payload,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise _RetryableHTTP("HTTP {}".format(resp.status_code))
                resp.raise_for_status()
                return resp.json()['choices'][0]['message']['content']
            except (_RetryableHTTP, httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < max_retries - 1:
                    time.sleep(2.0 * (2 ** attempt))  # 2s,4s,8s 退避
                    continue
                raise
        if last_exc:
            raise last_exc
