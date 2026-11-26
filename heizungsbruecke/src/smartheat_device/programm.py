"""Fähigkeit `programm` (Wochenplan, Away als Wunsch, Leerstand, Uebersteuerung): beide Hosts melden sie nur, wenn die
Planrechnung auf dem Geraet ueberhaupt laufen und wirken kann. Fehlt eine Bedingung, bleibt das Feld weg (= false), und
das Geraet regelt mit dem Wunsch weiter (sichere Richtung). Hostfrei: die Hosts liefern nur ihre Auskuenfte."""
from collections.abc import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Stellvertreter fuer die Zonendatenbank: die Zone der Konfiguration steht erst im Dokument, die Datenbank ist
#: entweder im Prozess ladbar oder nicht.
ZONE_PROBE = "Europe/Berlin"


def zonendaten_vorhanden() -> bool:
    """Die Zonen-Datenbank ist im Prozess ladbar (tzdata bzw. die des Systems)."""
    try:
        ZoneInfo(ZONE_PROBE)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def angeboten(*, uhr_port: bool, soll_quellen: Sequence[bool]) -> bool:
    """True nur mit Zonendaten, Uhr-Port und schreibbarer Soll-Quelle. soll_quellen: je Kreis der Konfiguration, ob sein
    Soll-Ziel schreibbar ist; jeder Kreis braucht eines (strengere Lesart). Leer = noch kein Kreis bekannt (etwa bei
    der Registrierung): der Host kann dann eine Soll-Quelle haben."""
    return uhr_port and zonendaten_vorhanden() and all(soll_quellen)
