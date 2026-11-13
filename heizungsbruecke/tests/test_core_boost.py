from datetime import UTC, datetime, timedelta

import pytest
from runtime_helpers import ankunftsschwelle_der_laufzeit

from smartheat_core import boost, write_budget
from smartheat_core.boost import COMFORT_BOOST_MAX_SECONDS, comfort_boost_expired, decide_boost

#: Anstieg durch den Kunden, kein Profil: die Angaben sind Pflicht, dies ist das Verhalten der Wunschaenderung am Thermostat.
KUNDE = {"aufheizen": None, "anstieg_vom_kunden": True}


def test_decide_boost_inactive_on_first_ever_tick_no_previous_target():
    decision = decide_boost(
        room_actual=18.0, room_target=20.0, previous_room_target=None,
        boost_was_active=False, arrival_threshold_k=0.5, **KUNDE,
    )
    assert decision is False


def test_decide_boost_inactive_when_target_unchanged_even_if_room_cold():
    # Kernverhalten der Neudefinition: Kaelte OHNE Zielwert-Erhoehung loest KEIN
    # Boost mehr aus (bewusst entfernte
    # Sicherheitsnetz-Eigenschaft).
    decision = decide_boost(
        room_actual=15.0, room_target=20.0, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5, **KUNDE,
    )
    assert decision is False


def test_decide_boost_activates_when_target_raised():
    decision = decide_boost(
        room_actual=19.0, room_target=21.0, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5, **KUNDE,
    )
    assert decision is True


def test_decide_boost_does_not_activate_for_tiny_float_noise_change():
    decision = decide_boost(
        room_actual=19.0, room_target=20.005, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5, **KUNDE,
    )
    assert decision is False


def test_decide_boost_stays_active_until_arrival_threshold_reached():
    # room_actual bewusst auf 19.4 gesetzt (nicht 19.6 wie im urspruenglichen Task-Brief):
    # mit arrival_threshold_k=0.5 muss der Abstand zum Ziel (hier 0.6) GROESSER als die
    # Schwelle sein, damit Boost noch nicht "angekommen" ist und aktiv bleibt -- bei 19.6
    # waere der Abstand (0.4) bereits kleiner als die Schwelle (0.5), das haette laut der
    # in Step 3 des Briefs vorgegebenen Formel (`room_actual < room_target -
    # arrival_threshold_k`) sofort zum Beenden gefuehrt und stand im Widerspruch zum
    # eigenen Testnamen sowie zu test_decide_boost_exits_once_within_arrival_threshold
    # direkt darunter.
    decision = decide_boost(
        room_actual=19.4, room_target=20.0, previous_room_target=20.0,
        boost_was_active=True, arrival_threshold_k=0.5, **KUNDE,
    )
    assert decision is True


def test_decide_boost_exits_once_within_arrival_threshold():
    decision = decide_boost(
        room_actual=19.6, room_target=20.0, previous_room_target=20.0,
        boost_was_active=True, arrival_threshold_k=0.4, **KUNDE,
    )
    assert decision is False


@pytest.mark.parametrize(("raum_ist", "bleibt_aktiv"), [
    pytest.param(20.51, False, id="abstand-0.49"), pytest.param(20.5, False, id="abstand-0.5"),
    pytest.param(20.49, True, id="abstand-0.51"),
])
def test_comfort_boost_endet_an_der_echten_ankunftsschwelle(tmp_path, raum_ist, bleibt_aktiv):
    """Schwelle aus der echten RuntimeConfig (Vaillant x Heizkoerper, 0,5 K): beim Soll 21,0 bleibt ein laufender
    Comfort-Boost nur bei ist < soll - 0,5 aktiv; bei 0,49 und genau 0,5 K Abstand endet er."""
    decision = decide_boost(
        room_actual=raum_ist, room_target=21.0, previous_room_target=21.0,
        boost_was_active=True, arrival_threshold_k=ankunftsschwelle_der_laufzeit(tmp_path), **KUNDE,
    )
    assert decision is bleibt_aktiv


def test_decide_boost_stays_active_across_a_second_target_raise():
    # Zweite Erhoehung waehrend Boost bereits aktiv ist: kein Sonderfall noetig,
    # Exit-Pruefung vergleicht ohnehin gegen den AKTUELLEN room_target.
    decision = decide_boost(
        room_actual=20.5, room_target=22.0, previous_room_target=21.0,
        boost_was_active=True, arrival_threshold_k=0.5, **KUNDE,
    )
    assert decision is True


@pytest.mark.parametrize(("vorher", "nachher", "boost"), [
    (20.5, 21.0, True),     # ein Thermostat-Schritt 0,5 K
    (20.5, 20.6, True),     # 0,1-K-Thermostat
    (20.5, 20.509, False),  # knapp unter der Schwelle 0,01 K
    (20.5, 20.5, False),
    (20.5, 20.0, False),    # Senkung
])
def test_kleinste_reale_erhoehung_startet_den_boost(vorher, nachher, boost):
    """Auch die kleinste reale Erhoehung der Wunschtemperatur startet den Boost."""
    assert decide_boost(room_actual=20.0, room_target=nachher, previous_room_target=vorher,
                        boost_was_active=False, arrival_threshold_k=0.5, **KUNDE) is boost


@pytest.mark.parametrize(("raum_ist", "startet"), [
    pytest.param(21.0, False, id="raum-gleich-soll"),
    pytest.param(21.4, False, id="raum-ueber-soll"),
    pytest.param(20.6, False, id="abstand-0.4"),
    pytest.param(20.5, False, id="grenzwert-soll-minus-0.5"),
    pytest.param(20.49, True, id="abstand-0.51"),
    pytest.param(15.0, True, id="kalt"),
])
def test_comfort_boost_startet_nur_wenn_der_raum_noch_nicht_angekommen_ist(raum_ist, startet):
    """Regel 4: eine Erhoehung startet den Boost nur bei Raum < Soll - Ankunftsschwelle; bei warmem Raum
    gibt es keinen Schein-Start, der den echten Boost 30 min sperrte. Der Grenzwert genau Soll - 0,5 startet nicht."""
    assert decide_boost(room_actual=raum_ist, room_target=21.0, previous_room_target=20.0,
                        boost_was_active=False, arrival_threshold_k=0.5, **KUNDE) is startet


def test_start_und_weiterlaufen_nutzen_dieselbe_schwelle():
    """Ohne und mit laufendem Boost entscheidet dieselbe Bedingung; nur die Erhoehung kommt zum Start hinzu."""
    for raum_ist in (20.4, 20.5, 20.51, 21.0):
        start = decide_boost(room_actual=raum_ist, room_target=21.0, previous_room_target=20.0,
                             boost_was_active=False, arrival_threshold_k=0.5, **KUNDE)
        laeuft = decide_boost(room_actual=raum_ist, room_target=21.0, previous_room_target=21.0,
                              boost_was_active=True, arrival_threshold_k=0.5, **KUNDE)
        assert start is laeuft


def test_ein_laufender_boost_bleibt_bei_warmem_raum_nicht_aktiv_und_bei_kaltem_raum_aktiv():
    """Laufender Boost unveraendert: die Erhoehung spielt keine Rolle, nur der Raum."""
    kalt = decide_boost(room_actual=19.0, room_target=21.0, previous_room_target=21.0,
                        boost_was_active=True, arrival_threshold_k=0.5, **KUNDE)
    warm = decide_boost(room_actual=21.0, room_target=21.0, previous_room_target=20.0,
                        boost_was_active=True, arrival_threshold_k=0.5, **KUNDE)
    assert (kalt, warm) == (True, False)


# --- Mutationsprobe ------------------------------------------------------------------------

def test_a_rise_of_exactly_the_epsilon_does_not_start_the_boost():
    """Mutationsprobe (Mutant 8): die Schwelle gilt strikt (>), eine Erhoehung genau um den Epsilon-Wert startet nichts."""
    vorher = 20.0
    nachher = vorher + boost._TARGET_RISE_EPSILON_K
    assert decide_boost(room_actual=19.0, room_target=nachher, previous_room_target=vorher,
                        boost_was_active=False, arrival_threshold_k=0.5, **KUNDE) is False


_BEGINN = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(("dauer", "abgelaufen"), [
    (timedelta(0), False),
    (timedelta(seconds=COMFORT_BOOST_MAX_SECONDS - 1), False),
    (timedelta(seconds=COMFORT_BOOST_MAX_SECONDS), True),  # genau 4 h: abgelaufen (>=)
    (timedelta(seconds=COMFORT_BOOST_MAX_SECONDS + 1), True),
])
def test_comfort_boost_expires_exactly_at_the_maximum_duration(dauer, abgelaufen):
    """Mutationsprobe (Mutanten 14, 17, 18): Vorzeichen, Grenze (>=) und Rueckgabe der Hoechstdauer."""
    assert comfort_boost_expired(_BEGINN, _BEGINN + dauer) is abgelaufen


def test_an_unknown_start_never_expires():
    assert comfort_boost_expired(None, _BEGINN) is False


def test_boost_seit_in_der_zukunft_ist_abgelaufen():
    """Liegt der Beginn nach der Wanduhr (Uhr zurueckgesprungen), ist die Dauer nicht mehr bekannt: der Boost
    endet lieber frueher als gar nicht."""
    assert comfort_boost_expired(_BEGINN + timedelta(seconds=1), _BEGINN) is True
    assert comfort_boost_expired(_BEGINN, _BEGINN) is False  # Gegenprobe: gleicher Zeitpunkt ist die Dauer 0


@pytest.mark.parametrize(("mono_seit", "abgelaufen"), [
    (0.0, False),
    (COMFORT_BOOST_MAX_SECONDS - 1, False),
    (COMFORT_BOOST_MAX_SECONDS, True),  # genau 4 h auf der monotonen Uhr: abgelaufen (>=)
    (COMFORT_BOOST_MAX_SECONDS + 1, True),
])
def test_boost_endet_auch_nach_4h_auf_der_monotonen_uhr(mono_seit, abgelaufen):
    """Die Wanduhr zeigt erst eine Stunde (zurueckgesprungen), die monotone Uhr zaehlt die Hoechstdauer."""
    assert comfort_boost_expired(_BEGINN, _BEGINN + timedelta(hours=1), mono_seit) is abgelaufen


def test_die_monotone_dauer_ist_unabhaengig_vom_wanduhrbeginn():
    """Ein Boost aus einem frueheren Lauf hat keinen Beginn (None); die monotone Uhr des Prozesses zaehlt trotzdem."""
    assert comfort_boost_expired(None, _BEGINN, COMFORT_BOOST_MAX_SECONDS) is True
    assert comfort_boost_expired(None, _BEGINN, 60.0) is False
    assert comfort_boost_expired(None, _BEGINN, None) is False


# --- Aufheizen nach Profil: Boost nur bei `boost`, Kurve und fehlende Angabe unterdruecken ---------------------

def _start(vorher, nachher, **kw):
    return decide_boost(room_actual=18.0, room_target=nachher, previous_room_target=vorher,
                        boost_was_active=False, arrival_threshold_k=0.5, **kw)


@pytest.mark.parametrize(("aufheizen", "erwartet"), [
    pytest.param("boost", True, id="boost"),
    pytest.param("kurve", False, id="kurve"),
    pytest.param(None, False, id="ohne-angabe"),
    pytest.param("", False, id="leer"),
    pytest.param("Boost", False, id="andere-schreibweise"),
    pytest.param("unbekannt", False, id="unbekannt"),
])
def test_anstieg_durch_plan_startet_den_boost_nur_bei_aufheizen_boost(aufheizen, erwartet):
    """Beginn der warmen Phase, Ende von Away und Leerstand: nur das Profil `boost` startet den Comfort-Boost;
    `kurve`, eine fehlende oder unbekannte Angabe unterdruecken ihn (weniger Heizen und Schreiben, sicher)."""
    assert _start(20.0, 21.0, aufheizen=aufheizen, anstieg_vom_kunden=False) is erwartet


@pytest.mark.parametrize("aufheizen", ["boost", "kurve", None, "unbekannt"])
def test_erhoehung_durch_den_kunden_startet_den_boost_wie_heute(aufheizen):
    """Die Erhoehung durch den Kunden hat mit dem Aufheizen des Profils nichts zu tun."""
    assert _start(20.0, 21.0, aufheizen=aufheizen, anstieg_vom_kunden=True) is True


@pytest.mark.parametrize(("vorher", "nachher", "raum_ist"), [
    (None, 21.0, 18.0), (20.0, 21.0, 18.0), (20.0, 20.0, 15.0), (20.0, 19.0, 15.0), (20.0, 21.0, 20.9),
    (20.0, 20.005, 18.0),
])
@pytest.mark.parametrize("aktiv", [False, True])
def test_anstieg_durch_den_kunden_entscheidet_wie_die_wunschaenderung_am_thermostat(vorher, nachher, raum_ist, aktiv):
    """Mit der Herkunft `Kunde` zaehlt jede Erhoehung, der Raum darf noch nicht angekommen sein; das Profil ist ohne
    Wirkung."""
    erwartet = (raum_ist < nachher - 0.5) if aktiv else (
        vorher is not None and nachher > vorher + 0.01 and raum_ist < nachher - 0.5)
    for aufheizen in (None, "kurve", "boost"):
        assert decide_boost(room_actual=raum_ist, room_target=nachher, previous_room_target=vorher,
                            boost_was_active=aktiv, arrival_threshold_k=0.5,
                            aufheizen=aufheizen, anstieg_vom_kunden=True) is erwartet


@pytest.mark.parametrize("fehlt", [["aufheizen"], ["anstieg_vom_kunden"], ["aufheizen", "anstieg_vom_kunden"]])
def test_aufheizen_und_herkunft_des_anstiegs_sind_pflicht(fehlt):
    """Ein vergessener Parameter darf nicht still das Verhalten eines Kundenanstiegs ergeben: er ist ein Fehler."""
    angaben = {"aufheizen": "kurve", "anstieg_vom_kunden": False}
    for name in fehlt:
        del angaben[name]
    with pytest.raises(TypeError):
        decide_boost(room_actual=18.0, room_target=21.0, previous_room_target=20.0,
                     boost_was_active=False, arrival_threshold_k=0.5, **angaben)


@pytest.mark.parametrize("aufheizen", ["boost", "kurve", None])
def test_ein_laufender_boost_endet_unabhaengig_vom_aufheizen_an_der_ankunft(aufheizen):
    """Das Profil entscheidet nur ueber den Start; Weiterlaufen und Ende bleiben (Ankunft, Raum noch kalt)."""
    for raum_ist, bleibt in ((19.0, True), (20.5, False)):
        assert decide_boost(room_actual=raum_ist, room_target=21.0, previous_room_target=21.0,
                            boost_was_active=True, arrival_threshold_k=0.5,
                            aufheizen=aufheizen, anstieg_vom_kunden=False) is bleibt


@pytest.mark.parametrize("aufheizen", ["boost", "kurve", None])
def test_kein_boost_ohne_anstieg_auch_nicht_bei_aufheizen_boost(aufheizen):
    for vorher, nachher in ((20.0, 20.0), (20.0, 19.0), (None, 21.0), (20.0, 20.0 + boost._TARGET_RISE_EPSILON_K)):
        assert _start(vorher, nachher, aufheizen=aufheizen, anstieg_vom_kunden=False) is False


def test_bei_kurve_ist_der_anstieg_nach_dem_nachziehen_verbraucht():
    """Das Soll wird beim naechsten Pruefen als Vorwert gemerkt (`previous_room_target`): bei `kurve` gibt es weder
    beim Anstieg noch danach einen Boost; der Raum waermt langsam ueber die Heizkurve."""
    assert _start(20.0, 21.0, aufheizen="kurve", anstieg_vom_kunden=False) is False
    assert _start(21.0, 21.0, aufheizen="kurve", anstieg_vom_kunden=False) is False


def test_quote_bleibt_bei_jedem_aufheizen_unueberschritten():
    """Auch mit Profil `boost` bleibt die Quote (hoechstens 6 Starts am Tag, 1800 s Abstand): zehn Plananstiege im
    Abstand von 1800 s starten hoechstens sechsmal, und nie zwei Starts naeher als 1800 s."""
    regel = write_budget.START_QUOTA
    assert (regel.max_per_day, regel.waits) == (6, (1800,))
    tag = "2026-11-13"
    eintrag = None
    starts = []
    for i in range(10):
        jetzt = 1000.0 + i * 1800
        if _start(20.0, 21.0, aufheizen="boost", anstieg_vom_kunden=False) and write_budget.allowed(
                eintrag, regel, jetzt, tag):
            eintrag = write_budget.counted(eintrag, jetzt, tag)
            starts.append(jetzt)
    assert len(starts) == 6
    assert all(b - a >= 1800 for a, b in zip(starts, starts[1:], strict=False))
    assert not write_budget.allowed(eintrag, regel, starts[-1] + 86000, tag)
    assert not write_budget.allowed(write_budget.counted(None, 1000.0, tag), regel, 1000.0 + 1799, tag)


def test_kurve_verbraucht_keine_quote():
    """`kurve` startet nie einen Boost, also zaehlt der Anstieg nicht gegen die Tagesquote."""
    eintrag = None
    for i in range(10):
        if _start(20.0, 21.0, aufheizen="kurve", anstieg_vom_kunden=False):
            eintrag = write_budget.counted(eintrag, 1000.0 + i * 1800, "2026-11-13")
    assert eintrag is None
