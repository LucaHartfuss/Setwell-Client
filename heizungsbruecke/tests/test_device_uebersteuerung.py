"""Uebersteuerung der kuehlen Phase am Geraet: geht als Bedienwunsch mit programm (werte.uebersteuerung) zum Server, nur
mit der Faehigkeit programm; Ablehnung als ValueError, keine Zeiten im Log."""
import logging

import pytest
from device_helpers import World, _bedienung

BIS = "2027-01-20T18:00:00Z"


def _welt(tmp_path, *, programm=True) -> World:
    welt = World(tmp_path)
    if programm:
        welt.host.faehigkeiten = lambda: {"drivers": ["simulation"], "zigbee": True, "programm": True}
    welt.verbinden()
    welt.empfangen("down/operation", _bedienung(version=3, kreise=[{
        "kreis": 1, "modus": "manuell", "raum_soll": 21.0, "wochenplan": None, "abwesend": None, "leerstand": None,
        "uebersteuerung": None, "abwesend_temp_standard": None}]))
    return welt


def test_die_faehigkeit_programm_kommt_vom_host(tmp_path):
    assert _welt(tmp_path).device.programm_faehig() is True
    assert _welt(tmp_path / "ohne", programm=False).device.programm_faehig() is False


def test_die_uebersteuerung_geht_als_bedienwunsch_mit_programm_zum_server(tmp_path):
    welt = _welt(tmp_path)
    welt.device.uebersteuerung_senden(1, 19.0, BIS, "soll_entity", "nutzer")
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert (gesendet["kreis"], gesendet["herkunft"], gesendet["durch"], gesendet["basis_version"]) == (
        1, "soll_entity", "nutzer", 3)
    assert gesendet["werte"] == {"uebersteuerung": {"temp": 19.0, "bis": BIS}}
    assert "raum_soll" not in gesendet["werte"]


def test_ohne_faehigkeit_wird_sie_abgelehnt_und_nichts_gesendet(tmp_path):
    welt = _welt(tmp_path, programm=False)
    with pytest.raises(ValueError):
        welt.device.uebersteuerung_senden(1, 19.0, BIS, "soll_entity", None)
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


@pytest.mark.parametrize("temp,bis,herkunft", [
    (15.0, BIS, "soll_entity"),  # unter der Absenk-Untergrenze
    (99.0, BIS, "soll_entity"),
    (19.0, "morgen", "soll_entity"),
    (19.0, BIS, "unbekannt"),
    (True, BIS, "soll_entity"),
], ids=["zu_kalt", "zu_warm", "kein_zeitpunkt", "herkunft", "bool"])
def test_ungueltige_werte_sind_abgelehnt(tmp_path, temp, bis, herkunft):
    welt = _welt(tmp_path)
    with pytest.raises(ValueError):
        welt.device.uebersteuerung_senden(1, temp, bis, herkunft, None)
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


def test_das_ende_des_leerstands_geht_als_ein_wunsch_mit_dem_wert_der_phase_danach(tmp_path):
    welt = _welt(tmp_path)
    assert welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer") == 22.0
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert gesendet["werte"] == {"raum_soll": 22.0, "leerstand": None}
    assert (gesendet["herkunft"], gesendet["durch"]) == ("soll_entity", "nutzer")


def test_das_ende_in_der_kuehlen_phase_traegt_die_uebersteuerung_statt_des_wunsches(tmp_path):
    welt = _welt(tmp_path)
    assert welt.device.betriebsart_beenden(1, "leerstand", None, {"temp": 19.0, "bis": BIS}, "soll_entity", None) is None
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert gesendet["werte"] == {"leerstand": None, "uebersteuerung": {"temp": 19.0, "bis": BIS}}


def test_das_ende_von_away_geht_als_wunsch_und_gilt_lokal_sofort(tmp_path):
    welt = World(tmp_path)
    welt.host.faehigkeiten = lambda: {"drivers": ["simulation"], "zigbee": True, "programm": True}
    welt.verbinden()
    welt.empfangen("down/operation", _bedienung(version=3, kreise=[{
        "kreis": 1, "modus": "abwesend", "raum_soll": 21.0, "wochenplan": None, "leerstand": None, "uebersteuerung": None,
        "abwesend": {"bis": BIS, "temp": 16.0, "aufheizen_ab": None}, "abwesend_temp_standard": None}]))
    assert welt.device.bedienung()["kreise"][0]["modus"] == "abwesend"
    welt.device.betriebsart_beenden(1, "abwesend", 22.0, None, "soll_entity", None)
    assert welt.device.bedienung()["kreise"][0]["modus"] == "manuell"
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert gesendet["werte"] == {"raum_soll": 22.0, "abwesend": None}


@pytest.mark.parametrize("art,raum_soll,uebersteuerung,herkunft", [
    ("leerstand", 22.0, {"temp": 19.0, "bis": BIS}, "soll_entity"),  # beides
    ("leerstand", None, None, "soll_entity"),  # keines
    ("urlaub", 22.0, None, "soll_entity"),
    ("leerstand", True, None, "soll_entity"),
    ("leerstand", 22.0, None, "unbekannt"),
    ("leerstand", None, {"temp": 15.0, "bis": BIS}, "soll_entity"),  # unter der Absenk-Untergrenze
    ("leerstand", None, {"temp": 19.0, "bis": "morgen"}, "soll_entity"),
])
def test_ein_ungueltiges_ende_wird_abgelehnt_und_nichts_gesendet(tmp_path, art, raum_soll, uebersteuerung, herkunft):
    welt = _welt(tmp_path)
    with pytest.raises(ValueError):
        welt.device.betriebsart_beenden(1, art, raum_soll, uebersteuerung, herkunft, None)
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


def test_das_ende_ohne_faehigkeit_wird_abgelehnt_und_away_bleibt(tmp_path):
    welt = _welt(tmp_path, programm=False)
    with pytest.raises(ValueError):
        welt.device.betriebsart_beenden(1, "abwesend", 22.0, None, "soll_entity", None)
    assert welt.device.bedienung() == welt.device.documents.inhalt("bedienung")
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


def test_im_log_stehen_keine_zeiten_oder_werte(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    welt = _welt(tmp_path)
    welt.device.uebersteuerung_senden(1, 19.0, BIS, "soll_entity", None)
    welt.device.schritt()
    with pytest.raises(ValueError):
        welt.device.uebersteuerung_senden(1, 15.0, BIS, "soll_entity", None)
    assert "2027" not in caplog.text and BIS not in caplog.text
