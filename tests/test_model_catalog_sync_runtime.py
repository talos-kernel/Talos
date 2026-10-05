from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from talos import models
from talos.credentials import CredentialStore, Route
from talos.eventlog import EventLog
from talos.model_catalog_sync import ModelCatalogSync, refresh_configured


def result(slug="openai-api", models=("new",), error="", complete=False):
    return SimpleNamespace(
        slug=slug, models=tuple(models), error=error, complete=complete,
    )


def test_success_publishes_and_records_only_counts(tmp_path):
    log = EventLog(tmp_path / "events.db")
    published = []
    sync = ModelCatalogSync(
        lambda: (
            result(models=("new", "newer"), complete=True),
            result("down", (), "secret detail"),
        ),
        lambda: published.append(True),
        log,
    )

    assert sync.refresh_once() is True
    assert published == [True]
    row = log.recent(1, ("catalog.synced",))[0]["payload"]
    assert row == {
        "providers": {"openai-api": 2},
        "authoritative": ["openai-api"],
        "additive": [],
        "failed": ["down"],
    }
    assert "secret detail" not in str(row)


def test_failure_keeps_last_registry_and_logs_only_exception_type(tmp_path):
    log = EventLog(tmp_path / "events.db")
    published = []

    def broken():
        raise RuntimeError("token=must-not-enter-the-log")

    sync = ModelCatalogSync(broken, lambda: published.append(True), log)
    assert sync.refresh_once() is False
    assert published == []
    row = log.recent(1, ("catalog.sync_failed",))[0]["payload"]
    assert row == {"error": "RuntimeError"}


def test_completed_cycle_reloads_cli_catalogue_even_without_http_models(tmp_path):
    log = EventLog(tmp_path / "events.db")
    published = []
    sync = ModelCatalogSync(
        lambda: (result("offline", (), "network"),),
        lambda: published.append(True),
        log,
    )

    assert sync.refresh_once() is True
    assert published == [True]


def test_start_is_non_blocking_runs_immediately_and_is_idempotent(tmp_path):
    log = EventLog(tmp_path / "events.db")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def refresh():
        calls.append(True)
        entered.set()
        release.wait(2)
        return (result(),)

    sync = ModelCatalogSync(refresh, lambda: None, log)
    before = time.monotonic()
    sync.start()
    sync.start()
    elapsed = time.monotonic() - before
    try:
        assert elapsed < 0.25
        assert entered.wait(1)
        assert calls == [True]
    finally:
        release.set()
        sync.stop()


def test_configured_refresh_reuses_exact_provider_routes(monkeypatch, tmp_path):
    captured = {}

    def refresh(slugs, **kwargs):
        captured.update(slugs=tuple(slugs), **kwargs)
        return ()

    monkeypatch.setattr(models, "refresh", refresh)
    credentials = CredentialStore({
        "openai-api": Route("openai-api", "openai-key", "https://one.invalid/v1"),
        "ollama": Route("ollama", "", "https://ollama.test.invalid/v1"),
    })

    assert refresh_configured(credentials, tmp_path / "models.json") == ()
    assert captured["slugs"] == ("ollama", "openai-api")
    assert captured["keys"] == {"openai-api": "openai-key", "ollama": ""}
    assert captured["base_urls"] == {
        "openai-api": "https://one.invalid/v1",
        "ollama": "https://ollama.test.invalid/v1",
    }
    assert set(captured["provider_specs"]) == {"ollama", "openai-api"}
    assert captured["path"] == tmp_path / "models.json"


def test_composition_starts_sync_only_for_long_running_service_and_stops_it():
    source = (Path(__file__).resolve().parents[1] / "talos" / "__main__.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    starts = []
    stops = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "model_catalog_sync":
            continue
        if node.func.attr == "start":
            starts.append(node)
        elif node.func.attr == "stop":
            stops.append(node)
    assert len(starts) == 1 and len(stops) == 1
    assert "if not once:\n        model_catalog_sync.start()" in source
    assert source.index("if cli_channel is not None:") < source.index("model_catalog_sync.start()")
    assert source.index("if chat_channel is not None:") < source.index("model_catalog_sync.start()")
    assert source.index("finally:\n        model_catalog_sync.stop()") > source.index(
        "model_catalog_sync.start()"
    )
