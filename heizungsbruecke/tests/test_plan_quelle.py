"""Eingabe des geltenden Solls aus den Dokumenten und die Rechnung mit Rueckfall auf den Wunsch (plan_quelle.py)."""
import logging
from datetime import UTC, datetime

import pytest

from smartheat_core.plan import SollErgebnis
from smartheat_runtime.plan_quelle import PlanEingabe, eingabe_des_geraets, geltendes_soll, plan_eingabe

JETZT = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
PLAN = [{"tag": tag, "warm_ab": "06:30", "kuehl_ab": "22:30"} for tag in range(1, 8)]


def _konfiguration(zeitzone="Europe/Berlin", **kreis):
    return {"zeitzone": zeitzone, "kreise": [{"kreis": 2, "aufheizen": "boost", "absenkung_k": 3}, {"kreis": 1, **kreis}]}


def _bedienung(**kreis):
    return {"kreise": [{"kreis": 1, "modus": "manuell", "raum_soll": 20.0}, {"kreis": 2, "modus": "manuell", **kreis}]}


def test_die_eingabe_nimmt_je_kreis_den_eigenen_eintrag_und_die_zone_der_anlage():
    eingabe = plan_eingabe(_konfiguration(), _bedienung(raum_soll=22.0), 2)

    assert eingabe == PlanEingabe(
        zone="Europe/Berlin",
        bedienung={"kreis": 2, "modus": "manuell", "raum_soll": 22.0},
        konfiguration={"kreis": 2, "aufheizen": "boost", "absenkung_k": 3},
    )


@pytest.mark.parametrize("konfiguration", [
    {"kreise": [{"kreis": 1}]},  # keine Zone: ein Dokument vor der Betriebsart
    _konfiguration(zeitzone=""), _konfiguration(zeitzone=None), _konfiguration(zeitzone=3),
    {"zeitzone": "Europe/Berlin", "kreise": [{"kreis": 7}]},  # der Kreis fehlt
    {"zeitzone": "Europe/Berlin", "kreise": [{"kreis": True}]},
    {"zeitzone": "Europe/Berlin"}, None, [], "x",
])
def test_ohne_zone_oder_kreis_gibt_es_keine_eingabe(konfiguration):
    assert plan_eingabe(konfiguration, _bedienung(), 1) is None


@pytest.mark.parametrize("bedienung", [None, {}, {"kreise": []}, {"kreise": "x"}, {"kreise": [{"kreis": 5}]},
                                       {"kreise": [{"kreis": True, "raum_soll": 20.0}]}, []])
def test_ohne_bedienung_des_kreises_gibt_es_keine_eingabe(bedienung):
    assert plan_eingabe(_konfiguration(), bedienung, 1) is None


def test_ohne_plan_ist_das_soll_der_wunsch_und_ohne_soll_quelle_kein_soll():
    assert geltendes_soll(None, 21.0, JETZT, uhr_vertrauenswuerdig=True, soll_quelle_aus=False) == \
        SollErgebnis(21.0, "wunsch", None)
    assert geltendes_soll(None, 21.0, JETZT, uhr_vertrauenswuerdig=True, soll_quelle_aus=True) == \
        SollErgebnis(None, None, None)


def test_mit_plan_rechnet_die_funktion_und_nimmt_den_gemerkten_wunsch_statt_des_dokuments():
    eingabe = plan_eingabe(_konfiguration(absenkung_k=3), _bedienung(raum_soll=17.0, wochenplan=PLAN), 2)
    assert eingabe is not None

    # 23:30 UTC ist in Berlin nach 22:30: kuehle Phase; `raum_soll` des Dokuments (17) zaehlt nicht, der Wunsch 21 schon.
    erg = geltendes_soll(eingabe, 21.0, JETZT, uhr_vertrauenswuerdig=True, soll_quelle_aus=False)

    assert (erg.soll, erg.soll_art) == (18.0, "absenkung")


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", None])
def test_eine_unbrauchbare_eingabe_faellt_auf_den_wunsch_zurueck_ohne_werte_im_log(zone, caplog):
    eingabe = PlanEingabe(zone=zone, bedienung={"kreis": 1, "modus": "manuell", "raum_soll": 21.0}, konfiguration={})

    with caplog.at_level(logging.DEBUG, logger="smartheat_runtime.plan_quelle"):
        erg = geltendes_soll(eingabe, 21.0, JETZT, uhr_vertrauenswuerdig=True, soll_quelle_aus=False)

    assert erg == SollErgebnis(21.0, "wunsch", None)
    assert "Mars" not in caplog.text and "ValueError" in caplog.text


def test_bei_soll_quelle_aus_gibt_es_auch_mit_plan_kein_soll():
    eingabe = plan_eingabe(_konfiguration(), _bedienung(wochenplan=PLAN), 2)

    assert geltendes_soll(eingabe, 21.0, JETZT, uhr_vertrauenswuerdig=True, soll_quelle_aus=True) == \
        SollErgebnis(None, None, None)


def test_die_eingabe_des_geraets_liest_beide_dokumente_bei_jedem_aufruf():
    class Geraet:
        konfig, bedien = _konfiguration(), (_bedienung(raum_soll=22.0), False)

        def konfiguration(self):
            return self.konfig

        def bedienung(self):
            return self.bedien

    geraet = Geraet()

    assert eingabe_des_geraets(geraet, 2).bedienung["raum_soll"] == 22.0
    geraet.bedien = (_bedienung(raum_soll=19.0), True)
    assert eingabe_des_geraets(geraet, 2).bedienung["raum_soll"] == 19.0
    geraet.bedien = (None, False)
    assert eingabe_des_geraets(geraet, 2) is None
