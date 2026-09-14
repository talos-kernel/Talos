"""Custom providers must work on cold boot, not only appear in a picker."""
import ast
import inspect
import json
from types import SimpleNamespace

import pytest

from talos import catalog, config, customproviders, credentials
from talos import __main__ as entrypoint
from talos.api_reasoner import ApiReasoner
from talos.provider import ModelSelection


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(catalog, 'PROVIDERS', catalog.PROVIDERS)
    monkeypatch.setattr(catalog, '_BY_SLUG', dict(catalog._BY_SLUG))
    monkeypatch.delenv('TALOS_MODEL_WORKER', raising=False)


def definition(*, keyed=False):
    row = {'name': 'test-proxy', 'base_url': 'http://127.0.0.1:12345/v1',
           'models': ['test-model']}
    if keyed:
        row['env_key'] = 'TEST_PROXY_KEY'
    return json.dumps([row])


def boot_config(tmp_path, monkeypatch, *, keyed=False):
    path = tmp_path / 'custom.json'
    path.write_text(definition(keyed=keyed))
    monkeypatch.setattr(config, '_read_env_file', lambda _: {})
    monkeypatch.setenv('TALOS_CUSTOM_PROVIDERS', str(path))
    monkeypatch.setenv('TALOS_ALLOWED_PRINCIPALS', 'cli:123')
    monkeypatch.delenv('TEST_PROXY_KEY', raising=False)
    return config.load_config(require_channel=False)


def test_cold_boot_has_keyless_custom_route(tmp_path, monkeypatch):
    cfg = boot_config(tmp_path, monkeypatch)
    assert cfg.api_credentials.routes['test-proxy'].base_url == 'http://127.0.0.1:12345/v1'
    assert cfg.api_credentials.routes['test-proxy'].api_key == ''


def test_cold_boot_reads_only_custom_key(tmp_path, monkeypatch):
    path = tmp_path / 'custom.json'
    path.write_text(definition(keyed=True))
    monkeypatch.setattr(config, '_read_env_file', lambda _: {'TEST_PROXY_KEY': 'test-only-own-key'})
    monkeypatch.setenv('TALOS_CUSTOM_PROVIDERS', str(path))
    monkeypatch.setenv('TALOS_ALLOWED_PRINCIPALS', 'cli:123')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-foreign-key')
    cfg = config.load_config(require_channel=False)
    assert cfg.api_credentials.routes['test-proxy'].api_key == 'test-only-own-key'


def test_registered_custom_provider_builds_native_reasoner():
    infos = customproviders.parse(definition())
    catalog.register(infos)
    store = credentials.from_lookup(lambda _: '', custom_providers=infos)
    reasoner = ApiReasoner('test-proxy', 'test-model', store, timeout_s=5, worker='')
    url, headers, body = reasoner._request('test system', 'test user')
    assert url == 'http://127.0.0.1:12345/v1/chat/completions'
    assert 'Authorization' not in headers
    assert not {'tools', 'tool_choice'} & body.keys()


def test_service_factory_uses_native_route_not_hermes_cli():
    infos = customproviders.parse(definition())
    catalog.register(infos)
    store = credentials.from_lookup(lambda _: '', custom_providers=infos)
    tree = ast.parse(inspect.getsource(entrypoint))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'build_reasoner')
    scope = dict(vars(entrypoint))
    scope.update(config=SimpleNamespace(api_credentials=store, reasoner_timeout_s=5),
                 meter=None, skills_catalogue=lambda: '')
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<service-factory>', 'exec'), scope)
    result = scope['build_reasoner'](ModelSelection('test-proxy', 'test-model'))
    assert isinstance(result, ApiReasoner)


def test_missing_custom_key_never_borrows_another_providers_key():
    infos = customproviders.parse(definition(keyed=True))
    catalog.register(infos)
    store = credentials.from_lookup(lambda k: 'test-only-foreign-key' if k == 'OPENAI_API_KEY' else '', custom_providers=infos)
    with pytest.raises(credentials.MissingKey):
        ApiReasoner('test-proxy', 'test-model', store, timeout_s=5, worker='')


def test_repeat_boot_keeps_routes_without_mutating_builtin_catalog(tmp_path, monkeypatch):
    original = catalog.PROVIDERS
    cfg = boot_config(tmp_path, monkeypatch)
    catalog.register(cfg.custom_provider_infos)
    again = config.load_config(require_channel=False)
    assert again.custom_provider_infos == cfg.custom_provider_infos
    assert again.api_credentials.routes == cfg.api_credentials.routes
    assert catalog.PROVIDERS is original
    assert customproviders.parse(definition().replace('12345', '54321')) == ()


def test_config_factory_http_path(tmp_path, monkeypatch):
    from tests.test_api_reasoner import FakeHttp, FakeResponse, sse
    import talos.api_reasoner as api
    cfg = boot_config(tmp_path, monkeypatch)
    catalog.register(cfg.custom_provider_infos)
    http = FakeHttp(FakeResponse([
        sse({'choices': [{'delta': {'content': 'CUSTOM_OK'}}]}), 'data: [DONE]']))
    monkeypatch.setattr(api, '_default_http', lambda: http)
    tree = ast.parse(inspect.getsource(entrypoint))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'build_reasoner')
    scope = dict(vars(entrypoint))
    scope.update(config=cfg, meter=None, skills_catalogue=lambda: '')
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<service-factory>', 'exec'), scope)
    reasoner = scope['build_reasoner'](ModelSelection('test-proxy', 'test-model'))
    assert isinstance(reasoner, ApiReasoner)
    assert reasoner.reason_composed('system', 'user') == 'CUSTOM_OK'
    assert http.calls[0]['url'] == 'http://127.0.0.1:12345/v1/chat/completions'
    assert 'Authorization' not in http.calls[0]['headers']
    assert not {'tools', 'tool_choice'} & http.body.keys()


def test_worker_rejects_custom_provider_even_when_agent_registered_it():
    from talos.modelworker import handle_frame
    catalog.register(customproviders.parse(definition()))
    def forbidden(*args, **kwargs):
        pytest.fail('worker must not construct an agent-owned route')
    frame = json.dumps({'provider': 'test-proxy', 'model': 'test-model',
                        'messages': [{'role': 'user', 'content': 'test'}],
                        'base_url': 'http://untrusted.example', 'env_key': 'OPENAI_API_KEY'}).encode()
    result = handle_frame(frame, credentials.CredentialStore(), build=forbidden)
    assert result['ok'] is False
    assert result['kind'] == 'invalid_request'


@pytest.mark.parametrize('provider', ['not-registered', 'claude-cli', 'openai-codex', 'nous-portal'])
def test_unknown_and_cli_providers_do_not_get_native_transport(provider):
    with pytest.raises(ValueError):
        ApiReasoner(provider, 'test-model', credentials.CredentialStore(), timeout_s=5, worker='')
