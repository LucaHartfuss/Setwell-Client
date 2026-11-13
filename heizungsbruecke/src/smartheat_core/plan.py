"""Soll eines Heizkreises aus Bedienung und Konfiguration, rein: nur Standardbibliothek, keine Uhr, keine Datei.

`soll_effektiv` bekommt die Zeit von aussen (`jetzt` mit Zeitzone) und rechnet in der Zone der Anlage, nie in der des
Hosts. Vorrang von oben nach unten: Leerstand, Abwesend, Plan kuehl (Absenktemperatur), Wunsch bzw. Plan warm. Der
Notbetrieb steht darueber, er kennt kein Soll und wirkt nur ueber die Hebel. Ohne Wochenplan und ohne passende
Ueberlagerung ist das Soll der gemerkte Wunsch, `soll_art` ist `wunsch`, einen naechsten Wechsel gibt es nicht. Steht
die Soll-Quelle auf aus oder Frostschutz, gibt es kein Soll (dann wirkt nur der Frostnaehe-Ausloeser des Geraets).

Die Funktion begrenzt den Wunsch nicht: der Bereich gilt am Wunsch-Eingang und in der Schreibpipeline. Alles, was
darunter faellt, nimmt sie nie unter eine Untergrenze aus `safety.py` (Absenktemperatur, Leerstand, Abwesend,
Uebersteuerung). Ein Teil der Bedienung, der nicht lesbar ist oder die Grenzen verletzt (Wochenplan, `abwesend`,
`leerstand`, `uebersteuerung`, ein unbrauchbarer Wert der Konfiguration), gilt wie nicht gesetzt: das Soll faellt dann
in die warme Richtung auf Plan oder Wunsch zurueck. Ob ein Dokument als Ganzes gueltig ist, entscheidet der
Dokument-Pruefer, nicht diese Rechnung. Ein Fehler (`ValueError`) bleibt `jetzt`, `zone`, `konfiguration` und dem
Wunsch vorbehalten.

Sommerzeit: die Zeiten des Plans sind Wanduhrzeiten der Zone. Sie werden je Tag in Zeitpunkte (UTC) umgerechnet; eine
Zeit, die es nicht gibt (Fruehjahr), gilt ab dem ersten gueltigen Moment danach, eine doppelte (Herbst) beim ersten
Vorkommen. Die Wochentage zaehlen nach ISO: 1 ist Montag, 7 Sonntag."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from smartheat_core.safety import (
    LEERSTAND_UNTERGRENZE_C,
    absenktemperatur,
    leerstand_bis_gueltig,
    leerstand_temperatur_gueltig,
    phasendauer_gueltig,
)
from smartheat_core.zahlen import ist_endliche_zahl

#: Quelle des geltenden Solls (Telemetrie `soll_art`).
SOLL_ART_WUNSCH = "wunsch"
SOLL_ART_ABSENKUNG = "absenkung"
SOLL_ART_AUFHEIZEN = "aufheizen"
SOLL_ART_ABWESEND = "abwesend"
SOLL_ART_LEERSTAND = "leerstand"
SOLL_ART_UEBERSTEUERT = "uebersteuert"

_TAG_MINUTEN = 24 * 60
_WOCHE_MINUTEN = 7 * _TAG_MINUTEN
_EINTRAG_FELDER = frozenset({"tag", "warm_ab", "kuehl_ab"})
#: Tage vor und nach dem Datum eines Zeitpunkts, fuer die der Plan in Zeitpunkte umgerechnet wird: mehr als eine
#: Woche, damit jede Phase um den Zeitpunkt vollstaendig dabei ist.
_VORLAUF_TAGE = 9
_NACHLAUF_TAGE = 10
#: So weit voraus sucht die Rechnung nach dem naechsten Wechsel des Plans, wenn kein Zeitpunkt der Ueberlagerung
#: dazwischenkommt: der Plan wiederholt sich jede Woche.
_PLAN_SUCHTAGE = 8


@dataclass(frozen=True)
class SollErgebnis:
    """`soll` None = kein Soll (Soll-Quelle aus oder Frostschutz), dann ist auch `soll_art` None.
    `naechster_wechsel`: der erste Zeitpunkt nach `jetzt` (UTC), an dem sich Soll oder `soll_art` aendern; None, wenn
    sich nichts mehr aendert. `uhr_unklar`: die Uhr war nicht vertrauenswuerdig, obwohl eine Betriebsart gesetzt ist, die
    deshalb ruht (Zustand fuer die Meldung, kein Text); dann ist das Soll der Wunsch und es gibt keinen naechsten
    Wechsel."""

    soll: float | None
    soll_art: str | None
    naechster_wechsel: datetime | None
    uhr_unklar: bool = False


def soll_effektiv(jetzt: datetime, zone: str, bedienung: Mapping[str, object], konfiguration: Mapping[str, object],
                  *, uhr_vertrauenswuerdig: bool, soll_quelle_aus: bool = False) -> SollErgebnis:
    """Soll des Kreises zu `jetzt`; `bedienung` ist der Eintrag des Kreises (`modus`, `raum_soll`, `wochenplan`,
    `abwesend`, `leerstand`, `uebersteuerung`), `konfiguration` der des Kreises (`absenkung_k`, `aufheizdauer_min`).
    `uhr_vertrauenswuerdig`: das Urteil der Uhr (`wallclock.vertrauenswuerdig`), ohne Vorgabe, damit es nie vergessen
    wird; nur ein echtes True gilt. Sonst ruhen Wochenplan, Abwesenheit, Leerstand und Uebersteuerung: das Soll ist der
    Wunsch (die warme Richtung), `uhr_unklar` zeigt, dass dabei etwas ruht. Die Wanduhr bestimmt dann nichts, auch
    nicht die Gueltigkeit eines Leerstands. `soll_quelle_aus`: die Soll-Quelle steht auf aus oder Frostschutz."""
    if not isinstance(jetzt, datetime) or jetzt.utcoffset() is None:
        raise ValueError("jetzt braucht eine Zeitzone")
    _zone_pruefen(zone)
    if not isinstance(konfiguration, Mapping):
        raise ValueError("konfiguration ist kein Objekt")
    wunsch = bedienung.get("raum_soll") if isinstance(bedienung, Mapping) else None
    if not isinstance(wunsch, int | float) or not ist_endliche_zahl(wunsch):
        raise ValueError("bedienung hat keinen brauchbaren Wunsch")
    if soll_quelle_aus:
        return SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None)
    if uhr_vertrauenswuerdig is not True:
        return SollErgebnis(soll=float(wunsch), soll_art=SOLL_ART_WUNSCH, naechster_wechsel=None,
                            uhr_unklar=_betriebsart_gesetzt(bedienung))
    rechnung = _Rechnung(ZoneInfo(zone), bedienung, konfiguration, float(wunsch), jetzt)
    zustand = rechnung.zustand(jetzt)
    return SollErgebnis(soll=zustand.soll, soll_art=zustand.art,
                        naechster_wechsel=rechnung.naechster_wechsel(jetzt, zustand))


def wochenplan_fehler(wochenplan: object) -> str | None:
    """Pruefung der Form eines Wochenplans; None = in Ordnung, sonst ein Code: `form` (kein Verzeichnis von sieben
    Eintraegen mit genau `tag`, `warm_ab`, `kuehl_ab`), `anzahl` (nicht sieben Eintraege), `tag` (die Tage 1 bis 7
    nicht genau einmal), `zeit` (keine Uhrzeit `HH:MM`, `24:00` gibt es nicht), `gleiche_zeit` (warm und kuehl zur
    selben Uhrzeit), `wechselfolge` (die 14 Wechsel der Woche, Sonntag zyklisch vor Montag, wechseln nicht streng
    zwischen warm und kuehl) und `phase_zu_kurz` (eine Phase kuerzer als die Mindestdauer aus `safety.py`). Liegt
    `kuehl_ab` vor `warm_ab`, beginnt die kuehle Phase am Folgetag (Nachtschicht). Die Phasen zaehlen in Minuten der
    Wanduhr, ohne Sommerzeit."""
    return _wochenplan_lesen(wochenplan)[1]


def anstieg_vom_kunden(vorher_art: str | None, jetzt_art: str | None) -> bool:
    """Kommt ein Anstieg des Solls vom Kunden? Ja bei `uebersteuert` und beim Wunsch, wenn zuvor schon der Wunsch (oder
    kein Soll) galt: dann hat der Kunde ihn erhoeht. Plan (Ende der Absenkung, Aufheizen), Ende von Abwesend und
    Leerstand und das Ende einer Uebersteuerung kommen vom Plan und sind nicht der Kunde."""
    if jetzt_art == SOLL_ART_UEBERSTEUERT:
        return True
    return jetzt_art == SOLL_ART_WUNSCH and vorher_art in (SOLL_ART_WUNSCH, None)


def _betriebsart_gesetzt(bedienung: Mapping[str, object]) -> bool:
    """Ist eine Betriebsart gesetzt, die ohne vertrauenswuerdige Uhr ruht? Rein nach der Form, nie nach der Wanduhr:
    ein lesbarer Wochenplan, oder eine Abwesenheit bzw. ein Leerstand im passenden Modus, oder eine Uebersteuerung."""
    modus = bedienung.get("modus")
    return (_wochenplan_lesen(bedienung.get("wochenplan"))[1] is None
            or (modus == "abwesend" and isinstance(bedienung.get("abwesend"), Mapping))
            or (modus == "leerstand" and isinstance(bedienung.get("leerstand"), Mapping))
            or isinstance(bedienung.get("uebersteuerung"), Mapping))


def _zone_pruefen(zone: object) -> None:
    if not isinstance(zone, str):
        raise ValueError("zone ist keine bekannte Zeitzone")
    try:
        ZoneInfo(zone)
    except (ValueError, LookupError, OSError):
        raise ValueError("zone ist keine bekannte Zeitzone") from None


# --- Wochenplan ---------------------------------------------------------------------------------------------------


def _minute_des_tages(wert: object) -> int | None:
    if not isinstance(wert, str) or len(wert) != 5 or not wert.isascii() or wert[2] != ":":
        return None
    stunde, minute = wert[:2], wert[3:]
    if not stunde.isdecimal() or not minute.isdecimal():
        return None
    stunden, minuten = int(stunde), int(minute)
    return stunden * 60 + minuten if stunden < 24 and minuten < 60 else None


def _wochenplan_lesen(wochenplan: object) -> tuple[dict[int, tuple[int, int]] | None, str | None]:
    """(Zeiten je Tag, Fehlercode): je ISO-Tag die Minuten von `warm_ab` und `kuehl_ab`."""
    if not isinstance(wochenplan, list | tuple):
        return None, "form"
    for eintrag in wochenplan:
        if not isinstance(eintrag, Mapping) or set(eintrag) != _EINTRAG_FELDER:
            return None, "form"
    if len(wochenplan) != 7:
        return None, "anzahl"
    tage = [eintrag["tag"] for eintrag in wochenplan]
    if any(isinstance(tag, bool) or not isinstance(tag, int) for tag in tage) or sorted(tage) != list(range(1, 8)):
        return None, "tag"
    zeiten: dict[int, tuple[int, int]] = {}
    for eintrag in wochenplan:
        warm, kuehl = _minute_des_tages(eintrag["warm_ab"]), _minute_des_tages(eintrag["kuehl_ab"])
        if warm is None or kuehl is None:
            return None, "zeit"
        zeiten[eintrag["tag"]] = (warm, kuehl)
    if any(warm == kuehl for warm, kuehl in zeiten.values()):
        return None, "gleiche_zeit"
    wechsel: list[tuple[int, int]] = []  # (Minute der Woche, 0 = warm, 1 = kuehl)
    for tag, (warm, kuehl) in zeiten.items():
        anfang = (tag - 1) * _TAG_MINUTEN
        wechsel.append((anfang + warm, 0))
        wechsel.append(((anfang + kuehl + (_TAG_MINUTEN if kuehl < warm else 0)) % _WOCHE_MINUTEN, 1))
    wechsel.sort()
    folgen = list(zip(wechsel, wechsel[1:] + wechsel[:1], strict=True))
    if any(vorher[1] == danach[1] for vorher, danach in folgen):
        return None, "wechselfolge"
    for vorher, danach in folgen:
        if not phasendauer_gueltig(timedelta(minutes=(danach[0] - vorher[0]) % _WOCHE_MINUTEN)):
            return None, "phase_zu_kurz"
    return zeiten, None


# --- Zeit ----------------------------------------------------------------------------------------------------------


def _zeitpunkt(wert: object) -> datetime | None:
    """Zeitpunkt in UTC aus einem Datum mit Zone oder aus Text `JJJJ-MM-TTTHH:MM:SSZ`; sonst None."""
    try:
        if isinstance(wert, datetime):
            return wert.astimezone(UTC) if wert.utcoffset() is not None else None
        if isinstance(wert, str) and len(wert) == 20 and wert.isascii():
            return datetime.strptime(wert, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None
    return None


def _minuten(wert: object) -> timedelta | None:
    """Dauer in Minuten; fehlend (None) ist null, unbrauchbar (nicht endlich, negativ, zu gross) ist None."""
    if wert is None:
        return timedelta(0)
    if not ist_endliche_zahl(wert) or wert < 0:
        return None
    try:
        return timedelta(minutes=wert)
    except OverflowError:
        return None


def _instant(wand: datetime, zone: ZoneInfo) -> datetime:
    """Zeitpunkt (UTC) einer Wanduhrzeit der Zone: beim ersten Vorkommen einer doppelten Zeit, ab dem ersten gueltigen
    Moment danach bei einer Zeit, die es nicht gibt."""
    erstes = wand.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if erstes.astimezone(zone).replace(tzinfo=None) == wand:
        return erstes
    zweites = wand.replace(tzinfo=zone, fold=1).astimezone(UTC)
    vorher, nachher = min(erstes, zweites), max(erstes, zweites)
    while nachher - vorher > timedelta(seconds=1):
        mitte = vorher + timedelta(seconds=int((nachher - vorher).total_seconds()) // 2)
        if mitte.astimezone(zone).replace(tzinfo=None) >= wand:
            nachher = mitte
        else:
            vorher = mitte
    return nachher


# --- Lesen der Ueberlagerungen -------------------------------------------------------------------------------------


def _raumtemperatur(wert: object) -> float | None:
    """Temperatur von Abwesend oder Uebersteuerung: endlich und mindestens die Untergrenze aus `safety.py`."""
    if ist_endliche_zahl(wert) and wert >= LEERSTAND_UNTERGRENZE_C:
        return float(wert)
    return None


@dataclass(frozen=True)
class _Fenster:
    """Leerstand (`von` bis `bis`) oder Abwesend (`von` None, bis `bis`)."""

    von: datetime | None
    bis: datetime
    temp: float
    aufheizen_ab: datetime | None


def _leerstand_lesen(wert: object, jetzt: datetime) -> _Fenster | None:
    if not isinstance(wert, Mapping):
        return None
    von, bis = _zeitpunkt(wert.get("von")), _zeitpunkt(wert.get("bis"))
    temp = wert.get("temp")
    if (von is None or bis is None or not isinstance(temp, int | float) or not leerstand_bis_gueltig(von, bis, jetzt)
            or not leerstand_temperatur_gueltig(temp)):
        return None
    return _Fenster(von, bis, float(temp), _zeitpunkt(wert.get("aufheizen_ab")))


def _abwesend_lesen(wert: object) -> _Fenster | None:
    if not isinstance(wert, Mapping):
        return None
    bis, temp = _zeitpunkt(wert.get("bis")), _raumtemperatur(wert.get("temp"))
    if bis is None or temp is None:
        return None
    return _Fenster(None, bis, temp, _zeitpunkt(wert.get("aufheizen_ab")))


def _uebersteuerung_lesen(wert: object) -> tuple[float, datetime] | None:
    if not isinstance(wert, Mapping):
        return None
    temp, bis = _raumtemperatur(wert.get("temp")), _zeitpunkt(wert.get("bis"))
    return (temp, bis) if temp is not None and bis is not None else None


@dataclass(frozen=True)
class _Zustand:
    soll: float
    art: str


class _Rechnung:
    """Eine Berechnung fuer einen Kreis: liest Bedienung und Konfiguration einmal und bewertet beliebige Zeitpunkte."""

    def __init__(self, zone: ZoneInfo, bedienung: Mapping[str, object], konfiguration: Mapping[str, object],
                 wunsch: float, jetzt: datetime) -> None:
        self._zone = zone
        self._wunsch = wunsch
        modus = bedienung.get("modus")
        zeiten = None
        if bedienung.get("wochenplan") is not None:
            zeiten, fehler = _wochenplan_lesen(bedienung.get("wochenplan"))
            if fehler is not None:
                zeiten = None
        self._zeiten = zeiten
        self._leerstand = _leerstand_lesen(bedienung.get("leerstand"), jetzt) if modus == "leerstand" else None
        self._abwesend = _abwesend_lesen(bedienung.get("abwesend")) if modus == "abwesend" else None
        self._uebersteuerung = _uebersteuerung_lesen(bedienung.get("uebersteuerung"))
        aufheizdauer = konfiguration.get("aufheizdauer_min")
        if aufheizdauer is None:
            plan_dauer, weg_dauer = timedelta(0), timedelta(0)
        elif isinstance(aufheizdauer, Mapping):
            plan_dauer, weg_dauer = _minuten(aufheizdauer.get("plan")), _minuten(aufheizdauer.get("weg")) or timedelta(0)
        else:
            plan_dauer, weg_dauer = None, timedelta(0)
        self._weg_dauer = weg_dauer
        self._absenk_temp = self._absenktemperatur(konfiguration.get("absenkung_k"))
        self._plan_dauer = plan_dauer
        self._hat_absenkung = zeiten is not None and self._absenk_temp is not None and plan_dauer is not None
        self._intervalle_je_tag: dict[date, list[tuple[datetime, datetime]]] = {}

    def _absenktemperatur(self, tiefe: object) -> float | None:
        if tiefe is None:
            return None
        try:
            temperatur = absenktemperatur(self._wunsch, tiefe)
        except ValueError:
            return None
        return temperatur if tiefe != 0 else None

    # --- Plan ---

    def _intervalle(self, zeitpunkt: datetime) -> list[tuple[datetime, datetime]]:
        """Kuehle Phasen (Beginn, Ende als Zeitpunkte) um `zeitpunkt`, nach Beginn sortiert."""
        try:
            tag = zeitpunkt.astimezone(self._zone).date()
        except OverflowError:
            return []  # am Rand des darstellbaren Bereichs: kein Plan
        if tag not in self._intervalle_je_tag:
            self._intervalle_je_tag[tag] = self._intervalle_berechnen(tag)
        return self._intervalle_je_tag[tag]

    def _intervalle_berechnen(self, mitte: date) -> list[tuple[datetime, datetime]]:
        if self._zeiten is None:
            return []
        wechsel: list[tuple[datetime, datetime, bool]] = []  # (Zeitpunkt, Wanduhrzeit, kuehl)
        try:
            for versatz in range(-_VORLAUF_TAGE, _NACHLAUF_TAGE + 1):
                tag = mitte + timedelta(days=versatz)
                warm, kuehl = self._zeiten[tag.isoweekday()]
                anfang = datetime(tag.year, tag.month, tag.day)
                wand_warm = anfang + timedelta(minutes=warm)
                wand_kuehl = anfang + timedelta(minutes=kuehl + (_TAG_MINUTEN if kuehl < warm else 0))
                wechsel.append((_instant(wand_warm, self._zone), wand_warm, False))
                wechsel.append((_instant(wand_kuehl, self._zone), wand_kuehl, True))
        except OverflowError:
            return []
        wechsel.sort(key=lambda w: (w[0], w[1]))
        return [(vorher[0], danach[0]) for vorher, danach in zip(wechsel, wechsel[1:], strict=False)
                if vorher[2] and not danach[2]]

    def _aufheizbeginn_plan(self, warm_ab: datetime) -> datetime | None:
        try:
            return warm_ab - self._plan_dauer if self._plan_dauer is not None else None
        except OverflowError:
            return None

    def _plan_zustand(self, zeitpunkt: datetime) -> _Zustand:
        wunsch = _Zustand(self._wunsch, SOLL_ART_WUNSCH)
        absenktemperatur_plan = self._absenk_temp
        if not self._hat_absenkung or absenktemperatur_plan is None:
            return wunsch
        for kuehl, warm in self._intervalle(zeitpunkt):
            if not kuehl <= zeitpunkt < warm:
                continue
            aufheizen = self._aufheizbeginn_plan(warm)
            if aufheizen is None or not phasendauer_gueltig(aufheizen - kuehl):
                return wunsch  # die kuehle Phase ist nach dem Aufheizen zu kurz: keine Absenkung
            if zeitpunkt >= aufheizen:
                return _Zustand(self._wunsch, SOLL_ART_AUFHEIZEN)
            if self._uebersteuerung is not None and zeitpunkt < self._uebersteuerung[1] <= aufheizen:
                return _Zustand(self._uebersteuerung[0], SOLL_ART_UEBERSTEUERT)
            return _Zustand(absenktemperatur_plan, SOLL_ART_ABSENKUNG)
        return wunsch

    def _plan_grenzen(self, von: datetime, bis: datetime) -> list[datetime]:
        grenzen: set[datetime] = set()
        for kuehl, warm in self._intervalle(von):
            grenzen.update((kuehl, warm))
            aufheizen = self._aufheizbeginn_plan(warm)
            if aufheizen is not None:
                grenzen.add(aufheizen)
        return sorted(grenze for grenze in grenzen if von < grenze < bis)

    # --- Ueberlagerung ---

    def _aufheizbeginn(self, fenster: _Fenster) -> datetime | None:
        if fenster.aufheizen_ab is not None:
            return fenster.aufheizen_ab if fenster.aufheizen_ab < fenster.bis else None
        if not self._weg_dauer:
            return None
        try:
            return fenster.bis - self._weg_dauer
        except OverflowError:
            return None

    def _aufheizzustand(self, fenster: _Fenster, zeitpunkt: datetime) -> _Zustand | None:
        """Aufheizen vor dem Ende von Abwesend oder Leerstand: das Soll, das der Plan zum Ende vorsieht."""
        beginn = self._aufheizbeginn(fenster)
        if beginn is None or zeitpunkt < beginn:
            return None
        return _Zustand(self._plan_zustand(fenster.bis).soll, SOLL_ART_AUFHEIZEN)

    def zustand(self, zeitpunkt: datetime) -> _Zustand:
        leerstand, abwesend = self._leerstand, self._abwesend
        if leerstand is not None and leerstand.von is not None and leerstand.von <= zeitpunkt < leerstand.bis:
            if self._uebersteuerung is not None and zeitpunkt < self._uebersteuerung[1] <= leerstand.bis:
                return _Zustand(self._uebersteuerung[0], SOLL_ART_UEBERSTEUERT)
            return self._aufheizzustand(leerstand, zeitpunkt) or _Zustand(leerstand.temp, SOLL_ART_LEERSTAND)
        if abwesend is not None and zeitpunkt < abwesend.bis:
            return self._aufheizzustand(abwesend, zeitpunkt) or _Zustand(abwesend.temp, SOLL_ART_ABWESEND)
        return self._plan_zustand(zeitpunkt)

    # --- Wechsel ---

    def _zeitpunkte_der_ueberlagerung(self) -> set[datetime]:
        zeitpunkte: set[datetime] = set()
        for fenster in (self._leerstand, self._abwesend):
            if fenster is None:
                continue
            zeitpunkte.add(fenster.bis)
            if fenster.von is not None:
                zeitpunkte.add(fenster.von)
            beginn = self._aufheizbeginn(fenster)
            if beginn is not None:
                zeitpunkte.add(beginn)
        if self._uebersteuerung is not None:
            zeitpunkte.add(self._uebersteuerung[1])
        return zeitpunkte

    def naechster_wechsel(self, jetzt: datetime, aktuell: _Zustand) -> datetime | None:
        """Erster Zeitpunkt nach `jetzt`, an dem sich Soll oder Art aendern. Zwischen zwei Zeitpunkten der
        Ueberlagerung kommen nur Wechsel des Plans in Frage (nur mit Absenkung)."""
        schluessel = (aktuell.soll, aktuell.art)
        beginn = jetzt
        for ende in [*sorted(z for z in self._zeitpunkte_der_ueberlagerung() if z > jetzt), None]:
            if self._hat_absenkung:
                try:
                    horizont = beginn + timedelta(days=_PLAN_SUCHTAGE)
                except OverflowError:
                    horizont = datetime.max.replace(tzinfo=UTC)
                for grenze in self._plan_grenzen(beginn, horizont if ende is None else min(horizont, ende)):
                    gewechselt = self.zustand(grenze)
                    if (gewechselt.soll, gewechselt.art) != schluessel:
                        return grenze
            if ende is None:
                return None
            danach = self.zustand(ende)
            if (danach.soll, danach.art) != schluessel:
                return ende
            beginn = ende
        return None
