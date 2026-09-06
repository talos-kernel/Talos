"""Markdown der Antworten -> Telegram-HTML (`parse_mode=HTML`).

Warum HTML statt legacy-`Markdown`: das Modell schreibt CommonMark (`**fett**`),
legacy-Telegram versteht nur `*fett*` — also standen Sternchen roh im Chat, und
ein `read_file` mit ungerader Unterstrich-Zahl liess Telegram mit 400 ablehnen
(„could not deliver the answer"). HTML hat keine Unterstrich-/Sternchen-Falle:
was hier nicht konvertiert wird, bleibt sichtbarer Text, nie ein Zustellfehler.

Bewusst kleiner Umfang — was Telegrams HTML-Modus hergibt und das Modell
wirklich schreibt: `**fett**`, `inline code`, ```-Blöcke, `>`-Zitate,
`~~durchgestrichen~~`, `[Text](url)`. Kursiv mit Einzel-Sternchen wird nicht
angefasst: `2 * 3` ist kein Satzfehler, den es zu reparieren gilt.
"""
from __future__ import annotations

import html
import re

_FENCED = re.compile(r"```\w*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")

# Platzhalter aus dem Steuerzeichen-Bereich: kommen in Antworttexten praktisch
# nie vor, und alles dahinter ist bereits fertiges HTML — die spaeteren
# Regeln duerfen es nicht mehr anfassen.
_HOLD = "\x00{}\x00"
_HOLD_RE = re.compile("\x00(\\d+)\x00")


def _bold_fragment(value: str) -> str:
    # Telegram forbids code/pre inside bold. Keep mixed cells intact rather than
    # splitting an existing link or style tag at an embedded placeholder.
    if _HOLD_RE.search(value) or "<" in value:
        return value
    return f"<b>{value}</b>" if value else ""


def _table_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _tables(text: str) -> str:
    """Telegram has no table tag: keep headers and values as narrow, readable rows.

    Code is already held aside, so pipes inside commands cannot become columns.
    Require a real separator row and matching cell counts; ordinary prose is untouched.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0

    while index < len(lines):
        headers = _table_cells(lines[index])
        separator = _table_cells(lines[index + 1]) if index + 1 < len(lines) else []
        if (len(headers) < 2 or len(separator) != len(headers)
                or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)):
            out.append(lines[index])
            index += 1
            continue
        index += 2
        out.append(" · ".join(_bold_fragment(header) for header in headers))
        while index < len(lines):
            cells = _table_cells(lines[index])
            if len(cells) != len(headers):
                break
            if len(cells) == 2:
                out.append(f"• {_bold_fragment(cells[0])}: {cells[1]}")
            else:
                out.append("\n".join(f"• {_bold_fragment(header)}: {cell}" for header, cell in zip(headers, cells)))
                out.append("")
            index += 1
    return "\n".join(out)


def to_telegram_html(text: str) -> str:
    """Konvertiert das Markdown einer Antwort in Telegram-HTML.

    Rein funktional und total: jeder Eingabetext liefert einen sendbaren
    String; ein Konvertierungsfehler ist per Konstruktion ausgeschlossen
    (extrahieren -> escapen -> taggen -> zuruecksetzen).
    """
    held: list[str] = []

    def hold(fragment: str) -> str:
        held.append(fragment)
        return _HOLD.format(len(held) - 1)

    # 1. Code zuerst herausloesen — sein Inhalt wird escaped, nie formatiert.
    text = _FENCED.sub(lambda m: hold(f"<pre>{html.escape(m.group(1).strip())}</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: hold(f"<code>{html.escape(m.group(1))}</code>"), text)

    # 2. Der Rest ist Fliesstext: escapen, dann die Auszeichnung als Tags setzen.
    text = html.escape(text, quote=False)
    text = _BOLD.sub(lambda m: _bold_fragment(m.group(1)), text)
    text = _STRIKE.sub(
        lambda m: m.group(1) if _HOLD_RE.search(m.group(1)) else f"<s>{m.group(1)}</s>", text
    )

    def link(match: re.Match[str]) -> str:
        # Code-styled link labels remain clickable text; code cannot nest in links.
        label = _HOLD_RE.sub(
            lambda m: re.sub(r"^<(?:code|pre)>|</(?:code|pre)>$", "", held[int(m.group(1))]),
            match.group(1),
        )
        url = html.escape(html.unescape(match.group(2)), quote=True)
        return hold(f'<a href="{url}">{label}</a>')

    # Hold complete links too: a pipe in a URL or label is not a table boundary.
    text = _LINK.sub(link, text)
    text = _tables(text)

    # 3. Zitatzeilen: nach dem Escapen steht da `&gt; ` am Zeilenanfang.
    #    Zusammenhaengende Zeilen werden EIN Blockquote (Telegram verschachtelt nicht).
    lines = text.split("\n")
    out: list[str] = []
    quote: list[str] = []

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in lines:
        if line.startswith("&gt;"):
            quote.append(line[4:].lstrip())
        else:
            flush_quote()
            out.append(line)
    flush_quote()
    text = "\n".join(out)

    # 4. Code zurueck — nach allem, damit keine Regel hineingreift.
    return _HOLD_RE.sub(lambda m: held[int(m.group(1))], text)
