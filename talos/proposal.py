"""Repair incomplete model proposals before they become tool calls or questions.

This is syntax guidance, not a policy verdict. It never invents arguments, executes
an action, grants permission, or retries a request that reached the executor.
"""

REQUIRED_TEXT = {
    "read_file": ("path",), "write_file": ("path",),
    "run_shell": ("command",), "remote_exec": ("host", "command"),
    "web_fetch": ("url",), "browse": ("url",),
    "web_search": ("query",), "vault_search": ("query",),
    "session_search": ("query",), "vault_get": ("path",),
    "entity_status": ("name",), "delegate": ("question",),
    "delegate_code": ("prompt",), "delegate_codex": ("prompt",),
    "delegate_agy": ("prompt",), "delegate_status": ("job_id",),
}
MAX_REPAIRS = 2


def malformed(text):
    """Recognize an attempted control reply, not examples embedded in prose.

    Called only after parsing failed. Never salvage one call from a malformed batch:
    the model must propose a single complete request, which still goes to the kernel.
    """
    return text.lstrip().startswith("TOOL_CALL:")


MALFORMED_NOTE = (
    '[The last TOOL_CALL reply was not one valid request. Nothing ran from that reply. '
    'Return exactly one TOOL_CALL: {"tool":"name","args":{...}} with valid JSON, '
    'or answer in prose if finished. Use existing receipts; do not repeat completed, '
    'uncertain or declined actions. Every corrected request still passes the kernel.]'
)


def problem(tool, args):
    """Return argument names or a fixed schema error, never argument values."""
    missing = [name for name in REQUIRED_TEXT.get(tool, ())
               if not isinstance(args.get(name), str) or not args[name].strip()]
    if missing:
        return "provide non-empty text for: " + ", ".join(missing)
    if tool == "write_file" and not isinstance(args.get("content"), str):
        return "provide content as text (an empty string is allowed)"
    if tool in {"computer_run", "computer_status"}:
        from .computer.contract import validate
        try:
            validate(args, read=tool == "computer_status")
        except (ValueError, TypeError):
            return "use the documented computer schema with all required arguments"
    return ""


def note(tool, issue):
    return (f"[Incomplete tool proposal for {tool}: {issue}. Nothing ran and no approval "
            "was requested. Correct the arguments from the task and observed context. "
            "Do not ask the operator to repair tool syntax, invent facts, or repeat a "
            "declined action. The corrected TOOL_CALL still passes the security kernel.]")
