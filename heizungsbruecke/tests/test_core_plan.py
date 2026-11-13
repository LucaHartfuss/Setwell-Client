import ast
import copy
import dataclasses
import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from smartheat_core import plan, wallclock
from smartheat_core.plan import SollErgebnis, anstieg_vom_kunden, soll_effektiv, wochenplan_fehler

JETZT = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ZONE = "Europe/Berlin"
KONFIGURATION: dict = {}


def bedienung(raum_soll=21.0):
    return {"kreis": 1, "modus": "manuell", "raum_soll": raum_soll}


def test_ohne_plan_ist_das_soll_der_gemerkte_wunsch():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(21.5), KONFIGURATION)
    assert ergebnis == SollErgebnis(soll=21.5, soll_art="wunsch", naechster_wechsel=None)


def test_ergebnis_ist_unveraenderlich():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ergebnis.soll = 5.0  # type: ignore[misc]


@pytest.mark.parametrize("wunsch", [5.0, 35.0, 5, 35, 4.9, 35.1])
def test_wunsch_am_rand_wird_unveraendert_durchgereicht(wunsch):
    # Die Grenzen des Raumbereichs setzt der Wunsch-Eingang; hier wird nichts begrenzt.
    assert soll_effektiv(JETZT, ZONE, bedienung(wunsch), KONFIGURATION).soll == wunsch


def test_soll_quelle_aus_oder_frostschutz_ergibt_kein_soll():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(21.5), KONFIGURATION, soll_quelle_aus=True)
    assert ergebnis == SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None)


def test_soll_quelle_an_ist_der_standard():
    assert soll_effektiv(JETZT, ZONE, bedienung(20.0), KONFIGURATION, soll_quelle_aus=False).soll == 20.0


def test_naive_jetzt_wird_abgelehnt():
    with pytest.raises(ValueError, match="^jetzt braucht eine Zeitzone$"):
        soll_effektiv(datetime(2026, 11, 2, 12, 0), ZONE, bedienung(), KONFIGURATION)


@pytest.mark.parametrize("jetzt", ["2026-11-02T12:00:00+00:00", None, 1.0])
def test_jetzt_ohne_datum_wird_abgelehnt(jetzt):
    with pytest.raises(ValueError, match="^jetzt braucht eine Zeitzone$"):
        soll_effektiv(jetzt, ZONE, bedienung(), KONFIGURATION)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", "../etc/passwd", "Europe/Berlin\x00", None, 3, "/etc"])
def test_ungueltige_zone_wird_abgelehnt_ohne_den_namen_zu_nennen(zone):
    with pytest.raises(ValueError, match="^zone ist keine bekannte Zeitzone$") as fehler:
        soll_effektiv(JETZT, zone, bedienung(), KONFIGURATION)
    assert "Mars" not in str(fehler.value)


@pytest.mark.parametrize("wunsch", [float("nan"), float("inf"), True, None, "21", [21.0]])
def test_unbrauchbarer_wunsch_wird_abgelehnt_ohne_den_wert_zu_nennen(wunsch):
    with pytest.raises(ValueError, match="^bedienung hat keinen brauchbaren Wunsch$"):
        soll_effektiv(JETZT, ZONE, bedienung(wunsch), KONFIGURATION)


@pytest.mark.parametrize("doc", [None, [], "x", {"kreis": 1}])
def test_bedienung_ohne_wunsch_wird_abgelehnt(doc):
    with pytest.raises(ValueError, match="^bedienung hat keinen brauchbaren Wunsch$"):
        soll_effektiv(JETZT, ZONE, doc, KONFIGURATION)


@pytest.mark.parametrize("konfiguration", [None, [], "x"])
def test_konfiguration_ohne_objekt_wird_abgelehnt(konfiguration):
    with pytest.raises(ValueError, match="^konfiguration ist kein Objekt$"):
        soll_effektiv(JETZT, ZONE, bedienung(), konfiguration)


def test_die_rechnung_ist_rein_und_wiederholbar(monkeypatch):
    # Keine Uhr: die Wanduhr des Clients wird nicht gelesen, das Ergebnis haengt nur von den Eingaben ab.
    def verboten():
        raise AssertionError("Uhr gelesen")

    monkeypatch.setattr(wallclock, "_now", verboten)
    eingabe = bedienung(19.0)
    erstes = soll_effektiv(JETZT, ZONE, eingabe, KONFIGURATION)
    assert soll_effektiv(JETZT, ZONE, eingabe, KONFIGURATION) == erstes
    assert eingabe == bedienung(19.0)


def test_die_zeitzone_des_hosts_aendert_nichts(monkeypatch):
    import time

    erstes = soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION)
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    try:
        assert soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION) == erstes
    finally:
        monkeypatch.undo()
        time.tzset()


def test_modul_importiert_nur_reine_bausteine_und_oeffnet_keine_dateien():
    baum = ast.parse(pathlib.Path(plan.__file__).read_text(encoding="utf-8"))
    importiert = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            importiert |= {a.name.split(".")[0] for a in knoten.names}
        elif isinstance(knoten, ast.ImportFrom) and knoten.module:
            importiert.add(knoten.module.split(".")[0])
    assert importiert <= {"__future__", "dataclasses", "datetime", "collections", "zoneinfo", "smartheat_core"}
    aufgerufen = {k.func.id for k in ast.walk(baum) if isinstance(k, ast.Call) and isinstance(k.func, ast.Name)}
    assert not aufgerufen & {"open", "print", "exec", "eval", "__import__"}
    assert "wallclock" not in importiert and "time" not in importiert


# --- Plan, Abwesenheit, Leerstand --------------------------------------------------------------------------------

DATEN = pathlib.Path(__file__).parent / "daten" / "plan_rechnung.json"
NACHT = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)  # Berlin: 23:00 Winterzeit, im Plan "alltag" kuehl
KONF = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 0, "weg": 0}}


def utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def text(zeitpunkt):
    return zeitpunkt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wochenplan(warm="06:30", kuehl="22:30"):
    return [{"tag": tag, "warm_ab": warm, "kuehl_ab": kuehl} for tag in range(1, 8)]


def doc(modus="manuell", raum_soll=21.0, **teile):
    return {"kreis": 1, "modus": modus, "raum_soll": raum_soll, **teile}


def leerstand(von="2026-11-01T00:00:00Z", bis="2026-11-20T10:00:00Z", temp=16.0, aufheizen_ab=None):
    return {"von": von, "bis": bis, "temp": temp, "aufheizen_ab": aufheizen_ab}


def abwesend(bis="2026-11-10T16:00:00Z", temp=17.0, aufheizen_ab=None):
    return {"bis": bis, "temp": temp, "aufheizen_ab": aufheizen_ab}


def ergebnis(jetzt, bedienung, konfiguration=KONF, zone=ZONE):
    return soll_effektiv(jetzt, zone, bedienung, konfiguration)


def paar(erg):
    return erg.soll, erg.soll_art


def lade_fixture():
    return json.loads(DATEN.read_text(encoding="utf-8"))


def fall_eingabe(fall, plaene):
    bedienung = doc(fall.get("modus", "manuell"))
    if "plan" in fall:
        bedienung["wochenplan"] = plaene[fall["plan"]]
    for teil in ("abwesend", "leerstand", "uebersteuerung"):
        if teil in fall:
            bedienung[teil] = fall[teil]
    konfiguration = {"absenkung_k": fall.get("absenkung_k", 3),
                     "aufheizdauer_min": fall.get("aufheizdauer_min", {"plan": 0, "weg": 0})}
    return bedienung, konfiguration


FIXTURE = lade_fixture()
FAELLE = [pytest.param(fall, id=fall["name"]) for fall in FIXTURE["faelle"]]


@pytest.mark.parametrize("fall", FAELLE)
def test_fixture_rechnung_von_hand(fall):
    bedienung, konfiguration = fall_eingabe(fall, FIXTURE["plaene"])
    erg = ergebnis(utc(fall["jetzt"]), bedienung, konfiguration, fall["zone"])
    erwartet = fall["erwartet"]
    assert paar(erg) == (erwartet["soll"], erwartet["soll_art"])
    if erwartet["naechster_wechsel"] is None:
        assert erg.naechster_wechsel is None
    else:
        assert erg.naechster_wechsel == utc(erwartet["naechster_wechsel"])
        assert erg.naechster_wechsel.utcoffset() == timedelta(0)


@pytest.mark.parametrize("fall", FAELLE)
def test_fixture_der_naechste_wechsel_ist_der_erste_wechsel(fall):
    bedienung, konfiguration = fall_eingabe(fall, FIXTURE["plaene"])
    jetzt = utc(fall["jetzt"])
    erg = ergebnis(jetzt, bedienung, konfiguration, fall["zone"])
    if erg.naechster_wechsel is None:
        return
    davor = ergebnis(erg.naechster_wechsel - timedelta(seconds=1), bedienung, konfiguration, fall["zone"])
    danach = ergebnis(erg.naechster_wechsel, bedienung, konfiguration, fall["zone"])
    assert paar(davor) == paar(erg)
    assert paar(danach) != paar(erg)


def test_ohne_plan_und_ohne_ueberlagerung_bleibt_das_heutige_ergebnis():
    for bedienung in (doc(), doc(wochenplan=None), doc(abwesend=None, leerstand=None, uebersteuerung=None)):
        assert ergebnis(NACHT, bedienung) == SollErgebnis(21.0, "wunsch", None)
    assert ergebnis(NACHT, doc(), {}) == SollErgebnis(21.0, "wunsch", None)


def test_plan_warm_ist_der_wunsch_und_kuehl_die_absenkung():
    bedienung = doc(wochenplan=wochenplan())
    assert paar(ergebnis(datetime(2026, 11, 2, 12, 0, tzinfo=UTC), bedienung)) == (21.0, "wunsch")
    assert paar(ergebnis(NACHT, bedienung)) == (18.0, "absenkung")


@pytest.mark.parametrize(("wunsch", "tiefe", "erwartet"), [
    (21.0, 3, 18.0), (21.0, 5, 16.0), (21.0, 10, 16.0), (17.0, 5, 16.0), (16.0, 3, 16.0), (15.0, 3, 15.0),
    (12.0, 3, 12.0), (25.0, 3, 22.0), (21.5, 0.5, 21.0),
])
def test_absenkung_geht_nie_unter_die_untergrenze_und_hebt_nie_an(wunsch, tiefe, erwartet):
    konfiguration = {"absenkung_k": tiefe, "aufheizdauer_min": {"plan": 0, "weg": 0}}
    erg = ergebnis(NACHT, doc(raum_soll=wunsch, wochenplan=wochenplan()), konfiguration)
    assert erg.soll == erwartet


@pytest.mark.parametrize("tiefe", [-1, float("nan"), float("inf"), True, "3", None, [3]])
def test_unbrauchbare_absenkung_heisst_keine_absenkung(tiefe):
    konfiguration = {"absenkung_k": tiefe, "aufheizdauer_min": {"plan": 0, "weg": 0}}
    assert ergebnis(NACHT, doc(wochenplan=wochenplan()), konfiguration) == SollErgebnis(21.0, "wunsch", None)


@pytest.mark.parametrize("aufheizdauer", [{"plan": -5, "weg": 0}, {"plan": "x", "weg": 0}, {"plan": True, "weg": 0},
                                          {"plan": 10**12, "weg": 0}, "x", [1]])
def test_unbrauchbare_aufheizdauer_des_plans_heisst_keine_absenkung(aufheizdauer):
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": aufheizdauer}
    assert paar(ergebnis(NACHT, doc(wochenplan=wochenplan()), konfiguration)) == (21.0, "wunsch")


def test_fehlende_absenkung_und_aufheizdauer_gelten_als_null():
    assert paar(ergebnis(NACHT, doc(wochenplan=wochenplan()), {"aufheizdauer_min": None})) == (21.0, "wunsch")
    assert paar(ergebnis(NACHT, doc(wochenplan=wochenplan()), {"absenkung_k": 3})) == (18.0, "absenkung")


def test_leerstand_geht_vor_abwesend_und_plan():
    bedienung = doc("leerstand", wochenplan=wochenplan(), abwesend=abwesend(), leerstand=leerstand())
    assert ergebnis(NACHT, bedienung).soll == 16.0
    assert ergebnis(NACHT, bedienung).soll_art == "leerstand"


def test_abwesend_geht_vor_dem_plan():
    erg = ergebnis(datetime(2026, 11, 9, 12, 0, tzinfo=UTC), doc("abwesend", wochenplan=wochenplan(), abwesend=abwesend()))
    assert paar(erg) == (17.0, "abwesend")


def test_ein_teil_ohne_passenden_modus_gilt_nicht():
    jetzt = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    assert paar(ergebnis(jetzt, doc("manuell", leerstand=leerstand(), abwesend=abwesend()))) == (21.0, "wunsch")
    assert paar(ergebnis(jetzt, doc("abwesend", leerstand=leerstand()))) == (21.0, "wunsch")
    assert paar(ergebnis(jetzt, doc("leerstand", abwesend=abwesend()))) == (21.0, "wunsch")


def test_leerstand_gilt_nur_im_fenster_von_bis():
    bedienung = doc("leerstand", leerstand=leerstand(von="2026-11-05T00:00:00Z", bis="2026-11-06T00:00:00Z"))
    assert ergebnis(utc("2026-11-04T23:59:59Z"), bedienung).soll_art == "wunsch"
    assert ergebnis(utc("2026-11-05T00:00:00Z"), bedienung).soll_art == "leerstand"
    assert ergebnis(utc("2026-11-05T23:59:59Z"), bedienung).soll_art == "leerstand"
    assert ergebnis(utc("2026-11-06T00:00:00Z"), bedienung).soll_art == "wunsch"


@pytest.mark.parametrize("fenster", [
    leerstand(temp=14.9), leerstand(temp=None), leerstand(temp="16"), leerstand(temp=float("nan")),
    leerstand(temp=True), leerstand(bis="2026-11-01T00:00:00Z"), leerstand(bis="2026-10-30T00:00:00Z"),
    leerstand(von="2025-11-01T00:00:00Z", bis="2026-11-20T10:00:00Z"), leerstand(von="2026-11-01T00:00:00"),
    leerstand(von="gestern"), leerstand(bis=None), leerstand(bis="2026-11-20 10:00:00"), "Leerstand", [], 5,
])
def test_ungueltiger_leerstand_wird_wie_nicht_gesetzt_behandelt(fenster):
    erg = ergebnis(utc("2026-11-05T12:00:00Z"), doc("leerstand", leerstand=fenster, wochenplan=wochenplan()))
    assert erg.soll_art in {"wunsch", "absenkung"}


def test_leerstand_an_der_untergrenze_und_hoechstdauer_gilt():
    von = utc("2026-11-01T00:00:00Z")
    am_rand = leerstand(temp=15.0, bis=text(von + timedelta(days=365)))
    assert paar(ergebnis(utc("2026-11-05T12:00:00Z"), doc("leerstand", leerstand=am_rand))) == (15.0, "leerstand")
    zu_lang = leerstand(temp=15.0, bis=text(von + timedelta(days=365, seconds=1)))
    assert ergebnis(utc("2026-11-05T12:00:00Z"), doc("leerstand", leerstand=zu_lang)).soll_art == "wunsch"


def test_zeiten_als_text_und_als_datum_rechnen_gleich():
    als_text = doc("leerstand", leerstand=leerstand())
    als_datum = doc("leerstand", leerstand=leerstand(von=utc("2026-11-01T00:00:00Z"), bis=utc("2026-11-20T10:00:00Z")))
    jetzt = utc("2026-11-05T12:00:00Z")
    assert ergebnis(jetzt, als_text) == ergebnis(jetzt, als_datum)
    naiv = doc("leerstand", leerstand=leerstand(von=datetime(2026, 11, 1)))
    assert ergebnis(jetzt, naiv).soll_art == "wunsch"


def test_abwesend_gilt_bis_zum_ende_und_braucht_eine_gueltige_temperatur():
    jetzt = utc("2026-11-10T15:59:59Z")
    assert paar(ergebnis(jetzt, doc("abwesend", abwesend=abwesend()))) == (17.0, "abwesend")
    assert paar(ergebnis(utc("2026-11-10T16:00:00Z"), doc("abwesend", abwesend=abwesend()))) == (21.0, "wunsch")
    assert paar(ergebnis(jetzt, doc("abwesend", abwesend=abwesend(temp=15.0)))) == (15.0, "abwesend")
    for ungueltig in (abwesend(temp=14.9), abwesend(temp=None), abwesend(temp="17"), abwesend(bis=None),
                      abwesend(bis="morgen"), {"bis": "2026-11-10T16:00:00Z"}, "abwesend", None):
        assert ergebnis(jetzt, doc("abwesend", abwesend=ungueltig)).soll_art == "wunsch"


def test_aufheizen_vor_dem_ende_nimmt_das_soll_des_plans_zum_ende():
    bedienung = doc("abwesend", wochenplan=wochenplan(), abwesend=abwesend(bis="2026-11-10T22:00:00Z"))
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 0, "weg": 120}}
    erg = ergebnis(utc("2026-11-10T20:00:00Z"), bedienung, konfiguration)
    assert paar(erg) == (18.0, "aufheizen")
    assert erg.naechster_wechsel == utc("2026-11-10T22:00:00Z")
    erg = ergebnis(utc("2026-11-10T19:59:59Z"), bedienung, konfiguration)
    assert paar(erg) == (17.0, "abwesend")
    assert erg.naechster_wechsel == utc("2026-11-10T20:00:00Z")


def test_aufheizen_ab_vom_server_geht_der_aufheizdauer_vor():
    bedienung = doc("abwesend", abwesend=abwesend(aufheizen_ab="2026-11-10T14:00:00Z"))
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 0, "weg": 60}}
    assert ergebnis(utc("2026-11-10T13:59:59Z"), bedienung, konfiguration).soll_art == "abwesend"
    assert ergebnis(utc("2026-11-10T14:00:00Z"), bedienung, konfiguration).soll_art == "aufheizen"
    ohne = doc("abwesend", abwesend=abwesend())
    assert ergebnis(utc("2026-11-10T14:00:00Z"), ohne, konfiguration).soll_art == "abwesend"
    assert ergebnis(utc("2026-11-10T15:00:00Z"), ohne, konfiguration).soll_art == "aufheizen"


@pytest.mark.parametrize("weg", [-1, "x", None, True, float("nan"), 10**12])
def test_unbrauchbare_aufheizdauer_weg_heisst_kein_aufheizen(weg):
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 0, "weg": weg}}
    erg = ergebnis(utc("2026-11-10T15:59:59Z"), doc("abwesend", abwesend=abwesend()), konfiguration)
    assert paar(erg) == (17.0, "abwesend")
    assert erg.naechster_wechsel == utc("2026-11-10T16:00:00Z")


def test_uebersteuerung_gilt_nur_in_der_kuehlen_phase_oder_im_leerstand():
    kuehl = utc("2026-11-02T23:00:00Z")
    warm = utc("2026-11-02T12:00:00Z")
    ueber = {"temp": 20.0, "bis": "2026-11-03T05:00:00Z"}
    assert paar(ergebnis(kuehl, doc(wochenplan=wochenplan(), uebersteuerung=ueber))) == (20.0, "uebersteuert")
    assert ergebnis(warm, doc(wochenplan=wochenplan(), uebersteuerung={"temp": 20.0, "bis": "2026-11-02T14:00:00Z"})
                    ).soll_art == "wunsch"
    assert ergebnis(kuehl, doc(uebersteuerung=ueber)).soll_art == "wunsch"  # ohne Plan keine Absenkung zu uebersteuern
    assert ergebnis(utc("2026-11-09T12:00:00Z"), doc("abwesend", abwesend=abwesend(),
                                                     uebersteuerung={"temp": 20.0, "bis": "2026-11-09T14:00:00Z"})
                    ).soll_art == "abwesend"


@pytest.mark.parametrize("ueber", [
    {"temp": 14.9, "bis": "2026-11-03T05:00:00Z"}, {"temp": None, "bis": "2026-11-03T05:00:00Z"},
    {"temp": 20.0, "bis": None}, {"temp": 20.0, "bis": "2026-11-02T22:59:59Z"}, {"temp": 20.0}, "x", [],
    {"temp": 20.0, "bis": "2026-11-03T05:30:01Z"},
])
def test_ungueltige_oder_abgelaufene_uebersteuerung_gilt_nicht(ueber):
    erg = ergebnis(utc("2026-11-02T23:00:00Z"), doc(wochenplan=wochenplan(), uebersteuerung=ueber))
    assert paar(erg) == (18.0, "absenkung")


def test_uebersteuerung_endet_vor_dem_aufheizbeginn():
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 60, "weg": 0}}
    ueber = {"temp": 20.0, "bis": "2026-11-03T05:30:00Z"}  # Ende der kuehlen Phase, aber nach dem Aufheizbeginn
    assert paar(ergebnis(utc("2026-11-02T23:00:00Z"), doc(wochenplan=wochenplan(), uebersteuerung=ueber),
                         konfiguration)) == (18.0, "absenkung")
    ueber = {"temp": 20.0, "bis": "2026-11-03T04:30:00Z"}
    erg = ergebnis(utc("2026-11-02T23:00:00Z"), doc(wochenplan=wochenplan(), uebersteuerung=ueber), konfiguration)
    assert paar(erg) == (20.0, "uebersteuert")
    assert erg.naechster_wechsel == utc("2026-11-03T04:30:00Z")


def test_uebersteuerung_im_leerstand_endet_mit_dem_leerstand():
    bedienung = doc("leerstand", leerstand=leerstand(bis="2026-11-05T16:00:00Z"),
                    uebersteuerung={"temp": 19.0, "bis": "2026-11-06T00:00:00Z"})
    assert ergebnis(utc("2026-11-05T12:00:00Z"), bedienung).soll_art == "leerstand"


def test_ungueltiger_wochenplan_wird_wie_kein_plan_behandelt():
    ohne = ergebnis(NACHT, doc())
    for kaputt in (wochenplan()[:6], wochenplan("06:30", "06:30"), wochenplan("24:00", "22:30"), "Plan", [], {}, 7):
        assert ergebnis(NACHT, doc(wochenplan=kaputt)) == ohne


def test_ungueltiger_wochenplan_nimmt_nur_den_plan_weg_nicht_die_ueberlagerung():
    kaputt = wochenplan()[:6]
    assert paar(ergebnis(utc("2026-11-05T12:00:00Z"), doc("leerstand", wochenplan=kaputt, leerstand=leerstand()))) == (
        16.0, "leerstand")


def test_unbekannte_zone_wird_auch_mit_plan_abgelehnt():
    with pytest.raises(ValueError, match="^zone ist keine bekannte Zeitzone$"):
        ergebnis(NACHT, doc(wochenplan=wochenplan()), zone="Mars/Olympus")


def test_die_eingaben_bleiben_unveraendert():
    bedienung = doc("leerstand", wochenplan=wochenplan(), abwesend=abwesend(), leerstand=leerstand(),
                    uebersteuerung={"temp": 20.0, "bis": "2026-11-05T14:00:00Z"})
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 30, "weg": 30}}
    vorher = copy.deepcopy((bedienung, konfiguration))
    ergebnis(utc("2026-11-05T12:00:00Z"), bedienung, konfiguration)
    assert (bedienung, konfiguration) == vorher


def test_plan_ohne_aenderung_hat_keinen_naechsten_wechsel():
    konfiguration = {"absenkung_k": 0, "aufheizdauer_min": {"plan": 0, "weg": 0}}
    assert ergebnis(NACHT, doc(wochenplan=wochenplan()), konfiguration) == SollErgebnis(21.0, "wunsch", None)


def test_wochenende_und_wochengrenze_nutzen_den_eintrag_des_tages():
    plan_doc = wochenplan()
    plan_doc[6] = {"tag": 7, "warm_ab": "09:00", "kuehl_ab": "21:00"}  # Sonntag spaeter warm
    sonntag_morgen = utc("2026-11-08T07:30:00Z")  # Sonntag 08:30 Berlin
    assert paar(ergebnis(sonntag_morgen, doc(wochenplan=plan_doc))) == (18.0, "absenkung")
    montag_morgen = utc("2026-11-09T07:30:00Z")  # Montag 08:30 Berlin
    assert paar(ergebnis(montag_morgen, doc(wochenplan=plan_doc))) == (21.0, "wunsch")


# --- Form des Wochenplans -------------------------------------------------------------------------------------


def mit(eintraege, zeile, **felder):
    ergebnis_liste = copy.deepcopy(eintraege)
    ergebnis_liste[zeile - 1].update(felder)
    return ergebnis_liste


def wochenplan_aus(zeiten):
    return [{"tag": i + 1, "warm_ab": w, "kuehl_ab": k} for i, (w, k) in enumerate(zeiten)]


def test_gueltige_wochenplaene_haben_keinen_fehler():
    assert wochenplan_fehler(wochenplan()) is None
    assert wochenplan_fehler(wochenplan("07:00", "02:30")) is None  # Nachtschicht, kuehl am Folgetag
    assert wochenplan_fehler(wochenplan("06:30", "07:30")) is None  # genau 60 Minuten warm
    assert wochenplan_fehler(wochenplan("00:00", "23:00")) is None
    assert wochenplan_fehler(wochenplan("23:59", "22:59")) is None  # 60 Minuten kuehl ueber Mitternacht
    verschieden = wochenplan_aus([("06:00", "08:00"), ("07:00", "22:00"), ("06:00", "08:00"), ("07:00", "22:00"),
                                  ("06:00", "08:00"), ("08:00", "23:00"), ("09:00", "21:00")])
    assert wochenplan_fehler(verschieden) is None


@pytest.mark.parametrize(("plan_doc", "code"), [
    (None, "form"), ("plan", "form"), ({"1": 1}, "form"), (7, "form"), (b"x", "form"),
    (wochenplan()[:6], "anzahl"), (wochenplan() + [{"tag": 8, "warm_ab": "06:30", "kuehl_ab": "22:30"}], "anzahl"),
    ([], "anzahl"),
    (mit(wochenplan(), 1, tag=2), "tag"),  # Tag 2 doppelt, Tag 1 fehlt
    (mit(wochenplan(), 7, tag=0), "tag"), (mit(wochenplan(), 7, tag=8), "tag"),
    (mit(wochenplan(), 1, tag=True), "tag"), (mit(wochenplan(), 1, tag="1"), "tag"),
    (mit(wochenplan(), 1, tag=1.0), "tag"),
    (mit(wochenplan(), 1, warm_ab="24:00"), "zeit"), (mit(wochenplan(), 1, kuehl_ab="24:00"), "zeit"),
    (mit(wochenplan(), 1, warm_ab="6:30"), "zeit"), (mit(wochenplan(), 1, warm_ab="06:60"), "zeit"),
    (mit(wochenplan(), 1, warm_ab="06:30:00"), "zeit"), (mit(wochenplan(), 1, warm_ab=""), "zeit"),
    (mit(wochenplan(), 1, warm_ab=None), "zeit"), (mit(wochenplan(), 1, kuehl_ab=630), "zeit"),
    (mit(wochenplan(), 1, warm_ab="０６:３０"), "zeit"), (mit(wochenplan(), 1, warm_ab="-1:30"), "zeit"),
    (mit(wochenplan(), 1, warm_ab=" 6:30"), "zeit"), (mit(wochenplan(), 1, warm_ab="06:30\n"), "zeit"),
    (mit(wochenplan(), 3, warm_ab="08:00", kuehl_ab="08:00"), "gleiche_zeit"),
    (mit(wochenplan(), 3, warm_ab="00:00", kuehl_ab="00:00"), "gleiche_zeit"),
    (mit(wochenplan(), 2, ort="Berlin"), "form"), ([*wochenplan()[:6], "x"], "form"),
    ([*wochenplan()[:6], {"tag": 7, "warm_ab": "06:30"}], "form"),
    (wochenplan("06:30", "07:29"), "phase_zu_kurz"),  # 59 Minuten warm
    (wochenplan("06:30", "05:45"), "phase_zu_kurz"),  # 45 Minuten kuehl
    (wochenplan("06:30", "06:31"), "phase_zu_kurz"),
    (mit(wochenplan(), 7, warm_ab="06:30", kuehl_ab="05:50"), "phase_zu_kurz"),  # ueber die Wochengrenze: Montag 06:30
])
def test_formfehler_des_wochenplans(plan_doc, code):
    assert wochenplan_fehler(plan_doc) == code


def test_wechselfolge_muss_abwechseln_auch_ueber_die_wochengrenze():
    # Montag warm 06:00, kuehl Dienstag 05:00; Dienstag warm 04:00: zwei Mal warm hintereinander.
    zeiten = [("06:00", "05:00"), ("04:00", "22:00")] + [("06:00", "22:00")] * 5
    assert wochenplan_fehler(wochenplan_aus(zeiten)) == "wechselfolge"
    # Sonntag warm 22:00, kuehl Montag 08:00; Montag warm 06:00: warm, warm, kuehl, kuehl
    zeiten = [("06:00", "20:00")] + [("06:00", "22:00")] * 5 + [("22:00", "08:00")]
    assert wochenplan_fehler(wochenplan_aus(zeiten)) == "wechselfolge"


def test_nachtschicht_ueber_die_wochengrenze_ist_gueltig():
    assert wochenplan_fehler(wochenplan("07:00", "02:30")) is None


def test_formpruefung_veraendert_den_plan_nicht():
    plan_doc = wochenplan()
    vorher = copy.deepcopy(plan_doc)
    wochenplan_fehler(plan_doc)
    assert plan_doc == vorher


# --- Quelle des Anstiegs --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("vorher", "jetzt", "kunde"), [
    (None, "wunsch", True), ("wunsch", "wunsch", True),
    ("absenkung", "wunsch", False), ("aufheizen", "wunsch", False), ("abwesend", "wunsch", False),
    ("leerstand", "wunsch", False), ("uebersteuert", "wunsch", False),
    (None, "uebersteuert", True), ("wunsch", "uebersteuert", True), ("absenkung", "uebersteuert", True),
    ("uebersteuert", "uebersteuert", True),
    ("wunsch", "absenkung", False), ("absenkung", "aufheizen", False), ("leerstand", "aufheizen", False),
    ("wunsch", "abwesend", False), ("wunsch", "leerstand", False), ("abwesend", "aufheizen", False),
    ("wunsch", None, False), (None, None, False),
])
def test_anstieg_kommt_nur_vom_kunden_wenn_wunsch_oder_uebersteuerung_ihn_tragen(vorher, jetzt, kunde):
    assert anstieg_vom_kunden(vorher, jetzt) is kunde


# --- Eigenschaften --------------------------------------------------------------------------------------------

ZONEN = ["Europe/Berlin", "America/New_York", "Asia/Kolkata", "Australia/Lord_Howe", "UTC"]
BASIS = datetime(2026, 3, 10, tzinfo=UTC)


def hhmm(minuten):
    return f"{minuten // 60:02d}:{minuten % 60:02d}"


@st.composite
def faelle(draw, *, ueberlagerung=None, mit_aufheizen=True, mit_uebersteuerung=True):
    zone = draw(st.sampled_from(ZONEN))
    jetzt = BASIS + timedelta(minutes=draw(st.integers(0, 60 * 24 * 40)))
    wunsch = draw(st.floats(5.0, 35.0))
    modus = ueberlagerung or draw(st.sampled_from(["manuell", "abwesend", "leerstand"]))
    bedienung = doc(modus, raum_soll=wunsch)
    if draw(st.booleans()):
        warm = draw(st.integers(0, 1439))
        kuehl = (warm + draw(st.integers(60, 1380))) % 1440
        bedienung["wochenplan"] = wochenplan(hhmm(warm), hhmm(kuehl))
    abwesend_bis = jetzt + timedelta(minutes=draw(st.integers(-600, 6000)))
    bedienung["abwesend"] = abwesend(bis=text(abwesend_bis), temp=draw(st.floats(10.0, 25.0)))
    von = jetzt + timedelta(minutes=draw(st.integers(-3000, 1000)))
    bis = von + timedelta(minutes=draw(st.integers(60, 30000)))
    bedienung["leerstand"] = leerstand(von=text(von), bis=text(bis), temp=draw(st.floats(10.0, 25.0)))
    if mit_uebersteuerung and draw(st.booleans()):
        ende = jetzt + timedelta(minutes=draw(st.integers(-60, 1000)))
        bedienung["uebersteuerung"] = {"temp": draw(st.floats(10.0, 25.0)), "bis": text(ende)}
    weg = draw(st.integers(0, 1500)) if mit_aufheizen else 0
    konfiguration = {"absenkung_k": draw(st.floats(0.0, 20.0)),
                     "aufheizdauer_min": {"plan": draw(st.integers(0, 1500)) if mit_aufheizen else 0, "weg": weg}}
    return jetzt, zone, bedienung, konfiguration


@given(faelle())
def test_eigenschaft_nie_unter_der_untergrenze_und_nie_ueber_dem_wunsch_durch_die_absenkung(fall):
    jetzt, zone, bedienung, konfiguration = fall
    wunsch = bedienung["raum_soll"]
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration)
    assert erg.soll is not None
    if erg.soll_art == "wunsch":
        assert erg.soll == wunsch
    if erg.soll_art == "absenkung":
        assert min(wunsch, 16.0) <= erg.soll <= wunsch
    if erg.soll_art in {"abwesend", "leerstand", "uebersteuert"}:
        assert erg.soll >= 15.0
    if erg.soll_art == "aufheizen":
        assert erg.soll >= min(wunsch, 15.0)


@given(faelle(ueberlagerung="leerstand", mit_aufheizen=False, mit_uebersteuerung=False))
def test_eigenschaft_leerstand_gewinnt_gegen_abwesend_und_plan(fall):
    jetzt, zone, bedienung, konfiguration = fall
    fenster = bedienung["leerstand"]
    gueltig = utc(fenster["von"]) <= jetzt < utc(fenster["bis"]) and fenster["temp"] >= 15.0
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration)
    if gueltig and utc(fenster["bis"]) - utc(fenster["von"]) <= timedelta(days=365):
        assert paar(erg) == (fenster["temp"], "leerstand")
    else:
        assert erg.soll_art != "leerstand"


@given(faelle(ueberlagerung="abwesend", mit_aufheizen=False, mit_uebersteuerung=False))
def test_eigenschaft_abwesend_gewinnt_gegen_leerstand_und_plan(fall):
    jetzt, zone, bedienung, konfiguration = fall
    weg = bedienung["abwesend"]
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration)
    if jetzt < utc(weg["bis"]) and weg["temp"] >= 15.0:
        assert paar(erg) == (weg["temp"], "abwesend")
    else:
        assert erg.soll_art not in {"abwesend", "leerstand"}


@given(faelle())
def test_eigenschaft_naechster_wechsel_ist_der_erste_wechsel(fall):
    jetzt, zone, bedienung, konfiguration = fall
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration)
    if erg.naechster_wechsel is None:
        return
    assert erg.naechster_wechsel > jetzt
    assert erg.naechster_wechsel.utcoffset() == timedelta(0)
    davor = soll_effektiv(erg.naechster_wechsel - timedelta(seconds=1), zone, bedienung, konfiguration)
    danach = soll_effektiv(erg.naechster_wechsel, zone, bedienung, konfiguration)
    assert paar(davor) == paar(erg)
    assert paar(danach) != paar(erg)
