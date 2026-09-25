# Beta release-candidate ledger

The baseline contract is frozen at [the beta contract](beta-contract.md). An RC
counts only when it has a distinct versioned source commit and signed release
artifact, local full gate, public hosted macOS/Ubuntu CI, OSV and hygiene results,
install/upgrade/rollback read-back, and a completed 20-point pre-launch security
audit. A rerun of the same artifact does not create another RC. Three consecutive
RCs must meet the gate without an unresolved critical/high finding.

| Candidate | State | Evidence |
|---|---|---|
| RC1 — 0.20.0-alpha.rc.1 | **passed, 1 of 3** | Signed artifact at `c14d3c8`; local gate, hosted macOS/Ubuntu CI, OSV/hygiene, Mac/Pi install/upgrade/rollback and pre-launch audit passed. [Executed evidence](beta-evidence-rc1-2026-09-25.md). |
| RC2 | source preparation passed, **not counted** | App build/provenance, pinned installer verifier and live-model E2E checked at `a8f165f`; [hosted CI](https://github.com/talos-kernel/Talos/actions/runs/36137817309) passed. Still requires a distinct versioned, signed release artifact after RC1. |
| RC3 | not started | Requires a distinct versioned, signed artifact after RC2. |

RC2 preparation (2026-09-25): hosted Python 3.11 macOS/Ubuntu each passed 2,912
tests with two repository-specific skips and 263 adversarial cases. macOS additionally
passed 50 desktop tests and built the ad-hoc signed preview from clean public source.
The 44-case real-model suite passed twice after harness fixes; earlier setup/CLI and
text-assertion failures were retained in the audit. Locked Python/Swift OSV scans
found no issues. The revised installer passed clean installation on Mac and ARM64
Linux using the existing signed RC1 archive; poisoned verifier hashes were rejected.
These are source/bootstrap checks, not a new signed RC. The app preview is not
notarized. A fresh visual E2E of the 0.3.3 preview passed: account status, attended
chat, project/model-origin separation and persisted history were checked. This
desktop verification does not replace the distinct RC2 signed-archive gate.

Do not label any artifact `0.20.0-beta.1` or update the website's latest-version
pointer until the release audit and beta exit gates are complete. Track a failed
candidate as failed; do not silently erase it from the sequence.

RC1 compatibility review: [dashboard Host/Origin boundary](dashboard-rc1-migration.md).
This tightens a read boundary; private proxies preserving their Host header need
an explicit operator environment setting. Tool grants and stored state are unchanged.

RC1 pre-publication build `ccf1f6b` failed clean installation on both Mac and Pi:
the archive excluded `scripts/beta-observe.py`, while its shipped regression tests
imported that module. No release tag or asset was published. The installer stopped
before configuration/startup. This build does not count toward the passing sequence;
the corrected archive must repeat the complete artifact gate.
