"""Die Modellliste eines Anbieters — live geholt, aber nie blind uebernommen.

Bis hierher war der Katalog handkuratiert: was ein Anbieter neu herausbringt, musste
jemand eintragen. Hermes fragt stattdessen `/v1/models` ab, und das ist der bessere
Weg — aber nur mit vier Vorbehalten, die hier den Code bestimmen.

⚠️ **Alte Zwischenspeicher ergaenzen weiter nur.** Erst ein neuer, vollstaendig
validierter und ausdruecklich als massgeblich gespeicherter Schnappschuss darf die
angebotene Liste dieses Anbieters ersetzen. Faellt eine spaetere Abfrage aus, antwortet
der Anbieter leer oder liefert er Unsinn, bleibt der letzte gute Stand stehen.

⚠️ **Nichts blockiert den Start.** Der Dienst gleicht im Hintergrund sofort und danach
hoechstens taeglich ab; `talos models --refresh` tut dasselbe auf Ansage. Antworten und
Hochfahren lesen immer zuerst den letzten guten Stand von der Platte.

⚠️ **Die Antwort ist fremder Text.** Sie landet in einer Auswahlliste und in
Telegram-Callback-Daten, und die sind auf 64 Byte begrenzt — ein Modellname von 300
Zeichen zerlegt den Picker, nicht die Sicherheit, aber kaputt ist kaputt. Deshalb
Form, Laenge und Anzahl begrenzt.

⚠️ **Ohne Schluessel kein fremder Abruf.** Nur ein vom Aufrufer gelieferter, gepruefter
lokaler OpenAI-kompatibler Anbieter darf schluessellos gefragt werden. Bei allen
schluesselpflichtigen Anbietern bedeutet ein fehlender Schluessel weiterhin: kein Netz.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

import fcntl

# Ein Tag. Modelle erscheinen nicht stuendlich, und ein Zwischenspeicher, der staendig
# ablaeuft, ist keiner.
CACHE_TTL_S = 24 * 60 * 60
FETCH_TIMEOUT_S = 20
# Der Picker traegt den Namen in Telegram-Callback-Daten (64 Byte insgesamt).
MAX_NAME_CHARS = 60
MAX_MODELS = 60
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")

# Wer wo fragt. Anthropic weicht in Kopfzeile und Version ab — das ist der ganze
# Unterschied, deshalb eine Tabelle statt zweier Codewege.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "anthropic-api": ("https://api.anthropic.com/v1", "x-api-key"),
    "openai-api": ("https://api.openai.com/v1", "bearer"),
    # Dieselben zwei Protokolle, dieselbe Tabelle: der ganze Unterschied ist die Adresse.
    # Die Route aus `credentials` traegt sie ohnehin bereits — dies ist der Rueckhalt
    # fuer Bestaende ohne Adresse.
    "nvidia-nim": ("https://integrate.api.nvidia.com/v1", "bearer"),
    "kimi": ("https://api.kimi.com/coding/v1", "bearer"),
}
ANTHROPIC_VERSION = "2023-06-01"
AUTHORITATIVE = "authoritative"

# Nur diese Adapter belegen, dass ihre erfolgreiche, nicht paginierte `/models`-Antwort
# die komplette Liste ist. Andere OpenAI-kompatible Endpunkte duerfen neue Namen
# beitragen, aber niemals alte entfernen, solange ihr Vollstaendigkeitssignal unbekannt
# ist. CLI/OAuth-Kataloge laufen ohnehin ueber Hermes und bleiben ebenfalls additiv.
AUTHORITATIVE_LISTINGS = frozenset({
    "anthropic-api", "openai-api", "ollama", "lm-studio",
})


@dataclass(frozen=True)
class Fetched:
    slug: str
    models: tuple[str, ...]
    fetched_at: float
    error: str = ""
    complete: bool = False


def is_model_id(name: str) -> bool:
    """Sieht das aus wie eine Modell-ID? Dieselbe Form fuer eine live geholte ID und
    fuer einen Schluessel in `TALOS_MODEL_OVERRIDES`: was hier nicht durchkaeme, kann
    dort nichts treffen."""
    return bool(name) and len(name) <= MAX_NAME_CHARS and bool(_NAME_OK.match(name))


def clean_names(raw: Iterable[object]) -> tuple[str, ...]:
    """Was aus einer fremden Antwort als Modellname durchgeht.

    Reihenfolge bleibt erhalten (Anbieter sortieren sinnvoll: neu zuerst), Doubletten
    fallen weg, und alles, was nicht wie ein Bezeichner aussieht, ebenfalls.
    """
    gesehen: list[str] = []
    for eintrag in raw or ():
        name = str(eintrag or "").strip()
        if not is_model_id(name):
            continue
        if name not in gesehen:
            gesehen.append(name)
        if len(gesehen) >= MAX_MODELS:
            break
    return tuple(gesehen)


def _ids(payload: object) -> tuple[tuple[str, ...], bool]:
    """Eine *vollstaendig* brauchbare ``/models``-Antwort.

    ``clean_names`` bleibt absichtlich tolerant fuer alte Cache-Dateien. Ein neuer
    massgeblicher Schnappschuss darf dagegen nicht aus einer abgeschnittenen oder nur
    teilweise brauchbaren Fremdantwort entstehen: sonst koennte gerade die Begrenzung
    Modelle verschwinden lassen, die der Anbieter sehr wohl genannt hat.
    """
    if not isinstance(payload, dict):
        return (), False
    eintraege = payload.get("data")
    if not isinstance(eintraege, list) or not eintraege or len(eintraege) > MAX_MODELS:
        return (), False
    namen: list[str] = []
    for eintrag in eintraege:
        if not isinstance(eintrag, dict):
            return (), False
        name = eintrag.get("id")
        if not isinstance(name, str) or name != name.strip() or not is_model_id(name):
            return (), False
        if name not in namen:
            namen.append(name)
    # A first page is useful for additions, never for removals. Providers use several
    # continuation spellings; any truthy one makes this response incomplete.
    weiter = payload.get("has_more") is True or any(
        payload.get(name) not in (None, "", False)
        for name in ("next", "next_page", "next_page_token", "next_cursor")
    )
    return tuple(namen), not weiter


def _provider_route(slug: str, *, api_key: str, base_url: str,
                    provider_spec: object | None) -> tuple[str, str, str]:
    """Adresse, Authentisierung und ein sicherer Fehlercode fuer genau einen Anbieter.

    ``provider_spec`` ist absichtlich strukturell statt auf ``catalog.ProviderInfo``
    typisiert. Damit bleibt dieses kleine Netzmodul unabhaengig vom Katalog und kann
    trotzdem dessen gepruefte Eintraege sowie gleich geformte Testdoubles verwenden.
    """
    vorgabe, art = ENDPOINTS.get(slug, ("", "bearer"))
    if provider_spec is not None:
        if str(getattr(provider_spec, "slug", "")) != slug:
            return "", "", "provider specification mismatch"
        wire = str(getattr(provider_spec, "wire", ""))
        auth = str(getattr(provider_spec, "auth", ""))
        if wire not in ("openai", "anthropic"):
            return "", "", "unsupported provider protocol"
        if auth == "local" and wire == "openai":
            art = "none"
        elif auth == "api-key":
            art = "x-api-key" if wire == "anthropic" else "bearer"
        else:
            return "", "", "unsupported provider authentication"
        vorgabe = str(getattr(provider_spec, "base_url", "") or vorgabe)
    wurzel = (base_url or vorgabe).rstrip("/")
    if not wurzel:
        return "", "", "no endpoint known for this provider"
    if art != "none" and not api_key.strip():
        return "", "", "no key — not asked"
    return wurzel, art, ""


def fetch(slug: str, *, api_key: str, base_url: str = "", get: Callable | None = None,
          now: Callable[[], float] = time.time,
          provider_spec: object | None = None) -> Fetched:
    """Holt die Liste eines Anbieters. Fehler sind ein Ergebnis, keine Ausnahme.

    Ein Anbieter, der gerade nicht antwortet, darf weder den Aufruf noch den Katalog
    umwerfen — er darf nur nichts beitragen.
    """
    wurzel, art, fehler = _provider_route(
        slug, api_key=api_key, base_url=base_url, provider_spec=provider_spec,
    )
    if fehler:
        return Fetched(slug, (), now(), fehler)

    kopf = ({"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
            if art == "x-api-key" else
            ({"Authorization": f"Bearer {api_key}"} if art == "bearer" else {}))
    if get is None:
        import requests

        get = requests.get
    try:
        antwort = get(f"{wurzel}/models", headers=kopf, timeout=FETCH_TIMEOUT_S)
        # Ein Fehlerkoerper kann zufaellig wie eine Modellliste aussehen. Deshalb wird
        # der Status VOR JSON und Inhalt geprueft; weder Koerper noch Exception-Text
        # gelangen in ``Fetched.error``.
        status = getattr(antwort, "status_code", None)
        if status is not None:
            try:
                erfolgreich = 200 <= int(status) < 300
            except (TypeError, ValueError):
                erfolgreich = False
            if not erfolgreich:
                return Fetched(slug, (), now(), "HTTP request failed")
        elif callable(getattr(antwort, "raise_for_status", None)):
            antwort.raise_for_status()
        daten = antwort.json() if callable(getattr(antwort, "json", None)) else None
    except Exception as fehler:                  # Netz, TLS, JSON — hier alles dasselbe
        return Fetched(slug, (), now(), type(fehler).__name__)
    namen, vollstaendig = _ids(daten)
    if namen and not _allows_claude_models(slug, provider_spec):
        from .provider import _looks_like_claude_model

        if any(_looks_like_claude_model(name) for name in namen):
            return Fetched(slug, (), now(), "no usable model names in the answer")
    wire = str(getattr(provider_spec, "wire", "")) if provider_spec is not None else ""
    if slug == "anthropic-api" or wire == "anthropic":
        # Anthropic's list endpoint is cursor-paginated. Absence of the explicit final
        # marker is not proof that this was the final page.
        vollstaendig = vollstaendig and isinstance(daten, dict) and daten.get("has_more") is False
    massgeblich = bool(
        namen and vollstaendig and slug in AUTHORITATIVE_LISTINGS
    )
    return Fetched(
        slug,
        namen,
        now(),
        "" if namen else "no usable model names in the answer",
        complete=massgeblich,
    )


def load_cache(path: Path) -> dict[str, dict]:
    try:
        daten = json.loads(path.read_text(encoding="utf-8"))
        return daten if isinstance(daten, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as datei:
            json.dump(cache, datei, indent=2, sort_keys=True)
            datei.flush()
            os.fsync(datei.fileno())
        temp.replace(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _fresh_entry(cache: dict[str, dict], slug: str, *, ttl_s: int,
                 now: Callable[[], float]) -> dict:
    eintrag = cache.get(slug)
    if not isinstance(eintrag, dict):
        return {}
    try:
        alter = now() - float(eintrag.get("fetched_at") or 0)
    except (TypeError, ValueError, OverflowError):
        return {}
    if alter > ttl_s or alter < 0:               # negativ = die Uhr ist gesprungen
        return {}
    return eintrag


def _merge_entry(cache: dict[str, dict], slug: str, *, ttl_s: int,
                 now: Callable[[], float]) -> dict:
    """Der Stand fuer den Katalog: Altbestand mit TTL, letzter guter Stand ohne.

    Ein massgeblicher Schnappschuss ist nicht bloss eine Beschleunigung fuer den
    naechsten Netzaufruf. Er ist der letzte vom Anbieter vollstaendig bestaetigte
    Katalog und gilt deshalb bis ein spaeterer *erfolgreicher* Abruf ihn ersetzt.
    """
    eintrag = cache.get(slug)
    if isinstance(eintrag, dict) and eintrag.get(AUTHORITATIVE) is True:
        return eintrag
    return _fresh_entry(cache, slug, ttl_s=ttl_s, now=now)


def _complete_cached_names(entry: dict) -> tuple[str, ...]:
    """Strict names for destructive replacement; malformed cache is never complete."""
    raw = entry.get("models")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_MODELS:
        return ()
    if any(not isinstance(name, str) or name != name.strip() or not is_model_id(name)
           for name in raw):
        return ()
    if len(set(raw)) != len(raw):
        return ()
    return tuple(raw)


def fresh_models(cache: dict[str, dict], slug: str, *, ttl_s: int = CACHE_TTL_S,
                 now: Callable[[], float] = time.time) -> tuple[str, ...]:
    """Was im Zwischenspeicher steht — sofern es nicht zu alt ist."""
    eintrag = _fresh_entry(cache, slug, ttl_s=ttl_s, now=now)
    return clean_names(eintrag.get("models") or ())


def _allows_claude_models(slug: str, provider_spec: object | None = None) -> bool:
    # Keep the exact routing boundary from `safe_talos_registry`: foreign providers,
    # including Bedrock aliases, cannot reintroduce Claude models through live data.
    # `provider_spec` may describe a protocol, but a protocol is not authorization.
    return slug in ("claude-cli", "anthropic-api")


def merged(registry, cache: dict[str, dict], *, ttl_s: int = CACHE_TTL_S,
           now: Callable[[], float] = time.time):
    """Katalog plus frische Namen oder der letzte massgebliche Schnappschuss.

    ⚠️ Der Filter aus `provider.safe_talos_registry` gilt hier erneut: eine Live-Liste
    darf kein Claude-Modell unter einem fremden Anbieter einschleusen — sonst laeuft
    ein Aufruf ueber ein Konto, ueber das niemand entschieden hat.
    """
    from .provider import Provider, ProviderRegistry, _looks_like_claude_model

    ergaenzt = []
    for anbieter in registry.providers:
        eintrag = _merge_entry(cache, anbieter.slug, ttl_s=ttl_s, now=now)
        live = clean_names(eintrag.get("models") or ())
        komplett = _complete_cached_names(eintrag)
        sicher = live if _allows_claude_models(anbieter.slug) else tuple(
            m for m in live if not _looks_like_claude_model(m)
        )
        # Ein manipulierter massgeblicher Cache darf durch den Sicherheitsfilter nicht
        # zur unvollstaendigen Ersatzliste werden. In diesem Fall gilt wie bei Altbestand
        # nur die gefahrlose, additive Semantik.
        if (eintrag.get(AUTHORITATIVE) is True and komplett
                and sicher == komplett):
            ergaenzt.append(Provider(anbieter.slug, anbieter.label, komplett))
            continue
        live = sicher
        neue = tuple(m for m in live if m not in anbieter.models)
        ergaenzt.append(
            Provider(anbieter.slug, anbieter.label, anbieter.models + neue)
            if neue else anbieter
        )
    return ProviderRegistry(tuple(ergaenzt))


@contextmanager
def _cache_lock(path: Path):
    """Cross-process lock for the cache read-modify-write transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def refresh(slugs: Iterable[str], *, keys: dict[str, str], path: Path,
            base_urls: dict[str, str] | None = None, get=None,
            now: Callable[[], float] = time.time,
            provider_specs: Mapping[str, object] | None = None) -> tuple[Fetched, ...]:
    """Holt die genannten Anbieter und schreibt den Zwischenspeicher fort.

    Ein Fehlschlag loescht den alten Eintrag NICHT: eine Stoerung beim Anbieter darf
    keine Liste vernichten, die gestern noch stimmte.
    """
    ergebnisse = []
    for slug in slugs:
        ergebnis = fetch(slug, api_key=keys.get(slug, ""),
                         base_url=(base_urls or {}).get(slug, ""), get=get, now=now,
                         provider_spec=(provider_specs or {}).get(slug))
        ergebnisse.append(ergebnis)
    brauchbar = tuple(ergebnis for ergebnis in ergebnisse if ergebnis.models)
    if not brauchbar:
        # Ein reiner Fehlschlag beruehrt weder Cache noch Lock-Datei: Inhalt, mtime und
        # der letzte atomar geschriebene gute Stand bleiben exakt erhalten.
        return tuple(ergebnisse)

    # Fetching stays outside the lock; only the short read-merge-replace transaction is
    # serialized. A manual and an automatic refresh can therefore never overwrite the
    # other provider's newly written entry with an older in-memory copy.
    with _cache_lock(path):
        cache = load_cache(path)
        geaendert = False
        for ergebnis in brauchbar:
            bisher = cache.get(ergebnis.slug)
            bisher = bisher if isinstance(bisher, dict) else {}
            try:
                bisher_geholt = float(bisher.get("fetched_at") or 0)
            except (TypeError, ValueError, OverflowError):
                bisher_geholt = 0.0
            if bisher_geholt > ergebnis.fetched_at:
                # Two processes may finish out of order. A response fetched earlier
                # can never roll a newer provider snapshot back after waiting for lock.
                continue
            alte_namen = clean_names(bisher.get("models") or ())
            if ergebnis.complete:
                neu = {
                    "models": list(ergebnis.models),
                    "fetched_at": ergebnis.fetched_at,
                    AUTHORITATIVE: True,
                }
            else:
                vereint = alte_namen + tuple(
                    name for name in ergebnis.models if name not in alte_namen
                )
                if vereint == alte_namen:
                    continue
                neu = {"models": list(vereint), "fetched_at": ergebnis.fetched_at}
                if bisher.get(AUTHORITATIVE) is True:
                    # This remains a safe superset of the last complete snapshot: new
                    # names are proven, old names are retained until a complete list.
                    neu[AUTHORITATIVE] = True
            cache[ergebnis.slug] = neu
            geaendert = True
        if geaendert:
            save_cache(path, cache)
    return tuple(ergebnisse)


def render(results: Iterable[Fetched], cache: dict[str, dict]) -> str:
    from .ux import SYM_FAIL, SYM_OK

    zeilen = [""]
    for ergebnis in results:
        gespeichert = len(clean_names((cache.get(ergebnis.slug) or {}).get("models") or ()))
        if ergebnis.models:
            scope = "" if ergebnis.complete else " — additions only (listing not proven complete)"
            zeilen.append(
                f"  {SYM_OK} {ergebnis.slug:16} {len(ergebnis.models)} models{scope}"
            )
        else:
            hinweis = ergebnis.error or "nothing returned"
            nachsatz = f" — keeping {gespeichert} cached" if gespeichert else ""
            zeilen.append(f"  {SYM_FAIL} {ergebnis.slug:16} {hinweis}{nachsatz}")
    return "\n".join(zeilen) + "\n"


def _catalog():
    """Derselbe Katalog wie im laufenden Agenten — und derselbe Rueckfall.

    Ohne Hermes auf der Maschine bleiben die beiden API-Wege uebrig; `safe_talos_registry`
    haengt sie ohnehin immer an. Ein fehlender Nachbar darf diesen Befehl nicht umwerfen,
    sonst ist er ausgerechnet auf einer frischen Installation unbrauchbar.
    """
    from .config import HERMES_MODELS, HERMES_PROVIDER_CATALOG
    from .provider import HermesCatalogLoader, Provider, ProviderRegistry, safe_talos_registry

    try:
        roh = HermesCatalogLoader(HERMES_PROVIDER_CATALOG, HERMES_MODELS).load()
    except Exception:
        roh = ProviderRegistry((Provider(
            "claude-cli", "placeholder", ("claude-fable-5", "claude-fable-5-1")
        ),))
    return safe_talos_registry(roh)


def known_model_ids() -> frozenset[str]:
    """Jede Modell-ID, die `talos models` zeigen wuerde — Katalog plus Zwischenspeicher.

    Der Massstab fuer „gibt es dieses Modell", den `talos doctor` an die Overrides legt.
    Bewusst dieselbe Quelle wie die Anzeige: ein Name, der hier fehlt, fehlt auch dort.
    """
    from .config import MODEL_CACHE

    voll = merged(_catalog(), load_cache(Path(MODEL_CACHE)))
    return frozenset(model for anbieter in voll.providers for model in anbieter.models)


def run_models(argv: list[str] | None = None, *, out=None, get=None) -> int:
    """`talos models [--refresh]` — zeigt den Katalog, holt ihn auf Wunsch neu.

    Betreiber-Overrides (`TALOS_MODEL_OVERRIDES`) stehen unter dem Modell, das sie
    treffen, und sind als solche gekennzeichnet: der Katalog liefert diese Zahlen nicht,
    und die Anzeige darf nicht so tun. Was keinen Katalog-Eintrag trifft, steht als
    Warnung darunter — und kaputtes JSON ist eine Fehlerzeile mit Rueckgabewert 1, nie
    ein stiller Katalog ohne Overrides.
    """
    import sys

    from . import catalog, modelinfo
    from .config import MODEL_CACHE, load_config, load_model_overrides
    from .ux import SYM_FAIL

    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    pfad = Path(MODEL_CACHE)

    if "--refresh" in argumente:
        # Dieselbe Quelle wie der Denkweg: Anbieter → Schluessel UND Adresse. Vorher stand
        # hier eine zweite, handverdrahtete Liste, die nur `os.environ` las — eine
        # Installation mit Geheimnisdatei bekam damit „nichts zurueckgegeben" statt ihrer
        # Modelle, und die Adresse eines eigenen Gateways fehlte ganz.
        konfig = load_config()
        bestand = konfig.api_credentials
        specs = {
            info.slug: info
            for info in (*catalog.PROVIDERS, *konfig.custom_provider_infos)
        }
        # Die vier historischen Ziele bleiben dabei, damit bestehende Ausgaben und
        # Konfigurationen unveraendert weiterarbeiten. Zusaetzlich werden alle wirklich
        # konfigurierten Routen gefragt — darunter lokale, absichtlich schluessellose
        # OpenAI-kompatible Server und Betreiber-Anbieter mit exaktem Basispfad.
        slugs = tuple(dict.fromkeys((*ENDPOINTS, *bestand.routes)))
        ergebnisse = refresh(
            slugs,
            keys={slug: route.api_key for slug, route in bestand.routes.items()},
            base_urls={slug: route.base_url for slug, route in bestand.routes.items()},
            provider_specs=specs,
            path=pfad, get=get,
        )
        schreiben(render(ergebnisse, load_cache(pfad)))

    # Ohne die ganze Konfiguration: dieser Befehl laeuft auch ohne Token und Allowlist,
    # und die Overrides gehoeren zur Anzeige des Katalogs, nicht zum Dienst.
    fehler = ""
    try:
        overrides = load_model_overrides()
    except ValueError as problem:
        overrides, fehler = modelinfo.EMPTY, str(problem)

    voll = merged(_catalog(), load_cache(pfad))
    overrides = modelinfo.reconcile(
        overrides, (model for anbieter in voll.providers for model in anbieter.models)
    )
    schreiben("\n")
    for anbieter in voll.providers:
        schreiben(f"  {anbieter.slug:16} {len(anbieter.models):3} models   {anbieter.label}\n")
        for modell in anbieter.models:
            eintrag = overrides.get(modell)
            if eintrag is not None:
                info = modelinfo.merge(catalog.model_info(modell), eintrag)
                schreiben(f"  {'':16} {modell}: {modelinfo.describe(info)}\n")
    if overrides.entries:
        schreiben(f"\n  {len(overrides.entries)} model override(s) from {modelinfo.ENV_VAR}\n")
    for grund in overrides.dropped:
        schreiben(f"  {SYM_FAIL} {grund}\n")
    if fehler:
        schreiben(f"  {SYM_FAIL} {fehler}\n")
    schreiben(f"\n  cache: {pfad}"
              f"{' — empty, run `talos models --refresh`' if not pfad.is_file() else ''}\n\n")
    return 1 if fehler else 0


__all__ = [
    "CACHE_TTL_S",
    "ENDPOINTS",
    "MAX_MODELS",
    "MAX_NAME_CHARS",
    "Fetched",
    "clean_names",
    "fetch",
    "fresh_models",
    "is_model_id",
    "known_model_ids",
    "load_cache",
    "merged",
    "refresh",
    "render",
    "run_models",
    "save_cache",
]
