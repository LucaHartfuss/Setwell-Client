"""Die Anlagen der Laufzeit-Akzeptanzsuite: eine Gastherme (Viessmann, Heizkoerper) mit Simulationstreiber, ein
Thermostat und ein Raumfuehler am Zigbee-Bus. Alles Literale (Adressen, Bindungsparameter wie der Server sie sendet);
nichts kommt aus dem Produktcode oder aus den Hilfen der uebrigen Tests, damit ein Fehler dort nicht auch die
Erwartung verschiebt."""
import copy

SENSOR = "0x00124b0000000001"
THERMOSTAT = "0x00124b0000000002"

_TEMPERATUR = {"type": "numeric", "name": "temperature", "property": "temperature", "access": 1, "unit": "C"}
_BATTERIE = {"type": "numeric", "name": "battery", "property": "battery", "access": 1, "unit": "%"}
_BATTERIE_LEER = {"type": "binary", "name": "battery_low", "property": "battery_low", "access": 1}
_KLIMA = {"type": "climate", "features": [
    {"name": "occupied_heating_setpoint", "property": "occupied_heating_setpoint", "access": 7},
    {"name": "local_temperature", "property": "local_temperature", "access": 5},
]}
#: Was Zigbee2MQTT in bridge/devices ueber die beiden Geraete sagt (Auszug: Anzeige und Messwerte)
GERAETE = [
    {"ieee_address": SENSOR, "friendly_name": SENSOR, "type": "EndDevice", "interview_completed": True,
     "definition": {"model": "SNZB-02D", "vendor": "SONOFF", "exposes": [_TEMPERATUR, _BATTERIE]}},
    {"ieee_address": THERMOSTAT, "friendly_name": THERMOSTAT, "type": "EndDevice", "interview_completed": True,
     "definition": {"model": "TRV-Test", "vendor": "Test", "exposes": [_KLIMA, _BATTERIE_LEER]}},
]

_KANAELE = ("electrical_heating", "electrical_dhw", "primary_heating", "primary_dhw", "thermal_heating",
            "thermal_dhw", "electrical_total")
_SCHUTZ_EEPROM = {"tag": 10, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 6}
_SCHEMATA = {"praefixe": ["hz_operationmode_", "heating_circuit_operation_mode_"],
             "normal_bereich": {"hz_operationmode_": [16.0, 28.0], "heating_circuit_operation_mode_": [18.0, 25.0]}}
#: Bindungsparameter je Hebelsatz, wie der Server sie sendet (ohne die Energiekanaele)
_PARAMETER = {
    "vaillant_vrc720": {
        "schritte": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1},
        "toleranzen": {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.05, "min_flow": 0.05},
        "wartezeit_s": 2100, "schreibgruppen": [], "zurueck": ["curve", "room_setpoint", "heat_limit", "min_flow"],
        "zurueck_optional": ["heat_limit"], "nur_lesen": [], "zaehler": "daily",
        "schreibschutz": {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 6}, "schemata": {},
    },
    "weishaupt_wwp": {
        "schritte": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5},
        "toleranzen": {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.25},
        "wartezeit_s": 120, "schreibgruppen": [], "zurueck": ["curve", "room_setpoint", "heat_limit"],
        "zurueck_optional": [], "nur_lesen": [], "zaehler": "daily", "schreibschutz": _SCHUTZ_EEPROM,
        "schemata": _SCHEMATA,
    },
    "weishaupt_wwp_basis": {
        "schritte": {"room_setpoint": 0.5}, "toleranzen": {"room_setpoint": 0.25}, "wartezeit_s": 120,
        "schreibgruppen": [], "zurueck": ["room_setpoint"], "zurueck_optional": [], "nur_lesen": ["curve"],
        "zaehler": "daily", "schreibschutz": _SCHUTZ_EEPROM, "schemata": _SCHEMATA,
    },
    "viessmann_vicare": {
        "schritte": {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
        "toleranzen": {"curve": 0.05, "level": 0.5, "room_setpoint": 0.5}, "wartezeit_s": 180,
        "schreibgruppen": [["curve", "level"]], "zurueck": ["curve", "level", "room_setpoint"],
        "zurueck_optional": [], "nur_lesen": [], "zaehler": "daily",
        "schreibschutz": {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 4}, "schemata": {},
    },
}
#: Leere Konfiguration: das Abmelden vom Dienst (Felder wie die Konfiguration, ohne Inhalt)
LEERE_KONFIGURATION = {"anlage": None, "anlage_name": None, "weg": None, "kreise": [], "tagestick": None,
                       "mindest_software": "0.6.0", "mindest_integration": None, "software": None,
                       "bindungsparameter": None, "push_regeln": None, "update_noetig": None, "updates": None}


def konfiguration(satz: str = "viessmann_vicare", verteilsystem: str = "Heizkoerper", treiber: dict | None = None) -> dict:
    """Konfigurationsdokument (ohne schema/version) wie der Server es sendet: ein Kreis, Standard mit
    Simulationstreiber; `treiber` ersetzt ihn (Treiber-ID und Parameter)."""
    raum = {"raum": 1, "name": "Wohnzimmer", "rolle": "referenz", "fuehler": [f"zigbee:{SENSOR}:temperature"],
            "soll_quelle": False, "versatz_k": 0.0, "ziel_fest": None}
    bindung = {"treiber": treiber or {"id": "simulation", "parameter": {"lever_set": satz, "heizkreis": 0}},
               "thermostat": THERMOSTAT}
    parameter = copy.deepcopy(_PARAMETER[satz])
    parameter["energie"] = {kanal: "kWh" for kanal in _KANAELE}
    return {
        "anlage": "akzeptanz", "anlage_name": "Akzeptanzanlage",
        "weg": "simulation" if treiber is None else "viessmann_vicare",  # Weg des Cloud-Treibers: die Viessmann-Cloud
        "kreise": [{"kreis": 1, "name": "Kreis 1", "bindung": bindung, "raeume": [raum],
                    "hebelsatz": satz, "verteilsystem": verteilsystem, "abo": "aktiv"}],
        "tagestick": "12:00", "mindest_software": "0.6.0", "mindest_integration": None,
        "software": None, "bindungsparameter": parameter, "push_regeln": None,
        "update_noetig": None, "updates": None,
    }
