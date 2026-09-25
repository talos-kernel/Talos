# Beta readiness

Talos is still labelled alpha because the deterministic permission kernel is stable,
while the operator-facing surface is still allowed to change. This document makes the
exit criteria explicit instead of treating `beta` as a calendar decision.

## Current baseline — 0.19.23-alpha

| Gate | State | Evidence |
|---|---|---|
| Deterministic kernel and approval boundary | green | 263/263 adversarial cases |
| Regression suite | green | 2,900 collected tests; public run: 2,923 passed, 2 skipped |
| Dependency security | green | OSV scan: no findings |
| Public-repository hygiene | green | `scripts/check-public-hygiene.py` passed |
| Signed release artifacts | green | Ed25519 archive signature and SHA-256 read back over HTTPS |
| Pi5 deployment and rollback | green | Backup before deploy, rsync read-back, service restart and version read-back |
| Cross-platform hosted CI | blocked externally | GitHub account billing/spending limit prevents jobs from starting; no test step runs |
| Stable operator/API contract | in progress | `/eval`, `/heartbeat`, permission grants and configuration are still evolving |
| Upgrade matrix across supported Python versions | not yet evidenced | Current workflow executes Python 3.11 on Ubuntu and macOS when billing permits |
| Long-running stability evidence | not yet complete | Requires a defined observation window with no critical regression |

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

## Exit to 1.0

`1.0.0` is a separate promise: the beta contract must have survived a real operating
window without a critical security or data-loss regression, and the public upgrade path
must be documented for at least one previous beta release. The kernel may become stricter
without a major version; changing the meaning of an existing approval, tool, identity or
stored schedule requires an explicit migration or a major release.

## What can and cannot be done locally

The test, red-team, hygiene, release-signing, deployment, migration and rollback work can
be executed locally and on the Pi. GitHub Actions billing, payment-method repair and
account spending limits require an account-owner action; they cannot be fixed in source
code. Until that is resolved, local green results remain valid evidence but are not a
substitute for the hosted cross-platform gate.
