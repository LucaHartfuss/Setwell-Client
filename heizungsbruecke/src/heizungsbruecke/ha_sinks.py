"""Meldungs-Sink des HA-Hosts: Meldungen als Push an die Notify-Dienste und als persistent_notification;
im Geraete-Pfad zuerst als up/notifications an den Geraetekern (GeraetUndHaSink, wie das Gateway).
Das Status-Ereignis `smartheat_status` sendet geraet_status.GeraetStatus. Ereignisname, Titel und Benachrichtigungs-IDs
sind Cross-Repo-Vertrag mit der Integration (Contract-Check (Vertrag V5))."""
import logging
import re
import threading
from collections.abc import Callable

from smartheat_core import meldungen
from smartheat_core.logtext import lesbar
from smartheat_core.meldungen import Meldung
from smartheat_device import nachrichten
from smartheat_device.host import MeldungSink
from smartheat_runtime.texts import HA_TEXTS

logger = logging.getLogger(__name__)

EVENT_TYPE = "smartheat_status"
#: Ereignis je Uebergang einer Meldung an die Integration: {schema, geraet_id, key, code, params, kreis?, stufe, offen}.
EREIGNIS_MELDUNG = "smartheat_meldung"
MELDUNG_SCHEMA = 1
#: Felder einer Meldung im Status-Ereignis und im Meldungs-Ereignis; `kreis` steht nur bei einer Kreismeldung.
MELDUNG_FIELDS = ("key", "code", "params", "kreis", "stufe")
#: Felder des Ereignisses je Uebergang; `kreis` wie oben nur, wenn gesetzt.
MELDUNG_EVENT_FIELDS = ("schema", "geraet_id", "key", "code", "params", "kreis", "stufe", "offen")
TITLE = "SmartHeat"


def notification_id(key: str) -> str:
    """Stabile ID je Schluessel: eine neue Meldung ersetzt die vorige, statt sich zu stapeln."""
    return "smartheat_" + re.sub(r"[^a-z0-9_]", "_", key.lower())


class HaNotifySink:
    def __init__(self, ha_api, services: list[str]) -> None:
        self._ha_api = ha_api
        self._services = list(services)

    def push(self, key: str, meldung: Meldung, *, begleitet: bool = False) -> None:
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

    def ereignis(self, art: str, daten: dict) -> None:
        self._ha_api.fire_event(art, daten)


def meldung_daten(key: str, meldung: Meldung) -> dict:
    """Die Meldung im Ereignis (Status `meldungen` und `smartheat_meldung`): {key, code, params, stufe} und `kreis`,
    wenn der Key zu einem Kreis gehoert. Code und Parameter des Katalogs, nie ein Text und nie ein Name (S-12): ein Key
    mit Entity-, Raum- oder Anlagennamen geht als `<kategorie>:<Pruefsumme>` (wie auf dem Draht), die lokalen Parameter
    fehlen."""
    draht_key, kreis = nachrichten.draht_key(key)
    draht = meldungen.draht(meldung)
    daten: dict = {"key": draht_key, "code": draht["code"], "params": draht["params"], "stufe": draht["stufe"]}
    if kreis is not None:
        daten["kreis"] = kreis
    return daten


class GeraetUndHaSink:
    """NotifySink der Laufzeit im HA-Geraete-Pfad: jede Meldung zuerst an den Geraetekern (MeldungSink wie beim Gateway,
    wirft nie), dann an HA (Notify-Dienste, persistent_notification) und als Ereignis `smartheat_meldung` je Uebergang
    (`offen` wie beim MeldungSink: show offen, withdraw geschlossen mit der zuletzt offenen Meldung des Keys, push offen
    nur, solange der Key offen ist; eine Entwarnung ohne offene Meldung hat keinen Code und feuert nichts). Ohne
    Geraete-ID feuert nichts. Ein HA-Fehler (HA Core startet) erreicht den Notifier (show/withdraw duerfen werfen), die
    Meldung an den Server ist dann schon unterwegs; ein Fehler beim Feuern des Ereignisses stoert die Meldung nicht."""

    def __init__(self, geraet: MeldungSink, ha: HaNotifySink, geraet_id: Callable[[], str | None]) -> None:
        self._geraet, self._ha, self._geraet_id = geraet, ha, geraet_id
        self._offen: dict[str, Meldung] = {}
        self._lock = threading.Lock()

    def _ereignis(self, key: str, meldung: Meldung | None, *, offen: bool) -> None:
        geraet_id = self._geraet_id()
        if geraet_id is None or meldung is None:
            return
        daten = {"schema": MELDUNG_SCHEMA, "geraet_id": geraet_id, **meldung_daten(key, meldung), "offen": offen}
        try:
            self._ha.ereignis(EREIGNIS_MELDUNG, daten)
        except Exception as fehler:
            logger.warning("Meldungs-Ereignis konnte nicht gefeuert werden: %s", type(fehler).__name__)

    def push(self, key: str, meldung: Meldung, *, begleitet: bool = False) -> None:
        self._geraet.push(key, meldung)
        self._ha.push(key, meldung)
        if begleitet:
            return  # show bzw. withdraw im selben Uebergang feuert das Ereignis
        with self._lock:
            offen = key in self._offen
        self._ereignis(key, meldung, offen=offen)

    def show(self, key: str, meldung: Meldung) -> None:
        self._geraet.show(key, meldung)
        with self._lock:
            self._offen[key] = meldung
        try:
            self._ha.show(key, meldung)
        finally:
            self._ereignis(key, meldung, offen=True)

    def withdraw(self, key: str) -> None:
        self._geraet.withdraw(key)
        with self._lock:
            meldung = self._offen.pop(key, None)
        try:
            self._ha.withdraw(key)
        finally:
            self._ereignis(key, meldung, offen=False)
