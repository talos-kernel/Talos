#!/usr/bin/env python3
"""Build the public Telegram demo without contacting Telegram.

The transport and chat chrome are simulated. Talos' work-trail, reply receipt,
background-completion text and scheduled-result wrapper are produced by the current
Python implementation, so a wording change is visible when this asset is rebuilt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import html
from pathlib import Path
import re
import textwrap

from PIL import Image, ImageDraw, ImageFont

from talos.agent_loop import AgentProgress, ProgressStage
from talos.channel import ScheduledRun
from talos.notify import Watch, completion_text
from talos.telegram import TelegramActivity
from talos.ux import EXPRESSIVE


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "assets" / "talos-telegram-demo.gif"
WIDTH, HEIGHT = 900, 1120


def typeface(paths: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    """Load the first available public system font on macOS or Linux."""
    for path in paths:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    raise RuntimeError(
        "No suitable TrueType font found. Install DejaVu Sans or Noto Sans."
    )


TEXT_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
)
EMOJI_FONTS = (
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
)


@dataclass
class DemoClient:
    now: list[float] = field(default_factory=lambda: [0.0])
    messages: dict[int, str] = field(default_factory=dict)
    sent_order: list[int] = field(default_factory=list)
    next_id: int = 1

    def send_message(self, _chat_id: int, text: str, **_kwargs: object) -> int:
        message_id = self.next_id
        self.next_id += 1
        self.messages[message_id] = text
        self.sent_order.append(message_id)
        return message_id

    def edit_message_text(
        self, _chat_id: int, message_id: int, text: str, **_kwargs: object
    ) -> None:
        self.messages[message_id] = text

    def delete_message(self, _chat_id: int, message_id: int) -> None:
        self.messages.pop(message_id, None)

    def send_chat_action(self, _chat_id: int, _action: str = "typing") -> None:
        return


def progress(tool: str, status: str, *, step: int, summary: str = "") -> AgentProgress:
    stage = ProgressStage.TOOL if status == "running" else ProgressStage.RESULT
    return AgentProgress(
        stage, tool=tool, status=status, step=step, max_steps=7, summary=summary
    )


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def talos_states() -> tuple[str, str, str, str, str]:
    client = DemoClient()
    activity = TelegramActivity(
        client,
        4242,
        clock=lambda: client.now[0],
        heartbeat_s=0,
        min_edit_interval=0,
        conversation="telegram:demo",
        background_jobs=lambda _conversation: 1,
        name="Talos",
        style=EXPRESSIVE,
    )
    activity.progress(
        progress("skill_view", "running", step=1, summary="skill — evidence-hygiene")
    )
    activity.progress(progress("skill_view", "done", step=1))
    activity.progress(progress("read_file", "running", step=2, summary="read — notes.md"))
    client.now[0] = 3
    reading = plain(client.messages[client.sent_order[0]])
    activity.progress(progress("read_file", "done", step=2))
    activity.progress(
        progress("write_file", "running", step=3, summary="write — summary.md")
    )
    client.now[0] = 7
    activity.progress(progress("write_file", "needs_human", step=3))
    approval = plain(client.messages[client.sent_order[0]])
    activity.progress(progress("write_file", "done", step=3))
    activity.progress(progress("delegate_codex", "running", step=4))
    client.now[0] = 15
    activity.progress(progress("delegate_codex", "done", step=4))
    activity.succeed()
    trail = plain(client.messages[client.sent_order[0]])
    receipt = plain(client.messages[client.sent_order[-1]])
    activity.cleanup()
    worker = completion_text(
        Watch("demo-42", "telegram:demo", "verify the saved summary", "delegate_codex"),
        {"state": "done", "summary": "The summary matches the source notes.",
         "files": [], "returncode": 0},
    )
    scheduled = ScheduledRun("41c226d2", "Daily project status").wrap(
        "The project is healthy. The dependency scan found no known vulnerabilities."
    )
    return reading, approval, trail + "\n\n" + receipt, worker, scheduled


def font(size: int) -> ImageFont.FreeTypeFont:
    return typeface(TEXT_FONTS, size)


BODY = font(27)
SMALL = font(21)
TITLE = font(30)
try:
    EMOJI = typeface(EMOJI_FONTS, 32)
    COLOUR_EMOJI = True
except RuntimeError:
    EMOJI = font(32)
    COLOUR_EMOJI = False


def rich_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
              *, regular: ImageFont.FreeTypeFont, fill: str) -> None:
    """Draw mixed text with the native colour-emoji font instead of tofu boxes."""
    x, y = xy
    emoji_symbols = {"⏰", "⏱", "⏸", "⏳", "✅", "❌", "✍", "↩"}
    index = 0
    while index < len(text):
        char = text[index]
        cluster = char
        if index + 1 < len(text) and text[index + 1] == "\ufe0f":
            cluster += text[index + 1]
            index += 1
        use_emoji = ord(char) >= 0x1F000 or char in emoji_symbols
        active = EMOJI if use_emoji else regular
        if use_emoji and COLOUR_EMOJI:
            draw.text((x, y - 2), cluster, font=active, embedded_color=True)
        else:
            draw.text((x, y), cluster, font=active, fill=fill)
        x += int(draw.textlength(cluster, font=active))
        index += 1


def wrapped(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, width=width, replace_whitespace=False) or [""])
    return lines


def bubble(draw: ImageDraw.ImageDraw, text: str, y: int, *, mine: bool = False,
           max_width: int = 690) -> int:
    lines = wrapped(text, 48 if max_width > 600 else 36)
    line_h = 36
    height = len(lines) * line_h + 34
    x1 = WIDTH - 34 - max_width if mine else 34
    x2 = WIDTH - 34 if mine else x1 + max_width
    fill = "#2b5278" if mine else "#182533"
    draw.rounded_rectangle((x1, y, x2, y + height), radius=24, fill=fill)
    for index, line in enumerate(lines):
        rich_text(draw, (x1 + 22, y + 17 + index * line_h), line,
                  regular=BODY, fill="#f5f7fa")
    return y + height + 16


def buttons(draw: ImageDraw.ImageDraw, y: int, *, selected: bool = False) -> int:
    labels = ["✓ Allow once", "▶ Allow this task", "∞ Always allow", "✕ Deny"]
    for index, label in enumerate(labels):
        x1 = 34 if index == 3 else 34 + index * 275
        x2 = WIDTH - 34 if index == 3 else x1 + 255
        top = y + (62 if index == 3 else 0)
        fill = "#2f80c9" if selected and index == 1 else "#23364a"
        draw.rounded_rectangle((x1, top, x2, top + 50), radius=14, fill=fill)
        draw.text((x1 + 14, top + 11), label, font=SMALL, fill="#72b6ed")
    return y + 128


def frame(messages: list[tuple[str, bool]], *, approval: bool = False,
          selected: bool = False, caption: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#0e1621")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 88), fill="#17212b")
    draw.ellipse((24, 16, 78, 70), fill="#b9783d")
    draw.text((96, 20), "Talos", font=TITLE, fill="#ffffff")
    draw.text((96, 54), "self-hosted · operator controlled", font=SMALL, fill="#7f91a4")
    y = 112
    for text, mine in messages:
        y = bubble(draw, text, y, mine=mine)
    if approval:
        y = buttons(draw, y, selected=selected)
    draw.rectangle((0, HEIGHT - 72, WIDTH, HEIGHT), fill="#17212b")
    draw.text((26, HEIGHT - 55), caption, font=SMALL, fill="#9fb3c8")
    draw.text((WIDTH - 26, HEIGHT - 55), "simulated transport · real Talos text",
              font=SMALL, fill="#6e8296", anchor="ra")
    return image


def main() -> None:
    reading, approval_text, finished, worker, scheduled = talos_states()
    user = "Summarise notes.md, save the result, then have a worker verify it."
    approval_card = (
        "Approval required\nwrite_file · workspace/summary.md\n"
        "The exact action stays subject to the kernel."
    )
    frames = [
        frame([(user, True)], caption="1 · request received"),
        frame([(user, True), (reading, False)], caption="2 · live measured work trail"),
        frame([(user, True), (approval_text, False), (approval_card, False)],
              approval=True, caption="3 · protected action waits for the operator"),
        frame([(user, True), (approval_text, False), (approval_card, False)],
              approval=True, selected=True, caption="4 · task-scoped approval selected"),
        frame([("Saved the summary. The verification worker is still running.", False),
               (finished, False)], caption="5 · reply receipt separates pending work"),
        frame([(finished, False), (worker, False)],
              caption="6 · the background result arrives separately"),
        frame([(scheduled, False)],
              caption="7 · a scheduled result is one labelled notification"),
    ]
    durations = [1400, 1800, 2200, 1100, 2600, 2800, 3200]
    frames[0].save(
        OUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(OUT)


if __name__ == "__main__":
    main()
