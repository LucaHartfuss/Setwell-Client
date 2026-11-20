"""Uebersteuerung der kuehlen Phase am Geraet: geht als Bedienwunsch mit programm (werte.uebersteuerung) zum Server, nur
mit der Faehigkeit programm; Ablehnung als ValueError, keine Zeiten im Log."""
import logging
from datetime import UTC, datetime

import pytest
from device_helpers import World, _bedienung

from smartheat_core import safety
from smartheat_core.plan import soll_effektiv
from smartheat_device import device

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


_LEERSTAND = {"von": "2027-01-15T08:00:00Z", "bis": "2027-02-15T10:00:00Z", "temp": 16.0, "aufheizen_ab": None}
_JETZT = datetime(2027, 1, 20, 12, 0, tzinfo=UTC)


def _leerstand_welt(tmp_path, *, neu=False) -> World:
    """Ein Geraet mit laufendem Leerstand (Bedienung Version 3); `neu`: dasselbe Verzeichnis, frischer Prozess."""
    welt = World(tmp_path)
    welt.host.faehigkeiten = lambda: {"drivers": ["simulation"], "zigbee": True, "programm": True}
    welt.verbinden()
    if not neu:
        welt.empfangen("down/operation", _leerstand_bedienung(3))
    return welt


def _leerstand_bedienung(version: int) -> dict:
    return _bedienung(version=version, kreise=[{
        "kreis": 1, "modus": "leerstand", "raum_soll": 21.0, "wochenplan": None, "abwesend": None,
        "leerstand": dict(_LEERSTAND), "uebersteuerung": None, "abwesend_temp_standard": None}])


def _soll_jetzt(welt: World) -> tuple[float | None, str | None]:
    eintrag = welt.device.bedienung()["kreise"][0]
    erg = soll_effektiv(_JETZT, "Europe/Berlin", eintrag, {"absenkung_k": 3, "aufheizdauer_min": {"plan": 0, "weg": 0}},
                        uhr_vertrauenswuerdig=True)
    return erg.soll, erg.soll_art


def test_das_ende_des_leerstands_gilt_lokal_sofort_ohne_antwort_des_servers(tmp_path):
    welt = _leerstand_welt(tmp_path)
    assert _soll_jetzt(welt) == (16.0, "leerstand")
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    eintrag = welt.device.bedienung()["kreise"][0]
    assert (eintrag["modus"], eintrag["leerstand"]) == ("manuell", None)
    assert _soll_jetzt(welt) == (21.0, "wunsch")  # das Soll ohne Leerstand; den Wert 22 haelt der Soll-Speicher
    assert welt.device.documents.inhalt("bedienung")["kreise"][0]["modus"] == "leerstand"  # das Dokument bleibt


def test_das_lokale_ende_des_leerstands_ueberlebt_einen_neustart(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    neu = _leerstand_welt(tmp_path, neu=True)
    assert _soll_jetzt(neu) == (21.0, "wunsch")
    assert neu.device.bedienung()["kreise"][0]["leerstand"] is None


def test_ein_neues_dokument_des_servers_beendet_das_lokale_ende_auch_nach_einem_neustart(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    neu = _leerstand_welt(tmp_path, neu=True)
    neu.empfangen("down/operation", _leerstand_bedienung(4))  # der Server lehnt ab: der Leerstand gilt wieder
    assert _soll_jetzt(neu) == (16.0, "leerstand")
    spaeter = _leerstand_welt(tmp_path, neu=True)
    assert _soll_jetzt(spaeter) == (16.0, "leerstand")


def test_ein_neues_dokument_mit_leerstand_setzt_das_lokale_ende_zurueck(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    welt.empfangen("down/operation", _leerstand_bedienung(4))
    assert _soll_jetzt(welt) == (16.0, "leerstand")
    assert welt.device.bedienung()["kreise"][0]["leerstand"] == _LEERSTAND


def test_das_lokale_ende_betrifft_nur_den_eigenen_kreis(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.empfangen("down/operation", _bedienung(version=4, kreise=[
        {"kreis": 1, "modus": "leerstand", "raum_soll": 21.0, "wochenplan": None, "abwesend": None,
         "leerstand": dict(_LEERSTAND), "uebersteuerung": None, "abwesend_temp_standard": None},
        {"kreis": 2, "modus": "leerstand", "raum_soll": 20.0, "wochenplan": None, "abwesend": None,
         "leerstand": dict(_LEERSTAND), "uebersteuerung": None, "abwesend_temp_standard": None}]))
    welt.device.betriebsart_beenden(2, "leerstand", 22.0, None, "soll_entity", "nutzer")
    modi = [eintrag["modus"] for eintrag in welt.device.bedienung()["kreise"]]
    assert modi == ["leerstand", "manuell"]


@pytest.mark.parametrize("raum_soll,uebersteuerung", [(None, {"temp": 15.0, "bis": BIS}), (True, None)],
                         ids=["unter_der_untergrenze", "kein_wert"])
def test_ein_abgelehntes_ende_aendert_den_leerstand_lokal_nicht(tmp_path, raum_soll, uebersteuerung):
    welt = _leerstand_welt(tmp_path)
    with pytest.raises(ValueError):
        welt.device.betriebsart_beenden(1, "leerstand", raum_soll, uebersteuerung, "soll_entity", None)
    assert _soll_jetzt(welt) == (16.0, "leerstand")
    assert _soll_jetzt(_leerstand_welt(tmp_path, neu=True)) == (16.0, "leerstand")


def test_das_lokale_ende_haelt_die_grenzen_ein_auch_bei_zu_hohem_wunsch(tmp_path):
    welt = _leerstand_welt(tmp_path)
    begrenzt = welt.device.betriebsart_beenden(1, "leerstand", 40.0, None, "soll_entity", "nutzer")
    assert begrenzt == safety.BEDIENBEREICH_MAX_C
    welt.device.schritt()
    assert welt.link.of("up/wish")[-1]["werte"]["raum_soll"] == safety.BEDIENBEREICH_MAX_C
    assert _soll_jetzt(welt) == (21.0, "wunsch")


def test_eine_folgedrehung_laesst_das_ende_des_leerstands_im_puffer_stehen(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    welt.device.schritt()
    welt.device.wunsch(23.0, herkunft="soll_entity", durch="nutzer")
    welt.device.schritt()
    assert welt.device.wunsch_puffer.wunsch(1).programm == {"leerstand": None}
    assert welt.link.of("up/wish")[-1]["werte"] == {"raum_soll": 23.0, "leerstand": None}


def test_eine_folgedrehung_in_der_kuehlen_phase_behaelt_das_ende_und_traegt_die_uebersteuerung(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.device.betriebsart_beenden(1, "leerstand", None, {"temp": 19.0, "bis": BIS}, "soll_entity", None)
    welt.device.uebersteuerung_senden(1, 20.0, BIS, "soll_entity", None)
    welt.device.schritt()
    assert welt.device.wunsch_puffer.wunsch(1).programm == {"leerstand": None, "uebersteuerung": {"temp": 20.0, "bis": BIS}}


def test_ohne_offenes_ende_ueberlebt_kein_leerstand_im_puffer(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.device.wunsch(23.0, herkunft="soll_entity", durch="nutzer")
    welt.device.schritt()
    assert welt.device.wunsch_puffer.wunsch(1).programm is None


def test_im_log_stehen_keine_zeiten_oder_werte(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    welt = _welt(tmp_path)
    welt.device.uebersteuerung_senden(1, 19.0, BIS, "soll_entity", None)
    welt.device.schritt()
    with pytest.raises(ValueError):
        welt.device.uebersteuerung_senden(1, 15.0, BIS, "soll_entity", None)
    assert "2027" not in caplog.text and BIS not in caplog.text


def test_scheitert_das_speichern_des_endes_geht_der_wunsch_trotzdem_hinaus(tmp_path, caplog):
    welt = _leerstand_welt(tmp_path)
    (welt.device._dir / device.LEERSTAND_BEENDET_DATEI).mkdir()  # Schreiben scheitert mit OSError
    assert welt.device.betriebsart_beenden(1, "leerstand", 22.5, None, "soll_entity", "nutzer") == 22.5
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert gesendet["werte"] == {"raum_soll": 22.5, "leerstand": None}
    assert welt.device.bedienung()["kreise"][0]["modus"] == "manuell"  # bis zum Neustart gilt das Ende
    assert "nicht gespeichert" in caplog.text and "22.5" not in caplog.text and "2027" not in caplog.text


@pytest.mark.parametrize("inhalt", ["{kaputt", '{"kreis": 1}', '["eins", 99, null, 1.5]'])
def test_eine_kaputte_leerstand_beendet_datei_laesst_den_leerstand_des_servers_gelten(tmp_path, inhalt):
    _leerstand_welt(tmp_path)
    (tmp_path / device.DEVICE_DIR / device.LEERSTAND_BEENDET_DATEI).write_text(inhalt)
    neu = _leerstand_welt(tmp_path, neu=True)
    neu.empfangen("down/operation", _leerstand_bedienung(4))
    assert _soll_jetzt(neu) == (16.0, "leerstand")


def test_away_nach_dem_ende_des_leerstands_traegt_das_offene_ende_weiter(tmp_path):
    welt = _leerstand_welt(tmp_path)
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    welt.device.schritt()
    welt.device._abwesend_wunsch(1, {"bis": BIS, "temp": 16.0, "aufheizen_ab": None}, "nutzer")  # sonst nicht erreichbar
    programm = welt.device.wunsch_puffer.wunsch(1).programm
    assert programm["leerstand"] is None and programm["abwesend"]["temp"] == 16.0
