"""Meldung zum erschoepften Abfragekontingent der Quelle (Gateway: Cloud-Treiber), nach dem Muster von
aussentemperatur. Eine Quelle mit Kontingent bietet `kontingent_erschoepft() -> bool` (True, solange das Kontingent
erschoepft ist und die Daten veraltet sind); die Quelle des HA-Hosts hat keins, dann gibt es nichts zu pruefen. Die
Meldung ist ein Code ohne Parameter, den Text bildet der Server. Nicht kritisch (nur Push, ein Wechsel je Ende der
Sperre); der Schluessel ist Freitext (V1, device_checks des Servers), kein Vertrag."""
from smartheat_core import meldungen
from smartheat_runtime.notifier import STATE_OK

KEY = "kontingent"
STATE_ERSCHOEPFT = "erschoepft"


def check(rt) -> None:
    abfrage = getattr(rt.signals, "kontingent_erschoepft", None)
    if abfrage is None:
        return
    if abfrage():
        rt.notifier.notify(KEY, STATE_ERSCHOEPFT, meldungen.neu("meldung.kontingent_erschoepft"), critical=False)
    else:
        rt.notifier.notify(KEY, STATE_OK, meldungen.neu("meldung.kontingent_frei"), critical=False)
