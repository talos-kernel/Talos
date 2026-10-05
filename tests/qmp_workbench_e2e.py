#!/usr/bin/env python3
"""Interactive browser E2E against the fixed offline QMP VM and service socket."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright

from talos.computer import web
from talos.computer.qmp_service import (
    Capture, Handler, QmpComputer, SocketServer, make_preflight,
)
from talos.computer.qmp import LocalQMP


def wait_socket(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        time.sleep(0.02)
    raise TimeoutError("control socket did not appear")


def png_pixels(path):
    raw = Path(path).read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("workbench preview is not PNG")
    position, width, height, payload = 8, None, None, []
    while position < len(raw):
        size = struct.unpack(">I", raw[position:position + 4])[0]
        name = raw[position + 4:position + 8]
        value = raw[position + 8:position + 8 + size]
        position += 12 + size
        if name == b"IHDR":
            width, height = struct.unpack(">II", value[:8])
        elif name == b"IDAT":
            payload.append(value)
        elif name == b"IEND":
            break
    if (width, height) != (1440, 900):
        raise RuntimeError("workbench preview geometry changed")
    scanlines = zlib.decompress(b"".join(payload))
    stride = width * 3
    pixels = bytearray()
    for offset in range(0, len(scanlines), stride + 1):
        if scanlines[offset] != 0:
            raise RuntimeError("unexpected PNG row filter")
        pixels.extend(scanlines[offset + 1:offset + stride + 1])
    if len(pixels) != width * height * 3:
        raise RuntimeError("workbench preview pixels are incomplete")
    return pixels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--disk", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--browser", type=Path,
                        default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    options = parser.parse_args()

    session = json.loads(options.session.resolve().read_text())
    required_profile = {
        "architecture": "x86_64", "distribution": "debian",
        "desktop": "hyprland", "geometry": [1440, 900],
        "offline": True, "display": "software-headless",
    }
    if any(session.get(name) != value for name, value in required_profile.items()):
        raise RuntimeError(
            "workbench E2E requires the fixed offline x86_64 Debian/Hyprland profile")
    pci_devices = session.get("pci_devices")
    if (not isinstance(pci_devices, list) or not 1 <= len(pci_devices) <= 32
            or any(not isinstance(item, list) or len(item) != 2
                   or any(type(number) is not int or not 0 <= number <= 65535
                          for number in item)
                   for item in pci_devices)):
        raise RuntimeError("workbench E2E requires a bounded expected PCI device list")
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    folders = {name: evidence / name for name in ("state", "captures", "scratch", "web-state")}
    for folder in folders.values():
        folder.mkdir(mode=0o700, exist_ok=True)
    # Darwin's AF_UNIX address limit is shorter than this checkout's path.
    # Only the ephemeral socket lives in /private/tmp; evidence remains private
    # under the caller-selected directory.
    runtime = Path(tempfile.mkdtemp(prefix="talos-qmp-e2e-", dir="/private/tmp"))
    control = runtime / "control.sock"
    secret = "w" * 32
    origin = "http://127.0.0.1"
    config = {
        "root": str(evidence),
        "owner": hashlib.sha256(b"talos-qmp-workbench-e2e").hexdigest(),
        "agent_uid": os.getuid() + 1000, "client_gid": os.getgid(),
        "qmp": str(Path(session["qmp"]).resolve()),
        "pid_file": str(evidence / "vm.pid"), "disk": str(options.disk.resolve()),
        "state": str(folders["state"]), "captures": str(folders["captures"]),
        "scratch": str(folders["scratch"]), "control_socket": str(control),
        "view_url": origin, "origin": origin, "view_secret": secret,
        "viewer": "snapshot", "web_state": str(folders["web-state"]),
        "architecture": session["architecture"],
        "distribution": session["distribution"],
        "desktop": session["desktop"], "geometry": session["geometry"],
        "pci_devices": pci_devices,
    }
    Path(config["pid_file"]).write_text(str(session["pid"]) + "\n")
    qmp = LocalQMP(config["qmp"], pid=session["pid"])
    capture = Capture(qmp, config["scratch"], settle=0.35)
    computer = QmpComputer(config, qmp=qmp,
                           capture=capture,
                           check_vm=make_preflight(
                               qmp, config["disk"], config["architecture"],
                               config["pci_devices"], capture))
    human_inputs = []
    original_human_input = computer.desktop.human_input
    def traced_human_input(args):
        human_inputs.append(dict(args))
        return original_human_input(args)
    computer.desktop.human_input = traced_human_input
    computer.desktop.control("agent", human=True)

    control.unlink(missing_ok=True)
    api = SocketServer(str(control), Handler)
    api.computer = computer
    control.chmod(0o600)
    api_thread = threading.Thread(target=api.serve_forever, daemon=True)
    api_thread.start()
    wait_socket(control)

    web.CONFIG = config
    web.CAPTURES = Path(config["captures"])
    web.CONTROL_SOCKET = control
    web.LOGIN_DB = Path(config["web_state"]) / "login.db"
    http = web.ThreadingHTTPServer(("127.0.0.1", 0), web.SnapshotHandler)
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()
    base = origin + ":" + str(http.server_address[1])
    config["view_url"] = config["origin"] = base
    smoke_status = web.rpc("read", {"op": "status"})
    smoke_preview = web.rpc("read", {"op": "preview"})
    if smoke_status["vm"] != "running" or not Path(smoke_preview["image_path"]).is_file():
        raise RuntimeError("service socket did not return a running preview")
    browser_result = {}
    try:
        with sync_playwright() as playwright:
            if not options.browser.is_file():
                raise RuntimeError(f"browser executable is missing: {options.browser}")
            browser = playwright.chromium.launch(headless=True,
                                                 executable_path=str(options.browser))
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            cookie = web.make_cookie(secret)
            context.add_cookies([{"name": web.COOKIE, "value": cookie, "url": base,
                                  "httpOnly": True, "sameSite": "Strict"}])
            page = context.new_page()
            errors, bad, mutations = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text)
                    if message.type == "error" else None)
            page.on("response", lambda response: bad.append((response.status, response.url))
                    if response.status >= 400 else None)
            page.on("request", lambda request: mutations.append(request.url)
                    if request.method == "POST" else None)

            page.goto(base, wait_until="domcontentloaded")
            expect(page.locator("#workspace")).to_be_visible()
            expect(page.locator("#capture")).to_be_visible(timeout=30000)
            try:
                page.wait_for_function("() => document.getElementById('capture').naturalWidth===1440")
            except Exception:
                page.screenshot(path=str(evidence / "failure.png"), full_page=True)
                raise RuntimeError(f"snapshot did not render: errors={errors}, http={bad}") from None
            expect(page.locator("#control-state")).to_have_text("Talos has control")
            page.screenshot(path=str(evidence / "01-agent.png"))

            page.locator("#expand").click()
            expect(page.locator("#expand")).to_have_attribute("aria-pressed", "true")
            page.locator("#screen-zoom").select_option("150")
            page.reload(wait_until="domcontentloaded")
            expect(page.locator("#expand")).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#screen-zoom")).to_have_value("150")
            page.locator("#screen-zoom").select_option("fit")

            jobs_before = len(computer.status()["jobs"])
            page.locator("#takeover").click()
            expect(page.locator("#control-state")).to_have_text("You have control")
            expect(page.locator("#keyboard-toggle")).to_be_enabled()
            page.locator("#capture").click(position={"x": 300, "y": 110})
            page.locator("#capture").press("Control+c")
            page.locator("#capture").press("Control+l")
            page.locator("#keyboard-toggle").click()
            page.locator("#keyboard-text").fill("echo TALOS_WEB_E2E")
            page.locator("#keyboard-send").click()
            expect(page.locator("#keyboard-text")).to_have_value("", timeout=10000)
            typed_preview = web.rpc("read", {"op": "preview"})["image_path"]
            page.locator('[data-key="Enter"]').click()
            page.wait_for_function("() => document.getElementById('screen-time').textContent.startsWith('Live')")
            page.wait_for_timeout(3000)
            entered_preview = web.rpc("read", {"op": "preview"})["image_path"]
            before_pixels, after_pixels = png_pixels(typed_preview), png_pixels(entered_preview)
            changed_pixels = sum(
                before_pixels[index:index + 3] != after_pixels[index:index + 3]
                for index in range(0, len(before_pixels), 3)
            )
            if changed_pixels < 150:
                raise RuntimeError(f"Enter produced no material visible change ({changed_pixels} pixels)")
            page.screenshot(path=str(evidence / "02-human-input.png"))
            expected_tail = [{"op": "key", "keys": "ctrl+c"},
                             {"op": "key", "keys": "ctrl+l"},
                             {"op": "type", "text": "echo TALOS_WEB_E2E"},
                             {"op": "key", "keys": "Return"}]
            if (len(human_inputs) != 5 or human_inputs[0].get("op") != "click"
                    or human_inputs[0].get("button") != 1
                    or not 0 <= human_inputs[0].get("x", -1) < 1440
                    or not 0 <= human_inputs[0].get("y", -1) < 900
                    or human_inputs[1:] != expected_tail):
                raise RuntimeError(f"browser input sequence changed: {human_inputs}")
            if len(computer.status()["jobs"]) != jobs_before:
                raise RuntimeError("operator input created a model job")

            page.locator("#release").click()
            expect(page.locator("#control-state")).to_have_text("Talos has control")
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#screen-zoom").select_option("fit")
            if page.evaluate("document.documentElement.scrollWidth") > 390:
                raise RuntimeError("mobile workbench overflows the viewport")
            page.screenshot(path=str(evidence / "03-mobile.png"))
            if errors or bad:
                raise RuntimeError(f"browser health failed: errors={errors}, http={bad}")
            browser_result = {"console_errors": errors, "http_errors": bad,
                              "post_requests": len(mutations), "control": computer.status()["control"],
                              "mobile_no_overflow": True, "preferences_survived_reload": True,
                              "operator_input_without_agent_job": True,
                              "service_preview_before_browser": True,
                              "enter_visible_changed_pixels": changed_pixels}
            browser_result["human_inputs"] = human_inputs
            context.close()
            browser.close()
    finally:
        http.shutdown(); http.server_close(); http_thread.join(2)
        api.shutdown(); api.server_close(); api_thread.join(2)
        control.unlink(missing_ok=True)
        computer.desktop.control("paused", human=True)
        qmp.close()

    # Reattach explicitly: an API restart must recover fail-closed and never replay.
    restarted_qmp = LocalQMP(config["qmp"], pid=session["pid"])
    try:
        restarted = QmpComputer(
            config, qmp=restarted_qmp,
            capture=(restarted_capture := Capture(
                restarted_qmp, config["scratch"], settle=0.35)),
            check_vm=make_preflight(
                restarted_qmp, config["disk"], config["architecture"],
                config["pci_devices"], restarted_capture))
        status = restarted.status()
        if status["control"] != "paused" or status["vm"] != "paused":
            raise RuntimeError("service restart did not recover fail-closed")
        browser_result["restart_fail_closed"] = True
    finally:
        restarted_qmp.close()

    (evidence / "result.json").write_text(json.dumps(browser_result, indent=2) + "\n")
    shutil.rmtree(runtime, ignore_errors=True)
    print(json.dumps({"ok": True} | browser_result, indent=2))


if __name__ == "__main__":
    main()
