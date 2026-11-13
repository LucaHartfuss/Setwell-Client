"""Akzeptanzszenarien G-1 der Gateway-Laufzeit: Grenzen, die kein Dienst und kein Bediener am Geraet verschieben kann.
Erwartungen sind Literale aus docs/akzeptanz-szenarien.md, nicht aus dem Produkt gelesen."""
import pytest
from anlagen import THERMOSTAT

HEBEL = {"curve": 1.0, "level": 0.0, "room_setpoint": 20.0}


def _thermostat_meldet(welt, **werte) -> None:
    """Das Thermostat meldet einen Zustand; die Entprellung des Wunsches laeuft ab."""
    welt.z2m.melden(THERMOSTAT, local_temperature=20.0, **werte)
    welt.runde(3)
    welt.runde(10)


@pytest.mark.akzeptanz("AK-G1-02")
def test_thermostat_wunsch_wird_begrenzt_und_zurueckgeschrieben(welt):
    welt.starten()
    welt.verbinden()
    welt.einrichten()
    welt.antworten(HEBEL)  # Antwort auf den Erststart-Tick
    assert welt.z2m.gesetzt == []

    # Das Thermostat meldet nachts 14 Grad: der Wunsch geht begrenzt auf 15 (roh 14) an den Dienst, das Thermostat
    # bekommt 15 zurueck, und die Anlage faehrt den Soll 15, auch wenn der Dienst dem Wunsch unbegrenzt folgt.
    _thermostat_meldet(welt, occupied_heating_setpoint=14.0)
    assert [(w["werte"], w["roh"], w["herkunft"]) for w in welt.wuensche()] == [({"raum_soll": 15.0}, 14.0, "thermostat")]
    assert welt.z2m.gesetzt == [15.0]
    welt.antworten({**HEBEL, "room_setpoint": 14.0})
    welt.runde(1)
    assert welt.anlagenstand()["hebel"]["room_setpoint"] == 15.0

    # Frostschutz-Stellung des Thermostats (7 Grad, nach dem Echo-Fenster von 60 s): kein Wunsch, nichts zurueckgeschrieben
    welt.runde(61)
    _thermostat_meldet(welt, occupied_heating_setpoint=7.0)
    assert len(welt.wuensche()) == 1
    assert welt.z2m.gesetzt == [15.0]

    # Thermostat aus: auch ein gedrehter Wert (18 Grad) ist kein Wunsch und wird nicht an die Quelle geschrieben
    welt.runde(61)
    _thermostat_meldet(welt, occupied_heating_setpoint=18.0, system_mode="off")
    assert len(welt.wuensche()) == 1
    assert welt.z2m.gesetzt == [15.0]
