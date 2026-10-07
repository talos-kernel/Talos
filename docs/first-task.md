# Your first useful task with Talos

Try a small job with a result you can inspect. These three recipes use only fictional
data. They are **instructions to reproduce**, not transcripts, benchmarks or claims
that every model will succeed. Keep real customer data and credentials out of a trial.

New here? Start with the [installation guide](../README.md#install) and the
[model, cost and permission FAQ](https://talos-agent.ch/faq/). Use your existing model
connection; provider usage may cost money. No new gateway subscription is required.

## Before the first conversation

In your installation directory, using its Python environment:

```bash
python -m talos version
python -m talos doctor
python -m talos chat
```

If setup is incomplete, run `python -m talos setup terminal` first. This configures the
local identity and model; it is not necessary to create a Telegram bot. With the signed
installer, use the `bin/talos` commands printed at the end of installation instead.
Do not configure a second Telegram poller beside an existing service.

Use an attended terminal for these tasks. Each prompt below is deliberately one line:
`talos chat` submits each newline as a separate turn. Paste one prompt, then wait for
the result before starting the next. `talos ask`, redirected output and background
runs cannot collect interactive approvals. Resolve relevant doctor findings; do not
turn off sandboxing or loosen policy to make a demonstration pass.

## 1. Turn rough notes into a useful handover

Paste this into the chat:

```text
Turn these fictional meeting notes into a short handover with three columns: action, owner, due date. Answer here only; do not use tools or write files. Notes: Rowan owns the draft and will finish it on 12 November 2026. Casey will review the draft on 13 November 2026. The launch date has not been decided. No owner was assigned to launch planning. Do not invent missing dates or owners.
```

**Check the result:** Rowan / draft / 12 November; Casey / review / 13 November;
launch owner and date explicitly unknown. This checks a useful model response, not
the permission kernel. No tool should have run.

## 2. Save a local note, then verify the actual file

Use a fresh filename if this example already exists. Paste:

```text
Create workspace/first-task-note.md only if it does not already exist. Its exact content must be two lines, with a newline after each. The first line is "# Trial handover" and the second is "Draft: Rowan. Review: Casey. Launch: undecided."; omit the enclosing quotation marks. Use file tools only, no shell or network. Read the file back after writing. Report the real tool outcome; do not claim success if the write or read failed. If the file already exists, stop without changing it.
```

Review any kernel-generated approval before answering it. An ordinary reversible
workspace write may already be allowed at your configured autonomy level; an approval
dialogue is **not** promised for every write. Do not choose a broader permission just
to reproduce this recipe.

**Check the result independently:** open `workspace/first-task-note.md` in your editor.
It must contain exactly the two requested lines. Repeating the prompt must leave the
existing file unchanged. A reply saying “saved” is not evidence on its own.
Keep the harmless note or remove that specific file yourself after inspecting it.

## 3. Compare two changes without inventing a launch claim

Paste:

```text
Compare these fictional release notes. Answer here only, with no tools. Before: CSV export is available. Scheduled reports are not available. After: CSV export remains available. Scheduled reports are now in preview. Known issue: preview reports sometimes arrive twice. Give one unchanged feature, one new feature, one caveat and one check before use. Do not describe preview as production-ready, or claim the duplicate issue is fixed.
```

**Check the result:** CSV export is unchanged; scheduled reports are a preview;
duplicate delivery remains a known issue; a practical check should inspect for duplicate
reports before relying on them. This is a model-quality exercise, not a real Talos bug
report or a feature announcement.

## How approvals fit a longer task

When Talos actually asks, **Allow once** covers the displayed action. **Allow this task**
covers eligible actions within the current task's enforced scope; it is not approval
for arbitrary future work. Exact-action standing approvals are a separate operator
choice. Hard denials remain denials. See the
[permission FAQ](https://talos-agent.ch/faq/#task-approval) before changing those settings.

After a tool-backed task, use `python -m talos status` and `python -m talos verify` to
inspect the installation's recorded activity and chain. A valid chain checks consistency
of the records, **not** that the answer was correct or an external side effect succeeded.
It does not protect against an administrator rewriting the entire local chain; see
[the security limits](../SECURITY.md).
Review outputs locally: logs can contain private paths and task content.

## Reproduce the mechanics without a model account

For a source checkout with the [development dependencies](../CONTRIBUTING.md#set-up)
installed:

```bash
python scripts/demo-proof.py
```

This runs three labelled groups of existing regression tests: file read/write and
receipts; approval gating, task scope and one-use capabilities; rollback and audit integrity.
The tests use temporary files and controlled runners, with **no model or provider
call**. They are not recordings of the three conversations above, a full installation
test, a live messenger test or a speed comparison. Read the exact selected tests in
[the script](../scripts/demo-proof.py). To run one group: `--case approval`.

For actual model-backed evidence, run the recipes yourself and record the model,
Talos version, task outcome and any correction needed. Do not publish a staged
terminal animation as a real conversation.

## Help another person get started

We are looking for independent first-install feedback on macOS, Linux, Raspberry Pi
and VPS setups. Completing all three tasks is optional: **a blocked install is useful
feedback too**. There is no star, account-upgrade or positive-review requirement.

[Share a first-run result](https://github.com/talos-kernel/Talos/issues/new?template=first_run.yml)
with your OS, version, install method and which checkpoint worked or failed.
Share only a short, manually redacted excerpt if one is necessary. Never attach
`talos.env`, `.env`, OAuth files, full event logs, private hostnames or personal paths.
For a security vulnerability, use [private reporting](../SECURITY.md), not a public issue.

Want to build instead? [Fork Talos](https://github.com/talos-kernel/Talos/fork), read
[CONTRIBUTING.md](../CONTRIBUTING.md), and choose a
[small existing issue](https://github.com/talos-kernel/Talos/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
