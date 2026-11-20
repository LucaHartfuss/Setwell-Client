"""Der zentrale Anzeige-Abgleich des Soll-Eingangs mit der echten Verdrahtung der Laufzeit (Planrechnung des lokalen Checks,
Lesewege des Workers, Wanduhr des Plans): das Thermostat bekommt nur das geltende Soll, und das nach jedem Anlass, der es
aendert, aus den Lesewegen und nie aus dem zuletzt abgelegten geltenden Soll."""
from datetime import UTC, datetime, timedelta

from fakes import FakeClock
from test_regulation import RESTORE_POINT, WOCHENPLAN, _mit_plan, _runtime

from smartheat_runtime import app
from smartheat_runtime.soll_eingang import (
    EV_SOLL_QUELLE,
    SollEingang,
    UebersteuerungsWeg,
    portal_beim_start,
)
from smartheat_runtime.soll_speicher import SOURCE_PORTAL, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

JETZT = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
UEB_BIS = "2026-11-05T13:00:00Z"
LEERSTAND = {"von": "2026-11-01T00:00:00Z", "bis": "2026-11-20T10:00:00Z", "temp": 16.0, "aufheizen_ab": None}
ABWESEND = {"bis": "2026-11-20T10:00:00Z", "temp": 17.0, "aufheizen_ab": None}
UEBERSTEUERUNG = {"temp": 19.0, "bis": UEB_BIS}


class Quelle:
    def __init__(self, wert):
        self.wert, self.geschrieben = wert, []

    def schreiben(self, value):
        self.geschrieben.append(value)
        return True

    def angezeigt(self):
        return self.wert

    def aus(self):
        return False


class Aufbau:
    """Laufzeit mit Plan, Worker mit den Lesewegen der Laufzeit, Speicher und Soll-Eingang daran."""

    def __init__(self, make_store, monkeypatch, tmp_path, bedienung, *, ueb=None, angezeigt=21.0, folgt=False):
        self.rt = rt = _runtime(make_store(backup={"last_room_target": 21.0, **RESTORE_POINT}), room_actual=20.0,
                                room_target=21.0)
        self.eingabe = _mit_plan(rt, monkeypatch, jetzt=JETZT,
                                 bedienung={"kreis": 1, "raum_soll": 21.0, "wochenplan": WOCHENPLAN, **bedienung})
        self.uhr = FakeClock()
        rt.worker = RegulationWorker(self.uhr)
        app._worker_lesewege(rt)
        rt.worker.register(EV_SOLL_QUELLE, lambda event: app._on_soll_quelle(rt, event))
        self.store = TargetStore(tmp_path / "soll.json", 21.0, self.uhr, lambda: self.eingabe["jetzt"].isoformat())
        if ueb is not None:
            self.store.uebersteuerung_setzen(ueb["temp"], ueb["bis"])
        self.store.anzeige_merken(angezeigt)
        self.quelle = Quelle(angezeigt)
        if folgt:  # das Thermostat zeigt, was man ihm schreibt
            schreiben = self.quelle.schreiben
            self.quelle.schreiben = lambda wert: [schreiben(wert), setattr(self.quelle, "wert", wert)][0]
        self.weg = UebersteuerungsWeg(
            lage=rt.worker.geltendes_soll, moeglich=lambda: True, senden=lambda *args: None,
            betriebsart=lambda: rt.worker.betriebsart(), betriebsart_trotz_aus=lambda: rt.worker.betriebsart_trotz_aus())
        self.eingang = SollEingang(
            rt.worker, self.store, self.quelle, wunsch=lambda wert, herkunft, durch: wert,
            soll_quelle_aus=lambda aus, wirksam: None, entprellen_s=0.0, uebersteuerung=self.weg)

    def check(self):
        app._run_local_check(self.rt, "Fehler im lokalen Check")
        self.rt.worker.run_pending()

    def weiter(self, sekunden):
        self.uhr.advance(sekunden)
        self.eingabe["jetzt"] += timedelta(seconds=sekunden)
        self.rt.worker.run_pending()


def test_ablauf_der_uebersteuerung_schreibt_die_leerstandstemperatur_auch_wenn_der_letzte_check_sie_noch_sah(
        make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0)
    a.check()
    assert a.rt.geltendes_soll.soll == 19.0 and a.quelle.geschrieben == []
    a.weiter(3700)  # kein Check dazwischen: der abgelegte Stand zeigt noch die Uebersteuerung
    assert a.quelle.geschrieben == [16.0]


def test_leerstand_beginnt_und_endet_nur_durch_den_check(make_store, monkeypatch, tmp_path):
    kommend = {**LEERSTAND, "von": "2026-11-05T13:00:00Z", "bis": "2026-11-05T15:00:00Z"}
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": kommend})
    a.check()
    assert a.quelle.geschrieben == []
    a.weiter(3900)  # der Leerstand hat begonnen, kein Ereignis ausser dem Check meldet es
    assert a.quelle.geschrieben == []
    a.check()
    assert a.quelle.geschrieben == [16.0]
    a.quelle.wert = 16.0
    a.store.thermostat_pruefen(16.0)  # das Thermostat bestaetigt die Schreibung
    a.weiter(7200)
    a.check()
    assert a.quelle.geschrieben == [16.0, 21.0]


def test_beginnt_away_bei_laufender_uebersteuerung_wird_sie_aufgehoben(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0)
    a.check()
    a.eingabe["bedienung"] = {"kreis": 1, "raum_soll": 21.0, "wochenplan": WOCHENPLAN, "modus": "abwesend",
                              "abwesend": ABWESEND}
    a.check()
    assert a.quelle.geschrieben == [17.0] and a.store.uebersteuerung() is None
    a.eingabe["bedienung"] = {"kreis": 1, "raum_soll": 21.0, "wochenplan": WOCHENPLAN, "modus": "leerstand",
                              "leerstand": LEERSTAND}
    a.quelle.wert = 17.0
    a.store.thermostat_pruefen(17.0)  # das Thermostat bestaetigt die Schreibung
    a.weiter(100)
    a.check()
    assert a.quelle.geschrieben == [17.0, 16.0]  # nicht wieder die alte Uebersteuerung


def test_ein_portal_wunsch_im_leerstand_behaelt_die_uebersteuerung_ohne_fenster(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0)
    a.check()
    a.rt.worker.post(Event("shg_target_input", {"value": 22.0, "source": SOURCE_PORTAL}))
    a.rt.worker.run_pending()
    assert a.store.value == 22.0 and a.store.uebersteuerung() == UEBERSTEUERUNG and a.quelle.geschrieben == []


def test_portal_beim_start_im_leerstand_behaelt_die_uebersteuerung_atomar(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0)
    geschrieben = []
    from smartheat_core import dateien
    echt = dateien.schreiben_json
    a_pfad = a.store._ueb_path
    dateien.schreiben_json = lambda pfad, inhalt, **kw: (geschrieben.append((pfad, inhalt)), echt(pfad, inhalt, **kw))[1]
    try:
        portal_beim_start(a.store, a.weg, 22.0)
    finally:
        dateien.schreiben_json = echt
    assert a.store.value == 22.0 and a.store.uebersteuerung() == UEBERSTEUERUNG
    assert [inhalt for pfad, inhalt in geschrieben if pfad == a_pfad] == []  # die Datei der Uebersteuerung blieb unberuehrt


def test_portal_beim_start_ausserhalb_des_leerstands_beendet_die_uebersteuerung(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, ueb=UEBERSTEUERUNG, angezeigt=19.0)
    portal_beim_start(a.store, a.weg, 22.0)
    assert a.store.value == 22.0 and a.store.uebersteuerung() is None


def test_eine_verlorene_schreibung_wird_je_check_nicht_wiederholt(make_store, monkeypatch, tmp_path):
    """Das Thermostat meldet nichts zurueck (die Live-Anzeige bleibt beim alten Wert): weder weitere Checks noch weitere
    Anlaesse schreiben dieselbe Anzeige noch einmal; das Nachschreiben der Pruefung der Meldung bleibt der eine Weg."""
    kommend = {**LEERSTAND, "von": "2026-11-05T13:00:00Z"}
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": kommend})
    a.check()  # der erste Check merkt nur das Geltende
    a.weiter(3900)
    a.check()
    assert a.quelle.geschrieben == [16.0]
    for _ in range(5):
        a.weiter(1900)  # Echo-Fenster und Frist des Schreibbefehls sind vorbei
        a.check()
        a.eingang.anzeige_abgleichen("noch einmal")
    assert a.quelle.geschrieben == [16.0] and a.quelle.wert == 21.0


def test_ohne_aenderung_schreibt_kein_check(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"})
    for _ in range(3):
        a.weiter(100)
        a.check()
    assert a.quelle.geschrieben == []


def test_eine_aenderung_des_geltenden_soll_waehrend_des_echo_fensters_wird_danach_nachgeholt(
        make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0, folgt=True)
    a.check()
    a.weiter(3700)
    assert a.quelle.geschrieben == [16.0]
    a.eingabe["bedienung"] = {"kreis": 1, "raum_soll": 21.0, "wochenplan": WOCHENPLAN, "modus": "abwesend",
                              "abwesend": ABWESEND}
    a.check()  # im Echo-Fenster: noch nicht
    assert a.quelle.geschrieben == [16.0]
    a.store.thermostat_pruefen(16.0)  # das Thermostat bestaetigt die Schreibung
    a.weiter(70)
    assert a.quelle.geschrieben == [16.0, 17.0]
