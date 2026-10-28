# From beta to a stable 0.20.0

The next proposed release is `0.20.0-rc.1`, followed by `0.20.0` only after the
evidence below passes. This is a qualification plan, not an announcement that
either version has shipped. The published baseline is `0.20.0-beta.9`.

The historical `0.20.0-alpha.rc.*` artifacts qualified the earlier transition
**into beta**. They are not candidates for this transition out of beta. `1.0.0`
remains a separate compatibility commitment, not an automatic rename of 0.20.0.

## Scope freeze

Keep the [operator contract](beta-contract.md), command names, approval meanings,
configuration and durable-state boundaries. During candidate qualification, accept
release-blocking fixes and their regression tests; defer unrelated features. Any
compatibility change needs a before/after test and migration instructions.

Terminal and Telegram on the documented Linux/macOS and Python 3.11–3.13 targets
remain the scope. A CLI release does not certify Windows, every optional computer
integration or a notarized desktop app. Unsupported configurations must be named,
not silently included in a broad claim that everything passed.

## Candidate and stable gates

| Gate | Required evidence |
|---|---|
| Source | Exact commit, clean checkout, full regression and adversarial suites; release-tooling and embedded verifier-pin checks; public hygiene and a reviewed secret scan. |
| Dependencies | Fresh OSV scans of all four locks: runtime, development, signature verifier and optional computer environment. Check installed distributions too, including pip. No unresolved critical/high finding. |
| Supported platforms | Native macOS and Linux hosted jobs on the exact commit; local Python 3.12/3.13 and ARM64 Linux checks with explicit skips and reasons. |
| Real model flow | Isolated real-model E2E: read, approval/refusal, exactly-once effect, restart/state restoration and the event chain. Record provider and results; do not send external test messages. |
| User-visible flow | Telegram reply/approval/background completion behavior, including conversation isolation and no false completion while waiting. Injected transport tests and an actual service probe are distinct evidence. |
| Exact artifact | Build from the qualified public commit; SHA-256, pinned Ed25519 verification, tamper rejection and archive hygiene. Source tests alone do not qualify an archive. |
| Installation/recovery | Clean install and beta.9→RC signed upgrade on macOS and ARM64 Linux; check operator identity/configuration, selected model, history, schedules, file modes and rollback. Rehearse isolated service restart and retain the prior runtime. |
| Delivery | Native Homebrew lifecycle, Pinokio clean/repeated install, and byte-for-byte HTTPS read-back of the release, website and installer definitions. Check main CI after each merge. |
| Observation before stable | Seven consecutive calendar days on a deployed RC, with daily health/event-chain records, investigated errors, no unexplained restart and no unresolved security or data-loss regression. Preserve failed samples. A release-blocking runtime fix starts a new candidate/window. |
| Stable promotion | Rebuild/sign the final version and requalify its exact bytes, including RC→stable upgrade and recovery. A version-only change is still a new artifact. |

The [observation collector](beta-observation.md) can keep its existing name and
timer. A record must identify the running version. Beta observations establish
history; they must not be relabelled as days on a newer RC. Model/network errors
need diagnosis; neither an arbitrary error-count threshold nor `exit 0` alone
certifies stability. Human review is still required for the release conclusion.

## Reproducible local preflight

Start with the current published baseline in a prepared, hash-locked development
environment:

```sh
python scripts/beta-gate.py --previous 0.20.0-beta.8 --current 0.20.0-beta.9
python -m pytest macos/Tests -q
python e2e.py
```

The gate requires an explicit, different `--previous` version. It checks release
tooling, source tests, adversarial cases, hygiene, embedded verifier pins, all four
dependency locks, a disposable clean install, and signed upgrade/migration/rollback.
`--without-upgrade` is a partial source check and says so; it does not qualify a
candidate. Tests and artifact downloads require their normal dependencies/network.

For an unpublished candidate, use `--current 0.20.0-rc.1`, `--previous
0.20.0-beta.9`, an explicit `--base` pointing to the candidate's `dist/` directory
parent, and `--assets-dir` containing both versions' archive/checksum/signature.
The checkout's installer must name the candidate. The existing scripts support
`file://` for an isolated pre-publication install. Run against the actual signed
bytes; do not change signature pins or skip verification to make a rehearsal pass.

Retain full command logs and first failures privately. Public evidence contains
only redacted results, exact commit/artifact identities and links. Retrying an
unchanged failure is not a fix; disclose retries and the actual cause.

## Publication order

1. Qualify the exact source and candidate archive before changing public pointers.
2. Publish the immutable signed GitHub assets; download and verify all three bytes.
3. Publish website assets and pages with backups/read-back, switching `latest.txt`
   last. Check its actual prior value rather than copying an old release constant.
4. Only then trigger Pinokio's live installer checks: its pinned installer downloads
   the archive from the website. Verify the installer digest against the exact
   published script before pushing a pin update. Qualify Homebrew's native lifecycle.
5. Merge package updates after their checks pass, verify public bytes and all main
   CI jobs, then perform the separately backed-up operator rollout and read-back.

Never rewrite an existing signed archive/tag or delete failed CI history to make
the release look clean. Publishing a release does not itself update a running
installation or restart its service.

## Qualification ledger

| Artifact | State | Evidence boundary |
|---|---|---|
| `0.20.0-beta.9` | Published baseline | [Core main CI](https://github.com/talos-kernel/Talos/actions/runs/40661310306), [Homebrew](https://github.com/talos-kernel/homebrew-tap/actions/runs/40661911664), [Pinokio](https://github.com/talos-kernel/talos-pinokio/actions/runs/40663657826). These qualify beta.9 only. |
| `0.20.0-rc.1` | Preparation started; not published | Needs its own commit, signed artifact, complete platform/install/recovery evidence and operator rollout. |
| `0.20.0` | Not qualified | Needs the RC observation window and final-artifact gates above. |
