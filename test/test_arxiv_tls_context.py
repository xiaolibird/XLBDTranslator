"""ipv4_client 的 SSL 上下文：开启 post_handshake_auth（与 http.client/urllib 的 ClientHello 一致）。

注意这不是 arXiv 406 的根治——406 更像 CDN 按「IP + TLS 指纹」分桶限流的罚期，根治靠
fulltext._arxiv_throttle（见 test_arxiv_query_and_pacing.py）。这里只离线断言「上下文长什么样、
有没有被真正传进 transport、校验有没有被放松」，不打网络。
"""
import ssl

import certifi
import httpx
import pytest

from src.scholar import academic_search, fulltext


def test_tls_context_enables_pha_and_keeps_verification():
    ctx = fulltext.tls_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.post_handshake_auth is True
    # 开 PHA 不能以放松校验为代价
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_tls_context_fallback_without_public_create_ssl_context(monkeypatch):
    """httpx < 0.28 没有公开的 create_ssl_context：走手工复刻分支，PHA 照样要开。"""
    monkeypatch.delattr(httpx, "create_ssl_context", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    ctx = fulltext.tls_context()
    assert ctx.post_handshake_auth is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_tls_context_fallback_honours_ssl_cert_file(monkeypatch):
    """回退分支要像 httpx 一样尊重 SSL_CERT_FILE（走 MITM 代理时的自定义根证书）。"""
    monkeypatch.delattr(httpx, "create_ssl_context", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    seen = {}
    real = ssl.create_default_context

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(fulltext.ssl, "create_default_context", spy)
    ctx = fulltext.tls_context()
    assert seen.get("cafile") == certifi.where()
    assert ctx.post_handshake_auth is True


def test_ipv4_client_passes_tls_context_into_transport(monkeypatch):
    """ipv4_client 必须把 tls_context() 真正交给 transport——Client(verify=…) 在自定义
    transport 时会被忽略，只有传给 HTTPTransport 才生效。"""
    marker = fulltext.tls_context()
    monkeypatch.setattr(fulltext, "tls_context", lambda: marker)
    captured = {}
    real_transport = httpx.HTTPTransport

    def recording_transport(*a, **kw):
        captured.update(kw)
        return real_transport(*a, **kw)

    monkeypatch.setattr(fulltext.httpx, "HTTPTransport", recording_transport)
    with fulltext.ipv4_client(timeout=5):
        pass
    assert captured.get("verify") is marker
    assert captured.get("local_address") == "0.0.0.0"   # 强制 IPv4 不能丢


def test_academic_search_client_uses_ipv4_client(monkeypatch):
    """AcademicSearchClient 不得再自建 httpx.Client（曾因此漏掉 TLS 修复）。"""
    calls = []
    real = fulltext.ipv4_client

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(academic_search, "ipv4_client", spy)
    with academic_search.AcademicSearchClient(timeout=7, tool="t-tool") as c:
        assert c._client.headers["User-Agent"].startswith("t-tool (")
        assert c._client.follow_redirects is True
    assert calls and calls[0]["timeout"] == 7


@pytest.mark.skipif(
    not hasattr(ssl.SSLContext, "post_handshake_auth"),
    reason="该 Python/OpenSSL 不支持 PHA 属性",
)
def test_live_client_context_really_has_pha():
    """直接看 httpcore 连接池里实际使用的上下文（内部属性，路径变了就改这里）。"""
    with fulltext.ipv4_client(timeout=5) as c:
        pool = getattr(c._transport, "_pool", None)
        ctx = getattr(pool, "_ssl_context", None)
        if ctx is None:
            pytest.skip("httpcore 内部结构已变，无法取到 ssl_context")
        assert ctx.post_handshake_auth is True
