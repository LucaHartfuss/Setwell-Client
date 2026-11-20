"""Eingabe des geltenden Solls je Kreis aus den Dokumenten und die Rechnung mit Rueckfall auf den Wunsch.

Die Regelung liest das Soll nur ueber `geltendes_soll`: die reine Rechnung (`smartheat_core.plan.soll_effektiv`) bekommt
den Wunsch aus dem Speicher des Kreises (das Thermostat bleibt die Wahrheit des Wunsches), nicht den `raum_soll` des
Dokuments. Die Dokumente werden bei jedem Check frisch gelesen; fehlt die Zeitzone der Anlage oder der Kreis, gibt es
keine Planeingabe, und das Soll ist der Wunsch wie ohne Plan."""
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from smartheat_core import kreisansicht
from smartheat_core.plan import SOLL_ART_ABWESEND, SOLL_ART_LEERSTAND, SOLL_ART_WUNSCH, SollErgebnis, soll_effektiv

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanEingabe:
    """Was die Rechnung des Kreises aus den Dokumenten braucht: die Zeitzone der Anlage und die Eintraege des Kreises
    aus Bedienung und Konfiguration."""

    zone: str
    bedienung: Mapping
    konfiguration: Mapping


def _eintrag(dokument, kreis: int) -> Mapping | None:
    """Der Eintrag des Kreises (`kreise[n].kreis`); eine Wahrheitszahl ist keine Kreisnummer."""
    for eintrag in kreisansicht.kreise(dokument):
        nummer = eintrag.get("kreis")
        if type(nummer) is int and nummer == kreis:
            return eintrag
    return None


def plan_eingabe(konfiguration, bedienung, kreis: int) -> PlanEingabe | None:
    """Die Planeingabe des Kreises oder None, wenn die Dokumente sie nicht hergeben (keine Zeitzone, kein Eintrag des
    Kreises in Konfiguration oder Bedienung). Prueft nichts weiter: eine unbrauchbare Zone faellt in `geltendes_soll`."""
    zone = konfiguration.get("zeitzone") if isinstance(konfiguration, Mapping) else None
    if not isinstance(zone, str) or not zone:
        return None
    kreis_konfiguration = _eintrag(konfiguration, kreis)
    kreis_bedienung = _eintrag(bedienung, kreis)
    if kreis_konfiguration is None or kreis_bedienung is None:
        return None
    return PlanEingabe(zone=zone, bedienung=dict(kreis_bedienung), konfiguration=dict(kreis_konfiguration))


def geltendes_soll(eingabe: PlanEingabe | None, wunsch: float | None, jetzt: datetime, *,
                   uhr_vertrauenswuerdig: bool, soll_quelle_aus: bool) -> SollErgebnis:
    """Das Soll, nach dem Kaelteschutz, Notfall-Boost und Comfort-Boost rechnen. Bei Soll-Quelle aus gibt es keines
    (dann gilt der Wunsch nicht, die Quelle ist aus). Ohne Planeingabe oder ohne Wunsch ist es der Wunsch. Kann die
    Rechnung nicht laufen (unbrauchbare Zone oder Dokumentwerte), gilt ebenfalls der Wunsch: die Regelung bleibt bei dem,
    was das Thermostat sagt, und der Fehler steht als Art im Log, nie mit dem Wert aus dem Dokument."""
    if soll_quelle_aus or wunsch is None:
        return SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None)
    nur_wunsch = SollErgebnis(soll=float(wunsch), soll_art=SOLL_ART_WUNSCH, naechster_wechsel=None)
    if eingabe is None:
        return nur_wunsch
    try:
        return soll_effektiv(jetzt, eingabe.zone, {**eingabe.bedienung, "raum_soll": wunsch}, eingabe.konfiguration,
                             uhr_vertrauenswuerdig=uhr_vertrauenswuerdig, soll_quelle_aus=False)
    except ValueError as fehler:
        logger.warning("Plan nicht berechenbar (%s), es gilt der Wunsch", type(fehler).__name__)
        return nur_wunsch


@dataclass(frozen=True)
class Betriebsart:
    """Die laufende Betriebsart des Kreises (`leerstand` oder `abwesend`) und das Soll der Phase, die nach ihrem Ende gilt
    (Plan ohne Betriebsart und ohne Uebersteuerung). `bis`: das Ende des Leerstands (bei Away None); eine Uebersteuerung
    im Leerstand reicht nicht darueber hinaus."""

    art: str
    danach: SollErgebnis | None
    bis: datetime | None = None


_ZEIT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _leerstand_ende(bedienung: Mapping) -> datetime | None:
    """Das Ende des Leerstands aus dem Bedienungsdokument (UTC-Text); None, wenn es fehlt oder unbrauchbar ist."""
    leerstand = bedienung.get("leerstand")
    text = leerstand.get("bis") if isinstance(leerstand, Mapping) else None
    try:
        return datetime.strptime(text, _ZEIT_FORMAT).replace(tzinfo=UTC) if isinstance(text, str) else None
    except ValueError:
        return None


def betriebsart_lage(eingabe: PlanEingabe | None, wunsch: float | None, jetzt: datetime, *,
                     uhr_vertrauenswuerdig: bool) -> Betriebsart | None:
    """Laeuft jetzt Leerstand oder Away (auch unter einer Uebersteuerung)? Dann die Art und das Soll danach, sonst None.
    Ohne Planeingabe, ohne Wunsch, ohne vertrauenswuerdige Uhr oder bei unbrauchbaren Dokumentwerten gibt es keine
    Betriebsart: die Eingabe laeuft den gewohnten Weg."""
    if eingabe is None or wunsch is None or uhr_vertrauenswuerdig is not True:
        return None
    ohne_ueb = {schluessel: wert for schluessel, wert in eingabe.bedienung.items() if schluessel != "uebersteuerung"}
    try:
        laufend = soll_effektiv(jetzt, eingabe.zone, {**ohne_ueb, "raum_soll": wunsch}, eingabe.konfiguration,
                                uhr_vertrauenswuerdig=True, soll_quelle_aus=False)
        if laufend.soll_art not in (SOLL_ART_LEERSTAND, SOLL_ART_ABWESEND):
            return None
        danach = soll_effektiv(jetzt, eingabe.zone, {**ohne_ueb, "raum_soll": wunsch, "modus": "wunsch"},
                               eingabe.konfiguration, uhr_vertrauenswuerdig=True, soll_quelle_aus=False)
    except ValueError:
        return None
    ende = _leerstand_ende(eingabe.bedienung) if laufend.soll_art == SOLL_ART_LEERSTAND else None
    return Betriebsart(art=laufend.soll_art, danach=danach, bis=ende)


def eingabe_des_geraets(geraet, kreis: int, speicher=None) -> PlanEingabe | None:
    """Die Planeingabe des Kreises aus den laufenden Dokumenten des Geraets (`konfiguration()`, `bedienung()`), bei jedem
    Aufruf frisch gelesen. Kennt die Bedienung keine Uebersteuerung, gilt die lokale aus dem Soll-Speicher des Kreises
    (`speicher.uebersteuerung()`): sie wirkt sofort und uebersteht einen Neustart, ein Eintrag des Servers geht vor."""
    eingabe = plan_eingabe(geraet.konfiguration(), geraet.bedienung()[0], kreis)
    if eingabe is None or speicher is None or isinstance(eingabe.bedienung.get("uebersteuerung"), Mapping):
        return eingabe
    lokal = speicher.uebersteuerung()
    if lokal is None:
        return eingabe
    return PlanEingabe(zone=eingabe.zone, bedienung={**eingabe.bedienung, "uebersteuerung": lokal},
                       konfiguration=eingabe.konfiguration)
