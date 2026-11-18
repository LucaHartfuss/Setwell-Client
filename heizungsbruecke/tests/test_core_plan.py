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
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(21.5), KONFIGURATION, uhr_vertrauenswuerdig=True)
    assert ergebnis == SollErgebnis(soll=21.5, soll_art="wunsch", naechster_wechsel=None)


def test_ergebnis_ist_unveraenderlich():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION, uhr_vertrauenswuerdig=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ergebnis.soll = 5.0  # type: ignore[misc]


@pytest.mark.parametrize("wunsch", [5.0, 35.0, 5, 35, 4.9, 35.1])
def test_wunsch_am_rand_wird_unveraendert_durchgereicht(wunsch):
    # Die Grenzen des Raumbereichs setzt der Wunsch-Eingang; hier wird nichts begrenzt.
    assert soll_effektiv(JETZT, ZONE, bedienung(wunsch), KONFIGURATION, uhr_vertrauenswuerdig=True).soll == wunsch


def test_soll_quelle_aus_oder_frostschutz_ergibt_kein_soll():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(21.5), KONFIGURATION, uhr_vertrauenswuerdig=True, soll_quelle_aus=True)
    assert ergebnis == SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None)


def test_soll_quelle_an_ist_der_standard():
    assert soll_effektiv(JETZT, ZONE, bedienung(20.0), KONFIGURATION, uhr_vertrauenswuerdig=True, soll_quelle_aus=False).soll == 20.0


def test_naive_jetzt_wird_abgelehnt():
    with pytest.raises(ValueError, match="^jetzt braucht eine Zeitzone$"):
        soll_effektiv(datetime(2026, 11, 2, 12, 0), ZONE, bedienung(), KONFIGURATION, uhr_vertrauenswuerdig=True)


@pytest.mark.parametrize("jetzt", ["2026-11-02T12:00:00+00:00", None, 1.0])
def test_jetzt_ohne_datum_wird_abgelehnt(jetzt):
    with pytest.raises(ValueError, match="^jetzt braucht eine Zeitzone$"):
        soll_effektiv(jetzt, ZONE, bedienung(), KONFIGURATION, uhr_vertrauenswuerdig=True)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", "../etc/passwd", "Europe/Berlin\x00", None, 3, "/etc"])
def test_ungueltige_zone_wird_abgelehnt_ohne_den_namen_zu_nennen(zone):
    with pytest.raises(ValueError, match="^zone ist keine bekannte Zeitzone$") as fehler:
        soll_effektiv(JETZT, zone, bedienung(), KONFIGURATION, uhr_vertrauenswuerdig=True)
    assert "Mars" not in str(fehler.value)


@pytest.mark.parametrize("wunsch", [float("nan"), float("inf"), True, None, "21", [21.0]])
def test_unbrauchbarer_wunsch_wird_abgelehnt_ohne_den_wert_zu_nennen(wunsch):
    with pytest.raises(ValueError, match="^bedienung hat keinen brauchbaren Wunsch$"):
        soll_effektiv(JETZT, ZONE, bedienung(wunsch), KONFIGURATION, uhr_vertrauenswuerdig=True)


@pytest.mark.parametrize("doc", [None, [], "x", {"kreis": 1}])
def test_bedienung_ohne_wunsch_wird_abgelehnt(doc):
    with pytest.raises(ValueError, match="^bedienung hat keinen brauchbaren Wunsch$"):
        soll_effektiv(JETZT, ZONE, doc, KONFIGURATION, uhr_vertrauenswuerdig=True)


@pytest.mark.parametrize("konfiguration", [None, [], "x"])
def test_konfiguration_ohne_objekt_wird_abgelehnt(konfiguration):
    with pytest.raises(ValueError, match="^konfiguration ist kein Objekt$"):
        soll_effektiv(JETZT, ZONE, bedienung(), konfiguration, uhr_vertrauenswuerdig=True)


def test_die_rechnung_ist_rein_und_wiederholbar(monkeypatch):
    # Keine Uhr: die Wanduhr des Clients wird nicht gelesen, das Ergebnis haengt nur von den Eingaben ab.
    def verboten():
        raise AssertionError("Uhr gelesen")

    monkeypatch.setattr(wallclock, "_now", verboten)
    eingabe = bedienung(19.0)
    erstes = soll_effektiv(JETZT, ZONE, eingabe, KONFIGURATION, uhr_vertrauenswuerdig=True)
    assert soll_effektiv(JETZT, ZONE, eingabe, KONFIGURATION, uhr_vertrauenswuerdig=True) == erstes
    assert eingabe == bedienung(19.0)


def test_die_zeitzone_des_hosts_aendert_nichts(monkeypatch):
    import time

    erstes = soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION, uhr_vertrauenswuerdig=True)
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    try:
        assert soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION, uhr_vertrauenswuerdig=True) == erstes
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


def ergebnis(jetzt, bedienung, konfiguration=KONF, zone=ZONE, vertrauen=True):
    return soll_effektiv(jetzt, zone, bedienung, konfiguration, uhr_vertrauenswuerdig=vertrauen)


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


def test_leerstand_mit_zu_spaetem_beginn_gilt_nicht_laenger_als_365_tage_ab_der_geraeteuhr():
    """Der Server schreibt `von`; ein Beginn weit in der Zukunft verlaengert die Hoechstdauer ab der Uhr des Geraets nicht."""
    jetzt = utc("2026-11-05T12:00:00Z")
    von = jetzt + timedelta(days=300)
    zu_lang = leerstand(temp=15.0, von=text(von), bis=text(von + timedelta(days=100)))
    assert ergebnis(jetzt, doc("leerstand", leerstand=zu_lang)).naechster_wechsel is None  # Fenster ungueltig
    am_rand = leerstand(temp=15.0, von=text(von), bis=text(jetzt + timedelta(days=365)))
    assert ergebnis(jetzt, doc("leerstand", leerstand=am_rand)).naechster_wechsel == von  # Fenster gilt


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


def test_abwesend_temperatur_hoechstens_der_bedienbereich():
    jetzt = utc("2026-11-10T12:00:00Z")
    assert paar(ergebnis(jetzt, doc("abwesend", abwesend=abwesend(temp=25.0)))) == (25.0, "abwesend")
    assert paar(ergebnis(jetzt, doc("abwesend", abwesend=abwesend(temp=25.5)))) == (21.0, "wunsch")
    assert paar(ergebnis(jetzt, doc("abwesend", abwesend=abwesend(temp=40.0)))) == (21.0, "wunsch")


def test_leerstand_temperatur_hoechstens_der_bedienbereich():
    jetzt = utc("2026-11-05T12:00:00Z")
    assert paar(ergebnis(jetzt, doc("leerstand", leerstand=leerstand(temp=25.0)))) == (25.0, "leerstand")
    assert paar(ergebnis(jetzt, doc("leerstand", leerstand=leerstand(temp=25.5)))) == (21.0, "wunsch")


def test_uebersteuerung_temperatur_von_16_bis_zum_bedienbereich():
    kuehl = utc("2026-11-02T23:00:00Z")
    bis = "2026-11-03T05:00:00Z"
    assert paar(ergebnis(kuehl, doc(wochenplan=wochenplan(), uebersteuerung={"temp": 16.0, "bis": bis}))
                ) == (16.0, "uebersteuert")
    assert paar(ergebnis(kuehl, doc(wochenplan=wochenplan(), uebersteuerung={"temp": 25.0, "bis": bis}))
                ) == (25.0, "uebersteuert")
    for ungueltig in (15.5, 15.0, 25.5, 40.0):
        erg = ergebnis(kuehl, doc(wochenplan=wochenplan(), uebersteuerung={"temp": ungueltig, "bis": bis}))
        assert paar(erg) == (18.0, "absenkung"), ungueltig


def test_uebersteuerung_im_leerstand_hat_dieselben_grenzen():
    jetzt = utc("2026-11-05T12:00:00Z")
    bis = "2026-11-05T16:00:00Z"
    for ungueltig in (15.5, 25.5):
        erg = ergebnis(jetzt, doc("leerstand", leerstand=leerstand(), uebersteuerung={"temp": ungueltig, "bis": bis}))
        assert paar(erg) == (16.0, "leerstand"), ungueltig


def test_abwesend_bis_hoechstens_365_tage_ab_der_geraeteuhr():
    jetzt = utc("2026-11-10T12:00:00Z")
    am_rand = abwesend(bis=text(jetzt + timedelta(days=365)))
    assert paar(ergebnis(jetzt, doc("abwesend", abwesend=am_rand))) == (17.0, "abwesend")
    zu_spaet = abwesend(bis=text(jetzt + timedelta(days=365, minutes=1)))
    erg = ergebnis(jetzt, doc("abwesend", abwesend=zu_spaet))
    assert paar(erg) == (21.0, "wunsch")
    assert erg.naechster_wechsel is None


def test_abwesend_zu_weit_voraus_faellt_auf_den_plan_zurueck():
    jetzt = utc("2026-11-02T23:00:00Z")
    zu_spaet = abwesend(bis=text(jetzt + timedelta(days=366)))
    assert paar(ergebnis(jetzt, doc("abwesend", wochenplan=wochenplan(), abwesend=zu_spaet))) == (18.0, "absenkung")


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


def test_aufheizdauer_mit_nur_einem_schluessel_gilt_fuer_den_anderen_als_null():
    nur_weg = {"absenkung_k": 3, "aufheizdauer_min": {"weg": 60}}
    assert paar(ergebnis(NACHT, doc(wochenplan=wochenplan()), nur_weg)) == (18.0, "absenkung")
    erg = ergebnis(utc("2026-11-10T15:00:00Z"), doc("abwesend", abwesend=abwesend()), nur_weg)
    assert paar(erg) == (21.0, "aufheizen")
    nur_plan = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 60}}
    assert ergebnis(utc("2026-11-03T04:29:59Z"), doc(wochenplan=wochenplan()), nur_plan).soll_art == "absenkung"
    assert ergebnis(utc("2026-11-03T04:30:00Z"), doc(wochenplan=wochenplan()), nur_plan).soll_art == "aufheizen"
    assert paar(ergebnis(utc("2026-11-10T15:59:59Z"), doc("abwesend", abwesend=abwesend()), nur_plan)) == (17.0, "abwesend")


@pytest.mark.parametrize("aufheizdauer", ["x", [60], 60])
def test_aufheizdauer_ohne_schluessel_heisst_kein_aufheizen_der_abwesenheit(aufheizdauer):
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": aufheizdauer}
    erg = ergebnis(utc("2026-11-09T16:00:00Z"), doc("abwesend", abwesend=abwesend(bis="2026-11-10T16:00:00Z")), konfiguration)
    assert paar(erg) == (17.0, "abwesend")
    assert erg.naechster_wechsel == utc("2026-11-10T16:00:00Z")


def test_ohne_aufheizdauer_gibt_es_absenkung_und_kein_aufheizen():
    ohne = {"absenkung_k": 3}
    assert paar(ergebnis(NACHT, doc(wochenplan=wochenplan()), ohne)) == (18.0, "absenkung")
    assert ergebnis(utc("2026-11-03T05:29:59Z"), doc(wochenplan=wochenplan()), ohne).soll_art == "absenkung"
    assert paar(ergebnis(utc("2026-11-10T15:59:59Z"), doc("abwesend", abwesend=abwesend()), ohne)) == (17.0, "abwesend")


def test_uebersteuerung_die_jetzt_endet_gilt_nicht_mehr():
    jetzt = utc("2026-11-02T23:00:00Z")
    konfiguration = {"absenkung_k": 3, "aufheizdauer_min": {"plan": 60, "weg": 0}}
    ueber = {"temp": 20.0, "bis": "2026-11-02T23:00:00Z"}
    assert paar(ergebnis(jetzt, doc(wochenplan=wochenplan(), uebersteuerung=ueber), konfiguration)) == (18.0, "absenkung")
    ueber = {"temp": 20.0, "bis": "2026-11-02T23:00:01Z"}
    erg = ergebnis(jetzt, doc(wochenplan=wochenplan(), uebersteuerung=ueber), konfiguration)
    assert paar(erg) == (20.0, "uebersteuert")
    assert erg.naechster_wechsel == utc("2026-11-02T23:00:01Z")


def test_uebersteuerung_im_leerstand_gilt_bis_zum_leerstandsende_einschliesslich():
    bedienung = doc("leerstand", leerstand=leerstand(bis="2026-11-05T16:00:00Z"),
                    uebersteuerung={"temp": 19.0, "bis": "2026-11-05T16:00:00Z"})
    erg = ergebnis(utc("2026-11-05T12:00:00Z"), bedienung)
    assert paar(erg) == (19.0, "uebersteuert")
    assert erg.naechster_wechsel == utc("2026-11-05T16:00:00Z")
    bedienung = doc("leerstand", leerstand=leerstand(bis="2026-11-05T16:00:00Z"),
                    uebersteuerung={"temp": 19.0, "bis": "2026-11-05T12:00:00Z"})
    assert ergebnis(utc("2026-11-05T12:00:00Z"), bedienung).soll_art == "leerstand"


def plan_mit_sonderzeiten(**je_tag):
    eintraege = wochenplan()
    for tag, (warm, kuehl) in je_tag.items():
        eintraege[int(tag[1:]) - 1] = {"tag": int(tag[1:]), "warm_ab": warm, "kuehl_ab": kuehl}
    return eintraege


@pytest.mark.parametrize("tag", range(1, 8))
def test_jeder_wochentag_nutzt_seinen_eigenen_eintrag(tag):
    # Montag 2026-11-02 (Winterzeit, UTC+1); warm_ab je Tag verschieden: 06:00 + 15 min je Tag
    eintraege = [{"tag": t, "warm_ab": "{:02d}:{:02d}".format(*divmod(6 * 60 + 15 * t, 60)), "kuehl_ab": "22:30"}
                 for t in range(1, 8)]
    minute = 6 * 60 + 15 * tag - 60  # in UTC
    tag_beginn = datetime(2026, 11, 1 + tag, tzinfo=UTC)
    warm = tag_beginn + timedelta(minutes=minute)
    assert paar(ergebnis(warm - timedelta(seconds=1), doc(wochenplan=eintraege))) == (18.0, "absenkung")
    erg = ergebnis(warm, doc(wochenplan=eintraege))
    assert paar(erg) == (21.0, "wunsch")
    assert erg.naechster_wechsel == tag_beginn + timedelta(hours=21, minutes=30)


def test_nachtschicht_am_sonntag_gehoert_zum_sonntag_und_ueberdeckt_den_montagmorgen():
    eintraege = plan_mit_sonderzeiten(t7=("14:00", "03:00"))  # Sonntag warm ab 14:00 bis Montag 03:00
    sonntag_vormittag = utc("2026-11-08T11:00:00Z")  # 12:00 Berlin
    assert paar(ergebnis(sonntag_vormittag, doc(wochenplan=eintraege))) == (18.0, "absenkung")
    assert ergebnis(sonntag_vormittag, doc(wochenplan=eintraege)).naechster_wechsel == utc("2026-11-08T13:00:00Z")
    montag_nacht = utc("2026-11-09T01:59:59Z")  # 02:59 Berlin, noch warm
    erg = ergebnis(montag_nacht, doc(wochenplan=eintraege))
    assert paar(erg) == (21.0, "wunsch")
    assert erg.naechster_wechsel == utc("2026-11-09T02:00:00Z")
    erg = ergebnis(utc("2026-11-09T02:00:00Z"), doc(wochenplan=eintraege))
    assert paar(erg) == (18.0, "absenkung")
    assert erg.naechster_wechsel == utc("2026-11-09T05:30:00Z")


@pytest.mark.parametrize("warm", ["02:07", "02:30"])
def test_warmer_beginn_in_der_luecke_der_sommerzeit_gilt_ab_dem_ersten_gueltigen_moment(warm):
    eintraege = plan_mit_sonderzeiten(t7=(warm, "22:30"))  # Sonntag 2026-03-29: 02:00 bis 03:00 Berlin gibt es nicht
    vorher = ergebnis(utc("2026-03-29T00:59:59Z"), doc(wochenplan=eintraege))
    assert paar(vorher) == (18.0, "absenkung")
    assert vorher.naechster_wechsel == utc("2026-03-29T01:00:00Z")
    assert paar(ergebnis(utc("2026-03-29T01:00:00Z"), doc(wochenplan=eintraege))) == (21.0, "wunsch")


@pytest.mark.parametrize("kuehl", ["02:07", "02:30"])
def test_kuehler_beginn_in_der_luecke_der_sommerzeit_gilt_ab_dem_ersten_gueltigen_moment(kuehl):
    eintraege = plan_mit_sonderzeiten(t7=("00:30", kuehl))
    vorher = ergebnis(utc("2026-03-29T00:59:59Z"), doc(wochenplan=eintraege))
    assert paar(vorher) == (21.0, "wunsch")
    assert vorher.naechster_wechsel == utc("2026-03-29T01:00:00Z")
    assert paar(ergebnis(utc("2026-03-29T01:00:00Z"), doc(wochenplan=eintraege))) == (18.0, "absenkung")


@pytest.mark.parametrize("jetzt", [datetime(9999, 12, 30, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
                                   datetime(1, 1, 3, tzinfo=UTC)])
def test_aeusserste_zeitpunkte_rechnen_ohne_absturz(jetzt):
    erg = ergebnis(jetzt, doc(wochenplan=wochenplan()))
    assert erg.soll_art in {"wunsch", "absenkung"}
    assert erg.naechster_wechsel is None or erg.naechster_wechsel > jetzt


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
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration, uhr_vertrauenswuerdig=True)
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
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration, uhr_vertrauenswuerdig=True)
    if gueltig and utc(fenster["bis"]) - utc(fenster["von"]) <= timedelta(days=365):
        assert paar(erg) == (fenster["temp"], "leerstand")
    else:
        assert erg.soll_art != "leerstand"


@given(faelle(ueberlagerung="abwesend", mit_aufheizen=False, mit_uebersteuerung=False))
def test_eigenschaft_abwesend_gewinnt_gegen_leerstand_und_plan(fall):
    jetzt, zone, bedienung, konfiguration = fall
    weg = bedienung["abwesend"]
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration, uhr_vertrauenswuerdig=True)
    if jetzt < utc(weg["bis"]) and weg["temp"] >= 15.0:
        assert paar(erg) == (weg["temp"], "abwesend")
    else:
        assert erg.soll_art not in {"abwesend", "leerstand"}


@given(faelle())
def test_eigenschaft_naechster_wechsel_ist_der_erste_wechsel(fall):
    jetzt, zone, bedienung, konfiguration = fall
    erg = soll_effektiv(jetzt, zone, bedienung, konfiguration, uhr_vertrauenswuerdig=True)
    if erg.naechster_wechsel is None:
        return
    assert erg.naechster_wechsel > jetzt
    assert erg.naechster_wechsel.utcoffset() == timedelta(0)
    davor = soll_effektiv(erg.naechster_wechsel - timedelta(seconds=1), zone, bedienung, konfiguration, uhr_vertrauenswuerdig=True)
    danach = soll_effektiv(erg.naechster_wechsel, zone, bedienung, konfiguration, uhr_vertrauenswuerdig=True)
    assert paar(davor) == paar(erg)
    assert paar(danach) != paar(erg)


# --- Uhr unklar: die Betriebsarten ruhen ---------------------------------------------------------------------------

UEBERSTEUERT = {"temp": 20.0, "bis": "2026-11-03T05:00:00Z"}
BETRIEBSARTEN = {
    "plan": doc(wochenplan=wochenplan()),
    "abwesend": doc(modus="abwesend", abwesend=abwesend(bis="2026-11-10T16:00:00Z")),
    "leerstand": doc(modus="leerstand", leerstand=leerstand()),
    "uebersteuerung": doc(wochenplan=wochenplan(), uebersteuerung=UEBERSTEUERT),
}


@pytest.mark.parametrize("name", sorted(BETRIEBSARTEN))
def test_ohne_vertrauenswuerdige_uhr_ist_das_soll_der_wunsch_und_der_merker_gesetzt(name):
    bedienung = BETRIEBSARTEN[name]
    mit = ergebnis(NACHT, bedienung)
    assert mit.soll_art != "wunsch" and mit.uhr_unklar is False  # mit Vertrauen wirkt die Betriebsart
    ohne = ergebnis(NACHT, bedienung, vertrauen=False)
    assert ohne == SollErgebnis(soll=21.0, soll_art="wunsch", naechster_wechsel=None, uhr_unklar=True)


def test_der_wunsch_bleibt_ohne_uhr_unbegrenzt_wie_er_ist():
    assert ergebnis(NACHT, doc(raum_soll=24.5, wochenplan=wochenplan()), vertrauen=False).soll == 24.5


def test_nach_der_synchronitaet_gilt_der_plan_an_der_aktuellen_stelle():
    bedienung = doc(wochenplan=wochenplan())
    kuehl, warm = utc("2026-11-02T22:00:00Z"), utc("2026-11-03T08:00:00Z")
    assert ergebnis(kuehl, bedienung, vertrauen=False).soll_art == "wunsch"
    assert paar(ergebnis(kuehl, bedienung)) == (18.0, "absenkung")
    assert paar(ergebnis(warm, bedienung, vertrauen=False)) == (21.0, "wunsch")
    assert ergebnis(warm, bedienung).naechster_wechsel == utc("2026-11-03T21:30:00Z")


@pytest.mark.parametrize("bedienung", [doc(), doc(wochenplan=None), doc(abwesend=None, leerstand=None, uebersteuerung=None),
                                       doc(wochenplan=[{"tag": 1}]), doc(modus="manuell", abwesend=abwesend()),
                                       doc(modus="manuell", leerstand=leerstand())])
def test_ruht_nichts_gibt_es_auch_keinen_merker(bedienung):
    assert ergebnis(NACHT, bedienung, vertrauen=False) == SollErgebnis(21.0, "wunsch", None, False)


@pytest.mark.parametrize("wert", [None, 1, "ja", 0, [], "True"])
def test_nur_ein_echtes_true_gilt_als_vertrauen(wert):
    ohne = soll_effektiv(NACHT, ZONE, BETRIEBSARTEN["plan"], KONF, uhr_vertrauenswuerdig=wert)
    assert ohne.soll_art == "wunsch" and ohne.uhr_unklar is True


def test_ohne_soll_quelle_ruht_nichts_und_es_gibt_keinen_merker():
    erg = soll_effektiv(NACHT, ZONE, BETRIEBSARTEN["plan"], KONF, uhr_vertrauenswuerdig=False, soll_quelle_aus=True)
    assert erg == SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None, uhr_unklar=False)


def test_das_urteil_der_uhr_ist_ein_pflichtparameter():
    with pytest.raises(TypeError):
        soll_effektiv(NACHT, ZONE, BETRIEBSARTEN["plan"], KONF)  # type: ignore[call-arg]


def test_die_pruefung_der_eingaben_gilt_auch_ohne_vertrauen():
    with pytest.raises(ValueError):
        soll_effektiv(datetime(2026, 11, 2, 22, 0), ZONE, BETRIEBSARTEN["plan"], KONF, uhr_vertrauenswuerdig=False)
    with pytest.raises(ValueError):
        soll_effektiv(NACHT, "Nirgendwo/Nichts", BETRIEBSARTEN["plan"], KONF, uhr_vertrauenswuerdig=False)
