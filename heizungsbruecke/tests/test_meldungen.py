"""Meldung als Code (smartheat_core.meldungen): Typ, Tabelle der Texte, Rendern und Ablage im Zustand."""
import math

import pytest

from smartheat_core import meldungen
from smartheat_core.meldungen import Meldung

_ZAHLEN = {"anzahl": 3, "limit": 50, "prozent": 12, "roh": 30.0, "begrenzt": 25.0, "wirksam": 21.5}


def _beispiel(code):
    """Parameter, mit denen jede Vorlage des Codes gerendert werden kann."""
    return {name: _ZAHLEN.get(name, "2026-11-30" if name == "ende" else "x") for name in meldungen.TABELLE[code].params}


def test_die_stufe_kommt_aus_der_tabelle():
    assert meldungen.neu("meldung.notbetrieb").stufe == "rot"
    assert meldungen.neu("meldung.batterie_ok", bezeichnung="Bad").stufe == "orange"


def test_jede_stufe_der_tabelle_steht_im_vertrag():
    assert {vorlage.stufe for vorlage in meldungen.TABELLE.values()} <= {"rot", "orange", "gelb"}


@pytest.mark.parametrize(("name", "params"), [
    ("geraet_doppelt", {}), ("update_noetig", {}), ("gesperrt", {}), ("konfiguration_abgelehnt", {"grund": "ungueltig"}),
])
def test_die_hinweise_des_geraets_sind_codes_der_stufe_rot(name, params):
    meldung = meldungen.neu(f"meldung.{name}", **params)
    assert meldung.stufe == "rot" and meldungen.text(meldung)
    assert meldungen.aus_dict(meldungen.zu_dict(meldung)) == meldung


def test_jeder_schluessel_der_hinweise_hat_einen_code_in_der_tabelle():
    """Der Befehl hinweis nimmt nur Schluessel aus wire.MELDUNGEN an; jeder hat den Code meldung.<schluessel>."""
    from smartheat_device import wire
    assert all(meldungen.PREFIX + key in meldungen.TABELLE for key in wire.MELDUNGEN)


def test_ein_unbekannter_code_scheitert_laut():
    with pytest.raises(ValueError, match="Code"):
        meldungen.neu("meldung.gibt_es_nicht")


def test_ein_fehlender_oder_fremder_parameter_scheitert_laut():
    with pytest.raises(ValueError, match="Parameter"):
        meldungen.neu("meldung.batterie_ok")
    with pytest.raises(ValueError, match="Parameter"):
        meldungen.neu("meldung.batterie_ok", bezeichnung="Bad", fremd="x")


@pytest.mark.parametrize("wert", [True, math.nan, math.inf, ["x"], None, {"a": 1}])
def test_ein_parameter_ist_nur_text_oder_endliche_zahl(wert):
    with pytest.raises(ValueError, match="Parameter"):
        meldungen.neu("meldung.batterie_ok", bezeichnung=wert)


def test_ein_zu_langer_text_wird_gekuerzt():
    meldung = meldungen.neu("meldung.konfiguration_fehler", grund="g" * 5000)
    assert len(meldung.params["grund"]) == meldungen.MAX_TEXT


def test_die_stufe_muss_zur_tabelle_passen():
    with pytest.raises(ValueError, match="Stufe"):
        Meldung("meldung.notbetrieb", {}, "gelb")


@pytest.mark.parametrize("code", sorted(meldungen.TABELLE))
def test_jede_vorlage_laesst_sich_rendern(code):
    text = meldungen.text(meldungen.neu(code, **_beispiel(code)))
    assert text and "{" not in text and "}" not in text


def test_die_texte_bleiben_wie_bisher():
    assert meldungen.text(meldungen.neu("meldung.manueller_eingriff", was="Vorlauf")) == (
        "SmartHeat: Die Heizungseinstellung wurde in der App verstellt (Vorlauf) und zurückgesetzt. "
        "Einstellungen bitte nur über das Raumthermostat.")
    assert meldungen.text(meldungen.neu("meldung.schreibzaehler", anzahl=10000)) == (
        "SmartHeat: Bisher wurden 10.000 Werte in die Heizung geschrieben. Der Hersteller nennt eine Grenze von "
        "100.000 Schreibvorgängen auf Lebensdauer des Reglers; bitte den Fachbetrieb informieren.")
    assert meldungen.text(meldungen.neu("meldung.abo_inaktiv", ende="2026-11-30")) == (
        "SmartHeat: Abo inaktiv. Die Heizung läuft noch bis 30.11.2026 im Notbetrieb weiter, danach stellt es die "
        "ursprünglichen Werte wieder ein und beendet die Steuerung.")
    assert meldungen.text(meldungen.neu("meldung.schreibbudget_boost", limit=50)).endswith(
        " Ein Boost startet erst morgen wieder.")


def test_der_text_nennt_die_einzahl_und_die_mehrzahl_der_fuehler():
    eins = meldungen.text(meldungen.neu("meldung.raumfuehler_restmittel", fuehler="Bad", anzahl=1))
    zwei = meldungen.text(meldungen.neu("meldung.raumfuehler_restmittel", fuehler="Bad", anzahl=2))
    assert eins == "SmartHeat: Raumfühler Bad liefert keine Werte, Mittelwert aus 1 Fühler."
    assert zwei == "SmartHeat: Raumfühler Bad liefert keine Werte, Mittelwert aus 2 Fühlern."


def test_zahlen_stehen_mit_komma_im_text():
    text = meldungen.text(meldungen.neu("meldung.wunsch_begrenzt", roh=30.0, begrenzt=25.0))
    assert text == "Wunschtemperatur auf 30 °C gestellt, SmartHeat regelt auf 25 °C"
    text = meldungen.text(meldungen.neu("meldung.soll_quelle_aus", wirksam=21.5))
    assert "regelt weiter auf 21,5 °C" in text


def test_die_herkunft_des_wunsches_bestimmt_die_quelle_im_text():
    meldung = meldungen.neu("meldung.wunsch_begrenzt", roh=30.0, begrenzt=25.0, herkunft="thermostat")
    assert meldungen.text(meldung).startswith("Thermostat auf 30 °C gestellt")


def test_der_host_bestimmt_praefix_und_traeger():
    notbetrieb = meldungen.neu("meldung.notbetrieb")
    assert meldungen.text(notbetrieb, praefix="SmartHeat-Gateway").startswith("SmartHeat-Gateway: Server antwortet")
    voll = meldungen.neu("meldung.datentraeger_nicht_beschreibbar")
    assert "des Gateways" in meldungen.text(voll, traeger="des Gateways")
    assert "des Home-Assistant-Systems" in meldungen.text(voll)


def test_der_zusatz_haengt_den_hinweis_zur_anderen_anlage_an():
    ohne = meldungen.text(meldungen.neu("meldung.abmeldung_boost_aktiv", werte="Vorlauf 45"))
    mit = meldungen.text(meldungen.neu("meldung.abmeldung_boost_aktiv", werte="Vorlauf 45", zusatz="zustand_andere_anlage"))
    assert mit == ohne + " Der Zustand gehört zu einer anderen Anlage, es wurde nichts zurückgeschrieben (zustand_andere_anlage)."


def test_die_ablage_im_zustand_ist_umkehrbar():
    meldung = meldungen.neu("meldung.raumfuehler_restmittel", fuehler="Bad", anzahl=2)
    ablage = meldungen.zu_dict(meldung)
    assert ablage == {"code": "meldung.raumfuehler_restmittel", "params": {"fuehler": "Bad", "anzahl": 2},
                      "stufe": "orange"}
    assert meldungen.aus_dict(ablage) == meldung


@pytest.mark.parametrize("ablage", [
    None, "text", [], {},
    {"code": "meldung.notbetrieb", "params": {}},
    {"code": "meldung.gibt_es_nicht", "params": {}, "stufe": "rot"},
    {"code": "meldung.notbetrieb", "params": {}, "stufe": "gelb"},
    {"code": "meldung.notbetrieb", "params": {"x": 1}, "stufe": "rot"},
    {"code": "meldung.notbetrieb", "params": [], "stufe": "rot"},
    {"code": "meldung.notbetrieb", "params": {}, "stufe": "rot", "text": "alt"},
    {"code": 7, "params": {}, "stufe": "rot"},
])
def test_eine_unbrauchbare_ablage_gibt_none(ablage):
    assert meldungen.aus_dict(ablage) is None
