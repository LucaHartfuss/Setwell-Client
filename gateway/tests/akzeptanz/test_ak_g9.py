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


#: Heizkoerperanlage Vaillant: Regelwerte des Dienstes, Comfort-Boost (hebt Kurve, Soll und Heizgrenze) und Ursprung
#: vor dem ersten Schreiben (die Heizgrenze 16 ist nicht der Regelwert 15: so zeigt sich, welcher Wert zurueckkommt)
REGELWERTE_VAILLANT = {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0, "min_flow": 22.0}
BOOST_VAILLANT = {"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0, "min_flow": 22.0}
URSPRUNG_VAILLANT = {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0, "min_flow": 20.0}
DREISSIG_TAGE = 30 * 86400


def _vaillant(welt) -> dict:
    return {name: wert for name, wert in welt.anlagenstand()["hebel"].items() if name in URSPRUNG_VAILLANT}


@pytest.mark.akzeptanz("AK-G9-01")
@pytest.mark.parametrize("ende", ["sign_off", "leere_konfiguration", "abo_ende"])
def test_ende_waehrend_des_boosts_stellt_zurueck(welt, ende):
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, konfiguration("vaillant_vrc720"))
    welt.antworten(REGELWERTE_VAILLANT)
    welt.raum(19.0)  # der Raum ist kalt, der Bewohner stellt 23 Grad: Comfort-Boost
    welt.runde(1)
    welt.bedienung(1, 23.0)
    welt.runde(10)
    assert _vaillant(welt) == BOOST_VAILLANT
    if ende == "sign_off":
        welt.befehl("c-1", "sign_off")
        welt.verbinden()
        welt.befehl("c-1", "sign_off")  # der Dienst stellt nach hello erneut zu
        welt.runde(10)
        ergebnis = welt.befehlsergebnis("c-1")
        assert ergebnis["ok"] is True and ergebnis["result"]["zurueckgesetzt"] is True
        assert {name: ergebnis["result"]["werte"][name] for name in URSPRUNG_VAILLANT} == URSPRUNG_VAILLANT
        assert welt.status()[-1] == "abgemeldet"
    elif ende == "leere_konfiguration":
        welt.abmelden(2)
        welt.verbinden()
        welt.runde(10)
        assert welt.ergebnis(2)["ok"] is True
        assert welt.letzter_status()["zustand"] == "wartet_auf_einrichtung"
    else:
        dokument = konfiguration("vaillant_vrc720")
        dokument["kreise"][0]["abo"] = "inaktiv"
        welt.konfiguration_senden(2, dokument)
        welt.verbinden()
        welt.runde(100)
        assert _vaillant(welt) == BOOST_VAILLANT  # die Karenzzeit: die Laufzeit regelt im Notbetrieb weiter
        assert welt.status()[-1] == "abo_inaktiv"
        welt.uhr.wand_versatz += DREISSIG_TAGE + 10
        welt.runde(100)
        welt.runde(300)
        assert welt.status()[-1] == "abo_beendet"
        assert any("Abo seit 30 Tagen inaktiv" in m["text"] and m["offen"] for m in welt.meldungen())
    assert _vaillant(welt) == URSPRUNG_VAILLANT  # der Boost ist beendet, alle Hebel stehen wie vor dem ersten Schreiben
    if ende != "leere_konfiguration":
        assert welt.letzter_status()["runtime_status"]["boost"] == "keiner"
    welt.runde(3600)  # nichts wird neu geschrieben, kein neuer Boost
    assert _vaillant(welt) == URSPRUNG_VAILLANT
    assert set(welt.z2m.gesetzt) == {23.0}  # das Thermostat bekam nur das geltende Soll
