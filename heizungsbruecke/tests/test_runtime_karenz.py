"""Karenz nach einem Softwarewechsel: die Meldung zum Konfigurationsfehler ("Heizungssteuerung gestoppt") wartet 15 Minuten
auf die neue Konfiguration; Zustand, Status und Ruhezustand bleiben wahr. Abgelehnte Konfiguration, abgelaufene Karenz,
laufender Boost, ein Start ohne Softwarewechsel und jeder Startfehler, der nicht die Form der Konfiguration betrifft
(unbekannter Hebelsatz, unbekanntes Verteilsystem, fehlende Bindungsparameter oder Pflichtzuordnungen), melden sofort:
Rastung, Positivliste, Sicherheitswerte, Treiber, Kanalverzeichnis, Anlage, ungueltige Werte. Die Karenz beginnt je Softwarestand nur einmal, auch wenn die
monotone Uhr nach einem Neustart zurueckspringt."""
import json

import pytest
from geraet_helpers import konfiguration_aus_optionen
from rastung_beispiele import rastung_aus
from runtime_helpers import OPTIONS, _start_bridge

from heizungsbruecke import config, dokument
from smartheat_core import dateien, rastung
from smartheat_device import fehler
from smartheat_runtime import app, karenz
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.roles import ManifestError

FEHLER = {"lever_set": "gibt_es_nicht"}  # Hebelsatz, den diese Software nicht kennt: Konfigurationsform
SOFORT = {"daily_trigger_time": "25:00"}  # ungueltiger Wert: Konfigurationsfehler ohne Bezug zur Form
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


def test_die_neupruefung_kommt_zum_ende_der_restlichen_karenz(env):
    _gemerkt(env)
    _start_bridge(env, **FEHLER)
    env.clock.advance(300)
    bridge = _start_bridge(env, **FEHLER)  # Neustart in der Karenz: noch 600 Sekunden

    env.clock.advance(599)
    bridge.worker.run_pending()
    assert env.geraet.neustarts == []

    env.clock.advance(1)
    bridge.worker.run_pending()
    assert env.geraet.neustarts == [1]  # nicht erst nach einer vollen Neupruefungsfrist


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


def test_ein_ungueltiger_wert_der_konfiguration_meldet_sofort(env):
    """Auch nach einem Softwarewechsel: ein Wert, den keine neue Software anders liest, wartet nicht."""
    _gemerkt(env)

    _start_bridge(env, **SOFORT)

    assert _gemeldet(env)
    assert json.loads(_datei(env).read_text()) == {"version": NEU}  # kein Beginn gemerkt


def test_ein_verstoss_gegen_die_rastung_meldet_sofort(env):
    _gemerkt(env)
    rastung.schreiben(config.RASTUNG_PATH, rastung_aus(konfiguration_aus_optionen(OPTIONS), verteilsystem="Fussbodenheizung"),
                      "2026-10-14T00:00:00+00:00")

    _start_bridge(env)

    assert _gemeldet(env)


def test_ein_verstoss_gegen_die_positivliste_meldet_sofort(env):
    _gemerkt(env)
    env.ha.states["person.luca"] = "home"

    _start_bridge(env, entity_operating_mode="person.luca")

    assert _gemeldet(env)


def test_ein_fehler_des_kanalverzeichnisses_meldet_sofort(env, monkeypatch):
    _gemerkt(env)

    def _manifest(*args, **kwargs):
        raise ManifestError("Pflichtrolle fehlt")

    monkeypatch.setattr("heizungsbruecke.host.build_manifest", _manifest)

    _start_bridge(env)

    assert _gemeldet(env)


def test_fehlende_entities_nach_dem_budget_melden_sofort(env, monkeypatch):
    _gemerkt(env)
    monkeypatch.setattr("heizungsbruecke.host.time.sleep", lambda s: None)
    del env.ha.states["number.heat_limit"]

    _start_bridge(env)

    assert _gemeldet(env)


def test_eine_boot_schleife_unterdrueckt_die_meldung_nicht_dauerhaft(env):
    """Springt die monotone Uhr nach einem Neustart zurueck, beginnt keine zweite Karenz."""
    _gemerkt(env)
    _start_bridge(env, **FEHLER)
    assert not _gemeldet(env)

    env.clock.advance(-900)  # Neustart des Geraets: die monotone Uhr beginnt von vorn
    _start_bridge(env, **FEHLER)

    assert _gemeldet(env)


def test_ein_weiterer_softwarewechsel_beginnt_eine_neue_karenz(env, monkeypatch):
    _gemerkt(env)
    _start_bridge(env, **FEHLER)
    env.clock.advance(-900)
    _start_bridge(env, **FEHLER)
    assert _gemeldet(env)
    env.ha.persistent.clear()

    monkeypatch.setattr("heizungsbruecke.host.ADDON_VERSION", "3.0.0")
    _start_bridge(env, **FEHLER)

    assert not _gemeldet(env)


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


def test_ein_zurueckgenommener_boost_laesst_die_meldung_warten(env, monkeypatch):
    """Die Pflichtzuordnung fehlt (Formgrund), die Bindung zum Zuruecknehmen ist aber lesbar."""
    monkeypatch.setattr("heizungsbruecke.dokument.lokal_befund", lambda *args: dokument.Befund(
        fehler.details("curve", "fehlt"), "Es fehlt die Zuordnung curve."))
    _gemerkt(env)
    save_backup(env.paths["BACKUP_PATH"], {
        "boost_active": True, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    env.ha.states["climate.zone"] = "heat_cool"

    _start_bridge(env)

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

    assert json.loads((tmp_path / karenz.DATEI).read_text()) == {
        "version": ALT, "beginn": clock(), "karenz_fuer": NEU}


def test_eine_zurueckgesprungene_uhr_beginnt_keine_zweite_karenz_derselben_version(tmp_path, clock):
    assert _karenz(tmp_path, clock).rest() is None  # ohne Datei und ohne Zustand keine Karenz
    _schreiben(tmp_path, {"version": ALT})
    assert _karenz(tmp_path, clock).rest() == 900

    clock.advance(-500)  # Neustart des Geraets: die monotone Uhr beginnt von vorn

    assert _karenz(tmp_path, clock).rest() is None


def test_nach_dem_ruecksprung_bleibt_es_bei_keiner_karenz_auch_wenn_die_uhr_wieder_aufholt(tmp_path, clock):
    _schreiben(tmp_path, {"version": ALT})
    _karenz(tmp_path, clock).rest()
    clock.advance(-500)
    assert _karenz(tmp_path, clock).rest() is None

    clock.advance(500)

    assert _karenz(tmp_path, clock).rest() == 900  # der gemerkte Beginn gilt weiter und laeuft ab
    clock.advance(900)
    assert _karenz(tmp_path, clock).rest() is None


def test_ein_weiterer_softwarewechsel_beginnt_nach_dem_ruecksprung_eine_neue_karenz(tmp_path, clock):
    _schreiben(tmp_path, {"version": ALT})
    _karenz(tmp_path, clock).rest()
    clock.advance(-500)

    neuer = karenz.Karenz(tmp_path / karenz.DATEI, "3.0.0", zustand_vorhanden=False, clock=clock)

    assert neuer.rest() == 900
    assert json.loads((tmp_path / karenz.DATEI).read_text())["karenz_fuer"] == "3.0.0"


def _version_drei(tmp_path, uhr):
    return karenz.Karenz(tmp_path / karenz.DATEI, "3.0.0", zustand_vorhanden=False, clock=uhr)


def test_ein_weiterer_wechsel_ohne_uhrsprung_erbt_den_beginn_der_laufenden_karenz(tmp_path, clock):
    """Die Karenz zaehlt vom gemerkten Beginn, nicht je Version: eine dritte Version in der Karenz der zweiten wartet
    nur den Rest ab (kuerzer, nie laenger: die sichere Richtung)."""
    _schreiben(tmp_path, {"version": ALT})
    assert _karenz(tmp_path, clock).rest() == 900
    clock.advance(300)

    assert _version_drei(tmp_path, clock).rest() == pytest.approx(600)


def test_ein_weiterer_wechsel_nach_dem_ende_der_karenz_ohne_uhrsprung_meldet_sofort(tmp_path, clock):
    _schreiben(tmp_path, {"version": ALT})
    _karenz(tmp_path, clock).rest()
    clock.advance(1000)

    assert _version_drei(tmp_path, clock).rest() is None


def test_eine_datei_ohne_vermerk_der_karenz_bleibt_lesbar_und_beginnt_bei_ruecksprung_neu(tmp_path, clock):
    """Dateien des alten Stands kennen das Feld nicht; unbekannte Felder werden ignoriert."""
    _schreiben(tmp_path, {"version": ALT, "beginn": clock() + 5000, "weiteres": 1})

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
