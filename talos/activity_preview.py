"""Bounded command projections for activity displays, never an execution policy.

Arbitrary shell text cannot be made safe by blacklisting credential patterns. Only
small, known argument grammars are displayed; everything else is visibly omitted.
This module neither executes commands nor decides whether a tool may run.
"""
from __future__ import annotations

import re
import shlex

MAX_PREVIEW = 220
HIDDEN = "[arguments hidden]"
SHORTENED = "# … preview shortened"
_OPAQUE = "command " + HIDDEN
_PROGRAMS = frozenset({
    "hostname", "date", "uptime", "whoami", "pwd", "uname", "df", "free", "ps",
    "systemctl", "journalctl", "docker", "git", "pytest", "python", "python3",
    "bash", "sh", "zsh", "node", "ruby", "perl", "php", "curl", "wget",
    "cat", "head", "tail", "ls", "du", "find", "rg", "grep", "qmd", "env",
    "sudo", "ssh", "redis-cli", "mysql", "psql", "openssl", "npm", "npx", "uv",
})
_FLAGS = {
    "hostname": {"-f", "-s"}, "uptime": {"-p", "-s"}, "whoami": set(),
    "pwd": {"-L", "-P"}, "uname": {"-a", "-s", "-r", "-m"},
    "df": {"-h", "-H", "-i", "-k", "-P", "-T", "--total", "/", "."},
    "free": {"-h", "-m", "-g", "-b", "--wide"},
    "ls": {"-l", "-a", "-h", "-la", "-lah", "-lh", "."},
    "du": {"-h", "-s", "-sh", "."},
}
_PROPERTIES = frozenset({
    "ActiveState", "SubState", "LoadState", "UnitFileState", "MainPID", "NRestarts",
    "Restart", "ExecMainStatus", "MemoryCurrent", "CPUUsageNSec", "TasksCurrent",
    "ActiveEnterTimestamp", "Description", "Id", "Names",
})
_PROTECTED = re.compile(
    r"(?i)(secret|token|password|passwd|credential|authorization|api.?key|\.env|"
    r"id_rsa|id_ed25519|\.pem|\.p12|\.pfx|shadow|netrc|pgpass|"
    r"\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|eyJ|AKIA))"
)


def safe_label(value: object) -> str:
    """Short identifiers only; never a path, URL, assignment or arbitrary payload."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", value):
        return ""
    if _PROTECTED.search(value) or re.search(r"[A-Za-z0-9_-]{28,}", value):
        return ""
    return value


def _program(word: str) -> str:
    name = word.rsplit("/", 1)[-1]
    if name in _PROGRAMS or re.fullmatch(r"python3\.\d{1,2}", name):
        return name
    return "command"


def _systemctl(args: list[str]) -> bool:
    args = list(args)
    while args and args[0] in {"--user", "--system", "--no-pager"}:
        args.pop(0)
    if not args or args.pop(0) not in {"show", "status", "is-active", "is-enabled",
                                      "list-units", "list-timers", "list-failed",
                                      "start", "stop", "restart", "reload"}:
        return False
    while args:
        word = args.pop(0)
        if word in {"--no-pager", "--all", "--failed", "--plain", "--quiet", "--value"}:
            continue
        if word in {"-p", "--property"}:
            if not args or not set(args.pop(0).split(",")) <= _PROPERTIES:
                return False
        elif word.startswith("--property="):
            if not set(word.partition("=")[2].split(",")) <= _PROPERTIES:
                return False
        elif not safe_label(word) or word.startswith("-"):
            return False
    return True


def _pytest(args: list[str]) -> bool:
    return all(word in {"-q", "-v", "-vv", "-x", "--disable-warnings", "tests", "tests/"}
               or bool(re.fullmatch(r"tests/(?:[A-Za-z0-9_.-]+/)*test_[A-Za-z0-9_.-]+\.py", word))
               for word in args)


def _allowed(program: str, args: list[str]) -> bool:
    if any(_PROTECTED.search(word) for word in args):
        return False
    if program in _FLAGS:
        return all(word in _FLAGS[program] for word in args)
    if program == "date":
        return not args or (len(args) == 1 and bool(re.fullmatch(r"\+[ %YymdHMSFTZz:+/._-]{1,80}", args[0])))
    if program == "systemctl":
        return _systemctl(args)
    if program == "ps":
        return args in [["aux"], ["-ef"]] or (len(args) == 2 and args[0] in {"-o", "-eo"}
               and set(args[1].split(",")) <= {"pid", "ppid", "etime", "pcpu", "pmem", "comm", "stat", "rss"})
    if program == "docker":
        return args in [["ps"], ["ps", "-a"], ["stats", "--no-stream"], ["system", "df"], ["images"]]
    if program == "git":
        return args in [["status"], ["status", "--short"], ["diff", "--stat"], ["diff", "--check"],
                        ["log", "--oneline"], ["branch", "--show-current"]]
    if program == "pytest":
        return _pytest(args)
    if program.startswith("python"):
        return args == ["--version"] or (args[:2] == ["-m", "pytest"] and _pytest(args[2:]))
    if program == "cat":
        return bool(args) and all(word in {"/etc/os-release", "/proc/meminfo", "/proc/loadavg"} for word in args)
    return False


def _segment(words: list[str]) -> str:
    if not words:
        return _OPAQUE
    program = _program(words[0])
    # Preserve only recognised executable paths. Other prefixes could themselves
    # contain credentials; even the harmless basename is then a partial preview.
    if words[0] != program and words[0] not in {"/bin/" + program, "/usr/bin/" + program}:
        return program + " " + HIDDEN
    if program == "command" or not _allowed(program, words[1:]):
        return program + " " + HIDDEN
    return shlex.join(words)


def command_preview(value: object) -> str:
    """Project literal command groups; opaque syntax fails closed before tokenization.

    Applying this again at the channel boundary is safe: omission markers remain
    omissions. There is no attempt to reconstruct or execute hidden arguments.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    if len(value) > 8192 or re.search(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069]", value):
        return _OPAQUE
    # Shell substitutions, escapes, redirections and program bodies are deliberately
    # outside the display grammar. No URL, query, header or variable value is shown.
    if any(mark in value for mark in ("$", "`", "\\", "<", ">", "(", ")", "{", "}")):
        first = value.strip().split(maxsplit=1)[0]
        return _program(first) + " " + HIDDEN
    shortened = SHORTENED in value
    # Keep quotes until operators have been distinguished from literal arguments.
    lexer = shlex.shlex(value, posix=False, punctuation_chars=";&|\n")
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return _OPAQUE
    groups: list[tuple[list[str], str]] = []
    words: list[str] = []
    for token in tokens:
        if token and all(char in ";&|\n" for char in token):
            token = token.strip("\n") or "\n"
        if token in {";", "&&", "||", "|", "\n"}:
            if words:
                groups.append((words, token))
                words = []
            elif token != "\n":
                return _OPAQUE
        elif token and all(char in ";&|\n" for char in token):
            return _OPAQUE
        else:
            try:
                literal = shlex.split(token)
            except ValueError:
                return _OPAQUE
            if len(literal) != 1:
                return _OPAQUE
            words.append(literal[0])
    if words:
        groups.append((words, ""))
    elif groups and groups[-1][1] in {"&&", "||", "|"}:
        return _OPAQUE
    parts: list[str] = []
    for index, (words, operator) in enumerate(groups):
        item = _segment(words)
        if index >= 6 or len("".join(parts)) + len(item) > MAX_PREVIEW - len(SHORTENED) - 5:
            shortened = True
            break
        parts.append(item)
        if operator and index < len(groups) - 1:
            parts.append((" " + operator if operator not in {";", "\n"} else ";") + "\n")
    text = "".join(parts).rstrip(";\n &|") or _OPAQUE
    return text + ("\n" + SHORTENED if shortened else "")
