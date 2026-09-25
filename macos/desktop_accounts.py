"""Discover local CLI accounts without importing credentials or changing a login.

An installed CLI, a stored login and a working Talos adapter are different facts.
Only fixed local status commands run here; no model requests or login flows.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tomllib


def cli_path(home: Path, path: str = "") -> str:
    candidates = [home / ".local/bin", home / ".kimi-code/bin", home / ".npm-global/bin",
                  Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
                  Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin")]
    candidates.extend(Path(p) for p in path.split(os.pathsep) if p and Path(p).is_absolute())
    return os.pathsep.join(dict.fromkeys(map(str, candidates)))


def _private_document(path: Path, *, toml: bool = False) -> dict:
    # Refuse redirected/foreign credentials. Values never leave this module.
    if path.is_symlink():
        return {}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 262144:
                return {}
            raw = stream.read(262145)
        if len(raw) > 262144:
            return {}
        data = tomllib.loads(raw.decode()) if toml else json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _oauth_present(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    return any(isinstance(data.get(key), str) and bool(data[key].strip())
               for key in ("access_token", "refresh_token", "accessToken", "refreshToken"))


def _local_status(binary: str, args: list[str], env: dict) -> tuple[int, str]:
    try:
        result = subprocess.run([binary, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=4, env=env, cwd=str(Path.home()))
        if len(result.stdout) + len(result.stderr) > 131072:
            return -1, ""
        return result.returncode, result.stdout + result.stderr
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return -1, ""


def _account(name: str, label: str, home: Path, env: dict) -> dict:
    binary = shutil.which(name, path=cli_path(home, env.get("PATH", "")))
    result = {"id": name, "label": label, "installed": bool(binary), "binary": binary or "",
              "status": "not_installed", "status_label": "Not installed", "auth_kind": "",
              "detail": "Install this CLI to use its existing account here.",
              "action": "", "action_label": ""}
    if not binary:
        return result
    result.update(status="unknown", status_label="Installed · sign-in not verified",
                  detail="The CLI is installed. Its login has not been verified.")
    if name in {"claude", "codex"}:
        args = ["auth", "status", "--json"] if name == "claude" else ["login", "status"]
        code, output = _local_status(binary, args, env)
        try:
            data = json.loads(output) if name == "claude" else {}
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        oauth = (code == 0 and data.get("loggedIn") is True and data.get("authMethod") == "claude.ai"
                 if name == "claude" else code == 0 and "logged in using" in output.lower()
                 and "chatgpt" in output.lower())
        signed_in = code == 0 and (data.get("loggedIn") is True if name == "claude"
                                  else "logged in using" in output.lower())
        signed_out = (data.get("loggedIn") is False if name == "claude"
                      else "not logged in" in output.lower())
        if signed_in:
            api_key = (data.get("authMethod") in {"api_key", "apiKey"} if name == "claude"
                       else "api key" in output.lower())
            result.update(status="signed_in", status_label="Signed in · OAuth" if oauth
                          else "Signed in · API key" if api_key else "Signed in",
                          auth_kind="oauth" if oauth else "api_key" if api_key else "unknown")
        elif signed_out:
            result.update(status="signed_out", status_label="Sign-in needed")
        if name == "claude":
            result.update(action="quick-claude", action_label="Use Claude" if signed_in else "Connect Claude",
                          detail="Uses the official Claude CLI. Talos checks the connection before switching.")
        else:
            result.update(detail="Codex CLI account detected. Talos chat currently connects through a separate Hermes profile.")
            if shutil.which("hermes", path=cli_path(home, env.get("PATH", ""))):
                result.update(action="codex", action_label="Connect Codex")
    elif name == "kimi":
        config = _private_document(home / ".kimi-code/config.toml", toml=True)
        providers = config.get("providers", {})
        configured = isinstance(providers, dict) and any(
            isinstance(p, dict) and isinstance(p.get("oauth"), dict) for p in providers.values())
        credentials = _private_document(home / ".kimi-code/credentials/kimi-code.json")
        if configured and _oauth_present(credentials):
            result.update(status="login_found", status_label="OAuth login found", auth_kind="oauth")
        result["detail"] = "Kimi Code is detected. Its standalone CLI login is not yet connected to Talos chat."
    elif name == "cline":
        config = _private_document(home / ".cline/data/settings/providers.json")
        providers = config.get("providers", {})
        if isinstance(providers, dict) and any(
            isinstance(p, dict) and isinstance(p.get("settings"), dict)
            and _oauth_present(p["settings"].get("auth")) for p in providers.values()):
            result.update(status="login_found", status_label="OAuth login found", auth_kind="oauth")
        result["detail"] = "Cline is detected. Its standalone CLI login is not yet connected to Talos chat."
    elif name == "agy":
        credentials = _private_document(home / ".gemini/antigravity-cli/antigravity-oauth-token")
        if _oauth_present(credentials):
            result.update(status="login_found", status_label="OAuth login found", auth_kind="oauth")
        result["detail"] = "Antigravity is detected. Talos supports it as a separately configured worker, not a main chat model."
    return result


def discover(home: Path | None = None, env: dict | None = None) -> list[dict]:
    home = home or Path.home()
    env = dict(os.environ if env is None else env)
    env["PATH"] = cli_path(home, env.get("PATH", ""))
    # Strip foreign SDK routing keys; status reports the CLI's own account.
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    accounts = [("claude", "Claude"), ("codex", "Codex"), ("agy", "Antigravity"),
                ("kimi", "Kimi Code"), ("cline", "Cline")]
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        return list(pool.map(lambda item: _account(*item, home, env), accounts))
