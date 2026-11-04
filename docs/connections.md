# Talos Connections

Talos Connections is the public connector surface built into Talos. It is not a bundled
service, account, marketplace or credential store. It gives an operator one honest view of:

- MCP servers already present in the isolated Claude-worker registry;
- connector descriptions drafted locally but still disabled.

```bash
talos connections list
talos connections draft \
  --name calendar-reader \
  --command /usr/local/bin/calendar-mcp \
  --description "Read-only calendar connector" \
  --arg --read-only \
  --confirm DISABLED
```

Draft creation refuses relative executables, credentials, malformed names, duplicate names,
oversized arguments, the agent sandbox, shared profile directories and unsafe files. The
draft file is atomically published with mode 0600. Listing never prints commands or arguments.

## Activation boundary

A draft is data and cannot execute. The operator must separately:

1. install and verify the executable;
2. copy/review the descriptor into the operator-owned MCP registry;
3. name it in the agent allowlist;
4. name it independently in the worker allowlist.

Talos passes only the chosen registered name to the isolated worker. No environment table is
accepted in a registry entry, no connector runs inside the main agent process, and a connector
grants no permission to the model. Credentials are provisioned outside public source and never
appear in a connection draft.
