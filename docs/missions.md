# Missions, projects and outcome receipts

Missions turn common tasks into repeatable briefs. They do not grant permissions:
the model proposes each action and the existing kernel still rules on it. A mission's
checks are instructions to the reasoner, not a new independent verification engine.

In an authorized chat, select an existing folder and inspect a mission before running it:

```text
/project add demo /path/to/project Keep unrelated work unchanged
/missions
/mission info folder-dossier
/mission run folder-dossier source=. output=talos-reports/dossier.md
/project context Verify the remaining source references next
/continue
```

Relative mission paths must remain inside the selected project. The project is context,
not an allowlist: selecting a protected folder does not make its contents accessible.
Projects and selection are scoped to the initiating principal and conversation. Removing
a project removes its context entry, not the underlying folder.

## Bundled tasks

| Mission | Result | Boundary |
|---|---|---|
| `repo-guardian` | Small change, executed tests and review handover | No push or merge is requested; normal execution gates still apply |
| `folder-dossier` | Source-linked findings and open questions | Source files are to remain unchanged |
| `mail-brief` | Prioritized brief and local reply drafts | Mailbox tools are read-only; no mail is sent by them |
| `server-watcher` | Measured health exceptions and recommendations | Requires an operator-configured remote alias and normal remote-exec approval |

Mailbox and remote access require existing operator configuration; missions do not
create credentials, add allowed hosts or enable tools automatically. A mission prompt
is not a hard sandbox for all other tools: security enforcement stays with the kernel.

## Receipts and local-model check

```sh
talos proof --format json
talos proof --run RUN_ID --out new-receipt.md
talos local-check --help
```

Proof summaries read the event log and exclude prompts, tool arguments, result bodies
and model answers. Artifact paths can still be sensitive: inspect a receipt before
sharing it. A successful recorded write is historical evidence, not proof that a file
has remained unchanged. Existing export destinations are refused, not overwritten.

The local-model check sends no user content. It tests loopback reachability and refuses
redirects; it cannot prove that a model server or any other process never uses the network.
