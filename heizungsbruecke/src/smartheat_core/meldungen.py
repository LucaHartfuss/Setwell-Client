"""Meldung als Code: `Meldung(code, params, stufe)` statt Meldungstext.

Der Client-Kern kennt keine Texte, sondern nur Codes aus dem gemeinsamen Katalog (`meldung.<name>`), ihre Parameter und
die Stufe. Die Stufe gehört zum Code und kommt aus der Tabelle, nicht vom Aufrufer. Ein Host rendert den Text nur dort,
wo noch ein Text auf den Draht oder in die Oberfläche muss (`text`); die Tabelle ist die einzige Stelle mit Wortlaut.

Parameter tragen nie mehr als Feldnamen, Zahlen und kurze Klartextwerte; was ein Wert enthalten darf, entscheidet der
Aufrufer (keine Entity- oder Raumnamen, Sicherheitsgrundsätze S-5, S-9, S-12). Ein Code ohne Eintrag, ein fehlender oder
fremder Parameter und eine Stufe gegen die Tabelle scheitern laut.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

PREFIX = "meldung."
STUFEN = ("rot", "orange", "gelb")
MAX_TEXT = 300
MAX_PARAMS = 6
#: Standardwerte der Rendering-Umgebung (Home Assistant); der Gateway-Host setzt seine über `HostTexts`.
STANDARD_PRAEFIX = "Heizungsbrücke"
STANDARD_TRAEGER = "des Home-Assistant-Systems"
_DATUM_PARAMS = frozenset({"ende"})
_QUELLE = {"thermostat": "Thermostat"}
_QUELLE_STANDARD = "Wunschtemperatur"

Param = str | int | float


def _zahl(wert: Param) -> str:
    """Zahl für den Text: Ganzzahlen unverändert, Kommazahlen mit Komma und ohne Nachkommanull."""
    if isinstance(wert, int):
        return str(wert)
    return f"{wert:g}".replace(".", ",")


def _tausender(wert: Param) -> str:
    return f"{int(wert):,}".replace(",", ".")


def _datum(wert: Param) -> str:
    return date.fromisoformat(str(wert)).strftime("%d.%m.%Y")


@dataclass(frozen=True)
class Umgebung:
    """Was der Host zum Rendern beisteuert: Absender der Hostmeldungen und Träger des Datenträgers."""
    praefix: str = STANDARD_PRAEFIX
    traeger: str = STANDARD_TRAEGER


@dataclass(frozen=True)
class Vorlage:
    stufe: str
    params: tuple[str, ...]
    render: Callable[[Mapping[str, Param], Umgebung], str]
    optional: tuple[str, ...] = field(default=())


def _vorlage(stufe: str, params: tuple[str, ...], text: str, optional: tuple[str, ...] = ()) -> Vorlage:
    """Textvorlage mit {name}-Platzhaltern; Zahlen laufen durch `_zahl`, Text bleibt unverändert."""
    def render(p: Mapping[str, Param], u: Umgebung) -> str:
        werte = {name: (_zahl(wert) if isinstance(wert, (int, float)) else wert) for name, wert in p.items()}
        return text.format(praefix=u.praefix, traeger=u.traeger, **werte)
    return Vorlage(stufe, params, render, optional)


def _wunsch_begrenzt(p: Mapping[str, Param], u: Umgebung) -> str:
    quelle = _QUELLE.get(str(p.get("herkunft", "")), _QUELLE_STANDARD)
    return f"{quelle} auf {_zahl(p['roh'])} °C gestellt, SmartHeat regelt auf {_zahl(p['begrenzt'])} °C"


def _soll_quelle_aus(p: Mapping[str, Param], u: Umgebung) -> str:
    return (f"Thermostat ist aus, SmartHeat regelt weiter auf {_zahl(p['wirksam'])} °C; "
            "Heizung aus über die Sommersperre bzw. im Portal")


def _restmittel(p: Mapping[str, Param], u: Umgebung) -> str:
    anzahl = p["anzahl"]
    einheit = "Fühler" if anzahl == 1 else "Fühlern"
    return f"SmartHeat: Raumfühler {p['fuehler']} liefert keine Werte, Mittelwert aus {_zahl(anzahl)} {einheit}."


def _batterie_prozent(p: Mapping[str, Param], u: Umgebung) -> str:
    return f"SmartHeat: Batterie von {p['bezeichnung']} ist schwach ({_zahl(p['prozent'])} %). Bitte bald wechseln."


def _schreibzaehler(p: Mapping[str, Param], u: Umgebung) -> str:
    return (f"SmartHeat: Bisher wurden {_tausender(p['anzahl'])} Werte in die Heizung geschrieben. Der Hersteller "
            "nennt eine Grenze von 100.000 Schreibvorgängen auf Lebensdauer des Reglers; bitte den Fachbetrieb "
            "informieren.")


def _abo_inaktiv(p: Mapping[str, Param], u: Umgebung) -> str:
    return (f"SmartHeat: Abo inaktiv. Die Heizung läuft noch bis {_datum(p['ende'])} im Notbetrieb weiter, danach "
            "stellt es die ursprünglichen Werte wieder ein und beendet die Steuerung.")


def _zusatz(p: Mapping[str, Param]) -> str:
    if "zusatz" not in p:
        return ""
    return f" Der Zustand gehört zu einer anderen Anlage, es wurde nichts zurückgeschrieben ({p['zusatz']})."


def _konfiguration_fehler_boost(p: Mapping[str, Param], u: Umgebung) -> str:
    return (f"SmartHeat: Konfigurationsfehler – {p['grund']}. Die Heizungssteuerung ist gestoppt, die Anlage steht "
            f"aber noch auf Boost-Werten. Bitte die ursprünglichen Werte von Hand einstellen ({p['werte']})."
            f"{_zusatz(p)}")


def _abmeldung_boost_aktiv(p: Mapping[str, Param], u: Umgebung) -> str:
    return ("SmartHeat wurde entfernt, die Heizung steht aber noch auf Boost-Werten (Konfiguration ungültig, kein "
            f"Zurücksetzen möglich). Bitte die ursprünglichen Werte von Hand einstellen ({p['werte']}).{_zusatz(p)}")


def _soll_quelle_aus_unbekannt(p: Mapping[str, Param], u: Umgebung) -> str:
    return "Thermostat ist aus, SmartHeat regelt weiter; Heizung aus über die Sommersperre bzw. im Portal"


_BUDGET = ("SmartHeat: Das Tageslimit von {limit} Schreibvorgängen an der Heizung ist erreicht. Neue Werte werden "
           "gespeichert und morgen geschrieben (Schonung des Gerätespeichers).")

TABELLE: dict[str, Vorlage] = {
    PREFIX + name: vorlage for name, vorlage in {
        "wunsch_begrenzt": Vorlage("orange", ("roh", "begrenzt"), _wunsch_begrenzt, ("herkunft",)),
        "soll_quelle_aus": Vorlage("orange", ("wirksam",), _soll_quelle_aus),
        "soll_quelle_aus_unbekannt": Vorlage("orange", (), _soll_quelle_aus_unbekannt),
        "raumfuehler_restmittel": Vorlage("orange", ("fuehler", "anzahl"), _restmittel),
        "raumfuehler_keiner": _vorlage(
            "orange", ("fuehler",),
            "SmartHeat: Raumfühler {fuehler} liefert keine Werte. Kein Raumfühler liefert mehr gültige Werte, die "
            "Heizkurve bleibt unverändert."),
        "raumfuehler_ok": _vorlage("orange", ("fuehler",), "SmartHeat: Raumfühler {fuehler} liefert wieder Werte."),
        "batterie_schwach": _vorlage(
            "orange", ("bezeichnung",), "SmartHeat: Batterie von {bezeichnung} ist schwach. Bitte bald wechseln."),
        "batterie_schwach_prozent": Vorlage("orange", ("bezeichnung", "prozent"), _batterie_prozent),
        "batterie_ok": _vorlage("orange", ("bezeichnung",), "SmartHeat: Batterie von {bezeichnung} wieder in Ordnung."),
        "manueller_eingriff": _vorlage(
            "orange", ("was",),
            "SmartHeat: Die Heizungseinstellung wurde in der App verstellt ({was}) und zurückgesetzt. "
            "Einstellungen bitte nur über das Raumthermostat."),
        "manueller_eingriff_limit": _vorlage(
            "orange", ("was",),
            "SmartHeat: Die Heizungseinstellung wird immer wieder verstellt ({was}). SmartHeat setzt sie heute nicht "
            "mehr zurück, um das Abrufkontingent der Hersteller-Cloud zu schonen, und versucht es morgen erneut."),
        "manueller_eingriff_ok": _vorlage(
            "orange", (), "SmartHeat: Die Heizungseinstellung steht wieder auf den gelernten Werten."),
        "quellwechsel": _vorlage("orange", (), "SmartHeat: Die Quelle der Raumtemperatur hat sich geändert."),
        "schreibbudget": _vorlage("orange", ("limit",), _BUDGET),
        "schreibbudget_boost": _vorlage("orange", ("limit",), _BUDGET + " Ein Boost startet erst morgen wieder."),
        "schreibzaehler": Vorlage("orange", ("anzahl",), _schreibzaehler),
        "aussentemperatur_ausgefallen": _vorlage(
            "orange", (),
            "SmartHeat: Die Außentemperatur liefert keine gültigen Werte. Bitte die Quelle der Außentemperatur "
            "prüfen."),
        "aussentemperatur_ok": _vorlage("orange", (), "SmartHeat: Die Außentemperatur liefert wieder Werte."),
        "entity_fehlt": _vorlage(
            "orange", ("feld",),
            "SmartHeat: {feld}: zugeordnete Entity fehlt in Home Assistant und wird bis zum nächsten Start nicht "
            "gelesen."),
        "entity_fehlt_ok": _vorlage(
            "orange", ("feld",),
            "SmartHeat: {feld}: zugeordnete Entity ist in Home Assistant wieder vorhanden und wird gelesen."),
        "datentraeger_nicht_beschreibbar": _vorlage(
            "rot", (),
            "SmartHeat: Der Datenträger {traeger} ist nicht beschreibbar (voll oder schreibgeschützt). Die "
            "Heizungsregelung pausiert, die Anlage behält ihre letzten Werte."),
        "datentraeger_ok": _vorlage(
            "rot", (), "SmartHeat: Der Datenträger ist wieder beschreibbar, die Heizungsregelung läuft wieder."),
        "notbetrieb": _vorlage(
            "rot", (),
            "{praefix}: Server antwortet nicht, Notbetrieb aktiv. Die Heizung wird bei Bedarf lokal abgesichert."),
        "notbetrieb_ok": _vorlage("rot", (), "{praefix}: Serververbindung wiederhergestellt, Notbetrieb beendet."),
        "datenfehler_lokal": _vorlage(
            "rot", ("rollen",),
            "{praefix}: Sensor(en) ohne gültigen Wert: {rollen}. Die Heizkurve bleibt unverändert, bis die Werte "
            "wieder verfügbar sind (z. B. Batterie prüfen)."),
        "datenfehler_server": _vorlage(
            "rot", ("grund",),
            "{praefix}: Server hat die Messwerte abgelehnt (Code {grund}). Die Heizkurve bleibt unverändert."),
        "datenfehler_schreiben": _vorlage(
            "rot", ("detail",),
            "{praefix}: Neue Heizkurve konnte nicht an die Anlage übertragen werden ({detail}). Wird automatisch "
            "erneut versucht."),
        "datenfehler_ok": _vorlage(
            "rot", (), "{praefix}: Messwerte wieder gültig, Heizkurve wird wieder angepasst."),
        "datenfehler_schreiben_ok": _vorlage(
            "rot", (), "{praefix}: Anlage wieder erreichbar, Heizkurve übertragen."),
        "konfiguration_fehler": _vorlage(
            "rot", ("grund",),
            "SmartHeat: Konfigurationsfehler – {grund}. Die Heizungssteuerung ist gestoppt, die Anlage behält ihre "
            "letzten Werte."),
        "konfiguration_fehler_boost": Vorlage(
            "rot", ("grund", "werte"), _konfiguration_fehler_boost, ("zusatz",)),
        "konfiguration_ok": _vorlage(
            "rot", (), "SmartHeat: Einrichtung in Ordnung, die Heizungssteuerung läuft."),
        "abo_inaktiv": Vorlage("rot", ("ende",), _abo_inaktiv),
        "abo_beendet": _vorlage(
            "rot", (),
            "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, die ursprünglichen Werte sind "
            "wieder eingestellt."),
        "abo_aktiv": _vorlage(
            "rot", (), "SmartHeat: Abo wieder aktiv, die Heizungssteuerung läuft wieder normal."),
        "wiederherstellung_fehlgeschlagen": _vorlage(
            "rot", (),
            "SmartHeat: Zurücksetzen auf die ursprünglichen Werte scheitert. Bitte die Anlage prüfen."),
        "wiederherstellung_ok": _vorlage(
            "rot", (), "SmartHeat: Die ursprünglichen Werte sind wieder eingestellt."),
        "abmeldung_zuruecksetzen_scheitert": _vorlage(
            "rot", ("werte",),
            "SmartHeat wurde entfernt, konnte die Heizung aber nicht auf die ursprünglichen Werte zurücksetzen "
            "({werte}). Es wird weiter versucht, sonst bitte diese Werte von Hand einstellen."),
        "abmeldung_boost_aktiv": Vorlage("rot", ("werte",), _abmeldung_boost_aktiv, ("zusatz",)),
    }.items()
}


def _wert_pruefen(code: str, name: str, wert: Any) -> Param:
    if isinstance(wert, bool) or not isinstance(wert, (str, int, float)):
        raise ValueError(f"Parameter {name} von {code}: nur Text oder Zahl")
    if isinstance(wert, float) and not math.isfinite(wert):
        raise ValueError(f"Parameter {name} von {code}: keine endliche Zahl")
    if isinstance(wert, str):
        return wert[:MAX_TEXT]
    return wert


@dataclass(frozen=True, eq=True)
class Meldung:
    """Eine Meldung des Geräts: Code, Parameter und Stufe (aus der Tabelle, nie frei gewählt)."""
    code: str
    params: dict[str, Param]
    stufe: str

    def __post_init__(self) -> None:
        vorlage = TABELLE.get(self.code) if isinstance(self.code, str) else None
        if vorlage is None:
            raise ValueError("Code nicht im Katalog der Geräte-Meldungen")
        if self.stufe != vorlage.stufe:
            raise ValueError(f"Stufe {self.stufe!r} passt nicht zu {self.code}")
        if not isinstance(self.params, Mapping):
            raise ValueError(f"Parameter von {self.code}: kein Wörterbuch")
        namen = set(self.params)
        erlaubt = set(vorlage.params) | set(vorlage.optional)
        if not set(vorlage.params) <= namen or not namen <= erlaubt or len(namen) > MAX_PARAMS:
            raise ValueError(f"Parameter von {self.code}: erwartet {sorted(vorlage.params)}, bekommen {sorted(namen)}")
        geprueft = {name: _wert_pruefen(self.code, name, self.params[name]) for name in sorted(namen)}
        for name in _DATUM_PARAMS & namen:
            try:
                date.fromisoformat(str(geprueft[name]))
            except ValueError:
                raise ValueError(f"Parameter {name} von {self.code}: kein Datum") from None
        object.__setattr__(self, "params", geprueft)

    def __hash__(self) -> int:
        return hash((self.code, self.stufe, tuple(sorted(self.params.items()))))


def neu(code: str, **params: Param) -> Meldung:
    """Meldung zum Code; die Stufe liefert die Tabelle."""
    vorlage = TABELLE.get(code)
    if vorlage is None:
        raise ValueError("Code nicht im Katalog der Geräte-Meldungen")
    return Meldung(code, dict(params), vorlage.stufe)


def text(meldung: Meldung, *, praefix: str = STANDARD_PRAEFIX, traeger: str = STANDARD_TRAEGER) -> str:
    """Wortlaut der Meldung für den Draht und die Oberfläche; die Hosttexte steuern Absender und Träger bei."""
    return TABELLE[meldung.code].render(meldung.params, Umgebung(praefix, traeger))


def zu_dict(meldung: Meldung) -> dict[str, Any]:
    """Ablage im Zustand (`backup.json`): Code, Parameter, Stufe."""
    return {"code": meldung.code, "params": dict(meldung.params), "stufe": meldung.stufe}


def aus_dict(ablage: Any) -> Meldung | None:
    """Gegenstück zu `zu_dict`; eine unbrauchbare Ablage gibt None (der Aufrufer entscheidet, was das heißt)."""
    if not isinstance(ablage, Mapping) or set(ablage) != {"code", "params", "stufe"}:
        return None
    try:
        return Meldung(ablage["code"], ablage["params"], ablage["stufe"])
    except (ValueError, TypeError):
        return None
