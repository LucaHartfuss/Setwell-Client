"""Gemeinsamer Teil der Geraete-Hosts: Bindung des Regel-Workers, Nachstellen von
Neustart und Bedienung, die vor dem Binden kamen, gemerkte Ursprungswerte, die Sicht der Laufzeit auf die
Soll-Dokumente, Bedienwunsch, Aufraeum-Markierung und das Aufheben der Abmelde-Markierung. HaDeviceHost und
GatewayDeviceHost erben davon und liefern nur ihre Hostteile (Faehigkeiten, Uhr, Treiberpruefung, Befehle, Anzeige,
Software, Pfade der Markierungen). Ebenfalls hier, weil beide Hosts es gleich brauchen: die Konfiguration der
Laufzeit beim Start (laufzeit_konfiguration, aus der beim Start einmal gelesenen Aufraeum-Markierung), die Ursprungswerte
des Servers (ursprungswerte_server: die Markierung hat Vorrang vor einer neuen Einrichtung) und das Warten von sign_off
auf den Status "abgemeldet" (abmeldung_abwarten, wiederherstellungspunkt, SIGN_OFF_WAIT_SECONDS). Rueckrufe des
Geraetekerns laufen im Geraete-Thread, worker_binden im Hauptthread; die Sperre trennt beide. Kein Rueckruf wirft (V2,
Konformitaetssuite testhilfen/host_konformitaet.py) ausser konfiguration_vorbereiten: scheitert dort das Schreiben der
Markierung (vor dem Speichern), geht der OSError an den Dokumentspeicher, und das Geraet haelt das Dokument zurueck
(Nr. 38)."""
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien, kreisansicht, meldungen
from smartheat_core.meldungen import Meldung
from smartheat_device.commands import Done, Waiting
from smartheat_device.documents import KONFIGURATION
from smartheat_device.hintergrund import Hintergrund
from smartheat_device.host import Anzeige, MeldungSink
from smartheat_runtime import aufraeumen, dokument_config, entitlement
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.soll_speicher import KREIS, SOURCE_PORTAL, raum_soll_von
from smartheat_runtime.status import STATUS_ABGEMELDET
from smartheat_runtime.texts import HA_TEXTS, HostTexts
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

#: Neustart der Laufzeit (neue laufzeitrelevante Konfiguration, sign_off); prozess.reload_anhaengen haengt ihn an.
_NOCH_NICHT_GEMELDET = object()

EV_RELOAD = "geraet_reload"
#: So lange wartet sign_off auf den Status "abgemeldet" (Frist des Befehls, beide Hosts).
SIGN_OFF_WAIT_SECONDS = 24 * 3600


def wiederherstellungspunkt(backup_pfad: Path) -> dict:
    """Der Wiederherstellungspunkt aus der Sicherung (restore_point), das Ergebnis `werte` von sign_off; fehlt er oder
    ist die Sicherung kaputt, {}."""
    backup = dateien.lesen_json(backup_pfad)
    punkt = backup.get("restore_point") if isinstance(backup, dict) else None
    return dict(punkt) if isinstance(punkt, dict) else {}


def abmeldung_abwarten(anlage: str, runtime_status: Callable[[], dict | None], backup_pfad: Path,
                       frist_bis: float) -> Waiting:
    """Der Wartezustand von sign_off (beide Hosts): fertig erst, wenn der Status der Laufzeit "abgemeldet" fuer diese
    Anlage zeigt (ein anderer Status oder eine andere Anlage wartet weiter); zurueckgesetzt ist, wer ohne `grund`
    abgemeldet ist, werte der Wiederherstellungspunkt. frist_bis: Ende des Wartens auf der Uhr des Hosts
    (Uhr + SIGN_OFF_WAIT_SECONDS)."""

    def check() -> Done | None:
        status = runtime_status()
        if not status or status.get("status") != STATUS_ABGEMELDET or status.get("tenant_id") != anlage:
            return None
        return Done({"zurueckgesetzt": status.get("grund") is None, "werte": wiederherstellungspunkt(backup_pfad)})

    return Waiting(check, frist_bis)


class KritischesEnde:
    """Datum, an dem das Geraet unter einer kritischen Mindestversion die urspruenglichen Werte wieder einstellt: der
    lokale Beginn der Frist (Entitlement-Datei je Kreis) plus `GRACE_PERIOD`, als ISO-Datum in der Zeitzone des
    gespeicherten Beginns, bei mehreren Kreisen das fruehere. `None` ohne kritische Mindestversion und ohne lesbaren
    Beginn (Uhr unklar: es wurde keiner gespeichert). Die Dateien werden je `CACHE_SECONDS` hoechstens einmal gelesen
    (eine beschaedigte Datei meldet sich so nur einmal je Fenster im Log); ohne kritische Mindestversion bleibt nichts
    im Zwischenspeicher. Liest nur."""
    CACHE_SECONDS = 30

    def __init__(self, kritisch: Callable[[], bool], pfad: Callable[[int], Path], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._kritisch, self._pfad, self._clock = kritisch, pfad, clock
        self._gelesen: tuple[tuple[int, ...], float, str | None] | None = None

    def __call__(self, kreise: list[int]) -> str | None:
        if not self._kritisch():
            self._gelesen = None
            return None
        schluessel, jetzt = tuple(kreise), self._clock()
        if self._gelesen is not None and self._gelesen[0] == schluessel and jetzt - self._gelesen[1] < self.CACHE_SECONDS:
            return self._gelesen[2]
        enden = []
        for kreis in schluessel:
            beginn = entitlement.load_inactive_since(self._pfad(kreis))
            if beginn is not None:
                enden.append(entitlement.grace_end(beginn).date().isoformat())
        ende = min(enden) if enden else None
        self._gelesen = (schluessel, jetzt, ende)
        return ende


class GeraetHostBasis:
    host: str
    version: str
    #: herkunft eines Bedienwunschs ohne eigene Angabe (HA: Soll-Entity, Gateway: Thermostat)
    wunsch_herkunft: str
    #: Absender und Datentraeger-Traeger der Meldungstexte (Gateway: eigene Texte)
    texts: HostTexts = HA_TEXTS

    def __init__(self) -> None:
        self.device = None  # smartheat_device.device.Device, nach dem Anlegen per verbinden()
        #: Die beim Start einmal gelesene Aufraeum-Markierung, wenn das Zuruecksetzen aussteht
        #: (prozess.aufraeumen_beim_start); der Host setzt sie vor dem Start der Laufzeit. Eine spaeter geschriebene
        #: Markierung kommt immer mit einer Neustart-Anforderung (konfiguration_uebernommen), der naechste Prozess
        #: liest sie.
        self.start_markierung: aufraeumen.Markierung | None = None
        self.letzte_anzeige: Anzeige | None = None
        #: blockierende Host-Aufrufe der Befehle neben dem Geraete-Thread; prozess.beenden stoppt ihn
        self.hintergrund = Hintergrund()
        #: Meldungen der Laufzeit an den Geraetekern (up/notifications); einer je Prozess
        self.notify_sink = MeldungSink(self._melden)
        self._workers: dict[int, RegulationWorker] = {}  # je Kreis der Worker seiner Laufzeit
        #: False im Ruhezustand (IdleBridge): der Worker des Kreises hat keinen Soll-Eingang
        self._soll_eingaenge: dict[int, bool] = {}
        self._lock = threading.Lock()  # binden (Hauptthread) gegen Rueckrufe (Geraete-Thread)
        self._neustart = False
        self._ursprungswerte: dict[int, dict] = {}  # je Kreis; Hauptthread schreibt, Geraet liest
        self._raum_soll: dict[int, float] = {}  # je Kreis die letzte Bedienung, bevor sein Worker gebunden ist
        #: Die Laufzeit der Anlage (kreise.Laufzeit): entscheidet je Konfigurationsaenderung, ob einzelne Kreise
        #: wieder anlaufen oder der Prozess neu startet. Ihre blockierende Wiederaufnahme laeuft in diesem eigenen
        #: Thread, nie im Geraete-Thread und nie hinter den Befehlen.
        self._laufzeit = None
        self._konfig_hintergrund = Hintergrund()
        #: Datum der urspruenglichen Werte unter kritischer Mindestversion (Meldung und lokales Status-Ereignis)
        self._kritisches_ende = KritischesEnde(self.update_noetig_kritisch, self.entitlement_pfad)
        #: Die zuletzt an den Geraetekern gegebene Meldung update_noetig (None = zu); nur der Geraete-Thread.
        #: Nach einem Prozessstart noch unbekannt: die erste Anzeige meldet immer, auch die Ruecknahme, weil eine
        #: vor dem Neustart offene Meldung beim Server sonst stehen bliebe.
        self._update_noetig_gemeldet: Meldung | None | object = _NOCH_NICHT_GEMELDET

    def entitlement_pfad(self, kreis: int) -> Path:
        """Die Entitlement-Datei des Kreises (Host): ihr Beginn der Frist gibt das Datum der urspruenglichen Werte."""
        raise NotImplementedError

    def aufraeumen_pfad(self) -> Path:
        """Wohin konfiguration_vorbereiten die Markierung aufraeumen.json schreibt (Host)."""
        raise NotImplementedError

    def abgemeldet_pfad(self) -> Path:
        """Wo sign_off die Abmelde-Markierung abgemeldet.json schreibt (Host); konfiguration_uebernommen hebt sie
        auf."""
        raise NotImplementedError

    # --- Verdrahtung ---

    def verbinden(self, device) -> None:
        self.device = device

    def worker_binden(self, worker: RegulationWorker, *, kreis: int = KREIS, soll_eingang: bool = True) -> None:
        """Bindet den Worker des Kreises `kreis` (ein neuer ersetzt den alten, so nach einer Wiederaufnahme).
        soll_eingang=False im Ruhezustand (IdleBridge, kein Trigger-Eingang): Bedienungen gehen nicht an den Worker
        (sonst je Dokument "Kein Handler ... verworfen"); sie bleiben im Geraetekern gespeichert und gelten beim
        naechsten Start mit Regelung. Neustart und letzte Bedienung des Kreises aus der Zeit vor dem Binden werden
        nachgestellt."""
        with self._lock:
            self._workers[kreis] = worker
            self._soll_eingaenge[kreis] = soll_eingang
            gemerkt = self._raum_soll.pop(kreis, None)
            if gemerkt is not None and soll_eingang:
                worker.post(Event(EV_TARGET_INPUT, {"value": gemerkt, "source": SOURCE_PORTAL}))
            if self._neustart:
                worker.post(Event(EV_RELOAD))

    def laufzeit_binden(self, laufzeit) -> None:
        """Die Laufzeit der Anlage, der konfiguration_uebernommen jede neue Konfiguration vorlegt."""
        self._laufzeit = laufzeit

    def ursprungswerte_merken(self, werte: dict, kreis: int = KREIS) -> None:
        with self._lock:
            self._ursprungswerte[kreis] = dict(werte)

    def ursprungswerte(self) -> dict:
        """Gemeldet wird nur Kreis 1: der Statuskoerper hat eine Ursprungswerte-Tabelle je Anlage; die Namen anderer
        Kreise wuerden ihr sonst falsch zugeordnet."""
        with self._lock:
            return dict(self._ursprungswerte.get(KREIS, {}))

    def neustart_anfordern(self) -> None:
        """Neustart des Prozesses: EV_RELOAD an jeden gebundenen Worker, spaeter gebundene bekommen ihn beim Binden."""
        with self._lock:
            self._neustart = True
            for worker in self._workers.values():
                worker.post(Event(EV_RELOAD))

    def _melden(self, key: str, meldung: Meldung | None, *, offen: bool) -> None:
        if self.device is not None:
            self.device.meldung(key, meldung, offen=offen)

    # --- Sicht der Laufzeit ---

    def konfiguration(self) -> dict | None:
        return self.device.konfiguration() if self.device is not None else None

    def konfiguration_abgelehnt(self) -> bool:
        """Hat der Geraetekern die zuletzt gelieferte Konfiguration abgelehnt (die Laufzeit regelt mit der alten)?"""
        return self.device is not None and self.device.documents.ablehnung(KONFIGURATION) is not None

    def bedienung(self) -> tuple[dict | None, bool]:
        if self.device is None:
            return None, False
        doc = self.device.documents.aktuell("bedienung")
        return self.device.bedienung(), bool(doc is not None and doc.dirty)

    def laufzeit_konfiguration(self) -> tuple[dict, bool]:
        """(Konfiguration fuer die Laufzeit, im Modus abgemeldet starten?). Steht nach einer
        leeren Konfiguration das Zuruecksetzen aus (start_markierung) und ist die gespeicherte Konfiguration noch nicht
        eingerichtet, die alte Konfiguration aus der Markierung; sonst die gespeicherte (leer: {})."""
        gespeichert = self.konfiguration()
        alt = self.start_markierung.konfiguration if self.start_markierung is not None else None
        if alt is not None and not dokument_config.eingerichtet(gespeichert):
            return dict(alt), True
        return dict(gespeichert or {}), False

    def ursprungswerte_server(self, konfiguration: dict, leer_abmelden: bool, kreis: int | None = None) -> dict:
        """Die Ursprungswerte, die der Server fuer die Wiederherstellung kennt; konfiguration und leer_abmelden stammen
        aus laufzeit_konfiguration. Steht das Zuruecksetzen nach einer leeren Konfiguration aus, gelten die Werte der
        Markierung -- auch wenn inzwischen eine neue Einrichtung (womoeglich einer anderen Anlage) eingetroffen ist
        (die Markierung hat Vorrang); sonst die aktuelle Konfiguration, solange sie eingerichtet ist."""
        if leer_abmelden:
            return dokument_config.ursprungswerte(konfiguration, kreis)
        aktuell = self.konfiguration()
        return dokument_config.ursprungswerte(aktuell if dokument_config.eingerichtet(aktuell) else konfiguration, kreis)

    def abo_status(self, kreis: int | None = None) -> str:
        return self.device.abo_status(kreis) if self.device is not None else dokument_config.abo_status(None)

    def update_noetig_kritisch(self) -> bool:
        return self.device.update_noetig_kritisch() if self.device is not None else False

    def kritisches_ende(self, kreise: list[int]) -> str | None:
        """Datum der urspruenglichen Werte unter einer kritischen Mindestversion (ISO) oder None (`KritischesEnde`)."""
        return self._kritisches_ende(kreise)

    def kreis_nummern(self) -> list[int]:
        """Die Kreisnummern der Konfiguration (leer ohne Konfiguration)."""
        return [k for eintrag in kreisansicht.kreise(self.konfiguration() or {})
                if isinstance(k := eintrag.get("kreis"), int) and not isinstance(k, bool)]

    def update_noetig_melden(self, anzeige: Anzeige) -> None:
        """Unter dem Zustand update_noetig geht die rote Meldung ueber den Geraetekern an den Server (Portal, Push und
        Mail an Eigentuemer und Bedienende): mit dem Datum der urspruenglichen Werte, ohne bekannten Beginn (Uhr
        unklar) die Fassung ohne Datum, nie beide. Gemeldet wird nur bei Aenderung; verlaesst das Geraet den Zustand
        (Update eingespielt), nimmt es die Meldung zurueck; die erste Anzeige nach einem Prozessstart meldet den Stand
        immer, auch als Ruecknahme (der Server schliesst Geraete-Meldungen nicht von selbst). Beide Hosts rufen es mit jeder Anzeige; Geraete-Thread."""
        if self.device is None:
            return
        meldung = None
        if anzeige.zustand == "update_noetig":
            ende = self.kritisches_ende(self.kreis_nummern() or [KREIS])
            meldung = (meldungen.neu(meldungen.PREFIX + "update_noetig", ende=ende) if ende is not None
                       else meldungen.neu(meldungen.PREFIX + "update_noetig_unbekannt"))
        if meldung == self._update_noetig_gemeldet:
            return
        self._melden("update_noetig", meldung, offen=meldung is not None)
        self._update_noetig_gemeldet = meldung

    def update_noetig_geaendert(self, kritisch: bool) -> None:
        """Die Kreise nehmen den Notbetrieb der kritischen Mindestversion auf bzw. verlassen ihn ohne Prozessneustart
        (im Hintergrund wie die Konfigurationsaenderung); ohne Laufzeit oder bei einem Fehler startet der Prozess neu."""
        laufzeit = self._laufzeit
        if laufzeit is None:
            self.neustart_anfordern()
            return
        self._konfig_hintergrund.submit(lambda: self._laufzeit_kritisch(laufzeit))

    def _laufzeit_kritisch(self, laufzeit) -> None:
        try:
            laufzeit.kritisch_geaendert()
        except Exception:
            logger.exception("Die Laufzeit konnte die kritische Mindestversion nicht anwenden, Neustart")
            self.neustart_anfordern()

    def wunsch(self, wert: float, herkunft: str | None = None, durch: str | None = None, kreis: int = KREIS) -> float:
        """Bedienwunsch fuer den Kreis `kreis` (HA: Soll-Entity oder smartheat_wunsch; Gateway: Thermostat): begrenzt
        vom Geraetekern, der ihn sendet; ohne Geraet unveraendert."""
        if self.device is None:
            return wert
        return self.device.wunsch(wert, herkunft=herkunft or self.wunsch_herkunft, durch=durch, kreis=kreis)

    def soll_quelle_aus(self, aus: bool, wirksam: float | None) -> None:
        if self.device is not None:
            self.device.soll_quelle_aus(aus, wirksam)

    # --- Rueckrufe des Geraetekerns (DeviceHost) ---

    def konfiguration_vorbereiten(self, alt: dict | None, neu: dict) -> None:
        if dokument_config.eingerichtet(alt) and not dokument_config.eingerichtet(neu):
            assert alt is not None
            aufraeumen.markieren(self.aufraeumen_pfad(), alt)  # vor dem Speichern

    def entfernen_wahl(self) -> str | None:
        """"freigeben", solange die Abmelde-Markierung der aktuellen Anlage den Wunsch traegt, das Geraet aus dem Konto zu
        loesen und der Server ihn noch nicht bestaetigt hat; sonst None."""
        if dokument_config.entfernen_gewuenscht(self.abgemeldet_pfad(), self.konfiguration()):
            return "freigeben"
        return None

    def entfernen_erledigt(self) -> None:
        """Der Server hat die Wahl bestaetigt: nur den Wunsch loeschen, die Abmelde-Markierung bleibt."""
        anlage = (self.konfiguration() or {}).get("anlage")
        if anlage is not None:
            dokument_config.entfernen_erledigt(self.abgemeldet_pfad(), anlage)

    def konfiguration_uebernommen(self, alt: dict | None, neu: dict) -> None:
        """Neustart bei laufzeitrelevanter Aenderung und wenn eine neue, nicht leere Konfiguration derselben Anlage die
        Abmelde-Markierung aufhebt (das Geraet folgt dem Server und verlaesst "abgemeldet"). Eine leere
        Konfiguration oder eine andere Anlage laesst die Markierung stehen; dieselbe Version erneut ruft hier nicht.
        Kein Neustart, solange weder die alte noch die neue Konfiguration eingerichtet ist: der Ruhezustand
        "nicht eingerichtet" liest kein Laufzeitfeld, und die erste leere Konfiguration nach dem Erstkontakt risse
        sonst einen laufenden Befehl mit."""
        aufgehoben = (dokument_config.eingerichtet(neu)
                      and dokument_config.abmeldung_aufheben(self.abgemeldet_pfad(), neu["anlage"]))
        einrichtung_beteiligt = dokument_config.eingerichtet(alt) or dokument_config.eingerichtet(neu)
        if aufgehoben:
            self.neustart_anfordern()
        elif not einrichtung_beteiligt:
            return
        elif self._laufzeit is None:
            if dokument_config.laufzeit_relevant(alt, neu):
                self.neustart_anfordern()
        else:
            laufzeit = self._laufzeit
            try:
                sofort = laufzeit.prozessneustart_noetig(alt, neu)
            except Exception:
                logger.exception("Die Laufzeit konnte die neue Konfiguration nicht pruefen, Neustart")
                sofort = True
            if sofort:
                self.neustart_anfordern()
            else:
                self._konfig_hintergrund.submit(lambda: self._laufzeit_fragen(laufzeit, alt, neu))

    def _laufzeit_fragen(self, laufzeit, alt: dict | None, neu: dict) -> None:
        """Die Laufzeit nimmt Kreise mit wieder aktivem Abo oder geaenderter Bindung allein wieder auf; verlangt sie
        den Prozessneustart oder scheitert sie, startet der Prozess neu (sicherer Weg)."""
        try:
            neustart = laufzeit.konfiguration_geaendert(alt, neu)
        except Exception:
            logger.exception("Die Laufzeit konnte die neue Konfiguration nicht anwenden, Neustart")
            neustart = True
        if neustart:
            self.neustart_anfordern()

    def bedienung_uebernommen(self, inhalt: dict) -> None:
        """Je Kreis der Raum-Soll seines Eintrags: an den Worker des Kreises, vor dem Binden gemerkt. Ein Kreis, den die
        Bedienung nicht nennt oder dessen Wert keine Zahl ist, bleibt unberuehrt."""
        with self._lock:
            eintraege = inhalt.get("kreise") if isinstance(inhalt, dict) else None
            genannt = [e["kreis"] for e in eintraege if isinstance(e, dict) and isinstance(e.get("kreis"), int)] \
                if isinstance(eintraege, list) else []
            kreise = {*self._workers, *self._raum_soll, *genannt}
            for kreis in sorted(kreise):
                value = raum_soll_von(inhalt, kreis)
                if not isinstance(value, int | float) or isinstance(value, bool):
                    continue
                worker = self._workers.get(kreis)
                if worker is None:
                    self._raum_soll[kreis] = float(value)  # worker_binden stellt die letzte nach
                elif self._soll_eingaenge.get(kreis, True):
                    worker.post(Event(EV_TARGET_INPUT, {"value": float(value), "source": SOURCE_PORTAL}))
