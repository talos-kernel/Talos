# Chat commands: Talos and Hermes

Source comparison: 2026-09-08, against the
[Hermes slash-command reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/slash-commands.md).
This describes this source branch, not an already-published release.

## Shared everyday controls

| Need / Hermes command | Talos |
|---|---|
| Help, command discovery | `/help`, `/commands`, optional search word |
| Identity | `/whoami`, local identity and chat admission |
| New context | `/new`, `/reset`; archive retained |
| Runtime status | `/status`, `/health`, `/version` |
| Background task | `/background`, `/bg`, `/btw`; separate context, labelled result, maximum three concurrent runs |
| Active background jobs | `/tasks`; only the requesting person's tasks in this chat |
| Correct an active task | `/steer <instruction>`; `/steer <bg_id> <instruction>` for a side task |
| Queue a separate next task | Telegram `/queue <task>`, `/q <task>`; bare command shows queue |
| Stop main task | `/stop`, `/cancel` without arguments |
| Stop one background task | `/cancel <bg_id>`, `/stop <bg_id>`; cancellation at the next step |
| Stop all jobs | `/stopall`, `/estop`; schedules remain installed |
| Retry last question | `/retry`; does not inherit consent or replay recorded tools |
| Model picker | `/model`, `/models`; existing configured providers, validated selection |
| Usage | `/usage`: measured tokens and calculated API-price equivalent, not a subscription bill |
| Approve / deny | `/approve [once|task|always]`, `/deny`; task lasts until completion/stop/error, no clock expiry |
| Inspect approvals | `/pending`, `/approvals`, `/allowed`, `/revoke <n>` |
| Memory | `/remember`, `/memory`, `/forget`; Talos' explicit fact store |
| Skills, tools | `/skills`, `/tools`, `/reload-skills`; live discovery, no implicit installation |
| Scheduled tasks / cron | `/every`, `/schedules`, `/unschedule`; interval and cron expressions |
| Automation templates | `/blueprint`, `/blueprints`, `/bp`; install/remove/enable/disable/status |
| Debug information | `/debug`, `/log`; local response, no automatic public upload |

## Different semantics — do not silently alias these

| Hermes | Talos distinction |
|---|---|
| `/undo` removes an exchange; `/rollback` restores checkpoints | Talos `/undo` restores the last successful file change through the kernel. It is not conversation deletion. |
| `/yolo` toggles approval bypass | Talos offers explicit **Allow this task**. Hard denials, capability checks and sandbox boundaries remain active. |
| `/reasoning <level>` controls effort/display | Talos `/reasoning` currently explains the active reasoning path; it does not implement Hermes' effort/display controls. |
| `/debug` uploads a report | Talos displays diagnostics locally; it does not publish private logs. |
| Account credits / billing / top-up | Nous-specific billing has no equivalent in a provider-neutral Talos subscription setup. |

## Not implemented as matching chat features yet

- Named session switching: `/sessions`, `/title`, `/resume`, Telegram `/topic`, cross-platform `/handoff`.
- Manual context compression, persistent `/goal`, session `/personality`, `/fast`, configurable `/footer`.
- `/voice` controls and Discord voice-channel joining; Talos' audio tools are separate capabilities.
- `/insights [days]`, `/diff`, multiple-checkpoint `/rollback` selection.
- MCP hot reload, gateway `/platform` pause/resume and `/sethome` management.
- Hermes-specific `/kanban`, `/curator`, `/suggestions`, skill bundles and marketplace management.
- Chat-driven `/update` and a draining `/restart`; Talos' verified updater remains a terminal command.
- CLI-only palette, skins, clipboard interaction and live session switching.

These are explicit gaps, not aliases that pretend to implement the same behaviour.
Full Hermes parity needs separate designs and tests for these stateful features.

Each background task uses a separate model instance with the selected provider/model
pinned at launch. It never shares the foreground process/cancel slot and does not
change the saved selection. A known provider cooldown is retained.

## Executed test surfaces

Telegram sends a separate, quiet progress update every 60 seconds during long work,
in addition to editing its activity card. Counts come from tool results; the current
activity uses fixed labels. No tool arguments or hidden reasoning enter the update.
The heartbeat also covers a blocked model call and stops at terminal delivery.
The model may explain a verified milestone or a changed approach before its next tool
request. Temporary work messages survive approval pauses, then disappear after result
delivery. Cleanup is scoped to that task, runs without holding the worker, and never
removes the final answer or execution log. A channel outage may leave some messages.
`tests/test_telegram_progress_updates.py` checks timing, privacy, terminal states,
delivery failures and an actual heartbeat through a local HTTP Telegram fixture.

- `tests/test_background_telegram_e2e.py`: actual local HTTP Telegram transport,
  production routing functions and ModelRouter, real worker/conductor/kernel, real file reads and
  SQLite read-back. Each of `/btw`, `/bg`, `/background` finishes while a main task is
  blocked; `/whoami` remains immediate, steering reaches the main task, and queued
  work runs once afterwards. The model and remote Telegram service are fixtures.
- `tests/test_chat_command_compatibility.py`: owner/chat/trust boundaries,
  background failure reporting, history isolation and no consent inheritance.
- `tests/test_task_approval_telegram_e2e.py`: one actual outgoing task-approval
  keyboard drives multiple real writes with one click; a new task asks again.
- `tests/test_task_approval_flow.py`: no clock expiry, cancellation, duplicate
  callbacks, target changes, hard denials, actual sandboxed shell effects and
  model-visible consent only after an operator decision.

No private bot token or live Telegram chat is used by these automated tests.

## Stop and bounded delegation

Commands accept whitespace including a newline after `/btw` or `/stop`. Stop closes
the active steering inbox immediately, propagates cancellation to synchronous
delegates, and rejects proposals returned after cancellation. `/queue` distinguishes
stopping from running until the current call has actually returned. A later task
gets a new run generation; an old completion cannot close or revive it.

Read-only delegations have a 180-second budget, independent cancellable model
instances and a captured caller identity for each invocation. Recursive delegation
is refused. A running non-cancellable tool may still need its own timeout to finish;
the UI does not claim that it has already ended.
