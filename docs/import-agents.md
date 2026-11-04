# Import from Hermes or OpenClaw

`talos import` first answers which portable parts of an existing agent home can be reviewed
for Talos. The dry run creates nothing and never changes the source. Its fingerprint can
then apply that exact unchanged plan into inactive staging.

```bash
talos import list
talos import hermes --source "$HOME/.hermes" --dry-run
talos import openclaw --source "$HOME/.openclaw" --dry-run
talos import hermes --source "$HOME/.hermes" --apply --confirm <fingerprint>
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
It grants no permission. Apply refuses any rejected/conflicting item, rereads every source
through the no-follow boundary, verifies its hash and byte length, writes files as 0600,
and publishes the complete 0700 staging directory atomically. The manifest contains only
relative paths, hashes and follow-up categories.

One skill can then be activated explicitly:

```bash
talos import activate-skill hermes <skill-name> --confirm <fingerprint>
```

Activation rereads and hashes the staged skill and passes it through Talos' existing
managed-skill parser, secret rejection, atomic writer and history contract. It never
activates every imported skill at once.

Inactive staging can be rolled back without deletion:

```bash
talos import rollback hermes --confirm <fingerprint>
```

Rollback moves the entire staged import to `data/import-backups/`; an already activated
skill remains active because silently changing a separately approved effect would be a
second, surprising action.

## What is always skipped

- API keys, tokens, OAuth state, `.env`, `auth.json` and auth profiles.
- Command allowlists, approvals, standing permissions and autonomy settings.
- Plugins, hooks, MCP commands and other executable configuration.
- Schedules, cron jobs, plans, sessions, logs and databases.

Credentials are configured again through Talos' own setup flow. Permissions are never
migrated: the manifest records that they require new review. Schedules, plugins and MCP
configuration remain disabled follow-up tasks rather than executable imports. Connector
drafts belong to [Talos Connections](connections.md), and still require two separate
operator allowlists before a worker can use them.
