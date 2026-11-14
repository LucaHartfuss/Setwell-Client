"""HA-Sinks fuer Status und Meldungen."""
import logging
import re

from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FailingServiceHa, FakeHa, logzeilen

from heizungsbruecke.ha_sinks import EREIGNIS_MELDUNG, MELDUNG_SCHEMA, GeraetUndHaSink, HaNotifySink, notification_id
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


def _kombiniert(ha, geraet_id="geraet-1", gemeldet=None):
    gemeldet = [] if gemeldet is None else gemeldet
    return GeraetUndHaSink(MeldungSink(lambda *args, **kwargs: gemeldet.append((args, kwargs))), HaNotifySink(ha, []),
                           lambda: geraet_id)


def _ereignisse(ha):
    return [daten for art, daten in ha.events if art == EREIGNIS_MELDUNG]


def test_the_combined_sink_gives_the_meldung_to_both_sides():
    ha, gemeldet = FakeHa(), []
    sink = _kombiniert(ha, gemeldet=gemeldet)

    sink.show("abo", ABO)

    assert ha.persistent == [("smartheat_abo", TEXT)]
    assert gemeldet == [(("abo", ABO), {"offen": True})]


def test_jeder_uebergang_feuert_ein_ereignis_mit_code_params_stufe_und_offen():
    ha = FakeHa()
    sink = _kombiniert(ha)

    sink.show("abo", ABO)
    sink.withdraw("abo")
    sink.push("abo", ABO)

    stufe = ABO.stufe
    assert _ereignisse(ha) == [
        {"schema": MELDUNG_SCHEMA, "geraet_id": "geraet-1", "key": "abo", "code": ABO.code, "params": {}, "stufe": stufe,
         "offen": offen} for offen in (True, False, False)]
    assert MELDUNG_SCHEMA == 1


def test_ein_push_eines_offenen_keys_ist_offen_und_die_entwarnung_traegt_den_code_der_offenen_meldung():
    ha = FakeHa()
    sink = _kombiniert(ha)
    rot = meldungen.neu("meldung.geraet_doppelt")

    sink.show("doppelt", rot)
    sink.push("doppelt", rot)
    sink.withdraw("doppelt")

    assert [(e["code"], e["stufe"], e["offen"]) for e in _ereignisse(ha)] == [
        (rot.code, "rot", True), (rot.code, "rot", True), (rot.code, "rot", False)]


def test_eine_entwarnung_ohne_offene_meldung_feuert_kein_ereignis():
    ha = FakeHa()
    _kombiniert(ha).withdraw("unbekannt")
    assert _ereignisse(ha) == []


def test_der_kreis_kommt_aus_dem_praefix_des_keys_und_namen_bleiben_draussen():
    """Ein Key mit Entity-Namen geht als `<kategorie>:<Pruefsumme>`, der Kreis als Feld; lokale Parameter (Namen) fehlen."""
    ha = FakeHa()
    stoerung = meldungen.neu("erzeuger.stoerung", roh_text="f_28", quelle="mypyllant")
    _kombiniert(ha).show("kreis-2:raumfuehler:sensor.wohnzimmer", stoerung)

    (ereignis,) = _ereignisse(ha)
    assert ereignis["kreis"] == 2 and re.fullmatch(r"raumfuehler:[0-9a-f]{10}", ereignis["key"])
    assert "wohnzimmer" not in str(ereignis) and "quelle" not in ereignis["params"]
    assert ereignis["params"] == {"roh_text": "f_28"}
    ha2 = FakeHa()
    _kombiniert(ha2).show("abo", ABO)
    assert "kreis" not in _ereignisse(ha2)[0]


def test_ohne_geraete_id_feuert_kein_ereignis_und_ein_fehler_beim_feuern_stoert_die_meldung_nicht(caplog):
    ha = FakeHa()
    _kombiniert(ha, geraet_id=None).show("abo", ABO)
    assert _ereignisse(ha) == [] and ha.persistent == [("smartheat_abo", TEXT)]

    class KaputtHa(FakeHa):
        def fire_event(self, event_type, data):
            raise RuntimeError("HA weg")

    kaputt = KaputtHa()
    with caplog.at_level(logging.WARNING, logger="heizungsbruecke.ha_sinks"):
        _kombiniert(kaputt).show(f"fehler:{FREMDTEXT}", ABO)
    assert kaputt.persistent and logzeilen(caplog) == ["Meldungs-Ereignis konnte nicht gefeuert werden: RuntimeError"]
