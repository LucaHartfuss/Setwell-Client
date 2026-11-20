"""Soll-Eingang in der kuehlen Phase: eine Aenderung am Thermostat oder an der lokalen Zahl ist dort eine Uebersteuerung
bis zum Aufheizbeginn, sonst ein Wunsch (Spec Betriebsarten 7)."""
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeClock

from smartheat_core import safety
from smartheat_core.plan import (
    SOLL_ART_ABSENKUNG,
    SOLL_ART_AUFHEIZEN,
    SOLL_ART_UEBERSTEUERT,
    SOLL_ART_WUNSCH,
    SollErgebnis,
)
from smartheat_runtime.leerstand_zaehler import FENSTER, UEBERSTEUERUNG_DAUER
from smartheat_runtime.runtime import EV_LOCAL_CHECK
from smartheat_runtime.soll_eingang import (
    EV_SOLL_MODUS,
    EV_TARGET_INPUT,
    Betriebsart,
    SollEingang,
    UebersteuerungsWeg,
)
from smartheat_runtime.soll_speicher import SOURCE_LOKAL, SOURCE_PORTAL, SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

AUFHEIZBEGINN = datetime(2026, 1, 15, 21, 30, tzinfo=UTC)
BIS = "2026-01-15T21:30:00Z"


class Quelle:
    def __init__(self, wert=21.0):
        self.wert, self.geschrieben = wert, []

    def schreiben(self, value):
        self.geschrieben.append(value)
        self.wert = value
        return True

    def angezeigt(self):
        return self.wert

    def aus(self):
        return False


class Aufbau:
    def __init__(self, tmp_path: Path, *, art=SOLL_ART_ABSENKUNG, faehig=True, wechsel=AUFHEIZBEGINN, weg=True,
                 fehler=None, entprellen_s=0.0):
        self.clock = FakeClock()
        self.worker = RegulationWorker(self.clock)
        self.jetzt = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)  # die Wanduhr des Speichers
        self.store = TargetStore(tmp_path / "soll.json", 21.0, self.clock, lambda: self.jetzt.isoformat())
        self.quelle = Quelle()
        self.wuensche, self.gesendet, self.checks, self.beendet = [], [], [], []
        self.betriebsart: Betriebsart | None = None
        self.lage = SollErgebnis(soll=18.0, soll_art=art, naechster_wechsel=wechsel)
        self.faehig, self.fehler = faehig, fehler
        self.beenden_ohne_wert = False  # der Geraetekern gibt zum Ende keinen begrenzten Wunsch zurueck
        self.worker.register(EV_LOCAL_CHECK, lambda event: self.checks.append(event.data))
        self.eingang = SollEingang(
            self.worker, self.store, self.quelle, wunsch=self._wunsch, soll_quelle_aus=lambda aus, wirksam: None,
            entprellen_s=entprellen_s,
            uebersteuerung=UebersteuerungsWeg(lage=lambda: self.lage, senden=self._senden, moeglich=lambda: self.faehig,
                                              betriebsart=lambda: self.betriebsart, beenden=self._beenden)
            if weg else None)

    def _beenden(self, art, raum_soll, uebersteuerung, herkunft, durch):
        self.beendet.append((art, raum_soll, uebersteuerung, herkunft, durch))
        return None if raum_soll is None or self.beenden_ohne_wert else min(max(raum_soll, 15.0), 25.0)

    def weiter(self, dauer: timedelta) -> None:
        self.jetzt += dauer

    def _wunsch(self, value, herkunft, durch):
        self.wuensche.append((value, herkunft, durch))
        return min(max(value, 15.0), 25.0)

    def _senden(self, temp, bis, herkunft, durch):
        if self.fehler is not None:
            raise self.fehler
        self.gesendet.append((temp, bis, herkunft, durch))

    def post(self, value, source, **extra):
        self.worker.post(Event(EV_TARGET_INPUT, {"value": value, "source": source, **extra}))
        self.worker.run_pending()

    def modus(self, mode):
        self.worker.post(Event(EV_SOLL_MODUS, {"mode": mode}))
        self.worker.run_pending()

    def entprellen(self):
        self.clock.advance(11)
        self.worker.run_pending()


VORHER = "2026-01-15T21:00:00Z"  # Ende einer Uebersteuerung, die schon vor der Eingabe galt


def test_kuehl_aenderung_am_thermostat_ist_eine_uebersteuerung_bis_zum_aufheizbeginn(tmp_path):
    a = Aufbau(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}
    assert (a.store.value, a.store.source) == (21.0, SOURCE_PORTAL)  # der Wunsch bleibt
    assert a.gesendet == [(19.0, BIS, "soll_entity", "nutzer")] and a.wuensche == []
    assert a.checks and a.checks[-1]["room_target_fired"] is True
    assert a.quelle.geschrieben == []  # das Thermostat zeigt den Wert schon


def test_kuehl_lokale_zahl_wirkt_wie_das_thermostat_und_gleicht_die_quelle_an(tmp_path):
    a = Aufbau(tmp_path)
    a.post(22.0, SOURCE_LOKAL, herkunft="ha", durch="nutzer")
    assert a.store.uebersteuerung() == {"temp": 22.0, "bis": BIS}
    assert a.gesendet == [(22.0, BIS, "ha", "nutzer")] and a.wuensche == []
    assert a.quelle.geschrieben == [22.0] and a.store.value == 21.0


def test_in_der_uebersteuerung_gilt_die_naechste_aenderung_wieder_bis_zum_gleichen_ende(tmp_path):
    a = Aufbau(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.lage = SollErgebnis(soll=19.0, soll_art=SOLL_ART_UEBERSTEUERT, naechster_wechsel=AUFHEIZBEGINN)
    a.post(20.5, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(21.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # zurueck auf den Wunsch ist auch eine Eingabe
    assert [g[0] for g in a.gesendet] == [19.0, 20.5, 21.0]
    assert a.store.uebersteuerung() == {"temp": 21.0, "bis": BIS}
    assert a.wuensche == []


def test_dieselbe_meldung_noch_einmal_ist_keine_neue_uebersteuerung(tmp_path):
    a = Aufbau(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert len(a.gesendet) == 1


@pytest.mark.parametrize("art", [SOLL_ART_WUNSCH, SOLL_ART_AUFHEIZEN], ids=["warm", "aufheizen"])
def test_warm_aenderung_ist_ein_neuer_wunsch(tmp_path, art):
    a = Aufbau(tmp_path, art=art, wechsel=None)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    assert a.wuensche == [(22.0, "soll_entity", "nutzer")] and a.store.value == 22.0
    assert a.store.uebersteuerung() is None and a.gesendet == []


@pytest.mark.parametrize("lage", [None, SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None)],
                         ids=["ohne_plan", "ohne_soll"])
def test_ohne_plan_ist_eine_aenderung_ein_neuer_wunsch(tmp_path, lage):
    a = Aufbau(tmp_path)
    a.lage = lage
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [(22.0, "soll_entity", None)] and a.store.uebersteuerung() is None


def test_kuehl_ohne_naechsten_wechsel_gibt_es_keine_uebersteuerung(tmp_path):
    a = Aufbau(tmp_path, wechsel=None)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [(22.0, "soll_entity", None)] and a.store.uebersteuerung() is None


def test_notbetrieb_ohne_server_bleibt_die_uebersteuerung_lokal(tmp_path):
    """Ohne Server (Notbetrieb) wirkt die Lage darunter weiter: die Uebersteuerung gilt lokal, auch wenn der Versand
    scheitert."""
    a = Aufbau(tmp_path, fehler=ValueError("kein Vertragswert"))
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS} and a.gesendet == []
    assert a.checks and a.checks[-1]["room_target_fired"] is True


def test_ohne_faehigkeit_programm_verhaelt_sich_alles_wie_heute(tmp_path):
    a = Aufbau(tmp_path, faehig=False)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(23.0, SOURCE_LOKAL, herkunft="ha")
    assert a.wuensche == [(22.0, "soll_entity", None), (23.0, "ha", None)]
    assert a.store.uebersteuerung() is None and a.gesendet == [] and a.store.value == 23.0


def test_ohne_weg_verhaelt_sich_alles_wie_heute(tmp_path):
    a = Aufbau(tmp_path, weg=False)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [(22.0, "soll_entity", None)] and a.store.uebersteuerung() is None


def test_das_portal_aendert_nur_den_wunsch_und_beendet_die_lokale_uebersteuerung(tmp_path):
    """Das Portal in der kuehlen Phase setzt die Uebersteuerung am Server; sie kommt gespiegelt im Dokument an
    (plan_quelle). Eine neue Bedienung des Servers beendet die lokale."""
    a = Aufbau(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(22.0, SOURCE_PORTAL)
    assert a.store.uebersteuerung() is None and a.store.value == 22.0 and a.wuensche == []


def test_echo_eigener_schreibvorgaenge_ist_keine_uebersteuerung(tmp_path):
    a = Aufbau(tmp_path)
    a.post(21.5, SOURCE_PORTAL)  # Bedienung des Servers: geht an die Quelle
    assert a.quelle.geschrieben == [21.5]
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # im Echo-Fenster
    a.clock.advance(120)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # ganzzahliges Thermostat: gerundeter Schreibwert
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() is None and a.gesendet == [] and a.wuensche == []
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # eine echte Drehung danach gilt
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}


def test_uebersteuerung_haelt_die_grenzen_der_sicherheit_ein_und_schreibt_den_begrenzten_wert_zurueck(tmp_path):
    a = Aufbau(tmp_path)
    a.post(safety.ABSENK_UNTERGRENZE_C - 2, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung()["temp"] == safety.ABSENK_UNTERGRENZE_C
    assert a.quelle.geschrieben == [safety.ABSENK_UNTERGRENZE_C]
    assert a.gesendet[-1][0] == safety.ABSENK_UNTERGRENZE_C
    a.clock.advance(120)
    a.post(safety.BEDIENBEREICH_MAX_C + 3, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung()["temp"] == safety.BEDIENBEREICH_MAX_C
    assert a.quelle.geschrieben[-1] == safety.BEDIENBEREICH_MAX_C


def test_thermostat_aus_verwirft_eine_offene_uebersteuerung(tmp_path):
    a = Aufbau(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.worker.post(Event(EV_SOLL_MODUS, {"mode": "off"}))
    a.worker.run_pending()
    a.post(23.0, SOURCE_LOKAL, herkunft="ha")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}  # abgeschlossen vor dem Ausschalten: bleibt
    assert len(a.gesendet) == 1


def test_im_log_stehen_keine_zeiten(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    a = Aufbau(tmp_path, fehler=ValueError("kein Vertragswert"))
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert "2026" not in caplog.text and "21:30" not in caplog.text and BIS not in caplog.text


# --- Verwerfen einer offenen Uebersteuerung (Soll-Quelle geht aus) ---

def test_verwerfen_stellt_die_uebersteuerung_von_vor_der_eingabe_wieder_her(tmp_path):
    """Galt schon eine Uebersteuerung (etwa aus der Datei nach dem Neustart), gilt nach dem Verwerfen wieder sie."""
    a = Aufbau(tmp_path, entprellen_s=10.0)
    a.store.uebersteuerung_setzen(19.0, VORHER)
    a.post(20.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() == {"temp": 20.0, "bis": BIS}
    a.modus("off")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": VORHER}


def test_verwerfen_nach_zwei_eingaben_stellt_die_uebersteuerung_vor_der_ersten_wieder_her(tmp_path):
    a = Aufbau(tmp_path, entprellen_s=10.0)
    a.store.uebersteuerung_setzen(19.0, VORHER)
    a.post(20.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() == {"temp": 22.0, "bis": BIS}
    a.modus("off")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": VORHER}


def test_verwerfen_ohne_vorherige_uebersteuerung_hinterlaesst_keine(tmp_path):
    a = Aufbau(tmp_path, entprellen_s=10.0)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}
    a.modus("off")
    assert a.store.uebersteuerung() is None


def test_nach_dem_verwerfen_ist_nichts_mehr_offen(tmp_path):
    a = Aufbau(tmp_path, entprellen_s=10.0)
    a.store.uebersteuerung_setzen(19.0, VORHER)
    a.post(20.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.modus("off")
    a.entprellen()
    assert a.gesendet == [] and a.quelle.geschrieben == []
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": VORHER}


# --- Bedienung des Servers schliesst die offene Uebersteuerung ---

def test_das_portal_schliesst_eine_offene_uebersteuerung(tmp_path):
    a = Aufbau(tmp_path, entprellen_s=10.0)
    a.store.uebersteuerung_setzen(19.0, VORHER)
    a.post(20.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(22.0, SOURCE_PORTAL)
    assert a.store.uebersteuerung() is None
    a.modus("off")  # Verwerfen stellt nichts mehr her
    assert a.store.uebersteuerung() is None and a.store.value == 22.0
    # eine spaeter gesetzte Uebersteuerung gehoert nicht zur geschlossenen Eingabe: die Entprellung sendet sie nicht
    a.store.uebersteuerung_setzen(18.0, VORHER)
    a.entprellen()
    assert a.gesendet == []


def test_nach_dem_portal_gilt_die_uebersteuerung_der_naechsten_eingabe_als_neue_eingabe(tmp_path):
    a = Aufbau(tmp_path, entprellen_s=10.0)
    a.post(20.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(22.0, SOURCE_PORTAL)
    a.store.uebersteuerung_setzen(18.0, VORHER)
    a.clock.advance(120)  # Echo-Fenster des Portal-Schreibens vorbei
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.modus("off")  # verwerfen: die Uebersteuerung von vor dieser Eingabe
    assert a.store.uebersteuerung() == {"temp": 18.0, "bis": VORHER}


# --- Was in der kuehlen Phase keine Eingabe ist ---

def test_echo_am_thermostat_in_der_kuehlen_phase_ist_keine_eingabe(tmp_path):
    a = Aufbau(tmp_path)
    a.post(21.5, SOURCE_PORTAL)  # eigenes Schreiben: Echo-Fenster
    a.checks.clear()
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() is None and a.gesendet == [] and a.checks == []
    assert a.quelle.geschrieben == [21.5]


def test_wiederholung_der_angezeigten_uebersteuerung_am_thermostat_ist_keine_eingabe(tmp_path):
    a = Aufbau(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert len(a.checks) == 1
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert len(a.gesendet) == 1 and len(a.checks) == 1


def test_unplausibler_lokaler_wert_in_der_kuehlen_phase_ist_keine_eingabe(tmp_path):
    a = Aufbau(tmp_path)
    a.post(50.0, SOURCE_LOKAL, herkunft="ha")
    assert a.store.uebersteuerung() is None and a.gesendet == [] and a.quelle.geschrieben == []
    assert a.checks == [] and a.wuensche == []


def test_dieselbe_lokale_zahl_mit_gleichem_ende_ist_keine_eingabe(tmp_path):
    a = Aufbau(tmp_path)
    a.store.uebersteuerung_setzen(19.0, BIS)
    a.post(19.0, SOURCE_LOKAL, herkunft="ha")
    assert a.gesendet == [] and a.quelle.geschrieben == [] and a.checks == []
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}


def test_dieselbe_lokale_zahl_mit_anderem_ende_ist_eine_eingabe(tmp_path):
    a = Aufbau(tmp_path)
    a.store.uebersteuerung_setzen(19.0, VORHER)
    a.post(19.0, SOURCE_LOKAL, herkunft="ha")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}
    assert a.gesendet == [(19.0, BIS, "ha", None)] and a.checks


def test_andere_lokale_zahl_mit_gleichem_ende_ist_eine_eingabe(tmp_path):
    a = Aufbau(tmp_path)
    a.store.uebersteuerung_setzen(19.0, BIS)
    a.post(20.0, SOURCE_LOKAL, herkunft="ha")
    assert a.store.uebersteuerung() == {"temp": 20.0, "bis": BIS}
    assert a.gesendet == [(20.0, BIS, "ha", None)] and a.checks


def test_ohne_eingabe_sendet_die_entprellung_keine_gespeicherte_uebersteuerung(tmp_path):
    """Eine aus der Datei geladene Uebersteuerung geht nur mit einer Eingabe an den Server, nicht mit einem Wunsch."""
    a = Aufbau(tmp_path, art=SOLL_ART_WUNSCH, wechsel=None)
    a.store.uebersteuerung_setzen(19.0, BIS)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [(22.0, "soll_entity", None)]
    assert a.gesendet == []


def test_die_quelle_wird_nur_nach_einer_lokalen_eingabe_angeglichen(tmp_path):
    a = Aufbau(tmp_path)
    a.post(22.0, SOURCE_LOKAL, herkunft="ha")
    assert a.quelle.geschrieben == [22.0]
    a.clock.advance(120)  # Echo-Fenster vorbei
    a.post(20.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # am Thermostat gedreht: es zeigt den Wert schon
    assert a.store.uebersteuerung()["temp"] == 20.0 and a.quelle.geschrieben == [22.0]


# Leerstand und Away am Thermostat (Spec Betriebsarten 6 und 7)

WARM_DANACH = SollErgebnis(soll=21.0, soll_art=SOLL_ART_WUNSCH, naechster_wechsel=None)
KUEHL_DANACH = SollErgebnis(soll=18.0, soll_art=SOLL_ART_ABSENKUNG, naechster_wechsel=AUFHEIZBEGINN)
LEERSTAND_LAGE = SollErgebnis(soll=16.0, soll_art="leerstand", naechster_wechsel=None)
KNAPP = timedelta(seconds=1)


def _zeit(wert: datetime) -> str:
    return wert.strftime("%Y-%m-%dT%H:%M:%SZ")


def _leerstand(tmp_path, *, danach=WARM_DANACH, **extra) -> Aufbau:
    a = Aufbau(tmp_path, **extra)
    a.lage = LEERSTAND_LAGE
    a.betriebsart = Betriebsart("leerstand", danach)
    return a


def _aendern(a: Aufbau, wert: float, abstand: timedelta = timedelta(minutes=10)) -> None:
    a.weiter(abstand)
    a.clock.advance(120)  # Echo-Fenster vorbei: eine echte Drehung
    a.post(wert, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")


def test_leerstand_erste_aenderung_ist_eine_uebersteuerung_von_vier_stunden(tmp_path):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    bis = _zeit(a.jetzt + UEBERSTEUERUNG_DAUER)
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": bis}
    assert a.gesendet == [(19.0, bis, "soll_entity", "nutzer")]
    assert a.beendet == [] and a.wuensche == [] and a.store.leerstand_zaehler.anzahl() == 1


def test_leerstand_zweite_aenderung_ist_wieder_eine_uebersteuerung(tmp_path):
    a = _leerstand(tmp_path)
    _aendern(a, 19.0)
    _aendern(a, 20.0)
    assert a.store.uebersteuerung() == {"temp": 20.0, "bis": _zeit(a.jetzt + UEBERSTEUERUNG_DAUER)}
    assert len(a.gesendet) == 2 and a.beendet == [] and a.store.leerstand_zaehler.anzahl() == 2


def test_leerstand_dritte_aenderung_beendet_ihn_und_der_wert_gilt_als_wunsch_in_der_warmen_phase(tmp_path):
    a = _leerstand(tmp_path)
    for wert in (19.0, 20.0, 22.0):
        _aendern(a, wert)
    assert a.beendet == [("leerstand", 22.0, None, "soll_entity", "nutzer")]
    assert len(a.gesendet) == 2  # nur die ersten beiden waren Uebersteuerungen
    assert a.store.value == 22.0 and a.store.uebersteuerung() is None
    assert a.store.leerstand_zaehler.anzahl() == 0  # das Ende beginnt ein neues Fenster


def test_leerstand_dritte_aenderung_in_der_kuehlen_phase_wird_eine_uebersteuerung_bis_zum_aufheizbeginn(tmp_path):
    a = _leerstand(tmp_path, danach=KUEHL_DANACH)
    for wert in (19.0, 20.0, 19.5):
        _aendern(a, wert)
    assert a.beendet == [("leerstand", None, {"temp": 19.5, "bis": BIS}, "soll_entity", "nutzer")]
    assert a.store.uebersteuerung() == {"temp": 19.5, "bis": BIS} and a.store.value == 21.0


@pytest.mark.parametrize("abstand,beendet", [(FENSTER - KNAPP, True), (FENSTER, False), (FENSTER + KNAPP, False)],
                         ids=["knapp_davor", "genau_am_ende", "knapp_danach"])
def test_leerstand_die_erste_aenderung_zaehlt_bis_zum_ende_des_fensters(tmp_path, abstand, beendet):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    _aendern(a, 20.0, timedelta(hours=1))
    _aendern(a, 22.0, abstand - timedelta(hours=1))
    assert bool(a.beendet) is beendet


@pytest.mark.parametrize("nach,noch_da", [(UEBERSTEUERUNG_DAUER - KNAPP, True), (UEBERSTEUERUNG_DAUER, False)],
                         ids=["knapp_davor", "am_ende"])
def test_leerstand_uebersteuerung_gilt_vier_stunden_nicht_laenger(tmp_path, nach, noch_da):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.weiter(nach)
    assert (a.store.uebersteuerung() is not None) is noch_da


def test_leerstand_ein_eigener_schreibvorgang_und_sein_echo_zaehlen_nicht(tmp_path):
    a = _leerstand(tmp_path)
    a.store.note_own_write(geschrieben=16.0)  # die Regelung hat die Leerstandstemperatur geschrieben
    a.post(16.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # ihr Echo
    assert a.store.leerstand_zaehler.anzahl() == 0 and a.gesendet == [] and a.beendet == []


def test_leerstand_dieselbe_anzeige_ein_zweites_mal_ist_keine_neue_aenderung(tmp_path):
    a = _leerstand(tmp_path)
    _aendern(a, 19.0)
    _aendern(a, 19.0)  # das Thermostat meldet denselben Wert erneut
    assert a.store.leerstand_zaehler.anzahl() == 1 and len(a.gesendet) == 1


def test_leerstand_eine_drehung_ueber_mehrere_meldungen_zaehlt_einmal(tmp_path):
    a = _leerstand(tmp_path, entprellen_s=10.0)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.post(19.5, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.leerstand_zaehler.anzahl() == 1 and a.gesendet == []
    a.entprellen()
    assert [g[0] for g in a.gesendet] == [19.5] and a.store.leerstand_zaehler.anzahl() == 1


def test_leerstand_das_fenster_ueberlebt_einen_neustart(tmp_path):
    a = _leerstand(tmp_path)
    _aendern(a, 19.0)
    _aendern(a, 20.0)
    b = Aufbau(tmp_path)  # derselbe Speicherort, neuer Eingang
    b.jetzt = a.jetzt
    b.lage, b.betriebsart = LEERSTAND_LAGE, Betriebsart("leerstand", WARM_DANACH)
    _aendern(b, 22.0)
    assert b.beendet == [("leerstand", 22.0, None, "soll_entity", "nutzer")]


def test_leerstand_ohne_zaehlbare_aenderung_der_lokalen_zahl_gilt_der_wunsch_fuer_danach(tmp_path):
    a = _leerstand(tmp_path)
    a.post(22.0, SOURCE_LOKAL, herkunft="ha", durch="nutzer")
    assert a.wuensche == [(22.0, "ha", "nutzer")]
    assert a.store.leerstand_zaehler.anzahl() == 0 and a.beendet == [] and a.store.uebersteuerung() is None


def test_away_jede_aenderung_am_thermostat_beendet_es(tmp_path):
    a = Aufbau(tmp_path)
    a.lage = SollErgebnis(soll=17.0, soll_art="abwesend", naechster_wechsel=None)
    a.betriebsart = Betriebsart("abwesend", WARM_DANACH)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    assert a.beendet == [("abwesend", 22.0, None, "soll_entity", "nutzer")]
    assert a.store.value == 22.0 and a.store.leerstand_zaehler.anzahl() == 0
    assert a.gesendet == [] and a.wuensche == []


def test_away_in_der_kuehlen_phase_danach_wird_die_aenderung_eine_uebersteuerung(tmp_path):
    a = Aufbau(tmp_path)
    a.lage = SollErgebnis(soll=17.0, soll_art="abwesend", naechster_wechsel=None)
    a.betriebsart = Betriebsart("abwesend", KUEHL_DANACH)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.beendet == [("abwesend", None, {"temp": 19.0, "bis": BIS}, "soll_entity", None)]
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}


def test_away_die_lokale_zahl_beendet_es_nicht(tmp_path):
    a = Aufbau(tmp_path)
    a.lage = SollErgebnis(soll=17.0, soll_art="abwesend", naechster_wechsel=None)
    a.betriebsart = Betriebsart("abwesend", WARM_DANACH)
    a.post(22.0, SOURCE_LOKAL, herkunft="ha")
    assert a.beendet == [] and a.wuensche == [(22.0, "ha", None)]


@pytest.mark.parametrize("art", ["leerstand", "abwesend"])
def test_ohne_faehigkeit_programm_bleibt_es_ein_wunsch(tmp_path, art):
    a = Aufbau(tmp_path, faehig=False)
    a.lage = LEERSTAND_LAGE
    a.betriebsart = Betriebsart(art, WARM_DANACH)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [(22.0, "soll_entity", None)] and a.beendet == [] and a.gesendet == []
    assert a.store.leerstand_zaehler.anzahl() == 0


def test_ohne_leerstand_und_ohne_away_bleibt_das_verhalten_wie_bisher(tmp_path):
    a = Aufbau(tmp_path, art=SOLL_ART_WUNSCH, wechsel=None)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [(22.0, "soll_entity", None)] and a.beendet == [] and a.store.leerstand_zaehler.anzahl() == 0
    kuehl = Aufbau(tmp_path / "kuehl")
    kuehl.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert kuehl.gesendet == [(19.0, BIS, "soll_entity", None)] and kuehl.beendet == []


def test_ohne_weg_fuer_die_betriebsart_bleibt_es_ein_wunsch(tmp_path):
    a = Aufbau(tmp_path, weg=False)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [(22.0, "soll_entity", None)]


# Das Ende bei offener Drehung, Schreiben an die Quelle und Absicherung des Sendens

UEBERSTEUERT_LAGE = SollErgebnis(soll=19.0, soll_art=SOLL_ART_UEBERSTEUERT, naechster_wechsel=AUFHEIZBEGINN)


def _gesendet_und_entprellt(a: Aufbau, werte: tuple[float, ...]) -> None:
    for wert in werte:
        _aendern(a, wert)
        a.entprellen()


@pytest.mark.parametrize("lage", [LEERSTAND_LAGE, UEBERSTEUERT_LAGE], ids=["leerstand", "uebersteuert"])
def test_leerstand_ende_nimmt_den_letzten_wert_der_drehung_und_zaehlt_sie_einmal(tmp_path, lage):
    a = _leerstand(tmp_path, entprellen_s=10.0)
    a.lage = lage  # mit Uebersteuerung gilt die kuehle Phase, die Eingabe waere sonst eine weitere Uebersteuerung
    _gesendet_und_entprellt(a, (19.0, 20.0))
    _aendern(a, 22.0)
    a.post(22.5, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    a.post(21.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")  # das angezeigte Soll: keine Eingabe
    assert a.store.leerstand_zaehler.anzahl() == 3
    a.entprellen()
    assert a.beendet == [("leerstand", 22.5, None, "soll_entity", "nutzer")]
    assert len(a.gesendet) == 2 and a.store.value == 22.5


def test_leerstand_ende_bei_offenem_wunsch_laeuft_den_gewohnten_weg(tmp_path):
    a = Aufbau(tmp_path, art=SOLL_ART_WUNSCH, wechsel=None, entprellen_s=10.0)
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")  # Wunsch offen, noch kein Leerstand
    a.lage, a.betriebsart = LEERSTAND_LAGE, Betriebsart("leerstand", WARM_DANACH)
    a.post(23.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    a.entprellen()
    assert a.wuensche == [(23.0, "soll_entity", "nutzer")] and a.beendet == []
    assert a.store.leerstand_zaehler.anzahl() == 0


def test_leerstand_bestaetigung_eines_eigenen_schreibvorgangs_ist_keine_eingabe_auch_mit_uebersteuerung(tmp_path):
    a = _leerstand(tmp_path)
    a.lage = UEBERSTEUERT_LAGE
    a.store.uebersteuerung_setzen(19.0, BIS)
    a.store.note_own_write(geschrieben=22.0)
    a.clock.advance(120)  # Echo-Fenster vorbei, der Schreibbefehl noch offen
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    assert a.gesendet == [] and a.beendet == [] and a.store.uebersteuerung() == {"temp": 19.0, "bis": BIS}
    assert a.store.leerstand_zaehler.anzahl() == 0


def test_leerstand_uebersteuerung_im_bereich_wird_nicht_an_die_quelle_zurueckgeschrieben(tmp_path):
    a = _leerstand(tmp_path)
    _aendern(a, 19.0)
    assert a.quelle.geschrieben == []


def test_leerstand_ende_in_der_kuehlen_phase_im_bereich_schreibt_nichts_zurueck(tmp_path):
    a = _leerstand(tmp_path, danach=KUEHL_DANACH)
    for wert in (19.0, 20.0, 19.5):
        _aendern(a, wert)
    assert a.quelle.geschrieben == []


def test_leerstand_ende_in_der_kuehlen_phase_unter_der_untergrenze_schreibt_den_begrenzten_wert_zurueck(tmp_path):
    a = _leerstand(tmp_path, danach=KUEHL_DANACH)
    for wert in (19.0, 20.0, safety.ABSENK_UNTERGRENZE_C - 1.0):
        _aendern(a, wert)
    untergrenze = safety.ABSENK_UNTERGRENZE_C
    assert a.beendet == [("leerstand", None, {"temp": untergrenze, "bis": BIS}, "soll_entity", "nutzer")]
    assert a.store.uebersteuerung() == {"temp": untergrenze, "bis": BIS}
    assert a.quelle.geschrieben == [untergrenze]


def test_leerstand_ende_in_der_warmen_phase_im_bereich_schreibt_nichts_zurueck(tmp_path):
    a = _leerstand(tmp_path)
    for wert in (19.0, 20.0, 22.0):
        _aendern(a, wert)
    assert a.quelle.geschrieben == [] and a.store.value == 22.0


def test_leerstand_ende_in_der_warmen_phase_ueber_der_begrenzung_schreibt_den_begrenzten_wert_zurueck(tmp_path):
    a = _leerstand(tmp_path)
    for wert in (19.0, 20.0, 27.0):
        _aendern(a, wert)
    assert a.beendet == [("leerstand", 27.0, None, "soll_entity", "nutzer")]
    assert a.store.value == 25.0 and a.quelle.geschrieben == [25.0]


def test_leerstand_ende_ohne_begrenzten_wunsch_vom_geraetekern_laesst_den_wert_stehen(tmp_path):
    a = _leerstand(tmp_path)
    a.beenden_ohne_wert = True
    for wert in (19.0, 20.0, 27.0):
        _aendern(a, wert)
    assert a.store.value == 27.0 and a.quelle.geschrieben == []


@pytest.mark.parametrize("wert,danach", [(27.0, WARM_DANACH), (15.0, KUEHL_DANACH)])
def test_leerstand_thermostat_aus_verwirft_das_offene_ende_und_schreibt_nichts(tmp_path, wert, danach):
    a = _leerstand(tmp_path, danach=danach, entprellen_s=10.0)
    _gesendet_und_entprellt(a, (19.0, 20.0))
    _aendern(a, wert)
    a.modus("off")
    a.entprellen()
    assert a.beendet == [] and a.quelle.geschrieben == [] and a.store.value == 21.0


def test_beenden_senden_ohne_offenes_ende_tut_nichts(tmp_path):
    a = _leerstand(tmp_path)
    a.eingang._beenden_senden()  # kein Ende vorgemerkt
    ohne_weg = Aufbau(tmp_path / "ohne", weg=False)
    ohne_weg.eingang._beenden_senden()
    angefangen = Aufbau(tmp_path / "wert")
    angefangen.eingang._beenden_offen = Betriebsart("leerstand", WARM_DANACH)  # vorgemerkt ohne Wert
    angefangen.eingang._beenden_senden()
    assert a.beendet == [] and ohne_weg.beendet == [] and angefangen.beendet == []
    assert angefangen.store.value == 21.0 and angefangen.eingang._beenden_offen is None


# Anzeige des Thermostats nach Ablauf und Neustart, Ende des Leerstands, obere Klemmung

def _ablaufen(a: Aufbau, dauer: timedelta = UEBERSTEUERUNG_DAUER + 5 * KNAPP) -> None:
    """Die Uebersteuerung laeuft ab: Wanduhr des Speichers und Uhr des Workers laufen um `dauer` weiter."""
    a.weiter(dauer)
    a.clock.advance(dauer.total_seconds())
    a.worker.run_pending()


def test_leerstand_nach_ablauf_zaehlt_die_wiederholte_anzeige_nicht_als_aenderung(tmp_path):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.weiter(UEBERSTEUERUNG_DAUER + KNAPP)  # abgelaufen, das Thermostat zeigt weiter 19
    assert a.store.uebersteuerung() is None
    a.clock.advance(120)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.leerstand_zaehler.anzahl() == 1 and len(a.gesendet) == 1 and a.store.uebersteuerung() is None


def test_leerstand_nach_ablauf_schreibt_die_leerstandstemperatur_an_die_quelle_zurueck(tmp_path):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.quelle.geschrieben == []
    _ablaufen(a)
    assert a.quelle.geschrieben == [LEERSTAND_LAGE.soll]


def test_leerstand_das_echo_des_zurueckschreibens_und_die_alte_anzeige_zaehlen_nicht(tmp_path):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    _ablaufen(a)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # verspaetete Meldung des alten Werts im Echo-Fenster
    a.post(LEERSTAND_LAGE.soll, SOURCE_THERMOSTAT, herkunft="soll_entity")  # Echo des Zurueckschreibens
    a.clock.advance(120)
    a.post(LEERSTAND_LAGE.soll, SOURCE_THERMOSTAT, herkunft="soll_entity")  # Wiederholung nach dem Echo-Fenster
    assert a.store.leerstand_zaehler.anzahl() == 1 and len(a.gesendet) == 1 and a.beendet == []


def test_leerstand_zurueckschreiben_nur_im_leerstand_und_nicht_bei_laufender_uebersteuerung(tmp_path):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    a.lage = SollErgebnis(soll=21.0, soll_art=SOLL_ART_WUNSCH, naechster_wechsel=None)  # Leerstand inzwischen beendet
    _ablaufen(a)
    assert a.quelle.geschrieben == []
    (tmp_path / "zweite").mkdir()
    b = _leerstand(tmp_path / "zweite")
    b.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    _aendern(b, 20.0, timedelta(hours=3))  # neue Uebersteuerung: die erste ist nicht das Ende
    _ablaufen(b, timedelta(hours=1, seconds=2))
    assert b.quelle.geschrieben == [] and b.store.uebersteuerung() is not None
    _ablaufen(b, timedelta(hours=3))
    assert b.quelle.geschrieben == [LEERSTAND_LAGE.soll]


def test_leerstand_nach_neustart_nach_ablauf_zaehlt_die_wiederholte_anzeige_nicht(tmp_path):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    b = Aufbau(tmp_path)  # Neustart nach Ablauf: nichts ist mehr gesetzt, das Thermostat zeigt 19
    b.jetzt = a.jetzt + UEBERSTEUERUNG_DAUER + KNAPP
    b.lage, b.betriebsart = LEERSTAND_LAGE, Betriebsart("leerstand", WARM_DANACH)
    b.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert b.store.uebersteuerung() is None and b.gesendet == [] and b.store.leerstand_zaehler.anzahl() == 1


def test_leerstand_nach_neustart_mit_laufender_uebersteuerung_wird_sie_spaeter_zurueckgeschrieben(tmp_path):
    a = _leerstand(tmp_path)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    b = Aufbau(tmp_path)
    b.jetzt = a.jetzt + timedelta(hours=1)
    b.lage, b.betriebsart = LEERSTAND_LAGE, Betriebsart("leerstand", WARM_DANACH)
    b.worker.run_pending()
    assert b.quelle.geschrieben == []
    _ablaufen(b)
    assert b.quelle.geschrieben == [LEERSTAND_LAGE.soll]


def test_leerstand_uebersteuerung_endet_mit_dem_leerstand(tmp_path):
    a = _leerstand(tmp_path)
    a.betriebsart = Betriebsart("leerstand", WARM_DANACH, bis=a.jetzt + timedelta(hours=1))
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": _zeit(a.jetzt + timedelta(hours=1))}


@pytest.mark.parametrize("rest,erwartet", [(UEBERSTEUERUNG_DAUER - KNAPP, UEBERSTEUERUNG_DAUER - KNAPP),
                                           (UEBERSTEUERUNG_DAUER, UEBERSTEUERUNG_DAUER),
                                           (UEBERSTEUERUNG_DAUER + KNAPP, UEBERSTEUERUNG_DAUER)],
                         ids=["knapp_davor", "genau_vier_stunden", "knapp_danach"])
def test_leerstand_uebersteuerung_haelt_vier_stunden_oder_bis_zum_ende_des_leerstands(tmp_path, rest, erwartet):
    a = _leerstand(tmp_path)
    a.betriebsart = Betriebsart("leerstand", WARM_DANACH, bis=a.jetzt + rest)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() == {"temp": 19.0, "bis": _zeit(a.jetzt + erwartet)}
    assert a.gesendet[0][1] == _zeit(a.jetzt + erwartet)


def test_leerstand_ende_in_der_kuehlen_phase_ueber_dem_maximum_wird_begrenzt(tmp_path):
    a = _leerstand(tmp_path, danach=KUEHL_DANACH)
    for wert in (19.0, 20.0, 27.0):
        _aendern(a, wert)
    oben = safety.BEDIENBEREICH_MAX_C
    assert a.beendet == [("leerstand", None, {"temp": oben, "bis": BIS}, "soll_entity", "nutzer")]
    assert a.store.uebersteuerung() == {"temp": oben, "bis": BIS} and a.quelle.geschrieben == [oben]
