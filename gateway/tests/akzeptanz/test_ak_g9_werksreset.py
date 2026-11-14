"""AK-G9-03, Gateway-Haelfte: der Werksreset am Knopf, wie ihn die Laufzeit im Container erlebt. Die Welt spielt den Host:
sie legt host/werksreset.json mit Phase angefordert an und nimmt sie wieder weg. Beobachtet werden nur Dateien (Anlage
`plant.json`, Antwort `werksreset_bereit.json`), Link (Status) und die Aufzeichnung der Zigbee-Schreibvorgaenge; die
Host-Haelfte (Frist, Loeschen, Protokoll) steht im Ordner der Host-Akzeptanz."""
import datetime
import json

import pytest
from anlagen import konfiguration

#: Startwerte der simulierten Anlage (woertlich): was die Rueckstellung wiederherstellt
URSPRUNG = {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0, "min_flow": 20.0}
#: Werte des Dienstes, verschieden von den Startwerten und innerhalb der lokalen Grenzen
GELERNT = {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0, "min_flow": 22.0}
RAUM = 22.0  # warm genug: kein Boost


def _hebel(welt) -> dict:
    return {name: wert for name, wert in welt.anlagenstand()["hebel"].items() if name in URSPRUNG}


def _antwortdatei(welt):
    return welt.host_dir.parent / "data" / "werksreset_bereit.json"


def _anfragedatei(welt):
    return welt.host_dir / "werksreset.json"


def _anfordern(welt) -> str:
    """Der Host legt die Anfrage an (Phase angefordert, seq 1, ts jetzt); die Laufzeit bekommt Zeit zu antworten."""
    ts = welt.uhr.wand().isoformat()
    _anfragedatei(welt).write_text(json.dumps({"phase": "angefordert", "seq": 1, "ts": ts}))
    welt.runde(10)
    welt.verbinden()
    welt.runde(10)
    return ts


def _tagestick(welt) -> list:
    """Ein Tag mit Tagestick vergeht (warm, kein Boost); die Snapshots, die dabei auf dem aktuellen Link gesendet wurden."""
    welt.bis_nach_tagestick(RAUM)
    welt.uhr.vor(86400 - 300 - 2)  # der Raumfuehler ist danach veraltet: er meldet vor dem naechsten Tick
    welt.raum(RAUM)
    welt.runde(300)
    return welt.link.von("up/snapshot")


def _regelt(welt, werte: dict) -> None:
    """Die Laufzeit regelt: der Tagestick sendet einen Snapshot, eine gueltige Antwort des Dienstes schreibt die Werte."""
    assert _tagestick(welt), "die Laufzeit sendet wieder Snapshots"
    welt.antworten(werte)
    welt.runde(1)
    assert _hebel(welt) == werte
    assert welt.status()[-1] != "abgemeldet"


def _schweigt(welt) -> None:
    """Die abgemeldete Laufzeit sendet nach einem Tag keinen Snapshot und schreibt nichts in die Anlage."""
    assert _tagestick(welt) == []
    assert _hebel(welt) == URSPRUNG
    assert welt.status()[-1] == "abgemeldet"


def _eingerichtet_und_regelnd(welt) -> None:
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, konfiguration("vaillant_vrc720"))
    welt.antworten(GELERNT)
    welt.runde(1)
    assert _hebel(welt) == GELERNT  # die Regelung hat geschrieben: der Reset hat etwas zurueckzustellen


def _angefordert_und_beantwortet(welt) -> None:
    _eingerichtet_und_regelnd(welt)
    ts = _anfordern(welt)
    assert _hebel(welt) == URSPRUNG  # die Ursprungswerte stehen wieder in der Anlage
    assert welt.status()[-1] == "abgemeldet"
    antwort = json.loads(_antwortdatei(welt).read_text())
    assert antwort["seq"] == 1 and antwort["ursprungswerte"] is True
    assert datetime.datetime.fromisoformat(antwort["ts"]) >= datetime.datetime.fromisoformat(ts)


@pytest.mark.akzeptanz("AK-G9-03")
@pytest.mark.parametrize("lage", ("angefordert", "verworfen", "verworfen_nach_neustart", "sign_off_bleibt"))
def test_ak_g9_03_werksreset_der_laufzeit(welt, lage):
    if lage == "angefordert":
        _angefordert_und_beantwortet(welt)
        _schweigt(welt)  # die abgemeldete Laufzeit regelt nicht mehr: kein Snapshot, kein Schreiben
        return
    _angefordert_und_beantwortet(welt)
    if lage == "verworfen_nach_neustart":
        antwort = _antwortdatei(welt).read_text()
        welt.container_neu_starten()  # die Anfrage liegt noch: schon beantwortet, kein neuer Reset
        welt.verbinden()
        welt.runde(10)
        assert welt.status()[-1] == "abgemeldet"
        assert _antwortdatei(welt).read_text() == antwort, "die Anfrage wurde ein zweites Mal beantwortet"
    if lage == "sign_off_bleibt":
        welt.befehl("c-1", "sign_off")  # der Dienst meldet die Anlage selbst ab
        welt.runde(10)
        assert welt.befehlsergebnis("c-1")["ok"] is True
    _anfragedatei(welt).unlink()  # der Host hat die Phase nicht erreicht: die Anfrage verschwindet
    welt.runde(10)
    welt.verbinden()
    welt.runde(10)
    if lage == "sign_off_bleibt":
        _schweigt(welt)  # die Abmeldung des Dienstes bleibt: kein Snapshot, kein Schreiben
        return
    assert welt.status()[-1] != "abgemeldet"  # die Abmeldung der Anfrage ist aufgehoben
    _regelt(welt, GELERNT)  # die Laufzeit regelt wieder: eine gueltige Antwort des Dienstes schreibt
