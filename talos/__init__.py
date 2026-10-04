"""Talos — autonomer Wächter-Agent. Das Paket; Name und Wesen stehen in SOUL.md.

This is the package/candidate version. ``site/install.sh`` is an independent pointer
to the last signed public release, because it runs before any package exists. During
qualification those values intentionally differ; ``site/status.json`` declares both.
Once a candidate is released, the installer pointer must be advanced to this version.
``tests/test_version.py`` enforces both states so an unpublished archive is never
advertised and a released package is never left behind.
"""

__version__ = "0.20.0-beta.4"
