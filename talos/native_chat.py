"""Native desktop presentation of the CLI identity, using existing channel contracts."""
from __future__ import annotations

from pathlib import Path
import threading
import uuid
import sys

from .channel import CallbackQuery, StructuredMessage
from .chatcli import ChatChannel
from .telegram import TelegramReply


class NativeInput(str):
    def __new__(cls, text: str, callback: CallbackQuery | None = None):
        value = super().__new__(cls, text)
        value.callback = callback
        return value


class NativeChatChannel(ChatChannel):
    def __init__(self, *args, background_jobs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.background_jobs = background_jobs
        self._native_lock = threading.Lock()
        self._message_id = 0

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        with self._native_lock:
            self._message_id += 1
            identifier = message.edit_message_id or self._message_id
        display_text = message.text
        if any(button.data.startswith("ap:") for row in message.keyboard for button in row):
            # Keep every kernel fact and expiry, but describe this channel's buttons.
            display_text = display_text.replace(
                "Reply yes (run once), always (run and allow exactly this from now on) or no (discard).",
                "Choose an approval option below.").replace(
                "Reply yes (approve this change once) or no (discard).", "Choose Allow once or Deny below.")
        self.observe({"kind": "structured", "text": display_text,
                      "channel_message_id": identifier,
                      "awaiting_answer": message.callback_notice == "Awaiting typed response",
                      "literal": not message.markdown,
                      "keyboard": [[{"label": button.label, "data": button.data} for button in row]
                                   for row in message.keyboard],
                      "buttons": [{"label": button.label, "data": button.data}
                                  for row in message.keyboard for button in row]})
        # Keep the terminal fallback complete; do not mirror the same text twice.
        from .terminalui import clean
        out = self.out or sys.stdout
        out.write(clean(message.text).rstrip("\n") + "\n")
        out.flush()

    def begin_reply(self, conversation: str):
        return NativeReply(self.observe, out=self.out)

    def begin_activity(self, conversation: str):
        activity = super().begin_activity(conversation)
        activity.background_jobs = lambda: self.background_jobs(conversation) if self.background_jobs else 0
        return activity

    def send_file(self, conversation: str, path: str) -> bool:
        artifact = Path(path).resolve(strict=True)
        if not artifact.is_file():
            return False
        delivered = self.observe({"kind": "attachment", "text": artifact.name, "path": str(artifact)})
        # A local reference is not a Telegram upload receipt and never authorizes
        # removal of the operator's original file.
        return delivered


class NativeReply(TelegramReply):
    """Reuse Telegram's tested streaming/control-line filter, with a local sink.

    Only presentation differs: Markdown remains Markdown, not Telegram HTML.
    The existing reasoner excludes hidden thinking before this public-text stream.
    """
    def __init__(self, emit, *, out=None):
        super().__init__(None, 0, min_edit_interval=0.15)
        self.emit = emit
        self.out = out or sys.stdout
        self.reply_id = uuid.uuid4().hex

    def _write(self, text: str) -> bool:
        if not any(character.isalnum() for character in text):
            return False
        if not self.emit({"kind": "stream", "reply_id": self.reply_id, "text": text}):
            return False
        self._message_id = 1
        self._shown = text
        return True

    def adopt(self, text: str) -> bool:
        self._done = True
        if self._progress_only or not text.strip():
            return False
        if not self.emit({"kind": "stream", "reply_id": self.reply_id, "text": text, "final": True}):
            return False
        from .terminalui import markdown_text, is_terminal
        self.out.write((markdown_text(text, out=self.out) if is_terminal(self.out) else text) + "\n")
        self._adopted = True
        self._shown = text
        return True

    def cleanup(self) -> None:
        self._done = True
        if not self._adopted:
            self.emit({"kind": "delete", "reply_id": self.reply_id})
