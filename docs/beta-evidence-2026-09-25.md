# Beta gate evidence — 2026-09-25

This is an executed preparation record for `0.19.23-alpha`, **not** a beta release.
Commands used the published `v0.19.22-alpha` and `v0.19.23-alpha` archives and their
SHA-256 and Ed25519 signatures. The upgrade rehearsal uses the **old release's**
updater with actual venv creation, hash-locked pip installs, regression and red-team
suites, state copy and directory switch. It then opens the migrated schedule and
rolls back the disposable tree. No operating installation was switched.

| Check | Executed result |
|---|---|
| Source regression suite on macOS | 2,900 passed |
| Adversarial suite | 263/263 |
| Public hygiene | Passed after new scripts and docs were staged |
| OSV against runtime and dev locks | No issues found |
| Fresh signed install on macOS | Installer exited 0; version, test suites, config 0600, command link and data directory read back |
| Fresh signed install on Pi5 ARM64 Linux | Same read-backs passed in a temporary prefix |
| Signed upgrade and rollback on macOS Python 3.11 | Passed; operator files, schedule row and heartbeat-column migration read back |
| Signed upgrade and rollback on macOS Python 3.12 | Passed with the same read-backs |
| Signed upgrade and rollback on macOS Python 3.13 | Passed twice with the same read-backs |
| Signed upgrade and rollback on Pi5 Python 3.13 | Passed in a temporary prefix; operating service untouched |
| Hosted public CI | [Run 36103848464](https://github.com/talos-kernel/Talos/actions/runs/36103848464): Python 3.11 macOS and Ubuntu jobs passed install, public hygiene, pytest and red-team steps |

The Pi service read-back showed version `0.19.23-alpha`, `active`, zero automatic
restarts since its 2026-09-25 00:01 CEST start, `health` status `ok`, and an intact
event chain (193,485 of 231,780 events chained). `health` also counted 14 errors
over the prior 24 hours, with a Telegram read timeout as the newest. The older
38,295 events predate the chain and cannot be cryptographically verified. This is
one observation, not seven-day stability evidence.

During harness hardening, one test run failed because a nested temporary directory
made a macOS Unix-socket path too long and changed home-dependent fixtures. The
updater returned a proof failure, discarded staging and left the old tree untouched.
The harness now retains normal `HOME`/`TMPDIR` but excludes credential and Talos
configuration variables. The clean-install and signed-upgrade target checks each
passed twice consecutively after that correction; this initial failure is not counted
as a clean first-run pass.

Still open: the **private mirror's** GitHub Actions jobs are stopped by its account's
billing/spending gate before any test step; the public release-source jobs above are
green. Three consecutive beta release candidates have not been cut, and the seven-day
operating window has not elapsed. The live Pi was not rolled back merely to rehearse
recovery. A green local gate does not substitute for those missing proofs.
