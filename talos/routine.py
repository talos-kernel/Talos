"""Operator-selected routine classes. Closed grammars, never shell heuristics."""
import os
import re
import shlex

COMPUTER_WORK = {"exec", "open", "click", "type", "key", "scroll", "browser"}
STATUS_PROPERTIES = {
    "MainPID", "ActiveState", "SubState", "LoadState", "UnitFileState",
    "Result", "ExecMainStatus", "NRestarts", "ActiveEnterTimestamp",
    "MemoryCurrent", "TasksCurrent", "CPUUsageNSec",
}


def remote_readonly(command):
    """A single diagnostic command; no shell operators, expansions or file paths."""
    if not isinstance(command, str) or not command or len(command) > 500:
        return False
    if any(c in command for c in "\n\r\x00;$`|&<>()\\"):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    if words[0] in {"df", "free", "uptime", "nproc", "uname"}:
        flags = {"df": {"-h", "-H", "-T", "-i", "-hT", "-P", "/"},
                 "free": {"-h", "-m", "-g", "--si"}, "uptime": {"-p", "-s"},
                 "nproc": {"--all"}, "uname": {"-a", "-r", "-m", "-s"}}
        return all(word in flags[words[0]] for word in words[1:])
    if words[0] != "systemctl":
        return False
    words = words[1:]
    if words[:1] == ["--user"]:
        words = words[1:]
    if len(words) < 2 or words[0] not in {"show", "is-active", "is-enabled"}:
        return False
    op, unit, *flags = words
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,120}\.service", unit):
        return False
    if op != "show":
        return not flags
    # show without an explicit, safe property set can print secret environments.
    if not flags or len(flags) % 2:
        return False
    return all(flags[i] in {"-p", "--property"}
               and all(p in STATUS_PROPERTIES for p in flags[i + 1].split(","))
               for i in range(0, len(flags), 2))


def operator_routine(req):
    if req.tool == "computer_run" and os.environ.get("TALOS_COMPUTER_AUTOAPPROVE") == "1":
        from .computer.contract import validate
        try:
            return validate(req.args) in COMPUTER_WORK
        except (ValueError, TypeError):
            return False
    return (req.tool == "remote_exec"
            and os.environ.get("TALOS_REMOTE_READONLY_AUTOAPPROVE") == "1"
            and remote_readonly(req.args.get("command")))
