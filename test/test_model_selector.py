from web.components.model_selector import ModelSelector


def _isolate_env(monkeypatch):
    # 防止本机 shell 导出的真实 key 让测试打真网
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def test_get_multimodal_models_returns_defaults_when_no_api_key(monkeypatch):
    _isolate_env(monkeypatch)
    ModelSelector.clear_cache()

    models = ModelSelector.get_multimodal_models(api_key=None)
    assert isinstance(models, list)
    assert len(models) >= 1

    # Check expected keys in model dict
    keys = {'name', 'display_name', 'capabilities', 'input_tokens', 'output_tokens'}
    assert keys.issubset(set(models[0].keys()))


def test_cache_behaviour(monkeypatch):
    _isolate_env(monkeypatch)
    ModelSelector.clear_cache()
    first = ModelSelector.get_multimodal_models()
    second = ModelSelector.get_multimodal_models()
    # Should return the cached list object (same id)
    assert first is second


def test_cache_invalidated_when_api_key_changes(monkeypatch):
    """UI 只在 provider 变化时 clear_cache；用户补填 key 后缓存必须自动失效，
    否则会一直拿到无 key 的默认列表"""
    _isolate_env(monkeypatch)
    ModelSelector.clear_cache()

    calls = []

    def fake_fetch(api_key=None):
        calls.append(api_key)
        return [{'name': f'model-for-{api_key}', 'display_name': 'x',
                 'capabilities': 'x', 'input_tokens': 1, 'output_tokens': 1}]

    monkeypatch.setattr(ModelSelector, '_get_gemini_models', staticmethod(fake_fetch))

    no_key = ModelSelector.get_multimodal_models('gemini', api_key=None)
    with_key = ModelSelector.get_multimodal_models('gemini', api_key='FAKE_KEY')
    assert calls == [None, 'FAKE_KEY'], "换 key 后应重新拉取，而不是命中旧缓存"
    assert no_key is not with_key

    # 相同 key 再次调用则命中缓存
    again = ModelSelector.get_multimodal_models('gemini', api_key='FAKE_KEY')
    assert again is with_key
    assert calls == [None, 'FAKE_KEY']
