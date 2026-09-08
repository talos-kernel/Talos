# Verification scope

The 0.19.0-alpha runtime is built from `b7e057e`. Source checks collected 2,623 tests:
2,617 passed and 6 platform-specific tests were skipped on macOS. All 217 adversarial
cases passed. Installing the signed archive into a fresh environment yielded
2,588 passes and 35 skips; repository-only checks are absent from release archives.
The separate Mac profile suite passed 25 tests. Pinned runtime, development, Computer
and Swift dependencies had no reported OSV findings in the release scan.

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

Run the repository suite with `python -m pytest tests/ -q` and adversarial checks with
`python redteam.py`. Browser fixtures require a separately provisioned Computer; see
[Computer setup](computer.md). A generated test or an unfinished run is not a pass.
