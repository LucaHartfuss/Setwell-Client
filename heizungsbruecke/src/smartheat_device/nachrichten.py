"""Bausteine der Geraetenachrichten: jede Nachricht des
Geraets entsteht hier und hat genau die Felder des Vertrags (wire.py). Eine Abweichung ist ein Programmierfehler:
Vertragsfehler (ein ValueError) mit Nachricht und Feldnamen, nie mit Werten (Regel 6); die Aufrufer im
Geraete-Thread loggen nur die Fehlerart. tools/contract_check.py ruft dieselben Bausteine fuer die Round-Trips mit
dem Server.

Ausser den Feldmengen setzen die Bausteine nur Vertragsregeln durch, die ohne Wert pruefbar sind: host aus HOSTS,
zustand aus ZUSTAENDE, stufe aus STUFEN, die Felder von kreise, raum und faehigkeiten, die Groesse des CSR."""
import hashlib
import re
from collections.abc import Iterable

from smartheat_device import wire


class Vertragsfehler(ValueError):
    """Eine Nachricht verletzt den Vertrag. Der Text nennt Nachricht und Feldnamen, nie Werte; ein Host-Rueckruf, der
    selbst ein ValueError wirft, ist damit nicht zu verwechseln."""


def pruefen(name: str, body: dict, pflicht: Iterable[str], optional: Iterable[str] = ()) -> dict:
    pflicht, erlaubt = set(pflicht), set(pflicht) | set(optional)
    fehlt, fremd = sorted(pflicht - set(body)), sorted(set(body) - erlaubt)
    if fehlt or fremd:
        raise Vertragsfehler(f"{name}: fehlt {fehlt}, fremd {fremd}")
    return body


def _host(name: str, body: dict, feld: str = "host") -> None:
    if body.get(feld) not in wire.HOSTS:
        raise Vertragsfehler(f"{name}: {feld} steht nicht in HOSTS")


#: Felder des hello, die nur der Host ha sendet (Vertrag: wire.HELLO_OPTIONAL).
HELLO_NUR_HA = ("auto_update", "integration_version")
_INTEGRATION_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,32}")  # die Form, die der Server annimmt


def integration_version_gueltig(wert) -> bool:
    """Versionstext in der Form, die der Server annimmt."""
    return isinstance(wert, str) and _INTEGRATION_VERSION.fullmatch(wert) is not None


def _faehigkeiten(name: str, wert) -> dict:
    """hello.faehigkeiten und register.capabilities: genau drivers und zigbee, dazu optional updater und programm."""
    if not isinstance(wert, dict):
        raise Vertragsfehler(f"{name}: kein Objekt")
    return pruefen(name, wert, wire.CAPABILITY_FIELDS, wire.CAPABILITY_OPTIONAL)


def _kreisnummern(name: str, eintraege, felder: tuple[str, ...]) -> list[dict]:
    """Eine Liste von Eintraegen mit genau `felder`, deren `kreis` ganze Zahlen von KREIS_MIN bis KREIS_MAX sind,
    aufsteigend und einmalig."""
    if not isinstance(eintraege, list):
        raise Vertragsfehler(f"{name}: keine Liste")
    letzte = wire.KREIS_MIN - 1
    for eintrag in eintraege:
        if not isinstance(eintrag, dict):
            raise Vertragsfehler(f"{name}: Eintrag ist kein Objekt")
        pruefen(name, eintrag, felder)
        nummer = eintrag["kreis"]
        if isinstance(nummer, bool) or not isinstance(nummer, int) or not letzte < nummer <= wire.KREIS_MAX:
            raise Vertragsfehler(f"{name}: kreis ist keine aufsteigende ganze Zahl von KREIS_MIN bis KREIS_MAX")
        letzte = nummer
    return eintraege


def hello(*, device_state: str | None = None, **felder) -> dict:
    body = {"schema": wire.SCHEMA, "schemata": list(wire.SCHEMATA), **felder}
    if device_state is not None:
        body["device_state"] = device_state
    pruefen("hello", body, wire.HELLO_FIELDS, wire.HELLO_OPTIONAL)
    _host("hello", body)
    for feld in HELLO_NUR_HA:
        if feld in body and body["host"] != "ha":
            raise Vertragsfehler(f"hello.{feld}: nur der Host ha sendet das Feld")
    if "auto_update" in body and body["auto_update"] is not None and not isinstance(body["auto_update"], bool):
        raise Vertragsfehler("hello.auto_update: kein Wahrheitswert")
    if body.get("integration_version") is not None and not integration_version_gueltig(body["integration_version"]):
        raise Vertragsfehler("hello.integration_version: kein Versionstext")
    _faehigkeiten("hello.faehigkeiten", body["faehigkeiten"])
    wege = body["wege"]
    if not isinstance(wege, list) or not all(isinstance(weg, str) for weg in wege) or len(set(wege)) != len(wege):
        raise Vertragsfehler("hello.wege: keine Liste verschiedener Texte")
    return body


def status(*, zustand: str, runtime_status, kreise: list[dict], ts: str, ursprungswerte: dict | None = None) -> dict:
    body = {"schema": wire.SCHEMA, "zustand": zustand, "runtime_status": runtime_status, "kreise": kreise, "ts": ts}
    if ursprungswerte:
        body["ursprungswerte"] = ursprungswerte
    pruefen("status", body, wire.STATUS_FIELDS, wire.STATUS_OPTIONAL)
    if zustand not in wire.ZUSTAENDE:
        raise Vertragsfehler("status: zustand steht nicht in ZUSTAENDE")
    for eintrag in _kreisnummern("status.kreise", kreise, wire.STATUS_KREIS_FIELDS):
        raum = eintrag["raum"]
        if raum is not None:
            if not isinstance(raum, dict):
                raise Vertragsfehler("status.kreise.raum: kein Objekt")
            pruefen("status.kreise.raum", raum, wire.RAUM_FIELDS)
    return body


_KREISPRAEFIX = re.compile(r"kreis-([1-9][0-9]{0,3}):(.*)", re.DOTALL)
_SCHLICHTER_KEY = re.compile(r"[a-z0-9_]{1,40}")
_KATEGORIE = re.compile(r"[a-z0-9_]{1,29}")


def draht_key(key: str) -> tuple[str, int | None]:
    """Draht-Key und Kreis zu einem Schluessel des Geraets. Ein schlichter Schluessel bleibt, wie er ist; jeder andere
    traegt Entity-, Raum- oder Anlagennamen oder einen Kreis und wird zu `<kategorie>:<Pruefsumme>` (Kategorie: der
    Teil vor dem ersten Doppelpunkt, sofern er nur `[a-z0-9_]` enthaelt, sonst `meldung`). Die Pruefsumme laeuft
    ueber den ganzen Schluessel samt Kreis, damit zwei Kreise zwei Draht-Keys ergeben; der Kreis geht als Feld."""
    if _SCHLICHTER_KEY.fullmatch(key):
        return key, None
    treffer = _KREISPRAEFIX.fullmatch(key)
    kreis, rest = (int(treffer.group(1)), treffer.group(2)) if treffer else (None, key)
    kategorie = rest.split(":", 1)[0][:29]
    if not _KATEGORIE.fullmatch(kategorie):
        kategorie = "meldung"
    pruefsumme = hashlib.sha256(key.encode("utf-8", errors="backslashreplace")).hexdigest()[:10]
    return f"{kategorie}:{pruefsumme}", kreis


def meldung(*, key: str, code: str | None, params: dict, stufe: str | None, offen: bool, ts: str,
            kreis: int | None = None, quelle: str | None = None, roh: str | None = None) -> dict:
    """Ein Eintrag von up/notifications: Code, Parameter und Stufe, nie ein Text. Schliesst das Geraet eine Meldung,
    die es nicht mehr kennt, ist `code` None (nur mit offen False, params leer, stufe None)."""
    body = {"key": key, "code": code, "params": params, "stufe": stufe, "offen": offen, "ts": ts}
    for name, wert in (("kreis", kreis), ("quelle", quelle), ("roh", roh)):
        if wert is not None:
            body[name] = wert
    pruefen("meldung", body, wire.NOTIFICATION_FIELDS, wire.NOTIFICATION_OPTIONAL)
    if code is None:
        if offen is not False or params != {} or stufe is not None:
            raise Vertragsfehler("meldung: ohne code nur geschlossen, ohne params und ohne stufe")
    elif stufe not in wire.STUFEN:
        raise Vertragsfehler("meldung: stufe steht nicht in STUFEN")
    return body


def entfernt(*, wahl: str, ts: str) -> dict:
    """up/entfernt: der Nutzer hat den Eintrag des Geraets entfernt und waehlt, ob es das Konto verlaesst."""
    body = pruefen("entfernt", {"schema": wire.SCHEMA, "wahl": wahl, "ts": ts}, wire.ENTFERNT_FIELDS)
    if wahl not in wire.ENTFERNT_WAHLEN:
        raise Vertragsfehler("entfernt: wahl steht nicht in ENTFERNT_WAHLEN")
    return body


def notifications(items: list[dict]) -> dict:
    return pruefen("notifications", {"schema": wire.SCHEMA, "items": items}, wire.NOTIFICATIONS_FIELDS)


# up/result: Pflicht je Ziel steht in wire.RESULT_PFLICHT_DOKUMENT (Dokument, Software) und RESULT_FIELDS (Befehl).
# Das Ziel ist command_id (Befehl) oder dokument und version (Dokument); beide Ziele stehen in RESULT_TARGETS.
ZIEL_BEFEHL, *ZIELE_DOKUMENT = wire.RESULT_TARGETS


def ergebnis_dokument(dokument: str, version, ok: bool, error: dict | None = None) -> dict:
    body = {"schema": wire.SCHEMA, "dokument": dokument, "version": version, "ok": ok}
    if error is not None:
        body["error"] = error
    return pruefen("ergebnis", body, (*wire.RESULT_PFLICHT_DOKUMENT, *ZIELE_DOKUMENT), wire.RESULT_OPTIONAL_DOKUMENT)


def _csr(name: str, csr) -> None:
    if not isinstance(csr, str) or len(csr.encode()) > wire.CSR_MAX_BYTES:
        raise Vertragsfehler(f"{name}: csr ist kein Text oder zu gross")


def register(*, werks_nachweis=None, **felder) -> dict:
    body = dict(felder)
    if werks_nachweis is not None:
        body["werks_nachweis"] = werks_nachweis
    pruefen("register", body, wire.REGISTER_FIELDS, wire.REGISTER_OPTIONAL)
    _host("register", body)
    _faehigkeiten("register.capabilities", body["capabilities"])
    _csr("register", body["csr"])
    return body


def zertifikat(csr: str) -> dict:
    body = pruefen("zertifikat", {"csr": csr}, wire.CERTIFICATE_FIELDS)
    _csr("zertifikat", csr)
    return body
