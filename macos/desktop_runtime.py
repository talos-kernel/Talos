"""Private desktop profile and a visible PTY entry into the existing Talos CLI.

The native window is a terminal host, not a second agent or permission broker.
No listener, credentials copied from another Talos, or automatic tool approvals.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

PROFILE = Path.home() / "Library" / "Application Support" / "Talos"
QUICK_MODEL = "claude-fable-5-1"


def backend_digest(backend: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(backend.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("The bundled runtime must not contain symbolic links.")
        if path.is_file():
            digest.update(path.relative_to(backend).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def private_path(path: Path, *, directory: bool = False) -> None:
    if path.is_symlink() or (path.exists() and
            (path.stat().st_uid != os.getuid() or path.is_dir() != directory)):
        raise RuntimeError("The Talos profile contains an unexpected path. Check it before continuing.")


def require_operator_terminal() -> None:
    if os.environ.get("TALOS_SANDBOX") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("Open this action in the Talos window. A real operator terminal is required.")


def terminate_children() -> None:
    """The model CLI creates its own process group; reap this session's tree too."""
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=3, check=True)
    pairs = [tuple(map(int, line.split())) for line in listing.stdout.splitlines() if len(line.split()) == 2]
    descendants = {os.getpid()}
    ordered = []
    while True:
        children = [pid for pid, parent in pairs if parent in descendants and pid not in descendants]
        if not children:
            break
        ordered.extend(children)
        descendants.update(children)
    for pid in reversed(ordered):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def interrupted(signum, frame) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    try:
        terminate_children()
    finally:
        raise KeyboardInterrupt


def clean_environment(profile: Path, source: Path, packages: Path) -> dict[str, str]:
    """A separate install must not inherit another bot's token, identity or model worker."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TALOS_", "TELEGRAM_", "PYTHON"))
           and k not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"}}
    # Never hide the sandbox marker while normalizing a child environment.
    if os.environ.get("TALOS_SANDBOX"):
        env["TALOS_SANDBOX"] = os.environ["TALOS_SANDBOX"]
    env.update({"TALOS_SECRETS_ENV": str(profile / "talos.env"),
                "PYTHONPATH": os.pathsep.join((str(source), str(packages))),
                "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "TERM": "xterm-256color", "COLORTERM": "truecolor"})
    paths = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env["PATH"] = os.pathsep.join(dict.fromkeys(paths + env.get("PATH", "").split(os.pathsep)))
    return env


def provision(bundle: Path, profile: Path = PROFILE) -> Path:
    """Copy immutable public code once per build; keep operator state outside releases."""
    if os.environ.get("TALOS_SANDBOX"):
        raise RuntimeError("Desktop setup is unavailable inside the agent sandbox.")
    backend = bundle / "backend"
    revision = (bundle / "backend.sha256").read_text().strip()
    if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        raise RuntimeError("The app's runtime manifest is invalid. Reinstall Talos.")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    if profile.is_symlink() or profile.stat().st_uid != os.getuid():
        raise RuntimeError("The Talos profile must be owned by this Mac account.")
    os.chmod(profile, 0o700)
    private_path(profile / ".install.lock")
    with (profile / ".install.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        releases = profile / "releases"
        private_path(releases, directory=True)
        releases.mkdir(exist_ok=True, mode=0o700)
        release = releases / revision[:16]
        private_path(release, directory=True)
        private_path(profile / "talos.env")
        for name in ("data", "workspace", "skills"):
            private_path(profile / name, directory=True)
            (profile / name).mkdir(exist_ok=True, mode=0o700)
        for name in ("SOUL.md", "AGENTS.md", "USER.md"):
            target = profile / name
            private_path(target)
            if not target.exists():
                content = (backend / name).read_text() if (backend / name).is_file() else ""
                target.write_text(content)
                target.chmod(0o600)
        if not release.exists():
            if backend_digest(backend) != revision:
                raise RuntimeError("The app's runtime is incomplete. Reinstall Talos.")
            staging = Path(tempfile.mkdtemp(prefix=".install-", dir=releases))
            try:
                shutil.copytree(backend, staging, dirs_exist_ok=True)
                for name in ("data", "workspace", "skills", "SOUL.md", "AGENTS.md", "USER.md"):
                    dest = staging / name
                    if dest.exists():
                        if dest.is_dir():
                            raise RuntimeError("Runtime archive unexpectedly contains operator data.")
                        dest.unlink()
                    dest.symlink_to(profile / name, target_is_directory=name in {"data", "workspace", "skills"})
                staging.rename(release)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return release


def status(profile: Path = PROFILE) -> dict:
    # This is configuration presence, not a promise that a provider is online.
    from talos.configcli import read_file

    values = read_file(profile / "talos.env")
    identity = f"cli:{os.getuid()}"
    allowed = values.get("TALOS_ALLOWED_PRINCIPALS", "").replace(",", " ").split()
    return {"configured": identity in allowed and bool(values.get("TALOS_MODEL_PROVIDER")),
            "provider": values.get("TALOS_MODEL_PROVIDER", ""),
            "model": values.get("TALOS_MODEL", ""),
            "claude_available": bool(shutil.which("claude")),
            "hermes_available": bool(shutil.which("hermes")),
            "telegram_configured": bool(values.get("TELEGRAM_BOT_TOKEN")),
            "workspace": str(profile / "workspace")}


def quick_claude(profile: Path = PROFILE) -> None:
    """Explicit Continue-with-Claude action: prove the route before committing setup."""
    require_operator_terminal()
    from talos.configcli import read_file, write_keys
    from talos.reasoner import ClaudeCliReasoner

    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("Claude is not installed. Choose Other connection to use an API key.")
    print("\n  Connecting your existing Claude account…\n", flush=True)
    auth = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True, timeout=20)
    try:
        logged_in = bool(json.loads(auth.stdout).get("loggedIn"))
    except (ValueError, TypeError):
        logged_in = False
    if not logged_in:
        print("  Sign in using the link below, then return to this window.\n", flush=True)
        if subprocess.run([binary, "auth", "login"]).returncode:
            raise RuntimeError("Sign-in was not completed. You can try again from Connections.")
    reasoner = ClaudeCliReasoner(binary, 120, model=QUICK_MODEL)
    answer = reasoner.reason("Connection check only. Reply exactly TALOS_DESKTOP_READY. Do not use tools.")
    if "TALOS_DESKTOP_READY" not in answer or "Reasoner error" in answer:
        raise RuntimeError("Claude did not pass the connection check. Use Other connection to choose a model.")
    from desktop_connections import save_connection
    save_connection(profile, {
        "TALOS_MODEL_PROVIDER": "claude-cli", "TALOS_MODEL": QUICK_MODEL,
        "TALOS_CLAUDE_BIN": binary, "TALOS_STATUS_STYLE": "expressive",
    })
    print("\n  ✓ Connected. Your Mac account is enabled; tool permissions still come from Talos.\n", flush=True)


def run_action(action: str, profile: Path = PROFILE) -> int:
    from talos import cli
    from talos.setup_wizard import run_setup

    if action == "status":
        print(json.dumps(status(profile)))
        return 0
    if action == "history":
        from desktop_state import history
        print(json.dumps(history(profile)))
        return 0
    require_operator_terminal()
    # No duplicate chats against the same profile/event log.
    private_path(profile / ".session.lock")
    with (profile / ".session.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A Talos session is already open. Return to that window first.")
        if action == "quick-claude":
            quick_claude(profile)
            action = "chat"
        if action in {"codex", "ollama"}:
            import desktop_connections
            getattr(desktop_connections, action)(profile)
            action = "chat"
        if action == "chat" and not status(profile)["configured"]:
            code = run_setup(["terminal", "--out", str(profile / "talos.env")])
            if code or not status(profile)["configured"]:
                return code or 1
        if action == "chat":
            return cli.dispatch(["chat"]) or 0
        if action in {"model", "telegram"}:
            from talos.configcli import read_file, write_keys
            identity = f"cli:{os.getuid()}"
            before = status(profile)["configured"]
            previous = read_file(profile / "talos.env")
            section = "model" if action == "model" else "identity"
            code = run_setup([section, "--out", str(profile / "talos.env")])
            if not code and action == "model":
                from desktop_connections import validate_values, save_connection
                values = read_file(profile / "talos.env")
                if values != previous:
                    try:
                        validate_values(values)
                    except Exception:
                        write_keys(profile / "talos.env", {key: previous.get(key, "") for key in values})
                        raise RuntimeError("The new connection failed its check. Your previous settings were restored.") from None
                    save_connection(profile, values)
            # Identity setup replaces Telegram recipients. Retain only the Mac
            # identity explicitly enabled earlier; never retain an old bot owner.
            if not code and action == "telegram" and before:
                values = read_file(profile / "talos.env")
                allowed = values.get("TALOS_ALLOWED_PRINCIPALS", "").replace(",", " ").split()
                write_keys(profile / "talos.env", {
                    "TALOS_ALLOWED_PRINCIPALS": ",".join(dict.fromkeys([*allowed, identity]))})
            return code
        if action == "telegram-run":
            if not status(profile)["telegram_configured"]:
                raise RuntimeError("Set up this Mac's Telegram bot in Connections first.")
            from talos.__main__ import run
            print("\n  Telegram is running while this session stays open. End session to stop.\n", flush=True)
            run(once=False)
            return 0
        if action == "doctor":
            return cli.dispatch(["doctor"]) or 0
        raise RuntimeError("Unknown desktop action.")


def main() -> int:
    bundle = Path(__file__).resolve().parent
    try:
        release = provision(bundle, PROFILE)
        env = clean_environment(PROFILE, release, bundle / "packages")
        os.environ.clear()
        os.environ.update(env)
        sys.path.insert(0, str(release))
        sys.path.insert(1, str(bundle / "packages"))
        # Relative file tools must create durable work, never files inside a
        # versioned code release that the next app update will stop using.
        os.chdir(PROFILE / "workspace")
        # Bundled trust roots also fix Python.org framework installs with no CA setup.
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
        action = sys.argv[1] if len(sys.argv) == 2 else "status"
        if action not in {"status", "history"}:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, interrupted)
        return run_action(action)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"\n  Could not continue: {error}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
