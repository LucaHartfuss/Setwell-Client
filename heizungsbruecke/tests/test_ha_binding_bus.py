"""Schreibpfad des Buswegs (vaillant_ebus, Hebelsatz vaillant_vrc720): Hebel sind number-Entities, die Betriebsart der Zone
ist ein select. Jeder Schreibvorgang geht durch dieselben lokalen Grenzen wie der Cloud-Weg (Wertebereich, 6 je Tag und
Schluessel, 1800 s Abstand, Wartezeit); Optionstexte sind eine Annahme und werden vor dem Schreiben geprueft."""
import logging
from datetime import date

import pytest
from bindung_beispiele import PARAMETER
from fakes import BUS_OPTIONEN, BUS_ZIELE, BusHa, FakeNotifySink, runtime_config

from heizungsbruecke.ha_binding import (
    BUS_BETRIEBSARTEN,
    BUS_MANUELL,
    BUS_ZEITPROGRAMM,
    HaPlantBinding,
    VaillantBusHaBinding,
    binding_for,
)
from smartheat_core import enforce, write_budget
from smartheat_core.bindungsparameter import beschreibung
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.worker import RegulationWorker

BESCHREIBUNG = beschreibung("vaillant_ebus", "vaillant_vrc720", PARAMETER["vaillant_vrc720"])
SETTLE = BESCHREIBUNG.settle_seconds
MODUS = BUS_ZIELE["mode_select"]
MANIFEST = ChannelManifest(refs=dict(BUS_ZIELE))
SAFETY = LocalSafety(
    ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={"curve": 1.2, "room_setpoint": 24.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
# Werte der Anlage vor SmartHeat (Ursprung) und die Werte, die der Server dann setzt.
URSPRUNG = {BUS_ZIELE["curve_current"]: 0.7, BUS_ZIELE["shift_current"]: 21.0, BUS_ZIELE["heat_limit"]: 15.0,
            BUS_ZIELE["min_flow"]: 22.0}
SERVER = {"curve": 1.1, "room_setpoint": 22.5, "heat_limit": 18.0, "min_flow": 25.0}
ENTITY = {"curve": BUS_ZIELE["curve_current"], "room_setpoint": BUS_ZIELE["shift_current"],
          "heat_limit": BUS_ZIELE["heat_limit"], "min_flow": BUS_ZIELE["min_flow"]}
TODAY = date(2026, 10, 3)


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch):
    monkeypatch.setattr(enforce, "_today", lambda: TODAY)
    monkeypatch.setattr(write_budget, "today", lambda: TODAY.isoformat())


def _binding(ha):
    return VaillantBusHaBinding(ha, MANIFEST, BESCHREIBUNG)


def _ha(mode="auto", **kwargs):
    ha = BusHa(mode=mode, **kwargs)
    ha.states.update(URSPRUNG)
    return ha


def _pipeline(make_store, clock, mode="auto", **kwargs):
    store = make_store()
    ha = _ha(mode, **kwargs)
    ha.clock = clock
    pipeline = LeverPipeline(store, _binding(ha), SAFETY, clock=clock)
    clock.advance(SETTLE + 1)
    return pipeline, store, ha


def _smartheat_laeuft(pipeline, clock, ha):
    pipeline.apply_server_values(SERVER)
    clock.advance(SETTLE + 1)
    assert ha.states[MODUS] == BUS_MANUELL
    assert {lever: ha.states[entity] for lever, entity in ENTITY.items()} == SERVER
    ha.writes.clear()


# --- Auswahl nach Weg ---------------------------------------------------------------------------------------------


def test_the_bus_way_gets_its_own_binding_and_the_cloud_way_keeps_the_climate_one():
    assert type(binding_for(BESCHREIBUNG, BusHa(), MANIFEST, "vaillant_ebus")) is VaillantBusHaBinding
    assert type(binding_for(BESCHREIBUNG, BusHa(), MANIFEST, "vaillant_myvaillant")) is HaPlantBinding


# --- Vorbereitung -------------------------------------------------------------------------------------------------


def test_prepare_sets_the_zone_to_manual_once_and_counts_the_write():
    ha = _ha("auto")
    binding = _binding(ha)
    assert binding.needs_preparation() is True
    assert binding.is_prepared() is False
    assert binding.prepare() is True
    assert ha.modus_aufrufe == [BUS_MANUELL]
    assert binding.physical_writes == 1
    assert binding.is_prepared() is True
    assert binding.prepare() is False
    assert ha.modus_aufrufe == [BUS_MANUELL]


def test_a_zone_already_on_manual_is_not_switched():
    ha = _ha(BUS_MANUELL)
    binding = _binding(ha)
    assert binding.prepare() is False
    assert ha.modus_aufrufe == []
    assert binding.physical_writes == 0


@pytest.mark.parametrize("ha", [
    pytest.param(BusHa(options=("off", "auto", "day", "night")), id="manuell_nicht_in_den_optionen"),
    pytest.param(BusHa(options=None), id="optionen_fehlen"),
    pytest.param(BusHa(options=("manual", 7)), id="optionen_unlesbar"),
])
def test_prepare_writes_nothing_when_the_option_is_not_offered(ha):
    ha.states[MODUS] = "auto"
    binding = _binding(ha)
    with pytest.raises(ValueError):
        binding.prepare()
    assert ha.modus_aufrufe == []
    assert binding.physical_writes == 0


def test_prepare_writes_nothing_when_the_select_is_unavailable():
    ha = _ha("unavailable")
    binding = _binding(ha)
    with pytest.raises(ValueError):
        binding.prepare()
    assert ha.modus_aufrufe == []


def test_the_mode_is_a_known_original_only_when_the_text_is_known():
    binding = _binding(_ha())
    assert binding.aux_zulaessig("mode_select", BUS_ZEITPROGRAMM) is True
    assert all(binding.aux_zulaessig("mode_select", option) for option in BUS_BETRIEBSARTEN)
    assert binding.aux_zulaessig("mode_select", "dry") is False
    assert binding.aux_zulaessig("curve", BUS_ZEITPROGRAMM) is False
    assert BUS_MANUELL in BUS_OPTIONEN and BUS_ZEITPROGRAMM in BUS_OPTIONEN


def test_read_aux_gives_the_raw_mode_and_fails_when_it_is_unreadable():
    assert _binding(_ha("day")).read_aux() == {"mode_select": "day"}
    with pytest.raises(ValueError):
        _binding(_ha("unavailable")).read_aux()


def test_the_number_levers_are_written_through_number_set_value():
    ha = _ha(BUS_MANUELL)
    binding = _binding(ha)
    binding.write("curve", 1.1)
    binding.write("room_setpoint", 22.5)
    assert ha.writes == [(ENTITY["curve"], 1.1), (ENTITY["room_setpoint"], 22.5)]
    assert binding.physical_writes == 2


def test_a_lever_on_an_unavailable_entity_is_not_written():
    ha = _ha(BUS_MANUELL)
    ha.states[ENTITY["curve"]] = "unavailable"
    with pytest.raises(ValueError):
        _binding(ha).write("curve", 1.1)
    assert ha.writes == []


# --- Pipeline: Vorbereitung vor dem Sollwert, Rueckstellung ueber die leere Konfiguration ---------------------------


def test_the_server_values_prepare_the_zone_first_and_clamp_to_the_local_limits(make_store, clock):
    pipeline, store, ha = _pipeline(make_store, clock, mode="auto")
    pipeline.apply_server_values({**SERVER, "curve": 9.0, "heat_limit": 99.0})
    assert ha.writes[0] == (MODUS, BUS_MANUELL)
    assert ha.states[ENTITY["curve"]] == 1.5
    assert ha.states[ENTITY["heat_limit"]] == 20.0
    assert store.state.aux_originals == {"mode_select": "auto"}


@pytest.mark.parametrize("always_restore", [True, False], ids=["abo_ende", "abmelden_und_werksreset"])
def test_every_lever_and_the_mode_go_back_and_the_mode_comes_last(make_store, clock, always_restore):
    pipeline, store, ha = _pipeline(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)

    assert pipeline.restore_and_clear(always_restore) is True

    assert {entity: ha.states[entity] for entity in URSPRUNG} == URSPRUNG
    assert ha.states[MODUS] == BUS_ZEITPROGRAMM
    assert ha.writes[-1] == (MODUS, BUS_ZEITPROGRAMM)
    assert ha.modus_aufrufe == [BUS_ZEITPROGRAMM]
    assert sorted(entity for entity, _ in ha.writes[:-1]) == sorted(URSPRUNG)
    assert store.state.aux_originals == {}


@pytest.mark.parametrize("original", [BUS_MANUELL, "off", "day", "dry"])
@pytest.mark.parametrize("always_restore", [True, False], ids=["abo_ende", "abmelden_und_werksreset"])
def test_an_original_mode_other_than_the_schedule_is_remembered_but_not_written(
        make_store, clock, original, always_restore):
    pipeline, store, ha = _pipeline(make_store, clock, mode=original)
    _smartheat_laeuft(pipeline, clock, ha)
    assert store.state.aux_originals == {"mode_select": original}

    assert pipeline.restore_and_clear(always_restore) is True

    assert {entity: ha.states[entity] for entity in URSPRUNG} == URSPRUNG
    assert ha.modus_aufrufe == []


def test_a_restore_without_the_schedule_option_writes_no_mode_and_stays_open(make_store, clock):
    pipeline, store, ha = _pipeline(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)
    ha.options = ("off", "manual")

    assert pipeline.restore_and_clear(True) is False

    assert ha.modus_aufrufe == []
    assert store.state.aux_originals == {"mode_select": "auto"}


def test_a_zone_the_customer_put_back_on_the_schedule_meanwhile_gets_no_setpoint_but_the_rest(make_store, clock):
    pipeline, store, ha = _pipeline(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)
    ha.states[MODUS] = "auto"

    assert pipeline.restore_and_clear(True) is True

    assert ENTITY["room_setpoint"] not in [entity for entity, _ in ha.writes]
    assert ha.states[ENTITY["curve"]] == URSPRUNG[ENTITY["curve"]]
    assert ha.writes[-1] == (MODUS, BUS_ZEITPROGRAMM)


def test_the_logs_carry_no_entity_id_and_no_manufacturer_text(make_store, clock, caplog):
    caplog.set_level(logging.INFO)
    pipeline, store, ha = _pipeline(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)
    pipeline.restore_and_clear(True)
    assert not [record for record in caplog.records if "ebusd" in record.getMessage()]


# --- Wartezeit ----------------------------------------------------------------------------------------------------


def test_a_read_is_trusted_only_after_the_settle_time_following_an_own_write(make_store, clock):
    pipeline, store, ha = _pipeline(make_store, clock, mode=BUS_MANUELL)
    pipeline.apply_server_values({"curve": 1.1})
    assert pipeline.settled("curve") is False
    clock.advance(SETTLE - 1)
    assert pipeline.settled("curve") is False
    clock.advance(2)
    assert pipeline.settled("curve") is True


# --- Durchsetzung: 6 je Tag und Schluessel, 1800 s Abstand --------------------------------------------------------------


def _rt(make_store, clock, ha, uptime=SETTLE + 1):
    store = make_store(backup={"restore_point": {"curve": 0.9, "room_setpoint": 21.0, "min_flow": 22.0,
                                                 "heat_limit": 15.0}})
    store.update(stable_target=21.0)
    kunde = FakeNotifySink()
    override = LeverPipeline(store, _binding(ha), SAFETY, clock=clock)
    ha.clock = clock
    clock.advance(uptime)
    rt = Runtime(
        manifest=MANIFEST, signals=ha, config=runtime_config(), worker=RegulationWorker(clock), store=store,
        override=override, notifier=Notifier(store, kunde), neustart=lambda: None, clock=clock,
    )
    rt.zone_prepared = True
    return rt


def _rounds(rt):
    for _ in range(enforce.DETECTION_ROUNDS):
        enforce.check_manual_override(rt)


def _curve_writes(rt):
    return [w for w in rt.signals.writes if w[0] == ENTITY["curve"]]


def test_the_enforcement_writes_at_most_six_times_a_day_and_starts_again_the_next_day(make_store, clock, monkeypatch):
    ha = _ha(BUS_MANUELL, reflects_writes=False)
    ha.states[ENTITY["curve"]] = 1.3
    rt = _rt(make_store, clock, ha)
    for _ in range(20):
        clock.advance(SETTLE + 1)
        _rounds(rt)
    assert len(_curve_writes(rt)) == 6
    assert [wert for _, wert in _curve_writes(rt)] == [0.9] * 6
    monkeypatch.setattr(enforce, "_today", lambda: date(2026, 10, 4))
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert len(_curve_writes(rt)) == 7


@pytest.mark.parametrize(("ago", "allowed"), [(enforce.RETRY_SECONDS - 1, False), (enforce.RETRY_SECONDS + 1, True)])
def test_the_enforcement_keeps_the_interval_between_two_writes(make_store, clock, ago, allowed):
    rt = _rt(make_store, clock, _ha(BUS_MANUELL))
    rt.store.update(write_budget={"enforce:curve": {"day": TODAY.isoformat(), "count": 1, "last": clock() - ago}})
    assert enforce.RETRY_SECONDS == 1800
    assert enforce._may_write(rt, "curve") is allowed


def test_the_enforcement_trusts_a_read_only_after_the_settle_time(make_store, clock):
    ha = _ha(BUS_MANUELL)
    ha.states[ENTITY["curve"]] = 1.3
    rt = _rt(make_store, clock, ha, uptime=0)
    _rounds(rt)
    assert _curve_writes(rt) == []
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert _curve_writes(rt) == [(ENTITY["curve"], 0.9)]
