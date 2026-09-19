# cli_anything (kuratierte CLI-Anything-Harnesses)

Talos kann kuratierte [CLI-Anything](https://github.com/HKUDS/CLI-Anything)-Harnesses
ausführen — pip-installierte CLIs, die Software wie n8n, LibreOffice oder Obsidian
agent-native machen. Welche Harnesses existieren, ist eine **Betreiber-Entscheidung**
und steht deklarativ in `data/cli-anything.json` (gitignored, fail-closed wie
`entities.json` und `mcp-servers.json`). Das Modell nennt nur Harness-**Namen** und
ein **Subcommand** aus der Allowlist — niemals Pfade, Pakete oder Versionen.

Die Funktion ist standardmässig **aus**: ohne `TALOS_CLI_ANYTHING_REGISTRY` (Pfad zur
Registry-Datei) ist jeder Aufruf `DENY`, bevor ein Prozess startet. Es gibt kein
`cli-hub install` zur Laufzeit und kein Auto-Discovery — installiert und geprüft wird
vom Betreiber, nicht vom Agenten.

## Registry-Format

```json
{
  "version": 1,
  "harnesses": [
    {
      "name": "n8n",
      "package": "cli-anything-n8n",
      "version": "1.2.3",
      "sha256": "<64 hex Zeichen — Hash des pip-Archivs>",
      "command": "/absoluter/pfad/zum/harness",
      "subcommands": ["start", "export:workflow"],
      "network": false,
      "timeout": 60
    }
  ]
}
```

Regeln (Verstoß kostet den einzelnen Eintrag, fail-closed):

- `version: 1` ist Pflicht; eine fehlende, kaputte oder zu grosse Datei
  (32-KB-Deckel) ist eine **leere Registry** — und damit jeder Aufruf `DENY`.
- `name`: `[a-z0-9][a-z0-9-]{0,30}`.
- `package` + `version` + `sha256` sind **Pflicht** (gepinnt und gehasht; vor der
  Aufnahme OSV-scannen — die Regel steht in der CLAUDE.md). Ein Eintrag ohne
  Pinning ist kein kuratierter Harness, sondern ein ungeprüftes Programm.
- `command` muss ein **absoluter Pfad** sein — kein PATH-Raten im Sandkasten.
- `subcommands` ist eine **Allowlist** (1–32 Einträge, Token ohne Whitespace):
  was nicht gelistet ist, ist `DENY` — der Kernel stimmt nie über eine Operation
  ab, die der Betreiber nicht benannt hat.
- `network` (Vorgabe `false`) öffnet dem Lauf das Netz in der Sandbox;
  `timeout` ist auf 120 Sekunden gedeckelt.
- Ein `env`-Feld ist hart verboten und kostet den ganzen Eintrag — der Harness
  erbt exakt das minimierte Sandbox-Env (Positivliste, keine Credentials).

## Sicherheitsmodell

- **Der Kernel urteilt ausnahmslos.** Unbekannter Harness oder Subcommand
  ausserhalb der Allowlist → `DENY` (keine Freigabefrage — der Mensch stimmt nie
  über etwas ab, das er nie kuratiert hat). Bekannt und erlaubt → `NEEDS_HUMAN`.
  Erleichterung nur als stehende Regel auf exakt `(harness, subcommand)` —
  „immer n8n" gibt es nicht, und die Datenargumente stehen im Freigabe-Dialog.
- **Die Attended-Auto-Freigabe endet hier** (`autonomy.attended_routine`,
  dieselbe Namens-Ausnahme wie `remote_exec`): die Sandbox begrenzt den Prozess,
  nicht die Wirkung eines kuratierten Drittprogramms.
- **Der Lauf ist eingesperrt** wie `run_shell` (bubblewrap / sandbox-exec): Wurzel
  read-only, schreiben nur im Workspace, Env auf die Positivliste reduziert, Netz
  nur wenn der Eintrag es öffnet. Gibt es kein einsperrendes Backend, wird
  **verweigert** statt ungeschützt ausgeführt.
- **argv wird gebaut, nie interpoliert** (`shlex.join`): jedes Modell-Argument
  bleibt genau ein argv-Element — `"; rm -rf x"` ist ein Argument, kein zweites
  Kommando.
- Der Beleg im Event-Log nennt rc, gedeckeltes stdout/stderr, Harness und
  Subcommand.
