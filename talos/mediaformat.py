"""Conservative Telegram presentation from file bytes, never a filename assertion."""
from __future__ import annotations

from pathlib import Path

from .vision import media_type


def telegram_kind(path: str) -> str:
    with Path(path).open("rb") as handle:
        head = handle.read(4096)
    image = media_type(head)
    if image == "image/gif":
        return "animation"
    if image in {"image/png", "image/jpeg"}:
        # Telegram photos are limited to 10 MB. Large images remain original documents.
        return "photo" if Path(path).stat().st_size <= 10 * 1024 * 1024 else "document"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 255
                                   and head[1] & 0xE6 == 0xE2):
        return "audio"  # MP3 metadata or MPEG layer III frame.
    if head.startswith(b"OggS") and b"OpusHead" in head[:512]:
        return "voice"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"M4A ":
            return "audio"
        if brand in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V "}:
            return "video"
    # WebP, archives, PDFs, office files and unknown audio/video codecs remain usable
    # downloads; no conversion, lost quality or speculative executable is required.
    return "document"
