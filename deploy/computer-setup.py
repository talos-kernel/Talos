#!/usr/bin/env python3
"""Provision the optional ARM64 computer. Operator-run, never a model tool."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import secrets
import shutil
import subprocess
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = Path("/var/lib/talos-computer")
IMAGE = "https://cloud.debian.org/images/cloud/trixie/20260831-2587/debian-13-generic-arm64-20260831-2587.qcow2"
SHA512 = "27287048285224e662722b54c77849c9bd1a2fd60ccea5f2e75c644fbd638fad7e32a97e8e3d628c8bbfdb9cf757951c8300f3c21758647c8906359d9c892b42"
UNITS = ("egress", "vm", "api", "web")


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def write_private(path, content, group):
    path.write_text(content)
    path.chmod(0o640)
    os.chown(path, 0, group)


def allow_capture_reads(captures, agent_uid):
    """Keep new screenshots readable even by a user manager with stale groups."""
    run("setfacl", "-m", f"u:{agent_uid}:r-x,d:u:{agent_uid}:r--", str(captures))


def profile_cloud(cloud, desktop=False):
    """Select installed capabilities before cloud-init ever sees credentials."""
    import copy
    cloud = copy.deepcopy(cloud)
    if not desktop:
        cloud["packages"] = ["dbus-user-session", "python3", "curl", "git", "ca-certificates"]
        cloud["write_files"] = [entry for entry in cloud["write_files"]
            if entry["path"] in {"/etc/apt/apt.conf.d/90talos-proxy", "/etc/environment",
                                 "/etc/ssh/sshd_config.d/80-talos.conf"}]
        cloud["runcmd"] = [command for command in cloud["runcmd"]
            if command[0] != "bash" and "talos-screen.service" not in command]
    cloud["packages"] += [name for name in ("chromium", "python3-venv") if name not in cloud["packages"]]
    browser_unit = (ROOT / "deploy/talos-guest-browser.service").read_text()
    if not desktop:
        browser_unit = browser_unit.replace(" --start-maximized ", " --headless=new ").replace(" talos-screen.service", "").replace("Environment=DISPLAY=:0\n", "")
    cloud["write_files"] += [
        {"path":"/etc/systemd/system/talos-browser.service", "content":browser_unit},
        {"path":"/opt/talos/browser-requirements.lock", "content":(ROOT / "deploy/computer-requirements.lock").read_text()},
    ]
    cloud["runcmd"] += [
        ["python3", "-m", "venv", "/opt/talos/browser-venv"],
        ["/opt/talos/browser-venv/bin/pip", "install", "--require-hashes", "-r", "/opt/talos/browser-requirements.lock"],
        ["systemctl", "enable", "--now", "talos-browser.service"],
    ]
    return cloud


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--desktop", action="store_true", help="install optional browser, graphical desktop and live takeover")
    parser.add_argument("--agent-user", required=True)
    parser.add_argument("--owner", required=True, help="channel-qualified operator, e.g. telegram:123")
    parser.add_argument("--origin", required=True, help="private HTTPS workbench origin")
    args = parser.parse_args()
    user = pwd.getpwnam(args.agent_user)
    origin = urlsplit(args.origin)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.query or origin.fragment or origin.path not in ("", "/")):
        parser.error("--origin must be a private HTTPS origin")
    if ":" not in args.owner or len(args.owner) > 200:
        parser.error("--owner must be a channel-qualified operator identity")
    if platform.machine() not in {"aarch64", "arm64"} or not Path("/dev/kvm").exists():
        parser.error("this deployment currently requires ARM64 Linux with /dev/kvm")
    if args.check:
        print("PASS: ARM64 KVM, local operator and HTTPS origin")
        print("Mode:", "Desktop (4 GB RAM)" if args.desktop else "Headless (2 GB RAM)")
        print("Provisioning requires root, 40 GB virtual disk and Debian packages.")
        return
    if os.geteuid() != 0:
        parser.error("run this reviewed provisioning script as root")
    if DATA.exists() or Path("/etc/talos-computer.json").exists():
        parser.error("computer already exists; refusing to replace its disk or credentials")
    run("apt-get", "install", "-y", "--no-install-recommends", "qemu-system-arm", "qemu-utils",
        "qemu-efi-aarch64", "cloud-image-utils", "novnc", "websockify", "acl")
    run("groupadd", "--system", "talos-computer-client")
    run("useradd", "--system", "--home-dir", str(DATA), "--shell", "/usr/sbin/nologin",
        "--groups", "kvm", "talos-computer")
    run("usermod", "-a", "-G", "talos-computer-client", args.agent_user)
    service = pwd.getpwnam("talos-computer")
    import grp
    client_group = grp.getgrnam("talos-computer-client").gr_gid
    DATA.mkdir(mode=0o700)
    os.chown(DATA, service.pw_uid, service.pw_gid)
    captures = Path("/var/lib/talos-computer-captures")
    captures.mkdir(mode=0o750)
    os.chown(captures, service.pw_uid, client_group)
    allow_capture_reads(captures, user.pw_uid)
    package = Path("/opt/talos-computer/talos")
    package.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "talos/__init__.py", package / "__init__.py")
    shutil.copytree(ROOT / "talos/computer", package / "computer", ignore=shutil.ignore_patterns("__pycache__"))
    admin = Path("/etc/talos-computer-admin")
    admin.mkdir(mode=0o700)
    for name, directory in (("admin", admin), ("host", admin), ("desk_key", DATA)):
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "talos-computer", "-f", str(directory / name))
    print("Downloading the pinned Debian image and verifying SHA-512 …")
    disk = DATA / "disk.qcow2"
    with urllib.request.urlopen(IMAGE, timeout=60) as response, disk.open("wb") as out:
        shutil.copyfileobj(response, out)
    digest = hashlib.file_digest(disk.open("rb"), "sha512").hexdigest()
    if digest != SHA512:
        disk.unlink()
        raise SystemExit("Image checksum mismatch; nothing was started.")
    run("qemu-img", "resize", str(disk), "40G")
    cloud = profile_cloud(json.loads((ROOT / "deploy/computer-cloud-init.json").read_text()), args.desktop)
    cloud["users"][0]["ssh_authorized_keys"] = [(DATA / "desk_key.pub").read_text().strip()]
    cloud["users"][1]["ssh_authorized_keys"] = [(admin / "admin.pub").read_text().strip()]
    cloud["ssh_keys"] = {"ed25519_private": (admin / "host").read_text(),
                         "ed25519_public": (admin / "host.pub").read_text()}
    cloud["write_files"].append({"path": "/opt/talos/guest.py",
                                "content": (ROOT / "talos/computer/guest.py").read_text(), "permissions": "0644"})
    for module in ("browser.py", "contract.py"):
        cloud["write_files"].append({"path": "/opt/talos/" + module,
                                   "content": (ROOT / "talos/computer" / module).read_text(), "permissions": "0644"})
    (admin / "user-data").write_text("#cloud-config\n" + json.dumps(cloud))
    (admin / "meta-data").write_text("instance-id: talos-computer-1\nlocal-hostname: talos-computer\n")
    run("cloud-localds", str(DATA / "seed.iso"), str(admin / "user-data"), str(admin / "meta-data"))
    host = (admin / "host.pub").read_text().split()
    (DATA / "known_hosts").write_text("talos-computer " + host[0] + " " + host[1] + "\n")
    for path in DATA.iterdir():
        os.chown(path, service.pw_uid, service.pw_gid)
        path.chmod(0o600)
    secret = secrets.token_urlsafe(32)
    config = {"desktop": args.desktop, "owner": hashlib.sha256(args.owner.encode()).hexdigest(), "agent_uid": user.pw_uid,
              "origin": args.origin.rstrip("/"), "view_url": args.origin.rstrip("/") + "/", "view_secret": secret}
    write_private(Path("/etc/talos-computer.json"), json.dumps(config), service.pw_gid)
    write_private(Path("/etc/talos-computer-view.key"), secret, user.pw_gid)
    env = (f"TALOS_COMPUTER_DESKTOP={int(args.desktop)}\n"
           f"TALOS_COMPUTER_OWNER_SHA256={config['owner']}\n"
           "TALOS_COMPUTER_SOCKET=/run/talos-computer-api/control.sock\n"
           "TALOS_COMPUTER_ROOT=/var/lib/talos-computer\n"
           f"TALOS_COMPUTER_VIEW_URL={args.origin.rstrip('/')}\n"
           "TALOS_COMPUTER_VIEW_KEY_FILE=/etc/talos-computer-view.key\n")
    write_private(Path("/etc/talos-computer-agent.env"), env, user.pw_gid)
    for unit in UNITS:
        name = "talos-computer-" + unit + ".service"
        unit_text = (ROOT / "deploy" / name).read_text()
        if unit == "api":
            # Existing user-service managers cache supplementary groups. A named-user
            # ACL lets the exact agent UID connect without restarting other agents.
            # control.sock inherits rw; vnc.sock chmod(0600) masks that ACL back out.
            access = (f"ExecStartPre=/usr/bin/setfacl -m u:{user.pw_uid}:rx,"
                      f"d:u:{user.pw_uid}:rw- /run/talos-computer-api\n")
            unit_text = unit_text.replace("ExecStart=", access + "ExecStart=", 1)
        if unit == "vm" and not args.desktop:
            unit_text = unit_text.replace("-m 4096", "-m 2048").replace("MemoryMax=5G", "MemoryMax=3G")
            unit_text = unit_text.replace(",hostfwd=tcp:127.0.0.1:5901-:5900", "")
        (Path("/etc/systemd/system") / name).write_text(unit_text)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "talos-computer-vm.service")
    print("VM provisioning started. Wait for cloud-init; then start API and web services.")
    print("Add EnvironmentFile=/etc/talos-computer-agent.env to the agent service and restart it.")
    print("Expose 127.0.0.1:8830 only through the configured private HTTPS proxy.")
    print("No access keys were printed. See docs/computer.md for verification and rollback.")


if __name__ == "__main__":
    main()
