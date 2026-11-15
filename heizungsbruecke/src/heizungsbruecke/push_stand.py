"""Stand der aufgegebenen Push-Meldungen aus dem Ereignis smartheat_push_aufgegeben der Integration.
Das Ereignis kommt vom HA-Event-Bus und ist nicht vertrauenswuerdig: gelesen wird es streng in
geraet_trigger.push_aufgegeben_lesen, hier landen nur Stufe (rot|orange) und Kreis (oder None, anlagenweit). Eine
Faelschung kann hoechstens je (Stufe, Kreis) eine orange Meldung oeffnen, nie mehr. Zeit kommt als monotone Sekunden
vom Aufrufer, nie von der Wanduhr. Der Stand liegt nur im Speicher: nach einem Neustart ist der Stand leer, der Host schliesst
die Meldungen des frueheren Prozesses beim ersten Takt (geraet_host._push_start_schliessen), die Integration gibt neue
Eintraege neu auf."""
import threading

#: Ohne neues Ereignis gleicher Stufe und gleichen Kreises seit so vielen Sekunden schliesst die Meldung.
FRIST_SECONDS = 24 * 3600


class PushStand:
    """Letztes Ereignis je (Stufe, Kreis). Der WS-Thread ruft aufgegeben(), der Geraete-Thread fragt mit offen() ab."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._zeiten: dict[tuple[str, int | None], float] = {}

    def aufgegeben(self, stufe: str, kreis: int | None, jetzt: float) -> None:
        """Ein Ereignis (auch ein weiteres gleicher Stufe und gleichen Kreises) beginnt die Frist neu."""
        with self._lock:
            self._zeiten[(stufe, kreis)] = jetzt

    def offen(self, jetzt: float) -> frozenset[tuple[str, int | None]]:
        """Stufe und Kreis mit einem Ereignis in den letzten FRIST_SECONDS; Abgelaufene fallen heraus."""
        with self._lock:
            for schluessel in [s for s, zeit in self._zeiten.items() if jetzt - zeit >= FRIST_SECONDS]:
                del self._zeiten[schluessel]
            return frozenset(self._zeiten)
