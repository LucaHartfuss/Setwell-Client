"""Soll-Eingang: Verhalten an den Stellen, die die uebrigen Tests nicht festhalten (Wartezeiten des Anzeige-Abgleichs,
Rueckgabewerte, Ketten der Planung, Nachschreiben nach dem Ende der Regelung). Alles ueber die Schnittstelle des
Soll-Eingangs; die Planung des Workers wird ueber `schedule` mitgeschrieben."""
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeClock
from test_soll_eingang_uebersteuerung import Aufbau, _aendern, _gesendet_und_entprellt, _leerstand

from smartheat_runtime.soll_eingang import (
    EV_ABGLEICH,
    EV_SOLL_ENDE,
    EV_SOLL_ENDE_ERLEDIGT,
    EV_UEB_ABLAUF,
    SollEingang,
    quelle_freigegeben,
)
from smartheat_runtime.soll_speicher import SOURCE_PORTAL, SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

PAUSE = 1801  # laenger als die Frist eines unbestaetigten Schreibbefehls


def _aufzeichnen(worker) -> list[tuple[float, str]]:
    """Schreibt jede Planung (Wartezeit, Art) des Workers mit."""
    plan: list[tuple[float, str]] = []
    echt = worker.schedule

    def schedule(delay_s, event):
        plan.append((delay_s, event.kind))
        return echt(delay_s, event)

    worker.schedule = schedule
    return plan


def _arten(plan, art) -> list[float]:
    return [delay for delay, kind in plan if kind == art]


def _einfach(tmp_path, **extra) -> Aufbau:
    """Aufbau ohne Uebersteuerungs-Verdrahtung: Wunsch 21, das Thermostat zeigt 21."""
    return Aufbau(tmp_path, weg=False, **extra)


def _verloren(a: Aufbau, anzeige: float) -> None:
    """Das Thermostat nimmt nichts an: es zeigt weiter `anzeige`, jede Schreibung geht verloren."""
    a.quelle.wert = anzeige

    def schreiben(wert):
        a.quelle.geschrieben.append(wert)
        return True

    a.quelle.schreiben = schreiben


# --- Wartezeiten bei offener Eingabe -----------------------------------------------------------------------------


def _offen_pruefen(a: Aufbau, plan) -> None:
    """Der Abgleich schreibt bei offener Eingabe nichts, meldet False und prueft nach Entprellzeit plus einer Sekunde."""
    vorher = list(a.quelle.geschrieben)
    assert a.eingang.anzeige_abgleichen("test") is False
    assert a.quelle.geschrieben == vorher
    assert _arten(plan, EV_ABGLEICH) == [11.0]


def test_offener_wunsch_haelt_den_abgleich_zurueck(tmp_path):
    a = _einfach(tmp_path, entprellen_s=10.0)
    plan = _aufzeichnen(a.worker)
    a.post(23.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    _offen_pruefen(a, plan)


def test_offene_uebersteuerung_haelt_den_abgleich_zurueck(tmp_path):
    a = Aufbau(tmp_path, entprellen_s=10.0)
    plan = _aufzeichnen(a.worker)
    a.post(19.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.store.uebersteuerung() is not None
    _offen_pruefen(a, plan)


def test_offenes_ende_der_betriebsart_haelt_den_abgleich_zurueck(tmp_path):
    a = _leerstand(tmp_path, entprellen_s=10.0)
    _gesendet_und_entprellt(a, (19.0, 20.0))
    plan = _aufzeichnen(a.worker)
    _aendern(a, 22.0)  # die dritte Aenderung beendet den Leerstand, nach dem Entprellen
    assert a.beendet == []
    _offen_pruefen(a, plan)


# --- Wartezeit bei Echo-Fenster und unbestaetigter Schreibung ----------------------------------------------------


def test_nach_eigener_schreibung_wartet_der_abgleich_hoechstens_ein_echo_fenster_plus_eine_sekunde(tmp_path):
    a = _einfach(tmp_path)
    plan = _aufzeichnen(a.worker)
    a.post(22.0, SOURCE_PORTAL)
    assert a.quelle.geschrieben == [22.0]
    a.post(23.0, SOURCE_PORTAL)  # die unbestaetigte Schreibung steht noch fuer 30 Minuten offen
    a.post(24.0, SOURCE_PORTAL)  # eine Kette: keine zweite Planung
    assert a.quelle.geschrieben == [22.0] and _arten(plan, EV_ABGLEICH) == [61.0]
    a.clock.advance(61)
    a.worker.run_pending()  # die wartende Pruefung laeuft und plant sich neu ein, solange die Schreibung offen ist
    assert a.quelle.geschrieben == [22.0] and _arten(plan, EV_ABGLEICH) == [61.0, 61.0]


def test_kurzer_rest_der_schreibung_wartet_rest_plus_eine_sekunde(tmp_path):
    a = _einfach(tmp_path)
    plan = _aufzeichnen(a.worker)
    a.post(22.0, SOURCE_PORTAL)
    a.quelle.wert = 21.0  # das Thermostat zeigt weiter den alten Wert
    a.clock.advance(1799.5)  # die Frist der Schreibung endet in einer halben Sekunde
    a.post(23.0, SOURCE_PORTAL)
    assert a.quelle.geschrieben == [22.0] and _arten(plan, EV_ABGLEICH) == [1.5]
    assert a.eingang.anzeige_abgleichen("test") is False


def test_ein_zweiter_abgleich_nach_derselben_schreibung_schreibt_nicht_noch_einmal(tmp_path):
    a = _einfach(tmp_path)
    a.post(22.0, SOURCE_PORTAL)
    a.quelle.wert = 21.0  # die Quelle meldet nichts zurueck
    a.clock.advance(PAUSE)
    assert a.eingang.anzeige_abgleichen("test") is False
    assert a.quelle.geschrieben == [22.0]


# --- Rueckgabe False ---------------------------------------------------------------------------------------------


def test_abgleich_ohne_quelle_meldet_false(tmp_path):
    worker = RegulationWorker(FakeClock())
    store = TargetStore(tmp_path / "soll.json", 21.0, FakeClock(), lambda: "2026-01-15T20:00:00+00:00")
    eingang = SollEingang(worker, store, None, wunsch=lambda wert, herkunft, durch: wert,
                          soll_quelle_aus=lambda aus, wirksam: None, entprellen_s=0.0)
    assert eingang.anzeige_abgleichen("test") is False


def test_abgleich_nach_dem_ende_der_regelung_meldet_false(tmp_path):
    a = _einfach(tmp_path)
    a.quelle.wert = 18.0
    a.worker.regelung_beendet = lambda: True
    assert a.eingang.anzeige_abgleichen("test") is False and a.quelle.geschrieben == []


def test_abgleich_bei_soll_quelle_aus_meldet_false(tmp_path):
    a = _einfach(tmp_path)
    a.quelle.wert = 18.0
    a.modus("off")
    assert a.eingang.anzeige_abgleichen("test") is False and a.quelle.geschrieben == []


def test_abgleich_meldet_false_wenn_die_quelle_das_geltende_schon_zeigt(tmp_path):
    a = _einfach(tmp_path)
    assert a.eingang.anzeige_abgleichen("test") is False and a.quelle.geschrieben == []


def test_abgleich_greift_auf_die_gemerkte_anzeige_zurueck_wenn_die_quelle_keine_meldet(tmp_path):
    a = _einfach(tmp_path)
    a.quelle.angezeigt = lambda: None
    a.store.anzeige_merken(21.0)
    assert a.eingang.anzeige_abgleichen("test") is False and a.quelle.geschrieben == []
    b = _einfach(tmp_path / "b")
    b.quelle.angezeigt = lambda: None
    b.store.anzeige_merken(19.0)
    assert b.eingang.anzeige_abgleichen("test") is True and b.quelle.geschrieben == [21.0]


# --- Anzeige merken ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("merken", "quelle", "erwartet"), [
    (False, 21.0, None),
    (True, 21.0, 21.0),
    (True, 19.0, None),
    (False, 19.0, None),
], ids=["ohne_merken_gleich", "merken_gleich", "merken_abweichend", "ohne_merken_abweichend"])
def test_gleiche_anzeige_wird_nur_auf_verlangen_gemerkt(tmp_path, merken, quelle, erwartet):
    a = _einfach(tmp_path)
    a.quelle.wert = quelle
    a.quelle.schreiben = lambda wert: False  # die Quelle ist nicht schreibbar: gemerkt wird nur, was sie schon zeigt
    assert a.store.angezeigt_zuletzt is None
    a.eingang.anzeige_abgleichen("test", gleiche_merken=merken)
    assert a.store.angezeigt_zuletzt == erwartet


def test_wieder_an_merkt_die_anzeige_die_die_quelle_schon_zeigt(tmp_path):
    a = _einfach(tmp_path)
    a.modus("off")
    a.modus("heat")
    assert a.quelle.geschrieben == [] and a.store.angezeigt_zuletzt == 21.0


# --- Freigabe der Quelle -----------------------------------------------------------------------------------------


class _Quelle:
    def __init__(self, aus=False):
        self._aus = aus

    def aus(self):
        return self._aus


@pytest.mark.parametrize(("angezeigt", "aus", "frei"), [
    (7.0, False, False), (7.5, False, True), (None, False, True), (21.0, True, False),
])
def test_quelle_ist_ab_ueber_der_frostschutz_stellung_freigegeben(angezeigt, aus, frei):
    assert quelle_freigegeben(_Quelle(aus), angezeigt) is frei


# --- Ablauf der Uebersteuerung: eine Kette ------------------------------------------------------------------------


def _mini(tmp_path, bis_nach: timedelta):
    """Soll-Eingang, dessen Speicher beim Start schon eine Uebersteuerung bis `jetzt + bis_nach` kennt."""
    jetzt = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    uhr = FakeClock()
    worker = RegulationWorker(uhr)
    plan = _aufzeichnen(worker)
    store = TargetStore(tmp_path / "soll.json", 21.0, uhr, lambda: jetzt.isoformat())
    store.uebersteuerung_setzen(19.0, (jetzt + bis_nach).isoformat())
    quelle = Aufbau(tmp_path / "x").quelle
    SollEingang(worker, store, quelle, wunsch=lambda wert, herkunft, durch: wert,
                soll_quelle_aus=lambda aus, wirksam: None, entprellen_s=0.0)
    return plan


@pytest.mark.parametrize(("rest", "erwartet"), [(timedelta(minutes=10), 601.0), (timedelta(milliseconds=200), 1.2)],
                         ids=["zehn_minuten", "knapp_vor_dem_ende"])
def test_das_ende_einer_uebersteuerung_wird_eine_sekunde_nach_ihrem_ende_eingeplant(tmp_path, rest, erwartet):
    plan = _mini(tmp_path, rest)
    assert _arten(plan, EV_UEB_ABLAUF) == [pytest.approx(erwartet)]


def test_weitere_uebersteuerungen_haengen_keine_zweite_kette_ein_und_die_kette_reisst_nicht_ab(tmp_path):
    a = _leerstand(tmp_path)
    plan = _aufzeichnen(a.worker)
    _aendern(a, 19.0)
    _aendern(a, 20.0)  # die erste Kette laeuft noch
    assert _arten(plan, EV_UEB_ABLAUF) == [14401.0]
    sekunden = 14281  # die erste Kette fuehrt auf den Stand, an dem die zweite Uebersteuerung noch 119 Sekunden laeuft
    a.weiter(timedelta(seconds=sekunden))
    a.clock.advance(sekunden)
    a.worker.run_pending()
    assert _arten(plan, EV_UEB_ABLAUF) == [14401.0, pytest.approx(120.0)]  # neu eingeplant, solange sie laeuft
    assert a.quelle.geschrieben == []
    a.weiter(timedelta(seconds=125))
    a.clock.advance(125)
    a.worker.run_pending()
    assert a.store.uebersteuerung() is None and a.quelle.geschrieben == [16.0]


# --- Nachschreiben -----------------------------------------------------------------------------------------------


def _verlorene_meldung(a: Aufbau, anzeige: float) -> None:
    """Nach der Frist meldet das Thermostat weiter den alten Wert `anzeige`."""
    a.clock.advance(PAUSE)
    a.post(anzeige, SOURCE_THERMOSTAT, herkunft="soll_entity")


def test_eine_neue_eigene_schreibung_bekommt_alle_wiederholungen_wieder(tmp_path):
    a = _einfach(tmp_path)
    _verloren(a, 20.0)
    a.post(22.0, SOURCE_PORTAL)
    for _ in range(3):
        _verlorene_meldung(a, 20.0)
    assert a.quelle.geschrieben == [22.0] * 4  # die Schreibung und drei Wiederholungen
    a.clock.advance(PAUSE)
    a.post(23.0, SOURCE_PORTAL)
    _verlorene_meldung(a, 20.0)
    assert a.quelle.geschrieben == [22.0] * 4 + [23.0, 23.0]


def test_nach_dem_ende_wird_das_soll_ende_hoechstens_dreimal_wiederholt(tmp_path):
    a = _einfach(tmp_path)
    a.worker.register(EV_SOLL_ENDE_ERLEDIGT, lambda event: None)
    _verloren(a, 18.0)
    a.worker.post(Event(EV_SOLL_ENDE))
    a.worker.run_pending()
    assert a.quelle.geschrieben == [21.0]
    for _ in range(3):
        _verlorene_meldung(a, 18.0)
    assert a.quelle.geschrieben == [21.0] * 4
    _verlorene_meldung(a, 18.0)  # danach gilt die Meldung als Eingabe des Kunden, nichts wird mehr geschrieben
    assert a.quelle.geschrieben == [21.0] * 4


def test_nachschreiben_ueberspringt_den_merker_der_letzten_abgleich_schreibung(tmp_path):
    """Ging eine andere Schreibung verloren als die des Abgleichs und gilt inzwischen wieder dessen Soll, schreibt das
    Nachschreiben es trotz des Merkers (geltend, bekannt) der Abgleich-Schreibung."""
    a = _einfach(tmp_path)
    _verloren(a, 20.0)
    a.post(22.0, SOURCE_PORTAL)  # der Abgleich schreibt 22 und merkt sich (22, 20)
    a.store.note_own_write(veraltet=20.0, geschrieben=21.0)  # danach eine Schreibung auf anderem Weg (kein Merker)
    _verlorene_meldung(a, 20.0)
    assert a.quelle.geschrieben == [22.0, 22.0]
