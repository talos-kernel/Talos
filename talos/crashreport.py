"""Absturzmeldungen an GlitchTip — Struktur, nie Inhalt.

Ein Agent sieht die Nachrichten des Betreibers, seine Prompts, die Argumente jedes
Werkzeugs und die Zugangsdaten, mit denen er arbeitet. Ein gewoehnlicher Sentry-Client
sammelt genau das ein: lokale Variablen je Frame, Breadcrumbs, Request-Koerper. Fuer
Talos waere das eine Exfiltration mit Logo — deshalb hier kein SDK, sondern ein
schmaler eigener Melder, der nur weitergibt, was zur Fehlersuche wirklich noetig ist:

  * der Typ der Ausnahme,
  * die Code-Stellen (Datei relativ zum Repo, Funktion, Zeile),
  * eine geschwaerzte, gekuerzte Meldung.

Was NIE das Geraet verlaesst: lokale Variablen, Verlauf, Prompts, Werkzeug-Argumente,
Umgebungsvariablen, der Hostname, absolute Pfade unterhalb von $HOME.

Aus: ohne `TALOS_GLITCHTIP_DSN` passiert gar nichts — kein Socket, kein Thread.
Eine Meldung darf den Agenten nie stoeren: jeder Fehler im Melden wird geschluckt.
"""
from __future__ import annotations

import os
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

DSN_ENV = "TALOS_GLITCHTIP_DSN"
TIMEOUT_SECONDS = 3.0
MAX_MESSAGE_CHARS = 180
MAX_FRAMES = 30

_PLACEHOLDER = "«geschwaerzt»"

# Reihenfolge zaehlt: die spezifischen Zuweisungen zuerst, damit „token=abc" nicht nur
# als langer Bezeichner, sondern als benanntes Geheimnis erkannt wird.
_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Zugangsdaten in einer URL: https://user:pass@host -> https://host
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1"),
    # Benannte Geheimnisse in Query, JSON oder Kommandozeile. Der fuehrende Teil
    # `[A-Za-z][A-Za-z0-9_-]*` ist noetig, weil `\btoken` in „telegram_token" nicht
    # greift: der Unterstrich ist ein Wortzeichen, es gibt dort keine Wortgrenze.
    (re.compile(
        r"(?i)\b(?:[A-Za-z][A-Za-z0-9_-]*[_-])?"
        r"(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password|passwd|secret|token|bearer|dsn|credential)"
        r"[A-Za-z0-9_-]*\s*[:=]\s*[\"']?[^\s\"',;)}\]]+"),
     _PLACEHOLDER),
    # freistehende lange, undurchsichtige Zeichenketten (Tokens, Hashes, Schluessel)
    (re.compile(r"\b[A-Za-z0-9_\-]{24,}\b"), _PLACEHOLDER),
    # Query-Strings koennen alles Moegliche tragen
    (re.compile(r"\?[^\s]{4,}"), "?" + _PLACEHOLDER),
)


def redact(text: str) -> str:
    """Geheimnisse und Heimatpfade aus einem Text entfernen.

    Bewusst grob: lieber eine Meldung, die zu wenig sagt, als eine, die ein Token
    in ein fremdes System traegt. Was hier durchrutscht, ist ein Sicherheitsfehler —
    redteam.py haelt Faelle dagegen.
    """
    if not text:
        return ""
    home = os.path.expanduser("~")
    if home and home != "/":
        text = text.replace(home, "~")
    for muster, ersatz in _SCRUBBERS:
        text = muster.sub(ersatz, text)
    text = " ".join(text.split())
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "…"
    return text


def _relative(filename: str, repo_root: str) -> str:
    """Datei relativ zum Repo. Alles ausserhalb wird auf den Basisnamen gekuerzt —
    ein absoluter Pfad verraet Benutzernamen und Verzeichnisstruktur."""
    try:
        if repo_root and os.path.commonpath([filename, repo_root]) == repo_root:
            return os.path.relpath(filename, repo_root)
    except (ValueError, OSError):
        pass
    return os.path.basename(filename)


def _frames(exc: BaseException, repo_root: str) -> list[dict[str, Any]]:
    """Nur Ort und Name. Ausdruecklich KEIN `vars` — dort steckt der Inhalt."""
    roh = traceback.extract_tb(exc.__traceback__)[-MAX_FRAMES:]
    return [
        {
            "filename": _relative(f.filename, repo_root),
            "function": f.name,
            "lineno": f.lineno,
            "in_app": True,
        }
        for f in roh
    ]


def parse_dsn(dsn: str) -> tuple[str, str] | None:
    """DSN -> (store-URL, oeffentlicher Schluessel). Unbrauchbares ergibt None."""
    m = re.match(r"^(https?)://([0-9a-zA-Z]+)@([^/]+)/(\d+)$", (dsn or "").strip())
    if not m:
        return None
    schema, key, host, projekt = m.groups()
    return f"{schema}://{host}/api/{projekt}/store/", key


class CrashReporter:
    """Meldet Abstuerze — oder tut nichts, wenn kein DSN gesetzt ist."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        send: Callable[[str, dict[str, str], dict[str, Any]], None] | None = None,
        release: str = "",
        repo_root: str = "",
        environment: str = "production",
    ) -> None:
        roh = dsn if dsn is not None else os.environ.get(DSN_ENV, "")
        zerlegt = parse_dsn(roh) if roh else None
        self._url, self._key = zerlegt if zerlegt else ("", "")
        self._send = send
        self._release = release
        self._environment = environment
        self._repo_root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._key)

    def payload(self, exc: BaseException, *, where: str) -> dict[str, Any]:
        """Der Koerper, so wie er das Geraet verlaesst. Oeffentlich, damit Tests und
        redteam.py genau das pruefen koennen, was gesendet wird."""
        return {
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "platform": "python",
            "level": "error",
            "logger": "talos",
            "release": self._release,
            "environment": self._environment,
            # Bewusst konstant: der echte Hostname ist eine Information ueber den
            # Betreiber, nicht ueber den Fehler.
            "server_name": "talos",
            "transaction": redact(where),
            "exception": {
                "values": [
                    {
                        "type": type(exc).__name__,
                        "value": redact(str(exc)),
                        "stacktrace": {"frames": _frames(exc, self._repo_root)},
                    }
                ]
            },
        }

    def report(self, exc: BaseException, *, where: str) -> bool:
        """True, wenn eine Meldung rausging. Wirft nie."""
        if not self.enabled:
            return False
        try:
            koerper = self.payload(exc, where=where)
            kopf = {
                "Content-Type": "application/json",
                "X-Sentry-Auth": (
                    "Sentry sentry_version=7, "
                    f"sentry_key={self._key}, sentry_client=talos-crashreport/1.0"
                ),
            }
            if self._send is not None:
                self._send(self._url, kopf, koerper)
                return True
            import requests

            requests.post(self._url, headers=kopf, json=koerper, timeout=TIMEOUT_SECONDS)
            return True
        except Exception:
            # Ein Melder, der den Agenten mit in den Abgrund zieht, ist schlimmer als
            # keiner. Hier endet jeder Fehler.
            return False
