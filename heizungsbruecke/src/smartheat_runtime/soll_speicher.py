"""Soll-Speicher: <daten>/runtime/room_target.json = {value, source, ts}, geschrieben
nur von der Laufzeit (im Worker-Thread), fuer Gateway und HA-Add-on gleich. Der zuletzt gestellte Wert gilt. Bedienung
vom Server (portal): 15-25 °C in 0,5-K-Schritten (Bedienbereich, keine Sicherheitsgrenze; der
Geraetekern prueft vorher, hier erneut). Soll-Quelle (thermostat; HA: Soll-Entity) und lokale Eingabe (lokal; HA:
Ereignis smartheat_wunsch): nur Plausibilitaet 5-35 °C, kein Clamp (den Bedienbereich setzt der Wunsch des
Geraetekerns). Nach eigenem Schreiben an die Soll-Quelle gelten deren Meldungen ECHO_WINDOW_SECONDS lang als Echo;
meldet sie das geltende Soll, aendert sich nichts (auch nicht die Quelle); ebenso, wenn der Server das geltende Soll
bestaetigt. Ein offener Schreibbefehl (PENDING_SECONDS) verwirft verspaetete Meldungen des alten Werts, bis
die Quelle den geschriebenen Wert meldet; jeder andere Wert gilt als Eingabe an der Quelle. Ein ganzzahliges Thermostat
zeigt einen halben Grad gerundet zurueck: der gerundete Schreibwert gilt als Bestaetigung, auch wiederholt, und ist keine
Eingabe.

Die Uebersteuerung der kuehlen Phase (Temperatur und Ende) liegt lokal in einer eigenen Datei neben dem Wunsch
(<name>.uebersteuerung.json) und uebersteht einen Neustart; der Wunsch bleibt dabei unveraendert. Abgelaufen oder
beschaedigt gilt sie als nicht gesetzt.

Die zuletzt angenommene oder geschriebene Anzeige des Thermostats liegt in <name>.angezeigt.json und uebersteht
ebenfalls einen Neustart. Gegen sie wird eine Meldung verglichen: ein Wert, den das Thermostat schon zeigt (auch nach dem
Ablauf der Uebersteuerung oder nach einem Neustart), ist nie eine Eingabe des Kunden.

Die Laufzeit importiert smartheat_device nicht (tests/test_runtime_purity.py); der Bedienbereich steht deshalb hier
und der Geraetekern (documents.py) nutzt diese Funktion; die Gleichheit der Grenzen mit dem Vertrag
(smartheat_device.wire.RAUM_SOLL_*) sichert test_soll_speicher.py."""
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from smartheat_core import dateien
from smartheat_runtime.leerstand_zaehler import LeerstandZaehler
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible

logger = logging.getLogger(__name__)

SOURCE_PORTAL = "portal"
SOURCE_THERMOSTAT = "thermostat"
SOURCE_LOKAL = "lokal"
_SOURCES = (SOURCE_PORTAL, SOURCE_THERMOSTAT, SOURCE_LOKAL)
BEDIENBEREICH_MIN, BEDIENBEREICH_MAX, BEDIENBEREICH_STEP = 15.0, 25.0, 0.5
ECHO_WINDOW_SECONDS = 60
PENDING_SECONDS = 1800
#: Ein Schreibbefehl, den das Thermostat nach PENDING_SECONDS nicht bestaetigt hat (es meldet weiter den alten Wert, etwa
#: ein schlafendes Geraet), wird bis zu so oft erneut geschrieben, ein Versuch je Ablauf. Danach gilt der gemeldete Wert
#: als Eingabe des Kunden wie bisher: ein Thermostat, das dauerhaft nicht folgt, wird nicht endlos beschrieben.
MAX_NACHSCHREIBEN = 3


def im_bedienbereich(value) -> bool:
    """Bedienbereich (keine Sicherheitsgrenze): 15-25 °C in 0,5-K-Schritten, wie Server und Geraetekern."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not BEDIENBEREICH_MIN <= value <= BEDIENBEREICH_MAX:  # zuerst: exakt fuer grosse int, NaN faellt hier durch
        return False
    steps = value / BEDIENBEREICH_STEP
    return abs(steps - round(steps)) < 1e-9


#: Startwert ohne gueltige Bedienung, beide Hosts
DEFAULT_RAUM_SOLL = 20.0


#: Der eine Kreis, den die Hosts heute regeln (Kreisnummern beginnen bei 1); die Bedienung traegt je Kreis einen Eintrag.
KREIS = 1


def raum_soll_von(bedienung, kreis: int = KREIS):
    """Der Raum-Soll des Kreises aus einem Bedienungsdokument (Eintrag mit dieser Nummer in `kreise`); None, wenn das
    Dokument fehlt, keine Liste fuehrt oder den Kreis nicht nennt. Der Wert ist ungeprueft (siehe im_bedienbereich)."""
    eintraege = bedienung.get("kreise") if isinstance(bedienung, Mapping) else None
    if not isinstance(eintraege, list):
        return None
    for eintrag in eintraege:
        if isinstance(eintrag, Mapping) and eintrag.get("kreis") == kreis:
            return eintrag.get("raum_soll")
    return None


def bedienung_start(bedienung, kreis: int = KREIS) -> float | None:
    """Startwert aus der Bedienung (5c 1.3): raum_soll des Kreises `kreis`, wenn im Bedienbereich, sonst None. Nur mit
    einem solchen Wert gleicht ein Host beim Start an."""
    value = raum_soll_von(bedienung, kreis)
    return float(value) if isinstance(value, int | float) and im_bedienbereich(value) else None


def is_valid_portal_target(value) -> bool:
    return im_bedienbereich(value)


def gespeicherter_wunsch(path: Path) -> float | None:
    """Der gemerkte Wunsch aus room_target.json, nur lesend (legt nie eine Datei an); None, wenn die Datei fehlt oder
    Wert bzw. Quelle unbrauchbar sind."""
    stored = dateien.lesen_json(path)
    if isinstance(stored, dict) and is_plausible(stored.get("value"), ROOM_TEMP_RANGE) and stored.get("source") in _SOURCES:
        return float(stored["value"])
    return None


class TargetStore:
    def __init__(self, path: Path, start_value: float, clock: Callable[[], float], now_iso: Callable[[], str],
                 on_change: Callable[[], None] | None = None) -> None:
        self._path = path
        self._clock = clock
        self._now_iso = now_iso
        self._on_change = on_change
        self._echo_until: float | None = None
        # (geschrieben, veraltete Werte, Frist) - nur im Speicher
        self._pending: tuple[float, frozenset[float], float] | None = None
        #: Was das Thermostat vor dem offenen Schreibbefehl zeigte; nur dessen Meldung nach der Frist heisst "kam nicht an"
        self._vorher: frozenset[float] = frozenset()
        self._abweichend = False  # seit dem offenen Schreibbefehl kam eine andere Meldung als `_vorher` (nur im Speicher)
        self._versuche = 0  # Wiederholungen des offenen Schreibbefehls (nur im Speicher)
        self._nachschreiben: tuple[float, float] | None = None  # (geschrieben, gemeldet): erneut schreiben (nur im Speicher)
        self._before: float | None = None  # Wert vor der letzten Aenderung (= was das Thermostat noch zeigt)
        self._gerundet: float | None = None  # ganzzahlige Anzeige des halben Grads, die das Thermostat zeigt (nur im Speicher)
        self._ueb_path = path.with_name(path.stem + ".uebersteuerung.json")
        self._ueb = self._ueb_lesen(dateien.lesen_json(self._ueb_path))
        self._angezeigt_path = path.with_name(path.stem + ".angezeigt.json")
        self._angezeigt = self._angezeigt_lesen(dateien.lesen_json(self._angezeigt_path))
        #: Die Aenderungen des Kunden im Leerstand (24-h-Fenster), neben dem Wunsch gespeichert.
        self.leerstand_zaehler = LeerstandZaehler(path.with_name(path.stem + ".leerstand.json"), now_iso)
        stored = dateien.lesen_json(path)
        if isinstance(stored, dict) and is_plausible(stored.get("value"), ROOM_TEMP_RANGE) and stored.get("source") in _SOURCES:
            self.value, self.source = float(stored["value"]), stored["source"]
        else:
            self.value, self.source = float(start_value), SOURCE_PORTAL
            self._write()

    def apply_portal(self, value, *, uebersteuerung_behalten: bool = False) -> bool:
        """Die Bedienung des Servers. Sie beendet die lokale Uebersteuerung, ausser der Aufrufer behaelt sie (Leerstand:
        dort merkt das Portal nur den Wunsch fuer danach). Die Uebersteuerung wird dann gar nicht angefasst: es gibt kein
        Fenster, in dem sie geloescht, aber noch nicht wieder gesetzt ist."""
        if not is_valid_portal_target(value):
            logger.warning("Wunschtemperatur %r aus dem Portal verworfen (erlaubt 15-25 °C in 0,5-K-Schritten)", value)
            return False
        if float(value) == self.value:  # Bestaetigung des geltenden Solls: die Quelle bleibt
            return False
        if not uebersteuerung_behalten:
            self.uebersteuerung_aufheben()  # eine neue Bedienung des Servers gilt vor der lokalen Uebersteuerung
        return self._set(float(value), SOURCE_PORTAL)

    def note_own_write(self, veraltet: float | None = None, geschrieben: float | None = None, *,
                       wiederholung: bool = False) -> None:
        """veraltet: was das Thermostat zuletzt gemeldet hat, falls bekannt (Wiedereinschalten: der Wert vor dem
        Ausschalten, nicht unbedingt der vorige Wert des Speichers); gilt zusaetzlich als alter Wert. geschrieben: der
        geschriebene Wert, wenn er nicht der Wunsch ist (Uebersteuerung); dann zeigte das Thermostat zuvor den Wunsch.
        wiederholung: dieselbe, unbestaetigte Schreibung noch einmal (zaehlt gegen MAX_NACHSCHREIBEN)."""
        if not wiederholung:
            self._versuche = 0
        now = self._clock()
        self._echo_until = now + ECHO_WINDOW_SECONDS
        self._gerundet = None
        written = self.value if geschrieben is None else geschrieben
        if self._pending is not None and now < self._pending[2]:  # zweiter Befehl vor der Bestaetigung des ersten
            stale = self._pending[1] | {self._pending[0]}
            vorher = self._vorher | {self._pending[0]}
        else:
            self._abweichend = False
            if written != self.value:  # Uebersteuerung, Leerstand: der Wunsch ist nur ein alter Wert des Befehls
                stale = frozenset({self.value})
                vorher = frozenset() if self._angezeigt is None else frozenset({self._angezeigt})
            else:
                stale = vorher = frozenset({self._before if self._before is not None else self.value})
        if veraltet is not None:
            stale |= {veraltet}
            vorher |= {veraltet}
        self._pending = (written, stale - {written}, now + PENDING_SECONDS)
        self._vorher = vorher - {written}
        self.anzeige_merken(written)

    def anzeige(self) -> float:
        """Was das Thermostat zeigen soll: die laufende Uebersteuerung, sonst der Wunsch."""
        ueb = self.uebersteuerung()
        return self.value if ueb is None else ueb["temp"]

    @property
    def angezeigt_zuletzt(self) -> float | None:
        """Die zuletzt angenommene oder geschriebene Anzeige des Thermostats (None: noch keine bekannt)."""
        return self._angezeigt

    def nachschreiben_nehmen(self) -> tuple[float, float] | None:
        """(geschrieben, gemeldet), wenn die letzte Pruefung einen unbestaetigten Schreibbefehl abgelaufen sah, dessen alten
        Wert das Thermostat weiter meldet: der Aufrufer schreibt `geschrieben` erneut. Einmal je Ablauf, dann leer."""
        offen, self._nachschreiben = self._nachschreiben, None
        return offen

    def anzeige_verwerfen(self) -> None:
        """Die gemerkte Anzeige gilt nicht mehr (das Thermostat ist aus: es zeigt, was der Kunde dort einstellt)."""
        if self._angezeigt is None:
            return
        self._angezeigt = None
        dateien.schreiben_json(self._angezeigt_path, {"temp": None}, modus=dateien.LESBAR)

    def anzeige_merken(self, wert: float) -> None:
        """Das Thermostat zeigt `wert` (angenommene Eingabe des Kunden): eine Wiederholung ist keine Eingabe mehr."""
        if wert == self._angezeigt:
            return
        self._angezeigt = wert
        dateien.schreiben_json(self._angezeigt_path, {"temp": wert}, modus=dateien.LESBAR)

    def echo_rest(self) -> float:
        """Sekunden, die das Echo-Fenster nach dem eigenen Schreiben noch laeuft (0 = keins)."""
        if self._echo_until is None:
            return 0.0
        return max(self._echo_until - self._clock(), 0.0)

    def schreibung_rest(self) -> float:
        """Sekunden, bis eine eigene Schreibung als erledigt gilt: Echo-Fenster und unbestaetigter Schreibbefehl (das
        Thermostat hat ihn nicht gemeldet, bis zur Frist). 0: nichts offen."""
        rest = self.echo_rest()
        if self._pending is not None:
            rest = max(rest, self._pending[2] - self._clock())
        return max(rest, 0.0)

    def apply_thermostat(self, value) -> bool:
        neu = self.thermostat_pruefen(value)
        if neu is None:
            return False
        self.anzeige_merken(neu)
        return self._set(neu, SOURCE_THERMOSTAT)

    def thermostat_pruefen(self, value, *, angezeigt: float | None = None) -> float | None:
        """Der Wert, wenn die Meldung des Thermostats eine Eingabe des Kunden ist (kein Echo, keine Bestaetigung, kein
        veralteter Wert, nicht der angezeigte Wert, plausibel), sonst None. Fuehrt die Buchfuehrung zum Echo und zum
        offenen Schreibbefehl, aendert aber den Wunsch nicht: ihn setzt der Aufrufer (`apply_thermostat`) oder nicht
        (Uebersteuerung). `angezeigt`: was das Thermostat zeigt, wenn es nicht der Wunsch ist (Uebersteuerung)."""
        if self._echo_until is not None and self._clock() < self._echo_until:
            if self._pending is not None and is_plausible(value, ROOM_TEMP_RANGE) and self._bestaetigt(float(value)):
                self._pending = None  # Bestaetigung im Echo-Fenster schliesst den offenen Befehl trotzdem
                self._versuche = 0
            return None
        if not is_plausible(value, ROOM_TEMP_RANGE):
            logger.warning("Wunschtemperatur %r vom Thermostat unplausibel, verworfen", value)
            return None
        value = float(value)
        if self._pending is not None:
            written, stale, until = self._pending
            if self._clock() >= until:
                self._pending = None
                if value != written:
                    if value in self._vorher and not self._abweichend and self._versuche < MAX_NACHSCHREIBEN:
                        self._versuche += 1  # der Befehl kam nicht an: der alte Wert ist keine Eingabe des Kunden
                        self._nachschreiben = (written, value)
                        logger.info("Thermostat meldet nach Ablauf der Frist noch den alten Sollwert %.1f, der Befehl "
                                    "wird wiederholt (Versuch %d)", value, self._versuche)
                        return None
                    logger.warning("Thermostat hat das Soll %.1f nicht bestaetigt, sein Wert %.1f gilt", written, value)
            elif value == written:
                self._pending = None
                self._versuche = 0
                self.anzeige_merken(value)
                return None
            elif value in stale:
                self._abweichend = self._abweichend or value not in self._vorher
                logger.info("Thermostat meldet noch einen alten Sollwert %.1f (Schreibbefehl offen), verworfen", value)
                return None
            else:
                self._pending = None
        bezug = self.value if angezeigt is None else angezeigt
        if value in (bezug, self._angezeigt, self._gerundet):
            return None  # angezeigtes Soll (Echo, Wiederholung), auch gerundet angezeigt
        return value

    def _bestaetigt(self, value: float, written: float | None = None) -> bool:
        """Der Schreibwert (`written`, sonst der des offenen Befehls) kommt zurueck, exakt oder von einem ganzzahligen
        Thermostat gerundet (halbes Grad, hoechstens 0,5 K daneben); die gerundete Anzeige wird gemerkt, damit ihre
        Wiederholung keine Eingabe ist."""
        if written is None:
            if self._pending is None:
                return False
            written = self._pending[0]
        if value == written:
            return True
        if written != round(written) and value == round(value) and abs(value - written) <= 0.5:
            self._gerundet = value
            return True
        return False

    def apply_lokal(self, value) -> bool:
        """Lokale Eingabe (HA: Wunschtemperatur-Entity der Integration): gilt sofort, ohne Echo-Pruefung
        (sie stammt nicht von der Soll-Quelle); nur plausibel 5-35 °C."""
        if not is_plausible(value, ROOM_TEMP_RANGE):
            logger.warning("Lokale Wunschtemperatur %r unplausibel, verworfen", value)
            return False
        return self._set(float(value), SOURCE_LOKAL)

    def begrenzen(self, value: float) -> bool:
        """Begrenzter Wunsch: der begrenzte Wert gilt sofort, die Quelle bleibt die des Wunsches
        (thermostat bzw. lokal; der Wunsch hat sie gerade gesetzt)."""
        return self._set(float(value), self.source)

    def wiederherstellen(self, value: float, source: str) -> bool:
        """Verworfener Thermostat-Wunsch (Thermostat aus): zurueck auf den Stand vor dem Wunsch."""
        return self._set(float(value), source)

    def _set(self, value: float, source: str) -> bool:
        if value == self.value and source == self.source:
            return False
        self._before = self.value
        self.value, self.source = value, source
        self._gerundet = None
        self._write()
        if self._on_change is not None:
            self._on_change()
        return True

    def uebersteuerung(self) -> dict | None:
        """Die lokale Uebersteuerung {temp, bis} oder None (nicht gesetzt, abgelaufen). `bis`: UTC-Zeitpunkt als Text."""
        if self._ueb is not None and self._abgelaufen(self._ueb["bis"]):
            self.uebersteuerung_aufheben()
        return dict(self._ueb) if self._ueb is not None else None

    def uebersteuerung_setzen(self, temp: float, bis: str) -> None:
        """Uebersteuerung der kuehlen Phase bis `bis`; der Aufrufer hat Temperatur und Ende geprueft. Der Wunsch
        bleibt unveraendert."""
        self._ueb = {"temp": float(temp), "bis": bis}
        dateien.schreiben_json(self._ueb_path, self._ueb, modus=dateien.LESBAR)

    def uebersteuerung_aufheben(self) -> None:
        if self._ueb is None:
            return
        self._ueb = None
        dateien.schreiben_json(self._ueb_path, None, modus=dateien.LESBAR)

    def jetzt(self) -> datetime:
        """Die Wanduhr des Speichers (mit Zeitzone); fuer Fristen, die einen Neustart ueberstehen muessen."""
        return datetime.fromisoformat(self._now_iso())

    def _abgelaufen(self, bis: str) -> bool:
        try:
            return datetime.fromisoformat(bis) <= datetime.fromisoformat(self._now_iso())
        except (ValueError, TypeError):  # TypeError: ein Ende ohne Zeitzone ist nicht vergleichbar
            return True

    def _ueb_lesen(self, stored) -> dict | None:
        """Gespeicherte Uebersteuerung, wenn sie Form hat und noch nicht abgelaufen ist; sonst None (kein Absturz)."""
        if not isinstance(stored, dict) or not is_plausible(stored.get("temp"), ROOM_TEMP_RANGE):
            return None
        bis = stored.get("bis")
        if not isinstance(bis, str):
            return None
        try:
            if datetime.fromisoformat(bis).utcoffset() is None:
                return None
        except ValueError:
            return None
        return {"temp": float(stored["temp"]), "bis": bis}

    @staticmethod
    def _angezeigt_lesen(stored) -> float | None:
        """Gemerkte Anzeige, wenn plausibel; sonst None (kein Absturz bei beschaedigter Datei)."""
        if isinstance(stored, dict) and is_plausible(stored.get("temp"), ROOM_TEMP_RANGE):
            return float(stored["temp"])
        return None

    def _write(self) -> None:
        dateien.schreiben_json(self._path, {"value": self.value, "source": self.source, "ts": self._now_iso()},
                               modus=dateien.LESBAR)
