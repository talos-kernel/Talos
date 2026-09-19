"""cli_anything — kuratierte CLI-Anything-Harnesses durch dasselbe Gate.

Die Registry ist operator-owned (`data/cli-anything.json`, fail-closed wie
mcp-servers.json): das Modell nennt Harness-Namen + Subcommand, nie Pfade oder
Versionen. Die teuren Fehler, die diese Tests bewachen: ein Harness, den
niemand kuratiert hat, laeuft trotzdem; ein Subcommand ausserhalb der
Allowlist wird zur Freigabefrage statt zu DENY; ein Shell-String aus
Modell-Argumenten wird zu zwei Kommandos; und die Attended-Auto-Freigabe
kippt das „ausnahmslos fragen" (sandbox_required zieht sonst in die
Routineklasse — dieselbe Falle, die remote_exec einmal hatte).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from talos import clianything, config, sandbox, schema, tools
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Principal, Trust
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.eventlog import EventLog
from talos.executor import Executor, Status
from talos.policy import TARGET_EXTRACTORS, PolicyKernel, ToolRequest, Verdict
from talos.snapshot import Snapshotter
from talos.standing import action_key, action_label
from talos.tools import default_manifest

OWNER = Principal("telegram", "100000001")
STRANGER = Principal("telegram", "749908869")

LINUX = sys.platform.startswith("linux")
LIVE_BACKEND = sandbox.select_backend(sandbox.default_backends())
requires_sandbox = pytest.mark.skipif(
    LIVE_BACKEND is None,
    reason=f"no working sandbox backend on {sys.platform}",
)

SECRET_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-leak",
    "OPENAI_API_KEY": "sk-oai-leak",
    "TELEGRAM_BOT_TOKEN": "123:leak",
}

EINTRAG_N8N = {
    "name": "n8n",
    "package": "cli-anything-n8n",
    "version": "1.2.3",
    "sha256": "a" * 64,
    "command": "/bin/echo",
    "subcommands": ["start", "export:workflow"],
    "network": False,
    "timeout": 30,
}


def _registry_datei(tmp_path, payload):
    pfad = tmp_path / "cli-anything.json"
    pfad.write_text(json.dumps(payload), encoding="utf-8")
    return pfad


def _mit_registry(tmp_path, monkeypatch, eintraege=None):
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": eintraege or [EINTRAG_N8N]})
    monkeypatch.setenv(clianything.CLI_ANYTHING_REGISTRY_ENV, str(pfad))
    return pfad


def _kernel() -> PolicyKernel:
    return PolicyKernel(default_manifest(), frozenset({OWNER}))


def _req(args, principal=OWNER):
    return ToolRequest("cli_anything", principal, dict(args))


# --- Registry-Parser: fail-closed wie mcp-servers.json -------------------------

def test_registry_reads_a_valid_file(tmp_path):
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [EINTRAG_N8N, {
        "name": "libreoffice",
        "package": "cli-anything-libreoffice",
        "version": "2.0.0",
        "sha256": "b" * 64,
        "command": "/usr/local/bin/lo-harness",
        "subcommands": ["convert"],
    }]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.names() == frozenset({"n8n", "libreoffice"})
    n8n = reg.get("n8n")
    assert n8n.command == "/bin/echo"
    assert n8n.subcommands == ("start", "export:workflow")
    assert n8n.network is False
    assert n8n.timeout == 30
    # Defaults: Netz aus, Zeitdeckel die Vorgabe.
    assert reg.get("libreoffice").network is False
    assert reg.get("libreoffice").timeout == clianything.DEFAULT_TIMEOUT_S
    assert reg.get("unbekannt") is None


def test_registry_missing_file_is_empty(tmp_path):
    reg = clianything.HarnessRegistry.from_path(tmp_path / "fehlt.json")
    assert reg.names() == frozenset()


def test_registry_broken_json_is_empty(tmp_path):
    pfad = tmp_path / "cli-anything.json"
    pfad.write_text("{kaputt", encoding="utf-8")
    assert clianything.HarnessRegistry.from_path(pfad).names() == frozenset()


def test_registry_wrong_or_missing_version_is_empty(tmp_path):
    for payload in ({}, {"version": 2, "harnesses": [EINTRAG_N8N]},
                    {"harnesses": [EINTRAG_N8N]}, ["kein-objekt"]):
        pfad = _registry_datei(tmp_path, payload)
        assert clianything.HarnessRegistry.from_path(pfad).names() == frozenset()


def test_registry_oversized_file_is_empty(tmp_path):
    eintraege = [dict(EINTRAG_N8N, name=f"harness-{i:03d}") for i in range(400)]
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": eintraege})
    assert pfad.stat().st_size > clianything.MAX_FILE_BYTES
    assert clianything.HarnessRegistry.from_path(pfad).names() == frozenset()


def test_registry_entry_with_env_key_is_discarded(tmp_path):
    """Der harte Credential-Schnitt: ein "env"-Feld waere ein zweiter,
    ungepruefter Weg fuer Geheimnisse in den Sandkasten — der ganze Eintrag
    faellt, die uebrigen bleiben lesbar (dieselbe Regel wie bei MCP)."""
    boese = dict(EINTRAG_N8N, name="boese", env={"API_KEY": "klau-mich"})
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [boese, EINTRAG_N8N]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.names() == frozenset({"n8n"})


def test_registry_rejects_bad_names_and_relative_commands(tmp_path):
    kaputt = [
        dict(EINTRAG_N8N, name="Gross"),                       # Muster: [a-z0-9-]
        dict(EINTRAG_N8N, name="hat_leerzeichen x"),
        dict(EINTRAG_N8N, name="x" * 32),                      # Laengendeckel
        dict(EINTRAG_N8N, name="rel", command="bin/n8n"),      # command muss absolut sein
        dict(EINTRAG_N8N, name="nolang", command="x" * 500),   # Kommando-Deckel
    ]
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [*kaputt, EINTRAG_N8N]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.names() == frozenset({"n8n"})


def test_registry_requires_pinning(tmp_path):
    """Ohne package+version+sha256 ist ein Eintrag kein kuratierter Harness,
    sondern ein ungeprueftes Programm — die drei Felder sind Pflicht."""
    kaputt = [
        {k: v for k, v in EINTRAG_N8N.items() if k != "package"},
        {k: v for k, v in EINTRAG_N8N.items() if k != "version"},
        {k: v for k, v in EINTRAG_N8N.items() if k != "sha256"},
        dict(EINTRAG_N8N, name="kurzerhash", sha256="abcd"),
        dict(EINTRAG_N8N, name="grosshash", sha256="A" * 64),
    ]
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [*kaputt, EINTRAG_N8N]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.names() == frozenset({"n8n"})


def test_registry_rejects_missing_or_bad_subcommands(tmp_path):
    kaputt = [
        {k: v for k, v in EINTRAG_N8N.items() if k != "subcommands"},  # Pflicht
        dict(EINTRAG_N8N, name="leer", subcommands=[]),                # leere Allowlist
        dict(EINTRAG_N8N, name="zuviele", subcommands=[f"s{i}" for i in range(40)]),
        dict(EINTRAG_N8N, name="meta", subcommands=["start", "stop; rm"]),
        dict(EINTRAG_N8N, name="zahl", subcommands=[1]),
    ]
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [*kaputt, EINTRAG_N8N]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.names() == frozenset({"n8n"})


def test_registry_rejects_nonbool_network_and_bad_timeout(tmp_path):
    kaputt = [
        dict(EINTRAG_N8N, name="netztext", network="yes"),
        dict(EINTRAG_N8N, name="zeittext", timeout="60"),
        dict(EINTRAG_N8N, name="zeitbool", timeout=True),
    ]
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [*kaputt, EINTRAG_N8N]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.names() == frozenset({"n8n"})


def test_registry_timeout_is_capped(tmp_path):
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [
        dict(EINTRAG_N8N, timeout=3600), dict(EINTRAG_N8N, name="null", timeout=0),
    ]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.get("n8n").timeout == clianything.MAX_TIMEOUT_S
    assert reg.get("null").timeout == 1


def test_registry_duplicate_names_first_wins(tmp_path):
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [
        EINTRAG_N8N, dict(EINTRAG_N8N, command="/usr/bin/anderes"),
    ]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.get("n8n").command == "/bin/echo"


def test_registry_harness_count_is_capped(tmp_path):
    eintraege = [dict(EINTRAG_N8N, name=f"harness-{i:02d}") for i in range(40)]
    reg = clianything.HarnessRegistry.from_mapping(
        {"version": 1, "harnesses": eintraege})
    assert len(reg.names()) == clianything.MAX_HARNESSES


def test_registry_path_override_must_be_absolute(tmp_path, monkeypatch):
    """Ein relativer Override loeste sich gegen das Arbeitsverzeichnis auf und
    liesse sich von dort tauschen — er ist keine Registry, sondern leer."""
    _mit_registry(tmp_path, monkeypatch)
    assert clianything.load().names() == frozenset({"n8n"})
    monkeypatch.setenv(clianything.CLI_ANYTHING_REGISTRY_ENV, "relative.json")
    assert clianything.load().names() == frozenset()
    monkeypatch.delenv(clianything.CLI_ANYTHING_REGISTRY_ENV)
    # Ohne Override gilt der feste Platz neben den anderen Betreiber-Dateien.
    assert clianything.DEFAULT_REGISTRY == config.DATA_DIR / "cli-anything.json"


# --- Kernel: Registry + Allowlist, ausnahmslos NEEDS_HUMAN ---------------------

def test_kernel_denies_without_registry_env(monkeypatch):
    """requires_env: ohne den Registry-Pfad des Betreibers gibt es keinen Grant."""
    monkeypatch.delenv(clianything.CLI_ANYTHING_REGISTRY_ENV, raising=False)
    out = _kernel().decide(_req({"name": "n8n", "subcommand": "start"}))
    assert out.verdict is Verdict.DENY
    assert clianything.CLI_ANYTHING_REGISTRY_ENV in out.reason


def test_kernel_denies_unknown_harness(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    out = _kernel().decide(_req({"name": "evil", "subcommand": "start"}))
    assert out.verdict is Verdict.DENY
    assert "not in the operator's registry" in out.reason


def test_kernel_denies_subcommand_outside_allowlist(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    out = _kernel().decide(_req({"name": "n8n", "subcommand": "delete:credentials"}))
    assert out.verdict is Verdict.DENY
    assert "outside the harness allowlist" in out.reason


def test_kernel_denies_malformed_names_and_args(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    kernel = _kernel()
    assert kernel.decide(_req({"name": "N8N", "subcommand": "start"})).verdict is Verdict.DENY
    assert kernel.decide(_req({"name": "", "subcommand": "start"})).verdict is Verdict.DENY
    # args muessen eine begrenzte Stringliste sein — kein Shell-String.
    assert kernel.decide(
        _req({"name": "n8n", "subcommand": "start", "args": "rm -rf x"})
    ).verdict is Verdict.DENY
    assert kernel.decide(
        _req({"name": "n8n", "subcommand": "start", "args": [1, 2]})
    ).verdict is Verdict.DENY
    assert kernel.decide(
        _req({"name": "n8n", "subcommand": "start", "args": ["x"] * 40})
    ).verdict is Verdict.DENY


def test_kernel_asks_for_known_harness_and_allowlisted_subcommand(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    out = _kernel().decide(_req({"name": "n8n", "subcommand": "export:workflow",
                                 "args": ["--id", "12"]}))
    assert out.verdict is Verdict.NEEDS_HUMAN
    assert "n8n export:workflow" in out.reason


def test_kernel_denies_strangers_before_the_registry(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    out = _kernel().decide(_req({"name": "n8n", "subcommand": "start"}, principal=STRANGER))
    assert out.verdict is Verdict.DENY


def test_cli_anything_has_a_target_extractor():
    """Ein Werkzeug ohne Extractor ist per Bauart DENY — der Eintrag muss stehen."""
    assert "cli_anything" in TARGET_EXTRACTORS
    assert TARGET_EXTRACTORS["cli_anything"]({"name": "n8n"}) == ()


def test_attended_autoapproval_does_not_cover_a_harness(tmp_path, monkeypatch):
    """sandbox_required zieht ein EXEC-Werkzeug sonst in die Routineklasse der
    Attended-Auto-Freigabe — fuer eine kuratierte Dritt-CLI ist das falsch,
    weil die Sandbox den Prozess begrenzt, nicht seine Wirkung. Dieselbe
    Namens-Ausnahme wie bei remote_exec."""
    _mit_registry(tmp_path, monkeypatch)
    governed = GovernedKernel(
        _kernel(), AutonomyGovernor(5), lambda _c: Trust.FULL,
        attended_autoapprove=True)
    out = governed.decide(_req({"name": "n8n", "subcommand": "start"}))
    assert out.verdict is Verdict.NEEDS_HUMAN


# --- Stehende Regeln: exakt (harness, subcommand) ------------------------------

def test_standing_rule_binds_exact_harness_and_subcommand(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    start = _req({"name": "n8n", "subcommand": "start"})
    export = _req({"name": "n8n", "subcommand": "export:workflow"})
    assert action_key(start) is not None
    assert action_key(start) != action_key(export)
    # Die Datenargumente gehoeren nicht in den Abdruck (write_file-Bindung).
    assert action_key(start) == action_key(
        _req({"name": "n8n", "subcommand": "start", "args": ["--x"]}))
    assert action_label(start) == "cli_anything n8n start"
    # Ohne Harness oder Subcommand gibt es keine stehende Regel.
    assert action_key(_req({"name": "", "subcommand": "start"})) is None
    assert action_key(_req({"name": "n8n"})) is None


# --- Runner: argv-Bau, Env-Leak, Receipt ---------------------------------------

def test_build_command_quotes_every_model_argument():
    harness = clianything.Harness(
        name="n8n", command="/bin/echo", package="p", version="1",
        sha256="a" * 64, subcommands=("start",))
    command = clianything.build_command(harness, "start", ["; rm -rf x", "a b"])
    import shlex
    assert shlex.split(command) == ["/bin/echo", "start", "; rm -rf x", "a b"]


def test_registry_accepts_two_token_subcommand(tmp_path):
    """Git-artig verschachtelte CLIs (cli-anything-n8n: `workflow list`) passen
    in die Allowlist — zwei enge Token, genau ein Leerzeichen."""
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [
        dict(EINTRAG_N8N, subcommands=["workflow list"])]})
    reg = clianything.HarnessRegistry.from_path(pfad)
    assert reg.get("n8n").subcommands == ("workflow list",)


def test_registry_rejects_bad_nested_subcommands(tmp_path):
    kaputt = [
        dict(EINTRAG_N8N, name="doppel", subcommands=["workflow  list"]),    # zwei Leerzeichen
        dict(EINTRAG_N8N, name="drei", subcommands=["workflow list all"]),   # drei Token
        dict(EINTRAG_N8N, name="rand", subcommands=[" list"]),               # Leerraum am Rand
        dict(EINTRAG_N8N, name="meta2", subcommands=["workflow list; rm"]),  # Metazeichen
    ]
    pfad = _registry_datei(tmp_path, {"version": 1, "harnesses": [*kaputt, EINTRAG_N8N]})
    assert clianything.HarnessRegistry.from_path(pfad).names() == frozenset({"n8n"})


def test_build_command_splits_nested_subcommand_into_two_argv():
    harness = clianything.Harness(
        name="n8n", command="/bin/echo", package="p", version="1",
        sha256="a" * 64, subcommands=("workflow list",))
    command = clianything.build_command(harness, "workflow list", ["--json"])
    import shlex
    assert shlex.split(command) == ["/bin/echo", "workflow", "list", "--json"]


def test_kernel_nested_allowlist_is_exact(tmp_path, monkeypatch):
    """`workflow list` deckt `workflow delete` nie — die Allowlist bleibt exakt."""
    _mit_registry(tmp_path, monkeypatch, [dict(EINTRAG_N8N, subcommands=["workflow list"])])
    erlaubt = _kernel().decide(_req({"name": "n8n", "subcommand": "workflow list"}))
    assert erlaubt.verdict is Verdict.NEEDS_HUMAN
    verworfen = _kernel().decide(_req({"name": "n8n", "subcommand": "workflow delete"}))
    assert verworfen.verdict is Verdict.DENY


def test_runner_refuses_unknown_harness_before_any_backend(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    with pytest.raises(clianything.CliAnythingError, match="not in the operator's registry"):
        tools.RUNNERS["cli_anything"](_req({"name": "evil", "subcommand": "start"}))
    with pytest.raises(clianything.CliAnythingError, match="outside the allowlist"):
        tools.RUNNERS["cli_anything"](_req({"name": "n8n", "subcommand": "rm"}))


def test_runner_without_registry_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv(clianything.CLI_ANYTHING_REGISTRY_ENV, str(tmp_path / "fehlt.json"))
    with pytest.raises(clianything.CliAnythingError, match="no harness registry"):
        tools.RUNNERS["cli_anything"](_req({"name": "n8n", "subcommand": "start"}))


@requires_sandbox
def test_runner_receipt_names_harness_and_subcommand(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    out = tools.RUNNERS["cli_anything"](
        _req({"name": "n8n", "subcommand": "export:workflow", "args": ["--id", "12"]}))
    assert out.startswith("rc=0 [")
    assert "n8n export:workflow" in out
    assert "--id 12" in out


@requires_sandbox
def test_the_harness_cannot_see_the_secrets(tmp_path, monkeypatch):
    """Env-Leak: der Harness erbt exakt die Positivliste des Sandkastens."""
    for key, value in SECRET_ENV.items():
        monkeypatch.setenv(key, value)
    # Ein echter Harness ist ein Programm; hier ein minimales, das seine
    # Umgebung ausgibt — derselbe Beweis wie in tests/test_sandbox.py.
    skript = tmp_path / "dumpenv.sh"
    skript.write_text("#!/bin/sh\nenv\n", encoding="utf-8")
    skript.chmod(0o755)
    _mit_registry(tmp_path, monkeypatch, [dict(
        EINTRAG_N8N, command=str(skript.resolve()), subcommands=["start"])])
    out = tools.RUNNERS["cli_anything"](_req({"name": "n8n", "subcommand": "start"}))
    assert out.startswith("rc=0 ["), out
    assert "leak" not in out
    assert "TALOS_SANDBOX=1" in out


@requires_sandbox
def test_injection_in_args_is_one_argv_element(tmp_path, monkeypatch):
    """`"; rm -rf x"` ist ein ARGUMENT, nie ein zweites Kommando: /bin/echo gibt
    es unveraendert zurueck, und kein `x` verschwindet irgendwo."""
    _mit_registry(tmp_path, monkeypatch)
    out = tools.RUNNERS["cli_anything"](
        _req({"name": "n8n", "subcommand": "start", "args": ["; rm -rf x"]}))
    assert "; rm -rf x" in out  # gequotet durchgereicht, nicht ausgefuehrt


# --- Executor-Pipeline: Urteil, Grant, Receipt ----------------------------------

def _executor(tmp_path: Path) -> Executor:
    kernel = _kernel()
    mint = CapabilityMint(kernel)
    return Executor(
        policy=kernel,
        log=EventLog(tmp_path / "ev.db"),
        snapshotter=Snapshotter(tmp_path / "snap"),
        runner=GrantedRunner(mint=mint, runners={"cli_anything": tools.RUNNERS["cli_anything"]}),
        mint=mint,
    )


def test_executor_parks_without_approval_and_runs_nothing(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    ex = _executor(tmp_path)
    out = ex.run(_req({"name": "n8n", "subcommand": "start"}), "run-cli-1")
    assert out.status is Status.NEEDS_HUMAN
    assert not [e for e in ex.log.recent(10) if e["type"] == "exec.result"
                and e["payload"].get("status") == "done"]


def test_executor_denies_unknown_harness_and_runs_nothing(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    ex = _executor(tmp_path)
    out = ex.run(_req({"name": "evil", "subcommand": "start"}), "run-cli-2",
                 human_approved=True)
    # Selbst ein „ja" macht aus DENY nichts — ein Grant kann den Mauern nichts.
    assert out.status is Status.DENIED


def test_approved_run_executes_and_leaves_a_receipt(tmp_path, monkeypatch):
    _mit_registry(tmp_path, monkeypatch)
    ex = _executor(tmp_path)
    out = ex.run(_req({"name": "n8n", "subcommand": "start"}), "run-cli-3",
                 human_approved=True)
    assert out.status is Status.DONE
    assert "n8n start" in out.result
    events = ex.log.recent(20)
    assert any(e["type"] == "grant.issued" for e in events)
    receipt = [e for e in events if e["type"] == "exec.result"][0]
    assert receipt["payload"]["status"] == "done"


# --- Vokabular und Anzeige ------------------------------------------------------

def test_tool_protocol_offers_cli_anything():
    from talos.reasoner import TOOL_PROTOCOL
    zeile = next(z for z in TOOL_PROTOCOL.splitlines() if z.startswith("- cli_anything "))
    assert "operator registry" in zeile and "denied" in zeile


def test_registry_env_key_is_operator_policy():
    """Der Registry-Pfad entscheidet, was laufen darf — `config set` darf ihn
    nicht schreiben (POLICY), aber `config list` muss ihn kennen."""
    eintrag = schema.get(clianything.CLI_ANYTHING_REGISTRY_ENV)
    assert eintrag is not None
    assert eintrag.kind == schema.POLICY
