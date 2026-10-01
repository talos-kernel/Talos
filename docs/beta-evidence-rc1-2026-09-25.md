# RC1 executed evidence — 2026-09-25

Artifact: [0.20.0-alpha.rc.1](https://github.com/talos-kernel/Talos/releases/tag/v0.20.0-alpha.rc.1),
public source `c14d3c8c74d60001d8aa0710b5e9d6b46d8d2bda`.
SHA-256: `4cd96d1d3cb7d1a86e34b999182459d4b6f1bce9bb5cb0a3c8a6872c8575cc9e`.
Downloaded release bytes matched the built archive; Ed25519 and checksum verified.
The tag resolves to the same source commit. Release is explicitly a prerelease.

| Gate | Executed result |
|---|---|
| Hosted Python 3.11 | [macOS and Ubuntu passed](https://github.com/talos-kernel/Talos/actions/runs/36134967299): 2,908 tests passed, 2 repository-specific skips each; 263/263 adversarial cases |
| Local source gate | Mac Python 3.13: 2,908 passed, 2 skips; red-team 263/263; public hygiene passed; locked runtime/dev OSV zero issues |
| Clean install | Signed corrected archive installed on Mac and ARM64 Pi; bundled suites passed, version, command, data directory and config mode 0600 read back |
| Upgrade | Previous 0.19.23-alpha updater verified the signature, built a new environment, ran suites, preserved operator files/workspace and read the existing schedule through the new schema |
| Rollback | Previous tree restored in disposable installations on both hosts; old version, schedule and configuration mode read back |
| Repetition | Mac clean installation passed twice. Pi has two verified clean-install passes; an intervening SSH-interrupted attempt is explicitly unverified, not reported as a pass |
| Dashboard fix | 69 focused dashboard/CLI tests passed twice; real HTTP foreign-authority probe now 403; browser stored-XSS probe rendered text without script execution |
| Pre-launch security | 20-point checklist completed with per-item tests/probes or explicit n/a reasons; 359 security-focused tests passed. No unresolved critical/high finding identified by these checks |
| Artifact hygiene | 381 text files scanned; no operator config/state or private endpoints included. History scanner's 10 hits reviewed: nine inert fixtures and one public verification key |

The initial unpublished archive at `ccf1f6b` failed installation because it excluded
the observer script imported by shipped tests. Its failure is retained in the RC
ledger. The corrected archive includes only that operator script, not repository
maintenance scripts. No failed artifact was published.

Residual: the installer's disposable signature-verifier dependency bootstrap is
not hash-pinned. Runtime/development dependency installation is hash-locked. A
configured dashboard proxy still needs its own authentication; Host validation is
not a substitute for access control. See the dashboard migration note.

Production services were not switched or restarted for these rehearsals. The
normal website update pointer remains 0.19.23-alpha. This evidence does not replace
the seven-day observation window, two further distinct passing candidates or a
44-case live-model E2E run (not rerun for this dashboard/packaging candidate).
