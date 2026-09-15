# The demo GIF — how it is made, and what in it is staged

`../talos-demo.gif` shows `talos chat`: the banner, a task with a write effect,
the kernel's approval question, the approval, the sandboxed execution and the
audit trail (`talos events`). Rebuild it with:

```bash
assets/demo/build_gif.sh                 # ollama/qwen3.8:latest (local, no account)
DEMO_PROVIDER=claude-cli DEMO_MODEL=… assets/demo/build_gif.sh
```

**What is real:** the banner, the kernel verdict, the approval text (kernel
facts, never model prose), the sandboxed `rm -r`, the event log. The model is a
real configured reasoner; the build script picks the provider because quotas
change (`DEMO_PROVIDER`/`DEMO_MODEL`).

**What is staged, honestly:**

- `workspace/old-logs/` is created by the build script so there is a harmless
  thing to delete.
- The autonomy dial is pre-seeded to **3 ("ask")** through the event log — the
  same way an operator's setting survives restarts. On the shipped default (5)
  the attended auto-approval would let a *sandboxed* `rm -r` run without a
  question, and the recording exists to show the question.
- The operator's lines are typed by `record_demo.py`.

**Why asciinema + agg and not VHS:** the model's latency varies from run to
run, and VHS 0.12 can only wait on fixed sleeps — a sleep-based tape types
"yes" into a question that is not on screen yet. `record_demo.py` drives a pty
and waits on screen *patterns* behind a read cursor, so the recording cannot
mistime. Both tools are single static binaries (no package manager needed);
pauses over two seconds are capped, so model latency does not inflate the GIF.

**Provenance of the shipped GIF:** recorded 2026-09-15 with
`ollama/qwen3.8:latest` (local, no account) — the claude-cli route was
quota-limited that day, and a local model is the honest default for a demo.
Re-record any time with the commands above.
