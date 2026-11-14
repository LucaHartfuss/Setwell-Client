"""Push-Regeln im Konfigurationsdokument: das Geraet liest sie mit Typ und Bereich, Ungueltiges ist der Standard und lehnt
das Dokument nie ab (der Server schreibt nur Gueltiges; die Integration prueft ihrerseits)."""
import pytest
from geraet_helpers import ha_konfiguration
from rastung_beispiele import fuer

from smartheat_core import config_check as cc
from smartheat_device import wire

STANDARD = cc.PushRegeln(ruhe_von=22, ruhe_bis=7, tageslimit=5)


def test_der_standard_ist_ruhe_22_bis_7_und_fuenf_je_tag():
    assert cc.PUSH_STANDARD == STANDARD
    assert cc.PUSH_TAGESLIMIT == (1, 5) and cc.PUSH_STUNDEN == (0, 23)
    assert cc.push_regeln_lesen(None) == STANDARD


@pytest.mark.parametrize("wert, erwartet", [
    ({"ruhe_von": 23, "ruhe_bis": 6, "tageslimit": 3}, cc.PushRegeln(23, 6, 3)),
    ({"ruhe_von": 0, "ruhe_bis": 23, "tageslimit": 1}, cc.PushRegeln(0, 23, 1)),
    ({"ruhe_von": 8, "ruhe_bis": 20, "tageslimit": 5}, cc.PushRegeln(8, 20, 5)),
])
def test_gueltige_regeln_werden_gelesen(wert, erwartet):
    assert cc.push_regeln_lesen(wert) == erwartet


@pytest.mark.parametrize("wert", [
    {"ruhe_von": 22, "ruhe_bis": 22, "tageslimit": 5},          # Ruhe von = bis
    {"ruhe_von": 24, "ruhe_bis": 7, "tageslimit": 5},           # Stunde ausser Bereich
    {"ruhe_von": -1, "ruhe_bis": 7, "tageslimit": 5},
    {"ruhe_von": 22, "ruhe_bis": 24, "tageslimit": 5},
    {"ruhe_von": "22", "ruhe_bis": 7, "tageslimit": 5},         # falscher Typ
    {"ruhe_von": 22.0, "ruhe_bis": 7, "tageslimit": 5},
    {"ruhe_von": True, "ruhe_bis": 7, "tageslimit": 5},
    {"ruhe_von": 22, "ruhe_bis": None, "tageslimit": 5},
    {"ruhe_von": 22, "tageslimit": 5},                          # Feld fehlt
    {},
])
def test_ungueltige_ruhezeit_ist_der_standard_ohne_die_uebrigen_werte_zu_verwerfen(wert):
    gelesen = cc.push_regeln_lesen(wert)
    assert (gelesen.ruhe_von, gelesen.ruhe_bis) == (22, 7)
    assert gelesen.tageslimit == (wert.get("tageslimit") if wert.get("tageslimit") else 5)


@pytest.mark.parametrize("limit", [0, 6, 50, -1, "3", 3.0, True, None])
def test_ungueltiges_tageslimit_ist_der_standard_ohne_die_ruhezeit_zu_verwerfen(limit):
    gelesen = cc.push_regeln_lesen({"ruhe_von": 23, "ruhe_bis": 6, "tageslimit": limit})
    assert gelesen == cc.PushRegeln(23, 6, 5)


@pytest.mark.parametrize("wert", ["x", 5, [1, 2, 3], True, 1.5])
def test_kein_objekt_ist_der_standard(wert):
    assert cc.push_regeln_lesen(wert) == STANDARD


def test_die_felder_stehen_im_vertrag_und_im_kern_gleich():
    assert wire.PUSH_REGELN_FIELDS == cc.PUSH_REGELN_FELDER == ("ruhe_von", "ruhe_bis", "tageslimit")
    assert "push_regeln" in wire.KONFIGURATION_FIELDS


@pytest.mark.parametrize("wert", [None, {"ruhe_von": 22, "ruhe_bis": 22, "tageslimit": 99}, "kaputt", [], 7])
def test_ein_dokument_mit_ungueltigen_push_regeln_wird_nicht_abgelehnt(wert):
    konfiguration = {**ha_konfiguration(), "push_regeln": wert}
    assert cc.pruefen(konfiguration, fuer(None, konfiguration), host=cc.HOST_HA,
                      treiber_pruefen=lambda k: None) is None
