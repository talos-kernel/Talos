# Beta baseline evidence — 2026-10-01

## Qualified public source and artifact

RC6 public source: `d543e026f70f7bc8f6943b85e92c981a3ecc09ff`.
Exact signed archive: `talos-0.20.0-alpha.rc.6.tar.gz`, 1,431,138 bytes,
SHA-256 `31c5d3f25c0bd9ec4f6f4b9575af012fe3269ce410288e83bf7bad00ed76d753`.
Ed25519 verification and a modified-payload rejection test passed. The archive was
scanned separately: 415 members, 392 text files, no private configuration or state.
It is a qualified candidate artifact, not a separately published RC6 release.

[Public CI 36842049693](https://github.com/talos-kernel/Talos/actions/runs/36842049693)
passed on Python 3.11 macOS and Ubuntu. Each job passed 2,931 tests with two explicit
repository/environment skips and all 263 adversarial cases. macOS additionally
passed 50 desktop tests and built/deep-strict-signature-verified the app preview.
The source collects 2,933 tests; collected count is not a claim that every platform
executes every case. Local Python 3.11 and 3.13 source runs passed all 2,933 tests.

Explicit runtime/development Python locks and Swift `Package.resolved` were scanned
with OSV: zero findings after the urllib3 2.8.0 fix. Public hygiene and new-diff secret
scans passed. Complete public history retained only the ten previously reviewed
inert fixture/public-verification findings, with no new finding.

## Exact-archive platform checks

| Check | macOS | ARM64 Linux |
|---|---|---|
| Clean install, version/config/command read-back | Passed | Passed |
| Upgrade from 0.19.23-alpha, retained state and schedule migration | Passed | Passed |
| Rollback to 0.19.23-alpha | Passed | Passed |
| Upgrade from RC5 and rollback | Passed | Passed |
| Isolated native service start/restart, distinct processes | launchd passed | systemd passed |
| Event chain and schedule retained after restart | Passed | Passed |
| External connections from service fixture | Zero | Zero |
| Streaming/deflate/chunk-header transport probes | Passed twice | Passed twice |
| Fixture cleanup | Passed | Passed |

The real-model E2E suite passed **44/44 on its only run against this public archive**.
The messenger transport was an isolated sink, not a second live Telegram poller.
Production services and the installed desktop application were not replaced.

No qualification test failed or required a retry. One SSH connection ended after
test-environment dependency installation, before a qualification check started;
the completed install was inspected before continuing. Earlier private fixture
development failures remain in the private audit and are not erased by this result.

## Seven-day operating observation

Seven consecutive calendar dates, September 25 through October 1, have successful
daily health, chain and explicit verification records. Counts are nondecreasing.
These records describe the operating alpha installation, not seven days of RC6 or
beta operation. Historical samples are not retroactively upgraded to the new
channel-health semantics.

This was **not error-free or uninterrupted uptime**:

- Transient channel failures were reviewed rather than hidden by a green sample.
- A September 27 compression timeout evicted six active prompt-context entries.
  Every underlying delivered question/answer represented by those entries remains
  in the durable transcript. The generated summary text itself was not preserved
  verbatim. This was a Medium continuity incident; bounded compression retry is
  covered by the subsequent candidate regressions.
- A September 29 restart was an explicit, orderly maintenance restart. The host
  journal records the restart command; the operator separately confirmed the
  authorized registry reload on October 1. The originating client session was not
  forensically attributed, and human confirmation is not presented as such proof.
- A September 30 restart activated the documented channel-health hotfix.

The documented observation criterion passes with these qualifications: no critical
security or durable-data-loss regression and no unexplained restart remained in the
reviewed window. This does not guarantee that future runs cannot fail.

## Beta publication boundary

`0.20.0-beta.1` promotes this runtime with version/documentation metadata only.
Final beta assets are signed and independently requalified before publication;
their checksums and exact source reference belong in the
[release record](https://github.com/talos-kernel/Talos/releases/tag/v0.20.0-beta.1),
not a self-referential checksum inside the archive. The frozen operator contract
and recovery instructions remain separate from the evidence. No running instance
is automatically upgraded by publishing the beta.
