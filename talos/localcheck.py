"""A zero-data privacy check for OpenAI-compatible local model servers."""
from __future__ import annotations

import ipaddress
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULTS = {
    "ollama": "http://127.0.0.1:11434/v1",
    "lm-studio": "http://127.0.0.1:1234/v1",
}
TIMEOUT_S = 10
SYNTHETIC_PROMPT = "Reply with exactly LOCAL_OK. This is a synthetic connectivity check."


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The checked initial host is not authority for a second destination.
        return None


def _base_url(value: str) -> str:
    raw = str(value).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("local check requires an http(s) loopback URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials are not allowed in the local check URL")
    if parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("local check requires a plain loopback base URL")
    host = parsed.hostname.lower()
    if host == "localhost":
        # Avoid a second, mutable DNS/hosts-file decision after validation.
        authority = "127.0.0.1" + (f":{parsed.port}" if parsed.port is not None else "")
        return urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("local check refuses non-loopback hosts") from None
        if not address.is_loopback:
            raise ValueError("local check refuses non-loopback hosts")
    return raw


def _json_request(opener, url: str, *, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if data is None else "POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with opener.open(request, timeout=TIMEOUT_S) as response:
        raw = response.read(256 * 1024)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("local server returned a non-object JSON response")
    return value


def check(provider: str, *, base_url: str = "", model: str = "") -> dict:
    name = str(provider).strip().lower()
    if name not in DEFAULTS:
        raise ValueError("provider must be ollama or lm-studio")
    base = _base_url(base_url or DEFAULTS[name])
    # Ignore proxy environment variables: even a configured corporate proxy must not
    # observe the synthetic local request or turn a loopback test into outbound traffic.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
    models_response = _json_request(opener, base + "/models")
    entries = models_response.get("data")
    models = [str(item.get("id")) for item in entries
              if isinstance(item, dict) and str(item.get("id") or "").strip()] \
        if isinstance(entries, list) else []
    if not models:
        raise ValueError("local server reported no models")
    chosen = str(model).strip() or models[0]
    if chosen not in models:
        raise ValueError(f"model is not offered by the local server: {chosen}")
    started = time.perf_counter()
    response = _json_request(opener, base + "/chat/completions", payload={
        "model": chosen,
        "messages": [
            {"role": "system", "content": "This is a local synthetic health check."},
            {"role": "user", "content": SYNTHETIC_PROMPT},
        ],
        "temperature": 0,
        "max_tokens": 8,
    })
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    choices = response.get("choices")
    present = bool(
        isinstance(choices, list) and choices and isinstance(choices[0], dict)
        and isinstance(choices[0].get("message"), dict)
        and str(choices[0]["message"].get("content") or "").strip()
    )
    return {
        "ok": present,
        "provider": name,
        "base_url": base,
        "transport": "loopback only",
        "model": chosen,
        "models_available": len(models),
        "latency_ms": elapsed,
        "response_present": present,
        "data_used": "fixed synthetic prompt only",
    }


USAGE = (
    "  usage: talos local-check [--provider ollama|lm-studio] [--base-url URL] "
    "[--model NAME] [--json]\n"
    "  Proves a loopback-only model list + inference using a fixed synthetic prompt;\n"
    "  no conversation, file, credential or operator text is sent.\n"
)


def run_local_check(argv: list[str] | None = None, *, out=None) -> int:
    args = list(argv or [])
    write = (out or sys.stdout).write
    if "--help" in args or "-h" in args:
        write(USAGE)
        return 0

    def value(flag: str, default: str = "") -> str:
        if flag not in args:
            return default
        index = args.index(flag)
        if index + 1 >= len(args):
            raise ValueError(f"missing value for {flag}")
        return args[index + 1]

    try:
        value_flags = {"--provider", "--base-url", "--model"}
        consumed: set[int] = set()
        for index, item in enumerate(args):
            if index in consumed or item == "--json":
                continue
            if item not in value_flags or index + 1 >= len(args):
                raise ValueError(f"unknown or incomplete option: {item}")
            consumed.add(index + 1)
        result = check(value("--provider", "ollama"), base_url=value("--base-url"),
                       model=value("--model"))
        if "--json" in args:
            write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        else:
            mark = "OK" if result["ok"] else "FAILED"
            write(
                f"  local privacy check: {mark}\n"
                f"  {result['provider']} · {result['model']} · {result['latency_ms']} ms\n"
                "  loopback only · fixed synthetic prompt · no operator data sent\n"
            )
        return 0 if result["ok"] else 1
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as error:
        write(f"  local privacy check failed: {error}\n")
        return 2


__all__ = ["check", "run_local_check"]
