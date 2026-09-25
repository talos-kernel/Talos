# Frozen beta contract baseline

This is the frozen operator-facing baseline for the `0.20.0-beta` release-candidate
line, based on public source commit `8376890` and running `0.19.23-alpha`. It does
not retroactively freeze the alpha line or claim a beta release. Before the first
beta tag, every item needs a corresponding test and migration note where a prior
alpha behaved differently. Any change to this baseline requires an explicit
compatibility review and entry in the [RC ledger](beta-rc-ledger.md).

## Supported platforms

| Surface | Beta target | Limit |
|---|---|---|
| Host | Linux and macOS | Windows is not a tested host. |
| Python | 3.11–3.13 for the beta target | Hosted CI must run 3.11 on both systems; a newer interpreter needs explicit evidence before it is promised. |
| CPU | x86-64 and ARM64 | The Pi5 is the ARM64 Linux operating sample. |
| Entry points | Terminal and Telegram | Mail is optional and needs its own configuration. |
| Model connection | Configured API or signed-in supported CLI | No model account, API credit or subscription is bundled. |
| Computer | Optional, separately provisioned | Its browser checks do not imply general site compatibility. |

## Commands and permission semantics

The command names and arguments in [command compatibility](command-compatibility.md)
are the frozen baseline. New optional commands may be added with tests; an existing
name, argument or approval meaning must not be silently repurposed. Exact wording of
model answers and cosmetic CLI prose is not a compatibility promise. `/eval` is local
advisory preflight; it never replaces a model
call or grants permission. `/heartbeat` controls scheduled idle work; an unattended
run remains under `UnattendedCeiling` and cannot obtain a human approval.

Every proposed tool action receives a deterministic `ALLOW`, `NEEDS_HUMAN` or `DENY`
before execution. `DENY` is not approvable. "Allow once" is bound to one exact
action; "Allow this task" lasts only for the current foreground task; "Always allow"
is bound to an exact fingerprint and remains revocable. A plan, model statement,
background run, schedule or local classifier never creates authority. A future beta
may tighten a boundary, but any changed meaning of an existing grant needs an
explicit compatibility note and test.

## Configuration and durable state

`talos.env` and operator-owned `SOUL.md`, `AGENTS.md`, `USER.md`, `data/` and
`workspace/` survive a signed update by copy. `config set` only writes keys marked
`SETTING` in `talos/schema.py`; `SECRET` and `POLICY` require the appropriate setup
or operator file edit. A secret read is always redacted, including when unset.

`data/schedules.db` migrations are additive; existing tasks retain their identity,
prompt, principal and timing, and new flags default off. No migration may silently
delete or broaden a task. The event log and schedule database are operator data,
not replaceable package files.

## Updates and compatibility

`talos update --check` only reads the version pointer. A full update checks SHA-256
and the Ed25519 signature pinned in the running installation, builds a sibling tree,
installs hashed dependency locks and runs regression and red-team suites before
switching. It copies operator state, keeps the previous complete tree and starts or
restarts no service. An operator must restart a running service after the switch.
The new tree's state copy can diverge from the old tree after a restart; rollback
must therefore follow [the recovery runbook](recovery.md), not assume that later
writes appear in both trees.

No silent field removal, argument repurposing, approval broadening or destructive
schema migration within the beta line. A necessary breaking change needs a versioned
migration note, a before/after test and an explicit operator action. This baseline
becomes a release promise only when the beta exit gates pass.
