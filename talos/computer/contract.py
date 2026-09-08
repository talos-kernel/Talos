"""Strict public request schema, shared by policy and computer transport."""
import re
from pathlib import PurePosixPath

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
DESKTOP_OPS = {"screenshot", "open", "click", "type", "key", "scroll"}
READ_OPS = {"status", "job", "screenshot", "files", "routines", "routine"}
ACTIONS = {"exec", "open", "click", "type", "key", "scroll", "browser", "pause", "resume", "stop"}
BROWSER_ACTIONS = {"inspect", "navigate", "fill", "type", "select", "check", "click", "submit", "press", "wait", "upload"}


def validate_browser(args):
    action = args.get("action")
    if action not in BROWSER_ACTIONS:
        raise ValueError("unknown browser action")
    allowed = {"action", "frame", "page"}
    if "frame" in args:
        text(args["frame"], 500, "frame selector")
    if "page" in args and (type(args["page"]) is not int or not 0 <= args["page"] <= 30):
        raise ValueError("page must be a browser tab index")
    if action == "navigate":
        from urllib.parse import urlsplit
        allowed.add("url")
        url = urlsplit(text(args.get("url"), 2000, "URL"))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("an HTTP(S) URL without credentials is required")
    elif action != "inspect":
        allowed.add("selector")
        text(args.get("selector"), 500, "observed selector")
        if action in {"fill", "type", "select", "press"}:
            allowed.add("value")
            if not isinstance(args.get("value"), str) or len(args["value"]) > 8000 or "\x00" in args["value"]:
                raise ValueError("value must be text of at most 8000 characters")
        if action == "check":
            allowed.add("checked")
            if type(args.get("checked", True)) is not bool:
                raise ValueError("checked must be boolean")
        if action == "upload":
            allowed.add("path")
            relative_path(args.get("path"))
    return allowed
KEYS = {"Return", "Tab", "Escape", "BackSpace", "Delete", "Up", "Down", "Left", "Right",
        "Home", "End", "Page_Up", "Page_Down", "ctrl+l", "ctrl+a", "ctrl+c", "ctrl+v", "alt+Tab"}


def slug(value, label="name"):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError(f"{label} must contain 1–48 lowercase letters, digits or hyphens")
    return value


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("a short relative project path is required")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise ValueError("path must stay inside the project")
    return value


def text(value, limit, name):
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} must contain 1–{limit} characters")
    return value


def validate(args, *, read=False):
    if not isinstance(args, dict):
        raise ValueError("computer arguments must be an object")
    op = args.get("op", "status" if read else "")
    if op not in (READ_OPS if read else ACTIONS):
        raise ValueError("unknown computer operation")
    allowed = {"op"}
    if read:
        if op == "job":
            allowed |= {"job_id"}
            text(args.get("job_id"), 64, "job_id")
        if op == "files":
            allowed |= {"project"}
            slug(args.get("project"), "project")
        if op == "routine":
            allowed |= {"name"}
            slug(args.get("name"), "routine name")
    else:
        if op not in {"pause", "resume", "stop"}:
            allowed |= {"project", "key", "title", "checks"}
            slug(args.get("project"), "project")
            slug(args.get("key"), "operation key")
            text(args.get("title"), 120, "title")
            checks = args.get("checks", [])
            if not isinstance(checks, list) or len(checks) > 8:
                raise ValueError("at most eight checks are allowed")
            for check in checks:
                if not isinstance(check, dict) or set(check) != {"path", "sha256"}:
                    raise ValueError("checks require a relative path and expected sha256")
                relative_path(check["path"])
                if not re.fullmatch(r"[a-f0-9]{64}", str(check["sha256"])):
                    raise ValueError("invalid SHA-256 expectation")
        if op == "exec":
            allowed |= {"command", "timeout"}
            text(args.get("command"), 16000, "command")
            timeout = args.get("timeout", 60)
            if type(timeout) is not int or not 1 <= timeout <= 120:
                raise ValueError("timeout must be between 1 and 120 seconds")
        elif op == "browser":
            allowed |= validate_browser(args)
        elif op == "open":
            from urllib.parse import urlsplit
            allowed |= {"url"}
            url = urlsplit(text(args.get("url"), 2000, "URL"))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("a public HTTP(S) URL without credentials is required")
        elif op == "type":
            allowed |= {"text"}
            text(args.get("text"), 8000, "text")
        elif op == "key":
            allowed |= {"keys"}
            if args.get("keys") not in KEYS:
                raise ValueError("unsupported key combination")
        elif op == "click":
            allowed |= {"x", "y", "button"}
            for name, limit in (("x", 1439), ("y", 899)):
                if type(args.get(name)) is not int or not 0 <= args[name] <= limit:
                    raise ValueError("click lies outside the 1440×900 desktop")
            if args.get("button", 1) not in (1, 2, 3):
                raise ValueError("unsupported mouse button")
        elif op == "scroll":
            allowed |= {"direction", "amount"}
            if args.get("direction") not in ("up", "down"):
                raise ValueError("scroll direction must be up or down")
            if type(args.get("amount", 3)) is not int or not 1 <= args.get("amount", 3) <= 10:
                raise ValueError("scroll amount must be between 1 and 10")
    if set(args) - allowed:
        raise ValueError("unknown computer argument")
    return op
