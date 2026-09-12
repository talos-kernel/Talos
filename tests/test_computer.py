"""Computer capability boundaries and durable receipts."""
import hashlib
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from talos.autonomy import attended_routine
from talos.channel import Principal
from talos.computer.contract import validate
from talos.computer.egress import public_addresses
from talos.computer.state import Store
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.tools import default_manifest

OWNER = Principal("telegram", "100000001")
ACTION = {"op": "exec", "project": "report", "key": "create-report", "title": "Create report",
          "command": "printf done > report.txt", "checks": []}


@pytest.fixture
def kernel(monkeypatch):
    monkeypatch.setenv("TALOS_COMPUTER_SOCKET", "/run/example/control.sock")
    return PolicyKernel(default_manifest(), frozenset({OWNER}), shell_needs_human=False)


def test_computer_effect_never_inherits_shell_autoapproval(kernel):
    request = ToolRequest("computer_run", OWNER, ACTION)
    assert kernel.decide(request).verdict is Verdict.NEEDS_HUMAN
    assert not attended_routine(request, kernel.manifest.get(request.tool), kernel)


def test_computer_read_is_identity_bound(kernel):
    assert kernel.decide(ToolRequest("computer_status", OWNER, {"op": "status"})).verdict is Verdict.ALLOW
    stranger = Principal("telegram", "200000002")
    assert kernel.decide(ToolRequest("computer_status", stranger, {"op": "status"})).verdict is Verdict.DENY


def test_missing_configuration_fails_closed(kernel, monkeypatch):
    monkeypatch.delenv("TALOS_COMPUTER_SOCKET")
    assert kernel.decide(ToolRequest("computer_run", OWNER, ACTION)).verdict is Verdict.DENY


@pytest.mark.parametrize("changes", [
    {"op": "host_shell"}, {"host": "other-host"}, {"socket": "/run/docker.sock"},
    {"project": "../secrets"}, {"project": "/root"}, {"key": "a\x00b"},
    {"timeout": 121}, {"timeout": True}, {"command": ""},
    {"checks": [{"path": "../private", "sha256": "a" * 64}]},
    {"checks": [{"path": "/etc/passwd", "sha256": "a" * 64}]},
    {"checks": [{"path": "result", "sha256": "anything"}]},
])
def test_hostile_frames_are_denied_before_grant(kernel, changes):
    args = ACTION | changes
    assert kernel.decide(ToolRequest("computer_run", OWNER, args)).verdict is Verdict.DENY


@pytest.mark.parametrize("args", [
    {"op": "exec", "command": "id"}, {"op": "status", "command": "id"},
    {"op": "screenshot", "owner": "another-person"}, {"op": "files", "project": "../x"},
])
def test_read_tool_cannot_be_used_as_action(kernel, args):
    assert kernel.decide(ToolRequest("computer_status", OWNER, args)).verdict is Verdict.DENY


def test_target_is_operator_derived(kernel, monkeypatch):
    monkeypatch.setenv("TALOS_COMPUTER_ROOT", "/work/computer")
    request = ToolRequest("computer_run", OWNER, ACTION)
    assert kernel.guard_targets(request) == ("/work/computer",)


def test_repeated_operation_returns_same_receipt_without_reexecution(tmp_path):
    store = Store(tmp_path / "jobs.db")
    first, created = store.begin("owner", ACTION)
    assert created
    second, created = store.begin("owner", ACTION)
    assert not created and first["id"] == second["id"]
    with pytest.raises(ValueError, match="another request"):
        store.begin("owner", ACTION | {"command": "something else"})
    assert len(store.jobs("owner")) == 1


def test_parallel_duplicate_submissions_only_create_one_job(tmp_path):
    store = Store(tmp_path / "jobs.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.begin("owner", ACTION), range(8)))
    assert sum(created for _, created in results) == 1
    assert len({job["id"] for job, _ in results}) == 1


def test_restart_marks_ambiguous_work_interrupted_and_pauses(tmp_path):
    store = Store(tmp_path / "jobs.db")
    job, _ = store.begin("owner", ACTION)
    store.finish(job["id"], "running")
    store.control("agent")
    restarted = Store(tmp_path / "jobs.db")
    restarted.recover()
    assert restarted.control() == "paused"
    assert restarted.get("owner", job["id"])["state"] == "interrupted"
    assert restarted.begin("owner", ACTION)[1] is False


def test_no_cross_owner_receipt_read_or_unverified_routine(tmp_path):
    store = Store(tmp_path / "jobs.db")
    job, _ = store.begin("owner", ACTION)
    with pytest.raises(ValueError, match="not found"):
        store.get("stranger", job["id"])
    store.finish(job["id"], "needs_review")
    with pytest.raises(ValueError, match="passed"):
        store.save_routine("owner", job["id"], "report")
    store.finish(job["id"], "verified", {"checks": [{"passed": True}]})
    store.save_routine("owner", job["id"], "report")
    assert store.routines("owner")[0]["source_job"] == job["id"]
    assert store.routines("stranger") == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "100.64.0.1",
                                      "169.254.169.254", "::1", "fc00::1", "0.0.0.0"])
def test_proxy_blocks_private_and_special_destinations(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(ValueError, match="blocked"):
        public_addresses("rebind.example", 443)


def test_proxy_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError):
        public_addresses("mixed.example", 443)


def test_proxy_does_not_allow_other_protocol_ports():
    with pytest.raises(ValueError):
        public_addresses("example.com", 22)


def test_shell_cannot_reach_computer_control_socket(kernel):
    from talos.policy import SHELL_FORBIDDEN_PREFIXES
    assert "/run/talos-computer-api" in SHELL_FORBIDDEN_PREFIXES
    request = ToolRequest("run_shell", OWNER, {"command": "nc -U /run/talos-computer-api/control.sock"})
    assert kernel.decide(request).verdict is Verdict.DENY


def test_saved_routine_is_a_private_template_without_reused_key(tmp_path):
    store=Store(tmp_path / "jobs.db")
    job,_=store.begin("owner",ACTION)
    store.finish(job["id"],"verified",{"checks":[{"passed":True}]})
    store.save_routine("owner",job["id"],"report")
    template=store.routine("owner","report")
    assert template["template"]["command"]==ACTION["command"]
    assert "key" not in template["template"]
    assert len(store.jobs("owner"))==1
    with pytest.raises(ValueError,match="not found"):
        store.routine("stranger","report")


def test_proxy_denial_is_not_reported_as_transient_network_error(tmp_path_factory):
    import threading
    from talos.computer.egress import Server,Proxy
    # macOS counts the whole path against a small AF_UNIX address limit.
    endpoint=str(tmp_path_factory.mktemp("ipc") / "p.sock")
    server=Server(endpoint,Proxy)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        for request in (b"GET http://127.0.0.1/ HTTP/1.0\r\n\r\n",b"CONNECT 127.0.0.1:443 HTTP/1.0\r\n\r\n"):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(3);client.connect(endpoint);client.sendall(request)
                assert b" 403 " in client.recv(4096).split(b"\r\n",1)[0]
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def test_headless_kernel_denies_visual_ops_but_keeps_exec_approval(kernel, monkeypatch):
    monkeypatch.setenv("TALOS_COMPUTER_DESKTOP", "0")
    assert kernel.decide(ToolRequest("computer_status", OWNER, {"op":"screenshot"})).verdict is Verdict.DENY
    assert kernel.decide(ToolRequest("computer_run", OWNER, ACTION)).verdict is Verdict.NEEDS_HUMAN
    assert kernel.decide(ToolRequest("computer_status", OWNER, {"op":"files","project":"report"})).verdict is Verdict.ALLOW


def test_headless_cloud_has_no_gui_but_keeps_persistent_execution():
    import runpy
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy/computer-setup.py"))
    original = json.loads((Path(__file__).resolve().parents[1] / "deploy/computer-cloud-init.json").read_text())
    headless = module["profile_cloud"](original)
    assert "chromium" in headless["packages"] and "xvfb" not in headless["packages"]
    assert {"python3", "git", "dbus-user-session"} <= set(headless["packages"])
    assert any("/home/desk/Projects" in command for command in headless["runcmd"])
    assert ["loginctl", "enable-linger", "desk"] in headless["runcmd"]
    assert "talos-screen" not in json.dumps(headless)
    desktop = module["profile_cloud"](original, True)
    assert "--headless=new" in json.dumps(headless)
    assert "--headless=new" not in json.dumps(desktop)
    assert any(f["path"].endswith("browser-requirements.lock") for f in desktop["write_files"])
    assert "chromium" in original["packages"]

# --- Der Browser muss da sein, und wenn nicht, muss man es lesen koennen ----------
BROWSER_UNIT = Path(__file__).resolve().parents[1] / "deploy/talos-guest-browser.service"


def test_the_browser_stays_gone_after_a_clean_exit() -> None:
    """Chromium beendet sich auch SAUBER — geschlossenes Fenster, `chrome://quit`,
    ein aufgeraeumter OOM-Abbruch liefern Exit 0. Mit `Restart=on-failure` kam der
    Browser dann nie zurueck, und der naechste Auftrag lief in ein ECONNREFUSED.

    Gemessen am 12.09.2026: ein Agent verbrachte ueber zwanzig Minuten damit, von
    dieser Meldung zur Ursache zu kommen. Die Aufgabe selbst dauerte danach unter
    einer Minute.
    """
    unit = BROWSER_UNIT.read_text(encoding="utf-8")
    assert "Restart=always" in unit, "ein sauber beendeter Browser kommt nicht zurueck"
    assert "Restart=on-failure" not in unit


def test_the_restart_limit_silently_stops_the_browser() -> None:
    """`StartLimitIntervalSec` gehoert in [Unit]. In [Service] ignoriert systemd es seit
    v229 STILLSCHWEIGEND und haelt den Dienst nach fuenf Starts in zehn Sekunden
    endgueltig an — genau das, was `Restart=always` verhindern soll.

    ⚠️ Getrennt wird am ZEILENANFANG. Beim ersten Anlauf stand im Kommentar der Unit
    das Wort in eckigen Klammern, und der Test splittete daran statt am Abschnitt —
    er verschluckte sich an der eigenen Begruendung und meldete einen Fehler, den es
    nicht gab.
    """
    zeilen = BROWSER_UNIT.read_text(encoding="utf-8").splitlines()
    abschnitt = None
    gefunden = {}
    for zeile in zeilen:
        if zeile.startswith("[") and zeile.endswith("]"):
            abschnitt = zeile
        elif zeile.startswith("StartLimitIntervalSec="):
            gefunden[abschnitt] = zeile
    assert gefunden, "die Startgrenze fehlt ganz"
    assert "[Unit]" in gefunden, f"steht in {list(gefunden)} statt in [Unit] und wirkt nicht"


def test_an_unreachable_browser_reports_only_a_port() -> None:
    """Ein fehlender Dienst ist ein MANGEL, kein Verdikt — er gehoert benannt, samt dem
    einen Kommando, das ihn behebt (dieselbe Linie wie `remedy.py`). Eine nackte
    Portnummer laesst den Agenten raten, und Raten kostete hier zwanzig Minuten.

    ⚠️ Geprueft wird die QUELLE, nicht der Aufruf: `browser.py` importiert Playwright
    auf Modulebene, und Playwright ist keine Abhaengigkeit dieses Projekts — es lebt
    im Computer-Gast. Ein Test, der das Modul importiert, koennte hier nur
    uebersprungen werden, und ein uebersprungener Test sagt nichts.
    """
    quelle = (Path(__file__).resolve().parents[1] / "talos/computer/browser.py").read_text(
        encoding="utf-8")
    assert "connect_over_cdp" in quelle
    # ⚠️ Die AUFRUFSTELLE, nicht der Name. `"_verbinden(pw)"` allein traf auch die
    # Definitionszeile `def _verbinden(pw):` — die Gegenprobe blieb damit gruen,
    # obwohl der Aufruf durch den direkten `connect_over_cdp` ersetzt war.
    assert "browser = _verbinden(pw)" in quelle, "die Verbindung umgeht die klare Meldung"
    assert "browser = pw.chromium.connect_over_cdp" not in quelle
    helfer = quelle.split("def _verbinden", 1)[1].split("\ndef ", 1)[0]
    assert "talos-browser" in helfer, "die Meldung nennt den Dienst nicht"
    assert "systemctl start" in helfer, "die Meldung nennt das Kommando nicht"
    assert "Nothing was clicked" in helfer, "die Meldung sagt nicht, dass nichts geschah"
