#!/usr/bin/env python3
"""Browser E2E against the installed service-account Omarchy Computer."""
from __future__ import annotations

import argparse
import atexit
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "macos"))

from playwright.sync_api import expect, sync_playwright
from talos.configcli import read_file
from talos.computer.omarchy_service import framebuffer_visible


def pixels(path):
    raw = Path(path).read_bytes()
    position, width, height, payload = 8, None, None, []
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("capture is not PNG")
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
        raise RuntimeError("installed desktop geometry changed")
    rows, stride, result = zlib.decompress(b"".join(payload)), width * 3, bytearray()
    for offset in range(0, len(rows), stride + 1):
        if rows[offset] != 0:
            raise RuntimeError("unexpected PNG row filter")
        result.extend(rows[offset + 1:offset + stride + 1])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--browser", type=Path,
                        default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    options = parser.parse_args()
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(parents=True, mode=0o700, exist_ok=False)
    profile = options.profile.resolve(strict=True)
    values = read_file(profile / "talos.env")
    link_file = profile / "computer.url"
    if (link_file.is_symlink() or link_file.stat().st_uid != os.getuid()
            or link_file.stat().st_mode & 0o077):
        raise RuntimeError("local Computer link is not private")
    link = link_file.read_text(encoding="utf-8").strip()
    if not link.startswith("http://127.0.0.1:8830/#token="):
        raise RuntimeError("local Computer link is not the fixed loopback workbench")
    socket_path, owner = values["TALOS_COMPUTER_SOCKET"], values["TALOS_COMPUTER_OWNER_SHA256"]

    def rpc(kind, args):
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(20)
            channel.connect(socket_path)
            channel.sendall(json.dumps({"kind": kind, "owner": owner, "args": args}).encode() + b"\n")
            result = json.loads(channel.makefile("rb").readline())
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    initial = rpc("read", {"op": "status"})
    if initial["control"] != "paused" or initial["vm"] != "paused":
        raise RuntimeError("installed E2E must begin fail-closed")
    jobs_before = len(initial["jobs"])
    def leave_paused():
        try:
            if rpc("read", {"op": "status"})["control"] != "paused":
                rpc("action", {"op": "pause"})
        except Exception:
            pass
    atexit.register(leave_paused)
    errors, bad, posts = [], [], []
    result = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(options.browser))
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("response", lambda response: bad.append((response.status, response.url))
                if response.status >= 400 else None)
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        page.goto(link, wait_until="domcontentloaded")
        expect(page.locator("#workspace")).to_be_visible(timeout=30000)
        if "token=" in page.url:
            raise RuntimeError("access token remained in browser URL")
        expect(page.locator("#control-state")).to_have_text("Computer paused")
        page.locator("#takeover").click()
        expect(page.locator("#control-state")).to_have_text("You have control", timeout=30000)
        page.wait_for_function("() => document.getElementById('capture').naturalWidth===1440",
                               timeout=30000)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                ready = Path(rpc("read", {"op": "screenshot",
                                           "question": "installed desktop readiness"})["image_path"])
                if framebuffer_visible(ready.read_bytes()):
                    break
            except RuntimeError as error:
                if str(error) != "QMP framebuffer must be 1440x900 RGB":
                    raise
            page.wait_for_timeout(500)
        else:
            raise RuntimeError("installed guest never reached a visible desktop")
        for input_value in (
            {"op": "key", "keys": "Escape"}, {"op": "key", "keys": "Escape"},
            {"op": "type", "text": "exit"}, {"op": "key", "keys": "Return"},
        ):
            page.evaluate("""async input => {
              const response=await fetch('/api/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});
              if(!response.ok)throw new Error('desktop input refused '+response.status);
            }""", input_value)
        page.wait_for_timeout(1800)
        page.evaluate("""async () => {
          const response=await fetch('/api/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'key',keys:'super+Return'})});
          if(!response.ok)throw new Error('terminal key refused '+response.status);
        }""")
        page.wait_for_timeout(4500)
        page.locator("#keyboard-toggle").click()
        page.locator("#keyboard-text").fill("echo " + options.marker)
        page.locator("#keyboard-send").click()
        expect(page.locator("#keyboard-text")).to_have_value("", timeout=15000)
        typed = Path(rpc("read", {"op": "screenshot", "question": "before installed Enter"})["image_path"])
        page.locator('[data-key="Enter"]').click()
        before = pixels(typed)
        entered = typed
        changed = 0
        ocr = ""
        semantic_deadline = time.monotonic() + 15
        while time.monotonic() < semantic_deadline:
            page.wait_for_timeout(750)
            entered = Path(rpc("read", {"op": "screenshot", "question": "after installed Enter"})["image_path"])
            after = pixels(entered)
            changed = sum(before[index:index + 3] != after[index:index + 3]
                          for index in range(0, len(before), 3))
            if changed >= 150:
                ocr = subprocess.run(
                    ["/usr/bin/swift", str(Path(__file__).with_name("vision_ocr.swift")), str(entered)],
                    check=True, capture_output=True, text=True, timeout=60).stdout.replace(" ", "")
                if ocr.casefold().count(options.marker.casefold()) >= 2:
                    break
        shutil.copy2(typed, evidence / "01-before-enter.png")
        shutil.copy2(entered, evidence / "02-after-enter.png")
        (evidence / "ocr.txt").write_text(ocr, encoding="utf-8")
        if changed < 150:
            raise RuntimeError(f"installed Enter produced no visible change ({changed} pixels)")
        if ocr.casefold().count(options.marker.casefold()) < 2:
            raise RuntimeError("installed marker is not visible as both command and terminal output")
        page.screenshot(path=str(evidence / "03-workbench.png"), full_page=True)
        if len(rpc("read", {"op": "status"})["jobs"]) != jobs_before:
            raise RuntimeError("operator input created an agent job")
        page.locator("#release").click()
        expect(page.locator("#control-state")).to_have_text("Talos has control")
        page.locator("#pause").click()
        expect(page.locator("#control-state")).to_have_text("Computer paused")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#screen-zoom").select_option("fit")
        overflow = page.evaluate("document.documentElement.scrollWidth")
        if overflow > 390:
            raise RuntimeError(f"installed mobile workbench overflows: {overflow}")
        page.screenshot(path=str(evidence / "04-mobile.png"), full_page=True)
        if errors or bad:
            raise RuntimeError(f"browser health failed: console={errors}, http={bad}")
        result = {"ok": True, "visible_changed_pixels": changed,
                  "console_errors": errors, "http_errors": bad,
                  "post_requests": len(posts), "token_removed_from_url": True,
                  "operator_input_without_agent_job": True,
                  "mobile_no_overflow": True, "visible_marker_ocr": True,
                  "final_control": "paused"}
        context.close()
        browser.close()
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    atexit.unregister(leave_paused)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
