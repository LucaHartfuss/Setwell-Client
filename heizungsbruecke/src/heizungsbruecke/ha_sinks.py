"""Meldungs-Sink des HA-Hosts: Meldungen als Push an die Notify-Dienste und als persistent_notification;
im Geraete-Pfad zuerst als up/notifications an den Geraetekern (GeraetUndHaSink, wie das Gateway).
Das Status-Ereignis `smartheat_status` sendet geraet_status.GeraetStatus. Ereignisname, Titel und Benachrichtigungs-IDs
sind Cross-Repo-Vertrag mit der Integration (Contract-Check (Vertrag V5))."""
import logging
import re

from smartheat_core.logtext import lesbar
from smartheat_core.meldungen import Meldung
from smartheat_device.host import MeldungSink
from smartheat_runtime.texts import HA_TEXTS

logger = logging.getLogger(__name__)

EVENT_TYPE = "smartheat_status"
TITLE = "SmartHeat"


def notification_id(key: str) -> str:
    """Stabile ID je Schluessel: eine neue Meldung ersetzt die vorige, statt sich zu stapeln."""
    return "smartheat_" + re.sub(r"[^a-z0-9_]", "_", key.lower())


class HaNotifySink:
    def __init__(self, ha_api, services: list[str]) -> None:
        self._ha_api = ha_api
        self._services = list(services)

    def push(self, key: str, meldung: Meldung) -> None:
        message = HA_TEXTS.text(meldung)
        for service in self._services:
            try:
                self._ha_api.send_notification(service, message)
            except Exception:
                logger.warning("Push-Benachrichtigung '%s' an %s konnte nicht gesendet werden", lesbar(key), service)

    def show(self, key: str, meldung: Meldung) -> None:
        self._ha_api.create_persistent_notification(TITLE, HA_TEXTS.text(meldung), notification_id(key))

    def withdraw(self, key: str) -> None:
        self._ha_api.dismiss_persistent_notification(notification_id(key))


class GeraetUndHaSink:
    """NotifySink der Laufzeit im HA-Geraete-Pfad: jede Meldung zuerst an den Geraetekern (MeldungSink wie beim Gateway,
    wirft nie), dann an HA. Ein HA-Fehler (HA Core startet) erreicht den Notifier (show/withdraw duerfen werfen), die
    Meldung an den Server ist dann schon unterwegs."""

    def __init__(self, geraet: MeldungSink, ha: HaNotifySink) -> None:
        self._geraet, self._ha = geraet, ha

    def push(self, key: str, meldung: Meldung) -> None:
        self._geraet.push(key, meldung)
        self._ha.push(key, meldung)

    def show(self, key: str, meldung: Meldung) -> None:
        self._geraet.show(key, meldung)
        self._ha.show(key, meldung)

    def withdraw(self, key: str) -> None:
        self._geraet.withdraw(key)
        self._ha.withdraw(key)
