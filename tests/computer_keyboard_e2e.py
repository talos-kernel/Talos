"""Browser regression with an isolated RFB fixture; no live desktop is contacted.

Run with a Python environment containing Playwright and Chromium:
  python tests/computer_keyboard_e2e.py --evidence-dir /private/test-results
Real RFB/guest input must be checked separately before activating the UI.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://computer.example"
RFB = """
export default class RFB extends EventTarget {
 constructor(target) { super();this.keys=[];this.closed=false;
  target.replaceChildren(document.createElement('canvas'));
  window.connections.push(this);window.currentRFB=this;
  if(window.autoConnect)setTimeout(()=>this.dispatchEvent(new Event('connect')),30);
 }
 sendKey(...args){if(this.closed)throw Error('Input after disconnect');this.keys.push(args);}
 disconnect(){this.closed=true;setTimeout(()=>this.dispatchEvent(new Event('disconnect')),20);}
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for run in (1, 2):
            fixture = {"control": "agent", "vm": "running", "desktop": True,
                       "jobs": [], "routines": []}
            faults, posts, errors, bad, failed = [], [], [], [], []
            context = browser.new_context(viewport={"width": 390, "height": 844},
                                          is_mobile=True, has_touch=True)
            context.add_init_script("window.connections=[];window.autoConnect=true;")

            def route(request):
                path = request.request.url.removeprefix(ORIGIN)
                if path == "/api/status":
                    if faults:
                        request.fulfill(status=503, json={"error": "Test outage"})
                    else:
                        request.fulfill(json=fixture)
                elif path == "/api/control":
                    op = request.request.post_data_json["op"]
                    posts.append(op)
                    fixture["control"] = "human" if op == "takeover" else "agent"
                    request.fulfill(json={"ok": True})
                elif path == "/api/screen":
                    request.fulfill(content_type="image/svg+xml", body=
                        '<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="900"/>')
                elif path in ("/", "/app.js", "/style.css"):
                    file = "index.html" if path == "/" else path[1:]
                    request.fulfill(path=str(ROOT / "talos/computer/web" / file),
                        content_type={"index.html": "text/html", "app.js": "text/javascript",
                                      "style.css": "text/css"}[file])
                elif path == "/novnc/core/rfb.js":
                    request.fulfill(content_type="text/javascript", body=RFB)
                elif path == "/novnc/core/input/keysymdef.js":
                    request.fulfill(content_type="text/javascript", body=
                        "export default {lookup:u=>u<=255?u:0x01000000|u};")
                else:
                    raise AssertionError("Unexpected fixture request: " + path)

            context.route(ORIGIN + "/**", route)
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not faults else None)
            page.on("response", lambda r: bad.append(r.status) if r.status >= 400 and not faults else None)
            page.on("requestfailed", lambda r: failed.append(r.failure))
            page.goto(ORIGIN)
            expect(page.locator("#keyboard-toggle")).to_be_hidden()
            page.evaluate("window.autoConnect=false")
            page.locator("#takeover").click()
            expect(page.locator("#keyboard-toggle")).to_be_visible()
            expect(page.locator("#keyboard-toggle")).to_be_disabled()
            page.wait_for_function("!!window.currentRFB")
            page.evaluate("currentRFB.dispatchEvent(new Event('connect'))")
            expect(page.locator("#keyboard-toggle")).to_be_enabled()
            page.locator("#expand").click()
            page.locator("#keyboard-toggle").click()
            expect(page.locator("#keyboard")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth") <= 390
            page.get_by_label("Type a", exact=True).click()
            page.locator('[data-modifier="Shift"]').click()
            page.get_by_label("Type B", exact=True).click()
            page.locator('[data-modifier="Ctrl"]').click()
            page.get_by_label("Type a", exact=True).click()
            keys = page.evaluate("currentRFB.keys")
            assert keys == [[97], [65505, "ShiftLeft", True], [66],
                            [65505, "ShiftLeft", False], [65507, "ControlLeft", True],
                            [97], [65507, "ControlLeft", False]], keys
            page.locator("#keyboard-text").fill("hello ü@€")
            page.locator("#keyboard-send").click()
            keys = page.evaluate("currentRFB.keys.slice(7)")
            assert [k[0] for k in keys] == [104, 101, 108, 108, 111, 32, 252, 64, 0x10020ac], keys
            assert all(k[0] != 0xff0d for k in keys), "Send text pressed Enter"
            expect(page.locator("#keyboard-text")).to_have_value("")
            page.locator('[data-key="Enter"]').click()
            page.locator('[data-key="Tab"]').click()
            page.locator('[data-key="Left"]').click()
            assert page.evaluate("currentRFB.keys.slice(-3)") == [[0xff0d], [0xff09], [0xff51]]
            page.locator("#keyboard-symbols").click()
            page.get_by_label("Type 1", exact=True).click()
            page.locator("#keyboard-symbols").click()
            page.locator("#keyboard-text").fill("discard on close")
            page.locator("#keyboard-close").click()
            expect(page.locator("#keyboard")).to_be_hidden()
            expect(page.locator("#keyboard-text")).to_have_value("")
            page.locator("#keyboard-toggle").click()
            page.screenshot(path=str(args.evidence_dir / f"keyboard-mobile-{run}.png"))
            page.set_viewport_size({"width": 844, "height": 390})
            assert page.evaluate("document.documentElement.scrollWidth") <= 844
            page.locator("#keyboard-close").click()
            page.locator("#keyboard-toggle").click()
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.screenshot(path=str(args.evidence_dir / f"keyboard-desktop-{run}.png"))
            page.locator("#release").click()
            expect(page.locator("#keyboard")).to_be_hidden()
            expect(page.locator("#keyboard-toggle")).to_be_hidden()
            count = page.evaluate("connections[0].keys.length")
            page.locator('[data-key="Enter"]').evaluate("e=>e.click()")
            page.evaluate("connections[0].dispatchEvent(new Event('connect'))")
            assert page.evaluate("connections[0].keys.length") == count
            page.evaluate("window.autoConnect=true")
            page.locator("#takeover").click()
            expect(page.locator("#keyboard-toggle")).to_be_enabled()
            page.locator("#keyboard-toggle").click()
            page.locator("#keyboard-text").fill("discard on ownership loss")
            fixture["control"] = "agent"
            expect(page.locator("#keyboard")).to_be_hidden(timeout=7000)
            expect(page.locator("#keyboard-text")).to_have_value("")
            page.locator("#takeover").click()
            expect(page.locator("#keyboard-toggle")).to_be_enabled()
            page.locator("#keyboard-toggle").click()
            faults.append("status unavailable")
            expect(page.locator("#keyboard")).to_be_hidden(timeout=7000)
            expect(page.locator("#keyboard-toggle")).to_be_disabled()
            faults.clear()
            fixture.update(control="agent", desktop=False)
            expect(page.locator("#headless")).to_be_visible(timeout=7000)
            expect(page.locator("#keyboard-toggle")).to_be_hidden()
            assert not errors and not bad and not failed, (errors, bad, failed)
            assert posts == ["takeover", "release", "takeover", "takeover"], posts
            result = {"run": run, "fixture_only": True, "key_sequences": True,
                      "mobile_and_landscape": True, "unicode_composer": True,
                      "release_and_late_connect": True, "ownership_loss_clears_text": True,
                      "outage_disables_input": True, "headless_hides_keyboard": True,
                      "page_errors": errors, "unexpected_http_errors": bad, "request_failures": failed}
            results.append(result)
            print(json.dumps(result), flush=True)
            context.close()
        browser.close()
    (args.evidence_dir / "keyboard-fixture.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
