"""Bedienwunsch: ein lokal gestellter Sollwert wirkt sofort lokal (der Host wendet den begrenzten Wert
an, Boost wie heute). Das Geraet begrenzt ihn auf den Bedienbereich 15-25 °C und rundet kaufmaennisch auf 0,5 K (keine
Sicherheitsgrenze), schickt ihn als up/wish mit der Version der Bedienung, auf der es steht (basis_version),
und puffert ohne Verbindung je Kreis nur den juengsten (<daten>/device/wunsch-<kreis>.json). Erledigt ist er mit dem
PUBACK des Brokers; eine Trennung vorher schickt ihn nach dem naechsten hello erneut. Danach entscheidet der Server und schickt
die Bedienung zurueck, falls der Wunsch verliert."""
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from smartheat_core import dateien, meldungen
from smartheat_core.meldungen import Meldung
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_device import nachrichten, wire

WUNSCH_MUSTER = "wunsch-*.json"
#: Nach einem PUBACK mit Fehlercode (typisch Drosselung, 0x97) geht der Wunsch fruehestens nach dieser Zeit erneut
#: hinaus (monotone Uhr); ein neuer Wunsch und ein Verbindungsverlust heben die Sperre auf.
WUNSCH_NACH_ABLEHNUNG_SECONDS = 60.0
BEGRENZT_KEY = "wunsch_begrenzt"
SOLL_QUELLE_AUS_KEY = "soll_quelle_aus"


def begrenzen(wert) -> tuple[float, float | None]:
    """(begrenzter Wert, roh) - roh ist der gestellte Wert, wenn Bereich oder Rundung ihn veraendert haben."""
    if isinstance(wert, bool) or not isinstance(wert, int | float):
        raise ValueError("Wunsch ist keine Zahl")
    if not ist_endliche_zahl(wert):
        raise ValueError("Wunsch ist keine endliche Zahl")
    # erst in den Bereich, dann runden: riesige endliche Werte laufen so nicht in einen Ueberlauf (die Grenzen liegen
    # auf dem Raster, das Ergebnis ist dasselbe wie Runden und dann Begrenzen)
    im_bereich = min(wire.RAUM_SOLL_MAX, max(wire.RAUM_SOLL_MIN, wert))
    begrenzt = float(math.floor(im_bereich / wire.RAUM_SOLL_STEP + 0.5) * wire.RAUM_SOLL_STEP)
    return begrenzt, (None if begrenzt == wert else float(wert))


def wunsch_datei(kreis: int) -> str:
    return f"wunsch-{kreis}.json"


def begrenzt_meldung(roh: float, begrenzt: float, herkunft: str | None) -> Meldung:
    if herkunft is None:
        return meldungen.neu("meldung.wunsch_begrenzt", roh=roh, begrenzt=begrenzt)
    return meldungen.neu("meldung.wunsch_begrenzt", roh=roh, begrenzt=begrenzt, herkunft=herkunft)


def soll_quelle_aus_meldung(wirksam: float | None) -> Meldung:
    """Ohne wirksames Soll (unbekannt) nennt die Meldung keinen Wert; sie bleibt trotzdem offen."""
    if wirksam is None:
        return meldungen.neu("meldung.soll_quelle_aus_unbekannt")
    return meldungen.neu("meldung.soll_quelle_aus", wirksam=wirksam)


@dataclass(frozen=True)
class Wunsch:
    wish_id: str
    kreis: int
    basis_version: int
    ts: str
    uhr_synchron: bool
    raum_soll: float | None
    roh: float | None = None
    herkunft: str | None = None
    durch: str | None = None
    #: Die Programmwerte (Faehigkeit `programm`): Schluessel aus WISH_WERTE_PROGRAMM, Wert Objekt oder None (beenden).
    programm: dict | None = None

    def werte(self) -> dict:
        """`werte` der Nachricht: je Situation nur, was sich aendert (raum_soll und/oder Programmwerte)."""
        werte = {} if self.raum_soll is None else {"raum_soll": self.raum_soll}
        werte.update(self.programm or {})
        pflicht = wire.WISH_WERTE if self.raum_soll is not None else ()
        nachrichten.pruefen("wunsch.werte", werte, pflicht, wire.WISH_WERTE_PROGRAMM)
        return werte

    def payload(self) -> dict:
        werte = self.werte()
        body = {"schema": wire.SCHEMA, "wish_id": self.wish_id, "kreis": self.kreis,
                "basis_version": self.basis_version, "ts": self.ts,
                "uhr_synchron": self.uhr_synchron, "werte": werte}
        for key in wire.WISH_OPTIONAL:
            value = getattr(self, key)
            if value is not None:
                body[key] = value
        return nachrichten.pruefen("wunsch", body, wire.WISH_FIELDS, wire.WISH_OPTIONAL)


def pruefen_herkunft(herkunft: str | None, durch: str | None) -> None:
    """ValueError, wenn herkunft oder durch kein Vertragswert ist (None = nicht angegeben). Device.wunsch prueft so
    schon im aufrufenden Thread, wie bei einer nicht endlichen Zahl."""
    if herkunft is not None and herkunft not in wire.HERKUNFT:
        raise ValueError("herkunft unbekannt")
    if durch is not None and durch not in wire.DURCH:
        raise ValueError("durch unbekannt")


def pruefen_kreis(kreis) -> None:
    """ValueError, wenn kreis keine Kreisnummer ist (ganze Zahl von wire.KREIS_MIN bis KREIS_MAX, kein bool)."""
    if isinstance(kreis, bool) or not isinstance(kreis, int) or not wire.KREIS_MIN <= kreis <= wire.KREIS_MAX:
        raise ValueError("kreis ist keine Kreisnummer")


def _objekt(name: str, wert, felder: tuple[str, ...]) -> None:
    if not isinstance(wert, dict) or set(wert) != set(felder):
        raise ValueError(f"{name} hat nicht genau die Felder des Vertrags")


def pruefen_programm(programm: dict | None, herkunft: str | None) -> None:
    """ValueError, wenn die Programmwerte nicht zum Vertrag passen: nur die Schluessel aus WISH_WERTE_PROGRAMM; `leerstand`
    nur als null (das setzt der Server); `abwesend` als Objekt nur aus HA, vom Thermostat nur als null."""
    for key, wert in (programm or {}).items():
        if key not in wire.WISH_WERTE_PROGRAMM:
            raise ValueError("programm nennt einen Schluessel ausserhalb des Vertrags")
        if wert is None:
            continue
        if key == "leerstand":
            raise ValueError("leerstand setzt nur der Server")
        if key == "abwesend":
            if herkunft != "ha":
                raise ValueError("abwesend als Objekt kommt nur aus HA")
            _objekt("abwesend", wert, wire.ABWESEND_FIELDS)
        else:
            _objekt("uebersteuerung", wert, wire.UEBERSTEUERUNG_FIELDS)


def neu(raum_soll: float | None, roh: float | None, *, kreis: int, basis_version: int, uhr_synchron: bool,
        herkunft: str | None, durch: str | None, wall: float, programm: dict | None = None) -> Wunsch:
    """Ein Wunsch braucht mindestens einen Wert: raum_soll und/oder Programmwerte."""
    pruefen_herkunft(herkunft, durch)
    pruefen_kreis(kreis)
    pruefen_programm(programm, herkunft)
    if raum_soll is None and not programm:
        raise ValueError("Wunsch ohne Wert")
    ts = datetime.fromtimestamp(wall, UTC).isoformat(timespec="seconds")
    return Wunsch(uuid.uuid4().hex, kreis, basis_version, ts, uhr_synchron, raum_soll, roh, herkunft, durch,
                  dict(programm) if programm else None)


def _laden(path: Path) -> Wunsch | None:
    raw = dateien.lesen_json(path)
    if not isinstance(raw, dict):
        return None
    try:
        wunsch = Wunsch(**raw)
    except TypeError:
        return None
    gueltig = (isinstance(wunsch.wish_id, str) and isinstance(wunsch.kreis, int) and not isinstance(wunsch.kreis, bool)
               and wire.KREIS_MIN <= wunsch.kreis <= wire.KREIS_MAX and isinstance(wunsch.basis_version, int)
               and isinstance(wunsch.ts, str) and isinstance(wunsch.uhr_synchron, bool)
               and (isinstance(wunsch.raum_soll, int | float) and not isinstance(wunsch.raum_soll, bool)
                    or wunsch.raum_soll is None and bool(wunsch.programm))
               and (wunsch.programm is None or isinstance(wunsch.programm, dict)))
    if not gueltig:
        return None
    try:
        pruefen_programm(wunsch.programm, wunsch.herkunft)
        wunsch.werte()
    except ValueError:
        return None
    return wunsch


class _Platz:
    """Der Puffer eines Kreises: der Wunsch, die mid seines laufenden Sendens und die Sperre nach einer Ablehnung."""

    def __init__(self, wunsch: Wunsch | None = None) -> None:
        self.wunsch = wunsch
        self.mid: int | None = None
        self.gesperrt_bis: float | None = None  # monotone Uhr, nach einem abgelehnten PUBACK


class WunschPuffer:
    """Je Kreis der juengste ungesendete Wunsch (persistiert in wunsch-<kreis>.json) und die mid seines laufenden
    Sendens. Ein Wunsch eines Kreises ersetzt nie den eines anderen; mids sind je Link eindeutig."""

    def __init__(self, device_dir: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self._dir = device_dir
        self._clock = clock
        self._plaetze: dict[int, _Platz] = {}
        for path in sorted(device_dir.glob(WUNSCH_MUSTER)):
            wunsch = _laden(path)
            if wunsch is not None and path.name == wunsch_datei(wunsch.kreis):  # Datei und Inhalt nennen denselben Kreis
                self._plaetze[wunsch.kreis] = _Platz(wunsch)

    def wunsch(self, kreis: int) -> Wunsch | None:
        platz = self._plaetze.get(kreis)
        return platz.wunsch if platz is not None else None

    def kreise(self) -> list[int]:
        """Die Kreise mit einem gepufferten Wunsch, aufsteigend."""
        return sorted(kreis for kreis, platz in self._plaetze.items() if platz.wunsch is not None)

    def offen(self, kreis: int) -> bool:
        """Zu senden: der Kreis hat einen Wunsch, er ist nicht unterwegs und nicht wegen einer Ablehnung gesperrt."""
        platz = self._plaetze.get(kreis)
        return (platz is not None and platz.wunsch is not None and platz.mid is None
                and (platz.gesperrt_bis is None or self._clock() >= platz.gesperrt_bis))

    def setzen(self, wunsch: Wunsch) -> None:
        platz = self._plaetze.setdefault(wunsch.kreis, _Platz())
        platz.wunsch, platz.mid, platz.gesperrt_bis = wunsch, None, None
        dateien.schreiben_json(self._dir / wunsch_datei(wunsch.kreis), asdict(wunsch), modus=dateien.PRIVAT)

    def gesendet(self, kreis: int, mid: int | None) -> None:
        platz = self._plaetze.get(kreis)
        if platz is not None:
            platz.mid = mid

    def bestaetigt(self, mid: int) -> bool:
        for kreis, platz in self._plaetze.items():
            if platz.wunsch is not None and platz.mid is not None and platz.mid == mid:
                platz.wunsch, platz.mid = None, None
                (self._dir / wunsch_datei(kreis)).unlink(missing_ok=True)
                return True
        return False

    def abgelehnt(self, mid: int) -> None:
        """Der Broker lehnte die Nachricht ab (PUBACK mit Fehlercode): der Wunsch ist nicht unterwegs, er geht nach
        WUNSCH_NACH_ABLEHNUNG_SECONDS erneut hinaus (Drosselung nicht durch Sekundentakt verschaerfen)."""
        for platz in self._plaetze.values():
            if platz.mid is not None and platz.mid == mid:
                platz.mid = None
                platz.gesperrt_bis = self._clock() + WUNSCH_NACH_ABLEHNUNG_SECONDS

    def verbindung_verloren(self) -> None:
        for platz in self._plaetze.values():
            platz.mid, platz.gesperrt_bis = None, None
