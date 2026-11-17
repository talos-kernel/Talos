"""Private attended-CLI presentation and one-shot input; no independent authority."""
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import threading


class ChatMirror:
    def __init__(self, profile: Path):
        from desktop_runtime import private_path
        self.lock = threading.RLock()
        self.path = profile / ".desktop-chat.json"
        private_path(self.path)
        self.state = {"pid": os.getpid(), "started": time.time(), "phase": "starting",
                      "revision": 0, "items": []}
        self.emit({"kind": "starting"})

    def read(self, prompt: str) -> str:
        """One attended submit, including newlines, cannot become several CLI commands.

        The PTY carries only a random nonce (shorter than its canonical-line limit).
        The private one-shot payload carries text, never a path or tool invocation.
        """
        line = input(prompt)
        prefix = "TALOS_MESSAGE:"
        if not line.startswith(prefix):
            return line
        nonce = line[len(prefix):]
        if not re.fullmatch(r"[A-Fa-f0-9-]{36}", nonce):
            raise RuntimeError("Invalid desktop message identifier.")
        path = self.path.parent / (".desktop-input-" + nonce + ".json")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 160000:
                raise RuntimeError("The desktop message is not private or exceeds the size limit.")
            with os.fdopen(fd, "r", closefd=False) as stream:
                value = json.load(stream)
            text = value.get("text") if isinstance(value, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise RuntimeError("Invalid desktop message text.")
            callback = value.get("callback")
            if callback is not None:
                from talos.channel import CallbackQuery
                from talos.native_chat import NativeInput
                if (not isinstance(callback, dict) or not isinstance(callback.get("data"), str)
                        or not 0 < len(callback["data"]) <= 512
                        or type(callback.get("message_id")) is not int or callback["message_id"] < 1):
                    raise RuntimeError("Invalid desktop button response.")
                text = NativeInput(text, CallbackQuery(nonce, callback["data"], callback["message_id"]))
            path.unlink()
            return text
        finally:
            os.close(fd)

    def emit(self, event: dict):
        with self.lock:
            self._emit(event)

    def _emit(self, event: dict):
        from talos.vault import redact_secrets
        from talos.terminalui import clean
        event = dict(event)
        for key in ("text", "preview", "tool", "status"):
            if key in event:
                event[key] = redact_secrets(clean(str(event[key])))[:20000]
        kind = event["kind"]
        items = self.state["items"]
        if kind == "ready":
            if self.state["phase"] not in {"approval", "question"}:
                self.state["phase"] = "working" if any(item.get("status") in {"running", "queued"}
                                                         for item in items if item["kind"] == "user") else "ready"
        elif kind == "user":
            self.state["phase"] = "working"
            items.append(event)
        elif kind == "worker":
            for item in items:
                if item.get("message_id") == event.get("message_id"):
                    item["status"] = event["status"]
            if self.state["phase"] not in {"approval", "question"}:
                self.state["phase"] = "working" if any(item.get("status") in {"running", "queued"}
                                                         for item in items if item["kind"] == "user") else "ready"
        elif kind in {"stream", "structured"}:
            if kind == "structured" and (event.get("buttons") or event.get("awaiting_answer")):
                self.state["phase"] = "question"
            matching = next((item for item in items if
                            (kind == "stream" and item.get("reply_id") == event.get("reply_id")) or
                            (kind == "structured" and item.get("channel_message_id") == event.get("channel_message_id"))), None)
            event["kind"] = "assistant"
            if matching is not None:
                matching.update(event)
            else:
                items.append(event)
        elif kind == "delete":
            items[:] = [item for item in items if item.get("reply_id") != event.get("reply_id")]
        elif kind == "attachment":
            items.append(event)
        elif kind == "progress":
            if event["stage"] == "tool":
                event["kind"] = "tool"
                event["started"] = time.monotonic()
                items.append(event)
            elif event["stage"] == "result":
                for item in reversed(items):
                    if item["kind"] == "tool" and item["tool"] == event["tool"] and item["status"] == "running":
                        item["status"] = event["status"]
                        item["duration"] = round(time.monotonic() - item["started"], 1)
                        break
            elif event["stage"] == "plan":
                event["kind"] = "note"
                items.append(event)
        elif kind == "receipt":
            if event.get("approval"):
                self.state["phase"] = "approval"
        elif kind in {"assistant", "error", "note"}:
            items.append(event)
        self.state["items"] = items[-160:]
        for index, item in enumerate(self.state["items"]):
            item["id"] = index
        self.state["revision"] += 1
        fd, temporary = tempfile.mkstemp(prefix=".chat-view-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.state, stream)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
