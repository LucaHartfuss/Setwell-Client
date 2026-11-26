"""Lokale Pruefung der Felder der Faehigkeit `programm` in der Konfiguration: je Kreis Aufheizart, Absenkung und
Aufheizdauer, anlagenweit die Zeitzone. Die Ablehnung nennt Feld und Regel, keinen Text."""
import copy

import pytest
from geraet_helpers import gateway_konfiguration
from rastung_beispiele import fuer

from smartheat_core import config_check as cc
from smartheat_device import fehler, wire

NAN, INF = float("nan"), float("inf")
KREIS_PROGRAMM = {"aufheizen": "boost", "absenkung_k": 2.0, "aufheizdauer_min": {"plan": 60, "weg": 120}}


def _geprueft(neu):
    ablehnung = cc.pruefen(neu, fuer(None, neu), host=cc.HOST_GATEWAY)
    if ablehnung is not None:
        assert not hasattr(ablehnung, "text") and fehler.gueltig(ablehnung.details), ablehnung
    return ablehnung


def _ungueltig(feld, regel="ungueltig", **zahlen):
    return cc.Ablehnung(cc.UNGUELTIG, cc.befund(feld, regel, **zahlen))


def _konfiguration(kreis=None, **anlagenweit):
    neu = copy.deepcopy(gateway_konfiguration())
    neu["kreise"][0].update(KREIS_PROGRAMM if kreis is None else kreis)
    neu.update(anlagenweit)
    return neu


def test_ein_kreis_mit_allen_programmfeldern_und_die_zeitzone_sind_gueltig():
    assert _geprueft(_konfiguration(zeitzone="Europe/Berlin")) is None


def test_ohne_programmfelder_bleibt_die_konfiguration_gueltig():
    assert _geprueft(gateway_konfiguration()) is None


def test_die_programmfelder_stehen_im_vertrag():
    assert wire.KONFIGURATION_KREIS_PROGRAMM == cc.KONFIG_KREIS_PROGRAMM
    assert wire.KONFIGURATION_PROGRAMM == cc.KONFIG_PROGRAMM == ("zeitzone",)
    for feld in (*wire.KONFIGURATION_PROGRAMM, *wire.KONFIGURATION_KREIS_PROGRAMM, *wire.AUFHEIZDAUER_MIN_FIELDS):
        assert feld in wire.PRUEF_FELDER, feld


# --- absenkung_k: 0 bis 5 K in Schritten von 0,5 K (Regel 4) ---

@pytest.mark.parametrize("tiefe", [0, 0.0, 0.5, 1, 2.5, 4.5, 5, 5.0])
def test_die_absenkung_gilt_von_0_bis_5_k_in_halben_schritten(tiefe):
    assert _geprueft(_konfiguration({**KREIS_PROGRAMM, "absenkung_k": tiefe})) is None


@pytest.mark.parametrize("tiefe", [5.5, 6, 30, -0.5, -1, 1e12])
def test_eine_absenkung_ausserhalb_von_0_bis_5_k_ist_ein_bereichsfehler(tiefe):
    ablehnung = _geprueft(_konfiguration({**KREIS_PROGRAMM, "absenkung_k": tiefe}))
    assert ablehnung == _ungueltig("absenkung_k", "bereich", min=0.0, max=5.0), tiefe


@pytest.mark.parametrize("tiefe", [0.25, 0.1, 2.75, 4.9])
def test_eine_absenkung_zwischen_den_halben_schritten_ist_ungueltig(tiefe):
    assert _geprueft(_konfiguration({**KREIS_PROGRAMM, "absenkung_k": tiefe})) == _ungueltig("absenkung_k"), tiefe


@pytest.mark.parametrize("tiefe", [NAN, INF, -INF, True, False, "2", None, [2], {}])
def test_eine_absenkung_ohne_endliche_zahl_ist_ungueltig(tiefe):
    assert _geprueft(_konfiguration({**KREIS_PROGRAMM, "absenkung_k": tiefe})) == _ungueltig("absenkung_k"), tiefe


# --- aufheizdauer_min: je Art (plan, weg) mit Obergrenze 12 h bzw. 72 h ---

@pytest.mark.parametrize("dauer", [
    {"plan": 0, "weg": 0}, {"plan": 720, "weg": 4320}, {"plan": 90.5, "weg": 1}, {"plan": 719, "weg": 4319},
])
def test_die_aufheizdauer_gilt_bis_zu_ihren_obergrenzen(dauer):
    assert _geprueft(_konfiguration({**KREIS_PROGRAMM, "aufheizdauer_min": dauer})) is None


@pytest.mark.parametrize(("dauer", "art", "grenze"), [
    ({"plan": 721, "weg": 0}, "plan", 720), ({"plan": 0, "weg": 4321}, "weg", 4320),
    ({"plan": -1, "weg": 0}, "plan", 720), ({"plan": 0, "weg": -0.5}, "weg", 4320),
])
def test_eine_aufheizdauer_ueber_der_obergrenze_oder_negativ_ist_ein_bereichsfehler(dauer, art, grenze):
    ablehnung = _geprueft(_konfiguration({**KREIS_PROGRAMM, "aufheizdauer_min": dauer}))
    assert ablehnung == _ungueltig("aufheizdauer_min", "bereich", min=0, max=grenze), (dauer, art)


@pytest.mark.parametrize("dauer", [
    None, [], "60", 60, {}, {"plan": 0}, {"weg": 0}, {"plan": 0, "weg": 0, "extra": 0}, {"plan": NAN, "weg": 0},
    {"plan": 0, "weg": INF}, {"plan": True, "weg": 0}, {"plan": 0, "weg": "5"}, {"plan": None, "weg": 0},
])
def test_eine_aufheizdauer_mit_falscher_form_ist_ungueltig(dauer):
    ablehnung = _geprueft(_konfiguration({**KREIS_PROGRAMM, "aufheizdauer_min": dauer}))
    assert ablehnung == _ungueltig("aufheizdauer_min"), dauer


# --- aufheizen ---

@pytest.mark.parametrize("art", ["boost", "kurve"])
def test_beide_aufheizarten_sind_gueltig(art):
    assert _geprueft(_konfiguration({**KREIS_PROGRAMM, "aufheizen": art})) is None


@pytest.mark.parametrize("art", ["sofort", "", None, 1, True, "Boost"])
def test_eine_unbekannte_aufheizart_wird_abgelehnt(art):
    assert _geprueft(_konfiguration({**KREIS_PROGRAMM, "aufheizen": art})) == _ungueltig("aufheizen", "unbekannt"), art


@pytest.mark.parametrize("fehlt", sorted(KREIS_PROGRAMM))
def test_die_programmfelder_eines_kreises_gelten_ganz_oder_gar_nicht(fehlt):
    kreis = {feld: wert for feld, wert in KREIS_PROGRAMM.items() if feld != fehlt}
    assert _geprueft(_konfiguration(kreis)) == _ungueltig(fehlt, "fehlt")


def test_ein_fremdes_kreisfeld_bleibt_abgelehnt():
    assert _geprueft(_konfiguration({**KREIS_PROGRAMM, "abwesend_temp_standard": 17.0})) == _ungueltig("kreise", "unbekannt")


# --- zeitzone ---

@pytest.mark.parametrize("zone", ["Europe/Berlin", "UTC", "America/New_York", "Asia/Kolkata"])
def test_eine_bekannte_zeitzone_ist_gueltig(zone):
    assert _geprueft(_konfiguration(zeitzone=zone)) is None


@pytest.mark.parametrize("zone", ["Mars/Base", "Europe/Berlinn", "europe/berlin", "Europe", "a/../b", "Europe/Berlin\x00", "+01:00"])
def test_eine_unbekannte_zeitzone_wird_mit_dem_code_abgelehnt(zone):
    assert _geprueft(_konfiguration(zeitzone=zone)) == _ungueltig("zeitzone", "unbekannt"), zone


@pytest.mark.parametrize("zone", ["", None, 5, True, ["Europe/Berlin"], "z" * (cc.TEXT_MAX_CHARS + 1)])
def test_eine_zeitzone_ohne_text_ist_ungueltig(zone):
    assert _geprueft(_konfiguration(zeitzone=zone)) == _ungueltig("zeitzone"), zone


def test_die_leere_konfiguration_nimmt_keine_oder_eine_bekannte_zeitzone():
    leer = {"anlage": None}
    assert _geprueft(leer) is None
    assert _geprueft({**leer, "zeitzone": None}) is None
    assert _geprueft({**leer, "zeitzone": "Europe/Berlin"}) is None
    assert _geprueft({**leer, "zeitzone": "Mars/Base"}) == _ungueltig("zeitzone", "unbekannt")
    assert _geprueft({**leer, "zeitzone": 5}) == _ungueltig("zeitzone")


def test_die_obergrenzen_der_aufheizdauer_sind_12_und_72_stunden():
    assert dict(zip(cc.AUFHEIZDAUER_ARTEN, cc.AUFHEIZDAUER_GRENZE, strict=True)) == {"plan": 720, "weg": 4320}
