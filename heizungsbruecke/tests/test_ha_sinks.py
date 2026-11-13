"""HA-Sinks fuer Status und Meldungen."""
import logging

from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FailingServiceHa, FakeHa, logzeilen

from heizungsbruecke.ha_sinks import GeraetUndHaSink, HaNotifySink, notification_id
from smartheat_core import meldungen
from smartheat_device.host import MeldungSink

ABO = meldungen.neu("meldung.abo_beendet")
TEXT = meldungen.text(ABO)


def test_push_goes_to_every_service_even_if_one_fails():
    ha = FailingServiceHa()
    HaNotifySink(ha, ["notify.kaputt", "notify.handy"]).push("abo", ABO)
    assert ha.pushes == [TEXT]


def test_show_and_withdraw_use_the_stable_notification_id():
    ha = FakeHa()
    sink = HaNotifySink(ha, [])
    sink.show("raumfuehler:sensor.x", ABO)
    sink.withdraw("raumfuehler:sensor.x")
    assert ha.persistent == [("smartheat_raumfuehler_sensor_x", TEXT)]
    assert ha.dismissed == ["smartheat_raumfuehler_sensor_x"]
    assert notification_id("Abo") == "smartheat_abo"


def test_a_failing_push_logs_a_key_with_foreign_text_only_escaped(caplog):
    """Der Schluessel `fehler:<Text>` traegt Text des Servers; scheitert ein Notify-Dienst, steht er in
    der Warnung nur als Escape."""
    with caplog.at_level(logging.WARNING, logger="heizungsbruecke.ha_sinks"):
        HaNotifySink(FailingServiceHa(), ["notify.kaputt"]).push(f"fehler:{FREMDTEXT}", ABO)
    assert logzeilen(caplog) == [
        f"Push-Benachrichtigung 'fehler:{FREMDTEXT_IM_LOG}' an notify.kaputt konnte nicht gesendet werden"]


def test_the_combined_sink_gives_the_meldung_to_both_sides():
    ha, gemeldet = FakeHa(), []
    sink = GeraetUndHaSink(MeldungSink(lambda *args, **kwargs: gemeldet.append((args, kwargs))), HaNotifySink(ha, []))

    sink.show("abo", ABO)

    assert ha.persistent == [("smartheat_abo", TEXT)]
    assert gemeldet == [(("abo", TEXT), {"offen": True, "stufe": "rot"})]
