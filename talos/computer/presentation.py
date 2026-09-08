"""Operator-facing computer entry point; credentials never enter model context."""
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit
from ..channel import StructuredMessage, Trust


def chat_authorized(principal, conversation, channels):
    # Telegram group IDs differ from the sender ID. A personal bearer link must
    # never be posted into a group, even when an allowlisted operator asks there.
    expected = os.environ.get("TALOS_COMPUTER_OWNER_SHA256", "")
    return (principal.channel == "telegram" and conversation == str(principal)
            and channels is not None and channels.trust_of(principal.channel) == Trust.FULL
            and bool(expected) and hashlib.sha256(str(principal).encode()).hexdigest() == expected)


def entry():
    origin = os.environ.get("TALOS_COMPUTER_VIEW_URL", "").rstrip("/")
    keyfile = os.environ.get("TALOS_COMPUTER_VIEW_KEY_FILE", "")
    if not origin or not keyfile or not os.environ.get("TALOS_COMPUTER_SOCKET"):
        return StructuredMessage("✦ Computer is not set up yet.\n\nRun talos computer setup to get started.")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return StructuredMessage("✦ The Computer link needs a valid HTTPS configuration.")
    try:
        token = Path(keyfile).read_text().strip()
        if not token or len(token) > 128 or not all(c.isalnum() or c in "-_" for c in token):
            raise ValueError("invalid access token")
    except (OSError, ValueError):
        return StructuredMessage("✦ Your private Computer access is currently unavailable.")
    desktop = os.environ.get("TALOS_COMPUTER_DESKTOP", "1") == "1"
    features = ("👀 Watch the desktop and jobs\n🖱️ Take over, pause or return control\n"
                if desktop else "⌘ Run code, scripts and commands\n⏸ Pause or stop work at any time\n")
    return StructuredMessage(
        "✦ **Your Talos Computer**\n\n"
        f"[Open your workspace]({origin}/#token={token})\n\n"
        + features + "📁 Keep project files and checked results\n\n"
        "Your personal access link · keep it private.", markdown=True)
