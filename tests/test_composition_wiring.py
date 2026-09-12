"""Was der Conductor annimmt, muss die Kompositionswurzel auch uebergeben.

CLAUDE.md, Falle 7: „Eine gruene Suite kann einen kaputten Dienst uebersehen." Hier ist
die Variante, die am 12.09. wirklich passiert ist — und zwar zweimal in derselben Datei:

`Conductor` nimmt `supports_files` und `cleanup_sent_media`. `config.py` liest
`TALOS_CLEANUP_SENT_MEDIA` aus der Umgebung. `telegram.py` kann `supports_files`
beantworten. Alles vorhanden, alles getestet — nur uebergab `__main__.py` beide Felder
nicht. Weil sie Vorgabewerte haben (`None`, `False`), lief alles weiter: kein Absturz,
keine Warnung, 2415 gruene Tests. Der Betreiber setzte `TALOS_CLEANUP_SENT_MEDIA=1` und
bekam eine Einstellung ohne Wirkung. Die veroeffentlichte 0.19.1 verdrahtete beide.

Der Test liest deshalb die echte `Conductor(...)`-Konstruktion aus `__main__.py` mit
`ast` — nicht mit `import`, denn ein Import beweist bei dieser Fehlerklasse nichts
(CLAUDE.md: „`import` beweist nichts"). Er verlangt nicht, dass JEDES Feld uebergeben
wird; er verlangt es fuer die, bei denen ein Weglassen still eine Faehigkeit abschaltet.
"""
import ast
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WURZEL))

# Felder, deren Fehlen nicht auffaellt, weil ein Vorgabewert einspringt — und deren
# Fehlen genau deshalb eine Faehigkeit still abschaltet. Wer hier etwas ergaenzt, sagt
# damit: „ohne diese Zeile ist das Feature aus, ohne dass es jemand merkt."
PFLICHTFELDER = ("supports_files", "cleanup_sent_media", "send_file", "send_structured")


def _conductor_aufruf() -> ast.Call:
    baum = ast.parse((WURZEL / "talos" / "__main__.py").read_text(encoding="utf-8"))
    for knoten in ast.walk(baum):
        if (
            isinstance(knoten, ast.Call)
            and isinstance(knoten.func, ast.Name)
            and knoten.func.id == "Conductor"
        ):
            return knoten
    raise AssertionError("__main__.py baut keinen Conductor mehr — dann stimmt hier nichts")


@pytest.mark.parametrize("feld", PFLICHTFELDER)
def test_the_composition_root_forgets_a_capability_silently(feld: str) -> None:
    uebergeben = {kw.arg for kw in _conductor_aufruf().keywords}
    assert feld in uebergeben, (
        f"`{feld}` steht im Conductor, wird aber in __main__.py nicht uebergeben. "
        "Der Vorgabewert springt ein, die Faehigkeit ist still aus."
    )


def test_every_required_field_really_exists_on_the_conductor() -> None:
    """Gegenbeleg: sonst koennte der Test oben auf ausgedachte Namen bestehen."""
    from talos.conductor import Conductor

    felder = set(getattr(Conductor, "__dataclass_fields__", {}) or {})
    assert felder, "Conductor ist kein dataclass mehr — die Pruefung oben braucht neue Augen"
    for feld in PFLICHTFELDER:
        assert feld in felder, f"{feld} gibt es am Conductor gar nicht (mehr)"


def test_the_operator_setting_reaches_the_conductor() -> None:
    """`cleanup_sent_media` kommt aus der Umgebung — der Weg muss ganz durchgehen."""
    from talos import config as config_modul

    quelle = (WURZEL / "talos" / "config.py").read_text(encoding="utf-8")
    assert "TALOS_CLEANUP_SENT_MEDIA" in quelle, "die Einstellung wird nicht mehr gelesen"
    assert hasattr(config_modul.TalosConfig, "__dataclass_fields__")
    assert "cleanup_sent_media" in config_modul.TalosConfig.__dataclass_fields__

    werte = {
        kw.arg: kw.value
        for kw in _conductor_aufruf().keywords
        if kw.arg == "cleanup_sent_media"
    }
    ausdruck = ast.unparse(werte["cleanup_sent_media"])
    assert ausdruck == "config.cleanup_sent_media", (
        f"cleanup_sent_media kommt aus {ausdruck!r} statt aus der Konfiguration — "
        "dann entscheidet nicht mehr der Betreiber"
    )


# --- Der Bot-Token darf nie ins append-only Log ------------------------------------
def test_a_channel_error_carries_the_bot_token_into_the_log() -> None:
    """Der Fehlertext eines Kanals traegt die URL — und die traegt den Token.

    Das Log ist append-only und hash-verkettet: eine Zeile, die den Token enthaelt,
    laesst sich nicht mehr entfernen, ohne die Kette zu brechen. Also muss die
    Redaktion VOR dem Schreiben greifen.
    """
    from talos.__main__ import _ohne_token

    # Bewusst KEIN echt aussehender Wert: er traegt die Form (bot<ziffern>:<rest>),
    # die der Filter erkennen muss, und sonst nichts. Ein realistischer
    # Platzhalter waere ein Fund fuer scripts/check-public-hygiene.py — zu Recht.
    echt = "000000000:NICHT-ECHT-NUR-DIE-FORM"
    meldung = (
        f"HTTPSConnectionPool: 409 Conflict for url "
        f"https://api.telegram.org/bot{echt}/getUpdates"
    )
    sauber = _ohne_token(meldung)
    assert echt not in sauber, "der Token steht noch in der Zeile"
    assert "NICHT-ECHT-NUR-DIE-FORM" not in sauber
    # Die Meldung muss lesbar bleiben — eine unlesbare hilft bei der Fehlersuche nicht.
    assert "409 Conflict" in sauber and "api.telegram.org" in sauber
    assert "bot***" in sauber


def test_the_redaction_eats_an_ordinary_message() -> None:
    """Gegenbeleg: was keinen Token enthaelt, bleibt unveraendert."""
    from talos.__main__ import _ohne_token

    for harmlos in ("connection reset by peer", "IMAP login failed", "", "bot ohne token"):
        assert _ohne_token(harmlos) == harmlos


def test_the_composition_root_writes_the_raw_error() -> None:
    """Die Redaktion muss an der Schreibstelle haengen, nicht irgendwo daneben."""
    quelle = (WURZEL / "talos" / "__main__.py").read_text(encoding="utf-8")
    zeile = next(z for z in quelle.splitlines() if '"channel.error"' in z)
    block = quelle[quelle.index(zeile):quelle.index(zeile) + 260]
    assert "_ohne_token(" in block, "channel.error schreibt den Fehlertext ungefiltert"


def test_any_error_text_reaches_the_log_unredacted() -> None:
    """Die allgemeine Regel statt Stelle-fuer-Stelle-Flickerei.

    Am 12.09. wurde `channel.error` repariert — und `worker error` schrieb den
    Fehlertext zwei Bildschirmseiten weiter weiterhin ungefiltert ins Log. Beim
    naechsten `log.append(Event(...))` mit einer Fehlermeldung faengt dieser Test das,
    statt darauf zu warten, dass jemand die Stelle bemerkt. Wer eine Fehlermeldung
    wirklich roh braucht, muss das hier bewusst eintragen — und begruenden.
    """
    baum = ast.parse((WURZEL / "talos" / "__main__.py").read_text(encoding="utf-8"))
    roh: list[str] = []
    for knoten in ast.walk(baum):
        if not (isinstance(knoten, ast.Call) and isinstance(knoten.func, ast.Name)
                and knoten.func.id == "Event"):
            continue
        quelle = ast.unparse(knoten)
        if "str(error)" in quelle and "_ohne_token" not in quelle:
            roh.append(quelle[:90])
    assert not roh, (
        "Ein Fehlertext geht ungefiltert ins append-only Log — er kann den Bot-Token "
        f"tragen und ist danach nicht mehr entfernbar: {roh}"
    )


def test_the_model_picker_can_never_see_a_newly_enabled_model() -> None:
    """`/model --refresh` braucht `refresh_registry` — sonst zeigt es den Bootstand."""
    baum = ast.parse((WURZEL / "talos" / "__main__.py").read_text(encoding="utf-8"))
    for knoten in ast.walk(baum):
        if (isinstance(knoten, ast.Call) and isinstance(knoten.func, ast.Name)
                and knoten.func.id == "ModelPicker"):
            uebergeben = {kw.arg for kw in knoten.keywords}
            assert "refresh_registry" in uebergeben, (
                "ModelPicker bekommt kein refresh_registry — `--refresh` liefert still "
                "den Katalogstand vom Systemstart"
            )
            return
    raise AssertionError("__main__.py baut keinen ModelPicker mehr")
