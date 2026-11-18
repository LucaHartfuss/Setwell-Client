"""Batterieueberwachung der Raumfuehler/Thermostate. Die Batterie-Entities hat der
Wizard ermittelt (Feld bindung.batterien). Prozent-Sensor: < 20 -> niedrig, >= 25 -> ok,
dazwischen bleibt der letzte Zustand. binary_sensor: on -> niedrig, off -> ok. unavailable/
unknown aendert nichts. Gemeldet wird nur beim Wechsel (notifier), nicht kritisch."""
import logging
import math

from smartheat_core import meldungen
from smartheat_core.logtext import lesbar
from smartheat_core.meldungen import Meldung
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG

logger = logging.getLogger(__name__)

LOW_BELOW_PERCENT = 20
RECOVERED_FROM_PERCENT = 25
STATE_LOW = "niedrig"


def next_state(previous: str, art: str, raw: str) -> str:
    if art == BATTERY_LOW_FLAG:
        return {"on": STATE_LOW, "off": STATE_OK}.get(raw, previous)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return previous
    if not math.isfinite(value):
        return previous
    if value < LOW_BELOW_PERCENT:
        return STATE_LOW
    if value >= RECOVERED_FROM_PERCENT:
        return STATE_OK
    return previous


def _meldung(label: str, art: str, state: str, raw: str) -> Meldung:
    if state == STATE_OK:
        return meldungen.neu("meldung.batterie_ok", bezeichnung=label)
    if art != BATTERY_LOW_FLAG:
        try:
            prozent = float(raw)
        except (TypeError, ValueError):
            prozent = math.nan  # der Zustand blieb aus dem Vorlauf stehen, ohne lesbaren Wert
        if math.isfinite(prozent):
            return meldungen.neu("meldung.batterie_schwach_prozent", bezeichnung=label, prozent=prozent)
    return meldungen.neu("meldung.batterie_schwach", bezeichnung=label)


def check_batteries(rt) -> None:
    for battery in rt.config.battery_refs:
        key = f"batterie:{battery.ref}"
        try:
            raw = rt.signals.get_raw_state(battery.ref)
        except SignalNotFound:
            logger.info("Batterie-Entity %s nicht gefunden (404), wird uebersprungen", battery.ref)
            continue
        except SourceUnavailable as error:
            logger.warning("Batteriepruefung abgebrochen, %s: %s", rt.texts.source_unavailable_log, lesbar(str(error)))
            return
        except ValueError:
            continue
        state = next_state(rt.notifier.state(key), battery.art, raw)
        rt.notifier.notify(
            key, state, _meldung(rt.signals.device_key(battery.ref), battery.art, state, raw), critical=False,
        )
