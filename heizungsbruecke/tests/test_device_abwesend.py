"""Away aus Home Assistant am Geraet: lokal sofort und ohne Server in der Planrechnung, als Bedienwunsch mit Herkunft
`ha` zum Server, bis eine neue Bedienung des Servers kommt; Ende, Ablehnung als Hinweis, keine Rueckkehrzeit im Log."""
import logging
from datetime import UTC, datetime

import pytest
from device_helpers import WALL, World, _bedienung

from smartheat_device import device, wire
from smartheat_runtime.plan_quelle import geltendes_soll, plan_eingabe

JETZT = datetime.fromtimestamp(WALL, UTC)
BIS = datetime(2027, 1, 20, 18, 0, tzinfo=UTC)
BIS_TEXT = "2027-01-20T18:00:00Z"
KONFIGURATION = {"zeitzone": "Europe/Berlin", "kreise": [{"kreis": 1}]}


def _kreis(**eintrag) -> dict:
    """Ein Kreis der Bedienung mit dem vollen Feldsatz der Faehigkeit `programm`."""
    return {"kreis": 1, "modus": "manuell", "raum_soll": 21.0, "wochenplan": None, "abwesend": None,
            "leerstand": None, "uebersteuerung": None, "abwesend_temp_standard": None, **eintrag}


def _welt(tmp_path, **eintrag) -> World:
    welt = World(tmp_path)
    welt.verbinden()
    welt.empfangen("down/operation", _bedienung(version=3, kreise=[_kreis(**eintrag)]))
    return welt


def _soll(welt: World) -> tuple[float | None, str | None]:
    """Das geltende Soll des Kreises 1 aus den laufenden Dokumenten, bei Wunsch 21 °C."""
    eingabe = plan_eingabe(KONFIGURATION, welt.device.bedienung(), 1)
    ergebnis = geltendes_soll(eingabe, 21.0, JETZT, uhr_vertrauenswuerdig=True, soll_quelle_aus=False)
    return ergebnis.soll, ergebnis.soll_art


def _eintrag(welt: World) -> dict:
    return welt.device.bedienung()["kreise"][0]


def _hinweis(welt: World) -> dict | None:
    return welt.host.anzeigen_liste[-1].abwesend_abgelehnt


def test_away_wirkt_ohne_server_auf_das_geltende_soll(tmp_path):
    welt = _welt(tmp_path)
    welt.link.trennen()
    welt.device.schritt()
    assert _soll(welt) == (21.0, "wunsch")
    assert welt.device.abwesend_setzen(1, BIS, 16.0, "nutzer") is None
    assert _soll(welt) == (16.0, "abwesend")
    assert (_eintrag(welt)["modus"], _eintrag(welt)["abwesend"]) == (
        "abwesend", {"bis": BIS_TEXT, "temp": 16.0, "aufheizen_ab": None})


def test_away_geht_als_bedienwunsch_mit_herkunft_ha_zum_server(tmp_path):
    welt = _welt(tmp_path)
    assert welt.device.abwesend_setzen(1, BIS, 16.0, "nutzer") is None
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert (gesendet["kreis"], gesendet["herkunft"], gesendet["durch"], gesendet["basis_version"]) == (
        1, "ha", "nutzer", 3)
    assert gesendet["werte"] == {"abwesend": {"bis": BIS_TEXT, "temp": 16.0, "aufheizen_ab": None}}


def test_der_wunsch_geht_nach_dem_wiederverbinden_hinaus(tmp_path):
    welt = _welt(tmp_path)
    welt.link.trennen()
    welt.device.abwesend_setzen(1, BIS, 16.0, None)
    welt.device.schritt()
    assert welt.link.of("up/wish") == []
    welt.link.sent.clear()
    welt.link.verbinden()
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert gesendet["werte"]["abwesend"]["temp"] == 16.0 and "durch" not in gesendet


def test_ohne_temp_gilt_der_zuletzt_gewaehlte_wert_des_kreises(tmp_path):
    welt = _welt(tmp_path, abwesend_temp_standard=17.5)
    assert welt.device.abwesend_setzen(1, BIS, None, None) is None
    assert _soll(welt) == (17.5, "abwesend")
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert gesendet["werte"]["abwesend"]["temp"] == 17.5


def test_ein_neu_gewaehlter_wert_ist_danach_der_zuletzt_gewaehlte(tmp_path):
    welt = _welt(tmp_path, abwesend_temp_standard=17.5)
    welt.device.abwesend_setzen(1, BIS, 16.0, None)
    assert welt.device.abwesend_beenden(1, None) is None
    assert welt.device.abwesend_setzen(1, BIS, None, None) is None
    assert _soll(welt) == (16.0, "abwesend")


def test_ohne_temp_und_ohne_zuletzt_gewaehlten_wert_ist_es_abgelehnt(tmp_path):
    welt = _welt(tmp_path)
    assert welt.device.abwesend_setzen(1, BIS, None, None) == device.ABGELEHNT_TEMP
    assert _soll(welt) == (21.0, "wunsch")
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


def test_ein_kreis_ohne_bedienung_ist_abgelehnt(tmp_path):
    welt = _welt(tmp_path)
    assert welt.device.abwesend_setzen(2, BIS, 16.0, None) == device.ABGELEHNT_KREIS
    assert welt.device.abwesend_beenden(2, None) == device.ABGELEHNT_KREIS
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


def test_bei_leerstand_wird_away_abgelehnt(tmp_path):
    welt = _welt(tmp_path, modus="leerstand")
    assert welt.device.abwesend_setzen(1, BIS, 16.0, None) == device.ABGELEHNT_LEERSTAND
    assert _eintrag(welt)["modus"] == "leerstand"


def test_das_ende_nimmt_die_wirkung_zurueck_und_geht_als_null_zum_server(tmp_path):
    welt = _welt(tmp_path)
    welt.device.abwesend_setzen(1, BIS, 16.0, "nutzer")
    welt.device.schritt()
    mid = welt.link.topics().index("up/wish") + 1
    welt.link.gesendet(mid)
    welt.device.schritt()
    welt.link.sent.clear()
    assert welt.device.abwesend_beenden(1, "nutzer") is None
    assert _soll(welt) == (21.0, "wunsch")
    assert (_eintrag(welt)["modus"], _eintrag(welt)["abwesend"]) == ("manuell", None)
    welt.device.schritt()
    (gesendet,) = welt.link.of("up/wish")
    assert (gesendet["herkunft"], gesendet["werte"]) == ("ha", {"abwesend": None})


def test_das_ende_beendet_auch_ein_vom_server_gesetztes_away(tmp_path):
    abwesend = {"bis": BIS_TEXT, "temp": 16.0, "aufheizen_ab": None}
    welt = _welt(tmp_path, modus="abwesend", abwesend=abwesend)
    assert _soll(welt) == (16.0, "abwesend")
    assert welt.device.abwesend_beenden(1, None) is None
    assert _soll(welt) == (21.0, "wunsch")


def test_das_ende_ohne_laufendes_away_hat_einen_festen_grund(tmp_path):
    welt = _welt(tmp_path)
    assert welt.device.abwesend_beenden(1, None) == device.ABGELEHNT_KEIN_ABWESEND
    welt.device.schritt()
    assert welt.link.of("up/wish") == []


def test_eine_neue_bedienung_des_servers_loest_die_lokale_wirkung_ab(tmp_path):
    welt = _welt(tmp_path)
    welt.device.abwesend_setzen(1, BIS, 16.0, None)
    assert _soll(welt) == (16.0, "abwesend")
    welt.empfangen("down/operation", _bedienung(version=4, kreise=[_kreis()]))
    assert _soll(welt) == (21.0, "wunsch")
    assert _eintrag(welt)["abwesend"] is None


def test_die_bestaetigte_bedienung_mit_away_gilt_weiter(tmp_path):
    abwesend = {"bis": BIS_TEXT, "temp": 16.0, "aufheizen_ab": None}
    welt = _welt(tmp_path)
    welt.device.abwesend_setzen(1, BIS, 16.0, None)
    welt.empfangen("down/operation", _bedienung(version=4, kreise=[_kreis(modus="abwesend", abwesend=abwesend)]))
    assert _soll(welt) == (16.0, "abwesend")


def test_die_lokale_wirkung_verdraengt_das_gespeicherte_dokument_nicht(tmp_path):
    welt = _welt(tmp_path)
    welt.device.abwesend_setzen(1, BIS, 16.0, None)
    assert welt.device.documents.inhalt("bedienung")["kreise"][0]["modus"] == "manuell"


def test_die_ablehnung_ist_ein_lokaler_hinweis_mit_festem_grund_und_schliesst_beim_naechsten_erfolg(tmp_path):
    welt = _welt(tmp_path)
    assert wire.MELDUNGEN.count("abwesend_abgelehnt") == 0  # kein Draht-Key: nie eine Meldung an den Server
    welt.device.abwesend_abgelehnt(device.ABGELEHNT_TEMP)
    welt.device.schritt()
    hinweis = _hinweis(welt)
    assert hinweis is not None
    assert (hinweis["code"], hinweis["params"], hinweis["stufe"]) == (
        "meldung.abwesend_abgelehnt", {"grund": "temp"}, "orange")
    assert not any(m["key"] == "abwesend_abgelehnt"
                   for sendung in welt.link.of("up/notifications") for m in sendung["items"])
    assert welt.device.abwesend_setzen(1, BIS, 16.0, None) is None
    welt.device.schritt()
    assert _hinweis(welt) is None


def test_das_angenommene_ende_schliesst_den_hinweis(tmp_path):
    welt = _welt(tmp_path, modus="abwesend", abwesend={"bis": BIS_TEXT, "temp": 16.0, "aufheizen_ab": None})
    welt.device.abwesend_abgelehnt(device.ABGELEHNT_BIS)
    welt.device.schritt()
    assert _hinweis(welt)["params"] == {"grund": "bis"}
    assert welt.device.abwesend_beenden(1, None) is None
    welt.device.schritt()
    assert _hinweis(welt) is None


def test_die_rueckkehrzeit_steht_in_keinem_logeintrag(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    welt = _welt(tmp_path)
    welt.device.abwesend_setzen(1, BIS, 16.0, "nutzer")
    welt.device.schritt()
    welt.device.abwesend_beenden(1, "nutzer")
    welt.device.abwesend_setzen(1, BIS, None, None)
    welt.device.abwesend_setzen(1, BIS, 16.0, "unbekannt")
    welt.device.schritt()
    assert "2027-01-20" not in caplog.text and "18:00" not in caplog.text and "T18" not in caplog.text


@pytest.mark.parametrize("durch", ["unbekannt", 3])
def test_ein_unbekannter_urheber_ist_kein_wunsch(tmp_path, durch):
    welt = _welt(tmp_path)
    assert welt.device.abwesend_setzen(1, BIS, 16.0, durch) == device.ABGELEHNT_DURCH
    welt.device.schritt()
    assert welt.link.of("up/wish") == [] and _soll(welt) == (21.0, "wunsch")
