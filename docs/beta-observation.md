# Seven-day Pi observation

The optional `scripts/beta-observe.py` collector runs local `talos health --json`,
`talos verify`, `talos version` and a narrow `systemctl --user show` query once per
day. It writes only allowlisted status, counts and the service start timestamp to a
mode-0600 JSONL file in a mode-0700 directory. It never stores prompts, chat
messages, raw errors or CLI output, and it does not call a model or network service.

Install the example user service and timer from `deploy/talos-beta-observe.*` in the
operator's user systemd directory. The local environment file named in the service
must set `TALOS_BETA_ROOT` to the installation root and `TALOS_BETA_OUT` to an
absolute, operator-owned JSONL path. Do not put API keys or `talos.env` in that file.
The timer runs at 09:45 local time with `Persistent=true`. A nonzero service result
or `ok=false` is an attention signal; it does not trigger a recovery action.

Review seven **consecutive calendar days** of records. Check every day has a sample,
the event-chain count is nondecreasing, the chain and explicit verify remain good,
the service start timestamp has not changed unexpectedly, and no critical security
or data-loss regression occurred. `errors_24h` is diagnostic, not an automatic
failure threshold; investigate a rise without recording raw private text in the
release evidence. A failed day resets the seven-day window after diagnosis and fix.
The collector provides evidence; it cannot by itself certify beta readiness.
