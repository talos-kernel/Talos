"""Der Melder darf Struktur tragen, nie Inhalt."""
import json
import os

import pytest

from talos.crashreport import CrashReporter, parse_dsn, redact

GUELTIG = "https://c2921b0fabcdef@glitchtip.example.ch/21"


def _fehler(nachricht: str) -> Exception:
    """Eine Ausnahme MIT echter Spur — extract_tb braucht ein __traceback__."""
    try:
        raise ValueError(nachricht)
    except ValueError as e:
        return e


class _Sammler:
    def __init__(self) -> None:
        self.gesendet: list[tuple[str, dict, dict]] = []

    def __call__(self, url, kopf, koerper) -> None:
        self.gesendet.append((url, kopf, koerper))


# --- Aus, solange kein DSN dasteht ------------------------------------------------
def test_without_a_dsn_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("TALOS_GLITCHTIP_DSN", raising=False)
    sammler = _Sammler()
    melder = CrashReporter(send=sammler)
    assert melder.enabled is False
    assert melder.report(_fehler("x"), where="test") is False
    assert sammler.gesendet == []


def test_a_broken_dsn_switches_the_reporter_off():
    for muell in ["nonsense", "https://glitchtip.example.ch/21", "", "http://@host/1"]:
        assert CrashReporter(dsn=muell).enabled is False, muell


def test_a_valid_dsn_becomes_the_store_url():
    assert parse_dsn(GUELTIG) == (
        "https://glitchtip.example.ch/api/21/store/",
        "c2921b0fabcdef",
    )
    assert CrashReporter(dsn=GUELTIG).enabled is True


# --- Was NICHT mitgeht ------------------------------------------------------------
def test_frames_carry_no_local_variables():
    """`vars` je Frame ist der Weg, auf dem ein SDK Prompts und Tokens einsammelt."""
    melder = CrashReporter(dsn=GUELTIG)

    def tief():
        geheim = "sk-ant-api03-NIEMALS-IN-DIE-MELDUNG"  # noqa: F841
        raise RuntimeError("kaputt")

    try:
        tief()
    except RuntimeError as e:
        koerper = melder.payload(e, where="test")

    rahmen = koerper["exception"]["values"][0]["stacktrace"]["frames"]
    assert rahmen, "ohne Rahmen bewiese der Test nichts"
    for r in rahmen:
        assert "vars" not in r
    assert "NIEMALS" not in json.dumps(koerper)


def test_a_secret_in_the_message_reaches_the_wire():
    melder = CrashReporter(dsn=GUELTIG)
    geheim = "sk-ant-api03-AbCdEf0123456789XYZabcdefGHIJ"
    koerper = melder.payload(_fehler(f"auth failed: Authorization: Bearer {geheim}"), where="t")
    assert geheim not in json.dumps(koerper)


@pytest.mark.parametrize(
    "roh",
    [
        "telegram_token=123456789:AAHxyzABCDEFghijklmnopQRSTUV12345",
        'openai_api_key="sk-proj-9f8e7d6c5b4a3928"',
        "https://nutzer:hunter2@api.example.com/v1",
        "TALOS_GLITCHTIP_DSN=https://c2921b0f@glitchtip.example.ch/21",
    ],
)
def test_named_secrets_are_scrubbed(roh):
    sauber = redact(roh)
    for verboten in ["AAHxyz", "sk-proj", "hunter2", "c2921b0f"]:
        assert verboten not in sauber, f"{verboten!r} blieb in {sauber!r}"


def test_the_operators_home_path_becomes_a_tilde():
    heim = os.path.expanduser("~")
    sauber = redact(f"FileNotFoundError: {heim}/.talos/secrets/telegram.token")
    assert heim not in sauber
    assert "~/.talos/secrets" in sauber


def test_the_hostname_stays_on_the_machine():
    """Der echte Hostname sagt etwas ueber den Betreiber, nichts ueber den Fehler."""
    koerper = CrashReporter(dsn=GUELTIG).payload(_fehler("x"), where="t")
    assert koerper["server_name"] == "talos"
    assert os.uname().nodename not in json.dumps(koerper)


def test_frames_are_relative_to_the_repository():
    koerper = CrashReporter(dsn=GUELTIG).payload(_fehler("x"), where="t")
    for rahmen in koerper["exception"]["values"][0]["stacktrace"]["frames"]:
        assert not os.path.isabs(rahmen["filename"]), rahmen["filename"]


# --- Was mitgehen MUSS, sonst ist der Melder nutzlos -------------------------------
def test_the_report_still_names_the_fault():
    koerper = CrashReporter(dsn=GUELTIG).payload(_fehler("db connection refused"), where="main")
    wert = koerper["exception"]["values"][0]
    assert wert["type"] == "ValueError"
    assert "db connection refused" in wert["value"]
    assert koerper["transaction"] == "main"
    assert wert["stacktrace"]["frames"][-1]["function"] == "_fehler"


def test_a_long_message_is_cut_not_dropped():
    # Bewusst echte Woerter: ein langer Block aus [A-Za-z0-9_-] sieht wie ein Token aus
    # und wird — richtigerweise — ganz geschwaerzt. Hier geht es um die Laengengrenze.
    lang = "die datenbank antwortet nicht mehr " * 200
    koerper = CrashReporter(dsn=GUELTIG).payload(_fehler(lang), where="t")
    wert = koerper["exception"]["values"][0]["value"]
    assert 0 < len(wert) <= 200
    assert wert.startswith("die datenbank")


def test_a_long_opaque_blob_is_redacted_not_merely_cut():
    """Kuerzen allein genuegt nicht: die ersten 180 Zeichen eines Tokens sind ein Token."""
    koerper = CrashReporter(dsn=GUELTIG).payload(_fehler("Z" * 5000), where="t")
    assert koerper["exception"]["values"][0]["value"] == "«geschwaerzt»"


# --- Der Melder darf den Agenten nie mitreissen -----------------------------------
def test_a_failing_transport_never_reaches_the_agent():
    def kaputt(url, kopf, koerper):
        raise OSError("network unreachable")

    melder = CrashReporter(dsn=GUELTIG, send=kaputt)
    assert melder.report(_fehler("x"), where="t") is False


def test_the_auth_header_names_the_key():
    sammler = _Sammler()
    CrashReporter(dsn=GUELTIG, send=sammler).report(_fehler("x"), where="t")
    assert len(sammler.gesendet) == 1
    url, kopf, _ = sammler.gesendet[0]
    assert url == "https://glitchtip.example.ch/api/21/store/"
    assert "sentry_key=c2921b0fabcdef" in kopf["X-Sentry-Auth"]
