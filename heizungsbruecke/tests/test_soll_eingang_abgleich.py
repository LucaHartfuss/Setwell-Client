"""Der zentrale Anzeige-Abgleich des Soll-Eingangs mit der echten Verdrahtung der Laufzeit (Planrechnung des lokalen Checks,
Lesewege des Workers, Wanduhr des Plans): das Thermostat bekommt nur das geltende Soll, und das nach jedem Anlass, der es
aendert, aus den Lesewegen und nie aus dem zuletzt abgelegten geltenden Soll."""
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeClock
from test_regulation import RESTORE_POINT, WOCHENPLAN, _mit_plan, _runtime

from smartheat_core import dateien
from smartheat_runtime import app
from smartheat_runtime.soll_eingang import (
    EV_SOLL_ENDE,
    EV_SOLL_ENDE_ERLEDIGT,
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
        self.wert, self.geschrieben, self.ausgeschaltet = wert, [], False

    def schreiben(self, value):
        self.geschrieben.append(value)
        return True

    def angezeigt(self):
        return self.wert

    def aus(self):
        return self.ausgeschaltet


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
        if angezeigt is not None:
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


def _kreis_ausser_betrieb(a, ausser=True):
    a.rt.config = dataclasses.replace(a.rt.config, zustand_source=lambda: "ausser_betrieb" if ausser else "in_betrieb")


def _ende(a):
    a.rt.worker.post(Event(EV_SOLL_ENDE))
    a.rt.worker.run_pending()


def test_ablauf_der_uebersteuerung_nach_dem_soll_ende_schreibt_nichts(make_store, monkeypatch, tmp_path):
    """Nach dem Ende der Regelung zeigt die Soll-Quelle den gemerkten Wunsch (nie weniger); der Ablauf der Uebersteuerung
    darf sie nicht auf die Leerstandstemperatur absenken, auch nicht unter dem Wert, den der Kunde danach einstellt."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0, folgt=True)
    a.check()
    _kreis_ausser_betrieb(a)
    _ende(a)
    assert a.quelle.geschrieben == [21.0]
    a.quelle.wert = 23.0  # der Kunde dreht danach hoeher
    a.store.thermostat_pruefen(23.0)
    a.weiter(3700)  # die Uebersteuerung laeuft ab
    a.check()
    assert a.quelle.geschrieben == [21.0]


def test_der_leerstand_beginnt_bei_ausser_betrieb_ohne_schreibung(make_store, monkeypatch, tmp_path):
    kommend = {**LEERSTAND, "von": "2026-11-05T13:00:00Z"}
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": kommend})
    _kreis_ausser_betrieb(a)
    a.check()
    a.weiter(3900)
    a.check()
    assert a.quelle.geschrieben == []


def test_abo_ende_mit_laufender_uebersteuerung_im_leerstand_schreibt_nichts_weiter(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0, folgt=True)
    a.check()
    a.rt.idle = True  # abo._enter_idle: nur die Trigger stehen, die Ereignisse der Uebersteuerung laufen weiter
    a.rt.store.update(abo_finished=True)
    _ende(a)
    assert a.quelle.geschrieben == [21.0]
    a.weiter(3700)
    a.check()
    a.eingang.anzeige_abgleichen("noch einmal")
    assert a.quelle.geschrieben == [21.0]


def test_ohne_ende_aber_im_ruhezustand_schreibt_der_ablauf_nichts(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0)
    a.check()
    a.rt.idle = True
    a.weiter(3700)
    assert a.quelle.geschrieben == []


def test_die_wiederaufnahme_gleicht_einmal_das_geltende_soll_an(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0, folgt=True)
    a.check()
    _kreis_ausser_betrieb(a)
    _ende(a)
    a.weiter(3700)  # Uebersteuerung abgelaufen, Echo-Fenster vorbei
    a.store.thermostat_pruefen(21.0)
    assert a.quelle.geschrieben == [21.0]
    _kreis_ausser_betrieb(a, False)  # der Kreis kehrt in den Betrieb zurueck
    a.check()
    assert a.quelle.geschrieben == [21.0, 16.0]
    a.store.thermostat_pruefen(16.0)
    a.weiter(100)
    a.check()
    assert a.quelle.geschrieben == [21.0, 16.0]


def test_start_nach_ablauf_der_uebersteuerung_waehrend_der_auszeit_gleicht_im_leerstand_an(
        make_store, monkeypatch, tmp_path):
    """Dirty-Start: kein Angleichen aus einer Bedienung, die Uebersteuerung lief in der Auszeit ab, das Thermostat zeigt noch
    ihren Wert; der erste Check gleicht auf die Leerstandstemperatur an."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=19.0)
    a.check()
    assert a.quelle.geschrieben == [16.0]


def test_der_erste_check_schreibt_nie_den_blossen_wunsch_ueber_die_anzeige_des_kunden(make_store, monkeypatch, tmp_path):
    """Ohne Betriebsart und ohne Uebersteuerung ist der Wunsch das Geltende; das Thermostat darf dann beim Start etwas
    anderes zeigen (der Kunde hat es offline verstellt): kein ungebetenes Schreiben (das Gateway schrieb sonst 20,0)."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, angezeigt=22.0)
    a.check()
    assert a.quelle.geschrieben == []


def test_der_erste_check_schreibt_nicht_bei_unbekannter_anzeige(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=None)
    a.check()
    assert a.quelle.geschrieben == []


def test_der_erste_check_schreibt_nicht_bei_ausgeschalteter_quelle(make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=19.0)
    a.quelle.ausgeschaltet = True
    a.check()
    assert a.quelle.geschrieben == []


def test_der_erste_check_ueberschreibt_die_frostschutz_stellung_nicht(make_store, monkeypatch, tmp_path):
    """HA-Start im Leerstand: das Thermostat zeigt live 5 Grad (Frostschutz-Stellung, gehoert dem Kunden); der erste Check
    kommt vor dem Lesen des Solls und darf sie nicht auf die Leerstandstemperatur anheben. Danach greift `aus`."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=5.0)
    a.check()
    assert a.quelle.geschrieben == []
    a.rt.worker.post(Event("shg_target_input", {"value": 5.0, "source": "thermostat"}))
    a.rt.worker.run_pending()
    a.check()
    assert a.eingang.aus and a.quelle.geschrieben == []


def test_der_erste_check_schreibt_nicht_aus_der_gemerkten_anzeige_bei_leerem_spiegel(make_store, monkeypatch, tmp_path):
    """Gateway-Start: der Spiegel ist leer (die Live-Anzeige unbekannt), die gemerkte Anzeige ist noch die der Uebersteuerung.
    Ein Rueckfall auf die gemerkte Anzeige schriebe auf ein Thermostat, das nichts gemeldet hat."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=19.0)
    a.quelle.wert = None  # gemerkt 19, live unbekannt
    a.check()
    assert a.quelle.geschrieben == []


def _leerstand_ende(make_store, monkeypatch, tmp_path, *, angezeigt=21.0, mit_ende=False):
    """Leerstand (16 Grad) bei Wunsch 21, danach ist der Kreis ausser Betrieb: ohne Sperre ergaebe jeder Weg etwas anderes
    als den gemerkten Wunsch (Leerstandstemperatur oder Wunsch statt dem, was die Quelle zeigt)."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=angezeigt)
    a.check()
    a.weiter(100)
    a.geschrieben_vorher = list(a.quelle.geschrieben)
    _kreis_ausser_betrieb(a)
    if mit_ende:
        _ende(a)
    return a


def test_nach_dem_ende_schreibt_das_nachschreiben_nur_die_schreibung_des_soll_endes(make_store, monkeypatch, tmp_path):
    """Das Soll-Ende schreibt den Wunsch 21 ueber die Leerstandstemperatur 16; der Befehl geht verloren, das Thermostat
    meldet nach der Frist weiter 16: einmal noch den Wunsch, nie die Leerstandstemperatur."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=16.0)
    a.check()
    _kreis_ausser_betrieb(a)
    _ende(a)
    assert a.quelle.geschrieben == [21.0]
    a.weiter(1900)
    a.rt.worker.post(Event("shg_target_input", {"value": 16.0, "source": "thermostat"}))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben == [21.0, 21.0]


@pytest.mark.parametrize("beendet", [False, True])
def test_nach_dem_ende_schreibt_das_nachschreiben_nicht_unter_den_stand_des_thermostats(
        make_store, monkeypatch, tmp_path, beendet):
    """Der Wunsch 21 ging verloren, das Thermostat steht noch auf 23: in Betrieb schreibt das Nachschreiben ihn noch
    einmal, nach dem Ende der Regelung nicht (es senkte den Wert, den das Thermostat zeigt)."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, angezeigt=23.0)
    a.store.note_own_write(veraltet=23.0)
    if beendet:
        _kreis_ausser_betrieb(a)
    a.weiter(1900)
    a.rt.worker.post(Event("shg_target_input", {"value": 23.0, "source": "thermostat"}))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben == ([] if beendet else [21.0])


@pytest.mark.parametrize("beendet", [False, True])
def test_nach_dem_ende_schreibt_das_nachschreiben_nichts_unter_dem_wunsch(make_store, monkeypatch, tmp_path, beendet):
    """Eine Schreibung der Leerstandstemperatur ging verloren, die Regelung endet: das Nachschreiben bringt sie nicht mehr
    (in Betrieb schreibt es sie noch einmal)."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND}, angezeigt=21.0)
    a.check()  # schreibt 16, das Thermostat meldet weiter 21
    assert a.quelle.geschrieben == [16.0]
    if beendet:
        _kreis_ausser_betrieb(a)
    a.weiter(1900)
    a.rt.worker.post(Event("shg_target_input", {"value": 21.0, "source": "thermostat"}))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben == ([16.0] if beendet else [16.0, 16.0])


def test_nach_dem_ende_schreibt_ein_portal_wunsch_nichts(make_store, monkeypatch, tmp_path):
    a = _leerstand_ende(make_store, monkeypatch, tmp_path, angezeigt=16.0, mit_ende=True)
    vorher = list(a.quelle.geschrieben)
    a.rt.worker.post(Event("shg_target_input", {"value": 22.0, "source": SOURCE_PORTAL}))
    a.rt.worker.run_pending()
    assert a.store.value == 22.0 and a.quelle.geschrieben == vorher


def test_nach_dem_ende_schreibt_das_wiedereinschalten_nichts(make_store, monkeypatch, tmp_path):
    a = _leerstand_ende(make_store, monkeypatch, tmp_path, angezeigt=16.0)
    for modus in ("off", "heat"):
        a.rt.worker.post(Event("shg_soll_modus", {"mode": modus}))
        a.rt.worker.run_pending()
    assert a.quelle.geschrieben == a.geschrieben_vorher


def test_nach_dem_ende_schreibt_der_wartende_abgleich_nichts(make_store, monkeypatch, tmp_path):
    a = _leerstand_ende(make_store, monkeypatch, tmp_path, angezeigt=16.0)
    a.rt.worker.post(Event("shg_anzeige_abgleich"))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben == a.geschrieben_vorher


@pytest.mark.parametrize("mit_ende", [True, False])
def test_nach_dem_ende_plant_das_ende_einer_neuen_uebersteuerung_nichts_ein(make_store, monkeypatch, tmp_path, mit_ende):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND},
               ueb=UEBERSTEUERUNG, angezeigt=19.0)
    a.check()
    _kreis_ausser_betrieb(a)
    if mit_ende:
        _ende(a)
    geplant = []
    plan = a.rt.worker.schedule
    a.rt.worker.schedule = lambda sekunden, event: (geplant.append(event.kind), plan(sekunden, event))[1]
    a.store.uebersteuerung_setzen(20.0, "2026-11-05T15:00:00Z")  # nach dem Ende gesetzt, nicht durch die Regelung
    a.weiter(3700)  # das alte Ende der Uebersteuerung faellt
    assert "shg_ueb_ablauf" not in geplant


def test_die_stilllegung_endet_erst_mit_dem_check_nach_der_wiederaufnahme(make_store, monkeypatch, tmp_path):
    """Der Kreis laeuft wieder, aber noch kein Check: ein Portal-Wunsch schreibt nichts; der naechste Check gleicht einmal an."""
    a = _leerstand_ende(make_store, monkeypatch, tmp_path, angezeigt=16.0, mit_ende=True)
    a.quelle.wert = 21.0  # das Thermostat zeigt den Wunsch des Soll-Endes
    _kreis_ausser_betrieb(a, False)
    a.rt.idle = True  # die Regelung ruht weiter, ohne dass ein neues Soll-Ende kaeme
    a.check()  # waehrend der Ruhe: aendert nichts an der Stilllegung
    a.rt.idle = False
    vorher = list(a.quelle.geschrieben)
    a.rt.worker.post(Event("shg_target_input", {"value": 22.0, "source": SOURCE_PORTAL}))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben == vorher
    a.weiter(1900)  # Echo-Fenster der Schreibung des Soll-Endes vorbei: auch der wartende Abgleich schreibt nichts
    assert a.quelle.geschrieben == vorher
    a.check()
    assert a.quelle.geschrieben == [*vorher, 16.0]


@pytest.mark.parametrize("zustand", ["ausser_betrieb", "ruhezustand", "abo_ende"])
def test_nach_dem_ende_der_regelung_kennen_die_lesewege_keine_betriebsart(make_store, monkeypatch, tmp_path, zustand):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND})
    assert a.rt.worker.betriebsart() is not None and a.rt.worker.betriebsart_trotz_aus() is not None
    if zustand == "ausser_betrieb":
        _kreis_ausser_betrieb(a)
    elif zustand == "ruhezustand":
        a.rt.idle = True
    else:
        a.rt.store.update(abo_finished=True)
    assert a.rt.worker.betriebsart() is None and a.rt.worker.betriebsart_trotz_aus() is None
    assert a.eingang.geltende_anzeige() == 21.0


def test_nach_dem_ende_ist_eine_lokale_eingabe_ein_gewoehnlicher_wunsch(make_store, monkeypatch, tmp_path):
    """Ohne Betriebsart (Leerstand wuerde laufen) geht die lokale Zahl als Wunsch an die Quelle."""
    a = _leerstand_ende(make_store, monkeypatch, tmp_path, angezeigt=16.0)
    a.rt.worker.post(Event("shg_target_input", {"value": 22.0, "source": "lokal"}))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben[-1:] == [22.0] and a.store.value == 22.0


def test_nach_dem_ende_wird_die_uebersteuerung_aufgehoben_und_das_nachschreiben_schreibt_sie_nicht(
        make_store, monkeypatch, tmp_path):
    """Die Uebersteuerung 19 geht verloren, die Regelung endet, nach der Frist meldet das Thermostat wieder 22: der
    Wunsch gilt, die abgelaufene Uebersteuerung darf nicht noch einmal geschrieben werden."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, angezeigt=22.0)
    a.store.apply_portal(22.0)
    a.store.uebersteuerung_setzen(19.0, UEB_BIS)
    a.check()  # der erste Check schreibt die Uebersteuerung; der Befehl geht verloren (die Anzeige bleibt 22)
    assert a.quelle.geschrieben == [19.0]
    _kreis_ausser_betrieb(a)
    _ende(a)
    a.weiter(1900)  # die Frist des Schreibbefehls ist vorbei
    a.rt.worker.post(Event("shg_target_input", {"value": 22.0, "source": "thermostat"}))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben == [19.0] and a.store.uebersteuerung() is None


def test_ein_scheiternder_speicher_beim_aufheben_verhindert_das_soll_ende_nicht(make_store, monkeypatch, tmp_path):
    """Die Platte ist voll: die Uebersteuerung wird im Speicher aufgehoben, die Datei nicht. Das Soll-Ende schreibt den
    Wunsch trotzdem und meldet sich erledigt."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, ueb=UEBERSTEUERUNG, angezeigt=19.0, folgt=True)
    erledigt = []
    a.rt.worker.register(EV_SOLL_ENDE_ERLEDIGT, lambda event: erledigt.append(event))

    schreiben = dateien.schreiben_json

    def voll(pfad, *args, **kwargs):
        if pfad == a.store._ueb_path:
            raise OSError(28, "No space left")
        return schreiben(pfad, *args, **kwargs)

    monkeypatch.setattr(dateien, "schreiben_json", voll)
    _kreis_ausser_betrieb(a)
    _ende(a)
    assert a.quelle.geschrieben == [21.0] and len(erledigt) == 1 and a.store.uebersteuerung() is None


def test_nach_dem_ende_schreibt_das_nachschreiben_nicht_die_geschriebene_uebersteuerung_ueber_dem_wunsch(
        make_store, monkeypatch, tmp_path):
    """Der erste Check schreibt die Uebersteuerung 23 (Wunsch 21); der Befehl geht verloren, das Thermostat zeigt 21. Das
    Soll-Ende schreibt nichts (21 ist der Wunsch). Nach der Frist meldet das Thermostat 21: nichts, nie die 23."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, ueb={"temp": 23.0, "bis": UEB_BIS}, angezeigt=21.0)
    a.check()
    assert a.quelle.geschrieben == [23.0]
    _kreis_ausser_betrieb(a)
    _ende(a)
    assert a.quelle.geschrieben == [23.0]
    a.weiter(1900)
    a.rt.worker.post(Event("shg_target_input", {"value": 21.0, "source": "thermostat"}))
    a.rt.worker.run_pending()
    assert a.quelle.geschrieben == [23.0]


def _speicher_voll(monkeypatch):
    """Jedes Schreiben einer Datei scheitert (Datentraeger voll oder nur lesbar)."""
    def voll(*args, **kwargs):
        raise OSError(28, "No space left")

    monkeypatch.setattr(dateien, "schreiben_json", voll)


def test_scheitert_jedes_speichern_wird_das_soll_ende_trotzdem_geschrieben_und_erledigt_gemeldet(
        make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, ueb=UEBERSTEUERUNG, angezeigt=19.0, folgt=True)
    erledigt = []
    a.rt.worker.register(EV_SOLL_ENDE_ERLEDIGT, lambda event: erledigt.append(event))
    _speicher_voll(monkeypatch)
    _kreis_ausser_betrieb(a)
    _ende(a)
    assert a.quelle.geschrieben == [21.0] and len(erledigt) == 1
    assert a.store.uebersteuerung() is None and a.store.angezeigt_zuletzt == 21.0


def test_scheitert_jedes_speichern_gleicht_der_betrieb_trotzdem_an(make_store, monkeypatch, tmp_path):
    """Die Uebersteuerung laeuft ab, die Platte ist voll: die Leerstandstemperatur geht trotzdem an das Thermostat."""
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND,
                                                    "uebersteuerung": UEBERSTEUERUNG},
               ueb=UEBERSTEUERUNG, angezeigt=19.0, folgt=True)
    a.check()
    _speicher_voll(monkeypatch)
    a.weiter(3700)
    assert a.quelle.geschrieben == [16.0] and a.store.uebersteuerung() is None and a.store.angezeigt_zuletzt == 16.0


def test_scheitert_das_speichern_gilt_der_stand_im_speicher_und_die_aenderung_wird_gemeldet(
        make_store, monkeypatch, tmp_path):
    a = Aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"}, angezeigt=None)
    gemeldet = []
    a.store._on_change = lambda: gemeldet.append(a.store.value)
    _speicher_voll(monkeypatch)
    a.store.uebersteuerung_setzen(19.0, UEB_BIS)
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": UEB_BIS}
    assert a.store.apply_portal(22.0, uebersteuerung_behalten=True) is True
    assert a.store.value == 22.0 and gemeldet == [22.0]
