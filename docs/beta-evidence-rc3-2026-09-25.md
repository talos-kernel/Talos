# RC3 executed evidence — 2026-09-25

Release: [0.20.0-alpha.rc.3](https://github.com/talos-kernel/Talos/releases/tag/v0.20.0-alpha.rc.3).
Public source: `0786d5a984028e415679d0b36106b2a5307dbb15`.
Archive SHA-256: `e570194a56cf5b2c6bcc956af0eda8d7e972df5cc88e14cb814d5d8463d7895a`.
Ed25519 signature and checksum verified; changed archive bytes rejected.

RC3 deliberately introduces no runtime feature or permission change from RC2.
It is a distinct version/source/archive with a fresh complete qualification, not
a rerun counted under the same artifact identity.

| Gate | Executed result |
|---|---|
| Local core | 2,914 passed |
| Public hosted Python 3.11 | macOS and Ubuntu: 2,912 passed, two repository-specific skips |
| Adversarial | 263/263 |
| Desktop | 50 tests; clean-source preview built and deep/strict ad-hoc signature verified |
| Targeted security/desktop | 409 passed |
| Real-model E2E | 44/44 from this signed archive, existing Claude CLI route |
| Clean install | Mac and ARM64 Linux: version, config mode 0600, command and data read back |
| Upgrade and rollback | Both platforms: 0.19.23-alpha → RC3 and RC2 → RC3; operator state, schedule migration and rollback verified |
| Service lifecycle | launchd/systemd start and restart, different processes, preserved schedule/chain, fixture units unloaded |
| Dependencies | Explicit runtime/dev/verifier/Swift lock OSV: zero findings |
| Hygiene | Archive clean; full public history has only the ten previously reviewed inert-fixture/public-key findings |
| Security audit | 20 points completed, including real dashboard Host/Origin 403, SQL/XSS probes and TLS/header read-back |

[Hosted CI](https://github.com/talos-kernel/Talos/actions/runs/36141877494) completed
successfully on both platforms. The private mirror remains account-billing blocked
before any test step; it is not reported as passing.

Service lifecycle checks use an empty messenger fixture with external sockets denied;
they do not claim live Telegram delivery. Production and the default download remain
on the previous alpha. The macOS CI preview is not notarized; no installed app was
replaced as part of RC2/RC3 publication.

Three distinct candidates are now qualified. The seven-day operating observation
window remains a separate, unfinished gate; these runs do not create observation days.
