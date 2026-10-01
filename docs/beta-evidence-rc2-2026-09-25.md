# RC2 executed evidence — 2026-09-25

Release: [0.20.0-alpha.rc.2](https://github.com/talos-kernel/Talos/releases/tag/v0.20.0-alpha.rc.2).
Public source: `d972ea2d78f1ed6aeb937568c02717f178ddeb8a`.
Archive SHA-256: `9950bf51c5528500742fa9f65c2d33b5b4a0dd1dda202cec55e682f77baab016`.
Ed25519 signature and checksum verified; changed archive bytes rejected.

| Gate | Executed result |
|---|---|
| Local full core suite | 2,914 passed |
| Public hosted Python 3.11 | macOS and Ubuntu: 2,912 passed, two repository-specific skips |
| Adversarial suite | 263/263 |
| Desktop | 50 tests; clean-source preview build and deep/strict ad-hoc signature verification |
| Real-model E2E | 44/44 from the signed archive, existing Claude CLI route |
| Fresh install | Mac and ARM64 Linux; version, mode 0600, command link and data read back |
| Upgrade/rollback | Both platforms: 0.19.23-alpha → RC2 and RC1 → RC2; operator state, schedule migration and rollback read back |
| Service lifecycle | Isolated launchd/systemd start and restart; distinct processes, preserved schedule, valid chain, fixture units unloaded |
| OSV | Runtime/dev/verifier and Swift locks: zero findings |
| Public hygiene | Archive scan clean; history scan has the ten reviewed inert-fixture/public-key findings, no new secret |
| Pre-launch audit | 20-point review executed; dashboard foreign-host 403, real XSS/SQL probes and TLS/header read-back included |

[Hosted CI](https://github.com/talos-kernel/Talos/actions/runs/36140983150).
The service test uses an empty messenger fixture and forbids external sockets; it is
not evidence of live Telegram delivery. Production service and default download are
unchanged. The desktop CI artifact is not notarized and does not replace an installed app.

Earlier failures are retained: an initial version/status mismatch was fixed, its
targeted suite passed twice and the full gate then passed. A probe interpreter lacked
Playwright; the existing prepared interpreter ran the probe successfully. The isolated
systemd harness initially waited on a deliberately active oneshot unit; removing that
incorrect wait passed two fresh lifecycle runs. No published artifact was replaced.

RC2 counts as the second distinct qualified candidate, not completion of the separate
seven-day operating observation window.
