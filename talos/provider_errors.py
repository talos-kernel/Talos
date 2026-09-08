"""Bounded provider diagnostics; never a transport for prompts or credentials."""
from __future__ import annotations

import json
import re


class ReasonerFailure(RuntimeError):
    def __init__(self, message: str, *, kind: str, note: str = "",
                 provider: str = "", model: str = "", exit_code: int | None = None,
                 http_status: int | None = None, reset_hint: str = "",
                 fallback_allowed: bool = True) -> None:
        super().__init__(message)
        self.message, self.kind, self.note = message, kind, note
        self.provider, self.model = provider, model
        self.exit_code, self.http_status = exit_code, http_status
        self.reset_hint, self.fallback_allowed = reset_hint, fallback_allowed

    def event_data(self) -> dict:
        # An allowlist, not a dump of the provider's exception or response.
        return {"provider": self.provider, "model": self.model, "kind": self.kind,
                "exit_code": self.exit_code, "http_status": self.http_status,
                "reset_hint": self.reset_hint}


_MESSAGES = {
    "empty_response": "The provider returned no usable answer.",
    "key_rejected": "The provider rejected authentication.",
    "rate_limited": "The provider reported a rate or session limit.",
    "overloaded": "The provider is temporarily unavailable.",
    "network_failed": "The provider connection failed.",
    "timed_out": "The provider request timed out.",
    "http_failed": "The provider rejected the request.",
    "unknown": "The provider process failed without a classified diagnostic.",
}


def cli_failure(stdout: str, stderr: str, exit_code: int, *,
                provider: str, model: str) -> ReasonerFailure:
    """Classify declared errors on either stream, retaining no arbitrary text.

    Kimi adapters can emit TALOS_PROVIDER_ERROR followed by a JSON object. A
    native CLI's known error text is only considered after a failed exit.
    """
    kind, status, reset = "unknown", None, ""
    for raw in (stderr, stdout):
        bounded = (raw or "")[-16384:]
        candidates = [bounded, *reversed(bounded.splitlines()[-16:])]
        for line in candidates:
            try:
                obj = json.loads(line.removeprefix("TALOS_PROVIDER_ERROR "))
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "provider_error" and obj.get("kind") in _MESSAGES:
                kind = obj["kind"]
                code = obj.get("http_status")
                status = code if type(code) is int and 400 <= code <= 599 else None
                break
            if obj.get("type") == "result" and obj.get("is_error"):
                code = obj.get("api_error_status")
                status = code if type(code) is int and 400 <= code <= 599 else None
                text = str(obj.get("result", ""))
                if status == 429:
                    kind = "rate_limited"
                    # Keep only the time and timezone, never arbitrary result text.
                    match = re.search(r"resets (\d{1,2}:\d{2}(?:am|pm)? \([A-Za-z_/+-]+\))", text)
                    reset = match[1] if match else ""
                elif status in (401, 403):
                    kind = "key_rejected"
                elif status and status >= 500:
                    kind = "overloaded"
                elif status and status >= 400:
                    kind = "http_failed"
                break
        if kind != "unknown":
            break
        # The upstream Kimi print UI emits this exception as plain stdout.
        if ("The API returned an empty response." in bounded
                or "The API returned a response containing only thinking content" in bounded):
            kind = "empty_response"
            break
    message = _MESSAGES[kind]
    if reset:
        message = "You've hit your session limit · resets " + reset
    return ReasonerFailure(f"{provider}: {message}", kind=kind,
                           note=f"Exit {exit_code}", provider=provider, model=model,
                           exit_code=exit_code, http_status=status, reset_hint=reset,
                           fallback_allowed=False)
