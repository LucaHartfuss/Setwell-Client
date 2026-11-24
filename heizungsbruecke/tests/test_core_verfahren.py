"""Lokaler Verfahrensteil je Weg und Hebelsatz."""
import pytest

from heizungsbruecke import dokument
from smartheat_core import config_check
from smartheat_core.levers import LEVER_SETS
from smartheat_core.safety import LOCAL_SAFETY
from smartheat_core.verfahren import VERFAHREN, WEG_UNBEKANNT, WegUnbekannt, verfahren_fuer

#: Die Zeilen der Tabelle, woertlich: heute gibt es fuenf Wege; die Simulation traegt jeden Hebelsatz.
ZEILEN = {
    ("vaillant_myvaillant", "vaillant_vrc720"), ("vaillant_ebus", "vaillant_vrc720"),
    ("weishaupt_modbus", "weishaupt_wwp"), ("weishaupt_modbus", "weishaupt_wwp_basis"),
    ("viessmann_vicare", "viessmann_vicare"),
    ("simulation", "vaillant_vrc720"), ("simulation", "weishaupt_wwp"), ("simulation", "weishaupt_wwp_basis"),
    ("simulation", "viessmann_vicare"),
}


def test_tabelle_hat_genau_die_heutigen_zeilen():
    assert set(VERFAHREN) == ZEILEN
    assert all(v.lever_set is LEVER_SETS[hebelsatz] for (_, hebelsatz), v in VERFAHREN.items())


def test_ein_hebelsatz_hat_auf_jedem_weg_dieselbe_zeile():
    for (_, hebelsatz), zeile in VERFAHREN.items():
        assert zeile is VERFAHREN[next(k for k in VERFAHREN if k[1] == hebelsatz)]


def test_jeder_hebelsatz_hat_eine_zeile():
    assert {hebelsatz for _, hebelsatz in VERFAHREN} == set(LEVER_SETS)


def test_wege_der_tabelle_sind_die_wege_der_konfigurationspruefung():
    assert {weg for weg, _ in VERFAHREN} == set(config_check.WEGE)


def test_der_busweg_traegt_den_vaillant_hebelsatz_mit_der_zeile_des_cloud_wegs():
    assert verfahren_fuer("vaillant_ebus", "vaillant_vrc720") is verfahren_fuer("vaillant_myvaillant", "vaillant_vrc720")
    for hebelsatz in ("weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare", "erfunden"):
        assert verfahren_fuer("vaillant_ebus", hebelsatz) is None


def test_ein_mqtt_dokument_hat_die_schreibplattform_aber_ohne_geraet_kein_schreibziel():
    """Der Busweg hat die Zeile in SCHREIB_PLATTFORM; ohne die Entities am verankerten Registry-Geraet gibt es dennoch
    kein Schreibziel (die Verankerung steht in den Schreibregeln des Buswegs)."""
    from geraet_helpers import ha_konfiguration
    from rastung_beispiele import fuer

    neu = ha_konfiguration(bindung={"kreis": {"plattform": "mqtt", "anlage": "S", "nummer": "0", "geraet": "g1"}})
    assert neu["weg"] == "vaillant_ebus"
    assert config_check.pruefen(neu, fuer(None, neu), host=config_check.HOST_HA, treiber_pruefen=lambda k: None) is None
    assert dokument.SCHREIB_PLATTFORM[("vaillant_ebus", "vaillant_vrc720")] == "mqtt"
    b = dokument.bindung(neu)
    assert dokument.schreib_plattform(b) == "mqtt"
    assert dokument.schreibziele_befund(b, {}, beim_start=False, geraete={}) is not None
    # Gegenprobe: mit dem verankerten Geraet und den Entities daran ergibt sich ein Schreibziel
    ziele = {"curve": "number.ebusd_720_hc1heatcurve", "room_setpoint": "number.ebusd_720_z1manualtemp"}
    verankert = ha_konfiguration(bindung={
        "kreis": {"plattform": "mqtt", "anlage": "S", "nummer": "0", "geraet": "g1"},
        "rollen": {"room_target": "climate.wohnzimmer_thermostat::temperature", **ziele}})
    registry = {ref: {"entity_id": ref, "platform": "mqtt", "config_entry_id": "e-mqtt", "device_id": "g1"}
                for ref in ziele.values()}
    geraete = {"g1": {"id": "g1", "manufacturer": "Vaillant", "model": "VRC 720"}}
    assert dokument.schreibziele_befund(dokument.bindung(verankert), registry, beim_start=False,
                                        geraete=geraete) is None


def test_jede_schreibplattform_fuehrt_zu_einer_zeile_ihres_hebelsatzes():
    for weg, hebelsatz in dokument.SCHREIB_PLATTFORM:
        assert (weg, hebelsatz) in VERFAHREN


def test_auswahl_nach_weg_und_hebelsatz():
    assert verfahren_fuer("weishaupt_modbus", "weishaupt_wwp") is VERFAHREN[("weishaupt_modbus", "weishaupt_wwp")]
    assert verfahren_fuer("simulation", "viessmann_vicare") is VERFAHREN[("simulation", "viessmann_vicare")]


@pytest.mark.parametrize(("weg", "hebelsatz"), [
    ("vaillant_myvaillant", "viessmann_vicare"), ("viessmann_vicare", "vaillant_vrc720"),
    ("weishaupt_modbus", "vaillant_vrc720"), ("vaillant_myvaillant", "weishaupt_wwp_basis"),
    ("vaillant_myvaillant", "erfunden"),
])
def test_hebelsatz_ohne_zeile_am_weg_gibt_keine_zeile(weg, hebelsatz):
    assert verfahren_fuer(weg, hebelsatz) is None


@pytest.mark.parametrize("weg", ["vaillant_ebusd", "", "Simulation", "vaillant_myvaillant ", None, 3])
def test_unbekannter_weg_scheitert_laut(weg):
    with pytest.raises(WegUnbekannt) as fehler:
        verfahren_fuer(weg, "vaillant_vrc720")
    assert fehler.value.code == WEG_UNBEKANNT == config_check.WEG_UNBEKANNT == "weg_unbekannt"


def test_jede_zeile_hat_lokale_sicherheitswerte():
    assert {hebelsatz for _, hebelsatz in VERFAHREN} <= {hebelsatz for hebelsatz, _ in LOCAL_SAFETY}


def test_verfahrensfelder_wie_bis_1_0_0():
    v, w, b, s = (VERFAHREN[k] for k in (
        ("vaillant_myvaillant", "vaillant_vrc720"), ("weishaupt_modbus", "weishaupt_wwp"),
        ("weishaupt_modbus", "weishaupt_wwp_basis"), ("viessmann_vicare", "viessmann_vicare")))
    assert (v.prepared_lever, v.preparation_resets_setpoint, v.aux_originals, v.schemata) == (
        "room_setpoint", True, ("mode_select",), ())
    for x in (w, b):
        assert (x.prepared_lever, x.preparation_resets_setpoint) == ("room_setpoint", False)
        assert x.aux_originals == ("mode_select", "setpoint_comfort", "setpoint_setback")
        assert x.schemata == ("praefixe", "normal_bereich")
    assert (s.prepared_lever, s.preparation_resets_setpoint, s.aux_originals, s.schemata) == (
        "room_setpoint", False, ("mode_select",), ())
    assert v.labels["min_flow"] == "Mindestvorlauftemperatur" and s.preparation_label == "Heizprogramm"
