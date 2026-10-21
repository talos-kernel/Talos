#!/usr/bin/env python3
"""Render the own-tap ARM64 formula from the exact release artifact digests."""
from __future__ import annotations

import argparse
import re
from string import Template
from urllib.parse import urlsplit


FORMULA = Template('''class Talos < Formula
  include Language::Python::Virtualenv

  desc "Local autonomous agent with a deterministic security kernel"
  homepage "https://talos-agent.ch"
  url "$source_url"
  version "$version"
  sha256 "$source_sha256"
  license "MIT"

  depends_on :macos
  on_macos do
    depends_on macos: :sonoma
  end
  depends_on arch: :arm64
  depends_on "python@3.13"

  resource "locked-wheels" do
    url "$wheels_url"
    sha256 "$wheels_sha256"
  end

  def install
    libexec.install Dir["*"]
    libexec.install ".env.example"
    wheelhouse = buildpath/"wheelhouse"
    resource("locked-wheels").stage wheelhouse
    python = Formula["python@3.13"].opt_bin/"python3.13"
    virtualenv_create(libexec/".venv", python, system_site_packages: false)
    system python, "-m", "pip", "--isolated", "--disable-pip-version-check",
           "--python=#{libexec}/.venv/bin/python", "install", "--no-index",
           "--no-deps", "--only-binary=:all:", "--require-hashes",
           "--find-links=#{wheelhouse}", "-r", libexec/"requirements.lock"
    system python, "-m", "pip", "--isolated", "--python=#{libexec}/.venv/bin/python", "check"
    # Code stays in Cellar. No install/post_install hook creates user state.
    (libexec/".talos-install-manager").write "homebrew\\n"
    (bin/"talos").write_env_script libexec/"bin/talos",
      TALOS_INSTALL_MANAGER: "homebrew", PYTHONDONTWRITEBYTECODE: "1"
  end

  def caveats
    <<~EOS
      Start with: talos setup terminal
      Then run:   talos chat

      Your private profile is ~/.talos/homebrew (mode 700).
      Configuration, identity, projects and work survive package replacement.
      No other Talos installation is imported automatically.
      Upgrade with: brew upgrade talos
      Package rollback also retains the profile; uninstall does not delete it.
      Supported: Apple Silicon, macOS 14 or later.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{(bin/"talos").to_s.shellescape} version")
    assert_match "no event log yet", shell_output("#{(bin/"talos").to_s.shellescape} status")
    assert_match "brew upgrade", shell_output("#{(bin/"talos").to_s.shellescape} update", 1)
    assert_match "brew upgrade", shell_output("#{(bin/"talos").to_s.shellescape} update --prefix #{(testpath/"elsewhere").to_s.shellescape}", 1)
    refute_path_exists testpath/".talos/homebrew"
    system libexec/".venv/bin/python", "-c",
           "import av, ctranslate2, onnxruntime, cryptography, faster_whisper, ddgs, pypdf; " \\
           "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; " \\
           "key=Ed25519PrivateKey.generate(); msg=b'talos-install-proof'; " \\
           "key.public_key().verify(key.sign(msg), msg)"
  end
end
''')


def render(*, version: str, source_url: str, source_sha256: str,
           wheels_url: str, wheels_sha256: str) -> str:
    values = locals().copy()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*", version):
        raise ValueError("invalid release version")
    for value in (source_sha256, wheels_sha256):
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("artifact SHA256 must be 64 lowercase hex characters")
    for value in (source_url, wheels_url):
        # Constrain Ruby literals, including interpolation, as well as transports.
        if not re.fullmatch(r"[A-Za-z0-9./_~%?=&:+-]+", value):
            raise ValueError("artifact URL must be a public HTTPS URL without Ruby metacharacters")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("artifact URL must have a host and no credentials")
    return FORMULA.substitute(values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("version", "source-url", "source-sha256", "wheels-url", "wheels-sha256"):
        parser.add_argument("--" + name, required=True)
    args = vars(parser.parse_args())
    try:
        print(render(**args), end="")
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
