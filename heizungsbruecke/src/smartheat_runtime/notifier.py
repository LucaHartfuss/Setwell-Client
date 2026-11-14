"""Meldungen an den Kunden: Push (HA: an alle konfigurierten Notify-Dienste), bei
kritischen Anlaessen zusaetzlich eine offene Meldung (HA: persistent_notification), die beim
Rueckwechsel auf "ok" wieder verschwindet. Gemeldet wird nur bei einem Zustandswechsel je Schluessel. Der Zustand liegt
in BridgeState.notify_states (backup.json) und uebersteht Neustarts: kein Meldungssturm, wenn das
Add-on mit demselben Fehler erneut startet.

HA haelt persistent_notifications nur im Speicher. Deshalb bleibt die Meldung (Code, Parameter, Stufe) jeder
offenen kritischen Meldung in BridgeState.notify_meldungen, und republish_persistent() legt sie nach einem HA-Neustart
neu an -- ohne erneuten Push.

Die fuenf Hinweis-Kategorien (config_check.HA_HINWEISE, Feld bindung.hinweise_aus) lassen sich einzeln
abschalten: der Zustand wird weiter verfolgt und geloggt (Grundlage der Hinweise im Status), nur
der Push entfaellt. Kritische Meldungen sind nie abschaltbar."""
import logging

from smartheat_core import meldungen
from smartheat_core.logtext import lesbar
from smartheat_core.meldungen import Meldung
from smartheat_runtime.ports import NotifySink

logger = logging.getLogger(__name__)

STATE_OK = "ok"


def category(key: str) -> str:
    """Kategorie eines Meldeschluessels: der Teil vor ":" (raumfuehler:<entity> -> raumfuehler)."""
    return key.partition(":")[0]


class Notifier:
    def __init__(self, store, sink: NotifySink, hints_off=()) -> None:
        self._store = store
        self._sink = sink
        self._hints_off = frozenset(hints_off)

    def state(self, key: str) -> str:
        return self._store.state.notify_states.get(key, STATE_OK)

    def seed(self, key: str, state: str) -> None:
        """Uebernimmt einen anderswo persistierten Zustand (failsafe_state.json) ohne Meldung,
        wenn der Schluessel noch fehlt: sonst ginge nach einem Update oder einem gescheiterten
        Schreiben die Entwarnung verloren."""
        if state == STATE_OK or key in self._store.state.notify_states:
            return
        try:
            self._store.update(notify_states={**self._store.state.notify_states, key: state})
        except Exception:
            logger.exception("Meldezustand '%s' konnte nicht gespeichert werden", lesbar(key))

    def notify(self, key: str, state: str, meldung: Meldung, *, critical: bool, silent_ok: bool = False) -> bool:
        """True bei einem Zustandswechsel. Jeder Kanal ist best effort; ein Schreibfehler des
        Zustands verhindert die Meldung nicht (sie kaeme sonst nie an). Ohne Push (nur Log):
        abgeschaltete Hinweis-Kategorien und, mit silent_ok, der Wechsel auf "ok"."""
        if state == self.state(key) and not self._ablage_fehlt(key, state, critical):
            return False
        states = dict(self._store.state.notify_states)
        ablagen = dict(self._store.state.notify_meldungen)
        if state == STATE_OK:
            states.pop(key, None)
        else:
            states[key] = state
        if critical and state != STATE_OK:
            ablagen[key] = meldungen.zu_dict(meldung)
        else:
            ablagen.pop(key, None)
        try:
            self._store.update(notify_states=states, notify_meldungen=ablagen)
        except Exception:
            logger.exception("Meldezustand '%s' konnte nicht gespeichert werden, Meldung geht trotzdem raus",
                             lesbar(key))
        # Das Log nennt nur den Code: weder Parameter (koennen Namen oder Text vom Server tragen) noch Schluessel.
        if state == STATE_OK:
            logger.info("Meldung %s", meldung.code)
        else:
            logger.warning("Meldung %s", meldung.code)
        muted = (not critical and category(key) in self._hints_off) or (silent_ok and state == STATE_OK)
        if not muted:
            try:
                self._sink.push(key, meldung, begleitet=critical)
            except Exception:
                logger.warning("Push-Benachrichtigung '%s' konnte nicht gesendet werden", lesbar(key))
        if critical:
            self._update_persistent(key, state, meldung)
        return True

    def _ablage_fehlt(self, key: str, state: str, critical: bool) -> bool:
        """Ein offener kritischer Zustand ohne gueltige Ablage (Bestand vor den Codes, beschaedigter Eintrag): republish
        ueberspringt ihn, der Kunde und der Server haben ihn nie als Meldung gesehen. Bestaetigt die Pruefung ihn, gilt
        er als neu (Push, offene Meldung, Ablage); danach ist er unveraendert."""
        return (critical and state != STATE_OK
                and meldungen.aus_dict(self._store.state.notify_meldungen.get(key)) is None)

    def refresh_persistent(self, key: str, meldung: Meldung) -> None:
        """Legt die HA-Benachrichtigung eines unveraenderten, nicht-"ok" Zustands mit dem
        aktuellen Meldung erneut an (ersetzt die vorige je Schluessel), ohne Push. Fuer einen
        Startfehler, der bei jedem Start erneut auftritt: nach einem Host-Neustart fehlt die
        Benachrichtigung sonst, weil HA sie nicht speichert."""
        state = self.state(key)
        if state == STATE_OK:
            return
        ablage = meldungen.zu_dict(meldung)
        if self._store.state.notify_meldungen.get(key) != ablage:
            try:
                self._store.update(notify_meldungen={**self._store.state.notify_meldungen, key: ablage})
            except Exception:
                logger.exception("Meldung '%s' konnte nicht gespeichert werden", lesbar(key))
        self._update_persistent(key, state, meldung)

    def republish_persistent(self) -> None:
        """Legt die HA-Benachrichtigungen aller offenen kritischen Meldungen neu an, ohne Push
        (bei jedem (Wieder-)Verbinden mit HA). Ein Schluessel ohne gespeicherte Meldung (per seed()
        uebernommen) wird uebersprungen."""
        for key, state in self._store.state.notify_states.items():
            meldung = meldungen.aus_dict(self._store.state.notify_meldungen.get(key))
            if state != STATE_OK and meldung is not None:
                self._update_persistent(key, state, meldung)

    def clear_all(self, keep=()) -> None:
        """Abmelden: alle offenen HA-Benachrichtigungen ausser `keep` entfernen und
        ihre Meldezustaende leeren, ohne Push."""
        states = self._store.state.notify_states
        for key, state in states.items():
            if state != STATE_OK and key not in keep:
                self._update_persistent(key, STATE_OK, None)
        kept_states = {key: state for key, state in states.items() if key in keep}
        kept_meldungen = {key: ablage for key, ablage in self._store.state.notify_meldungen.items() if key in keep}
        try:
            self._store.update(notify_states=kept_states, notify_meldungen=kept_meldungen)
        except Exception:
            logger.exception("Meldezustaende konnten beim Abmelden nicht geleert werden")

    def _update_persistent(self, key: str, state: str, meldung: Meldung | None) -> None:
        try:
            if state == STATE_OK or meldung is None:
                self._sink.withdraw(key)
            else:
                self._sink.show(key, meldung)
        except Exception:
            logger.warning("Offene Meldung '%s' konnte nicht aktualisiert werden", lesbar(key))
