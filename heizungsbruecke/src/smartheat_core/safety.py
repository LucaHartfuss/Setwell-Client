"""Lokale Sicherheitswerte je (Hebelsatz x Verteilsystem) (Regel 4). Bleiben bewusst auf dem Client und
kommen nie vom Server. Jedes Profil des Servers hat hier eine Zeile, und seine Server-Bereiche liegen innerhalb
(Contract-Check, Vertrag V4); die Verteilsysteme spiegeln VERTEILSYSTEME_MIT_LOKALEN_SICHERHEITSWERTEN im Server (je
Seite gepinnt: tests/test_core_safety.py, Server tests/generic/test_profiles.py). Fehlen Werte, verweigert der Client
den Start, bis der Nutzer Werte freigibt."""
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from smartheat_core.levers import LEVER_SETS

# Betriebsarten (Absenkung, Leerstand, Wochenplan): Untergrenzen und Dauern liegen im Geraet und kommen nie vom Server.
# Sie gelten zusaetzlich zum Bedienbereich und zu room_setpoint des Hebelsatzes (der engere Bereich gewinnt) und aendern
# keine bestehende Grenze. Der Abstand zum Kaelteschutz bleibt: keine Betriebsart liegt unter 15 Grad.
ABSENK_UNTERGRENZE_C = 16.0
LEERSTAND_UNTERGRENZE_C = 15.0
LEERSTAND_VORGABE_C = 16.0
PHASE_MINDESTDAUER = timedelta(minutes=60)
LEERSTAND_HOECHSTDAUER = timedelta(days=365)
#: Absenkung der kuehlen Phase: 0 bis 5 K in Schritten von 0,5 K (Kopie aus smartheat_device.wire: ABSENKUNG_K_MIN/MAX/STEP,
#: gepinnt in test_core_vertrag_pin.py). Tiefer geht keine Absenkung, auch wenn ABSENK_UNTERGRENZE_C es erlaubte.
ABSENKUNG_MIN_K = 0.0
ABSENKUNG_MAX_K = 5.0
ABSENKUNG_SCHRITT_K = 0.5
#: Uebersteuerung waehrend des Leerstands: hoechstens 4 h; die dritte Aenderung innerhalb von 24 h beendet den Leerstand
#: (Zaehler und Entscheidung liegen im Geraet, nie auf dem Server).
UEBERSTEUERUNG_LEERSTAND_HOECHSTDAUER = timedelta(hours=4)
LEERSTAND_AENDERUNGEN_BEENDEN = 3
LEERSTAND_AENDERUNGEN_FENSTER = timedelta(hours=24)
#: Vorschlag der Abwesenheitstemperatur je Verteilsystem (Voreinstellung fuer den Kunden, keine Grenze).
ABWESENHEIT_VORSCHLAG_C: Mapping[str, float] = MappingProxyType({"Heizkoerper": 17.0, "Fussbodenheizung": 19.0})


@dataclass(frozen=True)
class LocalSafety:
    """ranges: Bereich je Hebel des Hebelsatzes (bei Vaillant auch der Mindestvorlauf: ihn liefert der Server, lokal
    wird er nur begrenzt).
    comfort_boost: Comfort-Boost-Werte je Hebel; leer = kein Comfort-Boost (Fussbodenheizung).
    emergency_boost_levers: der Notfall-Boost setzt diese Hebel auf ihr lokales Maximum.
    arrival_threshold_k: Ankunftsschwelle des Comfort-Boosts und Austrittsschwelle des Notfall-Boosts -- nicht die
    Ausloese-Schwelle (der Comfort-Boost startet nur bei einer Erhoehung des Raum-Solls, boost.decide_boost)."""

    ranges: Mapping[str, tuple[float, float]]
    comfort_boost: Mapping[str, float]
    emergency_boost_levers: tuple[str, ...]
    arrival_threshold_k: float


LOCAL_SAFETY: dict[tuple[str, str], LocalSafety] = {
    # Vaillant x Heizkoerper: Regel 4, Nutzer-Freigaben 2026-09-29 (Steigung, Wunschtemperatur, Mindestvorlauf,
    # Boost), 2026-09-30 (Heizgrenze) und 2026-10-01 (Heizgrenze bis 23 °C).
    # Die Heizgrenze lokal liegt bei allen Profilen ab 10 Grad (Untergrenze, der Server kann sie nicht tiefer setzen).
    # Der Kaelteschutz (emergency_boost.kaelteschutz_entscheiden) hebt auf das Maximum dieser Bereiche; als Raum-Ist
    # gilt -10 bis 35 Grad (smartheat_runtime.plausibility), alles andere ist ein Fuehlerfehler.
    ("vaillant_vrc720", "Heizkoerper"): LocalSafety(
        ranges={
            "curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (10.0, 23.0), "min_flow": (20.0, 30.0),
        },
        comfort_boost={"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    ),
    # Regel 4 (Nutzer-Freigabe 2026-10-02): Heizkoerper fuer die neuen Hebelsaetze; Comfort-Boost auf
    # die lokalen Maxima aller gesendeten Hebel. Ankunftsschwelle 0,5 K wie Vaillant (Annahme).
    ("weishaupt_wwp", "Heizkoerper"): LocalSafety(
        ranges={"curve": (0.30, 1.00), "room_setpoint": (16.0, 25.0), "heat_limit": (10.0, 23.0)},
        comfort_boost={"curve": 1.00, "room_setpoint": 25.0, "heat_limit": 23.0},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    ),
    ("weishaupt_wwp_basis", "Heizkoerper"): LocalSafety(
        ranges={"room_setpoint": (16.0, 25.0)},
        comfort_boost={"room_setpoint": 25.0},
        emergency_boost_levers=("room_setpoint",),
        arrival_threshold_k=0.5,
    ),
    ("viessmann_vicare", "Heizkoerper"): LocalSafety(
        ranges={"curve": (0.4, 1.4), "level": (-13.0, 3.0), "room_setpoint": (15.0, 25.0)},
        comfort_boost={"curve": 1.4, "level": 3.0, "room_setpoint": 25.0},
        emergency_boost_levers=("curve", "level", "room_setpoint"),
        arrival_threshold_k=0.5,
    ),
    # Regel 4 (Nutzer-Freigabe 2026-10-02): Fussbodenheizung -- alle Hebel am lokalen Maximum ergeben
    # bei -15 °C hoechstens 45 °C Vorlauf; kein Comfort-Boost (Estrich), der Notfall-Boost setzt alle gesendeten Hebel
    # auf ihr lokales Maximum. Mindestvorlauf (nur Vaillant) 20-30 wie bei Heizkoerpern. Die Profile bleiben inaktiv.
    ("vaillant_vrc720", "Fussbodenheizung"): LocalSafety(
        ranges={
            "curve": (0.10, 0.35), "room_setpoint": (15.0, 24.0), "heat_limit": (10.0, 20.0), "min_flow": (20.0, 30.0),
        },
        comfort_boost={},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    ),
    ("weishaupt_wwp", "Fussbodenheizung"): LocalSafety(
        ranges={"curve": (0.10, 0.50), "room_setpoint": (16.0, 24.0), "heat_limit": (10.0, 20.0)},
        comfort_boost={},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    ),
    ("weishaupt_wwp_basis", "Fussbodenheizung"): LocalSafety(
        ranges={"room_setpoint": (16.0, 24.0)},
        comfort_boost={},
        emergency_boost_levers=("room_setpoint",),
        arrival_threshold_k=0.5,
    ),
    ("viessmann_vicare", "Fussbodenheizung"): LocalSafety(
        ranges={"curve": (0.2, 0.4), "level": (-5.0, 3.0), "room_setpoint": (15.0, 24.0)},
        comfort_boost={},
        emergency_boost_levers=("curve", "level", "room_setpoint"),
        arrival_threshold_k=0.5,
    ),
}


#: Obergrenze der Temperatur von Abwesend, Leerstand und Uebersteuerung: das hoechste `room_setpoint` des Bedienbereichs
#: aller Profile (derselbe Bereich, den der Wunsch-Eingang kennt). Ein Wert darueber ist ungueltig, das Soll faellt dann
#: auf Plan bzw. Wunsch zurueck; der Bereich des gewaehlten Hebelsatzes begrenzt beim Schreiben weiter.
BEDIENBEREICH_MAX_C = max(profil.ranges["room_setpoint"][1] for profil in LOCAL_SAFETY.values())


def check_invariants(safety: LocalSafety, lever_set_id: str, verteilsystem: str) -> None:
    label = f"Hebelsatz '{lever_set_id}', Verteilsystem '{verteilsystem}'"
    lever_set = LEVER_SETS.get(lever_set_id)
    if lever_set is None:
        raise ValueError(f"{label}: unbekannter Hebelsatz")
    needed = set(lever_set.levers)
    if set(safety.ranges) != needed:
        raise ValueError(f"{label}: Bereiche fuer {sorted(safety.ranges)} statt {sorted(needed)}")
    for lever, (low, high) in safety.ranges.items():
        if low > high:
            raise ValueError(f"{label}: {lever} Minimum {low} groesser als Maximum {high}")
    for lever, value in safety.comfort_boost.items():
        if lever not in lever_set.levers:
            raise ValueError(f"{label}: Comfort-Boost {lever} ist kein gesendeter Hebel")
        low, high = safety.ranges[lever]
        if not low <= value <= high:
            raise ValueError(f"{label}: Comfort-Boost {lever}={value} ausserhalb [{low}, {high}]")
    unknown = sorted(set(safety.emergency_boost_levers) - set(lever_set.levers))
    if unknown:
        raise ValueError(f"{label}: Notfall-Boost auf unbekannte Hebel {', '.join(unknown)}")
    if safety.arrival_threshold_k <= 0:
        raise ValueError(f"{label}: Ankunftsschwelle {safety.arrival_threshold_k} muss positiv sein")


def resolve_local_safety(lever_set_id: str, verteilsystem) -> LocalSafety:
    """Kein stiller Rueckfall: fehlende Werte sind ein Fehler. Der Text fuer ein unbekanntes Verteilsystem ist der des
    Konfigurationsfehlers bis Add-on 0.29.0 (Kundentext)."""
    if lever_set_id not in LEVER_SETS:
        raise ValueError(f"keine lokalen Sicherheitswerte für Hebelsatz '{lever_set_id}'")
    safety = LOCAL_SAFETY.get((lever_set_id, verteilsystem)) if isinstance(verteilsystem, str) else None
    if safety is None:
        raise ValueError(f"keine lokalen Sicherheitswerte für Verteilsystem '{verteilsystem}'")
    check_invariants(safety, lever_set_id, verteilsystem)
    return safety


def _endliche_zahl(wert) -> bool:
    if not isinstance(wert, int | float) or isinstance(wert, bool):
        return False
    try:
        return math.isfinite(wert)
    except OverflowError:  # Ganzzahl ausserhalb des Gleitkommabereichs
        return False


def absenkung_k_gueltig(absenkung_k) -> bool:
    """Die Absenkung liegt von ABSENKUNG_MIN_K bis ABSENKUNG_MAX_K und ist ein Vielfaches von ABSENKUNG_SCHRITT_K."""
    return (_endliche_zahl(absenkung_k) and ABSENKUNG_MIN_K <= absenkung_k <= ABSENKUNG_MAX_K
            and absenkung_k % ABSENKUNG_SCHRITT_K == 0)


def absenktemperatur(wunsch_c, absenkung_k) -> float:
    """Kuehles Soll einer Absenkung: nie unter ABSENK_UNTERGRENZE_C, nie tiefer als ABSENKUNG_MAX_K unter dem Wunsch und nie
    waermer als der Wunsch. Eine Absenkung von 0 K laesst den Wunsch; liegt der Wunsch selbst unter der Untergrenze, bleibt
    er (es wird nie angehoben). Unbrauchbare Eingaben (keine endliche Zahl, negative Tiefe) sind ein Fehler: der Aufrufer
    faellt dann auf den Wunsch zurueck, die warme Richtung."""
    if not _endliche_zahl(wunsch_c) or not _endliche_zahl(absenkung_k) or absenkung_k < 0:
        raise ValueError(f"Absenkung aus Wunsch {wunsch_c!r} und Tiefe {absenkung_k!r} unbrauchbar")
    return min(float(wunsch_c), max(wunsch_c - min(absenkung_k, ABSENKUNG_MAX_K), ABSENK_UNTERGRENZE_C))


def _temperatur_im_bereich(temp_c, untergrenze_c: float) -> bool:
    return _endliche_zahl(temp_c) and untergrenze_c <= temp_c <= BEDIENBEREICH_MAX_C


def leerstand_temperatur_gueltig(temp_c) -> bool:
    """Die Leerstandstemperatur liegt von LEERSTAND_UNTERGRENZE_C bis BEDIENBEREICH_MAX_C."""
    return _temperatur_im_bereich(temp_c, LEERSTAND_UNTERGRENZE_C)


def abwesend_temperatur_gueltig(temp_c) -> bool:
    """Die Abwesenheitstemperatur liegt von LEERSTAND_UNTERGRENZE_C bis BEDIENBEREICH_MAX_C."""
    return _temperatur_im_bereich(temp_c, LEERSTAND_UNTERGRENZE_C)


def uebersteuerung_temperatur_gueltig(temp_c) -> bool:
    """Die Uebersteuerung der kuehlen Phase geht nicht unter die Absenk-Untergrenze und nicht ueber BEDIENBEREICH_MAX_C."""
    return _temperatur_im_bereich(temp_c, ABSENK_UNTERGRENZE_C)


def _zeiten_in_utc(*zeiten) -> tuple[datetime, ...] | None:
    """Die Zeitpunkte in UTC; None, wenn einer keine Zone hat, kein Zeitpunkt ist oder am Rand des Bereichs liegt."""
    if not all(isinstance(zeit, datetime) and zeit.utcoffset() is not None for zeit in zeiten):
        return None
    try:
        return tuple(zeit.astimezone(UTC) for zeit in zeiten)
    except OverflowError:  # Zeitpunkt am Rand des darstellbaren Bereichs
        return None


def leerstand_bis_gueltig(gesetzt: datetime, bis: datetime, jetzt: datetime) -> bool:
    """`bis` liegt nach dem Setzen und nach `jetzt`, jeweils hoechstens LEERSTAND_HOECHSTDAUER davon entfernt. `jetzt`
    ist die Geraeteuhr, vom Aufrufer uebergeben: `gesetzt` schreibt der Server, ein zu spaet gesetztes `gesetzt`
    verlaengert die Hoechstdauer ab jetzt nicht (beide Grenzen gelten zugleich). Alle drei
    Zeitpunkte brauchen eine Zone (`utcoffset()` nicht None); die Rechnung ist absolut in UTC, nie in Wandzeit."""
    zeiten = _zeiten_in_utc(gesetzt, bis, jetzt)
    if zeiten is None:
        return False
    gesetzt_utc, bis_utc, jetzt_utc = zeiten
    return (timedelta(0) < bis_utc - gesetzt_utc <= LEERSTAND_HOECHSTDAUER
            and timedelta(0) < bis_utc - jetzt_utc <= LEERSTAND_HOECHSTDAUER)


def abwesend_bis_gueltig(bis: datetime, jetzt: datetime) -> bool:
    """`bis` liegt nach `jetzt` und hoechstens LEERSTAND_HOECHSTDAUER (365 Tage) davon entfernt. `jetzt` ist die
    Geraeteuhr, vom Aufrufer uebergeben; beide Zeitpunkte brauchen eine Zone, die Rechnung ist absolut in UTC."""
    zeiten = _zeiten_in_utc(bis, jetzt)
    if zeiten is None:
        return False
    bis_utc, jetzt_utc = zeiten
    return timedelta(0) < bis_utc - jetzt_utc <= LEERSTAND_HOECHSTDAUER


def phasendauer_gueltig(dauer: timedelta) -> bool:
    """Jede Phase des Wochenplans dauert mindestens PHASE_MINDESTDAUER."""
    return isinstance(dauer, timedelta) and dauer >= PHASE_MINDESTDAUER


def abwesenheit_vorschlag(verteilsystem) -> float:
    """Vorschlag der Abwesenheitstemperatur je Verteilsystem; kein stiller Rueckfall fuer unbekannte Systeme."""
    vorschlag = ABWESENHEIT_VORSCHLAG_C.get(verteilsystem) if isinstance(verteilsystem, str) else None
    if vorschlag is None:
        raise ValueError(f"keine lokalen Sicherheitswerte für Verteilsystem '{verteilsystem}'")
    return vorschlag
