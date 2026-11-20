"""Zaehler der Aenderungen des Kunden am Thermostat im Leerstand, hostfrei.

Ein gleitendes Fenster von `FENSTER` (24 h): die dritte Aenderung darin (`BEENDEN_AB`) beendet den Leerstand, die erste
und zweite gelten als Uebersteuerung fuer `UEBERSTEUERUNG_DAUER` (4 h). Das Fenster liegt in einer Datei (ein Neustart
verliert nichts); eine fehlende oder kaputte Datei ist ein leeres Fenster. Die Zeit liefert die Wanduhr des
Soll-Speichers (`now_iso`), nie die monotone Uhr: sie gilt auch ueber einen Neustart hinweg. Gezaehlt wird nur, was der
Aufrufer als Aenderung des Kunden erkannt hat; eigene Schreibvorgaenge der Regelung ruft er gar nicht erst ein."""
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from smartheat_core import dateien, safety

logger = logging.getLogger(__name__)

FENSTER = safety.LEERSTAND_AENDERUNGEN_FENSTER
UEBERSTEUERUNG_DAUER = safety.UEBERSTEUERUNG_LEERSTAND_HOECHSTDAUER
#: Die wievielte Aenderung im Fenster den Leerstand beendet.
BEENDEN_AB = safety.LEERSTAND_AENDERUNGEN_BEENDEN


def _zeit(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        zeit = datetime.fromisoformat(text)
    except ValueError:
        return None
    return zeit.astimezone(UTC) if zeit.utcoffset() is not None else None


class LeerstandZaehler:
    def __init__(self, path: Path, now_iso: Callable[[], str]) -> None:
        self._path = path
        self._now_iso = now_iso
        self._im_speicher: list[datetime] | None = None  # der Stand; die Datei wird nur einmal gelesen

    def _jetzt(self) -> datetime:
        return datetime.fromisoformat(self._now_iso()).astimezone(UTC)

    def _lesen(self, jetzt: datetime) -> list[datetime]:
        """Die Aenderungen im Fenster, alte, zukuenftige und unbrauchbare Eintraege fallen heraus."""
        if self._im_speicher is None:
            roh = dateien.lesen_json(self._path)
            self._im_speicher = [zeit for eintrag in roh if (zeit := _zeit(eintrag)) is not None] \
                if isinstance(roh, list) else []
        return sorted(zeit for zeit in self._im_speicher if zeit <= jetzt and jetzt - zeit < FENSTER)

    def anzahl(self) -> int:
        return len(self._lesen(self._jetzt()))

    def eintragen(self) -> int:
        """Eine Aenderung des Kunden jetzt; Rueckgabe: ihre Anzahl im Fenster samt dieser."""
        jetzt = self._jetzt()
        zeiten = [*self._lesen(jetzt), jetzt]
        self._im_speicher = zeiten
        self._speichern([zeit.strftime("%Y-%m-%dT%H:%M:%S.%f%z") for zeit in zeiten])
        return len(zeiten)

    def leeren(self) -> None:
        self._im_speicher = []
        self._speichern([])

    def _speichern(self, inhalt: list[str]) -> None:
        """Der Stand im Speicher gilt zuerst und immer: ein Speicherfehler (Datentraeger voll oder nur lesbar) darf weder
        eine Aenderung des Kunden noch das Ende des Leerstands verhindern. Nach einem Neustart fehlen dann die Aenderungen
        seit dem letzten Speichern; das Fenster beginnt dort von vorn (der Leerstand endet spaeter, nie frueher). Die
        Warnung nennt keine Werte."""
        try:
            dateien.schreiben_json(self._path, inhalt, modus=dateien.LESBAR)
        except OSError as exc:
            logger.warning("Zaehler der Aenderungen nicht gespeichert (%s)", type(exc).__name__)
