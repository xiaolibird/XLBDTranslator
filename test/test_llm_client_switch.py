# -*- coding: utf-8 -*-
"""LLMClient 三个 bug 的回归测试。

覆盖：
1. _is_switchable 对数字状态码整词匹配（不误判 request id / token 计数里的
   数字子串），真实 401/402/403 仍能触发。
2. call() 对 gemini/openai 两条分支的 None 内容不再静默返回，改为抛出带
   明确原因的异常；且该异常不会被 _is_switchable 误判为可切换致命错误。
3. _call_agent 使用调用方传入的 conn，而非并发场景下可能已被
   _advance_chain 重建成别家 provider 的 self.conn。
4. _advance_chain 的「失败者是否仍是当前 conn」守卫：并发多线程对同一个
   已失效 provider 报错时，只有第一个线程真正推进链指针，其余线程只是被
   告知「已切换，换新 conn 重试」，不会重复推进把链烧穿。

全部 mock，不发真实请求、不涉及真实密钥。
"""
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.scholar.llm_client import LLMClient
from src.scholar.schema import LLMSettings


def _settings(**overrides) -> LLMSettings:
    base = dict(provider="deepseek", model="fake-model")
    base.update(overrides)
    return LLMSettings(**base)


# ==================== (a) _is_switchable 整词匹配 ====================

def test_is_switchable_ignores_request_id_substring():
    """异常消息里恰好出现 req-a402fb99：数字前后都是 \\w，不应整词命中 402"""
    client = LLMClient(_settings())
    exc = Exception("upstream error, trace id=req-a402fb99, please retry")
    assert client._is_switchable(exc) is False


def test_is_switchable_ignores_token_count_substring():
    """异常消息里恰好出现 "4021 tokens"：402 后面紧跟数字 1，不构成整词边界"""
    client = LLMClient(_settings())
    exc = Exception("response truncated at 4021 tokens, please shorten input")
    assert client._is_switchable(exc) is False


@pytest.mark.parametrize("code", [401, 402, 403])
def test_is_switchable_matches_real_status_code(code):
    """真实的 401/402/403（整词、前后是空格/括号）仍须判定为可切换"""
    client = LLMClient(_settings())
    exc = Exception("HTTP {} error: authentication failed".format(code))
    assert client._is_switchable(exc) is True


def test_is_switchable_matches_fatal_phrase():
    """非数字的致命短语（如 'insufficient balance'）保持原有行为不受影响"""
    client = LLMClient(_settings())
    exc = Exception("DeepSeek API error: insufficient balance")
    assert client._is_switchable(exc) is True


# ==================== (b) None 内容抛异常而非返回 None ====================

def test_gemini_none_text_raises_instead_of_returning_none():
    """response.text 在安全拦截/MAX_TOKENS 时为 None：必须抛异常，不能返回 None"""
    client = LLMClient(_settings())

    dummy_response = MagicMock()
    dummy_response.text = None
    dummy_response.candidates = []  # 触发 candidates[0] 的 IndexError，被内部吞掉

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = dummy_response

    conn = {'client': mock_client, 'model': 'gemini-3.5-flash', 'provider': 'gemini'}

    with pytest.raises(RuntimeError) as exc_info:
        client._call_once(conn, "prompt", None, None, None, False, max_retries=1)

    msg = str(exc_info.value)
    assert "为空" in msg
    # 安全拦截不应被误判为可切换致命错误（消息里不含 401/402/403/quota 等关键词）
    assert client._is_switchable(exc_info.value) is False


def test_openai_none_content_raises_instead_of_returning_none():
    """OpenAI 兼容 message.content 为 None（如命中内容过滤）时必须抛异常"""
    client = LLMClient(_settings())

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        'choices': [{'message': {'content': None}, 'finish_reason': 'content_filter'}]
    }

    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp

    conn = {
        'client': mock_client, 'base_url': 'http://fake', 'api_key': 'k',
        'model': 'fake-model', 'provider': 'openai-compatible',
    }

    with pytest.raises(RuntimeError) as exc_info:
        client._call_once(conn, "prompt", None, None, None, False, max_retries=1)

    msg = str(exc_info.value)
    assert "为空" in msg
    assert client._is_switchable(exc_info.value) is False


# ==================== (c) _call_agent 使用传入 conn ====================

def test_call_agent_uses_passed_conn_not_self_conn():
    """并发下 self.conn 可能已被另一线程重建成别家 provider；_call_agent 必须
    使用调用方传入的那份 conn，而不是重新读 self.conn。"""
    client = LLMClient(_settings(provider="claude-agent"))

    conn_passed_in = {'cli_path': '/usr/bin/correct-claude-cli', 'model': 'sonnet',
                       'provider': 'claude-agent'}
    # 模拟另一线程已经把 self._conn 重建成了别家 provider 的连接
    conn_wrong = {'client': MagicMock(), 'base_url': 'http://other', 'api_key': 'x',
                  'model': 'other-model', 'provider': 'openai-compatible'}
    client._conn = conn_wrong

    fake_envelope = '{"is_error": false, "result": "ok"}'
    fake_proc = MagicMock(returncode=0, stdout=fake_envelope, stderr='')

    with patch('src.scholar.llm_client.subprocess.run', return_value=fake_proc) as mock_run:
        result = client._call_agent(conn_passed_in, "prompt", "sonnet", False)

    assert result == "ok"
    called_cmd = mock_run.call_args[0][0]
    assert called_cmd[0] == '/usr/bin/correct-claude-cli'
    # 确认没有误用重建后的 conn_wrong（那份连接根本没有 cli_path，一旦误用
    # 就会在构造 cmd 时 KeyError）
    assert called_cmd[0] != conn_wrong.get('client')


# ==================== (d) _advance_chain 的「失败者是否仍是当前 conn」守卫 ====================

def test_advance_chain_second_caller_does_not_re_advance():
    """模拟并发：两个线程用同一份（已失效的）conn 几乎同时报错。

    第一个线程的 _advance_chain 应真正推进链指针（idx 0→1）；第二个线程
    传入的仍是那份旧 conn，此时 self._conn 已被第一个线程清空/重建，二者
    不再是同一对象 —— 应直接 return True（告知“已切换，换新 conn 重试”），
    不应再把指针推到 2。缺这道守卫时会一次失败烧穿整条三节点链。
    """
    client = LLMClient(_settings(provider="deepseek", fallback_providers="claude-agent,gemini"))
    assert client._chain == ["deepseek", "claude-agent", "gemini"]

    stale_conn = {'client': MagicMock(), 'base_url': 'http://fake', 'api_key': 'k',
                  'model': 'deepseek-v4-flash', 'provider': 'openai-compatible', 'name': 'deepseek'}
    client._conn = stale_conn
    client._chain_idx = 0

    # 线程 1：失败者与当前 conn 一致 —— 真正推进
    switched_1 = client._advance_chain(stale_conn, "402 payment required")
    assert switched_1 is True
    assert client._chain_idx == 1
    assert client._conn is None

    # 线程 2：仍拿着同一份 stale_conn 报错，但 self._conn 已经被线程 1 清空，
    # 说明“别的线程已经切过了”——不应再把指针推进到 2
    switched_2 = client._advance_chain(stale_conn, "402 payment required")
    assert switched_2 is True
    assert client._chain_idx == 1  # 关键断言：没有被线程 2 重复推进


def test_advance_chain_concurrent_threads_only_advance_once():
    """更贴近真实场景的线程级验证：4 个线程各自持有同一份失效 conn 并发调用
    _advance_chain，链指针总共只前进一步（而不是烧穿到链尾/越界）。"""
    client = LLMClient(_settings(provider="deepseek", fallback_providers="claude-agent,gemini"))
    stale_conn = {'client': MagicMock(), 'base_url': 'http://fake', 'api_key': 'k',
                  'model': 'deepseek-v4-flash', 'provider': 'openai-compatible', 'name': 'deepseek'}
    client._conn = stale_conn
    client._chain_idx = 0

    barrier = threading.Barrier(4)
    results = []

    def worker():
        barrier.wait()
        results.append(client._advance_chain(stale_conn, "402"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results)  # 每个线程都被告知「可以重试」
    assert client._chain_idx == 1  # 但链指针只前进了一次，未被烧穿


# ==================== (e) 内容层拒答不粘性切链 ====================
#
# 实测现场：221 篇精读回填里，一篇讲医疗 AI 安全的论文（正文含越狱样例）让
# claude CLI 回了 AUP 拒答，_advance_chain 把整个 client 永久推到本地 ollama，
# 同一批后续 28 篇全部陪葬——那 28 篇本身毫无问题，换个进程重跑全过。

_AUP = ("claude CLI 调用失败: API Error: Sonnet 5 can't help with this. "
        "Start a new session to continue.\n\nLearn more: https://www.anthropic.com/legal/aup")


def test_内容拒答被识别为内容层而非故障():
    client = LLMClient(_settings())
    assert client._is_content_refusal(RuntimeError(_AUP)) is True
    # 真·故障不该被误判成内容拒答，否则会把该粘的切换也拨回去
    assert client._is_content_refusal(Exception("402 payment required")) is False
    assert client._is_content_refusal(Exception("quota exhausted")) is False


def test_内容拒答仍然换下家接手():
    """拒答不等于放弃——尊重过滤器裁决、换下家读，只是不判定这家坏了。"""
    client = LLMClient(_settings(provider="claude-agent", fallback_providers="gemini"))
    assert client._is_switchable(RuntimeError(_AUP)) is True


def test_内容拒答切换不计入粘性代际():
    client = LLMClient(_settings(provider="claude-agent", fallback_providers="gemini"))
    conn = {'cli_path': '/x/claude', 'model': 'sonnet', 'provider': 'claude-agent',
            'name': 'claude-agent'}
    client._conn = conn
    gen0 = client._sticky_gen

    assert client._advance_chain(conn, _AUP, sticky=False) is True
    assert client._chain_idx == 1
    assert client._sticky_gen == gen0          # 关键：没记成「这家坏了」

    client._restore_chain(0, gen0)
    assert client._chain_idx == 0              # 拨回主 provider


def test_真故障切换是粘性的_不会被拨回():
    """欠费/限流是真坏了，后续调用不该再撞一次。"""
    client = LLMClient(_settings(provider="claude-agent", fallback_providers="gemini"))
    conn = {'cli_path': '/x/claude', 'model': 'sonnet', 'provider': 'claude-agent',
            'name': 'claude-agent'}
    client._conn = conn
    gen0 = client._sticky_gen

    assert client._advance_chain(conn, "402 payment required", sticky=True) is True
    assert client._sticky_gen == gen0 + 1

    client._restore_chain(0, gen0)             # 拿旧代际来拨——应被拒绝
    assert client._chain_idx == 1


def test_期间发生真故障时不撤销别人的合理推进():
    """线程 A 因内容拒答临时切走，线程 B 期间因 402 合理推进；A 收尾不该把 B 撤销。"""
    client = LLMClient(_settings(provider="claude-agent",
                                 fallback_providers="gemini,deepseek"))
    conn = {'cli_path': '/x/claude', 'model': 'sonnet', 'provider': 'claude-agent',
            'name': 'claude-agent'}
    client._conn = conn
    base_idx, base_gen = client._chain_idx, client._sticky_gen

    client._advance_chain(conn, _AUP, sticky=False)        # A：临时切到 gemini
    client._conn = {'client': MagicMock(), 'model': 'g', 'provider': 'gemini', 'name': 'gemini'}
    client._advance_chain(client._conn, "402", sticky=True)  # B：gemini 真欠费

    client._restore_chain(base_idx, base_gen)
    assert client._chain_idx == 2              # 停在 deepseek，没被拨回 claude-agent


# ==================== (f) 回退链的 provider:model 写法 ====================

def test_回退链支持同一家换模型():
    """`claude-agent:opus` 让 sonnet 挂了由 opus 接手，不必绕道做不了活的 ollama。"""
    client = LLMClient(_settings(provider="claude-agent",
                                 fallback_providers="claude-agent:opus,gemini"))
    assert client._chain == ["claude-agent", "claude-agent", "gemini"]
    assert client._chain_models == [None, "opus", None]


def test_同名不同模型不被去重吃掉():
    """去重按 (provider, model) 对；否则 claude-agent:opus 会被主链判为重复而丢失。"""
    client = LLMClient(_settings(provider="claude-agent",
                                 fallback_providers="claude-agent,claude-agent:opus,claude-agent:opus"))
    assert client._chain == ["claude-agent", "claude-agent"]
    assert client._chain_models == [None, "opus"]


def test_链上模型覆写真的落到_conn():
    client = LLMClient(_settings(provider="claude-agent", model="sonnet",
                                 fallback_providers="claude-agent:opus"))
    client._chain_idx = 1
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        conn = client._create_from_chain()
    assert conn['model'] == "opus"
    assert conn['provider'] == "claude-agent"


def test_无冒号写法保持原有行为():
    client = LLMClient(_settings(provider="deepseek", fallback_providers="claude-agent, gemini"))
    assert client._chain == ["deepseek", "claude-agent", "gemini"]
    assert client._chain_models == [None, None, None]
