"""Das geltende Soll, das der Kreis dem Server meldet (Snapshot: `room_target`, Telemetrie: `room_target` und `soll_art`).

Der Server misst gegen das Soll, das am Thermostat wirklich gilt, nicht gegen den Wunsch. Gemeldet wird es nur, wenn die
Dokumente eine Planeingabe hergeben (Faehigkeit `programm`) und der Kreis nicht in der Testphase ohne Setwell steht;
sonst bleibt es beim Wert und den Feldern wie vor den Betriebsarten."""
import logging

from smartheat_core.plan import SollErgebnis
from smartheat_runtime import testphase
from smartheat_runtime.runtime import Runtime

logger = logging.getLogger(__name__)


def gemeldet(rt: Runtime) -> SollErgebnis | None:
    """Das Ergebnis des letzten lokalen Checks, wenn es gemeldet wird; sonst None (ohne Soll, ohne Plan, in der
    Testphase). Wirft nie: eine nicht lesbare Planquelle gilt als kein Plan (Log nur mit der Fehlerart)."""
    soll = rt.geltendes_soll
    if soll is None or soll.soll is None or soll.soll_art is None:
        return None
    try:
        if testphase.aktiv(rt) or rt.config.plan_source() is None:
            return None
    except Exception as fehler:
        logger.warning("Planeingabe nicht lesbar (%s), das Soll wird nicht als geltendes gemeldet", type(fehler).__name__)
        return None
    return soll
