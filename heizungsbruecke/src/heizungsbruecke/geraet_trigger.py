"""HA-Trigger im Geraete-Pfad (Vertrag smartheat_runtime.ports.TriggerSource): ein subscribe_trigger auf
die Soll-Entity (mit for: 10 s), ihren HVAC-Modus (Climate), das Raumfuehler-Mittel, das Ereignis
smartheat_wunsch der Integration und den Tagestick. Der WS-Thread stellt nur Ereignisse in den Worker ein; dort
entscheidet der Soll-Eingang (smartheat_runtime.soll_eingang, entprellen 0 s): Wunsch mit Herkunft (soll_entity bzw.
ha) und durch (nutzer/automation aus dem HA-Kontext), Thermostat aus/Frostschutz, Angleichen der
Soll-Entity (nur Climate ist schreibbar) ohne Echo. Das Ereignis smartheat_integration (Lebenszeichen der Integration)
geht nur in den Stand der Integration (integration_stand), nie in den Worker."""
import logging
import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypeGuard

from heizungsbruecke.ha_trigger_client import HaTriggerClient
from heizungsbruecke.integration_stand import IntegrationStand
from heizungsbruecke.push_stand import PushStand
from smartheat_core import wallclock
from smartheat_core.logtext import lesbar
from smartheat_core.safety import abwesend_bis_gueltig, abwesend_temperatur_gueltig
from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS
from smartheat_runtime.dokument_config import ConfigError
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import (
    EV_SOLL_MODUS,
    EV_TARGET_INPUT,
    FROSTSCHUTZ_BIS,
    SollEingang,
    UebersteuerungsWeg,
    quelle_freigegeben,
)
from smartheat_runtime.soll_speicher import KREIS, SOURCE_LOKAL, SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

#: Intern (Worker): ein Bericht der Soll-Entity bzw. das (Nach-)Lesen ihres Zustands.
EV_SOLL_BERICHT = "ha_soll_bericht"
EV_SOLL_LESEN = "ha_soll_lesen"
EV_SOLL_NACHLESEN = "ha_soll_nachlesen"
#: Intern: ein gezeigter Wert, waehrend eine gescheiterte Angleichung offen ist (build: Gescheiterte Angleichung).
EV_SOLL_NACHHOLEN = "ha_soll_nachholen"
#: Ersatz fuer den Trigger, solange der WebSocket getrennt ist (wie der Watchdog der Laufzeit): im Takt des lokalen Checks
#: Modus und Soll der Soll-Entity lesen.
EV_SOLL_WACHE = "ha_soll_wache"
#: Modi, die kein Wechsel des Thermostats sind (Entity gerade nicht erreichbar): weder aus noch wieder an.
_MODI_OHNE_AUSSAGE = ("unavailable", "unknown")
NACHLESEN_PUFFER_SECONDS = 1.0
#: Bericht der Soll-Entity ohne Zahl (unavailable): hoechstens so oft eine Info-Zeile, und nur nach einer Zahl dazwischen.
OHNE_ZAHL_LOG_SECONDS = 3600.0

EREIGNIS_WUNSCH = "smartheat_wunsch"
#: Lebenszeichen der Integration (Version und gelesene Status-Schemata), gelesen in integration_stand.
EREIGNIS_INTEGRATION = "smartheat_integration"
#: Die Integration hat einen Push-Eintrag nach 24 h aufgegeben (Stufe und Kreis), gelesen in push_aufgegeben_lesen.
EREIGNIS_PUSH_AUFGEGEBEN = "smartheat_push_aufgegeben"
#: Away aus Home Assistant (Integration): setzen mit `bis` (und wahlweise `temp`) bzw. beenden, je Kreis.
EREIGNIS_ABWESEND = "smartheat_abwesend"
EREIGNIS_ABWESEND_ENDE = "smartheat_abwesend_ende"
#: Intern (Worker): ein Away-Ereignis nach dem Kreis-Routing; die Pruefung und der Aufruf des Hosts laufen dort.
EV_ABWESEND = "ha_abwesend"
#: Feste Gruende einer Ablehnung (nie ein Wert): der Hinweis `abwesend_abgelehnt` traegt nur diesen Code.
ABGELEHNT_KREIS = "kreis"
ABGELEHNT_BIS = "bis"
ABGELEHNT_TEMP = "temp"
HERKUNFT_SOLL = "soll_entity"
HERKUNFT_SMARTHEAT = "ha"
_PUSH_STUFEN = ("rot", "orange")

#: Rueckruf des Hosts: Away setzen (`bis`, `temp` oder None fuer den Standard des Hosts, `durch`) bzw. beenden (`durch`);
#: Ergebnis None = angenommen, sonst der feste Ablehnungsgrund. Der Host fuehrt aus wie das Portal (Ist zuerst finden,
#: Bedienwunsch mit Herkunft `ha`).
AbwesendSetzen = Callable[[datetime, float | None, str | None], str | None]
AbwesendBeenden = Callable[[str | None], str | None]


def durch_aus_kontext(context) -> str | None:
    """HA-Kontext des Ausloesers: Benutzer gesetzt = nutzer, sonst uebergeordneter Ausloeser = automation."""
    if not isinstance(context, dict):
        return None
    if context.get("user_id"):
        return "nutzer"
    if context.get("parent_id"):
        return "automation"
    return None


def _split(ref: str) -> tuple[str, str | None]:
    """`climate.wz::temperature` -> (`climate.wz`, `temperature`): ein Trigger braucht die echte Entity-ID,
    `::attribut` ist eine Konvention dieses Add-ons (siehe ha_api.get_state)."""
    entity_id, _, attribute = ref.partition("::")
    return entity_id, attribute or None


def _is_number(value) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _zustandswert(value) -> float | None:
    """Zahl aus einem Zustand (Text) oder Attribut (Zahl); unavailable, unknown und Unlesbares sind None."""
    try:
        if _is_number(value):
            return float(value)
        return float(value) if isinstance(value, str) else None
    except (ValueError, OverflowError):  # OverflowError: JSON liefert beliebig grosse Ganzzahlen
        return None


def pruefe_soll_ref(soll_ref: str | None) -> None:
    """Eine Climate-Soll-Entity braucht das Attribut (`climate.x::temperature`): ohne waere der Zustand der HVAC-Modus,
    kein Soll. Der Server sendet das nie (room_target ist climate::temperature)."""
    if soll_ref and soll_ref.startswith("climate.") and _split(soll_ref)[1] is None:
        raise ConfigError(f"Soll-Entity {soll_ref} ohne Attribut: bei einer Climate-Entity ist "
                          f"{soll_ref}::temperature erforderlich")


def _wunsch_kreis(daten) -> int | None:
    """Der Kreis, dem das Ereignis smartheat_wunsch gilt: `kreis` der Ereignisdaten (Pflicht); fehlt er oder ist er
    ungueltig (kein ganzzahliger Kreis ab 1), ist das Ergebnis None."""
    wert = daten.get("kreis") if isinstance(daten, dict) else None
    return wert if isinstance(wert, int) and not isinstance(wert, bool) and wert >= 1 else None


def push_aufgegeben_lesen(daten, anlagenkreise: frozenset[int] | None = None) -> tuple[str, int | None] | None:
    """(Stufe, Kreis) aus dem Ereignis smartheat_push_aufgegeben; None, wenn der Kern nicht lesbar ist. Pflicht sind
    `schema` (ganze Zahl ab 1) und `stufe` (rot oder orange). Der Kreis gilt nur, wenn er eine ganze Zahl ab 1 ist und
    (mit Anlage) zur Anlage gehoert, sonst ist die Aussage anlagenweit (Kreis None). Fremde Felder zaehlen nicht; das
    Ereignis ist nicht vertrauenswuerdig und wirft hier nie."""
    if not isinstance(daten, dict):
        return None
    schema, stufe = daten.get("schema"), daten.get("stufe")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        return None
    if not isinstance(stufe, str) or stufe not in _PUSH_STUFEN:
        return None
    kreis = _wunsch_kreis(daten)
    if kreis is not None and anlagenkreise is not None and kreis not in anlagenkreise:
        kreis = None
    return stufe, kreis


def _wunschwert(value) -> float | None:
    """raum_soll aus dem Ereignis smartheat_wunsch: eine endliche Zahl oder ein Text, der eine ist (HA-YAML sendet
    oft Text, etwa \"22.5\"); alles andere None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):  # OverflowError: JSON liefert beliebig grosse Ganzzahlen
        return None
    return number if math.isfinite(number) else None


def _abwesend_bis(wert) -> datetime | None:
    """`bis` aus dem Ereignis: ein Zeitpunkt als ISO-Text; alles andere None (die Zone und den Bereich prueft safety)."""
    if not isinstance(wert, str):
        return None
    try:
        return datetime.fromisoformat(wert)
    except ValueError:
        return None


def _abwesend_temp(daten: dict) -> tuple[bool, float | None]:
    """(gueltig, temp): fehlendes oder leeres `temp` ist gueltig (None: der Host nimmt seinen Standard), sonst muss die
    Zahl im Bereich aus safety liegen."""
    wert = daten.get("temp")
    if wert is None:
        return True, None
    zahl = _wunschwert(wert)
    if zahl is None or not abwesend_temperatur_gueltig(zahl):
        return False, None
    return True, zahl


class HaSollQuelle:
    """Die Soll-Entity als Soll-Quelle (SollQuelle): schreibbar nur als Climate-Entity (Attribut temperature).
    angleichen_offen: das letzte Schreiben an die Climate-Entity ist gescheitert (HA Core startet, HA lehnt ab), die
    Entity zeigt also nicht das geltende Soll; bis zum naechsten gelungenen Schreiben holt build() die Angleichung nach,
    statt einen gezeigten Wert als Eingabe zu nehmen. Nur Worker-Thread (und der Start davor)."""

    def __init__(self, ha_api, soll_ref: str) -> None:
        self._ha, self._ref = ha_api, soll_ref
        self._entity, _ = _split(soll_ref)
        self.angleichen_offen = False

    def schreiben(self, value: float) -> bool:
        if not self._entity.startswith("climate."):
            return False
        try:
            self._ha.set_climate_temperature(self._entity, value)
        except Exception as error:
            logger.warning("Soll-Entity nicht angeglichen (%s)", type(error).__name__)
            self.angleichen_offen = True
            return False
        self.angleichen_offen = False
        return True

    def angezeigt(self) -> float | None:
        try:
            value = self._ha.get_state(self._ref)
        except Exception:
            return None
        return float(value) if _is_number(value) else None

    def aus(self) -> bool:
        """Die Climate-Entity zeigt den Modus off (unlesbar zaehlt nicht als aus)."""
        if not self._entity.startswith("climate."):
            return False
        try:
            return self._ha.get_raw_state(self._entity) == "off"
        except Exception:
            return False


def build(manifest, ha_api, worker: RegulationWorker, store: TargetStore, soll_ref: str | None, *,
          tagestick: str | None, wache_s: float, wunsch, soll_quelle_aus,
          quelle: HaSollQuelle | None = None, kreis: int = KREIS,
          anlagenkreise: frozenset[int] | None = None, integration_stand: IntegrationStand | None = None,
          push_stand: PushStand | None = None, uhr: Callable[[], float] = time.monotonic,
          abwesend_setzen: AbwesendSetzen | None = None, abwesend_beenden: AbwesendBeenden | None = None,
          abwesend_abgelehnt: Callable[[str], None] | None = None,
          uebersteuerung: UebersteuerungsWeg | None = None, angleichen: bool = False,
          wanduhr: Callable[[], datetime] = wallclock.now) -> HaTriggerClient:
    """integration_stand: nimmt das Lebenszeichen der Integration (Ereignis smartheat_integration, anlagenweit) auf, nach
    der monotonen uhr; nur der Client, der ihn bekommt, abonniert das Ereignis (ohne Stand gibt es kein Abonnement).
    push_stand: ebenso fuer das Ereignis smartheat_push_aufgegeben (Stufe und Kreis, nur im Stand, nie im Worker).
    kreis: der Heizkreis des Clients; ein Ereignis smartheat_wunsch gilt dem Kreis seiner Ereignisdaten (Pflicht;
    ohne gueltigen Kreis wird es verworfen) und erreicht nur den Client dieses Kreises. anlagenkreise: alle Kreise der
    Anlage; gilt ein Wunsch keinem davon, meldet das nur der Client des niedrigsten Kreises (sonst eine Meldung je Kreis). soll_ref: die echte Soll-Entity aus der Konfiguration (nicht manifest.refs["room_target"], das ist SOLL_REF).
    tagestick: Uhrzeit des taeglichen Ticks (HH:MM) oder None; wache_s: Takt des lokalen Checks (RuntimeConfig).
    Der Bericht der Soll-Entity (EV_SOLL_BERICHT) geht als Eingabe der Soll-Quelle in den Soll-Eingang. Faellt er in
    das Echo-Fenster nach dem eigenen Schreiben, verwirft ihn der Soll-Speicher; HA meldet einen unveraenderten Wert
    nie wieder. Deshalb liest EV_SOLL_NACHLESEN die Entity kurz nach dem Fenster einmal erneut und gibt den Wert
    durch denselben Weg (Pending-/Stale-Logik entscheidet). Bei jeder (Re-)Verbindung liest EV_SOLL_LESEN Modus und Soll
    (Aenderungen waehrend der Trennung, Thermostat schon aus beim Start). Solange der WebSocket getrennt ist, liest
    EV_SOLL_WACHE im Takt des lokalen Checks dasselbe (der Raum-Soll kommt nur ueber den Soll-Eingang in den Soll-Speicher, nie
    direkt aus HA). Nur HA; das Gateway bleibt unberuehrt.

    Away (Fähigkeit programm): mit `abwesend_setzen` abonniert der Client die Ereignisse smartheat_abwesend und
    smartheat_abwesend_ende (Kreis wie beim Wunsch geroutet). Im Worker prueft er `bis` (Zone, in der Zukunft, hoechstens
    365 Tage nach der `wanduhr`) und `temp` gegen safety.py und ruft dann den Host (`abwesend_setzen`, `abwesend_beenden`);
    ein Ablehnungsgrund (eigener oder des Hosts) geht als fester Code an `abwesend_abgelehnt`, nie ein Wert, nie ins Log.
    `uebersteuerung`: Verdrahtung der Uebersteuerung der kuehlen Phase im Soll-Eingang (ohne sie bleibt jede Eingabe ein
    Wunsch).

    Gescheiterte Angleichung (quelle.angleichen_offen: Bedienung vom Server, Angleichen beim Start, Zurueckschreiben
    eines Wunschs): jeder gezeigte Wert (Bericht, Nachlesen, Lesen beim Verbinden und in der Wache; die Wache liest dann
    auch bei verbundenem WebSocket) holt sie nach (EV_SOLL_NACHHOLEN), statt Eingabe zu werden: das geltende Soll an
    die Entity, der gezeigte Wert gilt als veraltet. Erst nach dem gelungenen Schreiben zaehlt eine Bedienung an der
    Entity wieder als Wunsch; eine Drehung am Thermostat waehrend desselben Ausfalls verliert gegen die neuere
    Bedienung. Ausnahme Soll-Quelle aus (Modus off, Frostschutz-Stellung): der Wert ist Eingabe wie bisher, beim
    Wiedereinschalten gleicht der Soll-Eingang an. quelle: die Soll-Quelle, die auch das Angleichen beim Start nutzt
    (host.HaHost); ohne baut build sie selbst."""
    pruefe_soll_ref(soll_ref)
    if quelle is None and soll_ref:
        quelle = HaSollQuelle(ha_api, soll_ref)
    eingang = SollEingang(worker, store, quelle, wunsch=wunsch, soll_quelle_aus=soll_quelle_aus, entprellen_s=0,
                          uebersteuerung=uebersteuerung)
    soll_entity, soll_attribute = _split(soll_ref) if soll_ref else (None, None)
    climate = soll_entity is not None and soll_entity.startswith("climate.")
    triggers: list[dict] = []
    if soll_entity is not None:
        trigger = {"platform": "state", "entity_id": soll_entity}
        if soll_attribute:
            trigger["attribute"] = soll_attribute
        triggers.append({**trigger, "for": {"seconds": ROOM_TARGET_DEBOUNCE_SECONDS}})
        if climate:
            # HVAC-Modus: nur Zustandswechsel (to: null), nicht jede Attributaenderung des Thermostats.
            triggers.append({"platform": "state", "entity_id": soll_entity, "to": None})
    room_actual = manifest.refs.get("room_actual")
    if room_actual:
        entity_id, attribute = _split(room_actual)
        trigger = {"platform": "state", "entity_id": entity_id}
        if attribute:
            trigger["attribute"] = attribute
        triggers.append(trigger)
    triggers.append({"platform": "event", "event_type": EREIGNIS_WUNSCH})
    if integration_stand is not None:
        triggers.append({"platform": "event", "event_type": EREIGNIS_INTEGRATION})
    if push_stand is not None:
        triggers.append({"platform": "event", "event_type": EREIGNIS_PUSH_AUFGEGEBEN})
    if abwesend_setzen is not None:
        triggers.append({"platform": "event", "event_type": EREIGNIS_ABWESEND})
        triggers.append({"platform": "event", "event_type": EREIGNIS_ABWESEND_ENDE})
    if tagestick:
        triggers.append({"platform": "time", "at": tagestick})

    letzter_bericht: list = [None, None]  # (Wert, durch) des zuletzt verworfenen Berichts im Echo-Fenster
    nachlesen_offen = [False]

    def eingabe(wert: float, durch: str | None) -> None:
        worker.post(Event(EV_TARGET_INPUT, {"value": wert, "source": SOURCE_THERMOSTAT, "herkunft": HERKUNFT_SOLL,
                                            "durch": durch}))

    def gezeigt(wert: float, durch: str | None) -> bool:
        """Ein gezeigter Wert der Soll-Entity; True, wenn eine offene Angleichung ihn uebernimmt (dann in der
        Warteschlange nach einem schon gemeldeten Modus), sonst False (der Aufrufer nimmt ihn als Eingabe)."""
        if quelle is None or not quelle.angleichen_offen:
            return False
        worker.post(Event(EV_SOLL_NACHHOLEN, {"value": wert, "durch": durch}))
        return True

    def on_nachholen(event: Event) -> None:
        wert, durch = event.data["value"], event.data["durch"]
        assert quelle is not None  # nur aus gezeigt()
        if not quelle.angleichen_offen:  # inzwischen angeglichen: der Wert ist eine Eingabe wie jeder andere
            eingabe(wert, durch)
            return
        if eingang.aus or eingang.stillgelegt or wert <= FROSTSCHUTZ_BIS:  # Soll-Quelle aus oder Regelung beendet: wie
            quelle.angleichen_offen = False  # bisher, der Soll-Eingang gleicht beim Wiedereinschalten bzw. bei der
            # Wiederaufnahme an (nichts steht mehr offen); Frostschutz bleibt unangetastet
            eingabe(wert, durch)
            return
        if wert == eingang.geltende_anzeige():  # zeigt das Soll doch (HA hat es trotz Fehler uebernommen)
            quelle.angleichen_offen = False
            return
        eingang.anzeige_abgleichen("nachholen", bekannt=wert)

    def modus(mode) -> None:
        if isinstance(mode, str) and mode not in _MODI_OHNE_AUSSAGE:
            worker.post(Event(EV_SOLL_MODUS, {"mode": mode}))

    def on_bericht(event: Event) -> None:
        if "mode" in event.data:  # Modus und Soll gehen durch dieselbe Warteschlange: Reihenfolge der Meldungen bleibt
            modus(event.data["mode"])
            return
        wert, durch = event.data["value"], event.data["durch"]
        if gezeigt(wert, durch):
            return
        eingabe(wert, durch)
        rest = store.echo_rest()
        if rest > 0:
            letzter_bericht[:] = [wert, durch]
            if not nachlesen_offen[0]:
                nachlesen_offen[0] = True
                worker.schedule(rest + NACHLESEN_PUFFER_SECONDS, Event(EV_SOLL_NACHLESEN))

    def on_nachlesen(event: Event) -> None:
        rest = store.echo_rest()
        if rest > 0:  # inzwischen ein neues eigenes Schreiben: nach dessen Fenster
            worker.schedule(rest + NACHLESEN_PUFFER_SECONDS, Event(EV_SOLL_NACHLESEN))
            return
        nachlesen_offen[0] = False
        wert = quelle.angezeigt() if quelle is not None else None
        if wert is not None:
            durch = letzter_bericht[1] if letzter_bericht[0] == wert else None
            if not gezeigt(wert, durch):
                eingabe(wert, durch)
        letzter_bericht[:] = [None, None]

    def on_lesen(event: Event) -> None:
        if soll_entity is None or quelle is None:
            return
        if climate:
            try:
                modus(ha_api.get_raw_state(soll_entity))
            except Exception as error:
                logger.warning("HVAC-Modus der Soll-Entity nicht gelesen (%s)", type(error).__name__)
        wert = quelle.angezeigt()
        if wert is not None and not gezeigt(wert, None):
            eingabe(wert, None)

    worker.register(EV_SOLL_BERICHT, on_bericht)
    worker.register(EV_SOLL_NACHHOLEN, on_nachholen)
    worker.register(EV_SOLL_NACHLESEN, on_nachlesen)
    worker.register(EV_SOLL_LESEN, on_lesen)

    def abwesend_pruefen_und_rufen(daten: dict) -> str | None:
        """Der Ablehnungsgrund oder None (angenommen). Der Host bekommt nur geprueftes."""
        if daten["ende"]:
            return abwesend_beenden(daten["durch"]) if abwesend_beenden is not None else None
        bis = _abwesend_bis(daten["bis"])
        if bis is None or not abwesend_bis_gueltig(bis, wanduhr()):
            return ABGELEHNT_BIS
        temp_gueltig, temp = _abwesend_temp(daten)
        if not temp_gueltig:
            return ABGELEHNT_TEMP
        assert abwesend_setzen is not None  # nur abonniert, wenn gesetzt
        return abwesend_setzen(bis, temp, daten["durch"])

    def on_abwesend(event: Event) -> None:
        grund = event.data.get("abgelehnt") or abwesend_pruefen_und_rufen(event.data)
        if grund:
            logger.warning("Abwesenheit abgelehnt (%s)", grund)  # fester Code, nie ein Wert
            if abwesend_abgelehnt is not None:
                abwesend_abgelehnt(grund)

    worker.register(EV_ABWESEND, on_abwesend)

    def abwesend_ereignis(event: dict) -> None:
        """Kreis-Routing wie beim Wunsch; was den eigenen Kreis trifft (oder ein unbekannter Kreis, vom Client des
        niedrigsten Kreises gemeldet), geht in den Worker. Ungueltiger Kreis: verworfen, ohne Hinweis."""
        daten = event.get("data")
        ziel = _wunsch_kreis(daten)
        if ziel is None:
            logger.warning("Ereignis %s mit ungueltigem Kreis verworfen", event.get("event_type"))
            return
        if anlagenkreise is not None and ziel not in anlagenkreise:
            if kreis == min(anlagenkreise):
                worker.post(Event(EV_ABWESEND, {"abgelehnt": ABGELEHNT_KREIS}))
            return
        if ziel != kreis:
            return
        daten = daten if isinstance(daten, dict) else {}
        worker.post(Event(EV_ABWESEND, {"ende": event.get("event_type") == EREIGNIS_ABWESEND_ENDE,
                                        "bis": daten.get("bis"), "temp": daten.get("temp"),
                                        "durch": durch_aus_kontext(event.get("context"))}))

    ohne_zahl: list = [False, None]  # (gerade ohne Zahl, Zeitpunkt der letzten Info-Zeile); nur im WS-Thread

    def _ohne_zahl_melden() -> None:
        """Eine nicht verfuegbare Soll-Entity bleibt sichtbar: Info-Zeile beim Wechsel von Zahl zu keiner Zahl, hoechstens
        alle OHNE_ZAHL_LOG_SECONDS; ohne den gemeldeten Inhalt."""
        jetzt = time.monotonic()
        if not ohne_zahl[0] and (ohne_zahl[1] is None or jetzt - ohne_zahl[1] >= OHNE_ZAHL_LOG_SECONDS):
            logger.info("Soll-Entity %s meldet keinen Zahlenwert (etwa nicht verfuegbar), kein Bedienwunsch",
                        lesbar(str(soll_entity)))
            ohne_zahl[1] = jetzt
        ohne_zahl[0] = True

    def on_soll_entity(trigger: dict) -> bool:
        """True, wenn der Trigger die Soll-Entity betraf (Wunsch oder Modus); ein anderes Attribut derselben Entity
        (etwa current_temperature als Raumfuehler) ist ein lokaler Check."""
        attribute = trigger.get("attribute") or None
        to_state = trigger.get("to_state") or {}
        if attribute == soll_attribute:
            value = (to_state.get("attributes") or {}).get(attribute) if attribute else to_state.get("state")
            number = _zustandswert(value)
            if number is not None:
                ohne_zahl[0] = False
                worker.post(Event(EV_SOLL_BERICHT, {"value": number,
                                                    "durch": durch_aus_kontext(to_state.get("context"))}))
            else:
                _ohne_zahl_melden()
            return True
        if attribute is None and climate:
            worker.post(Event(EV_SOLL_BERICHT, {"mode": to_state.get("state")}))
            return True
        return False

    def on_trigger(trigger: dict) -> None:
        platform = trigger.get("platform")
        if platform == "event":
            event = trigger.get("event") or {}
            if event.get("event_type") == EREIGNIS_INTEGRATION:
                if integration_stand is not None:  # nur Version und Schemata; kein Wunsch, kein lokaler Check
                    integration_stand.ereignis(event.get("data"), uhr())
                return
            if event.get("event_type") == EREIGNIS_PUSH_AUFGEGEBEN:
                if push_stand is not None:  # nur Stufe und Kreis; kein Wunsch, kein lokaler Check, kein Log von Inhalten
                    gelesen = push_aufgegeben_lesen(event.get("data"), anlagenkreise)
                    if gelesen is not None:
                        push_stand.aufgegeben(gelesen[0], gelesen[1], uhr())
                return
            if event.get("event_type") in (EREIGNIS_ABWESEND, EREIGNIS_ABWESEND_ENDE):
                if abwesend_setzen is not None:  # kein Wunsch, kein lokaler Check
                    abwesend_ereignis(event)
                return
            ziel = _wunsch_kreis(event.get("data"))
            if ziel is None:
                logger.warning("Ereignis %s mit ungueltigem Kreis verworfen", EREIGNIS_WUNSCH)
                return
            if anlagenkreise is not None and ziel not in anlagenkreise:
                if kreis == min(anlagenkreise):
                    logger.warning("Ereignis %s mit unbekanntem Kreis verworfen", EREIGNIS_WUNSCH)
                return
            if ziel != kreis:
                return  # gilt dem Client eines anderen Kreises
            value = (event.get("data") or {}).get("raum_soll")
            number = _wunschwert(value)
            if number is None:
                logger.warning("Ereignis %s ohne gueltige raum_soll (Typ %s) verworfen", EREIGNIS_WUNSCH,
                               type(value).__name__)
                return
            worker.post(Event(EV_TARGET_INPUT, {"value": number, "source": SOURCE_LOKAL,
                                                "herkunft": HERKUNFT_SMARTHEAT,
                                                "durch": durch_aus_kontext(event.get("context"))}))
            return
        if platform == "state" and soll_entity is not None and trigger.get("entity_id") == soll_entity \
                and on_soll_entity(trigger):
            return
        worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=False)

    def on_connected() -> None:
        # Bei jeder (Re-)Verbindung, auch der ersten: Soll und HVAC-Modus der Soll-Entity lesen (eine Aenderung
        # waehrend der Trennung, etwa eines HA-Neustarts, meldet HA nicht nach), den lokalen Check mit frischem
        # room_target-Flag und den vollen Status neu senden (nach einem HA-Neustart hat die Integration ihn nicht mehr).
        worker.post(Event(EV_SOLL_LESEN))
        worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
        worker.post_coalesced(EV_SOURCE_CONNECTED)

    client = HaTriggerClient(ws_url=ha_api.websocket_url(), token=ha_api.token, triggers=triggers,
                             on_trigger_event=on_trigger, on_connected=on_connected)

    def on_wache(event: Event) -> None:
        worker.schedule(wache_s, Event(EV_SOLL_WACHE))  # zuerst: der Takt darf nie abreissen
        if not client.connected or (quelle is not None and quelle.angleichen_offen):
            on_lesen(event)

    worker.register(EV_SOLL_WACHE, on_wache)
    worker.schedule(wache_s, Event(EV_SOLL_WACHE))
    if angleichen and quelle is not None and soll_entity is not None and climate:
        _start_angleichen(eingang, quelle)
    return client


def _start_angleichen(eingang: SollEingang, quelle: HaSollQuelle) -> None:
    """"Bedienung angleichen" beim Start (Gateway: `_on_angleichen`): Thermostat, HA und Portal zeigen denselben Wert, ueber
    den Anzeige-Abgleich des Soll-Eingangs. Nicht bei Modus off und nicht in der Frostschutz-Stellung (bis FROSTSCHUTZ_BIS:
    Soll-Quelle aus, beim Wiedereinschalten gleicht der Soll-Eingang an). Scheitert das Schreiben, holt der Trigger es beim
    Verbinden nach (`quelle.angleichen_offen`). Ist die Anzeige beim Start unlesbar (HA startet noch), wird nichts
    geschrieben: es bleibt als offen vermerkt, und das Nachholen prueft die dann gelesene Anzeige (Frostschutz-Stellung,
    Modus off). Laeuft im Hauptthread vor dem Worker."""
    angezeigt = quelle.angezeigt()
    if not quelle_freigegeben(quelle, angezeigt):  # Modus off und Frostschutz-Stellung, wie im ersten Check des Soll-Eingangs
        return
    if angezeigt is None:  # unbekannt: weder die gemerkte Anzeige noch ein Blindschreiben
        quelle.angleichen_offen = True
        return
    eingang.anzeige_abgleichen("start", bekannt=angezeigt)
