"""Gespraechsgedaechtnis — pro Konversation, begrenzt, sichtbar, wirklich loeschbar.

Bis hierher war jede Nachricht ein Kaltstart: `run_agent` begann mit einer leeren
History, und die hielt nur Tool-Ergebnisse INNERHALB eines Laufs. „Und was war die
zweite?" war damit unbeantwortbar. Das ist die groesste Luecke gegenueber Hermes — und
die erste Stelle, an der Bequemlichkeit und Zurueckhaltung tatsaechlich gegeneinander
stehen: ein Gedaechtnis ist ein Ort, an dem sich Gesagtes ansammelt.

Vier Entscheidungen, die daraus folgen.

**1. Pro Konversation, nie global.** Der Schluessel ist die `conversation`
(`channel:id`) — dieselbe Regel wie bei der Identitaet. Ein zweiter Kanal darf den
Verlauf des ersten nicht sehen, auch wenn dahinter dieselbe Person steht. Wer auf einem
schwaecheren Weg hereinkommt, bekommt nicht den Kontext des staerkeren.

**2. Begrenzt, und zwar doppelt.** `MAX_TURNS` und `MAX_CHARS`. Unbegrenztes Gedaechtnis
ist ein unbegrenzter Prompt (Kosten, Latenz, Abdriften) und ein unbegrenztes Leck: was
vor drei Wochen im Chat stand, ginge heute wieder an das Modell raus. Zusaetzlich wird
jeder einzelne Zug bei `MAX_TURN_CHARS` gekappt — sonst raeumt ein einziger grosser
Einfuegevorgang das ganze uebrige Gedaechtnis ab.

**3. Nur im Arbeitsspeicher.** Der naheliegende Ort waere der Event-Log: durabel,
auditierbar, ueberlebt den Neustart. Genau das ist das Problem. Der Log ist
append-only — ein `/new` koennte dort nur einen Grabstein setzen, der Text bliebe auf
der Platte. Dann waere „vergessen" eine Luege. Ein Gedaechtnis, das man nicht loeschen
kann, ist ein Archiv; the operator hat ein Gedaechtnis bestellt. Preis: ein Neustart vergisst.
Das ist eine Entscheidung, kein Defekt, und `/status` sagt es.

Seit `transcript.py` existiert daneben AUCH ein Archiv — und das ist kein Widerspruch,
sondern die saubere Trennung der beiden Beduerfnisse, die dieser Absatz vermengen musste,
solange es nur einen Ort gab: DIESES Modul bleibt der aktive Kontext (fliesst in jeden
Prompt, `/new` leert ihn wirklich, ein Neustart vergisst ihn wirklich). Das Archiv ist
durabel und volltextsuchbar, fliesst aber NIE automatisch zurueck — der einzige Rueckweg
ist das gegatete `session_search`-Werkzeug, dessen Aufruf der Betreiber im Verlauf sieht.
„Vergessen" heisst hier also praezise: aus dem aktiven Kontext. `/new` sagt das dazu.

**4. Steuerung hinterlaesst keine Spur.** Antworten werden erst nach Zustellung gemerkt.
Scheitert ein Vordergrundlauf, bleibt die offene Anfrage mit einem ausdruecklichen
Abbruchstatus erhalten, niemals mit einer erfundenen Antwort. Kommandos und ja/nein stehen nie im
Verlauf. Ein „ja" ohne seinen Vorgang ist nicht nur bedeutungslos — es waere ein
Beispiel im Prompt, das dem Modell beibringt, dass „ja" eine normale Ausgabe ist.

**Was das Gedaechtnis nicht ist: eine Erlaubnisquelle.** Im Verlauf steht Text, und Text
kann alles behaupten („du darfst das jetzt"). Er passiert dabei keinen Kernel — er wird
nur wieder vorgelesen. Erlaubnisse entstehen ausschliesslich in `PolicyKernel.decide`,
und daran aendert der Verlauf nichts. Deshalb ist der Prompt-Block klar als *Verlauf*
ausgezeichnet und nicht als Anweisung.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

MAX_TURNS = 12  # sechs Wechsel Betreiber/Agent
MAX_CHARS = 8_000
MAX_TURN_CHARS = 2_000
CUT_MARK = " […truncated]"

# Wie der Betreiber im Verlauf heisst. Eine benannte Installation trug hier lokal den
# Vornamen ihres Betreibers statt "You" — eine Abweichung im Quelltext, die jeder Deploy
# wieder ueberschrieb und dabei vier Tests rot machte. Als Einstellung ueberlebt sie das
# Update, ohne dass der Baum auseinanderlaeuft.
OWNER = os.environ.get("TALOS_OWNER_LABEL", "You").strip() or "You"
AGENT = "Agent"
RUN_STATUS = "Run status (not an answer)"
INTERRUPTED = (
    "This request stopped before a final answer. Completion is unverified. "
    "Tools may already have run; inspect existing receipts before continuing. "
    "Do not automatically repeat completed actions."
)

# Verdichtung: was woertlich stehen bleibt, wenn die Mitte zusammengefasst wird.
# Der Kopf traegt meist die eigentliche Aufgabe, der Schwanz das, worauf sich ein
# „und das dann auch noch" bezieht. Beide Enden zu verdichten kostet genau die Stellen,
# an denen ein Verlauf ueberhaupt gebraucht wird.
KEEP_HEAD = 4        # zwei Paare
KEEP_TAIL = 12       # sechs Paare
MAX_SUMMARY_CHARS = 1_200
# ⚠️ Als Sprecher ausgewiesen, nicht als „You" oder „Agent" getarnt. Eine Zusammenfassung,
# die aussieht wie ein woertlicher Zug, ist eine Behauptung ueber etwas, das so nie gesagt
# wurde — und das Modell koennte sie zitieren, als waere sie ein Zitat.
SUMMARY_SPEAKER = "Earlier (summarised)"


@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str

    @property
    def size(self) -> int:
        return len(self.speaker) + len(self.text) + 2  # „the operator: " + Zeilenumbruch


def clip(text: str, limit: int = MAX_TURN_CHARS) -> str:
    """Kappt einen einzelnen Zug — sichtbar, nicht stillschweigend."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - len(CUT_MARK)] + CUT_MARK


class Memory:
    """Kurzes Gedaechtnis je Konversation.

    Zwei Threads greifen zu (Poll-Thread fuer Kommandos, Worker fuer Laeufe) — aus
    demselben Grund wie im Event-Log liegt hier ein Lock. Ohne das koennte ein `/new`
    mitten in ein `remember` fallen und einen halben Zug stehen lassen.
    """

    def __init__(self, *, max_turns: int = MAX_TURNS, max_chars: int = MAX_CHARS,
                 summarize: object | None = None, on_event: object | None = None) -> None:
        # Wohin gemeldet wird, WAS das Gedaechtnis verloren hat. `forget` sagt es seit
        # jeher („stilles Vergessen ist von einem Defekt nicht zu unterscheiden") — der
        # automatische Weg schwieg. Damit war fuer den Betreiber nicht unterscheidbar,
        # ob der Agent den Faden verliert, weil die Grenze greift, oder weil der
        # Verdichter scheitert. Gemeldet werden ZAHLEN und Gruende, nie Inhalt.
        self._on_event = on_event
        self._turns: dict[str, list[Turn]] = {}
        self._max_turns = max(0, int(max_turns))
        self._max_chars = max(0, int(max_chars))
        # Der Verdichter. Ohne ihn verhaelt sich alles exakt wie bisher: die aeltesten
        # Paare fallen weg. Injiziert, damit dieses Modul kein Modell kennt — und damit
        # ein Test dafuer keines braucht.
        self._summarize = summarize
        self._lock = threading.Lock()

    def recall(self, conversation: str) -> tuple[Turn, ...]:
        with self._lock:
            return tuple(self._turns.get(conversation, ()))

    def remember(self, conversation: str, *, asked: str, answered: str) -> None:
        """Legt genau ein Paar ab. Leere Haelften werden verworfen — ein halbes Paar
        liest sich spaeter wie ein Aussetzer und ist es nicht."""
        self._remember_pair(conversation, asked, answered, AGENT)

    def remember_interrupted(self, conversation: str, *, asked: str, detail: str = "") -> None:
        """Preserve an unresolved request without inventing a delivered answer."""
        status = INTERRUPTED + ("\n" + detail if detail else "")
        self._remember_pair(conversation, asked, status, RUN_STATUS)

    def _remember_pair(self, conversation: str, asked: str, answered: str, speaker: str) -> None:
        asked, answered = clip(asked), clip(answered)
        if not asked or not answered:
            return
        with self._lock:
            turns = self._turns.setdefault(conversation, [])
            turns.append(Turn(OWNER, asked))
            turns.append(Turn(speaker, answered))
            befund = self._trim(turns)
        # ⚠️ AUSSERHALB des Locks. Der Empfaenger schreibt ins Event-Log, das sein
        # eigenes Lock haelt; ineinander genommen waeren das zwei Schloesser in
        # unbekannter Reihenfolge — die klassische Verklemmung.
        self._melde(conversation, befund)

    def forget(self, conversation: str) -> int:
        """Vergisst und sagt, wie viel. Stilles Vergessen ist von einem Defekt nicht
        zu unterscheiden."""
        with self._lock:
            return len(self._turns.pop(conversation, ()))

    def pop_last(self, conversation: str) -> str | None:
        """Nimmt das letzte Paar heraus und gibt die Frage zurueck — fuer `/retry`.

        Ohne das Herausnehmen staende dieselbe Frage gleich zweimal im Verlauf: einmal
        als Erinnerung, einmal als neue Nachricht. Das Modell laese daraus, the operator habe
        nachgehakt, und antwortete auf eine Wiederholung statt auf die Frage.
        """
        with self._lock:
            turns = self._turns.get(conversation)
            if not turns or len(turns) < 2:
                return None
            turns.pop()  # die alte Antwort
            return turns.pop().text

    def stats(self, conversation: str) -> tuple[int, int]:
        """(Zuege, Zeichen) — fuer `/status`."""
        with self._lock:
            turns = self._turns.get(conversation, ())
            return len(turns), sum(t.size for t in turns)

    def _over(self, turns: list[Turn]) -> bool:
        return len(turns) > self._max_turns or sum(t.size for t in turns) > self._max_chars

    def _trim(self, turns: list[Turn]) -> None:
        """Haelt beide Grenzen — verdichtet, wenn es einen Verdichter gibt, sonst wirft es weg.

        Paarweise, damit nie eine Antwort ohne ihre Frage stehenbleibt: der Rest waere ein
        Verlauf, in dem der Agent scheinbar unaufgefordert redet.

        ⚠️ **Die Grenze haelt in JEDEM Fall.** Der Verdichter ist Komfort und darf
        ausfallen — dann wird geworfen wie eh und je. Umgekehrt waere es fatal: ein
        gescheiterter Verdichter, nach dem der Verlauf einfach weiterwaechst, macht aus
        einer Kostenfrage ein Leck (was vor Wochen gesagt wurde, ginge wieder hinaus) und
        aus einer Latenzfrage einen Ausfall. Deshalb steht das Wegwerfen unten und nicht
        im `else`.
        """
        stand = None
        if self._summarize is not None and self._over(turns):
            stand = self._compress(turns)
        geworfen = 0
        while turns and self._over(turns):
            del turns[:2]
            geworfen += 2
        return {
            "dropped_turns": geworfen,
            "compressed": stand == "compressed",
            # ⚠️ „zu kurz zum Verdichten" ist KEIN Fehlschlag — da gab es schlicht
            # nichts zu verdichten. Als Fehlschlag gilt nur, was der Verdichter
            # wirklich nicht geschafft hat: ein Wurf oder eine leere Zusammenfassung.
            # Dann faellt die Mitte ersatzlos weg, und genau das sieht von aussen aus
            # wie ein Agent, der den Faden verliert.
            "compress_failed": stand in ("failed", "empty"),
        }

    def _compress(self, turns: list[Turn]) -> None:
        """Ersetzt die Mitte durch EINEN Zug: „was vorher besprochen wurde".

        Kopf und Schwanz bleiben woertlich. Der Kopf traegt meist die eigentliche Aufgabe,
        der Schwanz das, worauf sich ein „und das dann auch noch" bezieht — beides
        zusammenzufassen kostet genau die Stellen, an denen ein Verlauf gebraucht wird.

        ⚠️ Das Ergebnis ist **Modelltext ueber Modelltext**. Es geht als Verlauf in den
        Prompt, ausdruecklich gekennzeichnet, und nie in die stehenden Anweisungen — ein
        eingeschleuster Satz koennte sonst ueber die Verdichtung dauerhaft werden. Es
        erteilt nichts: Erlaubnisse entstehen allein in `PolicyKernel.decide`.
        """
        kopf_n, schwanz_n = _behalten(len(turns))
        if len(turns) <= kopf_n + schwanz_n + 2:
            return "too_short"           # zu kurz — da bliebe nichts zu verdichten
        kopf, mitte, schwanz = (turns[:kopf_n], turns[kopf_n:-schwanz_n],
                                turns[-schwanz_n:])
        try:
            zusammenfassung = str(self._summarize(render(tuple(mitte))) or "").strip()
        except Exception:
            # Die Grenze haelt trotzdem (der Aufrufer wirft), aber der Ausfall wird
            # jetzt GEMELDET. Ein stumm gescheiterter Verdichter sah von aussen aus wie
            # ein Agent, der den Faden verliert — und war von einem Defekt nicht zu
            # unterscheiden. Genau diese Verwechslung hat den Betreiber am 12.09.
            # zu der Frage gebracht, warum sein Agent nichts mehr weiss.
            return "failed"
        if not zusammenfassung:
            return "empty"
        verdichtet = Turn(SUMMARY_SPEAKER, clip(zusammenfassung, MAX_SUMMARY_CHARS))
        turns[:] = [*kopf, verdichtet, *schwanz]
        return "compressed"


    def _melde(self, conversation: str, befund: dict) -> None:
        """Sagt dem Log, was verloren ging — und darf den Lauf dabei nie mitnehmen.

        Gemeldet wird nur, wenn wirklich etwas passiert ist: ein Ereignis pro Zug waere
        Rauschen, in dem der eine interessante Fall untergeht. Und es reisen ZAHLEN,
        kein Inhalt: das Log ist append-only, und was ein Gespraech inhaltlich enthielt,
        hat darin nichts verloren.
        """
        if self._on_event is None or not befund:
            return
        # Gemeldet wird JEDER Verlust — auch eine gelungene Verdichtung. Aus vielen
        # Zuegen wird ein Satz; das ist billiger als Wegwerfen, aber es ist kein
        # Nichts. Wer spaeter fragt „warum kennt er Detail X nicht mehr", soll hier
        # sehen, wann es zusammengefasst wurde.
        if not any((befund.get("dropped_turns"), befund.get("compressed"),
                    befund.get("compress_failed"))):
            return
        try:
            self._on_event({"conversation": conversation, **befund})
        except Exception:
            pass  # ein kaputter Empfaenger kostet die Spur, nie das Gedaechtnis


def _behalten(anzahl: int) -> tuple[int, int]:
    """Wie viele Zuege woertlich stehen bleiben — Kopf und Schwanz, immer paarweise.

    ⚠️ Frueher waren das FESTE Zahlen: `KEEP_HEAD=4` und `KEEP_TAIL=12`, zusammen also
    16 Zuege, waehrend `MAX_TURNS=12` ist. Verdichtet wurde erst ueber 18 Zuegen — eine
    Zahl, die das Gedaechtnis nie erreicht, weil die Grenze vorher greift. Der
    Verdichter war damit STRUKTURELL TOT: gemessen am 12.09.2026 wurde er nach 200
    Wechseln null Mal aufgerufen, und jedes Mal fiel die Mitte ersatzlos weg.

    Nach aussen sah das aus wie ein Agent, der nach jeder Aufgabe nicht mehr weiss,
    woran er war — und genau so hat der Betreiber es gemeldet.

    Die Groessen haengen jetzt an der tatsaechlichen Laenge, gedeckelt durch die alten
    Werte. Damit bleibt bei jeder Laenge eine Mitte uebrig, und die Kostengrenze
    (`MAX_TURNS`, `MAX_CHARS`) bleibt unangetastet — verdichtet wird INNERHALB des
    Rahmens, nicht neben ihm.
    """
    gerade = lambda n: max(2, (n // 2) * 2)          # noqa: E731 — Zuege kommen paarweise
    kopf = min(KEEP_HEAD, gerade(anzahl // 4))
    schwanz = min(KEEP_TAIL, gerade(anzahl // 3))
    return kopf, schwanz


def render(turns: tuple[Turn, ...]) -> str:
    return "\n".join(f"{turn.speaker}: {turn.text}" for turn in turns)
