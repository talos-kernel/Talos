"""Operator-owned CLI-Anything-Harness-Registry fuer das cli_anything-Werkzeug.

Warum sie existiert: CLI-Anything-Harnesses sind pip-installierte CLIs von
Dritten (n8n, LibreOffice, Obsidian, …). Welche davon der Agent ueberhaupt
aufrufen darf, ist eine Betreiber-Entscheidung und steht deklarativ in
`data/cli-anything.json` (neben mcp-servers.json: gleiche Konvention, gleiche
Fail-closed-Haertung). Das Modell nennt nur Harness-NAMEN und ein Subcommand
aus der Allowlist — niemals Pfade, Pakete oder Versionen. Die ausfuehrbare
Wahrheit (welches Binary, gepinnt auf welche Version mit welchem Hash) liegt
ausschliesslich in dieser Datei.

Haertung wie bei der MCP-Registry (`mcpservers.py`): Pflicht-`version`,
fail-closed LEER bei jedem Datei-Problem, harte Byte-/Felddeckel, ungueltige
Eintraege fallen einzeln heraus, der Rest bleibt lesbar. Zusaetzliche, hier
geschaerfte Regeln:

* `command` muss ein ABSOLUTER Pfad sein. Ein relativer Befehl loeste sich
  gegen PATH auf — und PATH im Sandkasten ist nicht der Ort, an dem ein
  gepinntes Werkzeug gesucht werden darf.
* `package` + `version` + `sha256` sind PFLICHT: der Hash des pip-Archivs
  dokumentiert, welche Bytes der Betreiber geprueft hat (gepinnt, gehasht,
  OSV-gescannt — die Regel steht in der CLAUDE.md). Ein Eintrag ohne Pinning
  ist kein kuratierter Harness, sondern ein ungeprueftes Programm.
* `subcommands` ist eine Allowlist, nie ein Muster: was nicht ausdruecklich
  gelistet ist, ist DENY — der Kernel stimmt nie ueber eine Operation ab,
  die der Betreiber nicht benannt hat.
* Ein `env`-Feld ist HART verboten und kostet den ganzen Eintrag — dieselbe
  Credential-Regel wie bei den MCP-Servern: der Harness erbt exakt das
  minimierte Sandbox-Env, ein zweiter Credential-Weg wird nicht aufgemacht.

Sprache: Kommentare deutsch, ausgegebene Texte englisch (Haus-Regel).
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

__all__ = [
    "CLI_ANYTHING_REGISTRY_ENV",
    "DEFAULT_REGISTRY",
    "HARNESS_NAME",
    "MAX_ARG_CHARS",
    "MAX_ARGS",
    "MAX_FILE_BYTES",
    "MAX_HARNESSES",
    "MAX_SUBCOMMAND_CHARS",
    "MAX_SUBCOMMANDS",
    "MAX_TIMEOUT_S",
    "CliAnythingError",
    "Harness",
    "HarnessRegistry",
    "build_command",
    "load",
    "registry_path",
    "subcommand_ok",
    "validated",
]

CLI_ANYTHING_REGISTRY_ENV = "TALOS_CLI_ANYTHING_REGISTRY"

# An derselben Ableitung verankert wie policy.INSTALL_DIR, nie als Zeichenkette
# geraten — aber bewusst NICHT aus config.py/policy.py importiert: der Kernel
# (policy.py) laedt dieses Modul, und sandbox.py laedt policy.py. Ein Import der
# Konfigurationsschicht von hier waere der Anfang eines Zyklus. Beide Ableitungen
# muessen uebereinstimmen; ein Test in tests/test_clianything.py haelt das fest.
DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "data" / "cli-anything.json"

# Die Datei ist deklarativ und klein; was groesser ist, ist keine Konfiguration
# mehr, sondern ein Unfall oder ein Angriff — fail-closed leer.
MAX_FILE_BYTES = 32 * 1024
MAX_HARNESSES = 32
MAX_SUBCOMMANDS = 32
MAX_SUBCOMMAND_CHARS = 64
MAX_ARGS = 32
MAX_ARG_CHARS = 400
MAX_COMMAND_CHARS = 400
MAX_PACKAGE_CHARS = 128
MAX_VERSION_CHARS = 64
# Haertes Maximum der Laufzeit — der Eintrag darf weniger verlangen, nie mehr.
MAX_TIMEOUT_S = 120
DEFAULT_TIMEOUT_S = 60

# Harness-Namen werden zu Kernel-Gruenden und Log-Zeilen — ein enger
# Zeichenvorrat, keine Ueberraschungen (dieselbe Regel wie bei Servernamen).
HARNESS_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
# Subcommands sind CLI-Woerter wie `start` oder `export:workflow` — und bei
# git-artig verschachtelten CLIs (cli-anything-n8n: `workflow list`) auch zwei
# Token, durch GENAU ein Leerzeichen getrennt. Nie mehr: ein drittes Token
# waere schon ein Satz, und Whitespace/Shell-Metazeichen gehoeren lexikalisch
# nicht hinein. Der Runner zerlegt die zwei Token wieder in zwei argv-Elemente.
SUBCOMMAND_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,62}")


def subcommand_ok(wert: object) -> bool:
    """Ein Subcommand ist ein oder zwei enge Token, genau ein Leerzeichen dazwischen."""
    if not isinstance(wert, str) or not wert or len(wert) > MAX_SUBCOMMAND_CHARS:
        return False
    teile = wert.split(" ")
    return 1 <= len(teile) <= 2 and all(SUBCOMMAND_TOKEN.fullmatch(t) for t in teile)
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Harness:
    """Ein freigeschalteter CLI-Anything-Harness. `network` oeffnet dem Lauf das
    Netz in der Sandbox (Vorgabe: aus); `timeout` ist auf MAX_TIMEOUT_S gedeckelt."""
    name: str
    command: str
    package: str
    version: str
    sha256: str
    subcommands: tuple[str, ...]
    network: bool = False
    timeout: int = DEFAULT_TIMEOUT_S


class CliAnythingError(ValueError):
    """Ein Harness-Aufruf wird nicht gestartet. Der Text ist der ehrliche Grund."""


def _harness(item: object) -> Harness | None:
    """Ein Eintrag oder None. Ungueltig faellt EINZELN heraus — ein kaputter
    Nachbar macht die guten Eintraege nicht unlesbar (entities-Konvention)."""
    if not isinstance(item, dict):
        return None
    if "env" in item:
        # Hart ablehnen, nicht still ueberlesen: ein "env"-Feld waere ein zweiter,
        # ungepruefter Credential-Weg in den Sandkasten (MCP-Regel).
        return None
    name = item.get("name")
    if not isinstance(name, str) or not HARNESS_NAME.fullmatch(name):
        return None
    command = item.get("command")
    if (not isinstance(command, str) or len(command) > MAX_COMMAND_CHARS
            or not os.path.isabs(command)):
        return None
    package = item.get("package")
    if not isinstance(package, str) or not package or len(package) > MAX_PACKAGE_CHARS:
        return None
    version = item.get("version")
    if not isinstance(version, str) or not version or len(version) > MAX_VERSION_CHARS:
        return None
    sha256 = item.get("sha256")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        return None
    subcommands = item.get("subcommands")
    if (not isinstance(subcommands, list) or not subcommands
            or len(subcommands) > MAX_SUBCOMMANDS
            or not all(subcommand_ok(s) for s in subcommands)):
        return None
    network = item.get("network", False)
    if not isinstance(network, bool):
        return None
    timeout = item.get("timeout", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return None
    return Harness(
        name=name,
        command=command,
        package=package,
        version=version,
        sha256=sha256,
        subcommands=tuple(dict.fromkeys(subcommands)),
        network=network,
        timeout=max(1, min(timeout, MAX_TIMEOUT_S)),
    )


class HarnessRegistry:
    """Die gelesene Registry. Namen sind eindeutig; der erste Eintrag gewinnt,
    wie bei den MCP-Servern — ein versehentliches Duplikat ueberschreibt nicht."""

    def __init__(self, harnesses: Sequence[Harness] = ()) -> None:
        eindeutig: dict[str, Harness] = {}
        for harness in harnesses[:MAX_HARNESSES]:
            eindeutig.setdefault(harness.name, harness)
        self.harnesses = tuple(eindeutig.values())
        self._by_name = dict(eindeutig)

    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    def get(self, name: str) -> Harness | None:
        return self._by_name.get(name)

    @classmethod
    def from_mapping(cls, payload: object) -> "HarnessRegistry":
        if (not isinstance(payload, Mapping) or payload.get("version") != 1
                or not isinstance(payload.get("harnesses"), list)):
            return cls()
        return cls(tuple(filter(None, (_harness(item) for item in payload["harnesses"]))))

    @classmethod
    def from_path(cls, path: Path) -> "HarnessRegistry":
        """Fail-closed: jedes Problem — fehlend, zu gross, kaputt — ist eine
        LEERE Registry, nie ein Fehler und nie ein geratener Teilststand."""
        try:
            candidate = Path(path)
            if not candidate.is_file() or candidate.stat().st_size > MAX_FILE_BYTES:
                return cls()
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            return cls.from_mapping(payload)
        except (OSError, UnicodeError, ValueError):
            return cls()


def registry_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """Der Pfad zur Registry — aus der Umgebung gelesen, nie aus Modellargumenten.

    Der Kernel liest die Umgebung SELBST (das `_config_files`-Muster): ein Gate,
    das die Konfigurationsschicht fragte, schuetzte erst, nachdem sie gelesen
    wurde. Ein gesetzter Override muss absolut sein — ein relativer Pfad loeste
    sich gegen das Arbeitsverzeichnis des Prozesses auf und liesse sich von dort
    tauschen; er ist keine Registry, sondern ein leerer Befund (fail-closed).
    """
    quelle = os.environ if environ is None else environ
    roh = str(quelle.get(CLI_ANYTHING_REGISTRY_ENV, "")).strip()
    if not roh:
        return DEFAULT_REGISTRY
    kandidat = Path(roh).expanduser()
    return kandidat if kandidat.is_absolute() else None


def load(environ: Mapping[str, str] | None = None) -> HarnessRegistry:
    """Die Registry dieses Laufs — fail-closed leer bei jedem Problem."""
    pfad = registry_path(environ)
    return HarnessRegistry() if pfad is None else HarnessRegistry.from_path(pfad)


def validated(
    args: object, environ: Mapping[str, str] | None = None
) -> tuple[Harness, str, tuple[str, ...]]:
    """Harness, Subcommand und zusaetzliche Argumente pruefen — jede Weigerung
    vor dem ersten Byte. Der Kernel hat laengst geurteilt; der Runner baut die
    Regel nicht nach, er prueft gegen DIESELBE Registry (skill_write-Muster)."""
    felder = getattr(args, "get", None)
    if felder is None:
        raise CliAnythingError("refused: cli_anything needs an argument object")
    name = str(args.get("name") or "").strip()  # type: ignore[union-attr]
    subcommand = str(args.get("subcommand") or "").strip()  # type: ignore[union-attr]
    registry = load(environ)
    if not registry.names():
        raise CliAnythingError(
            "refused: no harness registry — the operator points "
            f"{CLI_ANYTHING_REGISTRY_ENV} at a valid cli-anything.json"
        )
    harness = registry.get(name)
    if harness is None:
        raise CliAnythingError(
            f"refused: harness {name!r} is not in the operator's registry "
            f"({', '.join(sorted(registry.names())) or 'none'})"
        )
    if not subcommand or subcommand not in harness.subcommands:
        raise CliAnythingError(
            f"refused: subcommand {subcommand!r} is outside the allowlist of "
            f"{name!r} ({', '.join(harness.subcommands)})"
        )
    extra = args.get("args", [])  # type: ignore[union-attr]
    if (not isinstance(extra, list) or len(extra) > MAX_ARGS
            or not all(isinstance(a, str) and len(a) <= MAX_ARG_CHARS for a in extra)):
        raise CliAnythingError(
            f"refused: args must be a list of at most {MAX_ARGS} strings "
            f"(each at most {MAX_ARG_CHARS} characters)"
        )
    return harness, subcommand, tuple(extra)


def build_command(harness: Harness, subcommand: str, extra: Sequence[str]) -> str:
    """Die lokale Kommandozeile. `shlex.join` macht jedes Modell-Argument zu
    GENAU einem argv-Element — die Shell im Sandkasten kann daraus kein zweites
    Kommando gewinnen (dieselbe Bauart wie `remoteexec.build_command`). Ein
    verschachteltes Subcommand (`workflow list`) wird an seinem einen
    Leerzeichen wieder in zwei argv-Elemente zerlegt."""
    return shlex.join([harness.command, *subcommand.split(" "), *extra])
