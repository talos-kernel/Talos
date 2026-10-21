"""Eine Korrektur erreicht den LAUFENDEN Auftrag, statt hinter ihm zu warten.

Bis hierher galt: was waehrend eines Laufs hereinkommt, wird eingereiht. Das ist bei
einem zweiten, unabhaengigen Auftrag richtig — und falsch bei dem Satz, der am
haeufigsten kommt: „nein, das andere Verzeichnis". Die Korrektur wurde geschrieben,
*weil* der Lauf gerade laeuft, und sie hinter ihm anzustellen heisst, ihn erst falsch
zuende laufen zu lassen und dann noch einmal von vorn.

Was dabei verworfen wird
------------------------
Eine Revisionsschranke verwirft eine noch nicht zugelassene Modell-Proposal, bevor sie
geparst, freigegeben oder ausgefuehrt wird. Der Provider-Aufruf selbst wird hier bewusst
nicht abgebrochen: ein synchrones Netzwerk-``close`` kann Steuerung und Stop blockieren,
und ein spaeter globaler Abbruch kann schon den naechsten Lauf treffen. Laeuft bereits
ein zugelassenes Werkzeug, erreicht die Korrektur den naechsten Denkschritt.

Kein zusaetzliches Recht. Die Korrektur ist ein Zug DESSELBEN Sprechers, der den Lauf
gestartet hat. Jeder Werkzeugaufruf danach geht durch denselben Kernel wie vorher; eine
abgelehnte Handlung wird durch eine nachgeschobene Nachricht nicht erlaubt. Wer das
umgehen wollte, muesste den Kernel umgehen, nicht dieses Postfach.

Kein Weg fuer Dritte. `offer()` nimmt nur an, was von **derselben Kennung** in
**derselben Unterhaltung** kommt wie der laufende Auftrag. Beides, nicht eines: ein
zweiter erlaubter Mensch darf den Lauf eines anderen nicht lenken, und dieselbe Person
in einem anderen Chat redet ueber etwas anderes.

Und ausdruecklich kein Weg an einer offenen Rueckfrage vorbei. Steht eine Freigabe an,
ist die naechste Nachricht deren Antwort — sie darf nicht als Kurskorrektur in einen
Lauf rutschen, waehrend der Betreiber glaubt, „ja" zu einer Handlung gesagt zu haben.
Diese Sperre setzt der Conductor, der als Einziger weiss, ob eine Rueckfrage offen ist;
hier steht die zweite, unabhaengige Schranke (Kennung und Unterhaltung), damit ein
Fehler dort nicht schon alles ist.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

# Mehr als das staut sich nicht an: wer viermal nachschiebt, ohne dass ein Schritt
# vergeht, meint einen neuen Auftrag und keine Korrektur. Der Rest wird abgelehnt und
# der Sprecher erfaehrt es — stillschweigendes Verwerfen waere hier das Schlimmste.
MAX_PENDING = 4
MAX_LENGTH = 4000


@dataclass(frozen=True)
class Correction:
    """Ein Zug, der einen bereits laufenden Auftrag erreicht hat."""

    text: str
    principal: str
    conversation: str

    def as_turn(self) -> str:
        """Der Wortlaut, wie er in die Historie geht.

        ⚠️ Ausdruecklich als *Nachricht des Betreibers* markiert und nicht als Anweisung
        von aussen: die Historie ist der Ort, an dem fremder Text am teuersten waere.
        Der Rahmen sagt, woher sie kommt und was sie nicht ist — dass sie nichts
        erlaubt, entscheidet ohnehin der Kernel und nicht dieser Satz.
        """
        return (
            "[correction from the operator, while this run was in progress — same person, "
            f"same conversation, no additional rights]\n{self.text}"
        )


class Redirect:
    """Ein Postfach fuer den einen laufenden Auftrag. Von zwei Threads benutzt.

    Der Poll-Thread legt ab (`offer`), die Agentenschleife nimmt (`take`). Beides unter
    demselben Schloss — der Worker hat genau einen Platz, aber `offer` kommt aus einem
    anderen Thread als `take`, und eine Liste ohne Schloss verliert dabei Eintraege.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run: tuple[str, str] | None = None      # (principal, conversation)
        self._pending: list[Correction] = []
        self._generation = 0
        self._revision = 0

    def open(self, principal: str, conversation: str) -> int:
        """Ein Lauf erklaert sich fuer lenkbar. Verwirft Reste eines frueheren Laufs."""
        with self._lock:
            self._run = (principal, conversation)
            self._pending = []
            self._generation += 1
            self._revision = 0
            return self._generation

    def close(self, generation: int | None = None) -> None:
        """Kein Lauf mehr. Danach wird nichts mehr angenommen."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._run = None
            self._pending = []

    def admit(self, revision: int) -> bool:
        """Atomically linearize a proposal against accepted corrections.

        ``True`` means no correction won the race before this boundary.  A
        correction accepted afterwards belongs to the next step; without this
        explicit boundary, a check followed by parse/execute has a check/use gap.
        """
        with self._lock:
            return self._run is not None and self._revision == revision

    def active(self, generation: int) -> bool:
        with self._lock:
            return self._run is not None and self._generation == generation

    def is_open(self) -> bool:
        with self._lock:
            return self._run is not None

    def offer(self, principal: str, conversation: str, text: str) -> bool:
        """True = die Nachricht lenkt den laufenden Auftrag; False = sie tut es nicht.

        False heisst NICHT „verworfen": der Aufrufer reiht dann ein wie bisher. Diese
        Funktion entscheidet nur, ob umgelenkt wird — sie wirft nie etwas weg.
        """
        text = text.strip()
        if not text or len(text) > MAX_LENGTH:
            return False
        with self._lock:
            if self._run is None:
                return False
            # ⚠️ BEIDES muss stimmen. Nur die Kennung zu pruefen liesse dieselbe Person
            # aus einem anderen Chat in einen Lauf hineinreden, ueber den dort nie die
            # Rede war; nur die Unterhaltung zu pruefen liesse einen zweiten erlaubten
            # Menschen den Lauf eines anderen lenken.
            if self._run != (principal, conversation):
                return False
            if len(self._pending) >= MAX_PENDING:
                return False
            self._pending.append(Correction(text, principal, conversation))
            self._revision += 1
        return True

    def finish_proposal(self, revision: int) -> bool:
        """Atomically close a terminal proposal, or reject it as superseded.

        A message accepted before this boundary forces another turn.  Once this
        succeeds, ``offer`` returns ``False`` so a later message is queued as a new
        task instead of being accepted and then discarded by ``close``.
        """
        with self._lock:
            if self._run is None or self._revision != revision:
                return False
            self._run = None
            self._pending = []
            return True

    def take(self) -> tuple[Correction, ...]:
        """Holt alles Wartende und leert das Postfach. Leer, wenn nichts da ist."""
        _, pending = self.take_with_revision()
        return pending

    def take_with_revision(self) -> tuple[int, tuple[Correction, ...]]:
        """Atomically drain corrections and expose their admission revision."""
        with self._lock:
            wartend = tuple(self._pending)
            self._pending = []
            return self._revision, wartend
