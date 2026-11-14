"""Akzeptanzszenario G-3 der Gateway-Laufzeit: die Rastung des Sicherheitssatzes. Die Grenzen stehen hier woertlich
(Regel 4), nie aus dem Produktcode; Erwartungen sind Literale aus docs/akzeptanz-szenarien.md."""
import pytest
from anlagen import konfiguration

#: lokales Maximum der Fussbodenheizung (viessmann_vicare) je Hebel, woertlich
FBH_MAXIMA = {"curve": 0.4, "level": 3.0, "room_setpoint": 24.0}
#: was ein kaputter oder boesartiger Server fordert: weit ueber jedem Maximum
FORDERUNG = {"curve": 1.5, "level": 10.0, "room_setpoint": 30.0}
ABGEWIESEN = {"ok": False, "grund": "hebelsatz_eingerastet", "feld": None}


def _stand_haelt_grenzen(welt) -> None:
    """Der Server fordert weit ueber den Grenzen: die Anlage faehrt hoechstens die der Fussbodenheizung."""
    welt.antworten(FORDERUNG)
    welt.runde(1)
    hebel = welt.anlagenstand()["hebel"]
    assert {name: hebel[name] for name in FBH_MAXIMA} == FBH_MAXIMA


@pytest.mark.akzeptanz("AK-G3-03")
def test_rastung_weist_heizkoerper_ab(welt):
    heizkoerper = konfiguration("viessmann_vicare", "Heizkoerper")
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, konfiguration("viessmann_vicare", "Fussbodenheizung"))  # als Fussbodenheizung eingerichtet
    welt.runde(1)
    welt.vom_server("down/config", {"schema": 2, "version": 2, **heizkoerper})  # der Server schickt Heizkoerper
    assert welt.ergebnis(2) == ABGEWIESEN
    _stand_haelt_grenzen(welt)
    welt.abmelden(3)  # Abmelden loest die Rastung nicht
    assert welt.ergebnis(3)["ok"]
    welt.verbinden()
    welt.vom_server("down/config", {"schema": 2, "version": 4, **heizkoerper})  # und wieder Heizkoerper
    assert welt.ergebnis(4) == ABGEWIESEN
