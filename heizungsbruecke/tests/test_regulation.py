"""Lokaler Check und "Tick faellig?" (regulation.py) mit echtem StateStore und Hebel-Pipeline."""
import dataclasses
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, datei_stand, logzeilen, runtime_config

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import wallclock, write_budget
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.plan import SollErgebnis
from smartheat_core.safety import LocalSafety
from smartheat_runtime import regulation
from smartheat_runtime.backup_store import load_backup
from smartheat_runtime.plan_quelle import PlanEingabe
from smartheat_runtime.roles import ChannelManifest

# Unterscheidbar: Notfall (= Clamp-Maximum) 0.8/5.0, Comfort 0.5/2.0, Wiederherstellungspunkt 0.3/1.0.
SAFETY = LocalSafety(
    ranges={"curve": (0.2, 0.8), "room_setpoint": (0.0, 5.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={"curve": 0.5, "room_setpoint": 2.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
ROOM_ROLES = {"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"}
ENTITY_IDS = {**ROOM_ROLES, "curve_current": "number.curve", "shift_current": "number.shift"}
RESTORE_POINT = {"restore_point": {"curve": 0.3, "room_setpoint": 1.0}}
EMERGENCY = [("number.curve", 0.8), ("number.shift", 5.0)]
COMFORT = [("number.curve", 0.5), ("number.shift", 2.0)]
RESTORE = [("number.curve", 0.3), ("number.shift", 1.0)]


def _runtime(
    store, *, room_actual=20.0, room_target=21.0, entity_ids=ENTITY_IDS, states=None, safety=SAFETY, clock=None,
):
    """`room_target` ist der Stable-Target-Cache (None = nicht befuellt); `states` ergaenzt die
    HA-Werte (eine Exception als Wert wird beim Lesen geworfen)."""
    values = {"sensor.room_actual": room_actual, **(states or {})}
    ha_api = MagicMock(spec=HomeAssistantApi)
    ha_api.get_attribute.side_effect = ValueError("kein Wertebereich")  # wie eine Entity ohne min/max

    def _get_state(entity_id):
        value = values[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    ha_api.get_state.side_effect = _get_state
    manifest = ChannelManifest(refs=entity_ids)
    if room_target is not None:
        store.update(stable_target=room_target)
    return SimpleNamespace(
        manifest=manifest, signals=ha_api, ha_api=ha_api, config=runtime_config(), store=store,
        states=values, clock=clock or time.monotonic, boost_mono_start=None, kaelteschutz_mono_start=None, soll_quelle_aus=False,
        uhr_vertrauen=lambda: False, geltendes_soll=None,
        override=LeverPipeline(
            store, HaPlantBinding(ha_api, manifest, VAILLANT_MYPYLLANT), safety, **({} if clock is None else {"clock": clock}),
        ),
    )


#: Ursprungswerte der Vaillant-Rueckstellung, schon gemerkt: ein Check im Beharrungszustand liest und schreibt dann nur
#: noch, was er braucht (der erste Check merkt sie einmalig).
URSPRUENGE = {"originals": {"curve": 1.0, "room_setpoint": 20.0}, "aux_originals": {"mode_select": "keine_betriebsart"}}


def _writes(rt):
    return [call.args for call in rt.ha_api.set_number_value.call_args_list]


# --- run_local_check ---

def test_check_reads_only_room_actual_and_uses_cached_target(make_store):
    rt = _runtime(make_store(backup=dict(URSPRUENGE)), room_actual=19.0)

    regulation.run_local_check(rt)

    # nur der Fuehler wird gelesen (fuer den Check und den Kaelteschutz), nie das Soll: es kommt aus dem Cache
    assert {call.args[0] for call in rt.ha_api.get_state.call_args_list} == {"sensor.room_actual"}
    assert rt.store.state.boost_active is False  # ohne Vorwert keine "Erhoehung"
    assert _writes(rt) == []


def test_check_without_cached_target_is_skipped_visibly(make_store, caplog):
    rt = _runtime(make_store(), room_target=None)

    with caplog.at_level(logging.WARNING):
        regulation.run_local_check(rt)

    # ohne Soll laeuft nur die Frostnaehe des Kaelteschutzes: der Fuehler wird gelesen, sonst nichts
    assert {call.args[0] for call in rt.ha_api.get_state.call_args_list} <= {"sensor.room_actual"}
    assert _writes(rt) == []
    assert "room_target=None" in caplog.text


def test_check_without_room_roles_does_nothing(make_store):
    rt = _runtime(make_store(), entity_ids={"curve_current": "number.curve"})

    regulation.run_local_check(rt)

    rt.ha_api.get_state.assert_not_called()


def test_check_propagates_read_errors(make_store):
    rt = _runtime(make_store(), room_actual=RuntimeError("HA nicht erreichbar"))

    with pytest.raises(RuntimeError):
        regulation.run_local_check(rt)


def test_check_records_last_room_target(make_store, tmp_path):
    rt = _runtime(make_store(), entity_ids=ROOM_ROLES)

    regulation.run_local_check(rt)

    backup = load_backup(tmp_path / "backup.json")
    assert backup["last_room_target"] == 21.0

    rt.store.update(stable_target=22.0)
    regulation.run_local_check(rt)

    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 22.0


def test_target_raise_starts_comfort_boost(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_room_target": 20.0, **RESTORE_POINT}), room_actual=19.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT
    assert load_backup(tmp_path / "backup.json")["boost_active"] is True


def test_check_gibt_aufheizen_und_herkunft_des_anstiegs_ausdruecklich_weiter(make_store, monkeypatch):
    """Die Regelung kennt heute nur den Anstieg durch den Kunden (Wunschaenderung am Thermostat) und kein Profil;
    beides geht ausdruecklich an `decide_boost`, dessen Parameter keine Standardwerte haben."""
    aufrufe = []
    echt = regulation.decide_boost

    def spaeher(*args, **kwargs):
        aufrufe.append(kwargs)
        return echt(*args, **kwargs)

    monkeypatch.setattr(regulation, "decide_boost", spaeher)
    rt = _runtime(make_store(backup={"last_room_target": 20.0, **RESTORE_POINT}), room_actual=19.0)

    regulation.run_local_check(rt)

    assert len(aufrufe) == 1
    assert aufrufe[0]["aufheizen"] is None and aufrufe[0]["anstieg_vom_kunden"] is True
    assert _writes(rt) == COMFORT


def test_thermostat_step_of_half_a_kelvin_starts_comfort_boost(make_store, tmp_path):
    """Ein Thermostatschritt von 0,5 K startet den Comfort-Boost."""
    rt = _runtime(make_store(backup={"last_room_target": 20.5, **RESTORE_POINT}), room_actual=20.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT and load_backup(tmp_path / "backup.json")["boost_active"] is True


def test_floor_heating_never_starts_comfort_boost_on_a_rise(make_store, tmp_path):
    """Ohne Comfort-Boost-Zeile (Fussbodenheizung) startet auch eine echte Erhoehung keinen Boost."""
    fbh = dataclasses.replace(SAFETY, comfort_boost={})
    rt = _runtime(make_store(backup={"last_room_target": 20.0, **RESTORE_POINT}), room_actual=18.0, room_target=22.0,
                  safety=fbh)

    regulation.run_local_check(rt)

    assert _writes(rt) == [] and load_backup(tmp_path / "backup.json").get("boost_active") is not True


def test_comfort_boost_ends_on_arrival_and_restores(make_store, tmp_path):
    rt = _runtime(
        make_store(backup={"last_room_target": 21.0, "boost_active": True, **RESTORE_POINT}), room_actual=20.8,
    )

    regulation.run_local_check(rt)

    assert _writes(rt) == RESTORE
    assert load_backup(tmp_path / "backup.json")["boost_active"] is False


def test_boost_endet_nach_4h_monoton_auch_wenn_die_wanduhr_zurueckspringt(make_store, clock, monkeypatch):
    """Die Wanduhr springt nach dem Start zurueck, sodass sie erst 1 h Boost zeigt; die monotone Uhr zaehlt
    die 4 h und beendet den Boost (vorher lief er weiter, bis die Wanduhr wieder aufgeholt hatte)."""
    beginn = datetime(2026, 10, 8, 6, 0).astimezone()
    wand = {"t": beginn}
    monkeypatch.setattr(wallclock, "_now", lambda: wand["t"])
    rt = _runtime(make_store(backup={"last_room_target": 20.0, **RESTORE_POINT}), room_actual=19.0, room_target=21.0,
                  clock=clock)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active and _writes(rt) == COMFORT

    clock.advance(4 * 3600 - 1)
    wand["t"] = beginn + timedelta(hours=1)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active and _writes(rt) == COMFORT  # Gegenprobe: eine Sekunde vor der Grenze

    clock.advance(1)
    regulation.run_local_check(rt)
    assert not rt.store.state.boost_active and rt.store.state.boost_since is None
    assert _writes(rt) == COMFORT + RESTORE


def test_ein_neuer_boost_zaehlt_die_4h_ab_seinem_eigenen_start(make_store, clock):
    """Gegenprobe: der monotone Start eines beendeten Boosts wird geloescht; ein spaeterer Boost wird nicht
    nach dem alten Start beendet."""
    rt = _runtime(make_store(backup={"last_room_target": 20.0, **RESTORE_POINT}), room_actual=19.0, room_target=21.0,
                  clock=clock)
    regulation.run_local_check(rt)
    rt.states["sensor.room_actual"] = 20.8  # angekommen: der erste Boost endet
    regulation.run_local_check(rt)
    assert not rt.store.state.boost_active and rt.boost_mono_start is None

    clock.advance(5 * 3600)
    rt.states["sensor.room_actual"] = 19.0
    rt.store.update(stable_target=22.0)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active
    clock.advance(3600)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active


def test_steady_state_check_does_not_write_backup(make_store, tmp_path):
    # Datentraeger: ein Check ohne Aenderung schreibt nichts (Inode und Aenderungszeit von backup.json bleiben).
    store = make_store(backup={
        "last_room_target": 20.0, "boost_active": False, "last_published_target_rt": 20.0, **URSPRUENGE,
    })
    rt = _runtime(store, room_actual=20.0, room_target=20.0)
    vorher = datei_stand(tmp_path / "backup.json")

    regulation.run_local_check(rt)

    assert vorher is not None and datei_stand(tmp_path / "backup.json") == vorher


@pytest.mark.parametrize("notbetrieb,expected", [(True, EMERGENCY), (False, [])])
def test_emergency_boost_starts_only_during_notbetrieb(make_store, notbetrieb, expected):
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": notbetrieb})
    rt = _runtime(store, room_actual=18.5, room_target=20.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == expected
    assert store.state.emergency_boost_active is notbetrieb


def test_emergency_boost_ends_once_notbetrieb_is_over(make_store):
    store = make_store(backup={"emergency_boost_active": True, **RESTORE_POINT})
    rt = _runtime(store, room_actual=18.5, room_target=20.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == RESTORE
    assert store.state.emergency_boost_active is False


def test_comfort_start_during_emergency_boost_does_not_write(make_store):
    store = make_store(
        backup={"emergency_boost_active": True, "last_room_target": 20.0, **RESTORE_POINT},
        failsafe={"failsafe_active": True},
    )
    rt = _runtime(store, room_actual=19.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert (store.state.boost_active, store.state.emergency_boost_active) == (True, True)


def test_emergency_end_hands_device_to_running_comfort_boost(make_store):
    store = make_store(backup={
        "emergency_boost_active": True, "boost_active": True, "last_room_target": 21.0, **RESTORE_POINT,
    })
    rt = _runtime(store, room_actual=19.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT
    assert (store.state.boost_active, store.state.emergency_boost_active) == (True, False)


def test_emergency_end_without_comfort_boost_restores(make_store):
    store = make_store(backup={"emergency_boost_active": True, "last_room_target": 21.0, **RESTORE_POINT})
    rt = _runtime(store, room_actual=19.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == RESTORE


def test_refused_comfort_start_keeps_target_rise_pending(make_store, tmp_path):
    # B5: ohne Wiederherstellungspunkt bleibt der alte Sollwert gemerkt, der naechste Check
    # sieht die Erhoehung erneut.
    store = make_store(backup={"last_room_target": 20.0})
    rt = _runtime(
        store, room_actual=19.0, room_target=21.0,
        states={"number.curve": RuntimeError("Cloud nicht erreichbar"), "number.shift": 22.0},
    )

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 20.0

    rt.states["number.curve"] = 0.9
    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT
    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 21.0


DAY = "2026-10-03"
NEXT_DAY = "2026-10-04"


def test_a_comfort_start_refused_for_the_day_uses_up_the_increase(make_store, tmp_path, monkeypatch):
    # Nach 6 Starts am Tag gilt die Erhoehung als verbraucht; sonst startete nach dem Tageswechsel ein
    # Boost fuer eine Erhoehung vom Vortag.
    monkeypatch.setattr(write_budget, "today", lambda: DAY)
    store = make_store(backup={
        "last_room_target": 20.0, **RESTORE_POINT, "write_budget": {write_budget.BOOST_START: {"day": DAY, "count": 6}},
    })
    rt = _runtime(store, room_actual=19.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert rt.store.state.boost_active is False
    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 21.0

    monkeypatch.setattr(write_budget, "today", lambda: NEXT_DAY)
    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert rt.store.state.boost_active is False


def test_a_comfort_start_refused_for_the_wait_keeps_the_increase(make_store, clock, tmp_path, monkeypatch):
    # Gegenprobe: die 30 Minuten Abstand sind keine Tagesgrenze, der naechste Check holt den Start nach.
    monkeypatch.setattr(write_budget, "today", lambda: DAY)
    store = make_store(backup={"last_room_target": 20.0, **RESTORE_POINT})
    rt = _runtime(store, room_actual=19.0, room_target=21.0, clock=clock)
    write_budget.put(store, write_budget.BOOST_START, {"day": DAY, "count": 1, "last": clock.now})

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 20.0

    clock.advance(1800)
    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT
    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 21.0


def test_a_rise_with_a_warm_room_does_not_start_nor_count_and_does_not_block_a_real_boost(
    make_store, clock, tmp_path, monkeypatch, caplog,
):
    """Regel 4: eine Erhoehung bei angekommenem Raum (>= Soll - 0,5 K) startet keinen Boost, zaehlt nicht
    und schreibt nichts; eine echte Erhoehung bei kaltem Raum kurz danach startet ohne Wartezeit."""
    monkeypatch.setattr(write_budget, "today", lambda: DAY)
    store = make_store(backup={"last_room_target": 15.0, **RESTORE_POINT})
    rt = _runtime(store, room_actual=20.0, room_target=18.0, clock=clock)

    with caplog.at_level(logging.DEBUG):
        regulation.run_local_check(rt)

        assert _writes(rt) == []
        assert rt.store.state.boost_active is False
        assert write_budget.get(store, write_budget.BOOST_START) is None
        assert load_backup(tmp_path / "backup.json")["last_room_target"] == 18.0

        clock.advance(600)
        rt.store.update(stable_target=22.0)
        rt.states["sensor.room_actual"] = 20.5
        regulation.run_local_check(rt)

        assert _writes(rt) == COMFORT
        assert rt.store.state.boost_active is True
        assert write_budget.get(store, write_budget.BOOST_START)["count"] == 1
        assert "wartezeit" not in caplog.text


def test_check_after_abo_finished_writes_nothing(make_store, tmp_path):
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": True})
    rt = _runtime(store, room_actual=18.0, room_target=21.0)
    store.update(abo_finished=True)
    before = load_backup(tmp_path / "backup.json")

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert load_backup(tmp_path / "backup.json") == before


def test_check_in_abo_inactive_mode_runs_emergency_boost(make_store):
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": True})
    store.update(abo_inactive_since=datetime(2026, 9, 25).astimezone())
    rt = _runtime(store, room_actual=18.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == EMERGENCY


# --- claim_due_tick ---

def test_claim_due_tick_claims_target_change_and_books_it(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=20.5)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 9, 0)) == "target_change"
    assert load_backup(tmp_path / "backup.json")["last_published_target_rt"] == 20.5


def test_claim_due_tick_returns_none_and_writes_nothing_when_nothing_is_due(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=21.0)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 9, 0)) is None
    assert load_backup(tmp_path / "backup.json") == {"last_published_target_rt": 21.0}


def test_claim_due_tick_claims_daily_once_per_day(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=21.0)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) == "daily"
    assert load_backup(tmp_path / "backup.json")["last_daily_trigger_date"] == "2026-09-17"
    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 15, 0)) is None


def test_claim_due_tick_sends_daily_when_a_target_change_falls_on_the_daily_check(make_store, tmp_path):
    # Der Server lernt nur auf "daily" und wendet die Vorsteuerung auch dort an: ein gebuchter
    # Tagestick darf nicht als "target_change" rausgehen, sonst faellt der Lernschritt des Tages aus.
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=22.0)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) == "daily"
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["last_published_target_rt"], backup["last_daily_trigger_date"]) == (22.0, "2026-09-17")


def test_claim_due_tick_sends_target_change_when_daily_is_already_booked(make_store):
    rt = _runtime(
        make_store(backup={"last_published_target_rt": 21.0, "last_daily_trigger_date": "2026-09-17"}),
        room_target=22.0,
    )

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 14, 0)) == "target_change"


def test_claim_due_tick_is_claimed_again_after_a_failed_booking(make_store, tmp_path, monkeypatch):
    # Wie 0.16.0: ohne gespeicherte Buchung kein Tick, der naechste Check beansprucht ihn erneut.
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=20.5)

    def _broken_save(path, values):
        raise OSError("Datentraeger kaputt")

    with monkeypatch.context() as patch:
        patch.setattr("smartheat_runtime.backup_store.save_backup", _broken_save)
        with pytest.raises(OSError):
            regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5))
    assert (rt.store.state.last_published_target_rt, rt.store.state.last_daily_trigger_date) == (21.0, None)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) == "daily"
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["last_published_target_rt"], backup["last_daily_trigger_date"]) == (20.5, "2026-09-17")


@pytest.mark.parametrize("abo_inactive,room_target,now", [
    (True, 20.5, datetime(2026, 9, 17, 12, 5)),
    (False, None, datetime(2026, 9, 17, 9, 0)),  # ohne Soll nur der faellige Tagestick
])
def test_claim_due_tick_needs_active_abo_and_without_target_a_due_daily_tick(make_store, abo_inactive, room_target, now):
    store = make_store(backup={"last_published_target_rt": 21.0})
    rt = _runtime(store, room_target=room_target)
    if abo_inactive:
        store.update(abo_inactive_since=datetime(2026, 9, 25).astimezone())

    assert regulation.claim_due_tick(rt, now) is None


def test_claim_due_tick_without_cached_target_still_claims_the_due_daily_tick(make_store, tmp_path):
    # Der Versuch liest live und meldet das tote Thermostat als Datenfehler.
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=None)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) == "daily"
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["last_published_target_rt"], backup["last_daily_trigger_date"]) == (21.0, "2026-09-17")
    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 15, 0)) is None


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


def test_failed_save_keeps_the_previous_target_in_memory(make_store, monkeypatch):
    # N5: wie 0.16.0 -- ein nicht gespeicherter neuer Sollwert gilt noch nicht als gesehen.
    store = make_store(backup={"last_room_target": 21.0, "boost_active": True, **RESTORE_POINT})
    rt = _runtime(store, room_actual=20.0, room_target=21.5)
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)

    with pytest.raises(OSError):
        regulation.run_local_check(rt)

    assert rt.store.state.last_room_target == 21.0


# --- Mutationsprobe ---

@pytest.mark.parametrize("entity_ids", [
    {"room_target": "sensor.room_target", **{k: v for k, v in ENTITY_IDS.items() if not k.startswith("room_")}},
    {"room_actual": "sensor.room_actual", **{k: v for k, v in ENTITY_IDS.items() if not k.startswith("room_")}},
])
def test_check_needs_both_room_roles(make_store, entity_ids):
    store = make_store(backup={"last_room_target": 20.0, **RESTORE_POINT})
    rt = _runtime(store, room_actual=18.0, room_target=21.0, entity_ids=entity_ids)

    regulation.run_local_check(rt)

    rt.ha_api.get_state.assert_not_called()
    assert _writes(rt) == [] and store.state.room_actual_misses == 0


@pytest.mark.parametrize("room_target", [21.0, None])
def test_claim_due_tick_without_daily_trigger_time_claims_no_daily_tick(make_store, room_target):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=room_target)
    rt.config = runtime_config(daily_trigger_time=None)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) is None
    assert rt.store.state.last_daily_trigger_date is None


def test_an_unreadable_room_target_logs_the_error_text_from_ha_only_escaped(make_store, caplog):
    """Der Fehlertext beim Lesen des Raum-Solls aus HA steht nur als Escape im Log."""
    rt = _runtime(make_store(), room_target=21.0, states={"sensor.room_target": RuntimeError(FREMDTEXT)})
    with caplog.at_level(logging.WARNING, logger="smartheat_runtime.regulation"):
        regulation.refresh_stable_target(rt)
    assert f"room_target nicht lesbar, Stable-Target-Cache bleibt bei 21.0: {FREMDTEXT_IM_LOG}" in logzeilen(caplog)


# --- geltendes Soll aus dem Plan ---

BERLIN = ZoneInfo("Europe/Berlin")
WOCHENPLAN = [{"tag": tag, "warm_ab": "06:30", "kuehl_ab": "22:30"} for tag in range(1, 8)]
ABWESEND = {"bis": "2026-12-01T10:00:00Z", "temp": 17.0, "aufheizen_ab": None}


def _berlin(stunde, minute=0, tag=2):
    return datetime(2026, 11, tag, stunde, minute, tzinfo=BERLIN)


def _mit_plan(rt, monkeypatch, *, jetzt, bedienung=None, konfiguration=None, vertrauen=True, zeitzone="Europe/Berlin"):
    """Haengt eine Planquelle an die Laufzeit; das Ergebnis ist die veraenderliche Eingabe (`jetzt`, `bedienung`,
    `konfiguration`, `vertrauen`), die ein Test zwischen den Checks aendert."""
    eingabe = {
        "jetzt": jetzt, "vertrauen": vertrauen,
        "bedienung": bedienung if bedienung is not None else {"kreis": 1, "modus": "manuell", "raum_soll": 21.0, "wochenplan": WOCHENPLAN},
        "konfiguration": konfiguration if konfiguration is not None else {"kreis": 1, "absenkung_k": 3.0},
    }
    monkeypatch.setattr(wallclock, "_now", lambda: eingabe["jetzt"])
    rt.config = runtime_config(plan_source=lambda: PlanEingabe(zeitzone, eingabe["bedienung"], eingabe["konfiguration"]))
    rt.uhr_vertrauen = lambda: eingabe["vertrauen"]
    rt.geltendes_soll = None
    return eingabe


def _starts_comfort(rt):
    vorher = rt.store.state.boost_active
    regulation.run_local_check(rt)
    return not vorher and rt.store.state.boost_active


def test_ohne_plan_ist_das_geltende_soll_der_wunsch(make_store):
    rt = _runtime(make_store(backup={"last_room_target": 21.0, **RESTORE_POINT}), room_actual=20.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert rt.geltendes_soll == SollErgebnis(21.0, "wunsch", None)


def test_notfall_boost_liest_das_geltende_soll_der_kuehlen_phase(make_store, monkeypatch):
    """In der kuehlen Phase gilt 18 Grad (Wunsch 21, Absenkung 3 K): 19,5 Grad sind dort kein Notfall, gegen den Wunsch
    waeren es 1,5 K darunter. In der warmen Phase gilt der Wunsch, derselbe Raum startet den Notfall-Boost."""
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": True})
    rt = _runtime(store, room_actual=19.5, room_target=21.0)
    eingabe = _mit_plan(rt, monkeypatch, jetzt=_berlin(23, 30))

    regulation.run_local_check(rt)

    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (18.0, "absenkung")
    assert _writes(rt) == [] and store.state.emergency_boost_active is False

    eingabe["jetzt"] = _berlin(7, 0, tag=3)
    regulation.run_local_check(rt)

    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (21.0, "wunsch")
    assert _writes(rt) == EMERGENCY and store.state.emergency_boost_active is True


def test_kaelteschutz_rechnet_mit_dem_geltenden_soll_und_bei_soll_quelle_aus_ohne_soll(make_store, monkeypatch):
    gesehen = []
    echt = regulation._kaelteschutz_entscheiden

    def spaeher(rt, now, soll):
        gesehen.append(soll)
        return echt(rt, now, soll)

    monkeypatch.setattr(regulation, "_kaelteschutz_entscheiden", spaeher)
    rt = _runtime(make_store(backup={"last_room_target": 18.0, **RESTORE_POINT}), room_actual=19.5, room_target=21.0)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(23, 30))

    regulation.run_local_check(rt)
    rt.soll_quelle_aus = True
    regulation.run_local_check(rt)

    assert gesehen == [18.0, None]
    assert rt.geltendes_soll == SollErgebnis(None, None, None)


def test_die_soll_quelle_aus_schreibt_nichts_auch_nicht_ueber_den_plan(make_store, monkeypatch):
    rt = _runtime(make_store(backup={"last_room_target": 21.0, **RESTORE_POINT}), room_actual=15.0, room_target=21.0)
    rt.soll_quelle_aus = True
    _mit_plan(rt, monkeypatch, jetzt=_berlin(23, 30))

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert [aufruf for aufruf in rt.ha_api.method_calls if aufruf[0].startswith(("set_", "call_"))] == []


@pytest.mark.parametrize("profil,startet", [("boost", True), ("kurve", False), (None, False), ("unbekannt", False)])
def test_ein_anstieg_durch_den_plan_boostet_nur_beim_profil_boost(make_store, monkeypatch, profil, startet):
    rt = _runtime(make_store(backup={"last_room_target": 18.0, **RESTORE_POINT}), room_actual=18.0, room_target=21.0)
    konfiguration = {"kreis": 1, "absenkung_k": 3.0, **({} if profil is None else {"aufheizen": profil})}
    eingabe = _mit_plan(rt, monkeypatch, jetzt=_berlin(6, 0), konfiguration=konfiguration)
    regulation.run_local_check(rt)  # kuehle Phase, Soll 18: kein Anstieg
    assert rt.store.state.boost_active is False

    eingabe["jetzt"] = _berlin(6, 31)  # Beginn der warmen Phase: das Soll steigt auf 21
    regulation.run_local_check(rt)

    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (21.0, "wunsch")
    assert rt.store.state.boost_active is startet
    assert rt.store.state.last_room_target == 21.0  # der Anstieg ist verbraucht, auch ohne Boost


def test_die_erhoehung_durch_den_kunden_boostet_auch_beim_profil_kurve(make_store, monkeypatch):
    rt = _runtime(make_store(backup={"last_room_target": 21.0, **RESTORE_POINT}), room_actual=19.0, room_target=21.0)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(12, 0), konfiguration={"kreis": 1, "absenkung_k": 3.0, "aufheizen": "kurve"})
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active is False

    rt.store.update(stable_target=22.0)
    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is True


def test_nach_dem_start_gilt_ein_anstieg_ohne_bekannte_herkunft_nicht_als_der_des_kunden(make_store, monkeypatch):
    """Nach einem Neustart fehlt die Art des Solls davor: ein Anstieg gegen das gemerkte Soll kann der Plan sein und
    boostet beim Profil kurve nicht; ab dem zweiten Check kennt die Laufzeit die Herkunft."""
    rt = _runtime(make_store(backup={"last_room_target": 18.0, **RESTORE_POINT}), room_actual=18.0, room_target=21.0)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(7, 0), konfiguration={"kreis": 1, "absenkung_k": 3.0, "aufheizen": "kurve"})

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is False and rt.store.state.last_room_target == 21.0


def test_in_der_testphase_ohne_setwell_gibt_es_keinen_plan_boost_auch_beim_profil_boost(make_store, monkeypatch):
    rt = _runtime(make_store(backup={"last_room_target": 18.0, **RESTORE_POINT}), room_actual=18.0, room_target=21.0)
    eingabe = _mit_plan(rt, monkeypatch, jetzt=_berlin(6, 0), konfiguration={"kreis": 1, "absenkung_k": 3.0, "aufheizen": "boost"})
    rt.config = dataclasses.replace(rt.config, zustand_source=lambda: "testphase_ohne")
    regulation.run_local_check(rt)
    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (21.0, "wunsch")  # ohne Setwell gilt kein Plan
    eingabe["jetzt"] = _berlin(6, 31)

    regulation.run_local_check(rt)

    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (21.0, "wunsch")
    assert rt.store.state.boost_active is False


def test_bei_unklarer_uhr_gilt_der_wunsch_und_der_naechste_check_rechnet_neu(make_store, monkeypatch):
    rt = _runtime(make_store(backup={"last_room_target": 21.0, **RESTORE_POINT}), room_actual=21.0, room_target=21.0)
    eingabe = _mit_plan(rt, monkeypatch, jetzt=_berlin(23, 30), vertrauen=False)

    regulation.run_local_check(rt)

    assert rt.geltendes_soll == SollErgebnis(21.0, "wunsch", None, uhr_unklar=True)

    eingabe["vertrauen"] = True
    eingabe["jetzt"] = _berlin(23, 35)
    regulation.run_local_check(rt)

    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art, rt.geltendes_soll.uhr_unklar) == (18.0, "absenkung", False)


def test_eine_unbrauchbare_zone_haelt_die_regelung_nicht_an(make_store, monkeypatch, caplog):
    rt = _runtime(make_store(backup={"last_room_target": 21.0, **RESTORE_POINT}), room_actual=21.0, room_target=21.0)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(23, 30), zeitzone="Mars/Olympus")

    with caplog.at_level(logging.WARNING):
        regulation.run_local_check(rt)

    assert rt.geltendes_soll == SollErgebnis(21.0, "wunsch", None)
    assert "Mars" not in caplog.text


@pytest.mark.parametrize("profil,starts", [("boost", 6), ("kurve", 0)])
def test_zehn_plananstiege_an_einem_tag_halten_die_quote_von_sechs_starts(make_store, clock, monkeypatch, profil, starts):
    """Abwesenheit und Rueckkehr im Wechsel: jede Rueckkehr hebt das Soll von 17 auf 21 Grad durch den Plan, 60 Minuten
    nach der letzten, der Raum ist jedes Mal kalt. Mit Profil boost startet die Quote des Schreibbudgets sechs
    Comfort-Boosts am Tag, danach keinen mehr; mit Profil kurve keinen."""
    rt = _runtime(make_store(backup={"last_room_target": 17.0, **RESTORE_POINT}), room_actual=17.5, room_target=21.0, clock=clock)
    weg = {"kreis": 1, "modus": "abwesend", "raum_soll": 21.0, "abwesend": ABWESEND}
    da = {"kreis": 1, "modus": "manuell", "raum_soll": 21.0}
    eingabe = _mit_plan(rt, monkeypatch, jetzt=_berlin(6, 0), bedienung=weg,
                        konfiguration={"kreis": 1, "absenkung_k": 3.0, "aufheizen": profil})
    gestartet = 0
    for _ in range(10):
        eingabe["bedienung"], eingabe["jetzt"] = weg, eingabe["jetzt"] + timedelta(minutes=30)
        clock.advance(1800)
        regulation.run_local_check(rt)  # Abwesend: das Soll sinkt, ein laufender Boost endet
        assert rt.geltendes_soll.soll_art == "abwesend" and rt.store.state.boost_active is False
        eingabe["bedienung"], eingabe["jetzt"] = da, eingabe["jetzt"] + timedelta(minutes=30)
        clock.advance(1800)
        gestartet += _starts_comfort(rt)

    assert gestartet == starts


def test_fehlt_der_plan_nach_einer_absenkung_gilt_der_anstieg_nicht_als_der_des_kunden(make_store, monkeypatch):
    """Das Dokument verliert kurz sein Feld, waehrend die Absenkung gilt: das Soll wird zum Wunsch und steigt, aber der
    Plan hat das Soll vorher gesenkt, nicht der Kunde. Beim Profil kurve gibt es dann keinen Comfort-Boost."""
    rt = _runtime(make_store(backup={"last_room_target": 18.0, **RESTORE_POINT}), room_actual=18.0, room_target=21.0)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(23, 30), konfiguration={"kreis": 1, "absenkung_k": 3.0, "aufheizen": "kurve"})
    regulation.run_local_check(rt)
    assert rt.geltendes_soll.soll_art == "absenkung" and rt.store.state.boost_active is False

    rt.config = runtime_config(plan_source=lambda: None)
    regulation.run_local_check(rt)

    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (21.0, "wunsch")
    assert rt.store.state.boost_active is False


def test_ohne_jeden_plan_ist_ein_anstieg_des_wunsches_der_des_kunden(make_store):
    rt = _runtime(make_store(backup={"last_room_target": 20.0, **RESTORE_POINT}), room_actual=18.0, room_target=21.0)
    regulation.run_local_check(rt)
    rt.store.update(stable_target=22.0)

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is True


# --- ein Fehler der Plan-Eingabe haelt den Kaelteschutz nicht an ---

def _notfall_lage(make_store):
    """Notbetrieb mit zu kaltem Raum: der Notfall-Boost startet, sobald die Pruefung bis dorthin kommt."""
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": True})
    return store, _runtime(store, room_actual=19.5, room_target=21.0)


def _ausnahme(*_args, **_kwargs):
    raise RuntimeError("Fremdwert 99")


def test_eine_ausnahme_der_planquelle_laesst_den_kaelteschutz_laufen(make_store, monkeypatch, caplog):
    store, rt = _notfall_lage(make_store)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(12, 0))
    rt.config = runtime_config(plan_source=_ausnahme)

    with caplog.at_level(logging.WARNING):
        regulation.run_local_check(rt)

    assert _writes(rt) == EMERGENCY and store.state.emergency_boost_active is True
    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (21.0, "wunsch")
    assert "Fremdwert" not in caplog.text and "RuntimeError" in caplog.text


def test_eine_ausnahme_des_uhrurteils_laesst_den_kaelteschutz_laufen(make_store, monkeypatch):
    store, rt = _notfall_lage(make_store)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(12, 0))
    rt.uhr_vertrauen = _ausnahme

    regulation.run_local_check(rt)

    assert _writes(rt) == EMERGENCY and store.state.emergency_boost_active is True


def test_eine_ausnahme_der_planrechnung_laesst_den_kaelteschutz_laufen(make_store, monkeypatch):
    store, rt = _notfall_lage(make_store)
    _mit_plan(rt, monkeypatch, jetzt=_berlin(12, 0))
    monkeypatch.setattr("smartheat_runtime.plan_quelle.soll_effektiv", _ausnahme)

    regulation.run_local_check(rt)

    assert _writes(rt) == EMERGENCY and store.state.emergency_boost_active is True
    assert (rt.geltendes_soll.soll, rt.geltendes_soll.soll_art) == (21.0, "wunsch")
