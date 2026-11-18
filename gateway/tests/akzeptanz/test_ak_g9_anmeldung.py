"""Die Diagnoseseite im LAN ist erreichbar, waehrend die Laufzeit die Anlage regelt: eine Flut paralleler Anmeldungen
(echtes scrypt) prueft immer nur einmal zugleich, ein uebergrosser Kopf wird ohne Antwort geschlossen, die Laufzeit
schreibt waehrenddessen weiter, und kein Geheimnis gelangt ins Log. Die Verbindungen laufen ueber echte Sockets auf
127.0.0.1 gegen den Server, den die Laufzeit selbst startet."""
import logging

import pytest
from anlagen import konfiguration
from welt_diagnose import Flut, anmelden

PARALLEL = 30
RICHTIG = "KANARIE-richtig-7d1f3b"  # das Passwort des Etiketts (Kanarienwert)
FALSCH = "KANARIE-falsch-4c9e82"  # das Passwort der Flut (Kanarienwert)
WERTE = ({"curve": 0.9, "level": -5.0, "room_setpoint": 21.0}, {"curve": 0.9, "level": -5.0, "room_setpoint": 23.0})


@pytest.mark.akzeptanz("AK-G9-05")
def test_anmeldeflut_prueft_einzeln_und_stoert_die_regelung_nicht(welt, caplog):
    caplog.set_level(logging.DEBUG)
    port, eintrag = welt.diagnose_vorbereiten(RICHTIG)
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, konfiguration("viessmann_vicare"))
    welt.bis_nach_tagestick(22.0)

    def zyklus(nr: int) -> bool:
        """Ein Tag, eine Antwort des Dienstes mit anderem Wunschwert; ob die Laufzeit den Hebel geschrieben hat."""
        vorher = welt.anlagenstand()["geschrieben_um"].get("room_setpoint")
        seq = welt.neuer_tag_mit_tick(22.0)
        welt.vom_server("down/setpoints", {"schema": 5, "kreis": 1, "seq": seq, "ts": welt.uhr.wand().isoformat(),
                                           "status": "ok", "reason": None, "levers": WERTE[nr % 2], "learned": {}})
        welt.runde(2)
        stand = welt.anlagenstand()
        soll = WERTE[nr % 2]["room_setpoint"]
        return stand["geschrieben_um"].get("room_setpoint") != vorher and stand["hebel"]["room_setpoint"] == soll

    assert zyklus(0)  # die Regelung schreibt vor der Flut
    flut = Flut(port, FALSCH, PARALLEL)
    zyklen = geschrieben = 0
    while flut.laeuft():  # die Laufzeit arbeitet weiter, solange die Flut laeuft
        zyklen += 1
        geschrieben += zyklus(zyklen)
    flut.warten()
    assert zyklen >= 1 and geschrieben == zyklen, (zyklen, geschrieben)  # jede Antwort wurde waehrend der Flut geschrieben

    # genau eine Pruefung lief (falsches Passwort), alle anderen wurden abgewiesen, ohne zu pruefen
    status = sorted(code for code, _ in flut.antworten)
    assert status == [401] + [429] * (PARALLEL - 1), status
    assert all(retry is not None and int(retry) > 0 for code, retry in flut.antworten if code == 429)
    assert flut.kopf_antwort == b"", flut.kopf_antwort[:80]  # der Kopf von 6 MB: Verbindung zu, ohne Antwort

    # die abgewiesenen Versuche zaehlen nicht als Fehlversuch: das richtige Passwort wird angenommen, nicht gesperrt
    code, kopf = anmelden(port, RICHTIG)
    assert code == 303 and "set-cookie" in kopf, (code, kopf)

    protokoll = caplog.text
    assert caplog.records  # das Log wird erfasst
    for geheimnis in (RICHTIG, FALSCH, eintrag["hash"], eintrag["salz"]):
        assert geheimnis not in protokoll
