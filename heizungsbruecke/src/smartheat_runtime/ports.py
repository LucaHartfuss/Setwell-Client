"""Ports des hostneutralen Kerns: die einzigen Stellen, an denen die Laufzeit die Aussenwelt beruehrt.
Die Anlage ist smartheat_core.binding.PlantBinding, die Wanduhr smartheat_core.wallclock; die monotone Uhr wird als
Callable[[], float] uebergeben."""
from dataclasses import dataclass
from typing import Protocol

from smartheat_core.meldungen import Meldung


class SignalNotFound(LookupError):
    """Die Referenz gibt es bei der Quelle nicht (HA: HTTP 404). Ueberwachungen ueberspringen sie."""


class SignalUngueltig(ValueError):
    """Ein Signal liefert gerade keinen gueltigen Wert (nicht verfuegbar, keine Zahl, ausserhalb des Zahlenbereichs, kein
    Fuehler gueltig) - erwartet, kein Programmfehler. Die Hosts werfen sie beim Lesen eines Werts; Programmier- und
    Konfigurationsfehler bleiben ein einfacher ValueError. Unterklasse von ValueError: bestehende `except ValueError`
    gelten weiter."""


class SourceUnavailable(OSError):
    """Die Quelle antwortet nicht (HA nicht erreichbar, Zeitueberschreitung, andere HTTP-Fehler). Ueberwachungen beenden
    die Runde ohne Meldung."""


class QuelleErschoepft(SourceUnavailable):
    """Das Abfragekontingent der Quelle ist aufgebraucht (Gateway: Cloud-Treiber), die Daten sind veraltet. Der Text ist
    der Kundentext zum Kontingent; die Meldung dazu gibt kontingent.check, die Ueberwachungen beenden die Runde still."""


@dataclass(frozen=True)
class Stoerung:
    """Eine anstehende Meldung des Wärmeerzeugers: Herstellerschlüssel der Quelle (HA: `mypyllant`), die Kennung wie
    geliefert (die Laufzeit normalisiert sie) und ob es nur ein Hinweis ist (Wartung). Nie ein Text des Herstellers."""
    quelle: str
    kennung: str
    hinweis: bool = False


class SignalSource(Protocol):
    """Werte ueber hostspezifische Referenzen lesen (HA: Entity-ID bzw. `entity::attribut`). Ein ungueltiger Wert
    (nicht verfuegbar, keine Zahl, Attribut fehlt) wirft SignalUngueltig (ein ValueError), KeyError oder TypeError."""

    def get_state(self, ref: str) -> float: ...

    def get_raw_state(self, ref: str) -> str: ...

    def get_unit(self, ref: str) -> str:
        """Einheit hinter der Referenz (HA: Attribut unit_of_measurement); wirft wie get_raw_state, wenn keine da ist."""
        ...

    def device_key(self, ref: str) -> str:
        """Geraet hinter einer Referenz, fuer Meldeschluessel und Kundentexte (HA: Entity-ID ohne ::attribut,
        Gateway: IEEE-Adresse bzw. treiber:<rolle>)."""
        ...

    def get_stoerungen(self, ref: str) -> tuple[Stoerung, ...]:
        """Anstehende Stoerungen und Hinweise des Waermeerzeugers hinter der Referenz (HA: Attribut der Entity); leer,
        wenn die Quelle keine kennt oder keine ansteht. Fehler der Quelle wie bei get_raw_state."""
        ...


class StatusSink(Protocol):
    def publish(self, event: dict) -> None:
        """Status-Modell (Schema 2) veroeffentlichen. Darf werfen; der Reporter sendet beim naechsten Anlass erneut."""
        ...


class NotifySink(Protocol):
    """Meldungen an den Kunden. Entprellung, Hinweis-Schalter und offene Meldungen fuehrt der Notifier."""

    def push(self, key: str, meldung: Meldung, *, begleitet: bool = False) -> None:
        """Einmalige Nachricht (HA: alle Notify-Dienste). Fehler einzelner Kanaele behandelt der Sink selbst.
        `begleitet`: auf den Push folgt im selben Uebergang `show` bzw. `withdraw`; ein Sink, der je Uebergang ein
        Ereignis meldet, laesst es dann dem Aufruf danach."""
        ...

    def show(self, key: str, meldung: Meldung) -> None:
        """Offene kritische Meldung anlegen oder ersetzen (HA: persistent_notification). Darf werfen."""
        ...

    def withdraw(self, key: str) -> None:
        """Offene kritische Meldung zuruecknehmen. Darf werfen."""
        ...


class TriggerSource(Protocol):
    """Trigger-Eingang, Ereignisvertrag fuer jeden Host:
    - Aenderung der Wunschtemperatur: erst nach ROOM_TARGET_DEBOUNCE_SECONDS (debounce.py) Stabilitaet
      EV_LOCAL_CHECK(room_target_fired=True);
    - jede andere zugeordnete Signalaenderung: EV_LOCAL_CHECK(room_target_fired=False) (post_coalesced);
    - jede (Wieder-)Verbindung der Quelle: EV_LOCAL_CHECK(room_target_fired=True) und EV_SOURCE_CONNECTED.
    HA: WebSocket-Trigger (Entprellung ueber `for:`); Gateway: lokaler Bus mit Debouncer. `connected` steuert den
    Watchdog-Rueckfall."""

    @property
    def connected(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class MqttChannel(Protocol):
    """MQTT-Verbindung der Laufzeit: Snapshot und Telemetrie hoch, Setpoints herunter. Die Laufzeit haelt keine eigene
    Verbindung; app.start bekommt sie vom Host (mqtt_factory): smartheat_device.laufzeit.RuntimeChannel auf dem Link des
    Geraets."""

    @property
    def connect_failures(self) -> int: ...

    def is_connected(self) -> bool: ...

    def publish_snapshot(self, payload: dict) -> None: ...

    def publish_telemetry(self, payload: dict) -> None: ...

    def loop_start(self) -> None: ...

    def stop(self) -> None: ...
