"""Meldung zur unklaren Uhr je Kreis. Ruhen die Betriebsarten eines Kreises, weil die Uhr nicht vertrauenswuerdig ist
(`SollErgebnis.uhr_unklar` im geltenden Soll), meldet das Geraet einmal den Code `meldung.uhr_unklar` ohne Parameter und
ohne Zeiten (die Anzeige zeigt sie) und nimmt die Meldung zurueck, sobald der Merker faellt: die Uhr ist wieder
vertrauenswuerdig oder keine Betriebsart ist mehr gesetzt. Die Meldung ist offen (sie schliesst beim Server mit der
Ruecknahme), die Ruecknahme geht ohne Push. Gemeldet wird nur ein Uebergang, nie je Takt.

Ohne geltendes Soll (vor dem ersten Takt nach dem Start, Kreis ausser Betrieb) bleibt der Zustand, wie er ist: eine offene
Meldung ueberlebt den Neustart in der Ablage des Notifiers und wird mit dem ersten Soll bestaetigt oder geschlossen."""
from smartheat_core import meldungen
from smartheat_runtime.notifier import STATE_OK

KEY = "uhr_unklar"
STATE_UNKLAR = "unklar"


def check(rt) -> None:
    soll = rt.geltendes_soll
    if soll is None:
        return
    meldung = meldungen.neu("meldung.uhr_unklar")
    if soll.uhr_unklar:
        rt.notifier.notify(KEY, STATE_UNKLAR, meldung, critical=True)
    else:
        rt.notifier.notify(KEY, STATE_OK, meldung, critical=True, silent_ok=True)
