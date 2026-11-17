"""Der Watchdog beendet Boosts auch ohne HA-Ereignis: ein Raumfuehler mit konstantem Messwert loest in HA nichts aus,
bei verbundenem Trigger-Client kommt der lokale Check dann nur ueber den Takt des Watchdogs. Volle Laufzeit mit echtem
Worker und Fake-Trigger-Client (`env`); Wanduhr der Entscheidung und monotone Uhr der Laufzeit laufen im Gleichschritt
(die Hoechstdauer zaehlt beide, die frueher ablaufende gilt)."""
from runtime_helpers import _answer, _backup, _mqtt, _quiet_backup, _set_room_target, _start, _trigger
from test_runtime import _regulation_writes, _start_in_notbetrieb_with_emergency_boost
from test_runtime_kaelteschutz import STUNDE, wall  # noqa: F401 - Fixture
from test_runtime_watchdog_kaelteschutz import TAKT, _takte

COMFORT_BOOST_WERTE = [("number.curve_current", 1.5), ("number.shift_current", 25.0)]
NORMALZUSTAND = [("number.curve_current", 0.9), ("number.shift_current", 22.0), ("number.heat_limit", 16.0)]


def _comfort_boost_laeuft(env, wall):  # noqa: F811
    """Raum 21 Grad, das Soll steigt von 21 auf 22: der Comfort-Boost startet, der Server bestaetigt den Tick. Die
    Abweichung bleibt unter der Schwelle des Kaelteschutzes (2 K), dessen Uhr also nicht laeuft und den Watchdog nicht
    antreibt. Danach aendert sich nichts mehr (kein Fuehlerereignis, keine Server-Antwort ausser dieser)."""
    _quiet_backup(env)
    env.ha.states["sensor.room_actual"] = 21.0
    bridge = _start(env)
    _set_room_target(env, bridge, 22.0)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=22.0)
    state = bridge.store.state
    assert state.boost_active
    assert state.kaelteschutz_art is None and state.kaelteschutz_zaehlung.soll_seit is None  # Kaelteschutz-Uhr aus
    assert not state.kaelteschutz_zaehlung.frost_seit
    assert env.trigger_clients[-1].connected
    assert set(COMFORT_BOOST_WERTE) <= set(_regulation_writes(env))
    return bridge


def test_der_comfort_boost_endet_nach_vier_stunden_ohne_ha_ereignis_bei_verbundenem_trigger(env, wall):  # noqa: F811
    bridge = _comfort_boost_laeuft(env, wall)

    _takte(env, bridge, wall, 4 * STUNDE // TAKT - 1)  # 3:55: die Hoechstdauer ist noch nicht erreicht
    assert bridge.store.state.boost_active
    bisher = len(_regulation_writes(env))

    _takte(env, bridge, wall, 1)  # 4:00

    assert not bridge.store.state.boost_active
    assert _backup(env)["boost_active"] is False
    assert _regulation_writes(env)[bisher:] == NORMALZUSTAND


def test_der_notfall_boost_des_notbetriebs_haelt_bis_zur_ankunft_und_endet_mit_dem_ereignis(env, wall):  # noqa: F811
    """Der Notfall-Boost des Notbetriebs hat keine Hoechstdauer (sie gilt fuer Comfort-Boost und Kaelteschutz-Lauf): er
    endet bei Ankunft des Raums am Soll, und die Ankunft ist eine Aenderung des Fuehlerwerts, also ein HA-Ereignis. Ohne
    Ereignis gibt es nichts zu beenden, auch nach Stunden bleibt er stehen."""
    bridge = _start_in_notbetrieb_with_emergency_boost(env)
    assert bridge.store.state.emergency_boost_active
    assert bridge.store.state.delivery.notbetrieb

    _takte(env, bridge, wall, 6 * STUNDE // TAKT)

    assert bridge.store.state.emergency_boost_active

    env.ha.states["sensor.room_actual"] = 21.6
    _trigger(env, bridge, "sensor.room_actual")

    assert not bridge.store.state.emergency_boost_active


def test_ohne_boost_und_ohne_kaelteschutz_loest_der_watchdog_bei_verbundenem_trigger_keinen_check_aus(env, wall):  # noqa: F811
    """Gegenprobe gegen Dauerprueferei: ohne Boost und ohne Kaelteschutz-Uhr bleibt ein neues Soll in der Entity bis zum
    Ereignis ungelesen, auch nach vielen Takten."""
    _quiet_backup(env)
    env.ha.states["sensor.room_actual"] = 21.0
    bridge = _start(env)
    assert not bridge.store.state.boost_active
    env.ha.states["sensor.room_target"] = 20.5

    _takte(env, bridge, wall, 3)

    assert bridge.store.state.stable_target == 21.0
