# -*- coding: utf-8 -*-
"""LLMClient.call_with_meta / CallMeta 测试（可信度加固 WO-1）。

覆盖：回退链上的 provider/chain_index/fell_back、内容拒答临时切换后链位复原而 meta 仍记
真实应答者、多线程不串线、各 provider 分支的 served_model 解析、降级半截正文标记、
call() 与 call_with_meta() 文本一致。全部桩/mock，不联网、不调真实 LLM。

claude-agent 信封样本说明：modelUsage 是以模型 id 为键、值含 outputTokens 的字典，
结构依据 Claude Agent SDK 的 ModelUsage 类型（本机 CLI 2.1.287 二进制内可见 modelUsage /
outputTokens / costUSD 字样）。样本为按该结构手写的固定夹具，没有真实调用抓取。
"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.scholar.llm_client import CallMeta, LLMClient, _claude_envelope_served_model
from src.scholar.schema import LLMSettings


def _settings(**kw) -> LLMSettings:
    base = dict(provider="deepseek", model="fake-model")
    base.update(kw)
    return LLMSettings(**base)


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("HTTP {}".format(self.status_code), request=req,
                                        response=httpx.Response(self.status_code, request=req))

    def json(self):
        return self._body


def _ok_body(text="ok", model="srv-model"):
    return {"model": model, "choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class _PostClient:
    """假 httpx.Client：按 handler(payload) 决定响应。"""
    def __init__(self, handler):
        self.handler = handler

    def post(self, url, headers=None, json=None):
        return self.handler(json)


def _conn(name, handler, model="m"):
    return {'client': _PostClient(handler), 'base_url': 'http://x', 'api_key': 'k',
            'model': model, 'provider': 'openai-compatible'}


def _client_with(conns: dict, **kw) -> LLMClient:
    """conns: 链上名字 -> conn 字典（_create 的替身）。"""
    client = LLMClient(_settings(**kw))
    client._create = lambda name: dict(conns[name])
    return client


# ==================== 回退链 ====================

def test_主provider应答_meta记主链位():
    c = _client_with({'deepseek': _conn('deepseek', lambda p: _Resp(200, _ok_body("hi", "ds-real")),
                                        model="deepseek-v4-flash")})
    text, meta = c.call_with_meta("p", model="req-model")
    assert text == "hi"
    assert isinstance(meta, CallMeta)
    assert (meta.provider, meta.chain_index, meta.fell_back) == ("deepseek", 0, False)
    assert meta.requested_model == "req-model"
    assert meta.sent_model == "req-model"       # 主链尊重调用方 override
    assert meta.served_model == "ds-real"
    assert meta.degraded_partial is False
    assert len(meta.at) == 19                   # ISO 到秒


def test_主provider欠费_第二家应答_meta记第二家且fell_back():
    c = _client_with({
        'deepseek': _conn('deepseek', lambda p: _Resp(402), model="deepseek-v4-flash"),
        'ollama': _conn('ollama', lambda p: _Resp(200, _ok_body("b", "qwen3.5:35b")), model="qwen-local"),
    }, fallback_providers="ollama")
    text, meta = c.call_with_meta("p", model="deepseek-v4-pro")
    assert text == "b"
    assert (meta.provider, meta.chain_index, meta.fell_back) == ("ollama", 1, True)
    assert meta.requested_model == "deepseek-v4-pro"
    assert meta.sent_model == "qwen-local"      # 切链后 override 被忽略
    assert meta.served_model == "qwen3.5:35b"
    # 粘性：后续调用仍在第二家，但不再算「本次调用 fell_back」
    _, meta2 = c.call_with_meta("p")
    assert (meta2.provider, meta2.chain_index, meta2.fell_back) == ("ollama", 1, False)


def test_内容拒答临时切换_链位复原_meta仍记真实应答者():
    def refuse(p):
        raise RuntimeError("claude cli: Output blocked by content policy")
    c = _client_with({
        'deepseek': _conn('deepseek', refuse),
        'ollama': _conn('ollama', lambda p: _Resp(200, _ok_body("ok", "q")), model="qwen-local"),
    }, fallback_providers="ollama")
    text, meta = c.call_with_meta("p")
    assert (meta.provider, meta.chain_index, meta.fell_back) == ("ollama", 1, True)
    assert c._chain_idx == 0                     # 链位拨回，meta 不受影响


def test_call与call_with_meta文本一致():
    c = _client_with({'deepseek': _conn('deepseek', lambda p: _Resp(200, _ok_body("same")))})
    assert c.call("p") == c.call_with_meta("p")[0] == "same"


# ==================== 多线程 ====================

def test_多线程并发meta不串线():
    barrier = threading.Barrier(2, timeout=5)

    def handler(payload):
        barrier.wait()      # 保证两个调用在飞行中交叠
        tag = payload["messages"][0]["content"]
        return _Resp(200, _ok_body("t-" + tag, "model-" + tag))

    c = _client_with({'deepseek': _conn('deepseek', handler)})
    out = {}

    def run(tag):
        out[tag] = c.call_with_meta(tag)

    ts = [threading.Thread(target=run, args=(t,)) for t in ("A", "B")]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    for tag in ("A", "B"):
        text, meta = out[tag]
        assert text == "t-" + tag
        assert meta.served_model == "model-" + tag


# ==================== 各 provider 的 served_model ====================

def test_openai兼容_响应无model字段则served_model为None():
    c = _client_with({'deepseek': _conn('deepseek', lambda p: _Resp(200, {
        "choices": [{"message": {"content": "x"}}]}))})
    assert c.call_with_meta("p")[1].served_model is None


def test_gemini_取model_version():
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = SimpleNamespace(
        text="g", model_version="gemini-3.5-flash-001")
    c = _client_with({'gemini': {'client': fake_client, 'model': 'gemini-3.5-flash',
                                 'provider': 'gemini'}}, provider="gemini")
    text, meta = c.call_with_meta("p")
    assert text == "g"
    assert (meta.provider, meta.served_model) == ("gemini", "gemini-3.5-flash-001")


def test_gemini_无model_version或非字符串则None():
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = SimpleNamespace(text="g")
    c = _client_with({'gemini': {'client': fake_client, 'model': 'm', 'provider': 'gemini'}},
                     provider="gemini")
    assert c.call_with_meta("p")[1].served_model is None
    fake_client.models.generate_content.return_value = MagicMock(text="g")  # model_version 是 MagicMock
    assert c.call_with_meta("p")[1].served_model is None


_AGENT_CONN = {'cli_path': '/usr/bin/claude', 'model': 'sonnet', 'provider': 'claude-agent'}


def _agent_envelope(**over):
    e = {"is_error": False, "stop_reason": "end_turn", "terminal_reason": "completed",
         "api_error_status": None, "result": "hello", "usage": {"output_tokens": 50},
         "modelUsage": {
             "claude-haiku-4-5-20251001": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.0001},
             "claude-sonnet-5-5": {"inputTokens": 100, "outputTokens": 45, "costUSD": 0.01},
         }}
    e.update(over)
    return e


def _agent_client(**kw):
    return _client_with({'claude-agent': dict(_AGENT_CONN)}, provider="claude-agent", **kw)


def _run_agent(client, envelope, json_mode=False, **call_kw):
    proc = MagicMock(returncode=0, stdout=json.dumps(envelope), stderr='')
    with patch('src.scholar.llm_client.subprocess.run', return_value=proc), \
         patch('src.scholar.llm_client.time.sleep'):
        return client.call_with_meta("p", json_mode=json_mode, **call_kw)


def test_claude_agent_取modelUsage中输出token最多的模型():
    text, meta = _run_agent(_agent_client(), _agent_envelope())
    assert text == "hello"
    assert (meta.provider, meta.served_model) == ("claude-agent", "claude-sonnet-5-5")


def test_claude_agent_信封无modelUsage则None():
    env = _agent_envelope()
    del env["modelUsage"]
    assert _run_agent(_agent_client(), env)[1].served_model is None


@pytest.mark.parametrize("usage", [None, {}, [], "x", {"m": "bad"}, {"": {"outputTokens": 3}}])
def test_modelUsage畸形结构不抛(usage):
    env = {"modelUsage": usage}
    r = _claude_envelope_served_model(env)
    assert r in (None, "m")     # {"m": "bad"} 无 token 信息时仍可取唯一键


def test_modelUsage下划线写法兼容():
    env = {"modelUsage": {"a": {"output_tokens": 1}, "b": {"output_tokens": 9}}}
    assert _claude_envelope_served_model(env) == "b"


# ==================== 降级半截正文 ====================

_TRUNCATED = '[{"id": 1, "decision": "INCLUDE", "one_line": "可用"}, {"id": 2, "decis'


def test_降级半截正文_degraded_partial为真且保留served_model():
    c = _agent_client()
    text, meta = _run_agent(c, _agent_envelope(result=_TRUNCATED), json_mode=True, max_retries=1)
    assert text == _TRUNCATED
    assert meta.degraded_partial is True
    assert meta.served_model == "claude-sonnet-5-5"


def test_完整响应degraded_partial为假():
    good = '[{"id": 1, "decision": "INCLUDE"}]'
    _, meta = _run_agent(_agent_client(), _agent_envelope(result=good), json_mode=True)
    assert meta.degraded_partial is False


def test_上一次降级标记不泄漏到下一次调用():
    c = _agent_client()
    _run_agent(c, _agent_envelope(result=_TRUNCATED), json_mode=True, max_retries=1)
    good = '[{"id": 1, "decision": "INCLUDE"}]'
    _, meta = _run_agent(c, _agent_envelope(result=good), json_mode=True)
    assert meta.degraded_partial is False
