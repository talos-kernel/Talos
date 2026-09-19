"""Guest-side executor, installed root-owned. Runs as the unprivileged desk user."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path("/home/desk/Projects")
ENV = dict(os.environ, DISPLAY=":0", XDG_RUNTIME_DIR="/run/user/1000",
           http_proxy="http://10.0.2.100:3128", https_proxy="http://10.0.2.100:3128",
           HTTP_PROXY="http://10.0.2.100:3128", HTTPS_PROXY="http://10.0.2.100:3128")


def bounded_run(argv, *, cwd=None, timeout=60, data=None, output_limit=12000):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env=ENV, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            proc.communicate(data, timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            code = 124
        finally:
            # A command must not leave children behind in its process group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout.seek(0); stderr.seek(0)
        return code, stdout.read(output_limit).decode(errors="replace"), stderr.read(3000).decode(errors="replace")


def project_path(project):
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", project):
        raise ValueError("invalid project")
    path = ROOT / project
    if path.is_symlink():
        raise ValueError("project directory is a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.resolve().parent != ROOT.resolve():
        raise ValueError("project escaped its root")
    return path


def safe_file(root, relative):
    from pathlib import PurePosixPath
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("file must stay in its project")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("file missing or outside project")
    if resolved.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("file exceeds verification limit")
    return resolved


def screenshot():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "screen.png"
        code, _, _ = bounded_run(["scrot", "--overwrite", str(path)], timeout=8)
        if code or not path.is_file():
            raise RuntimeError("desktop capture unavailable")
        data = path.read_bytes()
        if len(data) > 1500000:
            raise RuntimeError("desktop capture too large")
        return {"png": base64.b64encode(data).decode(), "captured_at": time.time()}


def main(args):
    op = args["op"]
    if op == "screenshot":
        return screenshot()
    project = project_path(args["project"])
    if op == "browser":
        code, stdout, _ = bounded_run(
            ["/opt/talos/browser-venv/bin/python", "-B", "/opt/talos/browser.py"],
            cwd=project, timeout=55, data=json.dumps(args).encode(), output_limit=60000,
        )
        if code:
            return {"state":"failed", "verification":"browser driver unavailable; inspect before retrying"}
        return json.loads(stdout)
    if op == "files":
        entries = []
        for path in sorted(project.iterdir()):
            if path.is_file() and not path.is_symlink():
                entries.append({"name": path.name, "bytes": path.stat().st_size})
            if len(entries) == 100:
                break
        return {"project": args["project"], "files": entries}
    if op == "exec":
        argv = ["/bin/bash", "-lc", args["command"]]
    elif op == "open":
        # Im bestehenden Chromium-Fenster navigieren, statt bei jedem Aufruf einen neuen Tab
        # zu oeffnen (frueher: "--new-tab"). Laeuft noch kein Chromium, wird es einmalig
        # gestartet - "setsid -f" loest es aus der Prozessgruppe, damit bounded_run es
        # nicht sofort wieder killt.
        url = args["url"]
        if bounded_run(["pgrep", "-x", "chromium"], timeout=10)[0] == 0:
            bounded_run(["xdotool", "search", "--onlyvisible", "--class", "chromium",
                         "windowactivate", "--sync"], timeout=15)
            bounded_run(["xdotool", "key", "--clearmodifiers", "ctrl+l"], timeout=15)
            bounded_run(["xdotool", "type", "--clearmodifiers", "--delay", "8", "--file", "-"],
                        timeout=30, data=url.encode())
            argv = ["xdotool", "key", "--clearmodifiers", "Return"]
        else:
            argv = ["setsid", "-f", "chromium", "--proxy-server=http://10.0.2.100:3128", url]
    elif op == "click":
        argv = ["xdotool", "mousemove", "--sync", str(args["x"]), str(args["y"]), "click", str(args.get("button", 1))]
    elif op == "type":
        argv = ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--file", "-"]
    elif op == "key":
        argv = ["xdotool", "key", "--clearmodifiers", args["keys"]]
    elif op == "scroll":
        argv = ["xdotool", "click", "--repeat", str(args.get("amount", 3)), "--delay", "100",
                "4" if args["direction"] == "up" else "5"]
    else:
        raise ValueError("unsupported guest action")
    code, stdout, stderr = bounded_run(argv, cwd=project, timeout=args.get("timeout", 60),
                                      data=args["text"].encode() if op == "type" else None)
    checks = []
    for check in args.get("checks", []):
        try:
            path = safe_file(project, check["path"])
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            checks.append({"path": check["path"], "passed": actual == check["sha256"], "sha256": actual})
        except (ValueError, OSError):
            checks.append({"path": check["path"], "passed": False, "reason": "file unavailable inside project"})
    state = "failed" if code else ("verified" if checks and all(c["passed"] for c in checks) else "needs_review")
    if checks and not all(c["passed"] for c in checks):
        state = "failed"
    return {"state": state, "exit_code": code, "stdout": stdout, "stderr": stderr,
            "checks": checks, "verification": "file SHA-256 checks" if checks else "not independently verified"}


if __name__ == "__main__":
    try:
        print("TALOS_READY", flush=True)
        raw = sys.stdin.buffer.readline(40001)
        if len(raw) > 40000:
            raise ValueError("request too large")
        print(json.dumps(main(json.loads(raw)), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)[:180]}))
        raise SystemExit(1)
