# Planned QMP VM Computer for macOS

This document records a target deployment contract. It is not a statement that a
local Mac QMP Computer is available or hardware-qualified.

## Adapter and guest contract

`QmpInputAdapter` is the reusable boundary. It is generic: the adapter accepts a
pinned local QMP transport and a guest-only capture source, validates bounded visual
actions, and records non-replayed receipts. It does not select a guest operating
system, desktop, QEMU binary, disk or endpoint. A successful receipt proves dispatch,
not guest delivery or visible success.

The planned deployment fixes the parts outside that adapter:

- Apple silicon macOS host;
- x86_64 Debian guest with Hyprland;
- QEMU TCG (`tcg,thread=multi`), not HVF;
- fixed 1440×900 capture geometry and US keyboard layout;
- visual screenshot, click, type, bounded key, scroll and operator-takeover controls;
- no shell, browser protocol, guest-file or routine interface through this backend.

This is one fixed qualification target, not a promise that arbitrary QMP guests work
merely because the adapter is generic.

## Operator-supplied artifacts

Talos does not bundle or download QEMU or a guest disk. The planned installation
requires the operator to provide both:

1. a signed VM bundle containing the QEMU x86_64 runtime and support files, a strict
   manifest, and the fixed guest kernel and initrd; and
2. a matching, already-provisioned guest disk.

The installation contract must verify the bundle signature, reject manifest values
outside the fixed x86_64 Debian/Hyprland contract, verify declared hashes before and
after copying, and verify the supplied disk against the manifest. The ordinary Talos
installer and Mac app build must not fetch or manufacture either artifact.

## Prepared x86 headless verifier

`tests/qmp_x86_headless_e2e.py` is the first-run verifier for an operator-started
lab VM. It accepts only one exact session schema: the fixed x86_64 Debian/Hyprland
identity, 1440×900 software-headless display, the signed bundle's expected PCI
vendor/device list, and the canonical QMP socket plus peer PID. Run it as the same
unprivileged account that owns that QEMU process:

```sh
python3 tests/qmp_x86_headless_e2e.py \
  --session /private/path/session.json \
  --disk /private/path/debian-hyprland.qcow2 \
  --evidence-dir /private/path/evidence
```

The run rejects unknown session fields, rechecks target architecture, exact PCI
topology, character devices, disk, absolute pointer and capture geometry before
input, then measures terminal/key/text latencies. A fresh OCR-safe marker must be
visible after Return and absent after the shell exits. The result binds guest
identity, disk and harness hashes, screenshots, job receipts, timings and final
paused-state read-back. This harness is prepared and unit-tested; it has not yet run
against a real signed x86 bundle.

## Planned security boundary

The deployment design keeps QEMU and its control services under a dedicated hidden,
non-admin service identity. QEMU user-mode NAT may provide outbound guest networking,
but the fixed launch plan excludes host forwarding, tap or bridged networking, host
folders, clipboard integration and host credentials. The raw disk and QMP socket are
not model-selectable targets.

Every proposed visual action still passes through the normal Talos policy,
capability, executor and receipt path. Unsupported operations must fail explicitly;
they must not fall through to the Mac host or a remote Computer.

## Qualification status

The fixed x86_64 Debian/Hyprland guest under TCG on Apple silicon is **not yet
hardware-qualified**. No prior guest result, screenshot, input run, release gate or
other evidence can be reused for this target. Qualification must start from new
evidence bound to the exact source revision, signed VM bundle, guest-disk digest,
QEMU launch contract and host hardware used for the candidate deployment.

Until that work is complete, documentation and UI must describe this as planned and
must not imply a supported local Mac Computer installation.
