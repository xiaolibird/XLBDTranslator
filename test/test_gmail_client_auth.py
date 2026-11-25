# -*- coding: utf-8 -*-
"""Gmail OAuth 刷新：瞬时故障重试、刷新后写回、写回失败告警，全部离线桩。"""
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.scholar import gmail_client as GC


class _Creds:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.expired = True
        self.refresh_token = "refresh-token"
        self.valid = False

    def refresh(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.valid = True

    def to_json(self):
        return json.dumps({"token": "new-access-token", "expiry": "2026-11-22T10:00:00Z"})


def _client(tmp_path, monkeypatch, creds):
    token = tmp_path / "token.json"
    token.write_text("original-token", encoding="utf-8")
    oauth = tmp_path / "credentials.json"
    oauth.write_text("{}", encoding="utf-8")
    settings = SimpleNamespace(gmail=SimpleNamespace(
        credentials_path=oauth, token_path=token, scopes=["scope"]))
    monkeypatch.setattr(GC.Credentials, "from_authorized_user_file",
                        staticmethod(lambda *a, **k: creds))
    monkeypatch.setattr(GC, "Request", lambda: object())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(GC.time, "sleep", lambda _s: None)
    return GC.GmailClient(settings), token


def _no_browser(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("不应进入浏览器授权")
    monkeypatch.setattr(GC.InstalledAppFlow, "from_client_secrets_file", forbidden)


def test_refresh_retries_transient_ssl_and_persists_token(tmp_path, monkeypatch):
    creds = _Creds([
        OSError("[SSL: UNEXPECTED_EOF_WHILE_READING]"),
        TimeoutError("read timed out"),
        None,
    ])
    client, token = _client(tmp_path, monkeypatch, creds)
    _no_browser(monkeypatch)

    client._authenticate()

    assert client._creds is creds and creds.calls == 3
    assert json.loads(token.read_text(encoding="utf-8"))["token"] == "new-access-token"
    assert not (tmp_path / "token.json.tmp").exists()


def test_refresh_exhaustion_preserves_old_token_and_fails_headless(tmp_path, monkeypatch):
    creds = _Creds([OSError("SSL EOF"), OSError("SSL EOF"), OSError("SSL EOF")])
    client, token = _client(tmp_path, monkeypatch, creds)
    _no_browser(monkeypatch)

    with pytest.raises(RuntimeError, match="检查网络后重跑"):
        client._authenticate()

    assert creds.calls == 3
    assert token.read_text(encoding="utf-8") == "original-token"


def test_invalid_grant_is_not_retried(tmp_path, monkeypatch):
    creds = _Creds([RuntimeError("400 invalid_grant: Token has been revoked")])
    client, _token = _client(tmp_path, monkeypatch, creds)
    _no_browser(monkeypatch)

    with pytest.raises(RuntimeError, match="非交互环境"):
        client._authenticate()

    assert creds.calls == 1


def test_refresh_write_failure_notifies_but_digest_can_continue(tmp_path, monkeypatch):
    creds = _Creds([None])
    client, token = _client(tmp_path, monkeypatch, creds)
    _no_browser(monkeypatch)
    notified = []
    monkeypatch.setattr(GC, "notify", lambda title, text: notified.append((title, text)))

    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(GC.os, "replace", broken_replace)

    client._authenticate()

    assert client._creds is creds and creds.valid
    assert token.read_text(encoding="utf-8") == "original-token"
    assert len(notified) == 1 and "写回失败" in notified[0][1]


def test_transient_exhaustion_never_opens_browser_even_on_tty(tmp_path, monkeypatch):
    creds = _Creds([TimeoutError("network down")] * 3)
    client, _token = _client(tmp_path, monkeypatch, creds)
    _no_browser(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(RuntimeError, match="不代表需要重新授权"):
        client._authenticate()


def test_token_write_is_0600_and_concurrent_writers_never_share_tmp(tmp_path, monkeypatch):
    client, token = _client(tmp_path, monkeypatch, _Creds([]))
    token.chmod(0o600)
    errors = []

    class _StaticCreds:
        def __init__(self, value):
            self.value = value

        def to_json(self):
            return json.dumps({"token": self.value, "payload": "x" * 1000})

    def writer(i):
        try:
            client._save_credentials(_StaticCreds("token-{}".format(i)))
        except Exception as e:                       # noqa: BLE001 —— 收集线程真实失败
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert json.loads(token.read_text(encoding="utf-8"))["token"].startswith("token-")
    assert token.stat().st_mode & 0o777 == 0o600
    assert list(Path(tmp_path).glob("token.json.*.tmp")) == []
