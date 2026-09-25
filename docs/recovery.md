# Operator recovery

These steps preserve evidence and avoid a second writer. Replace paths with the
installation's real absolute path. Do not paste `talos.env` or event-log contents into
an issue or chat; they can contain credentials and private work.

## Failed update before the switch

The updater leaves the old tree in place and discards its staging tree. Record the
exit code and the failing step. Check free space, Python 3.11+, network/TLS and the
signed release assets. Do not bypass signature, checksum, lockfile or test failures.
Run `talos update --check` only after the underlying fault is understood.

## Bad result after a successful switch

1. Stop the service or interactive process and confirm it is no longer running.
2. Locate the exact `<install>.old-<previous-version>` directory reported by the
   updater. Do not delete either tree or use a wildcard.
3. Keep the new tree as a dated recovery copy, then move the old tree back to the
   original installation path. Preserve file owner and permissions, especially for
   `talos.env` if it was root-owned.
4. Check `talos version`, `talos doctor`, `talos health` and `talos verify` from the
   restored tree before restarting. Start the service once, then check its status.

The old tree contains operator state from the moment of the update. Work and events
created in the new tree afterwards are **not** automatically merged. Keep both
trees until the difference has been reviewed and backed up. A new database schema
may not be readable by old code; copying the new `data/` into the old tree is not a
safe default.

## Model configuration or provider failure

Run `talos doctor` and `talos config list` first; `config get` deliberately redacts
secrets. Correct the model with `talos setup model` or a validated `config set` for
a `SETTING` key, then restart the service if file settings changed. A missing key is
not a reason to widen the allowlist or disable the kernel. Use `talos health` and a
single harmless test question to read back recovery.

## Lost or confusing approvals

Use `/pending` and `/allowed` to inspect current requests and standing grants.
`/deny` closes a pending request; `/revoke <n>` removes a standing rule. `/stopall`
discards pending approvals and cancels work at step boundaries; it never approves
anything and does not remove schedules. Start a fresh task after the stop and check
that an approval-required action asks again. Do not rebuild approval state from a
model's summary; the kernel and event log are the record.
