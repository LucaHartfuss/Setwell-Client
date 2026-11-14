"""Stoerungen des Waermeerzeugers: das Geraet liest die Kennungen, die die Quelle (Rolle fault_codes) meldet, und macht
je Kennung eine offene Meldung (`erzeuger.stoerung`, ein Wartungshinweis `erzeuger.hinweis`), sobald sie 15 Minuten
ununterbrochen ansteht. Faellt die Kennung weg, schliesst die Meldung ohne Push; kehrt sie zurueck, zaehlt die Zeit neu.
Wechselt die Art einer offenen Kennung (Wartungshinweis zu Stoerung oder zurueck), schliesst die alte Meldung ohne Push,
und die neue Art zaehlt ihre eigenen 15 Minuten.
Je Kreis sind hoechstens 8 Meldungen offen. Die Telemetrie nennt die aelteste offene Stoerung (`erzeuger_stoerung`), ein
Hinweis zaehlt dort nicht.

Die Laufzeit liest und reicht durch, sie deutet nichts: nur die normalisierte Kennung (`[a-z0-9_]{1,32}`) verlaesst das
Geraet, nie ein Text des Herstellers. Eine Kennung, die sich nicht so schreiben laesst, wird uebersprungen (nie gekuerzt).
Das Log nennt weder Kennung noch Entity. Ohne die Rolle gibt es nichts zu lesen; ein Treiber ohne Quelle meldet nichts.

`normalisieren` und `lebensdauer_schritt` sind eine wortgleiche Kopie der Server-Seite
(heizungsserver.regelung.hersteller.stoerungen); der Contract-Check haelt beide zusammen. Der Zustand der Zaehlung liegt nur
im Speicher: nach einem Neustart bleiben die offenen Meldungen (BridgeState.notify_states, notify_meldungen) offen und
schliessen beim Wegfall, eine noch nicht offene Stoerung zaehlt neu."""
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from smartheat_core import meldungen
from smartheat_core.meldungen import Meldung
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable

logger = logging.getLogger(__name__)

KEY = "erzeuger_stoerung"
STATE_OFFEN = "offen"
OFFEN_NACH_S = 15 * 60  # Sekunden ununterbrochenen Anstehens bis die Meldung offen ist
MAX_OFFEN_JE_KREIS = 8
MAX_ROH = 32  # Zeichen der normalisierten Kennung

_ERLAUBT = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_TRENNER = str.maketrans({".": "_", "-": "_"})


def normalisieren(roh: object) -> str | None:
    """Die Kennung in der Form `[a-z0-9_]{1,32}` oder `None`. Gross wird klein, `.` und `-` werden `_`; jedes andere Zeichen,
    auch Leerraum, Steuerzeichen und alles ausserhalb von ASCII, macht die Kennung unbrauchbar. Zu lange Kennungen werden
    nie gekuerzt (zwei Kennungen duerften sonst zusammenfallen)."""
    if not isinstance(roh, str) or not roh.isascii():
        return None
    normal = roh.lower().translate(_TRENNER)
    if not 1 <= len(normal) <= MAX_ROH or not _ERLAUBT.issuperset(normal):
        return None
    return normal


@dataclass(frozen=True)
class Anstehen:
    """Zustand der Lebensdauer eines Kreises: seit wann eine Kennung ununterbrochen ansteht und welche Meldungen offen sind.
    Wird nie veraendert, `lebensdauer_schritt` liefert den naechsten."""
    seit: Mapping[str, float] = field(default_factory=dict)
    offen: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Wechsel:
    """Was ein Schritt aendert: neu offene und geschlossene Meldungen, dazu die Kennungen, die 15 Minuten anstehen, aber wegen
    der Obergrenze nicht offen sind (nur fuers Protokoll)."""
    geoeffnet: frozenset[str] = frozenset()
    geschlossen: frozenset[str] = frozenset()
    zurueckgehalten: frozenset[str] = frozenset()


def lebensdauer_schritt(zustand: Anstehen, anstehend: Collection[str], jetzt: float) -> tuple[Anstehen, Wechsel]:
    """Naechster Zustand zu den Kennungen, die `jetzt` (Sekunden, monoton oder Epoche) anstehen. Eine Kennung, die einmal
    fehlt, verliert ihre Zaehlung; kehrt sie zurueck, beginnt sie neu und bekommt eine neue Meldung. Springt die Uhr zurueck,
    beginnt die Zaehlung ebenfalls neu. Am Limit gewinnt die laenger anstehende Kennung (bei Gleichstand die kleinere)."""
    jetzt_menge = frozenset(anstehend)
    seit = {code: min(zustand.seit.get(code, jetzt), jetzt) for code in jetzt_menge}
    geschlossen = zustand.offen - jetzt_menge
    offen = set(zustand.offen & jetzt_menge)
    reif = sorted((code for code in jetzt_menge - offen if jetzt - seit[code] >= OFFEN_NACH_S),
                  key=lambda code: (seit[code], code))
    geoeffnet = set()
    for code in reif:
        if len(offen) < MAX_OFFEN_JE_KREIS:
            offen.add(code)
            geoeffnet.add(code)
    wechsel = Wechsel(frozenset(geoeffnet), frozenset(geschlossen), frozenset(reif) - frozenset(geoeffnet))
    return Anstehen(seit, frozenset(offen)), wechsel


@dataclass(frozen=True)
class Stand:
    """Zaehlung und die offenen Meldungen (Kennung -> Meldung) dieses Prozesses."""
    anstehen: Anstehen = field(default_factory=Anstehen)
    offen: Mapping[str, Meldung] = field(default_factory=dict)


def _wiederherstellen(rt) -> Stand:
    """Die offenen Meldungen aus der Ablage des Notifiers: ein Neustart ueberlebt sie, die Zaehlung beginnt neu."""
    offen: dict[str, Meldung] = {}
    zustaende = rt.store.state.notify_states
    for key, state in zustaende.items():
        kennung = key.removeprefix(KEY + ":")
        if state == STATE_OK or kennung == key:
            continue
        meldung = meldungen.aus_dict(rt.store.state.notify_meldungen.get(key))
        offen[kennung] = meldung or meldungen.neu("erzeuger.stoerung", roh_text=kennung, quelle="unbekannt")
    return Stand(Anstehen(offen=frozenset(offen)), offen)


def _stand(rt) -> Stand:
    if rt.erzeuger is None:
        rt.erzeuger = _wiederherstellen(rt)
    return rt.erzeuger


def _lesen(rt, ref: str) -> dict[str, Meldung] | None:
    """Kennung -> Meldung der anstehenden Eintraege; `None`, wenn die Quelle nicht antwortet (dann bleibt alles stehen)."""
    try:
        eintraege = rt.signals.get_stoerungen(ref)
    except SourceUnavailable:
        logger.warning("Pruefung der Erzeugerstoerungen abgebrochen, Quelle nicht erreichbar")
        return None
    except (SignalNotFound, ValueError, KeyError, TypeError):
        return {}
    anstehend: dict[str, Meldung] = {}
    for eintrag in eintraege:
        kennung = normalisieren(eintrag.kennung)
        if kennung is None:
            continue
        if kennung in anstehend and eintrag.hinweis:
            continue  # eine Stoerung geht dem Hinweis derselben Kennung vor
        code = "erzeuger.hinweis" if eintrag.hinweis else "erzeuger.stoerung"
        anstehend[kennung] = meldungen.neu(code, roh_text=kennung, quelle=eintrag.quelle)
    return anstehend


def _stufen_abgleichen(rt, stand: Stand, anstehend: Mapping[str, Meldung]) -> Stand:
    """Gleicht die offenen Meldungen mit dem ab, was jetzt ansteht. Fehlt einer offenen Kennung die gueltige Ablage
    (Bestand ohne Codes), meldet der Notifier sie mit der echten Meldung neu. Wechselt sonst die Art (Wartungshinweis zu
    Stoerung oder zurueck), schliesst die alte Meldung ohne Push und die neue Art zaehlt ihre eigenen 15 Minuten."""
    offen = dict(stand.offen)
    seit = dict(stand.anstehen.seit)
    geoeffnet = set(stand.anstehen.offen)
    for kennung in sorted(stand.offen):
        neu = anstehend.get(kennung)
        if neu is None:
            continue
        schluessel = f"{KEY}:{kennung}"
        if rt.notifier.notify(schluessel, STATE_OFFEN, neu, critical=True):
            offen[kennung] = neu
        elif neu.code != stand.offen[kennung].code:
            rt.notifier.notify(schluessel, STATE_OK, stand.offen[kennung], critical=True, silent_ok=True)
            del offen[kennung]
            seit.pop(kennung, None)
            geoeffnet.discard(kennung)
    return Stand(Anstehen(seit, frozenset(geoeffnet)), offen)


def check(rt) -> None:
    ref = rt.manifest.refs.get("fault_codes")
    stand = _stand(rt)
    if ref is None:
        anstehend: dict[str, Meldung] = {}
    else:
        gelesen = _lesen(rt, ref)
        if gelesen is None:
            return
        anstehend = gelesen
    stand = _stufen_abgleichen(rt, stand, anstehend)
    anstehen, wechsel = lebensdauer_schritt(stand.anstehen, anstehend, rt.clock())
    offen = {kennung: meldung for kennung, meldung in stand.offen.items() if kennung in anstehen.offen}
    for kennung in sorted(wechsel.geschlossen):
        rt.notifier.notify(f"{KEY}:{kennung}", STATE_OK, stand.offen[kennung], critical=True, silent_ok=True)
    for kennung in sorted(wechsel.geoeffnet):
        offen[kennung] = anstehend[kennung]
        rt.notifier.notify(f"{KEY}:{kennung}", STATE_OFFEN, anstehend[kennung], critical=True)
    if wechsel.zurueckgehalten:
        logger.debug("%d Erzeugermeldungen wegen der Obergrenze zurueckgehalten", len(wechsel.zurueckgehalten))
    rt.erzeuger = Stand(anstehen, offen)


def kennung(rt) -> str | None:
    """Die aelteste offene Stoerung (kein Hinweis) fuer die Telemetrie, sonst `None`."""
    if rt.manifest.refs.get("fault_codes") is None:
        return None
    stand = _stand(rt)
    stoerungen = [k for k, meldung in stand.offen.items() if meldung.code == "erzeuger.stoerung"]
    if not stoerungen:
        return None
    return min(stoerungen, key=lambda k: (stand.anstehen.seit.get(k, float("inf")), k))
