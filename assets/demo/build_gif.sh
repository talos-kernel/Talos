#!/usr/bin/env bash
# Baut assets/talos-demo.gif neu: Demo-Baum in /tmp, Aufnahme via asciinema,
# Rendering via agg. Werkzeuge: asciinema + agg (statische Binaries, keine
# Installation noetig) — VHS 0.12 kann nicht auf Bildschirmmuster warten, und
# die Modell-Latenz variiert; ein Sleep-Skript tippt „yes" in eine Frage, die
# noch nicht auf dem Schirm ist. record_demo.py wartet deshalb auf Muster.
#
# Modellwahl per Umgebung (Quote wechselt): DEMO_PROVIDER / DEMO_MODEL,
# Vorgabe ollama/qwen3.8:latest (lokal, ohne Konto). Fuer claude-cli muss die
# CLI angemeldet sein; der Pfad kommt aus TALOS_CLAUDE_BIN oder `which claude`.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO="${DEMO_DIR:-/tmp/talos-demo}"
CAST="${DEMO}.cast"
OUT="$REPO/assets/talos-demo.gif"

# 1. Sauberer Demo-Baum aus den getrackten Dateien — derselbe Weg wie sync-public.
rm -rf "$DEMO"
mkdir -p "$DEMO"
git -C "$REPO" ls-files -z | (cd "$REPO" && xargs -0 tar cf -) | tar xf - -C "$DEMO"
ln -sfn "$REPO/.venv" "$DEMO/.venv"
mkdir -p "$DEMO/data" "$DEMO/workspace/old-logs"

# 2. Der harmlose Schreibanlass: ein uebrig gebliebener Ordner im Workspace.
printf '2026-08-14 nightly sync ok\n' > "$DEMO/workspace/old-logs/backup-08-14.log"
printf '2026-08-15 nightly sync ok\n' > "$DEMO/workspace/old-logs/backup-08-15.log"
printf 'scratch\n' > "$DEMO/workspace/old-logs/scratch.txt"

# 2b. Der Autonomie-Regler steht auf 3 („ask" — jeder Effekt fragt): der Demo-Lauf
#     soll die Freigabe-Frage zeigen, und auf Stufe 5 wuerde die Attended-Routine
#     das sandboxed rm still freigeben. Der Stand kommt ueber das Event-Log — genau
#     so ueberlebt der Regler Neustarts, das ist also Betreiberzustand, kein Hack.
PYTHONPATH="$DEMO" "$REPO/.venv/bin/python" - "$DEMO" <<'PY'
import sys
from pathlib import Path
from talos.eventlog import Event, EventLog

log = EventLog(Path(sys.argv[1]) / "data" / "eventlog.db")
log.append(Event("boot", "autonomy", "autonomy.set", {"level": 3}))
PY

# 3. Aufnahme (Pausen ueber 2s werden in den Metadaten gedeckelt — Modell-Latenz
#    soll das GIF nicht aufblahen).
rm -f "$CAST"
asciinema rec --overwrite --headless --window-size 100x28 --idle-time-limit 2.0 \
  -c "python3 '$REPO/assets/demo/record_demo.py' '$DEMO'" "$CAST"

# 4. Rendering. 100 Spalten bei Schriftgroesse 16 ergeben ~1000px Breite —
#    das README skaliert auf 900px, der Text bleibt scharf.
agg --font-size 16 --idle-time-limit 2.0 --theme monokai "$CAST" "$OUT"

ls -la "$OUT"
