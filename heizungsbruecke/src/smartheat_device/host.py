"""Schnittstelle Host <-> Geraetekern: was der Host dem Paket liefert (Soll-Quelle, Uhr, Inventur,
Befehle, Anzeige) und wie das Paket ihm Dokumente uebergibt. Gateway und HA-Add-on (5c) setzen sie um. Alle
Rueckrufe laufen im Geraete-Thread, duerfen nicht lange blockieren (Befehle mit Wartezeit liefern Waiting) und keine
Ausnahme werfen: das Geraet faengt sie zwar ab und loggt nur Fehlerart und Ort (die Rueckrufe des Takts je Rueckruf und
Fehlerart einmal), aber die Wirkung des Rueckrufs fehlt dann (z. B. eine nicht angewandte Konfiguration bis zur
naechsten Version, Registrierung und hello erst in einem spaeteren Takt, kein up/status in diesem Takt, uhr_synchron
gilt als None, ursprungswerte als {}, die Anzeige entfaellt). Einzige Ausnahme: konfiguration_vorbereiten darf werfen;
dann speichert das Geraet nichts und haelt das Dokument zurueck."""
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from smartheat_core.meldungen import Meldung
from smartheat_device.commands import CommandRegister


class MeldungSink:
    """NotifySink der Laufzeit (smartheat_runtime.ports) auf den Geraetekern (up/notifications), fuer jeden Host
    (Gateway allein, HA-Add-on neben HA-Benachrichtigungen). Regeln: show = offene
    kritische Meldung, withdraw = geschlossen, push = Ereignis (offen nur, solange der key offen ist).
    `melden(key, meldung, *, offen)` ist Device.meldung (oder ein Host-Rueckruf darauf); Code, Parameter und Stufe
    stehen in der Meldung, einen Text bildet der Sink nicht. Geschlossen wird mit der zuletzt offenen Meldung des
    keys, ohne eine solche mit None (der Kern schliesst dann nur)."""

    def __init__(self, melden: Callable[..., None]) -> None:
        self._melden = melden
        self._offen: dict[str, Meldung] = {}
        self._lock = threading.Lock()

    def push(self, key: str, meldung: Meldung) -> None:
        with self._lock:
            offen = key in self._offen
        self._melden(key, meldung, offen=offen)

    def show(self, key: str, meldung: Meldung) -> None:
        with self._lock:
            self._offen[key] = meldung
        self._melden(key, meldung, offen=True)

    def withdraw(self, key: str) -> None:
        with self._lock:
            meldung = self._offen.pop(key, None)
        self._melden(key, meldung, offen=False)


@dataclass(frozen=True)
class Anzeige:
    """Was der Host anzeigt (Gateway: LED und Diagnoseseite; HA: Status und Reparatur-Hinweise, 5c)."""
    zustand: str  # wire.ZUSTAENDE
    server_ok: bool  # Link verbunden
    device_state: str | None  # zuletzt bekannter Uebernahmestatus (Bootstrap)
    eingerichtet: bool
    hinweise: tuple[dict, ...]  # Befehl hinweis: {key, code, params, stufe}
    meldungen: tuple[dict, ...]  # eigene Meldungen des Geraets (wunsch_begrenzt, soll_quelle_aus): {key, text}
    konfiguration_abgelehnt: dict | None  # zuletzt abgelehnte Konfiguration als Meldung {code, params, stufe}

    @property
    def code_zeigen(self) -> bool:
        """Uebernahme-Code zeigen bzw. melden (F1, Gateway-Seite und HA-Discovery gleich): nur solange
        der Server das Geraet als nicht uebernommen fuehrt und es weder startet noch gesperrt ist (der Sperrzustand
        steht nur im Speicher, device_state bleibt auf der Platte nicht_uebernommen)."""
        return (self.device_state == "nicht_uebernommen" and not self.eingerichtet
                and self.zustand not in ("startet", "gesperrt"))


class DeviceHost(Protocol):
    host: str  # wire.HOSTS
    version: str  # Software-Version (hello, register, Mindestversion)

    def faehigkeiten(self) -> dict:
        """hello.faehigkeiten bzw. register.capabilities: drivers, zigbee, optional updater, programm, testphase, kaelteschutz."""
        ...

    def wege(self) -> list[str]:
        """hello.wege: die Weg-IDs (Katalog des Servers), ueber die dieser Host Anlagen anbinden kann."""
        ...

    def uhr_synchron(self) -> bool | None:
        """Zeitdienst des Systems (Gateway: NTP); None = unbekannt, gilt als nicht synchron."""
        ...

    def treiber_pruefen(self, bindung: dict, /) -> dict | None:
        """details (smartheat_device.fehler), wenn die Treiberparameter nicht passen (config_check, Regel 6), sonst None.
        Gateway: `bindung`; HA: die ganze Konfiguration. Nur positional (config_check ruft so), daher darf
        der Host den Parameter passend benennen."""
        ...

    def befehle_eintragen(self, register: CommandRegister) -> None: ...

    def ursprungswerte(self) -> dict:
        """Gemerkte Ursprungswerte der Laufzeit (Hebel aus restore_originals und Hilfswerte); {} ohne Laufzeit."""
        ...

    def konfiguration_vorbereiten(self, alt: dict | None, neu: dict) -> None:
        """Vor dem Speichern einer geprueften Konfiguration: eingerichtet -> leer
        schreibt die Aufraeum-Markierung. Der einzige Rueckruf, der werfen darf: dann speichert das Geraet nichts und
        haelt das Dokument zurueck wie bei config_check.NichtPruefbar."""
        ...

    def konfiguration_uebernommen(self, alt: dict | None, neu: dict) -> None:
        """Neue Konfiguration (auch leer = nicht eingerichtet), schon gespeichert: Laufzeit neu starten, wenn
        dokument_config.laufzeit_relevant."""
        ...

    def bedienung_uebernommen(self, inhalt: dict) -> None:
        """Neue Bedienung: raum_soll anwenden und die Soll-Quelle angleichen, wenn sie schreibbar ist."""
        ...

    def software_soll(self, software: dict | None) -> None:
        """Feld software jeder Konfiguration mit bekanntem Schema, auch einer abgelehnten (Updater)."""
        ...

    def runtime_status(self) -> dict | None: ...

    def entfernen_wahl(self) -> str | None:
        """Wahl des Nutzers beim Entfernen, solange sie dem Server noch nicht zugestellt ist: "freigeben" (das Geraet
        verlaesst das Konto) oder None. Der Host liest sie aus der Abmelde-Markierung."""
        ...

    def entfernen_erledigt(self) -> None:
        """Der Server hat die Wahl bestaetigt (PUBACK): der Host loescht sie."""
        ...

    def raum(self, kreis: int = 1) -> dict | None:
        """{ist, soll, soll_quelle, ts} des Kreises `kreis` fuer up/status; None, solange er noch nichts veroeffentlicht hat."""
        ...

    def anzeigen(self, anzeige: Anzeige) -> None:
        """Jeden Takt; der Host drosselt selbst (LED-Datei, Seite)."""
        ...
