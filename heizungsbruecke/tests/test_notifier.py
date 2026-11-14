"""Meldungen mit Zustandsentprellung (notifier.py)."""
import logging
from unittest.mock import MagicMock

import pytest
from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FailingServiceHa, logzeilen

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_sinks import HaNotifySink, notification_id
from smartheat_core import meldungen, pipeline
from smartheat_core.config_check import HA_HINWEISE
from smartheat_core.meldungen import neu
from smartheat_runtime.notifier import STATE_OK, Notifier, category
from smartheat_runtime.state import StateStore

SERVICES = ["notify.mobile_app_a", "notify.mobile_app_b"]

NOTBETRIEB = neu("meldung.notbetrieb")
NOTBETRIEB_OK = neu("meldung.notbetrieb_ok")
ABO = neu("meldung.abo_beendet")
DATENFEHLER = neu("meldung.datenfehler_lokal", rollen="dat")
BATTERIE = neu("meldung.batterie_schwach", bezeichnung="Bad")
BATTERIE_OK = neu("meldung.batterie_ok", bezeichnung="Bad")
KONFIGURATION = neu("meldung.konfiguration_fehler", grund="x")
T_NOTBETRIEB = meldungen.text(NOTBETRIEB)
T_ABO = meldungen.text(ABO)


@pytest.fixture
def ha_api():
    return MagicMock(spec=HomeAssistantApi)


def _notifier(store, ha_api, services=SERVICES):
    return Notifier(store, HaNotifySink(ha_api, services))


def test_notification_id_is_a_slug():
    assert notification_id("raumfuehler:sensor.wz-1") == "smartheat_raumfuehler_sensor_wz_1"
    assert notification_id("notbetrieb") == "smartheat_notbetrieb"


def test_a_failing_notify_service_does_not_stop_the_persistent_notification_or_the_state(make_store):
    store = make_store()
    ha = FailingServiceHa()
    notifier = Notifier(store, HaNotifySink(ha, ["notify.kaputt", "notify.handy"]))
    assert notifier.notify("abo", "inaktiv", ABO, critical=True)
    assert ha.pushes == [T_ABO]
    assert ha.persistent == [("smartheat_abo", T_ABO)]
    assert store.state.notify_states == {"abo": "inaktiv"}


def test_unknown_previous_state_counts_as_ok(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    assert notifier.state("notbetrieb") == STATE_OK
    assert notifier.notify("notbetrieb", STATE_OK, NOTBETRIEB_OK, critical=True) is False
    ha_api.send_notification.assert_not_called()
    ha_api.dismiss_persistent_notification.assert_not_called()


def test_critical_state_change_pushes_every_service_and_creates_persistent(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    assert notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True) is True

    assert [c.args for c in ha_api.send_notification.call_args_list] == [
        ("notify.mobile_app_a", T_NOTBETRIEB), ("notify.mobile_app_b", T_NOTBETRIEB),
    ]
    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", T_NOTBETRIEB, "smartheat_notbetrieb",
    )


def test_same_state_is_silent(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True)
    ha_api.reset_mock()

    assert notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True) is False

    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_not_called()


def test_notify_is_silent_for_same_state_after_restart(make_store, ha_api, tmp_path):
    _notifier(make_store(), ha_api).notify("konfiguration", "fehler:x", KONFIGURATION, critical=True)
    ha_api.reset_mock()
    restarted = _notifier(StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json"), ha_api)

    assert restarted.notify("konfiguration", "fehler:x", KONFIGURATION, critical=True) is False
    ha_api.send_notification.assert_not_called()


def test_back_to_ok_pushes_dismisses_and_forgets_the_key(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)
    notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True)
    ha_api.reset_mock()

    assert notifier.notify("notbetrieb", STATE_OK, NOTBETRIEB_OK, critical=True) is True

    assert ha_api.send_notification.call_count == 2
    ha_api.dismiss_persistent_notification.assert_called_once_with("smartheat_notbetrieb")
    assert "notbetrieb" not in store.state.notify_states


def test_non_critical_never_touches_persistent_notifications(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    notifier.notify("batterie:sensor.x", "niedrig", BATTERIE, critical=False)
    notifier.notify("batterie:sensor.x", STATE_OK, BATTERIE_OK, critical=False)

    ha_api.create_persistent_notification.assert_not_called()
    ha_api.dismiss_persistent_notification.assert_not_called()
    assert ha_api.send_notification.call_count == 4


def test_failing_service_does_not_block_the_others(make_store, ha_api, caplog):
    ha_api.send_notification.side_effect = [RuntimeError("weg"), None]
    notifier = _notifier(make_store(), ha_api)

    with caplog.at_level(logging.WARNING):
        notifier.notify("abo", "inaktiv", ABO, critical=True)

    assert ha_api.send_notification.call_count == 2
    ha_api.create_persistent_notification.assert_called_once()
    assert "notify.mobile_app_a" in caplog.text


def test_failing_persistent_notification_is_only_logged(make_store, ha_api, caplog):
    ha_api.create_persistent_notification.side_effect = RuntimeError("HA weg")
    notifier = _notifier(make_store(), ha_api)

    with caplog.at_level(logging.WARNING):
        assert notifier.notify("abo", "inaktiv", ABO, critical=True) is True

    assert "abo" in caplog.text


class _RaisingPushSink:
    """NotifySink-Fake, dessen Push scheitert (ein Host-Sink ausser HA darf den Notifier nicht abbrechen)."""

    def __init__(self) -> None:
        self.shown: list = []

    def push(self, key: str, meldung, *, begleitet: bool = False) -> None:
        raise RuntimeError("Push kaputt")

    def show(self, key: str, meldung) -> None:
        self.shown.append((key, meldung))

    def withdraw(self, key: str) -> None:
        pass


def test_failing_sink_push_does_not_stop_the_persistent_notification_or_the_state(make_store, caplog):
    store = make_store()
    sink = _RaisingPushSink()
    notifier = Notifier(store, sink)

    with caplog.at_level(logging.WARNING):
        assert notifier.notify("abo", "inaktiv", ABO, critical=True) is True

    assert sink.shown == [("abo", ABO)]
    assert store.state.notify_states == {"abo": "inaktiv"}
    assert store.state.notify_meldungen == {"abo": meldungen.zu_dict(ABO)}
    assert "Push-Benachrichtigung 'abo' konnte nicht gesendet werden" in caplog.text


def test_state_write_failure_still_notifies(make_store, ha_api, monkeypatch):
    store = make_store()
    notifier = _notifier(store, ha_api)

    def _broken(*args, **kwargs):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _broken)
    assert notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True) is True

    assert store.state.notify_states == {"notbetrieb": "aktiv"}
    assert ha_api.send_notification.call_count == 2


def test_seed_sets_a_missing_state_silently_so_the_recovery_is_reported(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    notifier.seed("notbetrieb", "aktiv")
    ha_api.send_notification.assert_not_called()

    assert notifier.notify("notbetrieb", STATE_OK, NOTBETRIEB_OK, critical=True) is True


def test_seed_never_overrides_a_known_state(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("datenfehler", "lokal:dat", DATENFEHLER, critical=True)

    notifier.seed("datenfehler", "anlage")

    assert notifier.state("datenfehler") == "lokal:dat"


def test_without_services_only_persistent_notification_is_created(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api, services=[])

    notifier.notify("konfiguration", "fehler:veraltet", KONFIGURATION, critical=True)

    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_called_once()


def test_critical_message_is_kept_until_ok(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)

    notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True)
    notifier.notify("batterie:sensor.x", "niedrig", BATTERIE, critical=False)
    assert store.state.notify_meldungen == {"notbetrieb": meldungen.zu_dict(NOTBETRIEB)}

    notifier.notify("notbetrieb", STATE_OK, NOTBETRIEB_OK, critical=True)
    assert store.state.notify_meldungen == {}


def test_republish_persistent_recreates_open_critical_notifications_without_push(make_store, ha_api, tmp_path):
    """Review I3: HA haelt persistent_notifications nur im Speicher. Nach einem HA-Neustart legt
    das Add-on sie fuer jede noch offene kritische Stoerung neu an, auch nach eigenem Neustart."""
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True)
    notifier.notify("datenfehler", "lokal:dat", DATENFEHLER, critical=True)
    notifier.notify("datenfehler", STATE_OK, neu("meldung.datenfehler_ok"), critical=True)
    notifier.notify("batterie:sensor.x", "niedrig", BATTERIE, critical=False)
    ha_api.reset_mock()
    restarted = _notifier(StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json"), ha_api)

    restarted.republish_persistent()

    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", T_NOTBETRIEB, "smartheat_notbetrieb",
    )
    ha_api.send_notification.assert_not_called()
    ha_api.dismiss_persistent_notification.assert_not_called()


def _offen_ohne_ablage(make_store, ablagen):
    """Ein Speicher mit einem offenen kritischen Zustand, dessen Ablage fehlt oder ungueltig ist (Bestand vor der
    Umstellung auf Codes, beschaedigter Eintrag)."""
    return make_store(backup={"notify_states": {"datenfehler": "lokal:dat"}, "notify_meldungen": ablagen})


@pytest.mark.parametrize("ablagen", [{}, {"datenfehler": {"code": "gibt.es.nicht", "params": {}}}, {"datenfehler": "text"}])
def test_ein_offener_kritischer_zustand_ohne_gueltige_ablage_wird_bei_der_naechsten_pruefung_neu_gemeldet(
        make_store, ha_api, ablagen):
    """Ohne Ablage ueberspringt republish_persistent den Schluessel, und der Server sieht ihn nie: die naechste
    Pruefung, die denselben Zustand bestaetigt, meldet ihn mit Push und offener Meldung und legt die Ablage an."""
    store = _offen_ohne_ablage(make_store, ablagen)
    notifier = _notifier(store, ha_api)

    assert notifier.notify("datenfehler", "lokal:dat", DATENFEHLER, critical=True) is True

    ha_api.send_notification.assert_called()
    ha_api.create_persistent_notification.assert_called_once()
    assert store.state.notify_meldungen == {"datenfehler": meldungen.zu_dict(DATENFEHLER)}
    ha_api.reset_mock()
    assert notifier.notify("datenfehler", "lokal:dat", DATENFEHLER, critical=True) is False
    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_not_called()


def test_ein_offener_kritischer_zustand_mit_gueltiger_ablage_wird_nicht_doppelt_gemeldet(make_store, ha_api):
    store = _offen_ohne_ablage(make_store, {"datenfehler": meldungen.zu_dict(DATENFEHLER)})
    notifier = _notifier(store, ha_api)

    assert notifier.notify("datenfehler", "lokal:dat", DATENFEHLER, critical=True) is False

    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_not_called()


def test_ein_nicht_kritischer_zustand_hat_nie_eine_ablage_und_wird_nicht_neu_gemeldet(make_store, ha_api):
    store = make_store(backup={"notify_states": {"batterie:sensor.x": "niedrig"}})
    notifier = _notifier(store, ha_api)

    assert notifier.notify("batterie:sensor.x", "niedrig", BATTERIE, critical=False) is False

    ha_api.send_notification.assert_not_called()


def test_republish_persistent_skips_seeded_state_without_message(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)
    notifier.seed("notbetrieb", "aktiv")

    notifier.republish_persistent()

    ha_api.create_persistent_notification.assert_not_called()


def test_republish_persistent_failure_is_only_logged(make_store, ha_api, caplog):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True)
    notifier.notify("abo", "inaktiv", ABO, critical=True)
    ha_api.create_persistent_notification.side_effect = RuntimeError("HA weg")
    ha_api.create_persistent_notification.reset_mock()

    with caplog.at_level(logging.WARNING):
        notifier.republish_persistent()  # darf nicht werfen

    assert ha_api.create_persistent_notification.call_count == 2
    assert "notbetrieb" in caplog.text


def test_critical_event_while_ha_unreachable_is_restored_by_republish(make_store, ha_api):
    """Push und Benachrichtigung scheitern (HA weg): der Push wird nicht wiederholt, die
    Benachrichtigung kommt beim Wiederverbinden (republish_persistent) doch noch an."""
    ha_api.send_notification.side_effect = RuntimeError("HA weg")
    ha_api.create_persistent_notification.side_effect = RuntimeError("HA weg")
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True)
    ha_api.reset_mock()
    ha_api.send_notification.side_effect = None
    ha_api.create_persistent_notification.side_effect = None

    notifier.republish_persistent()

    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", T_NOTBETRIEB, "smartheat_notbetrieb",
    )
    ha_api.send_notification.assert_not_called()


def test_refresh_persistent_reasserts_an_unchanged_critical_state_without_push(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)
    notifier.notify("konfiguration", "fehler:hilfs_entities", KONFIGURATION, critical=True)
    ha_api.reset_mock()

    neuer = neu("meldung.konfiguration_fehler", grund="y")
    notifier.refresh_persistent("konfiguration", neuer)

    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", meldungen.text(neuer), "smartheat_konfiguration",
    )
    ha_api.send_notification.assert_not_called()
    assert store.state.notify_meldungen == {"konfiguration": meldungen.zu_dict(neuer)}


def test_refresh_persistent_does_nothing_for_an_ok_key(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)

    notifier.refresh_persistent("konfiguration", KONFIGURATION)

    ha_api.create_persistent_notification.assert_not_called()
    assert store.state.notify_meldungen == {}


def test_category_is_the_key_prefix():
    assert category("raumfuehler:sensor.wz") == "raumfuehler"
    assert category("quellwechsel") == "quellwechsel"


def test_switched_off_hint_category_is_tracked_and_logged_but_not_pushed(make_store, ha_api, caplog):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=["batterie"])

    with caplog.at_level(logging.WARNING):
        assert notifier.notify("batterie:sensor.x", "niedrig", BATTERIE, critical=False) is True

    ha_api.send_notification.assert_not_called()
    assert notifier.state("batterie:sensor.x") == "niedrig"
    assert "meldung.batterie_schwach" in caplog.text
    assert "Bad" not in caplog.text


def test_write_budget_hints_are_switchable():
    assert HA_HINWEISE[-2:] == ("schreibbudget", "schreibzaehler")
    assert category(pipeline.BUDGET_KEY) == "schreibbudget"
    assert category(pipeline.LIFETIME_KEY) == "schreibzaehler"


def test_switched_off_write_budget_hint_is_tracked_but_not_pushed(make_store, ha_api):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=["schreibbudget"])

    assert notifier.notify(pipeline.BUDGET_KEY, "erreicht", neu("meldung.schreibbudget", limit=50), critical=False) is True
    notifier.notify(pipeline.LIFETIME_KEY, "10000", neu("meldung.schreibzaehler", anzahl=10000), critical=False)

    assert notifier.state(pipeline.BUDGET_KEY) == "erreicht"
    assert ha_api.send_notification.call_count == len(SERVICES)  # nur der Zaehler-Hinweis


def test_other_hint_categories_are_still_pushed(make_store, ha_api):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=["batterie"])

    notifier.notify("raumfuehler:sensor.a", "ausgefallen", neu("meldung.raumfuehler_keiner", fuehler="Bad"), critical=False)

    assert ha_api.send_notification.call_count == len(SERVICES)


def test_critical_messages_ignore_the_hint_switches(make_store, ha_api):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=list(HA_HINWEISE))

    notifier.notify("batterie:sensor.x", "niedrig", BATTERIE, critical=True)

    assert ha_api.send_notification.call_count == len(SERVICES)


def test_silent_ok_logs_the_return_without_push(make_store, ha_api, caplog):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("manueller_eingriff", "1.3/24.5", neu("meldung.manueller_eingriff", was="Vorlauf"), critical=False)
    ha_api.reset_mock()

    with caplog.at_level(logging.INFO):
        assert notifier.notify("manueller_eingriff", STATE_OK, neu("meldung.manueller_eingriff_ok"), critical=False, silent_ok=True) is True

    ha_api.send_notification.assert_not_called()
    assert notifier.state("manueller_eingriff") == STATE_OK
    assert "meldung.manueller_eingriff_ok" in caplog.text


def test_clear_all_dismisses_open_notifications_and_forgets_states_without_push(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)
    notifier.notify("notbetrieb", "aktiv", NOTBETRIEB, critical=True)
    notifier.notify("batterie:sensor.x", "niedrig", BATTERIE, critical=False)
    ha_api.reset_mock()

    notifier.clear_all()

    dismissed = {c.args[0] for c in ha_api.dismiss_persistent_notification.call_args_list}
    assert dismissed == {"smartheat_notbetrieb", "smartheat_batterie_sensor_x"}
    ha_api.send_notification.assert_not_called()
    assert (store.state.notify_states, store.state.notify_meldungen) == ({}, {})


@pytest.mark.parametrize(("state", "stufe"), [("inaktiv", logging.WARNING), (STATE_OK, logging.INFO)],
                         ids=["warnung", "entwarnung"])
def test_the_log_line_carries_only_the_code(make_store, ha_api, caplog, state, stufe):
    """Das Log nennt weder Schluessel noch Parameter noch Text einer Meldung, nur ihren Code."""
    notifier = _notifier(make_store(), ha_api)
    if state == STATE_OK:
        notifier.seed("server:geheim", "inaktiv")
    meldung = neu("meldung.datenfehler_server", grund="Raum Kinderzimmer \ud800\n")

    with caplog.at_level(logging.INFO, logger="smartheat_runtime.notifier"):
        assert notifier.notify("server:geheim", state, meldung, critical=False) is True

    (zeile,) = [r for r in caplog.records if r.levelno == stufe]
    assert zeile.getMessage() == "Meldung meldung.datenfehler_server"
    assert [c.args for c in ha_api.send_notification.call_args_list] == [
        (service, meldungen.text(meldung)) for service in SERVICES]


class _ScheiternderSink:
    """NotifySink-Fake, dessen Push bzw. Anzeige scheitert (Fehlerpfade des Notifiers)."""

    def __init__(self, *, push_fehler: bool = False, show_fehler: bool = False) -> None:
        self._push_fehler, self._show_fehler = push_fehler, show_fehler

    def push(self, key: str, meldung, *, begleitet: bool = False) -> None:
        if self._push_fehler:
            raise RuntimeError("Push kaputt")

    def show(self, key: str, meldung) -> None:
        if self._show_fehler:
            raise RuntimeError("HA weg")

    def withdraw(self, key: str) -> None:
        pass


FREMDER_SCHLUESSEL = f"fehler:{FREMDTEXT}"  # HaHost.load: StartFailure(str(ConfigError)) traegt Text des Servers
FREMDER_SCHLUESSEL_IM_LOG = f"fehler:{FREMDTEXT_IM_LOG}"


def _kaputter_speicher(monkeypatch):
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


def _seed_ohne_speicher(store, monkeypatch):
    _kaputter_speicher(monkeypatch)
    Notifier(store, _ScheiternderSink()).seed(FREMDER_SCHLUESSEL, "aktiv")
    return "Meldezustand '{}' konnte nicht gespeichert werden"


def _notify_ohne_speicher(store, monkeypatch):
    _kaputter_speicher(monkeypatch)
    Notifier(store, _ScheiternderSink()).notify(FREMDER_SCHLUESSEL, "aktiv", ABO, critical=True)
    return "Meldezustand '{}' konnte nicht gespeichert werden, Meldung geht trotzdem raus"


def _push_scheitert(store, monkeypatch):
    Notifier(store, _ScheiternderSink(push_fehler=True)).notify(FREMDER_SCHLUESSEL, "aktiv", ABO, critical=False)
    return "Push-Benachrichtigung '{}' konnte nicht gesendet werden"


def _meldetext_ohne_speicher(store, monkeypatch):
    notifier = Notifier(store, _ScheiternderSink())
    notifier.notify(FREMDER_SCHLUESSEL, "aktiv", ABO, critical=True)
    _kaputter_speicher(monkeypatch)
    notifier.refresh_persistent(FREMDER_SCHLUESSEL, KONFIGURATION)
    return "Meldung '{}' konnte nicht gespeichert werden"


def _anzeige_scheitert(store, monkeypatch):
    Notifier(store, _ScheiternderSink(show_fehler=True)).notify(FREMDER_SCHLUESSEL, "aktiv", ABO, critical=True)
    return "Offene Meldung '{}' konnte nicht aktualisiert werden"


@pytest.mark.parametrize("pfad", [_seed_ohne_speicher, _notify_ohne_speicher, _push_scheitert,
                                  _meldetext_ohne_speicher, _anzeige_scheitert],
                         ids=["seed", "notify_speicher", "push", "refresh_speicher", "anzeige"])
def test_ein_meldeschluessel_mit_fremdem_text_steht_in_den_fehlerpfaden_nur_escaped_im_log(
    make_store, monkeypatch, caplog, pfad,
):
    """Der Schluessel `fehler:<Text>` eines Startfehlers traegt Text des Servers (Konfigurationsdokument).
    Scheitern Speichern, Push oder Anzeige, steht er in der Warnung nur als Escape (Zeilenumbruch, U+202E, U+061C)."""
    store = make_store()
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.notifier"):
        vorlage = pfad(store, monkeypatch)
    assert vorlage.format(FREMDER_SCHLUESSEL_IM_LOG) in logzeilen(caplog)
