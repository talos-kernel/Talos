"""Small, local release facts for self-description; no network or model aliases."""
from pathlib import Path
import re

from . import __version__


def release_context(source: Path | None = None) -> str:
    source = source or Path(__file__).resolve().parent.parent
    notes = "Local changelog unavailable; do not infer changes from file timestamps."
    versions = ""
    try:
        with (source / "CHANGELOG.md").open(encoding="utf-8") as stream:
            text = stream.read(24000)
        marker = f"## [{__version__}]"
        if marker in text:
            # Current and recent shipped changes, excluding the Unreleased section.
            notes = text[text.index(marker):][:3800]
        versions = ", ".join(re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)[:30])
    except (OSError, UnicodeError):
        pass
    return (
        f"\n\nRuntime facts: this process is Talos {__version__}. "
        "This is the running software version, not the model version. "
        "Content-addressed directory names do not replace talos.__version__. "
        "Do not identify the active runtime from the newest directory timestamp. "
        "The following local changelog describes installed code, not a live check of the latest public release. "
        "A configured model alias does not prove the provider's resolved model. "
        "Answer update questions directly from these facts; verify online before claiming a newer release.\n"
        f"Available local changelog sections: {versions or 'unavailable'}. "
        f"Full installed changelog: {source / 'CHANGELOG.md'}. "
        "The excerpt below is bounded. For an older version or a precise comparison, read that file with the normal tools; "
        "do not ask the operator to explain a version that the local changelog already documents.\n"
        f"<local-release-notes>\n{notes}\n</local-release-notes>"
    )
