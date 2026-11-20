"""Soll-Eingang, hostneutral: Bedienung vom Server (portal), Eingabe an der Soll-Quelle
(thermostat; HA: Soll-Entity) und lokale Eingabe (lokal; HA: Ereignis smartheat_wunsch) gehen als EV_TARGET_INPUT in den
Worker. Der Soll-Speicher schreibt, der Debouncer beendet die Eingabe (Gateway 10 s; HA 0 s, dort entprellt der
HA-Trigger mit for: 10 s). Erst dann geht ein Wunsch einmal an den Geraetekern (begrenzt auf den Bedienbereich); der
begrenzte Wert gilt sofort und wird an die Soll-Quelle zurueckgeschrieben, danach EV_LOCAL_CHECK(room_target_fired).
Eine lokale Eingabe gleicht die Soll-Quelle zusaetzlich an. Soll-Quelle aus (Modus off oder ein Soll bis
FROSTSCHUTZ_BIS; ein unbrauchbarer Wert der Quelle setzt oder beendet das nicht): kein Wunsch, auch eine lokale
Eingabe wird verworfen (5c F/Q3), Meldung ueber soll_quelle_aus, eine neue Bedienung wird nicht geschrieben; wieder an:
das geltende Soll geht an die Quelle, sofern sie etwas anderes zeigt (Echo-Fenster, offener Schreibbefehl). Die
Quelle selbst (Zigbee, HA-Entity) liefert der Host ueber SollQuelle.

Uebersteuerung (mit `UebersteuerungsWeg`, nur mit der Faehigkeit programm): in der kuehlen Phase des Plans (Soll-Art
Absenkung oder uebersteuert, mit naechstem Wechsel) ist eine Eingabe am Thermostat oder an der lokalen Zahl keine
Aenderung des Wunsches, sondern eine Uebersteuerung bis zum naechsten Wechsel (dem Aufheizbeginn). Sie gilt sofort und
lokal (Soll-Speicher), wird auf 16 °C (Absenk-Untergrenze) bis Bedienbereich-Maximum aus safety.py begrenzt und geht nach
dem Entprellen einmal als Wunsch mit programm an den Server; scheitert der Versand, gilt sie trotzdem lokal. In jeder
anderen Lage (warm, Aufheizen, ohne Plan, ohne Faehigkeit) bleibt es ein Wunsch wie bisher. Die Bedienung des Servers
bleibt Sache des Servers: sie beendet die lokale Uebersteuerung.

Leerstand und Away (mit `UebersteuerungsWeg.betriebsart` und `beenden`, nur mit der Faehigkeit programm): eine Eingabe
am Thermostat in Away beendet Away. Im Leerstand zaehlt `LeerstandZaehler` die Aenderungen des Kunden in 24 h: die erste
und zweite ist eine Uebersteuerung fuer 4 h (nach dem Plan hoechstens bis zum Ende des Leerstands), die dritte beendet ihn.
Gezaehlt wird nur, was `TargetStore.thermostat_pruefen` als Eingabe des Kunden durchlaesst (kein Echo eigener
Schreibvorgaenge, keine Wiederholung der Anzeige), einmal je entprellter Drehung. Das Ende geht nach dem Entprellen mit
dem letzten Wert als ein Wunsch an den Server (`programm` mit Betriebsart null; war die Phase danach kuehl, mit der
Uebersteuerung bis zum Aufheizbeginn). Die lokale Zahl und der Wunsch des Portals beenden nichts.

Anzeige des Thermostats: eine Meldung, die das Thermostat schon zeigt, ist nie eine Aenderung des Kunden. Dazu merkt der
Speicher die zuletzt angenommene oder geschriebene Anzeige. Laeuft eine Uebersteuerung des Leerstands ab, schreibt die
Laufzeit die Leerstandstemperatur an die Soll-Quelle zurueck (ein eigener Schreibvorgang mit der alten Anzeige als
veraltetem Wert); beim Angleichen (Wiedereinschalten) gilt die laufende Uebersteuerung vor dem Wunsch. Die
Uebersteuerung im Leerstand reicht hoechstens bis zu dessen Ende."""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from smartheat_core.plan import SOLL_ART_ABSENKUNG, SOLL_ART_LEERSTAND, SOLL_ART_UEBERSTEUERT, SollErgebnis
from smartheat_core.safety import ABSENK_UNTERGRENZE_C, BEDIENBEREICH_MAX_C
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_runtime.debounce import Debouncer
from smartheat_runtime.leerstand_zaehler import BEENDEN_AB, UEBERSTEUERUNG_DAUER
from smartheat_runtime.plan_quelle import Betriebsart
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible
from smartheat_runtime.runtime import EV_LOCAL_CHECK
from smartheat_runtime.soll_speicher import SOURCE_LOKAL, SOURCE_PORTAL, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

EV_TARGET_INPUT = "shg_target_input"
EV_SOLL_MODUS = "shg_soll_modus"
#: Meldet der Laufzeit jeden Wechsel von Soll-Quelle aus/an ({"aus": bool}); der Kaelteschutz rechnet dann ohne Soll.
EV_SOLL_QUELLE = "shg_soll_quelle"
#: Ende der Regelung (Abo-Ende, ausser Betrieb): die Soll-Quelle zeigt danach den gemerkten Wunsch, nie weniger.
EV_SOLL_ENDE = "shg_soll_ende"
#: Das Soll-Ende ist abgearbeitet (geschrieben, nicht noetig oder nicht Sache der Regelung): die Laufzeit loescht
#: den Merker "Soll-Ende offen". Ohne diese Meldung bleibt er offen und der naechste Start holt es nach.
EV_SOLL_ENDE_ERLEDIGT = "shg_soll_ende_erledigt"
#: Wiederholung eines Soll-Endes, das noch nicht gelang (Anzeige unbekannt oder Schreiben gescheitert).
EV_SOLL_ENDE_WIEDERHOLEN = "shg_soll_ende_wiederholen"
SOLL_ENDE_WIEDERHOLUNG_S = 60.0
#: Das Ende einer Uebersteuerung (Wanduhr des Speichers): danach geht die Leerstandstemperatur an die Soll-Quelle zurueck.
EV_UEB_ABLAUF = "shg_ueb_ablauf"
UEB_ABLAUF_PUFFER_S = 1.0
DEBOUNCE_KIND = "shg_target_debounce"
#: Ein Soll bis zu diesem Wert ist die Frostschutz-Stellung der Soll-Quelle, kein Bedienwunsch.
FROSTSCHUTZ_BIS = 7.0
MODUS_AUS = "off"
BETRIEBSART_LEERSTAND = "leerstand"
_ZEIT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class UebersteuerungsWeg:
    """Verdrahtung der Uebersteuerung durch den Host. `lage`: das geltende Soll des Kreises (None: unbekannt);
    `senden(temp, bis, herkunft, durch)`: die Uebersteuerung als Wunsch mit programm an den Geraetekern (ValueError, wenn
    er sie ablehnt); `moeglich`: das Geraet hat die Faehigkeit programm."""

    lage: Callable[[], SollErgebnis | None]
    senden: Callable[[float, str, str | None, str | None], None]
    moeglich: Callable[[], bool]
    #: Laeuft Leerstand oder Away (Betriebsart des Plans, auch unter einer Uebersteuerung)? None: weder noch.
    betriebsart: Callable[[], Betriebsart | None] | None = None
    #: `beenden(art, raum_soll, uebersteuerung, herkunft, durch)`: ein Wunsch, der die Betriebsart beendet und den Wert der
    #: Phase danach traegt (Wunsch oder Uebersteuerung); Rueckgabe: der begrenzte Wunsch oder None.
    beenden: Callable[[str, float | None, dict | None, str | None, str | None], float | None] | None = None


class SollQuelle(Protocol):
    def schreiben(self, value: float) -> bool:
        """Soll an die Quelle schreiben; False, wenn sie nicht schreibbar ist (dann kein Echo-Fenster)."""
        ...

    def angezeigt(self) -> float | None:
        """Was die Quelle gerade zeigt; None = unbekannt."""
        ...

    def aus(self) -> bool:
        """Die Quelle zeigt „aus“ (Modus off): sie gehoert dem Kunden."""
        ...


def ende_schreiben(wunsch: float, quelle: SollQuelle, *,
                   geschrieben: Callable[[float], None] | None = None) -> bool:
    """Soll-Ende an der Quelle: sie zeigt danach den gemerkten Wunsch, nie weniger. True = erledigt (nichts zu tun oder
    geschrieben), False = spaeter erneut (Anzeige unbekannt oder Schreiben gescheitert). Bei unbekannter Anzeige wird
    nicht geschrieben: ein hoeherer Wert des Kunden koennte dort stehen. Zeigt die Quelle gleich viel oder mehr, bleibt
    sie; bei „aus“ oder einer Frostschutz-Stellung gehoert sie dem Kunden. `geschrieben` bekommt nach einem Schreiben
    den vorher angezeigten Wert."""
    if quelle.aus():
        return True
    angezeigt = quelle.angezeigt()
    if angezeigt is None:
        return False
    if angezeigt >= wunsch or angezeigt <= FROSTSCHUTZ_BIS:
        return True
    if not quelle.schreiben(wunsch):
        return False
    if geschrieben is not None:
        geschrieben(angezeigt)
    return True


class SollEingang:
    """Nur im Worker-Thread."""

    def __init__(self, worker: RegulationWorker, store: TargetStore, quelle: SollQuelle | None, *,
                 wunsch: Callable[[float, str | None, str | None], float],
                 soll_quelle_aus: Callable[[bool, float | None], None], entprellen_s: float,
                 uebersteuerung: UebersteuerungsWeg | None = None) -> None:
        self._worker, self._store, self._quelle = worker, store, quelle
        self._wunsch, self._soll_quelle_aus = wunsch, soll_quelle_aus
        self._uebersteuerung = uebersteuerung
        self._aus: set[str] = set()  # Gruende: "modus", "frost"
        self._wunsch_offen = False
        self._lokal_offen = False
        self._ueb_offen = False  # eine Uebersteuerung ist gesetzt, aber noch nicht entprellt und gesendet
        self._ueb_roh: float | None = None  # der unbegrenzte Wert der Eingabe
        self._ueb_lokal = False  # die Eingabe kam von der lokalen Zahl (die Quelle muss angeglichen werden)
        self._vor_ueb: dict | None = None  # Uebersteuerung vor der ersten offenen Eingabe
        # Eine Aenderung beendet Leerstand oder Away; sie wird nach dem Entprellen mit dem letzten Wert gesendet
        self._beenden_offen: Betriebsart | None = None
        self._beenden_wert: float | None = None
        self._herkunft: tuple[str | None, str | None] = (None, None)
        self._vor_wunsch: tuple[float, str] | None = None  # Soll und Quelle vor dem ersten offenen Wunsch
        self._unbrauchbar_gemeldet: set[str] = set()  # Fehlerarten mit WARNING seit dem letzten brauchbaren Wert
        self._ende_wartet = False  # ein Soll-Ende kam bei offenem Wunsch: nach dem Entprellen nachholen
        self._ende_geplant = False  # eine Wiederholung des Soll-Endes ist eingeplant (eine Kette)
        self._ablauf_geplant = False  # das Ende der Uebersteuerung ist eingeplant (eine Kette)
        self._debouncer = Debouncer(worker, DEBOUNCE_KIND, entprellen_s, self._entprellt)
        worker.register(EV_TARGET_INPUT, self._on_target_input)
        worker.register(EV_SOLL_MODUS, self._on_mode)
        worker.register(EV_SOLL_ENDE, self._on_ende)
        worker.register(EV_SOLL_ENDE_WIEDERHOLEN, self._on_ende_wiederholen)
        worker.register(EV_UEB_ABLAUF, self._on_ueb_ablauf)
        self._ablauf_planen()  # eine Uebersteuerung, die den Neustart ueberlebt hat

    @property
    def aus(self) -> bool:
        """Soll-Quelle aus (Modus off oder Frostschutz-Stellung): kein Wunsch, keine Bedienung an die Quelle."""
        return bool(self._aus)

    def _schreiben(self, value: float, veraltet: float | None = None) -> None:
        if self._quelle is not None and self._quelle.schreiben(value):
            self._store.note_own_write(veraltet=veraltet, geschrieben=value)

    def _setze_aus(self, grund: str, aus: bool) -> None:
        vorher = bool(self._aus)
        war_aus = grund in self._aus
        if aus:
            self._aus.add(grund)
        else:
            self._aus.discard(grund)
        if bool(self._aus) != vorher:
            if self._aus:
                self._wunsch_verwerfen()  # Soll-Quelle aus ist kein Wunsch: auch ein offener nicht
            self._worker.post(Event(EV_SOLL_QUELLE, {"aus": bool(self._aus)}))
            self._soll_quelle_aus(bool(self._aus), self._store.value if self._aus else None)
        if grund == "modus" and war_aus and not self._aus:
            self._wieder_an()

    def _wieder_an(self) -> None:
        """Modus off -> an (ohne Frostschutz): das geltende Soll an die Quelle. Die naechsten Meldungen mit dem alten
        Soll (cache_state) fallen in Echo-Fenster und offenen Schreibbefehl statt ein Wunsch zu werden; die Quelle zeigt
        wieder das Soll (auch nach einem verworfenen Drehwert). Zeigt sie das Soll schon, nichts."""
        if self._quelle is None:
            return
        angezeigt = self._quelle.angezeigt()
        soll = self._store.anzeige()  # eine laufende Uebersteuerung gilt vor dem Wunsch
        if angezeigt == soll:
            return
        self._schreiben(soll, veraltet=angezeigt)

    def _ablauf_planen(self) -> None:
        """Plant das Ende der laufenden Uebersteuerung ein (eine Kette; ohne Uebersteuerung nichts)."""
        ueb = self._store.uebersteuerung()
        if self._ablauf_geplant or ueb is None:
            return
        try:
            rest = (datetime.fromisoformat(ueb["bis"]) - self._store.jetzt()).total_seconds()
        except (ValueError, TypeError):
            return
        self._ablauf_geplant = True
        self._worker.schedule(max(rest, 0.0) + UEB_ABLAUF_PUFFER_S, Event(EV_UEB_ABLAUF))

    def _on_ueb_ablauf(self, event: Event) -> None:
        """Die Uebersteuerung ist abgelaufen: im Leerstand zeigt die Soll-Quelle wieder die Leerstandstemperatur. Ohne das
        zeigte das Thermostat weiter den Wert des Kunden, und jede Wiederholung der Meldung sah aus wie eine neue Aenderung.
        Laeuft noch eine (neu gesetzt), wird erneut eingeplant; bei Soll-Quelle aus oder offener Eingabe des Kunden
        bleibt die Quelle, wie sie ist."""
        self._ablauf_geplant = False
        if self._store.uebersteuerung() is not None:
            self._ablauf_planen()
            return
        weg = self._uebersteuerung
        if (self._quelle is None or weg is None or self._aus or self._wunsch_offen or self._ueb_offen
                or self._beenden_offen is not None):
            return
        lage = weg.lage()
        if lage is None or lage.soll_art != SOLL_ART_LEERSTAND or lage.soll is None:
            return
        angezeigt = self._quelle.angezeigt()
        if angezeigt == lage.soll:
            return
        self._schreiben(lage.soll, veraltet=angezeigt if angezeigt is not None else self._store.angezeigt_zuletzt)

    def _wunsch_verwerfen(self) -> None:
        if self._wunsch_offen and self._vor_wunsch is not None:
            self._store.wiederherstellen(*self._vor_wunsch)
        if self._ueb_offen:
            if self._vor_ueb is not None:
                self._store.uebersteuerung_setzen(self._vor_ueb["temp"], self._vor_ueb["bis"])
            else:
                self._store.uebersteuerung_aufheben()
        self._ueb_offen = False
        self._vor_ueb = None
        self._beenden_offen, self._beenden_wert = None, None
        self._wunsch_offen = self._lokal_offen = False
        self._vor_wunsch = None
        if self._ende_wartet:
            self._ende_erledigt()  # die Quelle ist aus: sie gehoert dem Kunden, das Soll-Ende ist nicht mehr Sache der Regelung

    def _on_ende(self, event: Event) -> None:
        """Ende der Regelung: die Soll-Quelle zeigt den gemerkten Wunsch (`TargetStore.value`), damit keine Betriebsart
        ihren Wert dort stehen laesst (`ende_schreiben`: nie absenken, nicht bei unbekannter Anzeige). Nichts zu tun
        bei Soll-Quelle aus (Modus off, Frostschutz-Stellung: die Quelle gehoert dem Kunden) und ohne Quelle. Bei einem
        offenen, noch nicht entprellten Wunsch (der Kunde dreht gerade; sein Wert gewinnt) wird es nach dem Entprellen
        nachgeholt. Erledigt meldet es EV_SOLL_ENDE_ERLEDIGT; bis dahin wiederholt es sich im Takt
        SOLL_ENDE_WIEDERHOLUNG_S."""
        if self._quelle is None or self._aus:
            self._ende_erledigt()
        elif self._wunsch_offen:
            self._ende_wartet = True
        else:
            self._ende_wartet = False
            self._ende_versuchen()

    def _on_ende_wiederholen(self, event: Event) -> None:
        self._ende_geplant = False
        self._on_ende(event)

    def _ende_versuchen(self) -> None:
        assert self._quelle is not None
        if ende_schreiben(self._store.value, self._quelle,
                          geschrieben=lambda veraltet: self._store.note_own_write(veraltet=veraltet)):
            self._ende_erledigt()
        elif not self._ende_geplant:
            self._ende_geplant = True
            self._worker.schedule(SOLL_ENDE_WIEDERHOLUNG_S, Event(EV_SOLL_ENDE_WIEDERHOLEN))

    def _ende_erledigt(self) -> None:
        self._ende_wartet = False
        self._worker.post(Event(EV_SOLL_ENDE_ERLEDIGT))

    def _on_mode(self, event: Event) -> None:
        """Ein Modus ohne Text (Zigbee2MQTT `system_mode: null`) ist keine Aussage: er schaltet weder an noch aus (fail
        closed; sonst loeste er nach „aus“ ein „wieder an“ aus)."""
        mode = event.data.get("mode")
        if not isinstance(mode, str):
            return
        self._setze_aus("modus", mode == MODUS_AUS)

    def _on_target_input(self, event: Event) -> None:
        value, source = event.data["value"], event.data["source"]
        herkunft, durch = event.data.get("herkunft"), event.data.get("durch")
        if source == SOURCE_PORTAL:
            vorher = self._store.value
            if not self._store.apply_portal(value):
                return
            self._wunsch_offen = self._lokal_offen = False  # die Bedienung des Servers gilt
            self._vor_wunsch = None
            self._ueb_offen, self._vor_ueb = False, None
            self._beenden_offen, self._beenden_wert = None, None
            if not self._aus and self._store.value != vorher:
                self._schreiben(self._store.value)
        elif source == SOURCE_LOKAL:
            if self._aus:
                logger.info("Lokaler Wunsch verworfen: die Soll-Quelle ist aus (Spec 5c F/Q3)")
                return
            # In Leerstand und Away merkt die lokale Zahl nur den Wunsch fuer danach, auch bei laufender Uebersteuerung
            bis = None if self._betriebsart_laeuft() else self._kuehl_bis()
            if bis is not None:
                if self._ueb_eingabe(value, bis, herkunft, durch, thermostat=False):
                    self._debouncer.poke()
                return
            if not self._store.apply_lokal(value):
                return
            self._wunsch_offen = self._lokal_offen = True
            self._vor_wunsch = None
            self._herkunft = (herkunft, durch)
        else:
            if ist_endliche_zahl(value) and value <= FROSTSCHUTZ_BIS:
                self._unbrauchbar_gemeldet.clear()
                self._setze_aus("frost", True)
                return
            if not is_plausible(value, ROOM_TEMP_RANGE):
                # Unbrauchbar (nicht endlich, Text, bool, None, ueber dem Raumbereich): aendert die Frostschutz-Stellung
                # nicht, ist kein Wunsch; ein einziger solcher Wert hebt die Stellung nicht auf.
                self._unbrauchbar_melden(value)
                return
            self._unbrauchbar_gemeldet.clear()
            self._setze_aus("frost", False)
            if self._aus:
                return
            if self._betriebsart_eingabe(value, herkunft, durch):
                return
            bis = self._kuehl_bis()
            if bis is not None:
                if self._ueb_eingabe(value, bis, herkunft, durch, thermostat=True):
                    self._debouncer.poke()
                return
            vor_wunsch = (self._store.value, self._store.source)
            if not self._store.apply_thermostat(value):
                return
            if not self._wunsch_offen:
                self._vor_wunsch = vor_wunsch
            self._wunsch_offen = True
            self._lokal_offen = False
            self._herkunft = (herkunft, durch)
        self._debouncer.poke()

    def _betriebsart_eingabe(self, value: object, herkunft: str | None, durch: str | None) -> bool:
        """Eine Eingabe am Thermostat, solange Leerstand oder Away laeuft (nur mit der Faehigkeit programm; die lokale Zahl
        merkt dort nur den Wunsch). Away endet mit jeder Aenderung. Im Leerstand ist die erste und zweite Aenderung im
        24-h-Fenster eine Uebersteuerung fuer UEBERSTEUERUNG_DAUER, die dritte beendet ihn. Gezaehlt wird, was der Speicher
        als Eingabe des Kunden durchlaesst (kein Echo eigener Schreibvorgaenge), einmal je entprellter Drehung. True: die
        Eingabe ist hier behandelt (auch verworfen), False: sie laeuft den gewohnten Weg."""
        weg = self._uebersteuerung
        if weg is None or weg.betriebsart is None or weg.beenden is None or not weg.moeglich():
            return False
        if self._beenden_offen is not None:  # dieselbe Drehung: der letzte Wert gilt
            neu = self._store.thermostat_pruefen(value)
            if neu is not None:
                self._beenden_wert, self._herkunft = neu, (herkunft, durch)
                self._debouncer.poke()
            return True
        if self._wunsch_offen:
            return False
        lage = weg.betriebsart()
        if lage is None:
            return False
        aktuell = self._store.uebersteuerung()
        neu = self._store.thermostat_pruefen(value, angezeigt=None if aktuell is None else aktuell["temp"])
        if neu is None:
            return True
        self._herkunft = (herkunft, durch)
        beendet = lage.art != BETRIEBSART_LEERSTAND
        if lage.art == BETRIEBSART_LEERSTAND and not self._ueb_offen:
            beendet = self._store.leerstand_zaehler.eintragen() >= BEENDEN_AB
        if beendet:
            self._beenden_offen, self._beenden_wert = lage, neu
        else:
            ende = self._store.jetzt() + UEBERSTEUERUNG_DAUER
            if lage.bis is not None:  # der Plan verwirft eine Uebersteuerung, die ueber das Ende des Leerstands reicht
                ende = min(ende, lage.bis)
            self._ueb_eingabe(neu, ende.astimezone(UTC).strftime(_ZEIT_FORMAT), herkunft, durch, thermostat=True,
                              geprueft=neu)
            self._ablauf_planen()
        self._debouncer.poke()
        return True

    def _beenden_senden(self) -> None:
        """Nach dem Entprellen: die Betriebsart endet, der Wert folgt der Phase danach (warm oder ohne Plan: Wunsch, kuehl:
        Uebersteuerung bis zum Aufheizbeginn). Lehnt der Geraetekern ab, gilt der Wert lokal trotzdem."""
        lage, wert = self._beenden_offen, self._beenden_wert
        self._beenden_offen, self._beenden_wert = None, None
        weg = self._uebersteuerung
        if lage is None or wert is None or weg is None or weg.beenden is None:
            return
        herkunft, durch = self._herkunft
        danach = lage.danach
        if lage.art == BETRIEBSART_LEERSTAND:
            self._store.leerstand_zaehler.leeren()
        try:
            if danach is not None and danach.soll_art in (SOLL_ART_ABSENKUNG, SOLL_ART_UEBERSTEUERT) \
                    and danach.naechster_wechsel is not None:
                bis = danach.naechster_wechsel.astimezone(UTC).strftime(_ZEIT_FORMAT)
                temp = min(max(wert, ABSENK_UNTERGRENZE_C), BEDIENBEREICH_MAX_C)
                self._store.anzeige_merken(wert)  # das Thermostat zeigt den Wert der letzten Drehung
                self._store.uebersteuerung_setzen(temp, bis)
                weg.beenden(lage.art, None, {"temp": temp, "bis": bis}, herkunft, durch)
                if temp != wert:  # ein ausgeschaltetes Thermostat verwirft das offene Ende (`_wunsch_verwerfen`)
                    self._schreiben(temp)
                return
            self._store.uebersteuerung_aufheben()
            self._store.apply_thermostat(wert)
            begrenzt = weg.beenden(lage.art, wert, None, herkunft, durch)
            if begrenzt is not None and begrenzt != self._store.value:
                self._store.begrenzen(begrenzt)
                self._schreiben(begrenzt)
        except ValueError:
            logger.info("Ende von %s nicht an den Server gesendet (abgelehnt), der Wert gilt lokal", lage.art)

    def _betriebsart_laeuft(self) -> bool:
        weg = self._uebersteuerung
        return weg is not None and weg.betriebsart is not None and weg.betriebsart() is not None

    def _kuehl_bis(self) -> str | None:
        """Ende der Uebersteuerung (naechster Planwechsel, UTC-Text), wenn eine Eingabe jetzt eine waere: Faehigkeit
        programm, kein offener Wunsch, Plan in der kuehlen Phase (Absenkung oder uebersteuert) mit naechstem Wechsel.
        Sonst None: die Eingabe ist ein Wunsch wie bisher."""
        weg = self._uebersteuerung
        if weg is None or self._wunsch_offen or not weg.moeglich():
            return None
        lage = weg.lage()
        if (lage is None or lage.soll_art not in (SOLL_ART_ABSENKUNG, SOLL_ART_UEBERSTEUERT)
                or lage.naechster_wechsel is None):
            return None
        return lage.naechster_wechsel.astimezone(UTC).strftime(_ZEIT_FORMAT)

    def _ueb_eingabe(self, value: object, bis: str, herkunft: str | None, durch: str | None, *,
                     thermostat: bool, geprueft: float | None = None) -> bool:
        """Eine Eingabe in der kuehlen Phase wird zur lokalen Uebersteuerung (auf die Grenzen aus safety.py begrenzt).
        False: keine Eingabe des Kunden (Echo, Wiederholung, unplausibel). `geprueft`: der Wert, den der Speicher schon als
        Eingabe des Kunden durchgelassen hat."""
        aktuell = self._store.uebersteuerung()
        if thermostat:
            neu = geprueft if geprueft is not None else self._store.thermostat_pruefen(
                value, angezeigt=None if aktuell is None else aktuell["temp"])
            if neu is None:
                return False
            self._store.anzeige_merken(neu)
        else:
            if not is_plausible(value, ROOM_TEMP_RANGE):
                logger.warning("Lokale Wunschtemperatur unplausibel, verworfen")
                return False
            neu = float(value)  # type: ignore[arg-type]
            if aktuell is not None and aktuell["temp"] == neu and aktuell["bis"] == bis:
                return False
        if not self._ueb_offen:
            self._vor_ueb = aktuell
        self._ueb_offen = True
        self._ueb_roh, self._ueb_lokal = neu, not thermostat
        self._store.uebersteuerung_setzen(min(max(neu, ABSENK_UNTERGRENZE_C), BEDIENBEREICH_MAX_C), bis)
        self._herkunft = (herkunft, durch)
        return True

    def _ueb_senden(self) -> None:
        """Nach dem Entprellen: die Quelle zeigt die (begrenzte) Uebersteuerung, der Server bekommt sie als Wunsch mit
        programm. Lehnt der Geraetekern ab (Notbetrieb, kein Vertragswert), gilt sie trotzdem lokal."""
        self._ueb_offen, self._vor_ueb = False, None
        aktuell = self._store.uebersteuerung()
        if aktuell is None:
            return
        temp = aktuell["temp"]
        if (self._ueb_lokal or temp != self._ueb_roh) and not self._aus:
            self._schreiben(temp)
        if self._uebersteuerung is not None:
            herkunft, durch = self._herkunft
            try:
                self._uebersteuerung.senden(temp, aktuell["bis"], herkunft, durch)
            except ValueError:
                logger.info("Uebersteuerung nicht an den Server gesendet (abgelehnt), sie gilt lokal")

    def _unbrauchbar_melden(self, value: object) -> None:
        """Je Fehlerart eine WARNING bis zum naechsten brauchbaren Wert, danach DEBUG: das Gateway reicht jede
        Zigbee-Meldung mit occupied_heating_setpoint durch, ein dauerhaft unbrauchbarer Wert flutete sonst das Log.
        Ohne den Wert (Text aus HA oder Zigbee)."""
        if not isinstance(value, int | float) or isinstance(value, bool):
            art = f"kein Zahlenwert ({type(value).__name__})"
        elif not ist_endliche_zahl(value):
            art = "nicht endlich"
        else:
            art = "ausserhalb des Raumbereichs"
        if art in self._unbrauchbar_gemeldet:
            logger.debug("Wunschtemperatur vom Thermostat unbrauchbar (%s), verworfen", art)
            return
        self._unbrauchbar_gemeldet.add(art)
        logger.warning("Wunschtemperatur vom Thermostat unbrauchbar (%s), verworfen; weitere gleiche nur im DEBUG-Log "
                       "bis zu einem brauchbaren Wert", art)

    def _entprellt(self) -> None:
        if self._beenden_offen is not None:
            self._beenden_senden()
        if self._ueb_offen:
            self._ueb_senden()
        if self._wunsch_offen:
            lokal = self._lokal_offen
            herkunft, durch = self._herkunft
            self._wunsch_offen = self._lokal_offen = False
            self._vor_wunsch = None
            try:
                begrenzt = self._wunsch(self._store.value, herkunft, durch)
            except ValueError:
                begrenzt = self._store.value
            if begrenzt != self._store.value:
                self._store.begrenzen(begrenzt)
                if not self._aus:
                    self._schreiben(begrenzt)
            elif lokal and not self._aus:
                self._schreiben(self._store.value)
        if self._ende_wartet and not self._wunsch_offen:
            self._worker.post(Event(EV_SOLL_ENDE))  # das Soll-Ende kam bei offenem Wunsch: jetzt mit dem Wert des Kunden
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
