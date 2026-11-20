"""Soll-Eingang in der kuehlen Phase: eine Aenderung am Thermostat oder an der lokalen Zahl ist dort eine Uebersteuerung
bis zum Aufheizbeginn, sonst ein Wunsch (Spec Betriebsarten 7)."""
import logging
from datetime import UTC, datetime
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
from smartheat_runtime.runtime import EV_LOCAL_CHECK
from smartheat_runtime.soll_eingang import (
    EV_SOLL_MODUS,
    EV_TARGET_INPUT,
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
                 fehler=None):
        self.clock = FakeClock()
        self.worker = RegulationWorker(self.clock)
        self.store = TargetStore(tmp_path / "soll.json", 21.0, self.clock, lambda: "2026-01-15T20:00:00+00:00")
        self.quelle = Quelle()
        self.wuensche, self.gesendet, self.checks = [], [], []
        self.lage = SollErgebnis(soll=18.0, soll_art=art, naechster_wechsel=wechsel)
        self.faehig, self.fehler = faehig, fehler
        self.worker.register(EV_LOCAL_CHECK, lambda event: self.checks.append(event.data))
        self.eingang = SollEingang(
            self.worker, self.store, self.quelle, wunsch=self._wunsch, soll_quelle_aus=lambda aus, wirksam: None,
            entprellen_s=0.0,
            uebersteuerung=UebersteuerungsWeg(lage=lambda: self.lage, senden=self._senden,
                                              moeglich=lambda: self.faehig) if weg else None)

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
