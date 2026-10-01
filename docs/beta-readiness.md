# Beta readiness

Talos is still labelled alpha because the deterministic permission kernel is stable,
while the operator-facing surface is still allowed to change. This document makes the
exit criteria explicit instead of treating `beta` as a calendar decision.

## Local candidate — 0.20.0-alpha.rc.6 (not published)

RC6 combines the channel recovery health check with urllib3 2.8.0 security fixes.
Its changed locks, complete source and exact artifact require fresh qualification;
earlier RC results below do not certify RC6. No beta release or production upgrade
is implied. Seven daily observer records exist for September 25–October 1, but
incident and restart review remains separate from the collector's successful samples.

## Historical baseline — 0.19.23-alpha; candidate — 0.20.0-alpha.rc.4

The following table is the previous RC4 evidence snapshot, not a current security
or observation verdict.

| Gate | State | Evidence |
|---|---|---|
| Deterministic kernel and approval boundary | green | 263/263 adversarial cases |
| Regression suite | green for RC4 | RC4 local: 2,915 passed; public hosted macOS/Ubuntu CI run 36388585774 passed; RC4 adds the compression-retry regression |
| Dependency security | green | OSV scan: no findings |
| Public-repository hygiene | green | `scripts/check-public-hygiene.py` passed |
| Signed release artifacts | green for RC4 | RC4 archive SHA-256 `badd2224b2297e98cf7bb6ce664f8ba9c31984ccdf91424d92504128a51c72ff`; Ed25519 verification, tamper rejection and GitHub HTTPS read-back passed |
| Pi5 deployment | previous alpha only | Production remains 0.19.23-alpha; candidates qualified in isolation, not deployed over the operating instance |
| Clean install and signed upgrade | green for RC4 | RC4 clean install, 0.19.23-alpha/RC3 upgrades, schedule migration, rollback and state read-back passed in isolation |
| Candidate service restart | green in isolation | RC2 and RC3 launchd/systemd start/restart retained schedules and valid event chains; empty messenger fixture, no external sockets; fixture units unloaded |
| Production rollback | not executed | The live service was deliberately not rolled back; this remains an incident-only operation |
| Cross-platform hosted CI | green on public source | RC3 Python 3.11 macOS/Ubuntu passed; macOS additionally built and verified the preview in [public CI](https://github.com/talos-kernel/Talos/actions/runs/36141877494); the private mirror remains billing-blocked before any step |
| Release candidates | green, RC1–RC4 | RC1–RC3 qualified the original sequence; RC4 independently qualified the compression-retry fix; [ledger](beta-rc-ledger.md) |
| Stable operator/API contract | frozen, shipped in RC1 | [Beta contract](beta-contract.md); compatibility changes require review and migration notes |
| Upgrade matrix across supported Python versions | local green | Python 3.11, 3.12 and 3.13 passed on macOS; 3.13 passed on Pi5; public 3.11 hosted CI is green; private mirror billing remains blocked |
| Long-running stability evidence | in progress | Three consecutive days recorded (25–27 September); four further consecutive days are required; [observation procedure](beta-observation.md) |

## Exit to beta

Talos can move to `0.20.0-beta.1` when all of these are true:

1. GitHub Actions runs successfully on both supported operating systems; a red run must
   represent a real test result, not an account-level billing failure.
2. The command, permission, configuration, schedule and update contracts are documented
   and frozen for the beta line. Breaking changes require a migration note.
3. A clean install, upgrade from the previous alpha, schedule-database migration,
   backup/rollback and service restart are executed and read back on the Pi and one Mac
   or Linux installation.
4. Three consecutive release candidates pass the full suite, red-team suite, hygiene
   check and dependency scan, with no unresolved critical or high-severity finding.
5. The project has an explicit support matrix and a short recovery runbook for failed
   updates, broken model configuration and lost approval state.
6. A seven-day observation window on the operating Pi has daily health and event-chain
   checks, no critical security or data-loss regression, and no unexplained service
   restart. This is separate from a one-hour test run or a successful deploy.

## Exit to 1.0

`1.0.0` is a separate promise: the beta contract must have survived a real operating
window without a critical security or data-loss regression, and the public upgrade path
must be documented for at least one previous beta release. The kernel may become stricter
without a major version; changing the meaning of an existing approval, tool, identity or
stored schedule requires an explicit migration or a major release.

## What can and cannot be done locally

The public repository's hosted CI provides the release-source platform gate. The private
mirror's jobs are blocked by its account billing/spending setting before any step runs;
that account issue cannot be fixed in source code and should not be confused with a
test failure. Local green results and public hosted results are recorded separately.

Executed results for this baseline are in the [2026-09-25 evidence record](beta-evidence-2026-09-25.md).
Release-candidate accounting is in the [RC ledger](beta-rc-ledger.md). A source
preflight is not a counted RC or a published beta.

Run `python scripts/beta-gate.py` from a prepared development environment for source
tests, red-team cases, public hygiene, locked-dependency OSV, a disposable clean
install and an isolated signed upgrade from the previous published alpha, including
schedule migration and rollback.
The upgrade rehearsal creates and removes only a temporary installation; it does not
restart or change the operating Pi. Use `--without-upgrade` only for a partial source
check, never as evidence that the whole gate passed. See the [support and command
contract](beta-contract.md) and [recovery runbook](recovery.md).
