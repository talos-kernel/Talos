"""Run inside a test Computer with its persistent browser and pinned Playwright.

Proves the field guard end to end against real Chromium: an inspect receipt carries
a guard, a stale guard refuses the action instead of hitting a changed element, a
fresh guard is accepted again, and a malformed guard is rejected by the contract.
Uses only a temporary loopback fixture. No real site, form or credential.
Usage: /opt/talos/browser-venv/bin/python tests/computer_browser_guard_e2e.py
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DRIVER = os.environ.get("TALOS_BROWSER_TEST_DRIVER", "/opt/talos/browser.py")
PYTHON = os.environ.get("TALOS_BROWSER_TEST_PYTHON", sys.executable)

PAGE = ("<!doctype html><meta charset=utf-8><title>Guard fixture</title>"
        "<form><label>Name <input id=name name=name></label>"
        "<button id=go>Go</button></form>")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE.encode())


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/" % server.server_port

    def act(**kwargs):
        request = {"op": "browser", "project": "browser-guard-e2e",
                   "key": "guard-probe", "title": "guard probe"}
        request.update(kwargs)
        proc = subprocess.run([PYTHON, "-B", DRIVER], input=json.dumps(request),
                              text=True, capture_output=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    try:
        tab = act(action="navigate", url=base)["tab"]
        observed = act(action="inspect", tab=tab)
        field = [f for f in observed["page"]["fields"] if f["tag"] == "input"][0]
        guard = field["guard"]
        selector = field["selector"]

        accepted = act(action="fill", tab=tab, selector=selector, value="Ada", expect=guard)
        # The fill changed the element, so the guard observed before it is now stale.
        stale = act(action="fill", tab=tab, selector=selector, value="zweiter", expect=guard)
        fresh = act(action="inspect", tab=tab)
        guard_after = [f for f in fresh["page"]["fields"] if f["tag"] == "input"][0]["guard"]
        again = act(action="fill", tab=tab, selector=selector, value="dritter", expect=guard_after)
        malformed = act(action="fill", tab=tab, selector=selector, value="x", expect="nichthex")

        report = {
            "guard_initial": guard,
            "guard_after_change": guard_after,
            "guard_changed": guard != guard_after,
            "accepted": {"performed": accepted["action_performed"],
                         "value": accepted["observed_value"]},
            "refused_stale": {"performed": stale["action_performed"],
                              "reason": stale.get("reason")},
            "accepted_fresh": {"performed": again["action_performed"],
                               "value": again["observed_value"]},
            "refused_malformed": {"state": malformed.get("state"),
                                  "reason": malformed.get("reason")},
        }
        assert report["guard_changed"], report
        assert report["accepted"]["performed"] and report["accepted"]["value"] == "Ada", report
        assert report["refused_stale"]["performed"] is False, report
        assert report["accepted_fresh"]["performed"] and report["accepted_fresh"]["value"] == "dritter", report
        assert report["refused_malformed"]["state"] == "failed", report
        report["passed"] = True
        print(json.dumps(report, indent=1))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
