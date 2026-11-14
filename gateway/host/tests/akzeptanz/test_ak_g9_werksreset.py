"""AK-G9-03: Werksreset am Knopf. Beobachtet werden Dateien (Anlagenwerte `data/sim/plant.json`, Anfrage, Protokoll) und die
Aufrufe an Compose, Journal und Neustart. Der Container ist eine Attrappe, die nur ueber Dateien antwortet; die Frist laeuft
ueber die Uhr der Welt."""
import json

import pytest

SERVERWERTE = {"heizkurve": 1.8, "vorlauf_min": 28.0}
URSPRUNGSWERTE = {"heizkurve": 1.2, "vorlauf_min": 20.0}
KUNDENDATEN = ("data/device/identitaet.json", "data/device/sicherheitssatz.json", "data/secrets/drivers/vicare.json",
               "bus/credentials/runtime/bus.json", "bus/mosquitto/passwd", "zigbee2mqtt/configuration.yaml",
               "host/knopf.json")
BLEIBT = ("host/werk.json", "host/status.json", "bundles/0.3.0/docker-compose.yml", "archive/image.tar",
          "updater/state.json")


def _geloescht_und_neu_gestartet(welt, ergebnis) -> None:
    assert ergebnis["abgeschlossen"] is True
    assert welt.ablauf == ["down", "journal", "neustart"]
    assert not welt.anfrage_liegt()
    dateien = welt.baum()
    assert not [rel for rel in dateien if rel.startswith("data/")], "alles unter data/ ist weg"
    for rel in KUNDENDATEN:
        assert rel not in dateien, rel
    for rel in BLEIBT:
        assert rel in dateien, rel
    assert [(eintrag["ergebnis"]) for eintrag in welt.protokoll()] == ["ausgefuehrt"]


@pytest.mark.akzeptanz("AK-G9-03")
@pytest.mark.parametrize("lage", ("container_antwortet", "container_schweigt", "strom_weg_in_angefordert"))
def test_ak_g9_03_werksreset(welt, lage):
    if lage == "container_antwortet":
        ergebnis = welt.werksreset(antwortzeit=90.0)
        assert welt.fristen == [120.0]
        # vor dem Loeschen standen die Ursprungswerte wieder in der Anlage (der Container hatte geantwortet)
        assert json.loads(welt.beim_down["plant"]) == URSPRUNGSWERTE
        assert ergebnis["ursprungswerte"] is True
        _geloescht_und_neu_gestartet(welt, ergebnis)
        assert welt.protokoll()[0]["ursprungswerte"] is True
    elif lage == "container_schweigt":
        assert json.loads(welt.datei("data/sim/plant.json")) == SERVERWERTE
        ergebnis = welt.werksreset(antwortzeit=None)
        assert welt.fristen == [120.0]
        # nach der Frist wird trotzdem geloescht; die Anlage blieb auf den zuletzt geschriebenen Werten
        assert welt.ablauf[:1] == ["down"], "nach der Frist wird trotzdem geloescht"
        assert json.loads(welt.beim_down["plant"]) == SERVERWERTE
        assert ergebnis["ursprungswerte"] is False
        _geloescht_und_neu_gestartet(welt, ergebnis)
        assert welt.protokoll()[0]["ursprungswerte"] is False
    else:
        vorher_ausser_anfrage = {rel: text for rel, text in welt.baum().items() if rel != "host/werksreset.json"}
        assert welt.werksreset(antwortzeit=None, strom_weg=True) is None
        assert welt.docker.aufrufe == [], "vor dem Stromausfall wurde nichts angehalten"
        # Neustart der Welt auf den Dateien vom Stromausfall: ein Stromzyklus loest keinen Reset aus
        assert welt.start_nach_stromausfall() is False
        neu = welt.nachher
        assert not neu.anfrage_liegt(), "die Anfrage ist weg: der Container hebt damit die Abmeldung auf"
        assert neu.baum() == {**vorher_ausser_anfrage, "host/werksreset_protokoll.json": neu.datei("host/werksreset_protokoll.json")}
        assert [eintrag["ergebnis"] for eintrag in neu.protokoll()] == ["verworfen"]
        assert neu.docker.aufrufe == [] and neu.ablauf == []
