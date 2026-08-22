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


# ==================== (e) claude CLI 截断检测 ====================
# 2026-08-20：08-17 那轮 digest 有 15 篇（翻译，3 批 × 5）与 40 篇（filter，2 批 × 20）
# 因响应被截断而 JSON 解析失败。关键事实（实测 CLI 2.1.221 + 反推）：
# **那种截断的信封是干净的** —— is_error=false / terminal_reason=completed /
# api_error_status=null / stop_reason=end_turn，只有正文是半截的。所以判据必须
# 校验**正文**；只看信封字段一条都够不着。
# 反证：若信封当时带 is_error=true，第一批就会切链走掉（链非空），不可能出现
# 5 个批次全部重演同一形状、且 digest 正常收尾。

_AGENT_CONN = {'cli_path': '/usr/bin/claude', 'model': 'sonnet', 'provider': 'claude-agent'}

# 生产真实形状：干净信封 + 半截正文
_TRUNCATED = '[{"id": 1, "decision": "INCLUDE", "one_line": "可用"}, {"id": 2, "decis'
_CLEAN_ENVELOPE = {
    "is_error": False, "stop_reason": "end_turn", "terminal_reason": "completed",
    "api_error_status": None, "usage": {"output_tokens": 1503},
}


def _envelope(**over):
    e = dict(_CLEAN_ENVELOPE)
    e.update(over)
    return e


def _agent_run(client, envelope: dict, returncode: int = 0, json_mode: bool = True):
    import json as _json
    fake_proc = MagicMock(returncode=returncode, stdout=_json.dumps(envelope), stderr='')
    with patch('src.scholar.llm_client.subprocess.run', return_value=fake_proc):
        return client._call_agent(_AGENT_CONN, "prompt", "sonnet", json_mode)


def test_生产真实形状_干净信封加半截正文_必须判截断():
    """最要命也最容易漏的一种：CLI 退出 0、信封每个字段都正常，只有 result 是半截。
    旧代码原样返回，下游 json.loads 在文末报 Expecting ','，错因在这里就丢了。"""
    from src.scholar.llm_client import _RetryableHTTP
    client = LLMClient(_settings(provider="claude-agent"))
    with pytest.raises(_RetryableHTTP) as exc_info:
        _agent_run(client, _envelope(result=_TRUNCATED))
    assert "截断" in str(exc_info.value)
    # 残料必须挂在异常上带出去，否则下游 loads_lenient 抢救层被饿死
    assert exc_info.value.partial == _TRUNCATED


def test_重试耗尽后把半截正文交还调用方而不是抛掉():
    """抢救层（workflow 的 filter/翻译、pdf_ingest 的通读块）只有拿到半截正文才有活干。
    在 llm_client 层抛掉 = filter 从「20 篇救回 13 篇真裁决」退化成整批 0 篇。"""
    import json as _json
    client = LLMClient(_settings(provider="claude-agent"))
    fake_proc = MagicMock(returncode=0, stdout=_json.dumps(_envelope(result=_TRUNCATED)), stderr='')
    with patch('src.scholar.llm_client.subprocess.run', return_value=fake_proc) as run, \
         patch('src.scholar.llm_client.time.sleep') as slp, \
         patch('shutil.which', return_value='/usr/bin/claude'):
        out = client._call_once(_AGENT_CONN, "p", "sonnet", None, None, True, max_retries=4)
    assert out == _TRUNCATED          # 交还残料，供下游抢救
    # 只重试 1 次，不吃满 max_retries=4：一次 claude -p 的成本几乎全是与 prompt
    # 无关的建仓税，重试摊薄不了；而 topics/qa 在它们那层还各有一次「追加格式
    # 指令再试」——那次才带新信息，吃满 4 次只会把它挤到第 5 次。
    assert run.call_count == 2
    assert slp.call_count == 1
    # 注意别在这里断言 _chain_idx：切链发生在 call() 里，_call_once 根本不碰链位，
    # 在这一层断言链位不变是空转。真正的 failover 行为见下面走 call() 的两条。


def test_截断重试成功后不返回残料():
    """第 2 次拿到完整 JSON 就该正常返回，不能把第 1 次的残料漏出去。"""
    import json as _json
    client = LLMClient(_settings(provider="claude-agent"))
    good = '[{"id": 1, "decision": "INCLUDE"}]'
    procs = [MagicMock(returncode=0, stdout=_json.dumps(_envelope(result=_TRUNCATED)), stderr=''),
             MagicMock(returncode=0, stdout=_json.dumps(_envelope(result=good)), stderr='')]
    with patch('src.scholar.llm_client.subprocess.run', side_effect=procs) as run, \
         patch('src.scholar.llm_client.time.sleep'):
        out = client._call_once(_AGENT_CONN, "p", "sonnet", None, None, True, max_retries=4)
    assert out == good
    assert run.call_count == 2


def test_非json模式不校验正文():
    """json_mode=False 的调用方（如自由文本综述）拿到的本来就不是 JSON，
    对它做完整性校验会把每一次正常调用都判成截断。"""
    client = LLMClient(_settings(provider="claude-agent"))
    assert _agent_run(client, _envelope(result="这是一段自由文本，没有括号。"),
                      json_mode=False) == "这是一段自由文本，没有括号。"


def test_模型加了开场白但json完好_不得误判():
    """topics.py 的解析会从解释文字里抠出 JSON。若这里严格判定，
    这类响应会被误判成截断、白白重试 4 次并烧 4 份订阅额度。"""
    client = LLMClient(_settings(provider="claude-agent"))
    body = '好的，结果如下：\n[{"id": 1, "decision": "INCLUDE"}]\n以上。'
    assert _agent_run(client, _envelope(result=body)) == body


def test_确定性故障一次即抛不重试():
    """prompt_too_long / max_turns / budget_exhausted 这类重试 4 次必然 4 次重演。
    每次都要重付一整份 system prompt 建仓税（烧订阅额度），耗尽后还要切链把
    下家也照样烧一遍 —— 一次失败 = 8 次完整生成。"""
    import json as _json
    client = LLMClient(_settings(provider="claude-agent"))
    env = {"is_error": True, "result": None, "errors": ["Prompt is too long"],
           "terminal_reason": "prompt_too_long", "subtype": "error_during_execution"}
    fake_proc = MagicMock(returncode=1, stdout=_json.dumps(env), stderr='')
    with patch('src.scholar.llm_client.subprocess.run', return_value=fake_proc) as run, \
         patch('src.scholar.llm_client.time.sleep') as slp:
        with pytest.raises(RuntimeError):
            client._call_once(_AGENT_CONN, "p", "sonnet", None, None, True, max_retries=4)
    assert run.call_count == 1, "确定性故障不该重试"
    assert slp.call_count == 0


def test_错误信封没有result字段时改用errors数组():
    """subtype=error_* 的信封只有 errors[]、没有 result。只取 result 会让 msg
    恒为空串，关键词分类与 _is_content_refusal 全部落空，错因彻底丢失。"""
    import json as _json
    client = LLMClient(_settings(provider="claude-agent"))
    env = {"is_error": True, "result": None, "errors": ["Usage limit reached"],
           "terminal_reason": "blocking_limit"}
    fake_proc = MagicMock(returncode=1, stdout=_json.dumps(env), stderr='')
    from src.scholar.llm_client import _RetryableHTTP
    with patch('src.scholar.llm_client.subprocess.run', return_value=fake_proc):
        with pytest.raises(_RetryableHTTP) as exc_info:
            client._call_agent(_AGENT_CONN, "p", "sonnet", True)
    assert "Usage limit reached" in str(exc_info.value)


def test_瞬时故障走可重试():
    from src.scholar.llm_client import _RetryableHTTP
    client = LLMClient(_settings(provider="claude-agent"))
    with pytest.raises(_RetryableHTTP):
        _agent_run(client, {"is_error": True, "result": "Connection closed mid-response",
                            "terminal_reason": "api_error"}, returncode=1)


def test_tool_use是成功值不得当成失败():
    """--json-schema 走强制工具调用实现，成功时 stop_reason 恰是 'tool_use'（实测信封）。
    拦它会把每一次成功的结构化调用都判成失败。"""
    client = LLMClient(_settings(provider="claude-agent"))
    assert _agent_run(client, _envelope(result='{"a": 1}', stop_reason="tool_use")) == '{"a": 1}'


def test_tool_deferred是is_error为假的成功变体():
    """CLI 显式以 is_error=false 产出这两个变体，当故障处理会误伤。"""
    client = LLMClient(_settings(provider="claude-agent"))
    assert _agent_run(client, _envelope(result='{"a": 1}',
                                        terminal_reason="tool_deferred")) == '{"a": 1}'


def test_老信封无这些字段时行为不变():
    """历史/精简信封不带 stop_reason/terminal_reason，不得因缺字段被判故障。"""
    client = LLMClient(_settings(provider="claude-agent"))
    assert _agent_run(client, {"is_error": False, "result": "ok"}, json_mode=False) == "ok"


def test_确定性4xx不重试():
    """CLI 把上游 4xx 也标成 terminal_reason=api_error。实测 --json-schema 传了
    非对象根 schema 时就是这个形状——确定性请求错误，重试多少次都一样。"""
    import json as _json
    client = LLMClient(_settings(provider="claude-agent"))
    env = {"is_error": True, "terminal_reason": "api_error", "subtype": "success",
           "result": "API Error: 400 tools.8.custom.input_schema.type: Input should be 'object'"}
    fake_proc = MagicMock(returncode=0, stdout=_json.dumps(env), stderr='')
    with patch('src.scholar.llm_client.subprocess.run', return_value=fake_proc) as run, \
         patch('src.scholar.llm_client.time.sleep') as slp:
        with pytest.raises(RuntimeError):
            client._call_once(_AGENT_CONN, "p", "sonnet", None, None, True, max_retries=4)
    assert run.call_count == 1
    assert slp.call_count == 0


def test_429仍走重试():
    """限流是瞬时的，不能被 4xx 规则一刀切掉。"""
    from src.scholar.llm_client import _RetryableHTTP
    client = LLMClient(_settings(provider="claude-agent"))
    with pytest.raises(_RetryableHTTP):
        _agent_run(client, {"is_error": True, "terminal_reason": "api_error",
                            "result": "API Error: 429 rate limit exceeded"}, returncode=0)


# ==================== (f) 降级返回的健康记账（走 call()，不是 _call_once） ====================
# 切链只发生在 call() 里；_call_once 不碰链位，在那一层断言 failover 是空转。

def _call_with(client, envelope, n=1):
    """走完整 call() 路径跑 n 次，返回 (结果列表, subprocess 调用次数)。"""
    import json as _json
    proc = MagicMock(returncode=0, stdout=_json.dumps(envelope), stderr='')
    outs = []
    with patch('src.scholar.llm_client.subprocess.run', return_value=proc) as run, \
         patch('src.scholar.llm_client.time.sleep'), \
         patch('shutil.which', return_value='/usr/bin/claude'):
        for _ in range(n):
            try:
                outs.append(('OK', client.call("p", json_mode=True)))
            except Exception as e:
                outs.append(('RAISE', type(e).__name__))
    return outs, run.call_count


def test_持续吐半截的provider最终必须被切走():
    """`return partial` 会绕过 call() 里的 except → _is_switchable → _advance_chain
    整段。没有连击记账，一个持续只吐半截的 provider **永远切不掉**：每次都"成功"、
    链位纹丝不动，下家一次请求都发不出去——而它本来能给出完整响应。"""
    client = LLMClient(_settings(provider="claude-agent",
                                 fallback_providers="claude-agent:opus"))
    outs, _ = _call_with(client, _envelope(result=_TRUNCATED), n=4)
    kinds = [k for k, _ in outs]
    # 前 N-1 次降级返回残料（下游抢救层照常有活干），第 N 次判定 provider 降级并切链
    assert kinds[:2] == ['OK', 'OK'], kinds
    assert client._chain_idx > 0, "连续降级后必须切链，否则下家永远拿不到请求"


def test_拿到完整响应后降级连击清零():
    """偶发一次半截不该累积成"provider 已降级"。"""
    import json as _json
    client = LLMClient(_settings(provider="claude-agent"))
    good = '[{"id": 1, "decision": "INCLUDE"}]'
    seq = [_envelope(result=_TRUNCATED), _envelope(result=_TRUNCATED),  # 一次调用=2 次子进程
           _envelope(result=good)]
    procs = [MagicMock(returncode=0, stdout=_json.dumps(e), stderr='') for e in seq]
    with patch('src.scholar.llm_client.subprocess.run', side_effect=procs), \
         patch('src.scholar.llm_client.time.sleep'), \
         patch('shutil.which', return_value='/usr/bin/claude'):
        client.call("p", json_mode=True)      # 降级一次 → streak=1
        assert client._degraded_streak == 1
        client.call("p", json_mode=True)      # 完整 → 清零
    assert client._degraded_streak == 0


def test_纯散文反问不得冒充半截正文交给抢救层():
    """实测形状：模型没输出 JSON，而是反问「这个请求缺少主题——请告知后我再输出
    JSON」。它非空却一个字段都救不出，交下去只是把失败伪装成成功，而且因为走了
    `return` 路径，provider 永远不会被切走。"""
    client = LLMClient(_settings(provider="claude-agent"))
    prose = '这个请求缺少主题——40 条 title/one_line 需要围绕什么内容？请告知主题，我再按格式仅输出 JSON。'
    outs, _ = _call_with(client, _envelope(result=prose), n=1)
    assert outs[0][0] == 'RAISE', "散文反问必须抛，不能当残料返回"


# ==================== (g) 判据倒挂回归 ====================

def test_无开场白的完好json加尾随说明_不得判成截断():
    """判据曾经倒挂：`'[{...}]\\n以上是 2 条裁决。'` 判成截断（白重试 + 打假警报），
    而同样内容**加一句开场白**反而判成完整——加句废话就能让成本减半。
    现在判的是「括号栈闭没闭合」，两者都该是完整。"""
    client = LLMClient(_settings(provider="claude-agent"))
    body = '[{"id": 1, "decision": "INCLUDE"}]\n\n以上是 1 条裁决。'
    import json as _json
    proc = MagicMock(returncode=0, stdout=_json.dumps(_envelope(result=body)), stderr='')
    with patch('src.scholar.llm_client.subprocess.run', return_value=proc) as run, \
         patch('shutil.which', return_value='/usr/bin/claude'):
        assert client.call("p", json_mode=True) == body
    assert run.call_count == 1, "不该重试"


# ==================== (h) 确定性故障分类 ====================

@pytest.mark.parametrize("term,msg", [
    ("blocking_limit", "Prompt is too long"),        # ptl = prompt too long 的另一条出口
    ("rapid_refill_breaker", "rapid-refill breaker tripped"),
    ("api_error", "Credit balance is too low"),      # api_error 是混合桶
    ("api_error", "Not logged in · Please run /login"),
    ("api_error", "Invalid API key · Please run /login"),
])
def test_确定性故障只调一次(term, msg):
    """这些重试 4 次必然 4 次重演。更要命的是 claude-agent:opus 与 sonnet
    **共用同一个订阅额度池，不是独立故障域**——额度撞墙时 opus 那 4 次必然
    同样失败，等于在空账号上再烧 8 份建仓税。"""
    import json as _json
    client = LLMClient(_settings(provider="claude-agent"))
    env = {"is_error": True, "result": msg, "terminal_reason": term}
    proc = MagicMock(returncode=1, stdout=_json.dumps(env), stderr='')
    with patch('src.scholar.llm_client.subprocess.run', return_value=proc) as run, \
         patch('src.scholar.llm_client.time.sleep') as slp:
        with pytest.raises(RuntimeError):
            client._call_once(_AGENT_CONN, "p", "sonnet", None, None, True, max_retries=4)
    assert run.call_count == 1
    assert slp.call_count == 0


def test_确定性收尾但正文完整时照常采用():
    """确定性收尾时重试拿不到更好的正文，这是它唯一的机会，整份丢弃再切链是纯亏。

    必须断言那行 warning：只断言返回值是**非鉴别性**的——把整个分支删掉，正文会
    直接落到下面的完整性校验、照样返回同一个 body，测试仍然全绿。
    （另：别拿 max_turns 当例子，它恒配 is_error=true，走不到这个分支。）
    """
    client = LLMClient(_settings(provider="claude-agent"))
    body = '{"a": 1}'
    with patch('src.scholar.llm_client.logger.warning') as warn:
        assert _agent_run(client, _envelope(result=body,
                                            terminal_reason="hook_stopped")) == body
    assert any("hook_stopped" in str(c) for c in warn.call_args_list), \
        "确定性收尾分支必须留下痕迹，否则这条测试测不到分支在不在"


def test_空正文必须抛而不是静默返回空串():
    """gemini/openai 两条分支都有「内容为空 → 抛带 finish_reason 的异常」，
    只有这条链路一直没有。实测可达：挂 block 型 Stop hook 跑 headless，CLI 会
    空转到 10 轮后以 is_error=false / terminal_reason=completed / result='' 收尾。
    受害者是 deep_research（json_mode=False，无条件把返回值当报告落盘）。"""
    from src.scholar.llm_client import _RetryableHTTP
    client = LLMClient(_settings(provider="claude-agent"))
    with pytest.raises(_RetryableHTTP) as exc_info:
        _agent_run(client, _envelope(result=''), json_mode=False)
    assert "为空" in str(exc_info.value)
    # 空串抢救层救不出东西，不该挂 partial 冒充残料
    assert not getattr(exc_info.value, 'partial', '')


def test_切链时降级连击必须清零():
    """降级连击是**这一家**的案底。不清零的话下一棒（claude-agent:opus）一上任
    就带着前一棒的记录、降级额度为 0，一次故障直接烧到链尾。"""
    client = LLMClient(_settings(provider="claude-agent",
                                 fallback_providers="claude-agent:opus"))
    client._degraded_streak = 2
    with patch('shutil.which', return_value='/usr/bin/claude'):
        conn = client.conn
        assert client._advance_chain(conn, "模拟故障") is True
    assert client._chain_idx == 1
    assert client._degraded_streak == 0


# ==================== (e) rewind_chain 的边界与语义 ====================

def test_rewind_chain_noop_at_head():
    """链位本就在链首：不动、返回 False（别让调用方以为发生了切换）。"""
    client = LLMClient(_settings())
    assert client._chain_idx == 0
    assert client.rewind_chain() is False
    assert client._chain_idx == 0


def test_rewind_chain_returns_to_head():
    """粘性切走之后 rewind 回链首，后续调用重新从主 provider 起。"""
    client = LLMClient(_settings(fallback_providers="gemini"))
    client._chain_idx = 1
    assert client.rewind_chain() is True
    assert client._chain_idx == 0


def test_rewind_chain_survives_out_of_range_index():
    """**链位停在越界位**时 rewind 不得 IndexError。

    真实来源：`_create_from_chain` 在全部 provider 构造失败时，是先把 `_chain_idx`
    推到 `len(_chain)` 再抛 RuntimeError 的——指针就停在越界位。此处若裸取下标，
    异常会穿透到 workflow 的末尾重试（那里拿 rewind 当基础设施用），把「本来还能
    逐批兜底出 digest」变成整轮零产出且一条兜底记录都没有，比不做这次 rewind 更差。
    """
    client = LLMClient(_settings(fallback_providers="gemini"))
    client._chain_idx = len(client._chain)      # 模拟构造全失败后的越界位
    assert client.rewind_chain() is True        # 不抛
    assert client._chain_idx == 0
