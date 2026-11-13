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

#: Bindungsparameter des Hebelsatzes viessmann_vicare, wie der Server sie sendet
_PARAMETER = {
    "schritte": {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
    "toleranzen": {"curve": 0.05, "level": 0.5, "room_setpoint": 0.5}, "wartezeit_s": 180,
    "schreibgruppen": [["curve", "level"]], "zurueck": ["curve", "level", "room_setpoint"],
    "zurueck_optional": [], "nur_lesen": [], "zaehler": "daily",
    "schreibschutz": {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 4}, "schemata": {},
    "energie": {kanal: "kWh" for kanal in ("electrical_heating", "electrical_dhw", "primary_heating", "primary_dhw",
                                           "thermal_heating", "thermal_dhw", "electrical_total")},
}


def konfiguration() -> dict:
    """Konfigurationsdokument (ohne schema/version) wie der Server es sendet: ein Kreis mit Simulationstreiber."""
    raum = {"raum": 1, "name": "Wohnzimmer", "rolle": "referenz", "fuehler": [f"zigbee:{SENSOR}:temperature"],
            "soll_quelle": False, "versatz_k": 0.0, "ziel_fest": None}
    bindung = {"treiber": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 0}},
               "thermostat": THERMOSTAT}
    return {
        "anlage": "akzeptanz", "anlage_name": "Akzeptanzanlage", "weg": "simulation",
        "kreise": [{"kreis": 1, "name": "Kreis 1", "bindung": bindung, "raeume": [raum],
                    "hebelsatz": "viessmann_vicare", "verteilsystem": "Heizkoerper", "abo": "aktiv"}],
        "tagestick": "12:00", "mindest_software": "0.6.0", "mindest_integration": None,
        "software": None, "bindungsparameter": copy.deepcopy(_PARAMETER),
    }
