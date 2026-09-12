"""Eigene Anbieter des Betreibers — benannt, mit ihren Modellen, aus einer Datei.

Ein ausgelieferter Katalog kann nicht kennen, was der Betreiber selbst betreibt: einen
OAuth-Proxy vor einem Abo, einen Modell-Server im Tailnet, ein Gateway einer Firma.
Bisher blieb dafuer nur, den Namen eines fremden Anbieters zu *borgen* — der Lauf stand
dann unter „ollama", obwohl er zu etwas ganz anderem ging. Das ist im Protokoll gelogen
und bei der Fehlersuche teuer.

Hier bekommt so ein Anbieter seinen eigenen Namen, seine eigene Adresse und genau die
Modelle, die der Betreiber genannt hat.

Format (JSON, eine Liste) — bewusst dieselben Felder wie in Hermes' `custom_providers`:

    [{"name": "kimi-oauth",
      "label": "Kimi OAuth",
      "base_url": "http://127.0.0.1:17432/v1",
      "env_key": "KIMI_OAUTH_API_KEY",
      "models": ["k3", "k3-256k"]}]

Drei Grenzen, und alle drei sind Sicherheitsgrenzen, nicht Bequemlichkeit:

1. **Kein eingebauter Name.** Ein Eintrag, der `claude-cli`, `anthropic-api` oder einen
   anderen Katalognamen traegt, wird verworfen. Sonst koennte eine Datei den Weg zu
   Anthropic auf einen fremden Host umbiegen, ohne dass im Protokoll etwas anders aussieht.
2. **Nur http(s), nie mit Zugangsdaten in der Adresse.** `file://` liest Platten,
   `user:pass@host` traegt ein Geheimnis in jede Fehlermeldung.
3. **Fail closed, aber nie toedlich.** Ein kaputter Eintrag faellt weg; eine kaputte
   Datei ergibt eine leere Liste. Der Agent startet — er startet nur ohne diesen Anbieter.

Ein eigener Name ist kein Recht: jeder Zug geht weiter durch denselben Kernel.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .catalog import ProviderInfo, get as catalog_get

__all__ = ["load", "parse", "MAX_MODELS", "MAX_PROVIDERS"]

# Grenzen gegen eine versehentlich riesige Datei — der Katalog wird gelesen, nicht gefiltert.
MAX_PROVIDERS = 32
MAX_MODELS = 128
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _clean_name(value: object) -> str:
    """Kleingeschrieben und eng begrenzt — der Name landet in Env-Variablennamen."""
    name = str(value or "").strip().lower()
    return name if _NAME.fullmatch(name) else ""


def _clean_base_url(value: object) -> str:
    """http(s) ohne Zugangsdaten. Alles andere ist keine Adresse, die wir ansprechen."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        teile = urlsplit(raw)
    except ValueError:
        return ""
    if teile.scheme not in ("http", "https") or not teile.hostname:
        return ""
    if teile.username or teile.password:
        return ""
    return raw.rstrip("/")


def _clean_models(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    namen: list[str] = []
    for eintrag in value[:MAX_MODELS]:
        modell = str(eintrag or "").strip()
        if _MODEL.fullmatch(modell) and modell not in namen:
            namen.append(modell)
    return tuple(namen)


def parse(rohtext: str) -> tuple[ProviderInfo, ...]:
    """JSON-Text -> geprüfte Anbieter. Wirft nie; Unbrauchbares faellt weg."""
    try:
        daten = json.loads(rohtext)
    except (ValueError, TypeError):
        return ()
    if not isinstance(daten, list):
        return ()

    fertig: list[ProviderInfo] = []
    gesehen: set[str] = set()
    for eintrag in daten[:MAX_PROVIDERS]:
        if not isinstance(eintrag, dict):
            continue
        name = _clean_name(eintrag.get("name"))
        if not name or name in gesehen:
            continue
        # ⚠️ Die wichtigste Zeile der Datei: ein eigener Anbieter darf einen eingebauten
        # Namen NIE uebernehmen. Sonst zeigt das Protokoll weiter „claude-cli", waehrend
        # der Zug zu einem fremden Host geht.
        if catalog_get(name) is not None:
            continue
        base_url = _clean_base_url(eintrag.get("base_url"))
        modelle = _clean_models(eintrag.get("models"))
        if not base_url or not modelle:
            continue
        label = str(eintrag.get("label") or name).strip()[:80] or name
        env_key = _clean_env_key(eintrag.get("env_key"))
        gesehen.add(name)
        fertig.append(
            ProviderInfo(
                slug=name,
                label=label,
                auth="api-key" if env_key else "none",
                wire="openai",
                base_url=base_url,
                env_key=env_key,
                models=modelle,
                notes="operator-defined custom provider",
            )
        )
    return tuple(fertig)


def _clean_env_key(value: object) -> str:
    schluessel = str(value or "").strip().upper()
    return schluessel if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", schluessel) else ""


def load(path: str | Path) -> tuple[ProviderInfo, ...]:
    """Datei lesen und pruefen. Fehlt sie oder ist sie unlesbar: leere Liste, kein Wurf."""
    try:
        rohtext = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return ()
    return parse(rohtext)
