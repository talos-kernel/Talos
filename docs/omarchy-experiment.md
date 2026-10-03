# Omarchy Computer for macOS (beta)

Talos can use a separately installed Omarchy virtual machine as its local Mac
Computer. This backend is **visual-only with outbound Internet access**. When its private
local link is present, it replaces the remote Computer in the Mac app: the app does
not show or use a remote Computer URL. A Mac without Omarchy can still connect to the
separately supported ARM64 Linux/KVM backend.

The ordinary Talos one-line installer does not create this VM. The source installer
is an explicit Apple-silicon beta/developer workflow: it consumes a reviewed, signed
Try Omarchy app, a pre-provisioned guest disk, the Talos app and this backend source.
Those large guest artifacts are not downloaded or embedded by the repository.

## Boundary

- QEMU runs under the hidden, non-admin, no-login `talosomarchyd` account.
- QEMU's built-in user-mode network runs under the same no-login service account;
  no root network helper is installed. The VM has one virtio
  network card, with no `hostfwd`, bridged adapter, host folder, clipboard bridge,
  host credential or user home mount. The guest can initiate network connections;
  the Mac publishes no guest service through this path.
- QEMU's disk, PID, logs and raw QMP socket stay inside root-owned
  `/Library/Application Support/TalosOmarchy`. The operator cannot read the raw disk
  or QMP socket.
- A fixed local API exposes only status, screenshot, click, type, bounded keys,
  scroll, pause, resume and stop. Shell, browser protocol, guest files and routines
  fail explicitly and never fall through to the host.
- The workbench listens only on `127.0.0.1:8830`. Its random access token is carried
  once in the URL fragment, removed from browser history after login and stored only
  in operator-owned `0600` profile files.
- The API, web process and VM are separate root-installed LaunchDaemons running as
  the service account. There is no privileged network LaunchDaemon. A client group
  permits access only to normalized screenshot
  evidence and the authenticated control socket; it cannot list the evidence pool.
- Every agent input still travels through the normal Talos policy, capability,
  executor and receipt path. The model cannot select QMP, disk, socket, owner,
  service paths or workbench origin.

`LocalQMP` pins the local peer PID, bounds response time and size, correlates command
IDs, and never reconnects or replays an uncertain input. Durable operation keys
return the original receipt for identical arguments and reject changed arguments.
Startup recovers unfinished jobs as interrupted and pauses the VM. Human takeover
invalidates the current input generation and releases any uncertain held keys before
another owner can resume it.

Input acknowledgements are `needs_review`, not proof that a visible task succeeded.
The snapshot workbench makes that distinction explicit: an operator sees the guest,
takes control, returns control or pauses it. The on-screen keyboard serializes input.
Printable keys use explicit short key-down/key-up events; special keys and shortcuts
use a separate bounded hold so neither a swallowed Enter nor a repeating letter is
silently retried.

## Installation contract

`macos/omarchy_install.py --install` is the privileged phase. It verifies that its
inputs are regular files/directories, verifies the signed Omarchy bundle before the
copy and the copied QEMU executable afterward, validates the Talos runtime/backend,
creates or strictly validates the hidden no-login service identity, creates a fresh
immutable runtime directory, updates `runtime/current` atomically, installs the fixed
LaunchDaemons and starts them fail-closed. It never downloads,
accepts credentials on the command line or overwrites the source
guest disk.

The privileged phase writes one operator-owned handoff. The unprivileged
`--configure-profile` phase validates and consumes it, writes the exact Computer
capability keys to the private Talos profile and deletes the handoff. The Mac app
will display the local card only when `computer.url` is an operator-owned regular
file with mode `0600` and an exact loopback URL. It does not persist the token in
AppStorage.

Review the script and run `python3 macos/omarchy_install.py --help` before using it.
An install currently requires:

- Apple silicon and macOS 14 or newer;
- an administrator for the service-account and LaunchDaemon phase;
- a verified Try Omarchy app and provisioned 1440×900 ARM64 guest disk;
- the signed Talos app plus this exact backend source;
- 8 GB guest memory, four virtual CPUs and sufficient local disk space.

The installer is deliberately not a one-click guest-image fetcher. Guest provenance,
licensing and update policy must be settled before that can become a general-user
installation path.

## Verification

The installed E2E opens the real private workbench, removes the fragment token,
takes control, opens a terminal, types a unique marker through the visible keyboard,
presses Enter and uses macOS Vision OCR to require the marker both as command and as
terminal output. It also checks changed pixels, browser console and HTTP health,
absence of an agent job, mobile overflow, release and final pause. The acceptance
gate requires two clean consecutive runs.

A separate restart E2E replaces only the API process and proves that the PID changes
while durable jobs remain byte-for-byte unchanged and both control state and VM stay
paused. Unit tests cover the service boundary, fixed VM command, installer ownership,
profile import, local-link validation, QMP transport, duplicate non-replay and
backend-specific reasoner instructions.

## Deliberate limitations

- The guest has outbound user-mode NAT. That network is not a separate trust zone from
  the host LAN: do not treat browsing untrusted content as harmless merely because
  the VM has no inbound forwarding or host integration.
- Input uses a validated US keyboard layout; arbitrary Unicode and paste are not a
  hidden clipboard channel.
- Geometry is fixed at 1440×900. Capture refuses the wrong size or a blank first
  frame instead of guessing coordinates.
- The local backend is not feature-equivalent to the Linux Computer. Shell, browser,
  file and routine operations fail explicitly; there is no silent remote fallback.
- This beta does not make a public listener, share the Mac desktop or expose raw QMP.

Do not add inbound forwarding, a bridged adapter, host share, clipboard bridge,
public origin, unrestricted QMP access or same-identity service process for
convenience. Each would change the security boundary and requires a new threat review
and E2E evidence.
