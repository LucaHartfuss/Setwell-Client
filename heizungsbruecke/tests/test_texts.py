"""Kundentexte vom Host: die HA-Standardwerte sind die bisherigen Texte (wortgleich)."""
from dataclasses import fields

from smartheat_core import meldungen
from smartheat_runtime.texts import HA_TEXTS, HostTexts


def test_ha_defaults_are_the_previous_texts():
    assert HA_TEXTS.delivery_prefix == "Heizungsbrücke"
    assert HA_TEXTS.source_unavailable_log == "Home Assistant nicht erreichbar"


def test_every_text_has_a_default():
    assert all(isinstance(getattr(HA_TEXTS, field.name), str) for field in fields(HostTexts))



def test_a_host_renders_a_meldung_with_its_own_prefix_and_carrier():
    gateway = HostTexts(delivery_prefix="SmartHeat-Gateway", traeger="des Gateways")
    notbetrieb = meldungen.neu("meldung.notbetrieb")
    voll = meldungen.neu("meldung.datentraeger_nicht_beschreibbar")

    assert gateway.text(notbetrieb).startswith("SmartHeat-Gateway: Server antwortet nicht")
    assert "Datenträger des Gateways ist nicht beschreibbar" in gateway.text(voll)
    assert "Datenträger des Home-Assistant-Systems ist nicht" in HA_TEXTS.text(voll)
    assert HA_TEXTS.text(notbetrieb).startswith("Heizungsbrücke: Server antwortet nicht")
