"""Status-Ereignis des HA-Add-ons: smartheat_status Schema 4 = die Felder der Anlage aus dem Laufzeit-Status
(Schema 2, smartheat_runtime.status, unveraendert fuer Gateway und up/status), Geraet (ID, Zustand, Verbindung),
Anlagenname, Version des Konfigurationsdokuments und dessen kritische Mindestversion der Integration, die Meldungen
des Geraets (`meldungen`: Schluessel, Code, Parameter, Stufe; den Wortlaut bildet die Integration), die Ziele des Push
(`benachrichtigung`, nur Handy-Dienste, hoechstens 10), die stummgeschalteten Hinweis-Kategorien (`hinweise_aus`) der
Bindung und die Push-Regeln der Person (`push_regeln`), dazu `kreise`: je Kreis der Konfiguration
Name, Raum (Ist, wirksamer Soll aus dem Soll-Speicher), die Kreisfelder der Laufzeit (Boost, Abo, Hebel, Zustand,
Kaelteschutz ...) und die Felder des Anzeige-Dokuments. Nie der Uebernahme-Code (der steht nur in der Discovery):
ins Ereignis kommen nur die benannten Felder, nie ein ganzes Objekt der Quellen. Der Kreisname ist ein
Kundendatum: er steht nur in diesem lokalen Ereignis, nie in Log, Topic, Push oder Alarm. Gesendet bei jedem Ereignis der Laufzeit (auch dem
Lebenszeichen alle 300 s) und bei einer inhaltlichen Aenderung der Anzeige. Felder sind Cross-Repo-Vertrag mit der
Integration (Contract-Check)."""
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping

from heizungsbruecke import dokument as ha_dokument
from heizungsbruecke.ha_sinks import EVENT_TYPE, meldung_daten
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core import kreisansicht, meldungen
from smartheat_core.config_check import HA_DIENST, HA_HINWEISE, PUSH_REGELN_FELDER, push_regeln_lesen
from smartheat_device.documents import Dokument
from smartheat_device.host import Anzeige
from smartheat_runtime import status
from smartheat_runtime.dokument_config import ConfigError

logger = logging.getLogger(__name__)

EVENT_SCHEMA = 4
GERAET_FIELDS = ("geraet_id", "geraet_zustand", "server_verbunden", "meldungen", "anlage_name",
                 "konfig_version", "mindest_integration", "benachrichtigung", "hinweise_aus", "push_regeln")
#: Hoechstzahl der Notify-Dienste im Ereignis: was darueber liegt, geht nie hinaus (die Integration pusht an diese Ziele).
MAX_ZIELE = 10
#: Form der kritischen Mindestversion der Integration im Ereignis (wortgleich mit der Pruefung der Integration): drei
#: Ziffernfolgen (nur ASCII-Ziffern), hoechstens eine Endung (`-`/`+` mit Buchstaben und Ziffern oder `.dev` mit Zahl),
#: hoechstens 32 Zeichen; was nicht passt, geht nie ins Ereignis (kein Freitext des Servers, keine Domain-Endung).
MINDEST_INTEGRATION_RE = re.compile(
    r"(?=.{1,32}\Z)\d{1,6}\.\d{1,6}\.\d{1,6}(?:[-+][0-9A-Za-z]{1,16}|\.dev\d{1,6})?", re.ASCII)
#: Felder der Anlage im Ereignis und Felder eines Kreises: die Kreisfelder der Laufzeit plus die lokalen.
LOKAL_KREIS_FIELDS = ("name", "raum_soll", "raum_ist", "einlernphase_tag", "testphase_bis", "befunde", "abwesend")
EVENT_FIELDS = (*status.ANLAGE_FELDER, *GERAET_FIELDS, "kreise")
KREIS_EVENT_FIELDS = ("kreis", *LOKAL_KREIS_FIELDS, *status.KREIS_FELDER)
KONFIGURATION_ABGELEHNT = "konfiguration_abgelehnt"
#: Lokaler Hinweis (nicht in `wire.MELDUNGEN`): Away aus HA nicht uebernommen, mit festem Grund als Parameter.
ABWESEND_ABGELEHNT = "abwesend_abgelehnt"
#: HA nicht erreichbar: so lange Pause bis zum naechsten Versuch des Takts (wie discovery.RETRY_SECONDS: Log nicht fuellen).
WIEDERHOLEN_SECONDS = 60
#: Zustaende des Geraets, die einen Hinweis tragen, mit dem Code der Meldung (update_noetig: siehe `meldungen_liste`).
ZUSTAND_MELDUNGEN = {
    "gesperrt": meldungen.PREFIX + "gesperrt",
}


def meldungen_liste(anzeige: Anzeige, ende: str | None = None, *, kritisch: bool) -> list[dict]:
    """Meldungen der Anzeige als {key, code, params, stufe} (Code und Parameter, nie ein Text; die Integration bildet
    den Wortlaut): Hinweise des Servers, eigene Meldungen des Geraets, abgelehnte Konfiguration und die Zustaende
    update_noetig/gesperrt (was keine gueltige Meldung der Tabelle ist, entfaellt). Bei update_noetig mit abgelehnter
    Konfiguration stehen bewusst beide Meldungen da: der Grund der Ablehnung und die Handlungsanweisung. update_noetig
    ist unter einem kritischen Eintrag (`kritisch`) rot, mit `ende` als Datum der urspruenglichen Werte, ohne die Fassung
    ohne Datum; ohne kritischen Eintrag orange und ohne Datum (`meldungen.update_noetig`)."""
    result = []
    for eintrag in (*anzeige.hinweise, *anzeige.meldungen):
        meldung = meldungen.aus_dict({feld: eintrag.get(feld) for feld in ("code", "params", "stufe")}
                                     ) if isinstance(eintrag, dict) and eintrag.get("key") else None
        if meldung is not None:
            result.append(meldung_daten(str(eintrag["key"]), meldung))
    abgelehnt = meldungen.aus_dict(anzeige.konfiguration_abgelehnt)
    if abgelehnt is not None:
        result.append(meldung_daten(KONFIGURATION_ABGELEHNT, abgelehnt))
    abwesend = meldungen.aus_dict(anzeige.abwesend_abgelehnt)
    if abwesend is not None:
        result.append(meldung_daten(ABWESEND_ABGELEHNT, abwesend))
    if anzeige.zustand == "update_noetig":
        result.append(meldung_daten("update_noetig", meldungen.update_noetig(kritisch=kritisch, ende=ende)))
    if anzeige.zustand in ZUSTAND_MELDUNGEN:
        result.append(meldung_daten(anzeige.zustand, meldungen.neu(ZUSTAND_MELDUNGEN[anzeige.zustand])))
    return result


def _zahl(wert) -> float | None:
    return float(wert) if isinstance(wert, int | float) and not isinstance(wert, bool) else None


def _version(dokument: Dokument | None) -> int | None:
    version = dokument.version if dokument is not None else None
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _mindest_integration(konf: Mapping | None) -> str | None:
    wert = konf.get("mindest_integration") if isinstance(konf, Mapping) else None
    return wert if isinstance(wert, str) and MINDEST_INTEGRATION_RE.fullmatch(wert) else None


def _ziele(konf: Mapping | None) -> tuple[list[str], list[str]]:
    """Ziele des Push und stummgeschaltete Hinweis-Kategorien der Bindung (anlagenweit: die des ersten Kreises, wie sie
    der Host fuer die Senke liest). Nur Handy-Dienste nach `HA_DIENST` (hoechstens `MAX_ZIELE`) und Kategorien aus
    `HA_HINWEISE`, jeweils ohne Doppelte und in der Reihenfolge des Dokuments; Verworfenes wird nicht protokolliert (die
    Dienstnamen sind Kundendaten). Ohne Dokument oder mit unlesbarer Bindung beide Listen leer."""
    try:
        bindung = ha_dokument.bindung(konf)
    except ConfigError:
        return [], []
    dienste = list(dict.fromkeys(d for d in bindung.benachrichtigung if HA_DIENST.fullmatch(d)))
    kategorien = list(dict.fromkeys(h for h in bindung.hinweise_aus if h in HA_HINWEISE))
    return dienste[:MAX_ZIELE], kategorien


def _push_regeln(wert) -> dict[str, int]:
    """Die Push-Regeln des Dokuments (Ruhezeit und Tageslimit), je Feld mit dem Standard des Geraets bei Ungueltigem
    oder Fehlendem."""
    regeln = push_regeln_lesen(wert)
    return {feld: getattr(regeln, feld) for feld in PUSH_REGELN_FELDER}


def _abwesend(wert) -> dict | None:
    """Abwesenheit eines Kreises im Ereignis: genau `bis` (Zeitpunkt als Text) und `temp` (Zahl), sonst None; was der
    Bedienung sonst anhaengt (etwa `aufheizen_ab`) geht nicht hinaus."""
    if not isinstance(wert, Mapping):
        return None
    bis, temp = wert.get("bis"), _zahl(wert.get("temp"))
    if not isinstance(bis, str) or not bis or temp is None:
        return None
    return {"bis": bis, "temp": temp}


def abwesend_aus_bedienung(bedienung: Callable[[], Mapping | None]) -> Callable[[int], Mapping | None]:
    """Rueckruf fuer `lokale_sicht`: das Feld `abwesend` des Kreises aus dem Bedienungs-Dokument, nur im Modus `abwesend`
    (sonst gilt keine Abwesenheit, ein Rest im Feld zaehlt nicht)."""
    def lesen(kreis: int) -> Mapping | None:
        dokument = bedienung()
        for eintrag in (dokument.get("kreise") if isinstance(dokument, Mapping) else None) or ():
            if isinstance(eintrag, Mapping) and eintrag.get("kreis") == kreis and not isinstance(eintrag.get("kreis"), bool):
                return eintrag.get("abwesend") if eintrag.get("modus") == "abwesend" else None
        return None
    return lesen


def lokale_sicht(konfiguration: Callable[[], Dokument | None], soll: Callable[[int], float | None],
                 raum_ist: Callable[[int], float | None], anzeige: Callable[[], Mapping | None],
                 kritisch_ende: Callable[[list[int]], str | None] | None = None, *,
                 update_noetig_kritisch: Callable[[], bool],
                 abwesend: Callable[[int], Mapping | None] | None = None) -> Callable[[], dict]:
    """Die lokale Sicht des Ereignisses: Anlagenname, Version des Konfigurationsdokuments und dessen kritische
    Mindestversion der Integration (beide null ohne Dokument bzw. ohne gueltigen Wert) und je Kreis der Konfiguration
    Name, Soll (Soll-Speicher), Ist des Raums und die Felder des Anzeige-Dokuments (heute ohne Befund: null/null/[]).
    Version und Inhalt kommen aus einem Dokument, damit sie zusammenpassen. Dazu die Ziele des Push (`benachrichtigung`)
    und die stummgeschalteten Hinweis-Kategorien (`hinweise_aus`) der Bindung, leer ohne Dokument. `update_noetig_ende`
    ist das Datum der urspruenglichen Werte unter einer kritischen Mindestversion (`KritischesEnde`), sonst null;
    `update_noetig_kritisch` (Pflicht) ob die Konfiguration einen kritischen Eintrag traegt; beide gehen nicht
    ins Ereignis, nur in die Meldung. Liest nur, ohne Sperre."""
    def sicht() -> dict:
        dokument_konf = konfiguration()
        konf = dokument_konf.inhalt if dokument_konf is not None else None
        dokument = anzeige()
        eintraege = dokument.get("kreise") if isinstance(dokument, Mapping) else None
        je_kreis = {e.get("kreis"): e for e in eintraege if isinstance(e, Mapping)} if isinstance(eintraege, list) else {}
        kreise = {}
        for eintrag in kreisansicht.kreise(konf):
            nummer = eintrag.get("kreis")
            if not isinstance(nummer, int) or isinstance(nummer, bool):
                continue
            angezeigt = je_kreis.get(nummer, {})
            befunde = angezeigt.get("befunde")
            name = eintrag.get("name")
            kreise[nummer] = {
                "name": name if isinstance(name, str) else None, "raum_soll": _zahl(soll(nummer)),
                "raum_ist": _zahl(raum_ist(nummer)), "einlernphase_tag": angezeigt.get("einlernphase_tag"),
                "testphase_bis": angezeigt.get("testphase_bis"), "befunde": list(befunde) if isinstance(befunde, list) else [],
                "abwesend": _abwesend(abwesend(nummer)) if abwesend is not None else None,
            }
        name = konf.get("anlage_name") if isinstance(konf, Mapping) else None
        dienste, kategorien = _ziele(konf)
        return {"anlage_name": name if isinstance(name, str) else None, "konfig_version": _version(dokument_konf),
                "mindest_integration": _mindest_integration(konf), "benachrichtigung": dienste,
                "hinweise_aus": kategorien,
                "push_regeln": _push_regeln(konf.get("push_regeln") if isinstance(konf, Mapping) else None),
                "update_noetig_ende": kritisch_ende(list(kreise)) if kritisch_ende is not None else None,
                "update_noetig_kritisch": bool(update_noetig_kritisch()),
                "kreise": kreise}
    return sicht


class GeraetStatus:
    """Sendet nie zwei Ereignisse zugleich (_senden_lock) und ruft HA ausserhalb der Zustandssperre (_lock) auf: jedes
    Ereignis ist ein stimmiger Schnappschuss, und laufzeit_merken (sign_off) wartet nie auf HA. Ist HA nicht
    erreichbar (startup: services startet das Add-on vor HA Core), versucht der Takt des Geraete-Threads es erst nach
    WIEDERHOLEN_SECONDS erneut, ein Laufzeit-Ereignis in dieser Pause hoechstens einmal je Pause; eine Warnung je
    Ausfall (nur die Fehlerart), eine Info-Zeile bei der Erholung. Nach einem Fehler sendet der naechste erlaubte
    Versuch den aktuellen Stand, auch wenn er sich nicht geaendert hat."""

    def __init__(self, ha_api, geraet_id: Callable[[], str], lokal: Callable[[], Mapping], *,
                 laufzeit_merken: Callable[[dict], None], clock: Callable[[], float] = time.monotonic) -> None:
        """`lokal` liefert die lokale Sicht (`lokale_sicht`); es wird ausserhalb der Zustandssperre gerufen."""
        self._ha, self._geraet_id, self._lokal, self._merken = ha_api, geraet_id, lokal, laufzeit_merken
        self._clock = clock
        self._lock = threading.Lock()  # Zustand: Laufzeit-Ereignis und Anzeige
        self._senden_lock = threading.Lock()  # ein Sender zur Zeit; die Felder darunter nur mit ihm
        self._laufzeit: dict = {**dict.fromkeys(status.ANLAGE_FELDER), "status": status.STATUS_STARTET,
                                "addon_version": ADDON_VERSION}
        self._laufzeit_kreise: dict[int, dict] = {}
        self._anzeige: Anzeige | None = None
        self._zuletzt: dict | None = None
        self._pause_bis: float | None = None  # nach einem Fehler: vorher kein Versuch des Takts
        self._erzwungen_versucht = False  # ein Laufzeit-Ereignis in dieser Pause schon versucht

    def publish(self, event: dict) -> None:
        """StatusSink der Laufzeit (Worker-Thread). Wirft nicht: Fehler und Wiederholung regelt GeraetStatus (der
        Takt sendet nach der Pause den aktuellen Stand). Das flache Laufzeit-Ereignis ohne Kreisliste (tenant_id,
        status, grund fuer sign_off) geht an laufzeit_merken."""
        kreise = event.get("kreise")
        with self._lock:
            self._laufzeit = {key: event.get(key) for key in status.ANLAGE_FELDER}
            self._laufzeit_kreise = {
                eintrag["kreis"]: {key: eintrag.get(key) for key in status.KREIS_FELDER}
                for eintrag in (kreise if isinstance(kreise, list) else []) if isinstance(eintrag, dict)}
            self._merken(status.ohne_kreise(event))
        self._senden(force=True)

    def anzeige(self, anzeige: Anzeige) -> None:
        """Jeder Takt des Geraete-Threads; gesendet nur bei Aenderung (oder nach einem Fehler, nach der Pause)."""
        with self._lock:
            self._anzeige = anzeige
        self._senden(force=False)

    def _ereignis(self, lokal: Mapping) -> dict:
        anzeige = self._anzeige
        lokale_kreise = lokal["kreise"]
        kreise = [{
            "kreis": nummer,
            **{feld: lokale_kreise.get(nummer, {}).get(feld, [] if feld == "befunde" else None)
               for feld in LOKAL_KREIS_FIELDS},
            **{feld: self._laufzeit_kreise.get(nummer, {}).get(feld) for feld in status.KREIS_FELDER},
        } for nummer in sorted({*lokale_kreise, *self._laufzeit_kreise})]
        return {**self._laufzeit, "schema": EVENT_SCHEMA, "geraet_id": self._geraet_id(),
                "geraet_zustand": anzeige.zustand if anzeige is not None else "startet",
                "server_verbunden": anzeige.server_ok if anzeige is not None else False,
                "meldungen": (meldungen_liste(anzeige, lokal.get("update_noetig_ende"),
                                              kritisch=bool(lokal.get("update_noetig_kritisch")))
                              if anzeige is not None else []),
                "anlage_name": lokal["anlage_name"], "konfig_version": lokal["konfig_version"],
                "mindest_integration": lokal["mindest_integration"],
                "benachrichtigung": list(lokal.get("benachrichtigung") or ()),
                "hinweise_aus": list(lokal.get("hinweise_aus") or ()),
                "push_regeln": dict(lokal.get("push_regeln") or _push_regeln(None)), "kreise": kreise}

    def _senden(self, *, force: bool) -> None:
        with self._senden_lock:
            lokal = self._lokal()  # ausserhalb der Zustandssperre: liest Konfiguration und Soll-Speicher
            with self._lock:
                event = self._ereignis(lokal)
            if not force and event == self._zuletzt:
                return
            now = self._clock()
            in_pause = self._pause_bis is not None and now < self._pause_bis
            if in_pause:
                if not force or self._erzwungen_versucht:
                    return
                self._erzwungen_versucht = True
            try:
                self._ha.fire_event(EVENT_TYPE, event)
            except Exception as error:
                self._zuletzt = None  # der naechste erlaubte Versuch sendet den aktuellen Stand
                if not in_pause:  # Fehler im Pausenversuch verlaengert die Pause nicht
                    if self._pause_bis is None:
                        logger.warning("Status-Ereignis nicht an Home Assistant gesendet (%s), neuer Versuch alle "
                                       "%s s", type(error).__name__, WIEDERHOLEN_SECONDS)
                    self._pause_bis, self._erzwungen_versucht = now + WIEDERHOLEN_SECONDS, False
                return
            if self._pause_bis is not None:
                logger.info("Status-Ereignis wieder an Home Assistant gesendet")
                self._pause_bis, self._erzwungen_versucht = None, False
            self._zuletzt = event
