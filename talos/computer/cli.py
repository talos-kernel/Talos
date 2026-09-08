"""Computer CLI: clear diagnostics and the same private operator entry point."""
import os
from pathlib import Path
import sys
from ..terminalui import heading

HELP = """Usage: talos computer [status|open|setup|--help]

  status   check whether the private computer is configured
  open     print your private workbench link (keep it private)
  setup    show the Linux VM prerequisites and installation command

Computer actions from chat still pass the normal kernel.
"""


def run_computer(args, out=None):
    write = (out or sys.stdout).write
    if args in (["--help"], ["-h"]):
        write(HELP)
        return 0
    command = args[0] if args else "status"
    if len(args) > 1 or command not in {"status", "open", "setup"}:
        write(HELP)
        return 2
    write(heading("T A L O S  /  COMPUTER", "Your own workspace. Your control."))
    if command == "setup":
        write("  Linux ARM64 · KVM · 2 GB headless / 4 GB desktop · 40 GB disk\n\n"
              "  1. Review docs/computer.md and the deployment files.\n"
              "  2. Run deploy/computer-setup.py (headless by default; --desktop adds a GUI).\n"
              "  3. Add /etc/talos-computer-agent.env to your agent service.\n"
              "  4. Open /computer in your private operator chat.\n\n"
              "  Existing desktops, credentials and host folders are never imported.\n")
        return 0
    if command == "open":
        from .presentation import entry
        write(entry().text + "\n")
        return 0 if os.environ.get("TALOS_COMPUTER_SOCKET") else 1
    endpoint = os.environ.get("TALOS_COMPUTER_SOCKET", "")
    if not endpoint:
        write("  ○ Not configured yet. Run: talos computer setup\n")
        return 1
    if not Path(endpoint).is_socket():
        write("  ○ Control service unavailable. Your stored projects are not deleted.\n")
        return 1
    write("  ● Control socket present\n"
          "  Open /computer in your operator chat for the live VM and job status.\n")
    return 0
