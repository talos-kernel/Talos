#!/usr/bin/env python3
"""Drives the scripted `talos chat` session that becomes assets/talos-demo.gif.

The driver's stdout is the recording surface — run it under asciinema:

    asciinema rec --window-size 100x28 -c "python3 assets/demo/record_demo.py /tmp/talos-demo" demo.cast

It spawns a bash on a 100x28 pty, forwards everything the pty prints, and types
the demo beats. Beats wait on screen *patterns* behind a read cursor, never on
fixed sleeps: the model's latency varies between runs, and a reply that lands
while a line is still being typed must not be missed (a cleared buffer loses
exactly that reply; an unmarked one matches the previous prompt instead).

What is real in the recording: the banner, the kernel, the approval question,
the sandboxed execution and the event log. What is staged: the folder that gets
deleted (workspace/old-logs, created by build_gif.sh), the autonomy level
(build_gif.sh pre-seeds 3, "ask", the way an operator's dial survives restarts
via the event log) and the operator's lines. The model is a real configured
reasoner — which one is the build script's choice (DEMO_PROVIDER/DEMO_MODEL),
because quotas change.
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import termios
import time

COLS, ROWS = 100, 28
ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|\][^\x1b]*\x1b\\)")


class Session:
    def __init__(self, demo_dir: str) -> None:
        self.text = ""
        self.pos = 0
        env = {
            "PATH": f"{demo_dir}/bin:{os.path.expanduser('~/.local/bin')}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": os.path.expanduser("~"),
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            "SHELL": "/bin/bash",
            "PS1": "$ ",
            "MAIL": "/dev/null",  # sonst verraet der Prompt „You have new mail in /var/mail/<user>"
            "BASH_SILENCE_DEPRECATION_WARNING": "1",
            "TALOS_MODEL_PROVIDER": os.environ.get("DEMO_PROVIDER", "ollama"),
            "TALOS_MODEL": os.environ.get("DEMO_MODEL", "qwen3.8:latest"),
        }
        # A claude-cli run needs the binary's path; never hardcode a user's home
        # layout into a published script — resolve it or take the override.
        claude = os.environ.get("TALOS_CLAUDE_BIN") or shutil.which("claude")
        if claude:
            env["TALOS_CLAUDE_BIN"] = claude
        pid, self.fd = pty.fork()
        if pid == 0:
            os.chdir(demo_dir)
            os.execvpe("bash", ["bash", "--noprofile", "--norc"], env)
        self.pid = pid
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        os.set_blocking(self.fd, False)

    def pump(self, seconds: float) -> None:
        """Forward pty output to stdout for up to `seconds`."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:  # child closed the pty
                break
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            self.text += ESCAPES.sub("", chunk.decode("utf-8", "replace"))

    def wait(self, pattern: str, timeout: float) -> None:
        """Pump until output *after the last consumed match* matches — or fail loudly."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.search(pattern, self.text[self.pos:])
            if match:
                self.pos += match.end()
                return
            self.pump(0.25)
        raise SystemExit(
            f"demo beat timed out after {timeout}s waiting for: {pattern}\n"
            f"--- tail ---\n{ESCAPES.sub('', self.text)[-1500:]}"
        )

    def type(self, text: str, delay: float = 0.055) -> None:
        for ch in text:
            os.write(self.fd, ch.encode())
            self.pump(delay)

    def line(self, text: str) -> None:
        self.type(text)
        os.write(self.fd, b"\r")
        self.pump(0.3)

    def pause(self, seconds: float) -> None:
        self.pump(seconds)

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass


def main() -> None:
    demo_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/talos-demo"
    s = Session(demo_dir)
    try:
        s.wait(r"\$\s*$", 20)                       # shell is up
        s.line("talos chat")
        s.wait("/help for commands", 90)            # banner complete
        s.pause(2.0)
        s.line("please delete the leftover folder workspace/old-logs")
        s.wait("Approval required", 600)            # the kernel speaks
        s.wait("you ›", 60)                         # question parked, prompt back
        s.pause(2.0)
        s.line("yes")
        s.wait("you ›", 600)                        # executed, final answer printed
        s.pause(1.5)
        s.line("exit")
        s.wait(r"\$\s*$", 30)
        s.pause(1.0)
        s.line("clear")
        s.wait(r"\$\s*$", 10)
        s.line("talos events --limit 14")
        s.wait(r"why <id>", 60)                     # the audit trail, read-only
        s.pause(2.0)
        # Die Beleg-Zeile vertiefen: `why` erklaert Urteil, Regel und Ziele der
        # Ausfuehrung — die Id steht im Lauf, nicht im Skript (sie variiert).
        treffer = re.findall(r"(\d+)\s+\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+exec\.result\s+run_shell · done",
                             s.text)
        if treffer:
            s.line(f"talos why {treffer[-1]}")
            s.wait(r"the same run", 60)
        s.pause(4.0)
        s.line("exit")
        s.pause(1.0)
    finally:
        s.close()


if __name__ == "__main__":
    main()
