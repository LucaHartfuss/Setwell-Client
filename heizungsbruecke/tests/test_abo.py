"""Abo-inaktiv-Modus und Fristende (abo.py)."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import FakeClock, HaEventSink, meldungs_text, runtime_config

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_binding import HaPlantBinding
from heizungsbruecke.ha_sinks import HaNotifySink
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core.levers import LEVER_SETS
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime import abo, entitlement
from smartheat_runtime.backup_store import load_backup
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.ports import MqttChannel
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.soll_eingang import EV_SOLL_ENDE
from smartheat_runtime.status import StatusReporter
from smartheat_runtime.texts import HA_TEXTS

ABO_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))
OPTIONS = {"tenant_id": "t1"}
SAFETY = LocalSafety(
    ranges={"curve": (0.2, 0.8), "room_setpoint": (0.0, 5.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={"curve": 0.5, "room_setpoint": 2.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
BOTH_ROLES = {"curve_current": "number.curve", "shift_current": "number.shift"}


class Rekorder:
    """Stand-in fuer den Worker: merkt sich die eingestellten Ereignisse."""

    def __init__(self):
        self.ereignisse = []

    def post(self, event):
        self.ereignisse.append(event)


def _runtime(tmp_path, store, entity_ids=BOTH_ROLES, notify_services=("notify.mobile_app_handy",), clock=None,
             abo_source=lambda: entitlement.UNKNOWN, update_noetig_source=lambda: False):
    """`abo_source` ist die Abo-Quelle des Konfigurationsdokuments (Rueckruf des Hosts, RuntimeConfig),
    `update_noetig_source` die der kritischen Mindestversion."""
    manifest = ChannelManifest(refs=entity_ids)
    ha_api = MagicMock(spec=HomeAssistantApi)
    ha_api.get_attribute.side_effect = ValueError("kein Wertebereich")  # wie eine Entity ohne min/max
    return SimpleNamespace(
        manifest=manifest, signals=ha_api, ha_api=ha_api, store=store, mqtt_client=MagicMock(spec=MqttChannel),
        config=runtime_config(entitlement_path=tmp_path / "entitlement_state.json", abo_source=abo_source,
                              update_noetig_source=update_noetig_source),
        override=LeverPipeline(store, HaPlantBinding(ha_api, manifest, VAILLANT_MYPYLLANT), SAFETY),
        notifier=Notifier(store, HaNotifySink(ha_api, list(notify_services))),
        status=StatusReporter(
            HaEventSink(ha_api), OPTIONS["tenant_id"], store, LEVER_SETS["vaillant_vrc720"], ADDON_VERSION,
        ),
        clock=clock or FakeClock(), connection_failing_queried_at=None, texts=HA_TEXTS,
        uhr_vertrauen=lambda: True, frist_offen=False, worker=Rekorder(), trigger_client=None,
    )


def test_inactive_message_contains_grace_end_date():
    assert HA_TEXTS.text(abo.inactive_meldung(ABO_NOW)) == (
        "SmartHeat: Abo inaktiv. Die Heizung läuft noch bis 25.10.2026 im Notbetrieb weiter, "
        "danach stellt es die ursprünglichen Werte wieder ein und beendet die Steuerung."
    )


def test_enter_inactive_activates_notbetrieb_stops_mqtt_and_notifies_all_channels(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store(failsafe={"failsafe_active": False, "pending": {"seq": "s1", "trigger": "daily"}}))

    abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW
    assert (rt.store.state.delivery.notbetrieb, rt.store.state.delivery.pending) == (True, None)
    assert load_backup(tmp_path / "failsafe_state.json") == {"failsafe_active": True, "datenfehler": None, "pending": None}
    rt.mqtt_client.stop.assert_called_once()
    expected = HA_TEXTS.text(abo.inactive_meldung(ABO_NOW))
    rt.ha_api.send_notification.assert_called_once_with("notify.mobile_app_handy", expected)
    rt.ha_api.create_persistent_notification.assert_called_once_with("SmartHeat", expected, "smartheat_abo")


def test_enter_inactive_does_not_repeat_notification_when_already_marked(make_store, tmp_path, caplog):
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=5))
    rt = _runtime(tmp_path, make_store())

    with caplog.at_level(logging.WARNING):
        abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW - timedelta(days=5)
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()
    assert "20.10.2026" in caplog.text  # Fristende weiter im Log sichtbar


def test_enter_inactive_is_noop_when_already_in_mode(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store())
    rt.store.update(abo_inactive_since=ABO_NOW)

    abo.enter_inactive(rt, ABO_NOW)

    rt.mqtt_client.stop.assert_not_called()


def test_enter_inactive_survives_failing_channels(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store())
    rt.ha_api.send_notification.side_effect = RuntimeError("push kaputt")
    rt.ha_api.create_persistent_notification.side_effect = RuntimeError("ha kaputt")
    rt.mqtt_client.stop.side_effect = RuntimeError("paho kaputt")

    abo.enter_inactive(rt, ABO_NOW)  # darf nicht werfen

    assert rt.store.state.delivery.notbetrieb is True


def test_enter_inactive_with_failing_entitlement_persist_still_enters_mode(make_store, tmp_path, monkeypatch, caplog):
    def _failing(path, now, **optionen):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.entitlement.mark_inactive", _failing)
    rt = _runtime(tmp_path, make_store())

    with caplog.at_level(logging.ERROR):
        abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW
    assert load_backup(tmp_path / "failsafe_state.json")["failsafe_active"] is True
    rt.mqtt_client.stop.assert_called_once()
    rt.ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", HA_TEXTS.text(abo.inactive_meldung(ABO_NOW)), "smartheat_abo",
    )
    assert "Datentraeger kaputt" in caplog.text


def test_finish_grace_mid_boost_restores_learned_values_clamped(make_store, tmp_path):
    store = make_store(backup={
        "restore_point": {"curve": 0.4, "room_setpoint": 9.0},  # ueber dem Maximum 5.0 -> geclampt
        "emergency_boost_active": True, "boost_active": True,
    })
    rt = _runtime(tmp_path, store)

    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is True

    rt.ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    rt.ha_api.set_number_value.assert_any_call("number.shift", 5.0)
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["boost_active"], backup["emergency_boost_active"]) == (False, False)
    assert store.state.abo_finished is True
    rt.ha_api.send_notification.assert_called_once_with("notify.mobile_app_handy", meldungs_text("meldung.abo_beendet"))
    rt.ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", meldungs_text("meldung.abo_beendet"), "smartheat_abo",
    )


def test_finish_grace_counts_restore_as_done_when_saving_flags_fails(make_store, tmp_path, monkeypatch, caplog):
    store = make_store(backup={"restore_point": {"curve": 0.4, "room_setpoint": 2.0}, "emergency_boost_active": True})
    rt = _runtime(tmp_path, store)

    def _broken_save(path, values):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _broken_save)

    with caplog.at_level(logging.ERROR):
        assert abo.finish_grace(rt, always_restore=True, final_notice=True) is True

    assert store.state.abo_finished is True
    assert store.state.emergency_boost_active is False
    rt.ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    assert "Datentraeger kaputt" in caplog.text


def test_finish_grace_keeps_flags_when_restore_write_fails(make_store, tmp_path):
    store = make_store(backup={"restore_point": {"curve": 0.4}, "emergency_boost_active": True})
    rt = _runtime(tmp_path, store, entity_ids={"curve_current": "number.curve"})
    rt.ha_api.set_number_value.side_effect = RuntimeError("HA nicht erreichbar")

    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is False

    assert load_backup(tmp_path / "backup.json")["emergency_boost_active"] is True
    assert store.state.abo_finished is False
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()


def test_finish_grace_without_forced_restore_and_without_flags_writes_nothing(make_store, tmp_path, caplog):
    rt = _runtime(tmp_path,
        make_store(backup={"restore_point": {"curve": 0.4}, "boost_active": False}),
        entity_ids={"curve_current": "number.curve"},
    )

    with caplog.at_level(logging.INFO):
        assert abo.finish_grace(rt, always_restore=False, final_notice=False) is True

    rt.ha_api.set_number_value.assert_not_called()
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()
    assert "Frist" in caplog.text


@pytest.mark.parametrize("status, expect_inactive", [
    (entitlement.INACTIVE, True),
    (entitlement.ACTIVE, False),
    (entitlement.UNKNOWN, False),
])
def test_connection_failing_acts_only_on_a_clear_answer(make_store, tmp_path, status, expect_inactive):
    rt = _runtime(tmp_path, make_store(), abo_source=lambda: status)
    abo.handle_connection_failing(rt)
    assert (rt.store.state.abo_inactive_since is not None) is expect_inactive
    if status in (entitlement.ACTIVE, entitlement.UNKNOWN):  # normaler Ausfall: keine Meldung an den Kunden
        rt.ha_api.send_notification.assert_not_called()
        rt.ha_api.create_persistent_notification.assert_not_called()


def test_connection_failing_is_throttled(make_store, tmp_path):
    queries = []  # Abfragen der Abo-Quelle (Rueckruf des Hosts)
    rt = _runtime(tmp_path, make_store(), abo_source=lambda: queries.append(1) or entitlement.UNKNOWN)
    abo.handle_connection_failing(rt)
    abo.handle_connection_failing(rt)  # sofort danach: gedrosselt
    assert len(queries) == 1
    rt.clock.advance(599)
    abo.handle_connection_failing(rt)  # 599 s nach der Abfrage: noch gedrosselt
    assert len(queries) == 1
    rt.clock.advance(1)
    abo.handle_connection_failing(rt)  # genau 600 s: wieder abgefragt
    assert len(queries) == 2


def test_connection_failing_is_silent_in_the_abo_inactive_mode(make_store, tmp_path):
    queries = []
    rt = _runtime(tmp_path, make_store(), abo_source=lambda: queries.append(1) or entitlement.ACTIVE)
    rt.store.update(abo_inactive_since=ABO_NOW)
    abo.handle_connection_failing(rt)
    assert queries == []  # keine Abfrage der Abo-Quelle
    assert rt.connection_failing_queried_at is None and rt.store.state.abo_inactive_since == ABO_NOW
    rt.ha_api.send_notification.assert_not_called()


def _posted(rt):
    return [event.kind for event in rt.worker.ereignisse]


def _ohne_uhr(rt):
    rt.uhr_vertrauen = lambda: False


def test_frist_beginnt_nicht_ohne_vertrauenswuerdige_uhr(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store())
    _ohne_uhr(rt)

    abo.enter_inactive(rt, ABO_NOW)

    assert not (tmp_path / "entitlement_state.json").exists()  # nichts persistiert: keine Frist auf falscher Uhr
    assert rt.store.state.abo_inactive_since is None and rt.frist_offen is True
    rt.mqtt_client.stop.assert_not_called()
    rt.ha_api.send_notification.assert_not_called()


def test_laufende_frist_geht_auch_ohne_vertrauenswuerdige_uhr_weiter(make_store, tmp_path):
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=5))
    rt = _runtime(tmp_path, make_store())
    _ohne_uhr(rt)

    abo.enter_inactive(rt, ABO_NOW)  # Neustart mitten in der Frist: der gespeicherte Beginn gilt

    assert rt.store.state.abo_inactive_since == ABO_NOW - timedelta(days=5) and rt.frist_offen is False
    rt.mqtt_client.stop.assert_called_once()


def test_frist_beginnt_nachtraeglich_sobald_die_uhr_vertrauenswuerdig_ist(make_store, tmp_path, monkeypatch):
    monkeypatch.setattr("smartheat_core.wallclock._now", lambda: ABO_NOW)
    rt = _runtime(tmp_path, make_store(), abo_source=lambda: entitlement.INACTIVE)
    _ohne_uhr(rt)
    abo.enter_inactive(rt, ABO_NOW)
    abo.frist_nachholen(rt)
    assert rt.store.state.abo_inactive_since is None  # Uhr weiter unsicher

    rt.uhr_vertrauen = lambda: True
    abo.frist_nachholen(rt)

    assert rt.store.state.abo_inactive_since == ABO_NOW and rt.frist_offen is False
    assert entitlement.load_inactive_since(tmp_path / "entitlement_state.json") == ABO_NOW


def test_frist_nachholen_tut_nichts_ohne_offene_frist_oder_bei_wieder_aktivem_abo(make_store, tmp_path, monkeypatch):
    monkeypatch.setattr("smartheat_core.wallclock._now", lambda: ABO_NOW)
    rt = _runtime(tmp_path, make_store(), abo_source=lambda: entitlement.INACTIVE)
    abo.frist_nachholen(rt)  # keine Frist offen
    assert rt.store.state.abo_inactive_since is None
    rt.frist_offen = True
    rt.config = runtime_config(entitlement_path=tmp_path / "entitlement_state.json",
                               abo_source=lambda: entitlement.ACTIVE)  # Abo inzwischen wieder aktiv
    abo.frist_nachholen(rt)
    assert rt.store.state.abo_inactive_since is None and rt.frist_offen is False


def _abgelaufen(tmp_path, make_store, monkeypatch, vertrauen):
    monkeypatch.setattr("smartheat_core.wallclock._now", lambda: ABO_NOW)
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=31))
    rt = _runtime(tmp_path, make_store(), abo_source=lambda: entitlement.INACTIVE)
    rt.store.update(abo_inactive_since=ABO_NOW - timedelta(days=31))
    rt.uhr_vertrauen = lambda: vertrauen
    return rt


def test_fristende_wird_nur_bei_vertrauenswuerdiger_uhr_beurteilt(make_store, tmp_path, monkeypatch):
    rt = _abgelaufen(tmp_path, make_store, monkeypatch, vertrauen=False)
    abo.check_grace_end(rt)
    assert rt.store.state.abo_finished is False  # eine unsichere Uhr beendet nie: Verlaengerung ist die sichere Richtung
    rt.ha_api.set_number_value.assert_not_called()

    rt.uhr_vertrauen = lambda: True
    abo.check_grace_end(rt)
    assert rt.store.state.abo_finished is True


def test_fristende_laesst_die_betriebsarten_bis_zum_ende_weiterlaufen(make_store, tmp_path, monkeypatch):
    """Kulanzfrist (Notbetrieb): kein Ende der Regelung, die Quelle bekommt den Wunsch erst mit dem Fristende."""
    monkeypatch.setattr("smartheat_core.wallclock._now", lambda: ABO_NOW)
    rt = _runtime(tmp_path, make_store())
    abo.enter_inactive(rt, ABO_NOW)
    assert EV_SOLL_ENDE not in _posted(rt)

    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW)  # Beginn bleibt der alte
    rt.store.update(abo_inactive_since=ABO_NOW - timedelta(days=31))
    abo.check_grace_end(rt)
    assert _posted(rt).count(EV_SOLL_ENDE) == 1


def test_finish_grace_ohne_gelungene_rueckstellung_beendet_die_soll_quelle_nicht(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store(backup={"restore_point": {"curve": 0.4}, "boost_active": True}))
    rt.ha_api.set_number_value.side_effect = RuntimeError("HA nicht erreichbar")

    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is False

    assert EV_SOLL_ENDE not in _posted(rt)


def test_ende_festhalten_wiederholt_das_speichern_bis_es_gelingt(make_store, tmp_path, monkeypatch):
    rt = _abgelaufen(tmp_path, make_store, monkeypatch, vertrauen=True)
    echte = entitlement.mark_beendet
    monkeypatch.setattr(entitlement, "mark_beendet", lambda pfad: (_ for _ in ()).throw(OSError("voll")))
    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is True
    assert rt.store.state.abo_finished is True and not entitlement.ist_beendet(rt.config.entitlement_path)

    assert abo.ende_festhalten(rt) is False  # scheitert weiter: der Aufrufer versucht es im naechsten Takt erneut
    monkeypatch.setattr(entitlement, "mark_beendet", echte)
    assert abo.ende_festhalten(rt) is True
    assert entitlement.ist_beendet(rt.config.entitlement_path)
    assert abo.ende_festhalten(rt) is True  # schon gespeichert: nichts mehr zu tun


# --- kritische Mindestversion: derselbe Notbetrieb wie beim inaktiven Abo, andere Quelle, keine Abo-Texte -----------

def _kritisch(tmp_path, store, abo_status=entitlement.ACTIVE, **optionen):
    return _runtime(tmp_path, store, abo_source=lambda: abo_status, update_noetig_source=lambda: True, **optionen)


def test_unter_kritischer_mindestversion_geht_der_notbetrieb_an_ohne_abo_meldung(make_store, tmp_path):
    rt = _kritisch(tmp_path, make_store())

    abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW
    assert rt.store.state.delivery.notbetrieb is True
    rt.mqtt_client.stop.assert_called_once()  # Snapshots und Telemetrie ruhen
    rt.ha_api.send_notification.assert_not_called()  # die Abo-Meldung waere hier falsch
    rt.ha_api.create_persistent_notification.assert_not_called()
    assert entitlement.ist_kritisch(rt.config.entitlement_path)
    assert entitlement.load_inactive_since(rt.config.entitlement_path) == ABO_NOW


def test_ist_auch_das_abo_inaktiv_gilt_der_abo_anlass_mit_seiner_meldung(make_store, tmp_path):
    rt = _kritisch(tmp_path, make_store(), abo_status=entitlement.INACTIVE)

    abo.enter_inactive(rt, ABO_NOW)

    assert not entitlement.ist_kritisch(rt.config.entitlement_path)
    rt.ha_api.send_notification.assert_called_once_with("notify.mobile_app_handy", HA_TEXTS.text(abo.inactive_meldung(ABO_NOW)))


def test_kritisch_mit_unklarer_uhr_zaehlt_nicht_und_beginnt_nachtraeglich(make_store, tmp_path, monkeypatch):
    monkeypatch.setattr("smartheat_core.wallclock._now", lambda: ABO_NOW)
    rt = _kritisch(tmp_path, make_store())
    _ohne_uhr(rt)

    abo.enter_inactive(rt, ABO_NOW)
    abo.frist_nachholen(rt)

    assert not rt.config.entitlement_path.exists()  # kein Zaehler auf falscher Uhr
    assert rt.store.state.abo_inactive_since is None and rt.frist_offen is True
    rt.mqtt_client.stop.assert_not_called()

    rt.uhr_vertrauen = lambda: True
    abo.frist_nachholen(rt)

    assert rt.store.state.abo_inactive_since == ABO_NOW and rt.frist_offen is False
    assert entitlement.ist_kritisch(rt.config.entitlement_path)


def test_kritisch_neustart_mitten_in_der_frist_behaelt_den_gespeicherten_beginn(make_store, tmp_path):
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=12), kritisch=True)
    rt = _kritisch(tmp_path, make_store())
    _ohne_uhr(rt)  # nach dem Neustart ist die Uhr noch unklar

    abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW - timedelta(days=12)  # der Zaehler laeuft weiter
    assert rt.store.state.delivery.notbetrieb is True and rt.frist_offen is False
    rt.ha_api.send_notification.assert_not_called()


def _kritisch_abgelaufen(tmp_path, make_store, monkeypatch, tage):
    monkeypatch.setattr("smartheat_core.wallclock._now", lambda: ABO_NOW)
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=tage), kritisch=True)
    rt = _kritisch(tmp_path, make_store(backup={"restore_point": {"curve": 0.4, "room_setpoint": 1.0}}))
    rt.store.update(abo_inactive_since=ABO_NOW - timedelta(days=tage))
    return rt


def test_kritisch_nach_tag_31_stellt_die_ursprungswerte_wieder_her_und_beendet_ohne_abo_meldung(
        make_store, tmp_path, monkeypatch):
    rt = _kritisch_abgelaufen(tmp_path, make_store, monkeypatch, tage=31)

    abo.check_grace_end(rt)

    assert rt.store.state.abo_finished is True and entitlement.ist_beendet(rt.config.entitlement_path)
    assert EV_SOLL_ENDE in _posted(rt)
    assert rt.ha_api.set_number_value.call_count > 0  # dieselbe Rueckstellung wie beim Abo-Ende
    assert rt.idle is True
    rt.ha_api.send_notification.assert_not_called()  # weder "Abo beendet" noch eine Abo-Meldung


def test_kritisch_vor_tag_30_beendet_nichts(make_store, tmp_path, monkeypatch):
    rt = _kritisch_abgelaufen(tmp_path, make_store, monkeypatch, tage=29)

    abo.check_grace_end(rt)

    assert rt.store.state.abo_finished is False and not entitlement.ist_beendet(rt.config.entitlement_path)
    rt.ha_api.set_number_value.assert_not_called()


def test_kritisch_nach_tag_31_mit_unklarer_uhr_beendet_nichts(make_store, tmp_path, monkeypatch):
    rt = _kritisch_abgelaufen(tmp_path, make_store, monkeypatch, tage=31)
    _ohne_uhr(rt)

    abo.check_grace_end(rt)

    assert rt.store.state.abo_finished is False
    rt.ha_api.set_number_value.assert_not_called()


def test_nach_dem_update_ist_ein_aktives_abo_nach_der_frist_wieder_normalbetrieb(make_store, tmp_path, monkeypatch):
    rt = _kritisch_abgelaufen(tmp_path, make_store, monkeypatch, tage=31)
    rt.config = runtime_config(entitlement_path=tmp_path / "entitlement_state.json",
                               abo_source=lambda: entitlement.ACTIVE, update_noetig_source=lambda: False)
    rt.neustart = MagicMock(spec=lambda: None)

    abo.check_grace_end(rt)

    rt.neustart.assert_called_once()  # die Quelle ist nicht mehr kritisch: kein Ende, Neustart im Normalbetrieb
    assert rt.store.state.abo_finished is False


def test_ein_schon_festgehaltenes_ende_setzt_den_zustand_ohne_offenen_soll_merker(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store())
    entitlement.mark_beendet(rt.config.entitlement_path)

    assert abo.finish_grace(rt, always_restore=True, final_notice=False) is True

    assert rt.store.state.abo_finished is True
    assert rt.store.state.soll_ende_offen is False
    assert _posted(rt).count(EV_SOLL_ENDE) == 1


def test_die_gescheiterte_wiederherstellung_wird_kritisch_gemeldet():
    notifier = MagicMock()

    abo.report_restore(notifier, ok=False)

    notifier.notify.assert_called_once()
    assert notifier.notify.call_args.kwargs["critical"] is True


def test_fristende_tut_nichts_wenn_das_ende_schon_festgehalten_ist(make_store, tmp_path, monkeypatch):
    rt = _abgelaufen(tmp_path, make_store, monkeypatch, vertrauen=True)
    entitlement.mark_beendet(rt.config.entitlement_path)  # Datei steht, der Zustand noch nicht

    abo.check_grace_end(rt)

    assert rt.store.state.abo_finished is False
    assert _posted(rt) == []
    rt.ha_api.set_number_value.assert_not_called()
    rt.ha_api.send_notification.assert_not_called()
