"""Eingabe des geltenden Solls je Kreis aus den Dokumenten und die Rechnung mit Rueckfall auf den Wunsch.

Die Regelung liest das Soll nur ueber `geltendes_soll`: die reine Rechnung (`smartheat_core.plan.soll_effektiv`) bekommt
den Wunsch aus dem Speicher des Kreises (das Thermostat bleibt die Wahrheit des Wunsches), nicht den `raum_soll` des
Dokuments. Die Dokumente werden bei jedem Check frisch gelesen; fehlt die Zeitzone der Anlage oder der Kreis, gibt es
keine Planeingabe, und das Soll ist der Wunsch wie ohne Plan."""
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from smartheat_core import kreisansicht
from smartheat_core.plan import SOLL_ART_WUNSCH, SollErgebnis, soll_effektiv

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


def eingabe_des_geraets(geraet, kreis: int) -> PlanEingabe | None:
    """Die Planeingabe des Kreises aus den laufenden Dokumenten des Geraets (`konfiguration()`, `bedienung()`), bei jedem
    Aufruf frisch gelesen."""
    return plan_eingabe(geraet.konfiguration(), geraet.bedienung()[0], kreis)
