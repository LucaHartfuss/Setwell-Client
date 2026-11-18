"""Akzeptanzszenario G-8 der Gateway-Laufzeit: das Thermostat bekommt nur das geltende, lokal begrenzte Soll. Der
Bewohner stellt 23 Grad, ein Wunsch von 30 Grad wird auf die Obergrenze (25) begrenzt; Boost, Durchsetzen, Notbetrieb
und Abmelden schreiben nie auf das Thermostat. Werte und Grenzen stehen hier woertlich."""
import pytest

#: Werte des Dienstes (Regelwerte) und der Comfort-Boost der Heizkoerperanlage (Viessmann)
REGELWERTE = {"curve": 0.8, "level": 1.0, "room_setpoint": 23.0}
COMFORT_BOOST = {"curve": 1.4, "level": 3.0, "room_setpoint": 25.0}
URSPRUNG = {"curve": 1.0, "level": 0.0, "room_setpoint": 20.0}  # Startwerte der Anlage vor dem ersten Schreiben
OBERGRENZE = 25.0
#: Pruefintervall der lokalen Kontrolle (Durchsetzen) und Ack-Timeout des Notbetriebs in Sekunden
PRUEFINTERVALL = 300
ACK_TIMEOUT = 30


class _Phasen:
    """Die set-Nachrichten an das Thermostat je Phase des Laufs."""

    def __init__(self, welt) -> None:
        self.welt, self.grenzen, self.namen = welt, [], []

    def beginnen(self, name: str) -> None:
        self.namen.append(name)
        self.grenzen.append(len(self.welt.z2m.gesetzt))

    def werte(self) -> dict[str, list[float]]:
        alle = self.welt.z2m.gesetzt
        grenzen = [*self.grenzen, len(alle)]
        return {name: alle[grenzen[nr]:grenzen[nr + 1]] for nr, name in enumerate(self.namen)}


def _hebel(welt) -> dict:
    return {name: wert for name, wert in welt.anlagenstand()["hebel"].items() if name in COMFORT_BOOST}


@pytest.mark.akzeptanz("AK-G8-02")
@pytest.mark.parametrize("abmelden", ["sign_off", "leere_konfiguration"])
def test_thermostat_bekommt_nur_das_geltende_begrenzte_soll(welt, abmelden):
    phasen = _Phasen(welt)
    phasen.beginnen("start")
    welt.starten()
    welt.verbinden()
    welt.einrichten()
    welt.antworten({"curve": 0.8, "level": 1.0, "room_setpoint": 21.0})
    # Bedienung und Boost: der Raum ist kalt, der Bewohner stellt 23 Grad
    phasen.beginnen("bedienung")
    welt.raum(19.0)
    welt.runde(1)
    welt.bedienung(1, 23.0)
    welt.runde(10)
    phasen.beginnen("boost")
    assert _hebel(welt) == COMFORT_BOOST
    welt.antworten(REGELWERTE)
    welt.raum(23.5)  # der Raum ist warm: der Boost endet, die Regelwerte gelten wieder
    welt.runde(10)
    assert _hebel(welt) == REGELWERTE
    # Durchsetzen: die App verstellt Kurve und Soll, nach dem Pruefintervall setzt die Laufzeit zurueck
    phasen.beginnen("durchsetzen")
    welt.runde(70)
    welt.app_verstellt("curve", 1.9)
    welt.app_verstellt("room_setpoint", 24.0)
    welt.runde(PRUEFINTERVALL)
    welt.runde(PRUEFINTERVALL)
    assert _hebel(welt) == REGELWERTE
    assert "manueller_eingriff" in [m["key"] for m in welt.meldungen()]
    # Begrenzung: der Bewohner dreht das Thermostat auf 30 Grad, die Laufzeit begrenzt den Wunsch und zeigt 25
    phasen.beginnen("begrenzung")
    welt.thermostat_dreht(30.0)
    welt.runde(5)
    welt.runde(10)  # der Wunsch wird erst nach kurzer Beruhigung gesendet
    assert [(w.get("roh"), w["werte"]) for w in welt.wuensche()] == [(30.0, {"raum_soll": OBERGRENZE})]
    assert "wunsch_begrenzt" in [m["key"] for m in welt.meldungen()]
    # Notbetrieb: der Server schweigt ueber zwei Ack-Timeouts, der Raum ist weit unter dem Wunsch (Notfall-Boost)
    phasen.beginnen("notbetrieb")
    welt.runde(ACK_TIMEOUT)
    welt.runde(ACK_TIMEOUT)
    welt.runde(1)
    assert welt.status()[-1] == "notbetrieb"
    welt.raum(20.0)
    welt.runde(10)
    assert _hebel(welt) == COMFORT_BOOST  # der Notfall-Boost schreibt die Hebel, nicht das Thermostat
    # Abmelden: die Laufzeit stellt die Anlage zurueck, das Thermostat bleibt unberuehrt
    phasen.beginnen("abmelden")
    if abmelden == "sign_off":
        welt.befehl("c-1", "sign_off")
        welt.verbinden()
        welt.befehl("c-1", "sign_off")  # der Dienst stellt nach hello erneut zu
        assert welt.befehlsergebnis("c-1")["ok"] is True
    else:
        welt.abmelden(2)
    welt.runde(10)
    assert _hebel(welt) == URSPRUNG  # zurueckgestellt auf die Werte vor dem ersten Schreiben
    # das Thermostat bekam nur das geltende Soll: die 23 der Bedienung und die 25 der Begrenzung
    assert phasen.werte() == {"start": [], "bedienung": [23.0], "boost": [], "durchsetzen": [],
                              "begrenzung": [OBERGRENZE], "notbetrieb": [], "abmelden": []}
    assert welt.z2m.set_nachrichten == 2  # keine weitere set-Nachricht, gleich mit welchem Inhalt
