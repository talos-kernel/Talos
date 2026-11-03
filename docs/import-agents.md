# Import preview from Hermes or OpenClaw

`talos import` answers one question: which portable parts of an existing agent home could
be reviewed for Talos? The current command is deliberately a dry run. It creates no target
directory, backup, report or temporary file and never changes the source.

```bash
talos import list
talos import hermes --source "$HOME/.hermes" --dry-run
talos import openclaw --source "$HOME/.openclaw" --dry-run
```

The source path is explicit. Talos accepts an absolute path after shell expansion and
refuses a source or target boundary containing a symlink. Input files must be bounded,
regular, singly linked UTF-8 files. The preview contains relative paths, byte counts and
SHA-256 digests, never file bodies.

## What the preview considers

- `SOUL.md`, `AGENTS.md`, `USER.md` and `MEMORY.md`, where that source stores them.
- Agent Skills with a valid `SKILL.md`, including one category-directory layer.
- Existing staging destinations as conflicts. The preview never overwrites them.

Every candidate points below `data/import-staging/<source>/`. `staged` means “eligible for
operator review”, not active or trusted. Imported text can contain hostile instructions.
It grants no permission, and a skill must still pass Talos' managed-skill approval path
before it can become active.

## What is always skipped

- API keys, tokens, OAuth state, `.env`, `auth.json` and auth profiles.
- Command allowlists, approvals, standing permissions and autonomy settings.
- Plugins, hooks, MCP commands and other executable configuration.
- Schedules, cron jobs, plans, sessions, logs and databases.

Credentials are configured again through Talos' own setup flow. Permissions are never
migrated. External connector grants must be paired separately for Talos and are never
copied from Hermes or OpenClaw. A future apply command needs its own backup, atomic
publication, exact-item confirmation, read-back and rollback contract; this preview does
not imply that contract already exists.
