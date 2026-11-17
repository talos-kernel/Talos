from io import StringIO

from talos.chatcli import ChatChannel
from talos.terminalui import clean


class Terminal(StringIO):
    def isatty(self):
        return True


def test_terminal_reply_formats_markdown_without_corrupting_code():
    out = Terminal()
    reply = "## Status\n- **No remote access**: `remote_exec` is off.\n```python\nif True:\n    print('**literal**')\n```"
    ChatChannel(1000, out=out).send("cli:1000", reply)
    shown = clean(out.getvalue())
    assert "##" not in shown and "`remote_exec`" not in shown
    assert "No remote access: remote_exec is off." in shown
    assert "    print('**literal**')" in shown
    assert "```" not in shown
    pipe = StringIO()
    ChatChannel(1000, out=pipe).send("cli:1000", reply)
    assert pipe.getvalue() == reply + "\n"


def test_terminal_reply_cannot_inject_clipboard_or_screen_controls():
    out = Terminal()
    ChatChannel(1000, out=out).send("cli:1000", "\x1b]52;c;c2VjcmV0\x07**Visible**\x1b[2J\n`file_name`")
    assert "52;" not in out.getvalue() and "\x1b[2J" not in out.getvalue()
    assert clean(out.getvalue()) == "Visible\nfile_name\n"
