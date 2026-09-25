# Beta release-candidate ledger

The baseline contract is frozen at [the beta contract](beta-contract.md). An RC
counts only when it has a distinct versioned source commit and signed release
artifact, local full gate, public hosted macOS/Ubuntu CI, OSV and hygiene results,
install/upgrade/rollback read-back, and a completed 20-point pre-launch security
audit. A rerun of the same artifact does not create another RC. Three consecutive
RCs must meet the gate without an unresolved critical/high finding.

| Candidate | State | Evidence |
|---|---|---|
| RC1 | source preflight passed; **not counted** | 2,905 source tests, 263/263 adversarial cases, hygiene and OSV passed; published-alpha clean install and signed upgrade/rollback passed. Current 0.19.23-alpha baseline is not an RC artifact. |
| RC2 | not started | Requires a distinct versioned, signed artifact after RC1. |
| RC3 | not started | Requires a distinct versioned, signed artifact after RC2. |

Do not label any artifact `0.20.0-beta.1` or update the website's latest-version
pointer until the release audit and beta exit gates are complete. Track a failed
candidate as failed; do not silently erase it from the sequence.
