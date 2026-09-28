# RC4 executed evidence — 2026-09-28

Release: `0.20.0-alpha.rc.4` · public commit `8048353dd91527ec5b2de4780e3a43143c418b17`

Scope: opt-in GitHub prerelease only. Production remains `0.19.23-alpha`; no Pi
deployment, default-download promotion or app replacement was performed.

## Executed gates

| Gate | Result |
|---|---|
| Local regression suite | 2,915 passed in 46.15s |
| Adversarial suite | 263/263 |
| Public hygiene | Passed |
| Locked dependency OSV scan | 0 findings |
| Public hosted CI | Passed on macOS and Ubuntu, run [36388585774](https://github.com/talos-kernel/Talos/actions/runs/36388585774) |
| Artifact | 390 text files scanned; hygiene findings 0; Ed25519 verified; tamper rejected |
| Artifact SHA-256 | `badd2224b2297e98cf7bb6ce664f8ba9c31984ccdf91424d92504128a51c72ff` |
| Signed install and upgrade rehearsal | Clean install, 0.19.23-alpha and RC3 upgrade, schedule migration, rollback and state read-back passed |
| Real-model E2E | 44/44 cases passed from the signed RC4 archive |
| GitHub read-back | All three assets downloaded over HTTPS; checksum matched; signature reverified and tamper rejection repeated |

The first real-model attempt exposed a verification-runner environment defect: its
allowlist omitted non-secret terminal identity variables required for Claude OAuth
refresh. The candidate source was unchanged. The private runner was corrected to pass
only `HOME`, `PATH`, `LANG`, `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `TERM` and `PWD`,
then the complete 44-case run passed. This is recorded as a harness retry, not hidden.

The seven-day operating observation remains a separate gate. At this point the Pi has
three consecutive passing observation dates (25–27 September); four further dates are
required before beta promotion.
