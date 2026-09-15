"""Bounded recovery advice for transient READ failures, never permission bypasses."""
from .executor import Status

MAX_RECOVERIES = 2
READ_ROUTES = {
    "browse": "Check an installed skill or an API/CLI source. Use web_fetch or web_search for the fact if rendering failed.",
    "web_fetch": "Try web_search or a different authoritative source for the same fact; render only if JavaScript is necessary.",
    "web_search": "Use a known authoritative URL, installed skill or vault source.",
    "agent_consult": "Use available skills and previously authorized sources; state that consultation is unavailable.",
}
TRANSIENT = ("timeout", "timed out", "timeoutexpired", "connection refused", "connection reset",
             "network failure", "temporarily unavailable", "chromium", "browser unavailable")


def advice(tool, outcome, attempts):
    if attempts >= MAX_RECOVERIES or tool not in READ_ROUTES or outcome.status is not Status.ERROR:
        return ""
    evidence = (str(outcome.detail) + " " + str(outcome.result or "")).lower()
    oversized = tool == "web_fetch" and "response exceeds" in evidence
    if not oversized and not any(marker in evidence for marker in TRANSIENT):
        return ""
    route = ("Fetch a smaller source such as the raw README or an official text page, "
             "or use web_search. Do not fetch the same oversized page again. "
             if oversized else READ_ROUTES[tool])
    return ("[A read-only dependency failed. " + route +
            " Stay within the original goal and remaining plan budget. Every alternative still passes the kernel. "
            "Do not repeat an unchanged failed call, expand permissions or replay a write. "
            "Return a verified result or name the exact unresolved fact.]")
