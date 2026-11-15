import dataclasses
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest

from smartheat_core.clamping import target_value
from smartheat_core.safety import (
    ABSENK_UNTERGRENZE_C,
    ABWESENHEIT_VORSCHLAG_C,
    BEDIENBEREICH_MAX_C,
    LEERSTAND_UNTERGRENZE_C,
    LEERSTAND_VORGABE_C,
    LOCAL_SAFETY,
    absenktemperatur,
    abwesend_bis_gueltig,
    abwesend_temperatur_gueltig,
    abwesenheit_vorschlag,
    check_invariants,
    leerstand_bis_gueltig,
    leerstand_temperatur_gueltig,
    phasendauer_gueltig,
    resolve_local_safety,
    uebersteuerung_temperatur_gueltig,
)


def _verteilsysteme() -> tuple[str, ...]:
    return tuple(sorted({verteilsystem for _, verteilsystem in LOCAL_SAFETY}))


def test_verteilsysteme_cover_both_distribution_systems():
    """Spiegel von VERTEILSYSTEME_MIT_LOKALEN_SICHERHEITSWERTEN im Server (dort gepinnt in
    tests/generic/test_profiles.py; je Profil eine lokale Zeile meldet ein Contract-Check (Vertrag V4))."""
    assert _verteilsysteme() == ("Fussbodenheizung", "Heizkoerper")


@pytest.mark.parametrize(("lever_set", "verteilsystem"), [
    ("vaillant_vrc720", "Deckenheizung"), ("vaillant_vrc720", None), ("unbekannt", "Heizkoerper"),
    ("vaillant_vrc720", ""), ("vaillant_vrc720", "heizkoerper"), ("vaillant_vrc720", "Unbekannt"),
])
def test_missing_values_are_an_error(lever_set, verteilsystem):
    with pytest.raises(ValueError, match="keine lokalen Sicherheitswerte"):
        resolve_local_safety(lever_set, verteilsystem)


def test_missing_verteilsystem_keeps_the_customer_text():
    with pytest.raises(ValueError) as error:
        resolve_local_safety("vaillant_vrc720", "Deckenheizung")
    assert str(error.value) == "keine lokalen Sicherheitswerte für Verteilsystem 'Deckenheizung'"


@pytest.mark.parametrize(("change", "match"), [
    ({"ranges": {"curve": (0.4, 1.5)}}, "Bereiche fuer"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "curve": (1.6, 1.5)}}, "Minimum"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "min_flow": (35.0, 30.0)}}, "min_flow Minimum"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "heat_limit": (24.0, 23.0)}}, "heat_limit Minimum"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "room_setpoint": (26.0, 25.0)}}, "room_setpoint Minimum"),
    ({"comfort_boost": {"curve": 1.6}}, "Comfort-Boost curve=1.6"),
    ({"comfort_boost": {"min_flow": 35.0}}, "Comfort-Boost min_flow=35.0"),
    ({"emergency_boost_levers": ("level",)}, "Notfall-Boost"),
    ({"arrival_threshold_k": 0.0}, "Ankunftsschwelle"),
])
def test_invariants(change, match):
    broken = dataclasses.replace(LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")], **change)
    with pytest.raises(ValueError, match=match):
        check_invariants(broken, "vaillant_vrc720", "Heizkoerper")


def test_comfort_boost_may_be_off():
    off = dataclasses.replace(LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")], comfort_boost={})
    check_invariants(off, "vaillant_vrc720", "Heizkoerper")


def test_every_entry_is_consistent_and_boost_inside_the_ranges():
    for (lever_set_id, verteilsystem), safety in LOCAL_SAFETY.items():
        check_invariants(safety, lever_set_id, verteilsystem)
        for lever, (low, high) in safety.ranges.items():
            assert low <= high, (verteilsystem, lever)
        for lever, value in safety.comfort_boost.items():
            assert safety.ranges[lever][0] <= value <= safety.ranges[lever][1], (verteilsystem, lever)


def test_every_combination_of_lever_set_and_distribution_system_has_values():
    from smartheat_core.levers import LEVER_SETS

    assert set(LOCAL_SAFETY) == {(lever_set, system) for lever_set in LEVER_SETS for system in _verteilsysteme()}


@pytest.mark.parametrize("key", sorted(
    k for k in LOCAL_SAFETY if k[1] == "Heizkoerper" and k != ("vaillant_vrc720", "Heizkoerper")))
def test_radiator_comfort_boost_is_the_local_maximum_of_every_sent_lever(key):
    from smartheat_core.levers import LEVER_SETS

    safety = resolve_local_safety(*key)
    sent = LEVER_SETS[key[0]].levers
    assert dict(safety.comfort_boost) == {lever: safety.ranges[lever][1] for lever in sent}
    assert safety.emergency_boost_levers == sent


@pytest.mark.parametrize("lever_set", ["vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare"])
def test_floor_heating_has_no_comfort_boost_and_an_emergency_boost_on_every_sent_lever(lever_set):
    from smartheat_core.levers import LEVER_SETS

    safety = resolve_local_safety(lever_set, "Fussbodenheizung")
    assert dict(safety.comfort_boost) == {}
    # Der Mindestvorlauf ist ein gesendeter Hebel, aber kein Boost-Hebel: der Boost schreibt ihn nie.
    assert safety.emergency_boost_levers == tuple(
        lever for lever in LEVER_SETS[lever_set].levers if lever != "min_flow"
    )


def test_a_range_with_equal_minimum_and_maximum_is_valid():
    """Mutationsprobe (Mutant 69): Minimum == Maximum ist kein Fehler, nur Minimum > Maximum."""
    safety = LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")]
    fixed = dataclasses.replace(safety, ranges={**safety.ranges, "min_flow": (25.0, 25.0)}, comfort_boost={})
    check_invariants(fixed, "vaillant_vrc720", "Heizkoerper")


def test_a_comfort_boost_value_on_the_lower_range_limit_is_valid():
    """Mutationsprobe (Mutant 74): der Comfort-Boost-Wert darf auf der unteren Bereichsgrenze liegen."""
    safety = LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")]
    low = safety.ranges["curve"][0]
    check_invariants(dataclasses.replace(safety, comfort_boost={"curve": low}), "vaillant_vrc720", "Heizkoerper")


HEIZGRENZE_PROFILE = [key for key, safety in LOCAL_SAFETY.items() if "heat_limit" in safety.ranges]


def test_the_heat_limit_exists_in_four_profiles():
    assert len(HEIZGRENZE_PROFILE) == 4


@pytest.mark.parametrize("key", HEIZGRENZE_PROFILE, ids=[f"{s}-{v}" for s, v in HEIZGRENZE_PROFILE])
def test_the_heat_limit_is_never_written_below_10_degrees_nor_above_the_profile_maximum(key):
    low, high = LOCAL_SAFETY[key].ranges["heat_limit"]
    assert low == 10.0
    assert high == (23.0 if key[1] == "Heizkoerper" else 20.0)
    step = 0.1
    assert target_value(9.9, low, high, step) == 10.0
    assert target_value(10.0, low, high, step) == 10.0
    assert target_value(high, low, high, step) == high
    assert target_value(high + 0.1, low, high, step) == high
    assert target_value(8.0, low, high, step) == 10.0


# --- Grenzen der Betriebsarten (Absenkung, Leerstand, Phasen, Abwesenheits-Vorschlag) ---

NAN = float("nan")
INF = float("inf")
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(("wunsch", "tiefe", "erwartet"), [
    (21.0, 3.0, 18.0),
    (21.0, 5.0, 16.0),  # genau auf der Untergrenze
    (21.0, 5.5, 16.0),  # tiefer wird auf die Untergrenze gehoben
    (21.0, 30.0, 16.0),
    (25.0, 3.0, 22.0),
    (21.0, 0.0, 21.0),  # Tiefe 0 = keine Absenkung
    (16.0, 3.0, 16.0),  # Wunsch auf der Untergrenze: nicht tiefer
    (15.5, 3.0, 15.5),  # Wunsch unter der Untergrenze: nie waermer als der Wunsch
    (15.0, 5.0, 15.0),
])
def test_absenktemperatur_nie_unter_der_untergrenze_und_nie_ueber_dem_wunsch(wunsch, tiefe, erwartet):
    assert absenktemperatur(wunsch, tiefe) == erwartet


@pytest.mark.parametrize(("wunsch", "tiefe"), [
    (21.0, -0.5), (21.0, NAN), (21.0, INF), (21.0, -INF), (NAN, 3.0), (INF, 3.0), (21.0, True), ("21", 3.0), (21.0, None),
])
def test_absenktemperatur_verweigert_unbrauchbare_eingaben(wunsch, tiefe):
    with pytest.raises(ValueError, match="Absenk"):
        absenktemperatur(wunsch, tiefe)


@pytest.mark.parametrize(("temp", "erwartet"), [
    (15.0, True), (16.0, True), (25.0, True), (25.5, False), (40.0, False), (14.9, False), (14.99, False), (5.0, False), (-40.0, False),
    (NAN, False), (INF, False), (-INF, False), (None, False), ("16", False), (True, False),
])
def test_leerstandtemperatur_mindestens_15(temp, erwartet):
    assert leerstand_temperatur_gueltig(temp) is erwartet


def test_leerstand_voreinstellung_ist_selbst_gueltig():
    assert LEERSTAND_VORGABE_C == 16.0
    assert leerstand_temperatur_gueltig(LEERSTAND_VORGABE_C)


@pytest.mark.parametrize(("bis", "erwartet"), [
    (T0 + timedelta(seconds=1), True),
    (T0 + timedelta(days=30), True),
    (T0 + timedelta(days=365), True),  # genau 365 Tage
    (T0 + timedelta(days=365, seconds=1), False),
    (T0 + timedelta(days=366), False),
    (T0, False),  # nicht in der Zukunft
    (T0 - timedelta(seconds=1), False),
    (T0 - timedelta(days=1), False),
])
def test_leerstand_bis_zukunft_und_hoechstens_365_tage(bis, erwartet):
    assert leerstand_bis_gueltig(T0, bis, T0) is erwartet


def test_leerstand_bis_rechnet_in_absoluter_zeit_ueber_zonen():
    berlin = ZoneInfo("Europe/Berlin")
    jetzt = datetime(2026, 11, 13, 13, 0, tzinfo=berlin)  # 12:00 UTC
    assert leerstand_bis_gueltig(jetzt, T0 + timedelta(days=365), jetzt) is True
    assert leerstand_bis_gueltig(jetzt, T0 + timedelta(days=365, seconds=1), jetzt) is False


def test_leerstand_bis_rechnet_ueber_den_sommerzeitwechsel_in_absoluter_zeit():
    """Beide Zeitpunkte tragen dasselbe tzinfo-Objekt: Python rechnete die Differenz dann naiv in Wandzeit. Hier
    sind es naiv 364 Tage 23,5 h, absolut aber 365 Tage 30 Minuten (der Herbstwechsel liegt dazwischen)."""
    berlin = ZoneInfo("Europe/Berlin")
    jetzt = datetime(2027, 10, 30, 12, 0, tzinfo=berlin)  # MESZ
    ueber = datetime(2028, 10, 29, 11, 30, tzinfo=berlin)  # MEZ
    genau = datetime(2028, 10, 29, 10, 30, tzinfo=berlin)  # MEZ, absolut genau 365 Tage
    assert ueber.astimezone(UTC) - jetzt.astimezone(UTC) == timedelta(days=365, minutes=30)
    assert leerstand_bis_gueltig(jetzt, ueber, jetzt) is False
    assert leerstand_bis_gueltig(jetzt, genau, jetzt) is True


def test_leerstand_hoechstdauer_gilt_ab_der_geraeteuhr_nicht_ab_dem_gesetzten_zeitpunkt():
    """Ein Server mit falschem `gesetzt` (weit in der Zukunft) verlaengert die Hoechstdauer nicht."""
    gesetzt = T0 + timedelta(days=300)
    assert leerstand_bis_gueltig(gesetzt, gesetzt + timedelta(days=100), T0) is False  # 400 Tage ab jetzt
    assert leerstand_bis_gueltig(gesetzt, T0 + timedelta(days=365), T0) is True
    assert leerstand_bis_gueltig(gesetzt, T0 + timedelta(days=365, seconds=1), T0) is False


def test_leerstand_hoechstdauer_gilt_auch_ab_dem_gesetzten_zeitpunkt():
    """Eine in der Vergangenheit gesetzte Dauer ueber 365 Tage bleibt ungueltig, auch wenn der Rest ab jetzt kurz ist."""
    gesetzt = T0 - timedelta(days=100)
    assert leerstand_bis_gueltig(gesetzt, gesetzt + timedelta(days=365), T0) is True
    assert leerstand_bis_gueltig(gesetzt, gesetzt + timedelta(days=365, seconds=1), T0) is False


def test_leerstand_bis_muss_nach_der_geraeteuhr_liegen():
    gesetzt = T0 - timedelta(days=10)
    assert leerstand_bis_gueltig(gesetzt, T0 + timedelta(seconds=1), T0) is True
    assert leerstand_bis_gueltig(gesetzt, T0, T0) is False
    assert leerstand_bis_gueltig(gesetzt, T0 - timedelta(days=1), T0) is False


def test_leerstand_bis_nach_dem_setzen():
    assert leerstand_bis_gueltig(T0 + timedelta(days=2), T0 + timedelta(days=1), T0) is False
    assert leerstand_bis_gueltig(T0 + timedelta(days=1), T0 + timedelta(days=1), T0) is False


@pytest.mark.parametrize(("gesetzt", "bis", "jetzt"), [
    (T0.replace(tzinfo=None), T0 + timedelta(days=1), T0),
    (T0, (T0 + timedelta(days=1)).replace(tzinfo=None), T0),
    (T0, T0 + timedelta(days=1), T0.replace(tzinfo=None)),
    (None, T0, T0), (T0, None, T0), (T0, T0, None), ("2026-11-13", T0, T0),
])
def test_leerstand_bis_verweigert_zeiten_ohne_zone_und_fremde_typen(gesetzt, bis, jetzt):
    assert leerstand_bis_gueltig(gesetzt, bis, jetzt) is False


class _OhneVersatz(tzinfo):
    """Eine Zone, die keinen Versatz liefert: `tzinfo` ist gesetzt, `utcoffset()` aber None."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


def test_leerstand_bis_verweigert_eine_zone_ohne_versatz_statt_abzustuerzen():
    fremd = T0.replace(tzinfo=_OhneVersatz())
    assert leerstand_bis_gueltig(fremd, T0 + timedelta(days=1), T0) is False
    assert leerstand_bis_gueltig(T0, fremd, T0) is False
    assert leerstand_bis_gueltig(T0, T0 + timedelta(days=1), fremd) is False


@pytest.mark.parametrize(("dauer", "erwartet"), [
    (timedelta(minutes=60), True),
    (timedelta(minutes=61), True),
    (timedelta(hours=8), True),
    (timedelta(minutes=59, seconds=59), False),
    (timedelta(minutes=59), False),
    (timedelta(0), False),
    (timedelta(minutes=-60), False),
    (None, False), (3600, False),
])
def test_phase_mindestens_60_minuten(dauer, erwartet):
    assert phasendauer_gueltig(dauer) is erwartet


def test_abwesenheit_vorschlag_je_verteilsystem():
    assert abwesenheit_vorschlag("Heizkoerper") == 17.0
    assert abwesenheit_vorschlag("Fussbodenheizung") == 19.0


def test_abwesenheit_vorschlag_deckt_jedes_verteilsystem_und_liegt_ueber_der_untergrenze():
    assert set(ABWESENHEIT_VORSCHLAG_C) == set(_verteilsysteme())
    assert all(wert > LEERSTAND_UNTERGRENZE_C for wert in ABWESENHEIT_VORSCHLAG_C.values())


@pytest.mark.parametrize("verteilsystem", ["Deckenheizung", None, "", "heizkoerper", 17])
def test_abwesenheit_vorschlag_ohne_stillen_rueckfall(verteilsystem):
    with pytest.raises(ValueError, match="keine lokalen Sicherheitswerte für Verteilsystem"):
        abwesenheit_vorschlag(verteilsystem)


def test_untergrenzen_liegen_im_bedienbereich_und_die_absenkung_nicht_unter_dem_leerstand():
    """Der weiteste Bedienbereich (`room_setpoint`) der lokalen Sicherheitswerte umfasst beide Untergrenzen."""
    bereiche = [s.ranges["room_setpoint"] for s in LOCAL_SAFETY.values() if "room_setpoint" in s.ranges]
    assert bereiche
    untere, obere = min(lo for lo, _ in bereiche), max(hi for _, hi in bereiche)
    assert untere <= LEERSTAND_UNTERGRENZE_C <= ABSENK_UNTERGRENZE_C <= obere


def test_leerstand_bis_am_rand_des_zeitbereichs_ist_ungueltig_statt_ein_abbruch():
    spaet = datetime(9999, 12, 31, 23, 0, tzinfo=ZoneInfo("Etc/GMT+5"))  # in UTC ueber dem Bereich
    assert leerstand_bis_gueltig(T0, spaet, T0) is False


@pytest.mark.parametrize("riesig", [10**400, -(10**400), 10**309], ids=["1e400", "minus-1e400", "1e309"])
def test_riesige_ganzzahlen_sind_keine_brauchbaren_zahlen_statt_eines_abbruchs(riesig):
    """`math.isfinite` wirft bei einer Ganzzahl ueber dem Gleitkommabereich einen OverflowError."""
    assert leerstand_temperatur_gueltig(riesig) is False
    with pytest.raises(ValueError, match="Absenk"):
        absenktemperatur(riesig, 3.0)
    with pytest.raises(ValueError, match="Absenk"):
        absenktemperatur(21.0, riesig)
    assert abwesend_temperatur_gueltig(riesig) is False
    assert uebersteuerung_temperatur_gueltig(riesig) is False


def test_bedienbereich_maximum_ist_das_hoechste_raum_soll_aller_profile():
    assert max(profil.ranges["room_setpoint"][1] for profil in LOCAL_SAFETY.values()) == BEDIENBEREICH_MAX_C


@pytest.mark.parametrize(("temp", "erwartet"), [
    (15.0, True), (25.0, True), (14.9, False), (25.5, False), (40.0, False), (INF, False), (-INF, False), (NAN, False),
    (None, False), ("20", False), (True, False),
])
def test_abwesend_temperatur_von_15_bis_zum_bedienbereich(temp, erwartet):
    assert abwesend_temperatur_gueltig(temp) is erwartet


@pytest.mark.parametrize(("temp", "erwartet"), [
    (16.0, True), (25.0, True), (15.5, False), (15.0, False), (25.5, False), (40.0, False), (INF, False),
    (-INF, False), (NAN, False), (None, False), ("20", False), (True, False),
])
def test_uebersteuerung_temperatur_von_16_bis_zum_bedienbereich(temp, erwartet):
    assert uebersteuerung_temperatur_gueltig(temp) is erwartet


@pytest.mark.parametrize(("bis", "erwartet"), [
    (T0 + timedelta(seconds=1), True),
    (T0 + timedelta(days=30), True),
    (T0 + timedelta(days=365), True),  # genau 365 Tage
    (T0 + timedelta(days=365, minutes=1), False),
    (T0 + timedelta(days=365, seconds=1), False),
    (T0, False),  # nicht in der Zukunft
    (T0 - timedelta(days=1), False),
])
def test_abwesend_bis_zukunft_und_hoechstens_365_tage_ab_der_geraeteuhr(bis, erwartet):
    assert abwesend_bis_gueltig(bis, T0) is erwartet


def test_abwesend_bis_rechnet_in_absoluter_zeit_ueber_zonen():
    berlin = ZoneInfo("Europe/Berlin")
    jetzt = T0.astimezone(berlin)
    assert abwesend_bis_gueltig(T0 + timedelta(days=365), jetzt) is True
    assert abwesend_bis_gueltig((T0 + timedelta(days=365, minutes=1)).astimezone(berlin), jetzt) is False


@pytest.mark.parametrize(("bis", "jetzt"), [
    (T0.replace(tzinfo=None) + timedelta(days=1), T0),
    (T0 + timedelta(days=1), T0.replace(tzinfo=None)),
    (None, T0), (T0, None), ("2026-11-14", T0),
])
def test_abwesend_bis_verweigert_zeiten_ohne_zone_und_fremde_typen(bis, jetzt):
    assert abwesend_bis_gueltig(bis, jetzt) is False


def test_abwesend_bis_am_rand_des_zeitbereichs_ist_ungueltig_statt_ein_abbruch():
    spaet = datetime(9999, 12, 31, 23, 0, tzinfo=ZoneInfo("Etc/GMT+5"))
    assert abwesend_bis_gueltig(spaet, T0) is False
