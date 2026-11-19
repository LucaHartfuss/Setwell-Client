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


# --- Rueckkehr: was das Geraet aus den Dokumenten rechnet, ohne Server und ohne Portal ---

def _zeit(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _soll(jetzt, aufheizdauer, **bedienung):
    """Das geltende Soll von Kreis 1 (Wunsch 21 Grad, Absenkung 3 K, Plan warm 06:30 und kuehl 22:30 Ortszeit)."""
    konfiguration = {"zeitzone": "Europe/Berlin",
                     "kreise": [{"kreis": 1, "absenkung_k": 3, "aufheizdauer_min": aufheizdauer}]}
    dokument = {"kreise": [{"kreis": 1, "modus": "manuell", "wochenplan": PLAN, **bedienung}]}
    eingabe = plan_eingabe(konfiguration, dokument, 1)
    erg = geltendes_soll(eingabe, 21.0, _zeit(jetzt), uhr_vertrauenswuerdig=True, soll_quelle_aus=False)
    return erg.soll, erg.soll_art, erg.naechster_wechsel


def test_rueckkehr_in_der_kuehlen_phase_uebersteuerung_endet_zum_aufheizbeginn():
    aufheizdauer = {"plan": 60, "weg": 0}
    ueber = {"temp": 20.0, "bis": "2026-11-03T04:30:00Z"}  # Aufheizbeginn: 05:30 Uhr UTC (warm) minus 60 Minuten
    assert _soll("2026-11-02T23:00:00Z", aufheizdauer, uebersteuerung=ueber) == (20.0, "uebersteuert",
                                                                                _zeit("2026-11-03T04:30:00Z"))
    assert _soll("2026-11-03T04:30:00Z", aufheizdauer, uebersteuerung=ueber) == (21.0, "aufheizen",
                                                                                _zeit("2026-11-03T05:30:00Z"))


def test_rueckkehr_aus_dem_leerstand_in_warmer_phase_heizt_vorher_auf_das_warme_soll():
    leerstand = {"von": "2026-11-01T00:00:00Z", "bis": "2026-11-20T10:00:00Z", "temp": 16.0, "aufheizen_ab": None}
    aufheizdauer = {"plan": 0, "weg": 120}
    kwargs = {"modus": "leerstand", "leerstand": leerstand}
    assert _soll("2026-11-20T07:59:59Z", aufheizdauer, **kwargs) == (16.0, "leerstand", _zeit("2026-11-20T08:00:00Z"))
    assert _soll("2026-11-20T08:00:00Z", aufheizdauer, **kwargs) == (21.0, "aufheizen", _zeit("2026-11-20T10:00:00Z"))
    assert _soll("2026-11-20T10:00:00Z", aufheizdauer, **kwargs) == (21.0, "wunsch", _zeit("2026-11-20T21:30:00Z"))


def test_rueckkehr_aus_dem_leerstand_in_kuehler_phase_heizt_nur_auf_die_absenktemperatur():
    leerstand = {"von": "2026-11-01T00:00:00Z", "bis": "2026-11-20T22:30:00Z", "temp": 16.0, "aufheizen_ab": None}
    kwargs = {"modus": "leerstand", "leerstand": leerstand}
    aufheizdauer = {"plan": 0, "weg": 120}
    assert _soll("2026-11-20T20:30:00Z", aufheizdauer, **kwargs) == (18.0, "aufheizen", _zeit("2026-11-20T22:30:00Z"))
    assert _soll("2026-11-20T22:30:00Z", aufheizdauer, **kwargs) == (18.0, "absenkung", _zeit("2026-11-21T05:30:00Z"))


def test_abwesend_endet_zur_gespeicherten_zeit_ohne_server_und_ohne_aufheizbeginn_vom_server():
    abwesend = {"bis": "2026-11-10T16:00:00Z", "temp": 17.0, "aufheizen_ab": None}
    kwargs = {"modus": "abwesend", "abwesend": abwesend}
    aufheizdauer = {"plan": 0, "weg": 0}
    assert _soll("2026-11-10T15:59:59Z", aufheizdauer, **kwargs) == (17.0, "abwesend", _zeit("2026-11-10T16:00:00Z"))
    assert _soll("2026-11-10T16:00:00Z", aufheizdauer, **kwargs) == (21.0, "wunsch", _zeit("2026-11-10T21:30:00Z"))


def test_uebersteuerung_im_leerstand_endet_nach_vier_stunden_und_laengere_gilt_nicht():
    leerstand = {"von": "2026-11-01T00:00:00Z", "bis": "2026-11-20T10:00:00Z", "temp": 16.0, "aufheizen_ab": None}
    aufheizdauer = {"plan": 0, "weg": 0}

    def im_leerstand(jetzt, bis):
        return _soll(jetzt, aufheizdauer, modus="leerstand", leerstand=leerstand,
                     uebersteuerung={"temp": 19.0, "bis": bis})

    assert im_leerstand("2026-11-05T12:00:00Z", "2026-11-05T16:00:00Z") == (19.0, "uebersteuert",
                                                                           _zeit("2026-11-05T16:00:00Z"))
    assert im_leerstand("2026-11-05T16:00:00Z", "2026-11-05T16:00:00Z") == (16.0, "leerstand",
                                                                           _zeit("2026-11-20T10:00:00Z"))
    assert im_leerstand("2026-11-05T12:00:00Z", "2026-11-05T16:00:01Z") == (16.0, "leerstand",
                                                                           _zeit("2026-11-20T10:00:00Z"))
