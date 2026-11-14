"""Datentraeger nicht beschreibbar: Meldung an den Kunden. Der Zustand selbst steht
in StateStore.storage_failed (nur im Speicher); gemeldet wird nach jedem Worker-Ereignis
(app._after_each), entwarnt beim ersten erfolgreichen Schreiben (StateStore.flush im Takt
EV_HEALTH). Die Zustellmaschine meldet einen Datentraeger-Datenfehler nicht selbst (delivery.py),
sonst kaeme dieselbe Stoerung doppelt; wartet ein Tick daran, holt ihn derselbe Takt sofort nach
(delivery.StorageWritable)."""
from smartheat_core import meldungen
from smartheat_runtime.notifier import STATE_OK

KEY = "datentraeger"
STATE_FAILED = "nicht_beschreibbar"


def report(store, notifier) -> None:
    """Meldet nur bei einem Zustandswechsel (Notifier); kritisch, also Push und HA-Benachrichtigung."""
    if store.storage_failed:
        notifier.notify(KEY, STATE_FAILED, meldungen.neu("meldung.datentraeger_nicht_beschreibbar"), critical=True)
    else:
        notifier.notify(KEY, STATE_OK, meldungen.neu("meldung.datentraeger_ok"), critical=True)
