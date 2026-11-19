"""Was der Kreis dem Server als geltendes Soll meldet: nur mit Plan (Faehigkeit `programm`) und ausserhalb der
Testphase; sonst nichts Neues."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from smartheat_core import kreisansicht
from smartheat_core.plan import SollErgebnis
from smartheat_runtime import soll_meldung
from smartheat_runtime.plan_quelle import PlanEingabe, geltendes_soll

BERLIN = ZoneInfo("Europe/Berlin")
NACHTS = datetime(2026, 11, 2, 23, 0, tzinfo=BERLIN)  # kuehle Phase: Wunsch 21, Absenkung 3 K
KONFIGURATION = {"kreis": 1, "absenkung_k": 3.0, "aufheizdauer_min": {"plan": 0, "weg": 0}}
PLAN = [{"tag": tag, "warm_ab": "06:30", "kuehl_ab": "22:30"} for tag in range(1, 8)]


def _rt(soll, *, plan=True, zustand=kreisansicht.IN_BETRIEB):
    config = SimpleNamespace(plan_source=lambda: object() if plan else None, zustand_source=lambda: zustand)
    return SimpleNamespace(geltendes_soll=soll, config=config)


def _rechnen(bedienung, jetzt):
    modus = next((art for art in ("abwesend", "leerstand") if art in bedienung), "manuell")
    eingabe = PlanEingabe("Europe/Berlin", {"kreis": 1, "modus": modus, "wochenplan": PLAN, **bedienung}, KONFIGURATION)
    return geltendes_soll(eingabe, 21.0, jetzt, uhr_vertrauenswuerdig=True, soll_quelle_aus=False)


@pytest.mark.parametrize("bedienung, soll, art", [
    ({}, 18.0, "absenkung"),
    ({"abwesend": {"bis": "2026-11-09T10:00:00Z", "temp": 17.0, "aufheizen_ab": None}}, 17.0, "abwesend"),
    ({"leerstand": {"von": "2026-11-01T10:00:00Z", "bis": "2026-11-09T10:00:00Z", "temp": 16.0,
                    "aufheizen_ab": None}}, 16.0, "leerstand"),
    ({"uebersteuerung": {"temp": 23.0, "bis": "2026-11-03T04:30:00Z"}}, 23.0, "uebersteuert"),
], ids=["absenkung", "away", "leerstand", "uebersteuerung"])
def test_mit_plan_ist_das_gemeldete_das_geltende_soll_samt_art(bedienung, soll, art):
    ergebnis = _rechnen(bedienung, NACHTS)
    assert (ergebnis.soll, ergebnis.soll_art) == (soll, art)  # die Rechnung selbst, nicht der Wunsch

    assert soll_meldung.gemeldet(_rt(ergebnis)) == ergebnis


def test_ohne_geltendes_soll_wird_nichts_gemeldet():
    assert soll_meldung.gemeldet(_rt(None)) is None
    assert soll_meldung.gemeldet(_rt(SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None))) is None


def test_ohne_plan_bleibt_es_beim_heutigen_wert_ohne_neues_feld():
    assert soll_meldung.gemeldet(_rt(SollErgebnis(21.0, "wunsch", None), plan=False)) is None


def test_in_der_testphase_gilt_kein_plan():
    assert soll_meldung.gemeldet(_rt(SollErgebnis(21.0, "wunsch", None), zustand=kreisansicht.TESTPHASE_OHNE)) is None


def test_eine_kaputte_planquelle_meldet_nichts_und_wirft_nicht():
    def kaputt():
        raise RuntimeError("Dokument")

    rt = _rt(SollErgebnis(21.0, "wunsch", None))
    rt.config.plan_source = kaputt
    assert soll_meldung.gemeldet(rt) is None
