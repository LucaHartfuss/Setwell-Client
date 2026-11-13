"""Karenz nach einem Softwarewechsel: die Meldung zum Konfigurationsfehler ("Heizungssteuerung gestoppt") wartet 15 Minuten
auf die neue Konfiguration; Zustand, Status und Ruhezustand bleiben wahr. Abgelehnte Konfiguration, abgelaufene Karenz,
laufender Boost und ein Start ohne Softwarewechsel melden sofort."""
import json

import pytest
from runtime_helpers import _start_bridge

from smartheat_core import dateien
from smartheat_runtime import app, karenz
from smartheat_runtime.backup_store import save_backup

FEHLER = {"entity_shift_current": "climate.zone", "daily_trigger_time": "25:00"}  # ungueltig -> Konfigurationsfehler
NEU, ALT = "2.0.0", "1.9.0"


@pytest.fixture(autouse=True)
def _version(monkeypatch):
    monkeypatch.setattr("heizungsbruecke.host.ADDON_VERSION", NEU)


def _datei(env):
    return env.paths["BACKUP_PATH"].parent / karenz.DATEI


def _gemerkt(env, version=ALT):
    dateien.schreiben_json(_datei(env), {"version": version}, modus=dateien.PRIVAT)


def _gemeldet(env) -> bool:
    return any("Konfigurationsfehler" in text for _, text in env.ha.persistent)


def _status(env) -> list[str]:
    return [data["status"] for kind, data in env.ha.events if kind == "smartheat_status"]


def test_nach_dem_softwarewechsel_wartet_die_meldung_und_der_zustand_bleibt_wahr(env):
    _gemerkt(env)

    bridge = _start_bridge(env, **FEHLER)

    assert bridge.reason == "konfigurationsfehler"
    assert _status(env)[-1] == "konfigurationsfehler"
    assert not _gemeldet(env)


def test_die_meldung_kommt_nach_neunhundert_sekunden(env):
    _gemerkt(env)
    bridge = _start_bridge(env, **FEHLER)

    env.clock.advance(899)
    bridge.worker.run_pending()
    assert env.geraet.neustarts == []  # die Neupruefung steht noch aus

    env.clock.advance(1)
    bridge.worker.run_pending()
    assert env.geraet.neustarts == [1]
    assert not _gemeldet(env)

    zweiter = _start_bridge(env, **FEHLER)  # der Neustart der Neupruefung
    assert zweiter.reason == "konfigurationsfehler"
    assert _gemeldet(env)


def test_ein_neustart_in_der_karenz_verlaengert_sie_nicht(env):
    _gemerkt(env)
    _start_bridge(env, **FEHLER)

    env.clock.advance(899)
    _start_bridge(env, **FEHLER)
    assert not _gemeldet(env)

    env.clock.advance(1)
    _start_bridge(env, **FEHLER)
    assert _gemeldet(env)


def test_kommt_die_konfiguration_vor_dem_ende_gibt_es_keine_meldung(env):
    _gemerkt(env)
    _start_bridge(env, **FEHLER)
    env.clock.advance(300)

    bridge = _start_bridge(env)  # gueltige Konfiguration

    assert isinstance(bridge, app.Runtime)
    assert not _gemeldet(env)
    assert json.loads(_datei(env).read_text()) == {"version": NEU}


def test_nach_dem_ende_der_karenz_melden_alle_weiteren_starts_sofort(env):
    _gemerkt(env)
    _start_bridge(env, **FEHLER)
    env.clock.advance(900)
    _start_bridge(env, **FEHLER)
    assert json.loads(_datei(env).read_text()) == {"version": NEU}
    env.ha.persistent.clear()

    _start_bridge(env, **FEHLER)

    assert _gemeldet(env)


def test_ein_start_ohne_softwarewechsel_meldet_sofort(env):
    _gemerkt(env, NEU)

    _start_bridge(env, **FEHLER)

    assert _gemeldet(env)


def test_eine_abgelehnte_konfiguration_meldet_sofort(env):
    _gemerkt(env)

    _start_bridge(env, abgelehnt=True, **FEHLER)

    assert _gemeldet(env)


def test_ohne_gemerkte_software_aber_mit_zustand_gilt_der_start_als_wechsel(env):
    save_backup(env.paths["BACKUP_PATH"], {"last_room_target": 21.0})

    _start_bridge(env, **FEHLER)

    assert not _gemeldet(env)


def test_ohne_gemerkte_software_und_ohne_zustand_meldet_der_erste_start_sofort(env):
    _start_bridge(env, **FEHLER)

    assert _gemeldet(env)


def test_bleibt_ein_boost_auf_der_anlage_meldet_der_start_sofort(env, monkeypatch):
    _gemerkt(env)
    save_backup(env.paths["BACKUP_PATH"], {
        "boost_active": True, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    monkeypatch.setattr("heizungsbruecke.host.HaHost.sign_off_parts", lambda self: None)

    _start_bridge(env, **FEHLER)

    assert any("Boost-Werten" in text for _, text in env.ha.persistent)


def test_ein_zurueckgenommener_boost_laesst_die_meldung_warten(env):
    _gemerkt(env)
    save_backup(env.paths["BACKUP_PATH"], {
        "boost_active": True, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    env.ha.states["climate.zone"] = "heat_cool"

    _start_bridge(env, **FEHLER)

    assert env.ha.writes
    assert not _gemeldet(env)


# --- die Karenz selbst ---

def _karenz(tmp_path, uhr, *, zustand=False):
    return karenz.Karenz(tmp_path / karenz.DATEI, NEU, zustand_vorhanden=zustand, clock=uhr)


def _schreiben(tmp_path, inhalt):
    dateien.schreiben_json(tmp_path / karenz.DATEI, inhalt, modus=dateien.PRIVAT)


def test_ohne_softwarewechsel_keine_karenz(tmp_path, clock):
    _schreiben(tmp_path, {"version": NEU})

    assert _karenz(tmp_path, clock).rest() is None


def test_der_rest_zaehlt_vom_gemerkten_beginn_ab(tmp_path, clock):
    _schreiben(tmp_path, {"version": ALT})
    assert _karenz(tmp_path, clock).rest() == 900

    clock.advance(899)
    assert _karenz(tmp_path, clock).rest() == pytest.approx(1)

    clock.advance(1)
    assert _karenz(tmp_path, clock).rest() is None


def test_der_beginn_bewahrt_die_gemerkte_alte_version(tmp_path, clock):
    _schreiben(tmp_path, {"version": ALT})

    _karenz(tmp_path, clock).rest()

    assert json.loads((tmp_path / karenz.DATEI).read_text()) == {"version": ALT, "beginn": clock()}


def test_eine_zurueckgesprungene_uhr_beginnt_die_karenz_neu(tmp_path, clock):
    _schreiben(tmp_path, {"version": ALT, "beginn": clock() + 5000})

    assert _karenz(tmp_path, clock).rest() == 900


@pytest.mark.parametrize("beginn", ["gestern", None, float("nan"), float("inf"), True, [1]])
def test_ein_unbrauchbarer_beginn_beginnt_die_karenz_neu(tmp_path, clock, beginn):
    (tmp_path / karenz.DATEI).write_text(json.dumps({"version": ALT, "beginn": beginn}))

    assert _karenz(tmp_path, clock).rest() == 900


def test_eine_unlesbare_datei_gilt_mit_zustand_als_wechsel_ohne_zustand_nicht(tmp_path, clock):
    (tmp_path / karenz.DATEI).write_text("{kaputt")

    assert _karenz(tmp_path, clock, zustand=True).rest() == 900
    assert _karenz(tmp_path, clock, zustand=False).rest() == 900  # der Beginn steht jetzt mit alter Version in der Datei


def test_ohne_datei_und_ohne_zustand_gibt_es_keine_karenz(tmp_path, clock):
    assert _karenz(tmp_path, clock).rest() is None
    assert not (tmp_path / karenz.DATEI).exists()


def test_laesst_sich_der_beginn_nicht_merken_gibt_es_keine_karenz(tmp_path, clock, monkeypatch):
    """Ohne gemerkten Beginn liefe die Karenz nie ab: dann lieber sofort melden."""
    _schreiben(tmp_path, {"version": ALT})

    def _kaputt(*args, **kwargs):
        raise OSError("Datentraeger voll")

    monkeypatch.setattr(dateien, "schreiben_json", _kaputt)

    assert _karenz(tmp_path, clock).rest() is None


def test_beenden_merkt_die_software_und_verwirft_den_beginn(tmp_path, clock):
    _schreiben(tmp_path, {"version": ALT, "beginn": 1.0})

    _karenz(tmp_path, clock).beenden()

    assert json.loads((tmp_path / karenz.DATEI).read_text()) == {"version": NEU}
    assert _karenz(tmp_path, clock).rest() is None


def test_beenden_wirft_nicht_wenn_das_schreiben_scheitert(tmp_path, clock, monkeypatch):
    def _kaputt(*args, **kwargs):
        raise OSError("Datentraeger voll")

    monkeypatch.setattr(dateien, "schreiben_json", _kaputt)

    _karenz(tmp_path, clock).beenden()
