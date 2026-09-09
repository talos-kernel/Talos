# Verification scope

## 0.19.1-alpha

The published runtime is built from `075b0ca`. Source checks passed 2,777 tests with
8 platform skips on macOS. All 230 adversarial cases passed. The signed archive was
installed with the actual Bash installer into a fresh isolated directory: 2,774 tests
passed and 36 were skipped, including repository-only checks absent from the archive.
That installer invokes pytest from the archive root and includes the 25 Mac profile
tests; the source count above uses `tests/` only. Signatures, hashes, all 127 runtime
files, the command link, empty initial allowlist and configuration permissions were
independently checked. OSV reported no findings in runtime, development, Computer
or Swift pins. All 44 real-model E2E cases also passed with Claude CLI, using an
isolated chat transport and disposable test files. This exercises inference, the kernel,
approvals, execution, cancellation and history without polling a real Telegram bot.
The release remains an alpha; the Computer requires separate provisioning.

This release bundles task-scoped consent, command and queue handling, service-owned
cron dispatch, confirmed media delivery, consultation follow-through, temporary
Telegram progress and Computer observations with an optional takeover keyboard.
The source suite collects 2,785 tests and contains 230 adversarial cases. Tests
exercise separate kernel decisions for capture and image analysis, protected-file
rejection, cancellation, bounded repair and delivery failure without action replay.

Controlled tests on a provisioned Computer also read a random value from a disposable
browser window. Each completed with one capture, one image analysis and two reasoning
calls. After twenty dashboard refreshes, the saved agent image remained readable with
the same SHA-256. View checks covered expand, zoom, fullscreen and mobile layout.
These are bounded fixtures, not a promise about every website or task's running time.

For the published archive's exact installation results and release identifiers, see
[the online verification record](https://talos-agent.ch/docs/verification.md) and
[release assets](https://github.com/talos-kernel/talos/releases/tag/v0.19.1-alpha).
No operator profiles, credentials or private test captures are part of this source.

## Earlier 0.19.0-alpha baseline

The 0.19.0-alpha runtime is built from `b7e057e`. Source checks collected 2,623 tests:
2,617 passed and 6 platform-specific tests were skipped on macOS. All 217 adversarial
cases passed. Installing the signed archive into a fresh environment yielded
2,588 passes and 35 skips; repository-only checks are absent from release archives.
The separate Mac profile suite passed 25 tests. Pinned runtime, development, Computer
and Swift dependencies had no reported OSV findings in the release scan.

The published installer was also executed unchanged in an isolated installation directory.
Its root-level test run (including the Mac profile tests) passed 2,614 tests with 34 skips;
all 217 adversarial cases passed. Downloaded signatures, checksums, installed source,
configuration permissions and the command link were checked independently.

## Completion pilot

Completion candidate `604259e` completed the same 30 controlled headless tasks twice,
with correct results and normal termination in all cases. Four additional tasks also
passed. This pilot used one model, fixed budgets and isolated workspaces. It is evidence
for those tasks, not an independent ranking or a general superiority claim. The later
stable-tab browser correction was tested separately.

## Browser and control

`tests/computer_browser_e2e.py` runs real Chromium actions against temporary local forms:
registration, contact, cancellation, asynchronous receipts, required fields, a rejected
submission and a controlled security-confirmation fixture. It checks visible confirmation,
HTTP responses and a durable fixture ledger. Stable tab IDs prevent a reconnect from
selecting another page. These checks do not submit production registrations or establish
that every external site or CAPTCHA can be automated.

Live private-Computer checks also exercised VNC keyboard input, interruption of a running
job, rejection of agent actions during human control, no replay after hand-back, a newly
verified file checksum and persistent zoom preferences. Test data stays private.

## Provider recovery and Telegram

`tests/test_operator_recovery_integration.py` uses the actual Telegram HTTP client,
Conductor, Worker, ModelRouter and kernel against a local Telegram server and injected
provider errors. It verifies reachable control commands, explicit model switching,
retained request context and exactly one tool execution. Synthetic provider failures
are not live account outages. Transport checks and controlled repetition do not replace
long-term production observation.

A 30-minute ARM64 Linux soak completed 186 isolated fault cases in 62 rounds. Each
case checked control commands, explicit model recovery, retained context, one tool
execution and an empty queue at the end. The test process returned to one thread
after every round; the separately observed agent service had no automatic restarts.
This is a bounded synthetic-failure test, not a multi-day uptime measurement.

Run the repository suite with `python -m pytest tests/ -q` and adversarial checks with
`python redteam.py`. Browser fixtures require a separately provisioned Computer; see
[Computer setup](computer.md). A generated test or an unfinished run is not a pass.
