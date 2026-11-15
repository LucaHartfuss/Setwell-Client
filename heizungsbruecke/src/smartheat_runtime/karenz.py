"""Karenz nach einem Softwarewechsel: Nach einem Update kann die neue Version die alte Konfiguration nicht lesen, bis der
Server eine passende liefert. Die Meldung "Heizungssteuerung gestoppt" waere in dieser Zeit falscher Alarm; sie wartet
darum KARENZ_NACH_UPDATE_S auf die neue Konfiguration. Zustand, Status und Ruhezustand bleiben wahr, nur die Meldung
(Push und offene Meldung) wird zurueckgehalten.

Die Software, die zuletzt eine gueltige Konfiguration geladen hat, steht in einer kleinen Datei neben dem Zustand des
Kreises. Weicht sie von der laufenden ab, laeuft die Karenz; ihr Beginn steht auf der monotonen Uhr (Port `clock`, nie
die Wanduhr) in derselben Datei, damit ein Neustart in der Karenz sie nicht verlaengert. Zusammen mit dem Beginn steht
dort die Software, fuer die die Karenz begann: je Softwarestand beginnt sie hoechstens einmal. Springt die monotone Uhr
zurueck (Neustart des Geraets), beginnt deshalb keine zweite Karenz derselben Software; eine Boot-Schleife unter der
Karenz haelt die Meldung nicht dauerhaft zurueck. Erst ein weiterer Softwarewechsel beginnt eine neue Karenz. Laesst
sich der Beginn nicht merken, gibt es keine Karenz (lieber sofort melden als nie). Dateien des alten Stands ohne den
Vermerk bleiben lesbar (dann beginnt die Karenz bei zurueckgesprungener Uhr einmal neu und wird vermerkt).

Nur Startfehler, die die Form der Konfiguration betreffen (unbekannter Hebelsatz oder unbekanntes Verteilsystem,
fehlende Bindungsparameter oder Pflichtzuordnungen), nutzen die Karenz; Rastung, lokale Sicherheitswerte, Positivliste
und ungueltige Werte melden sofort (`StartFailure.konfigurationsform`).

Grenzen: Springt die Uhr in einer laufenden Karenz zurueck (Neustart des Geraets), meldet die Meldung sofort, auch wenn
die Karenz noch nicht abgelaufen waere; ein weiterer Softwarewechsel ohne Uhrsprung erbt den Beginn der vorigen Karenz,
die Meldung geht dann nach deren Ende. Eine Neustartschleife, deren Start nach dem gemerkten Beginn liegt, haelt die
Meldung bis zu KARENZ_NACH_UPDATE_S zurueck, weil ohne Boot-Kennung kein Neustart zu erkennen ist."""
import logging
import math
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien

logger = logging.getLogger(__name__)

#: So lange wartet die Meldung "Heizungssteuerung gestoppt" nach einem Softwarewechsel auf die neue Konfiguration.
KARENZ_NACH_UPDATE_S = 900
#: Dateiname neben dem Zustand des Kreises.
DATEI = "software_stand.json"


class Karenz:
    """Die Karenz eines Kreises. `version` ist die laufende Software, `zustand_vorhanden` sagt, ob der Kreis schon
    einen Zustand hat (ohne gemerkte Software gilt ein vorhandener Zustand als Stand vor dem Update; eine frische
    Anlage hat keine Karenz)."""

    def __init__(self, pfad: Path, version: str, *, zustand_vorhanden: bool, clock: Callable[[], float]) -> None:
        self._pfad, self._version, self._clock = pfad, version, clock
        self._zustand_vorhanden = zustand_vorhanden

    def rest(self) -> float | None:
        """Die Sekunden, die die Meldung noch wartet; None: es gibt keine Karenz (kein Softwarewechsel, abgelaufen
        oder nicht zu merken), die Meldung geht sofort. Der erste Aufruf nach einem Wechsel merkt den Beginn."""
        gelesen = dateien.lesen_json(self._pfad)
        if not self._wechsel(gelesen):
            return None
        jetzt = self._clock()
        beginn = _zahl(gelesen.get("beginn") if isinstance(gelesen, dict) else None)
        if beginn is None or beginn > jetzt:
            if self._schon_begonnen(gelesen):
                return None  # die Karenz dieser Software lief schon; der Beginn ist nicht mehr zu deuten
            return KARENZ_NACH_UPDATE_S if self._beginn_merken(gelesen, jetzt) else None
        rest = beginn + KARENZ_NACH_UPDATE_S - jetzt
        return rest if rest > 0 else None

    def beenden(self) -> None:
        """Die laufende Software gilt als gemerkt, die Karenz ist vorbei. Wirft nie: scheitert das Schreiben, laeuft
        beim naechsten Start die Karenz erneut (hoechstens eine Verzoegerung der Meldung)."""
        soll = {"version": self._version}
        if dateien.lesen_json(self._pfad) == soll:
            return
        try:
            dateien.schreiben_json(self._pfad, soll, modus=dateien.PRIVAT)
        except OSError:
            logger.warning("Softwarestand nicht gespeichert, gilt bis zum naechsten Schreiben")

    def _wechsel(self, gelesen) -> bool:
        if isinstance(gelesen, dict) and "version" in gelesen:
            return gelesen["version"] != self._version
        return self._zustand_vorhanden

    def _schon_begonnen(self, gelesen) -> bool:
        return isinstance(gelesen, dict) and gelesen.get("karenz_fuer") == self._version

    def _beginn_merken(self, gelesen, jetzt: float) -> bool:
        alt = gelesen.get("version") if isinstance(gelesen, dict) else None
        try:
            dateien.schreiben_json(
                self._pfad, {"version": alt if isinstance(alt, str) else None, "beginn": jetzt,
                             "karenz_fuer": self._version}, modus=dateien.PRIVAT)
        except OSError:
            logger.warning("Beginn der Karenz nicht gespeichert, die Meldung geht sofort")
            return False
        return True


def _zahl(wert) -> float | None:
    """Der Wert als Zahl, None bei allem, was kein endlicher Zahlenwert ist."""
    if isinstance(wert, int | float) and not isinstance(wert, bool) and math.isfinite(wert):
        return wert
    return None
