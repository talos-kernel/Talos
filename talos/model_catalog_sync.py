"""Bounded background refresh for provider-owned model catalogues.

The synchronizer owns no credentials and knows no endpoints.  Those stay in the
existing provider/credential layer; this object only schedules an injected refresh,
reloads the immutable API and CLI/OAuth registry, and records a secret-free event.
A failed cycle never replaces the last known-good registry.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from .eventlog import Event, EventLog, new_run_id

DEFAULT_INTERVAL_S = 24 * 60 * 60


def refresh_configured(credentials, path: Path, *, get=None) -> tuple[object, ...]:
    """Refresh every configured API/local route with its provider-bound endpoint.

    The credential store already binds each key to one provider and one base URL.
    Reusing that mapping prevents a catalogue refresh from creating a second, looser
    routing table or ever borrowing another provider's key.
    """
    from . import catalog, models

    routes = dict(credentials.routes)
    slugs = tuple(sorted(routes))
    specs = {
        slug: info for slug in slugs
        if (info := catalog.get(slug)) is not None
    }
    return tuple(models.refresh(
        slugs,
        keys={slug: route.api_key for slug, route in routes.items()},
        base_urls={slug: route.base_url for slug, route in routes.items()},
        provider_specs=specs,
        path=Path(path),
        get=get,
    ))


class ModelCatalogSync:
    """Refresh once in the background, then at a bounded fixed interval."""

    def __init__(
        self,
        refresh: Callable[[], Iterable[object]],
        publish: Callable[[], object],
        log: EventLog,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._refresh = refresh
        self._publish = publish
        self._log = log
        self._interval_s = max(60.0, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start at most one daemon thread; the first refresh is immediate."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="talos-model-catalog",
            )
            self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout_s)))

    def refresh_once(self) -> bool:
        """Run one cycle.  Failure is recorded by type and keeps old state intact."""
        try:
            results = tuple(self._refresh())
        except Exception as error:
            self._record("catalog.sync_failed", {"error": type(error).__name__})
            return False

        usable = tuple(result for result in results if getattr(result, "models", ()))
        # Publishing also reloads provider-owned CLI/OAuth catalogues.  An empty HTTP
        # result therefore is not a reason to skip it; the merge itself keeps the
        # last-known-good API cache when every remote request failed.
        try:
            self._publish()
        except Exception as error:
            self._record("catalog.publish_failed", {"error": type(error).__name__})
            return False

        self._record("catalog.synced", {
            "providers": {
                str(getattr(result, "slug", "")): len(tuple(getattr(result, "models", ())))
                for result in usable
                if str(getattr(result, "slug", ""))
            },
            "authoritative": sorted(
                str(getattr(result, "slug", "")) for result in usable
                if getattr(result, "complete", False) and str(getattr(result, "slug", ""))
            ),
            "additive": sorted(
                str(getattr(result, "slug", "")) for result in usable
                if not getattr(result, "complete", False) and str(getattr(result, "slug", ""))
            ),
            "failed": sorted(
                str(getattr(result, "slug", "")) for result in results
                if not getattr(result, "models", ()) and str(getattr(result, "slug", ""))
            ),
        })
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            if self._stop.wait(self._interval_s):
                return

    def _record(self, kind: str, payload: dict[str, object]) -> None:
        try:
            self._log.append(Event(new_run_id(), "provider", kind, payload))
        except Exception:
            # Audit loss must not take the live agent down; the refresh result itself
            # remains governed by the cache's atomic last-known-good semantics.
            pass


__all__ = ["DEFAULT_INTERVAL_S", "ModelCatalogSync", "refresh_configured"]
