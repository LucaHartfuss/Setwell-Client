"""Soll-Speicher: feste Zahl der Wiederholungen eines verlorenen Schreibbefehls, Zaehler nach einer Bestaetigung, Werte
eines zweiten Befehls vor der Bestaetigung, Anzeige mit Uebersteuerung, Restzeit einer offenen Schreibung."""
import pytest

from smartheat_runtime.soll_speicher import TargetStore

BIS = "2026-01-15T22:00:00Z"
FRIST = 1800  # Sekunden, nach denen ein unbestaetigter Schreibbefehl verfaellt
ECHO = 60  # Sekunden Echo-Fenster nach dem eigenen Schreiben


def _store(tmp_path, clock, start=20.0):
    return TargetStore(tmp_path / "runtime" / "room_target.json", start, clock, lambda: "2026-01-15T08:00:00+01:00")


def _wiederholungen_bis_eingabe(store, clock, wert, alt) -> int:
    """Wie oft der Aufrufer `wert` erneut schreibt, solange das Thermostat nach jeder Frist weiter `alt` meldet; endet,
    sobald die Meldung als Eingabe des Kunden gilt."""
    n = 0
    while n < 10:
        clock.advance(FRIST + 1)
        if store.thermostat_pruefen(alt) is not None:
            return n
        assert store.nachschreiben_nehmen() == (wert, alt)
        n += 1
        store.note_own_write(veraltet=alt, geschrieben=wert, wiederholung=True)
    raise AssertionError("die Wiederholungen enden nie")


def _verlorener_befehl(tmp_path, clock) -> TargetStore:
    store = _store(tmp_path, clock)  # das Thermostat zeigt 20
    store.apply_portal(22.0)
    store.note_own_write()
    return store


def test_ein_verlorener_befehl_wird_genau_dreimal_wiederholt(tmp_path, clock):
    store = _verlorener_befehl(tmp_path, clock)
    assert _wiederholungen_bis_eingabe(store, clock, 22.0, 20.0) == 3


def test_eine_wiederholung_ohne_vorangegangenen_befehl_beginnt_mit_dem_vollen_vorrat(tmp_path, clock):
    store = _store(tmp_path, clock)
    store.apply_portal(22.0)
    store.note_own_write(veraltet=20.0, geschrieben=22.0, wiederholung=True)
    assert _wiederholungen_bis_eingabe(store, clock, 22.0, 20.0) == 3


def test_die_bestaetigung_im_echo_fenster_setzt_den_zaehler_der_wiederholungen_zurueck(tmp_path, clock):
    store = _verlorener_befehl(tmp_path, clock)
    clock.advance(FRIST + 1)
    assert store.thermostat_pruefen(20.0) is None
    assert store.nachschreiben_nehmen() == (22.0, 20.0)  # erste Wiederholung verbraucht
    store.note_own_write(veraltet=20.0, geschrieben=22.0, wiederholung=True)
    assert store.thermostat_pruefen(22.0) is None  # Bestaetigung im Echo-Fenster
    store.note_own_write(veraltet=20.0, geschrieben=22.0, wiederholung=True)
    assert _wiederholungen_bis_eingabe(store, clock, 22.0, 20.0) == 3


def test_die_bestaetigung_nach_dem_echo_fenster_setzt_den_zaehler_der_wiederholungen_zurueck(tmp_path, clock):
    store = _verlorener_befehl(tmp_path, clock)
    clock.advance(FRIST + 1)
    assert store.thermostat_pruefen(20.0) is None
    assert store.nachschreiben_nehmen() == (22.0, 20.0)
    store.note_own_write(veraltet=20.0, geschrieben=22.0, wiederholung=True)
    clock.advance(ECHO + 1)
    assert store.thermostat_pruefen(22.0) is None  # Bestaetigung nach dem Echo-Fenster, vor der Frist
    store.note_own_write(veraltet=20.0, geschrieben=22.0, wiederholung=True)
    assert _wiederholungen_bis_eingabe(store, clock, 22.0, 20.0) == 3


def test_der_erste_schreibwert_eines_ueberholten_befehls_ist_nach_der_frist_ein_verlorener_befehl(tmp_path, clock):
    store = _store(tmp_path, clock)  # das Thermostat zeigt 20
    store.apply_portal(22.0)
    store.note_own_write()
    clock.advance(120)
    store.apply_portal(23.0)
    store.note_own_write()  # zweiter Befehl, bevor der erste bestaetigt ist
    clock.advance(FRIST + 1)
    assert store.thermostat_pruefen(22.0) is None
    assert store.nachschreiben_nehmen() == (23.0, 22.0)
    assert store.value == 23.0


def test_die_anzeige_ist_die_uebersteuerung_solange_sie_gilt_sonst_der_wunsch(tmp_path, clock):
    store = _store(tmp_path, clock)
    assert store.anzeige() == 20.0
    store.uebersteuerung_setzen(18.5, BIS)
    assert store.anzeige() == 18.5
    assert store.value == 20.0
    store.uebersteuerung_aufheben()
    assert store.anzeige() == 20.0


def test_die_restzeit_einer_schreibung_ist_der_laengere_von_echo_fenster_und_offenem_befehl(tmp_path, clock):
    store = _store(tmp_path, clock)
    assert store.schreibung_rest() == 0.0
    store.apply_portal(22.0)
    store.note_own_write()
    assert store.schreibung_rest() == pytest.approx(FRIST)
    clock.advance(100)
    assert store.echo_rest() == 0.0
    assert store.schreibung_rest() == pytest.approx(FRIST - 100)
