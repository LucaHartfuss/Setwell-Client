"""Grenzfaelle am Geraet, die nur ueber die Schnittstelle sichtbar sind: Abweisungsgrund bei ungueltigem Kreis oder
Urheber, Grenzen der Rueckkehrzeit, Uhrzustand im Wunsch, Hinweis und Begrenzungsmeldung beim Ende einer Betriebsart,
Kreisnummer an der oberen Grenze in der Datei des beendeten Leerstands."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from device_helpers import WALL, World, _bedienung

from smartheat_device import device, wire

JETZT = datetime.fromtimestamp(WALL, UTC)
BIS = datetime(2027, 1, 20, 18, 0, tzinfo=UTC)
BIS_TEXT = "2027-01-20T18:00:00Z"
ABWESEND = {"bis": BIS_TEXT, "temp": 16.0, "aufheizen_ab": None}
LEERSTAND = {"von": "2027-01-15T08:00:00Z", "bis": "2027-02-15T10:00:00Z", "temp": 16.0, "aufheizen_ab": None}


def _kreis(nummer: int = 1, **eintrag) -> dict:
    return {"kreis": nummer, "modus": "manuell", "raum_soll": 21.0, "wochenplan": None, "abwesend": None,
            "leerstand": None, "uebersteuerung": None, "abwesend_temp_standard": None, **eintrag}


def _welt(tmp_path, *, nummer: int = 1, **eintrag) -> World:
    welt = World(tmp_path)
    welt.host.faehigkeiten = lambda: {"drivers": ["simulation"], "zigbee": True, "programm": True}
    welt.verbinden()
    welt.empfangen("down/operation", _bedienung(version=3, kreise=[_kreis(nummer, **eintrag)]))
    return welt


def _wunsch(welt: World) -> dict:
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    return gesendet


def _begrenzt_meldung(welt: World) -> list[str]:
    welt.device.schritt()
    return [m["key"] for m in welt.host.anzeigen_liste[-1].meldungen]


# --- Abweisungsgrund: Kreisnummer und Urheber werden getrennt gemeldet ---

@pytest.mark.parametrize("durch", [None, "nutzer"])
@pytest.mark.parametrize("kreis", [0, wire.KREIS_MAX + 1, True])
def test_ein_ungueltiger_kreis_ist_abgelehnt_mit_dem_grund_kreis_auch_bei_gueltigem_urheber(tmp_path, kreis, durch):
    welt = _welt(tmp_path)
    assert welt.device.abwesend_setzen(kreis, BIS, 16.0, durch) == device.ABGELEHNT_KREIS
    assert welt.device.abwesend_beenden(kreis, durch) == device.ABGELEHNT_KREIS


@pytest.mark.parametrize("durch", ["unbekannt", 3])
def test_das_ende_von_away_mit_unbekanntem_urheber_hat_den_grund_durch(tmp_path, durch):
    welt = _welt(tmp_path, modus="abwesend", abwesend=ABWESEND)
    assert welt.device.abwesend_beenden(1, durch) == device.ABGELEHNT_DURCH
    welt.device.schritt()
    assert welt.link.of("up/wish") == []
    assert welt.device.bedienung()["kreise"][0]["modus"] == "abwesend"


# --- Rueckkehrzeit: Zeitpunkt in der Vergangenheit, ohne Zone, zu weit voraus ---

@pytest.mark.parametrize("bis", [
    JETZT - timedelta(hours=1),
    JETZT,
    JETZT.replace(tzinfo=None) + timedelta(days=1),
    JETZT + timedelta(days=366),
    "2027-01-20T18:00:00Z",
    None,
], ids=["vergangen", "jetzt", "ohne_zone", "zu_weit", "text", "keiner"])
def test_eine_ungueltige_rueckkehrzeit_ist_abgelehnt_und_wirkt_nicht(tmp_path, bis):
    welt = _welt(tmp_path)
    assert welt.device.abwesend_setzen(1, bis, 16.0, None) == device.ABGELEHNT_BIS
    assert welt.device.bedienung()["kreise"][0]["modus"] == "manuell"
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


# --- Das Ende von Away: nur ein laufendes Away, nicht jedes halbe Merkmal ---

def test_ein_kreis_im_modus_abwesend_ohne_away_objekt_hat_kein_laufendes_away(tmp_path):
    welt = _welt(tmp_path, modus="abwesend", abwesend=None)
    assert welt.device.abwesend_beenden(1, None) == device.ABGELEHNT_KEIN_ABWESEND


# --- Uhrzustand im Wunsch: bei Away, Uebersteuerung und Ende einer Betriebsart ---

@pytest.mark.parametrize("synchron", [True, False, None])
def test_der_wunsch_nennt_den_uhrzustand_bei_away(tmp_path, synchron):
    welt = _welt(tmp_path)
    welt.host.synchron = synchron
    assert welt.device.abwesend_setzen(1, BIS, 16.0, None) is None
    assert _wunsch(welt)["uhr_synchron"] is (synchron is True)


@pytest.mark.parametrize("synchron", [True, False, None])
def test_der_wunsch_nennt_den_uhrzustand_bei_der_uebersteuerung(tmp_path, synchron):
    welt = _welt(tmp_path)
    welt.host.synchron = synchron
    welt.device.uebersteuerung_senden(1, 19.0, BIS_TEXT, "soll_entity", "nutzer")
    assert _wunsch(welt)["uhr_synchron"] is (synchron is True)


@pytest.mark.parametrize("synchron", [True, False, None])
def test_der_wunsch_nennt_den_uhrzustand_beim_ende_einer_betriebsart(tmp_path, synchron):
    welt = _welt(tmp_path)
    welt.host.synchron = synchron
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    assert _wunsch(welt)["uhr_synchron"] is (synchron is True)


# --- Ende einer Betriebsart: Hinweis der Away-Ablehnung und Meldung der Begrenzung ---

def test_das_ende_von_away_am_thermostat_schliesst_den_hinweis_der_ablehnung(tmp_path):
    welt = _welt(tmp_path, modus="abwesend", abwesend=ABWESEND)
    welt.device.abwesend_abgelehnt(device.ABGELEHNT_TEMP)
    welt.device.schritt()
    assert welt.host.anzeigen_liste[-1].abwesend_abgelehnt is not None
    welt.device.betriebsart_beenden(1, "abwesend", 22.0, None, "soll_entity", "nutzer")
    welt.device.schritt()
    assert welt.host.anzeigen_liste[-1].abwesend_abgelehnt is None


def test_das_ende_des_leerstands_am_thermostat_laesst_den_hinweis_der_ablehnung_stehen(tmp_path):
    welt = _welt(tmp_path, modus="leerstand", leerstand=LEERSTAND)
    welt.device.abwesend_abgelehnt(device.ABGELEHNT_TEMP)
    welt.device.schritt()
    welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer")
    welt.device.schritt()
    assert welt.host.anzeigen_liste[-1].abwesend_abgelehnt is not None


def test_ein_ende_mit_begrenztem_wert_meldet_die_begrenzung(tmp_path):
    welt = _welt(tmp_path)
    assert welt.device.betriebsart_beenden(1, "leerstand", 30.0, None, "soll_entity", "nutzer") == 25.0
    assert _begrenzt_meldung(welt) == ["wunsch_begrenzt"]
    assert _wunsch(welt)["roh"] == 30.0


def test_ein_ende_mit_unbegrenztem_wert_schliesst_eine_offene_meldung_der_begrenzung(tmp_path):
    welt = _welt(tmp_path)
    welt.device.wunsch(30.0, herkunft="thermostat")
    assert _begrenzt_meldung(welt) == ["wunsch_begrenzt"]
    assert welt.device.betriebsart_beenden(1, "leerstand", 22.0, None, "soll_entity", "nutzer") == 22.0
    assert _begrenzt_meldung(welt) == []


def test_ein_ende_mit_uebersteuerung_laesst_eine_offene_meldung_der_begrenzung_stehen(tmp_path):
    welt = _welt(tmp_path)
    welt.device.wunsch(30.0, herkunft="thermostat")
    assert _begrenzt_meldung(welt) == ["wunsch_begrenzt"]
    welt.device.betriebsart_beenden(1, "leerstand", None, {"temp": 19.0, "bis": BIS_TEXT}, "soll_entity", "nutzer")
    assert _begrenzt_meldung(welt) == ["wunsch_begrenzt"]


# --- Datei des beendeten Leerstands: Kreisnummern von KREIS_MIN bis KREIS_MAX ---

@pytest.mark.parametrize("nummer", [wire.KREIS_MIN, wire.KREIS_MAX])
def test_die_gespeicherte_kreisnummer_an_der_grenze_gilt_nach_dem_neustart(tmp_path, nummer):
    _welt(tmp_path, nummer=nummer, modus="leerstand", leerstand=LEERSTAND)
    (tmp_path / device.DEVICE_DIR / device.LEERSTAND_BEENDET_DATEI).write_text(json.dumps([nummer]))
    neu = World(tmp_path)  # frischer Prozess, dasselbe Verzeichnis, kein neues Dokument des Servers
    eintrag = neu.device.bedienung()["kreise"][0]
    assert (eintrag["kreis"], eintrag["modus"], eintrag["leerstand"]) == (nummer, "manuell", None)


def test_eine_kreisnummer_ueber_der_grenze_in_der_datei_gilt_nicht(tmp_path):
    _welt(tmp_path, nummer=wire.KREIS_MAX, modus="leerstand", leerstand=LEERSTAND)
    (tmp_path / device.DEVICE_DIR / device.LEERSTAND_BEENDET_DATEI).write_text(json.dumps([wire.KREIS_MAX + 1]))
    neu = World(tmp_path)
    assert neu.device.bedienung()["kreise"][0]["modus"] == "leerstand"
