"""Build a self-contained local macOS app from public tracked runtime files.

Requires Xcode command-line tools, uv and a python-build-standalone directory.
Signing is local/ad-hoc by default; notarization and publication are separate.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

from desktop_runtime import backend_digest

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, **kwargs) -> None:
    subprocess.run(args, check=True, **kwargs)


def build(python_root: Path, destination: Path, identity: str = "-") -> Path:
    python_root = python_root.resolve(strict=True)
    python = python_root / "bin/python3"
    # A framework or venv depends on another installation and is not portable.
    if not python.is_file() or not (python_root / "lib/libpython3.13.dylib").is_file():
        raise SystemExit("Use an ARM64 python-build-standalone 3.13 directory (uv python dir 3.13).")
    run("swift", "build", "--package-path", str(ROOT / "macos"), "-c", "release")
    swift = ROOT / "macos/.build/release"
    # Pinned SwiftTerm handles Contents/Resources itself. Editing SwiftPM's
    # generated accessor is both unnecessary and lost at the next build plan.
    renderer = ROOT / "macos/.build/checkouts/SwiftTerm/Sources/SwiftTerm/Apple/Metal/MetalTerminalRenderer.swift"
    if 'Bundle.main.resourceURL?.appendingPathComponent(bundleName)' not in renderer.read_text():
        raise SystemExit("Review SwiftTerm's packaged-app resource lookup before building.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise SystemExit("Output already exists. Choose a new output path; existing apps are never deleted.")
    with tempfile.TemporaryDirectory(prefix="talos-bundle-", dir=destination.parent) as tmp:
        app = Path(tmp) / "Talos.app"
        contents = app / "Contents"
        resources = contents / "Resources"
        binary = contents / "MacOS/TalosApp"
        binary.parent.mkdir(parents=True)
        resources.mkdir()
        shutil.copy2(swift / "TalosApp", binary)
        # The generated accessor above resolves within the signed resource tree.
        shutil.copytree(swift / "SwiftTerm_SwiftTerm.bundle", resources / "SwiftTerm_SwiftTerm.bundle")
        shutil.copytree(python_root, resources / "python", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "site-packages"))
        packages = resources / "packages"
        run("uv", "pip", "install", "--python", str(python), "--target", str(packages),
            "--require-hashes", "-r", str(ROOT / "requirements.lock"))
        backend = resources / "backend"
        backend.mkdir()
        tracked = subprocess.check_output(["git", "ls-files", "-z", "talos", "blueprints", "SOUL.md"],
                                          cwd=ROOT).decode().split("\0")
        for relative in filter(None, tracked):
            source = ROOT / relative
            if source.is_symlink() or not source.is_file():
                raise SystemExit("Runtime input must be a regular tracked file.")
            target = backend / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (backend / "AGENTS.md").write_text("# Desktop instructions\n\nUse this profile's workspace for local projects.\n")
        (backend / "USER.md").write_text("# Your preferences\n\nAdd the preferences you want Talos to remember here.\n")
        (resources / "backend.sha256").write_text(backend_digest(backend) + "\n")
        shutil.copy2(ROOT / "macos/desktop_runtime.py", resources)
        shutil.copy2(ROOT / "macos/desktop_state.py", resources)
        shutil.copy2(ROOT / "macos/desktop_connections.py", resources)
        shutil.copy2(ROOT / "macos/desktop_hermes.py", resources)
        shutil.copy2(ROOT / "docs/computer.md", resources)
        shutil.copy2(ROOT / "site/brand/icon-512.png", resources / "mark.png")
        licenses = resources / "Licenses"
        licenses.mkdir()
        shutil.copy2(ROOT / "LICENSE", licenses / "Talos.txt")
        shutil.copy2(ROOT / "macos/.build/checkouts/SwiftTerm/LICENSE", licenses / "SwiftTerm.txt")
        iconset = Path(tmp) / "Talos.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
                run("sips", "-z", str(size * scale), str(size * scale),
                    str(ROOT / "site/brand/icon-512.png"), "--out", str(iconset / name),
                    stdout=subprocess.DEVNULL)
        run("iconutil", "-c", "icns", str(iconset), "-o", str(resources / "Talos.icns"))
        info = {"CFBundleIdentifier": "org.talos-kernel.desktop", "CFBundleName": "Talos",
                "CFBundleDisplayName": "Talos", "CFBundleExecutable": "TalosApp",
                "CFBundlePackageType": "APPL", "CFBundleIconFile": "Talos.icns",
                "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "2",
                "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
                "LSApplicationCategoryType": "public.app-category.productivity"}
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        # Sign nested Mach-O objects before sealing the outer bundle. No --deep
        # signing and no embedded developer identity/configuration in source.
        magic = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
        for path in sorted(app.rglob("*")):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    executable = stream.read(4) in magic
                if executable:
                    run("codesign", "--force", "--sign", identity, "--timestamp=none", str(path),
                        stdout=subprocess.DEVNULL)
        run("codesign", "--force", "--sign", identity, "--timestamp=none", str(app))
        run("codesign", "--verify", "--deep", "--strict", str(app))
        app.rename(destination)
    print(json.dumps({"app": str(destination), "signed": True, "notarized": False}))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "macos/build/Talos.app")
    parser.add_argument("--sign", default=os.environ.get("TALOS_APP_SIGN_IDENTITY", "-"))
    args = parser.parse_args()
    build(args.python_root, args.out.resolve(), args.sign)
