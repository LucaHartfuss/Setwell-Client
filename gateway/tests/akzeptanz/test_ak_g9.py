"""Akzeptanzszenario G-9 der Gateway-Laufzeit: die lokale Mindestliste der Rueckstellung je Hebelsatz. Ein Dienst kann
nicht erreichen, dass das Gateway beim Abmelden weniger zurueckstellt. Listen und Werte stehen hier woertlich."""
import pytest
from anlagen import konfiguration

#: Startwerte der simulierten Anlage (woertlich): was das Abmelden zurueckstellen muss
URSPRUNG = {"curve": 1.0, "level": 0.0, "room_setpoint": 20.0, "heat_limit": 16.0, "min_flow": 20.0}
#: lokale Mindestliste der Rueckstellung je Hebelsatz
MINDESTLISTE = {"vaillant_vrc720": ("curve", "room_setpoint", "heat_limit", "min_flow"),
                "weishaupt_wwp": ("curve", "room_setpoint", "heat_limit"),
                "weishaupt_wwp_basis": ("room_setpoint",), "viessmann_vicare": ("curve", "level", "room_setpoint")}
#: Werte des Dienstes, die von den Startwerten verschieden sind und innerhalb der lokalen Grenzen liegen
GELERNT = {"vaillant_vrc720": {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0, "min_flow": 22.0},
           "weishaupt_wwp": {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0},
           "weishaupt_wwp_basis": {"room_setpoint": 21.5},
           "viessmann_vicare": {"curve": 0.8, "level": -2.0, "room_setpoint": 21.5}}
KUERZER = [("vaillant_vrc720", []), ("weishaupt_wwp", []), ("weishaupt_wwp", ["curve", "room_setpoint"]),
           ("weishaupt_wwp_basis", []), ("viessmann_vicare", []), ("viessmann_vicare", ["curve", "level"])]


@pytest.mark.akzeptanz("AK-G9-06")
@pytest.mark.parametrize("satz,zurueck", KUERZER, ids=[f"{s}-{'-'.join(z) or 'leer'}" for s, z in KUERZER])
def test_kuerzere_rueckstellliste_wird_abgewiesen(welt, satz, zurueck):
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, konfiguration(satz))
    welt.antworten(GELERNT[satz])
    welt.runde(1)
    hebel = welt.anlagenstand()["hebel"]
    assert all(hebel[name] != URSPRUNG[name] for name in MINDESTLISTE[satz])  # die Regelung hat geschrieben
    neu = konfiguration(satz)
    neu["bindungsparameter"]["zurueck"] = zurueck  # der Dienst will weniger zurueckstellen als die Mindestliste
    neu["bindungsparameter"]["zurueck_optional"] = []
    welt.konfiguration_senden(2, neu)
    assert welt.ergebnis(2) == {"ok": False, "grund": "ungueltig", "feld": "zurueck"}  # abgewiesen, mit Feld
    welt.abmelden(3)  # das vorige Dokument gilt noch: es stellt mindestens die Mindestliste zurueck
    hebel = welt.anlagenstand()["hebel"]
    assert {name: hebel[name] for name in MINDESTLISTE[satz]} == {name: URSPRUNG[name] for name in MINDESTLISTE[satz]}
