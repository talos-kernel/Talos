from __future__ import annotations

from pathlib import Path
import threading

import pytest

from talos import models
from talos.catalog import ProviderInfo
from talos.provider import Provider, ProviderRegistry


class Response:
    def __init__(self, payload=None, *, status: int = 200, json_error: Exception | None = None):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def registry(*names: str) -> ProviderRegistry:
    return ProviderRegistry((Provider("openai-api", "OpenAI", tuple(names)),))


def cached(path: Path, names=("kept", "retired"), *, at: float = 10.0) -> dict:
    value = {
        "openai-api": {
            "models": list(names),
            "fetched_at": at,
            models.AUTHORITATIVE: True,
        }
    }
    models.save_cache(path, value)
    return value


def test_successful_snapshot_adds_new_models_and_removes_absent_models(tmp_path: Path) -> None:
    path = tmp_path / "models.json"

    result = models.refresh(
        ("openai-api",),
        keys={"openai-api": "test-key"},
        path=path,
        get=lambda *_a, **_k: Response({"data": [{"id": "kept"}, {"id": "new"}]}),
        now=lambda: 100.0,
    )

    cache = models.load_cache(path)
    assert result[0].models == ("kept", "new")
    assert result[0].complete is True
    assert cache["openai-api"] == {
        "models": ["kept", "new"],
        "fetched_at": 100.0,
        models.AUTHORITATIVE: True,
    }
    assert models.merged(registry("kept", "retired"), cache, now=lambda: 101.0).providers[
        0
    ].models == ("kept", "new")


def test_unmarked_legacy_cache_remains_add_only() -> None:
    legacy = {
        "openai-api": {"models": ["new"], "fetched_at": 100.0},
    }

    merged = models.merged(registry("kept", "retired"), legacy, now=lambda: 101.0)

    assert merged.providers[0].models == ("kept", "retired", "new")


def test_paginated_first_page_adds_but_never_removes(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    cached(path, ("kept", "retired"), at=10.0)

    result = models.refresh(
        ("openai-api",),
        keys={"openai-api": "test-key"},
        path=path,
        get=lambda *_a, **_k: Response({
            "data": [{"id": "new-on-first-page"}],
            "has_more": True,
            "next_cursor": "page-two",
        }),
        now=lambda: 20.0,
    )

    assert result[0].models == ("new-on-first-page",)
    assert result[0].complete is False
    entry = models.load_cache(path)["openai-api"]
    assert entry["models"] == ["kept", "retired", "new-on-first-page"]
    assert entry[models.AUTHORITATIVE] is True


def test_anthropic_needs_explicit_final_page_marker_for_removal(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    models.save_cache(path, {
        "anthropic-api": {
            "models": ["claude-kept", "claude-retired"],
            "fetched_at": 1.0,
            models.AUTHORITATIVE: True,
        }
    })

    partial = models.refresh(
        ("anthropic-api",), keys={"anthropic-api": "test-key"}, path=path,
        get=lambda *_a, **_k: Response({"data": [{"id": "claude-new"}]}),
        now=lambda: 2.0,
    )[0]
    assert partial.complete is False
    assert models.load_cache(path)["anthropic-api"]["models"] == [
        "claude-kept", "claude-retired", "claude-new"
    ]

    complete = models.refresh(
        ("anthropic-api",), keys={"anthropic-api": "test-key"}, path=path,
        get=lambda *_a, **_k: Response({
            "data": [{"id": "claude-new"}], "has_more": False,
        }),
        now=lambda: 3.0,
    )[0]
    assert complete.complete is True
    assert models.load_cache(path)["anthropic-api"]["models"] == ["claude-new"]


def test_stale_authoritative_snapshot_still_governs_until_a_later_success() -> None:
    cache = {
        "openai-api": {
            "models": ["kept", "new"],
            "fetched_at": 100.0,
            models.AUTHORITATIVE: True,
        }
    }
    long_after_ttl = 100.0 + models.CACHE_TTL_S + 1

    assert models.fresh_models(cache, "openai-api", now=lambda: long_after_ttl) == ()
    merged = models.merged(
        registry("kept", "retired"), cache, now=lambda: long_after_ttl
    )
    assert merged.providers[0].models == ("kept", "new")


def test_malformed_authoritative_cache_cannot_remove_curated_models() -> None:
    corrupt = {
        "openai-api": {
            "models": ["new", "bad model with spaces"],
            "fetched_at": 100.0,
            models.AUTHORITATIVE: True,
        }
    }

    merged = models.merged(registry("kept", "retired"), corrupt, now=lambda: 101.0)

    assert merged.providers[0].models == ("kept", "retired", "new")


def _network_failure(*_args, **_kwargs):
    raise OSError("secret network diagnostic")


def _timeout(*_args, **_kwargs):
    raise TimeoutError("secret timeout diagnostic")


@pytest.mark.parametrize(
    "get",
    [
        _timeout,
        _network_failure,
        lambda *_a, **_k: Response(
            {"data": [{"id": "attacker-model"}]}, status=401
        ),
        lambda *_a, **_k: Response(
            {"data": [{"id": "attacker-model"}]}, status=503
        ),
        lambda *_a, **_k: Response(
            json_error=ValueError("secret JSON diagnostic")
        ),
        lambda *_a, **_k: Response({"data": []}),
        lambda *_a, **_k: Response(
            {"data": [{"id": "apparently-valid"}, {"id": "x" * 500}]}
        ),
        lambda *_a, **_k: Response({"data": [{"id": "claude-smuggled-model"}]}),
    ],
    ids=(
        "timeout", "network", "auth", "http", "json", "empty", "malformed", "hostile"
    ),
)
def test_failures_never_overwrite_or_remove_the_last_good_snapshot(
    tmp_path: Path, get
) -> None:
    path = tmp_path / "models.json"
    expected = cached(path)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    result = models.refresh(
        ("openai-api",),
        keys={"openai-api": "test-key"},
        path=path,
        get=get,
        now=lambda: 20.0,
    )

    assert result[0].models == ()
    assert "secret" not in result[0].error
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    assert models.load_cache(path) == expected
    assert models.merged(registry("curated"), expected, now=lambda: 21.0).providers[
        0
    ].models == ("kept", "retired")


def test_noop_refresh_does_not_touch_cache_mtime(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    cached(path)
    before = path.stat().st_mtime_ns

    assert models.refresh((), keys={}, path=path, now=lambda: 20.0) == ()

    assert path.stat().st_mtime_ns == before


def test_http_status_is_checked_before_a_valid_looking_error_body(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    expected = cached(path)

    result = models.refresh(
        ("openai-api",),
        keys={"openai-api": "test-key"},
        path=path,
        get=lambda *_a, **_k: Response({"data": [{"id": "must-not-land"}]}, status=500),
        now=lambda: 20.0,
    )

    assert result[0].models == ()
    assert models.load_cache(path) == expected


def test_local_openai_provider_spec_is_keyless_and_keeps_its_exact_route(tmp_path: Path) -> None:
    spec = ProviderInfo(
        slug="local-lab",
        label="Local lab",
        auth="local",
        wire="openai",
        base_url="https://local-models.example/openai/v1/exact",
        notes="test route",
    )
    seen = []

    def get(url, *, headers, timeout):
        seen.append((url, headers, timeout))
        return Response({"data": [{"id": "local-model"}]})

    result = models.refresh(
        (spec.slug,),
        keys={},
        path=tmp_path / "models.json",
        provider_specs={spec.slug: spec},
        get=get,
        now=lambda: 30.0,
    )

    assert result[0].models == ("local-model",)
    assert seen == [
        ("https://local-models.example/openai/v1/exact/models", {}, models.FETCH_TIMEOUT_S)
    ]


def test_provider_spec_selects_anthropic_auth_for_a_custom_slug() -> None:
    spec = ProviderInfo(
        slug="anthropic-gateway",
        label="Anthropic gateway",
        auth="api-key",
        wire="anthropic",
        base_url="https://anthropic-gateway.example/exact/base",
        env_key="ANTHROPIC_GATEWAY_KEY",
        notes="test route",
    )
    seen = {}

    def get(url, *, headers, timeout):
        seen.update(url=url, headers=headers, timeout=timeout)
        return Response({"data": [{"id": "gateway-test-model"}]})

    result = models.fetch(
        spec.slug,
        api_key="test-key",
        provider_spec=spec,
        get=get,
        now=lambda: 40.0,
    )

    assert result.models == ("gateway-test-model",)
    assert result.complete is False
    assert seen["url"] == "https://anthropic-gateway.example/exact/base/models"
    assert seen["headers"] == {
        "x-api-key": "test-key",
        "anthropic-version": models.ANTHROPIC_VERSION,
    }


def test_foreign_anthropic_wire_cannot_reintroduce_claude_models(tmp_path: Path) -> None:
    spec = ProviderInfo(
        slug="aws-bedrock", label="Bedrock", auth="api-key", wire="anthropic",
        base_url="https://bedrock.invalid/v1", env_key="BEDROCK_KEY", notes="test",
    )
    result = models.refresh(
        (spec.slug,), keys={spec.slug: "test-key"}, path=tmp_path / "models.json",
        provider_specs={spec.slug: spec},
        get=lambda *_a, **_k: Response({
            "data": [{"id": "anthropic.claude-opus"}], "has_more": False,
        }),
        now=lambda: 4.0,
    )[0]

    assert result.models == ()
    assert not (tmp_path / "models.json").exists()


def test_concurrent_refreshes_keep_both_provider_updates(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    entered = threading.Barrier(2)

    def run(slug: str, model: str) -> None:
        def get(*_args, **_kwargs):
            entered.wait(timeout=2)
            return Response({"data": [{"id": model}]})
        models.refresh(
            (slug,), keys={slug: "test-key"}, base_urls={slug: "https://same.invalid/v1"},
            path=path, get=get, now=lambda: 5.0,
        )

    first = threading.Thread(target=run, args=("openai-api", "gpt-new"))
    second = threading.Thread(target=run, args=("nvidia-nim", "nim-new"))
    first.start(); second.start(); first.join(3); second.join(3)

    assert not first.is_alive() and not second.is_alive()
    cache = models.load_cache(path)
    assert cache["openai-api"]["models"] == ["gpt-new"]
    assert cache["nvidia-nim"]["models"] == ["nim-new"]


def test_older_completed_fetch_cannot_overwrite_a_newer_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    cached(path, ("newer",), at=20.0)

    result = models.refresh(
        ("openai-api",), keys={"openai-api": "test-key"}, path=path,
        get=lambda *_a, **_k: Response({"data": [{"id": "older"}]}),
        now=lambda: 10.0,
    )[0]

    assert result.complete is True
    assert models.load_cache(path)["openai-api"]["models"] == ["newer"]


def test_atomic_cache_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "models.json"

    models.save_cache(path, {"provider": {"models": ["one"], "fetched_at": 1.0}})

    assert models.load_cache(path)["provider"]["models"] == ["one"]
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
