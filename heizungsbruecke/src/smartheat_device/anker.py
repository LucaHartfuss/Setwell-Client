"""Vertrauensanker der Bootstrap-Antworten, Seite des Geraets: der Server signiert jede Erfolgsantwort von register und
certificate mit einem ECDSA-P-256-Schluessel (Base64 der DER-Form); das Geraet prueft die empfangene Antwort gegen die
oeffentlichen Schluessel seiner Release-Datei `anker.pem` (aktiver Schluessel und Reserve), bevor es ein Feld liest. So
kann niemand ausser dem Server dem Geraet einen Zustand oder einen Broker unterschieben, auch nicht mit einem fremden
Zertifikat fuer die Domain.

Signiert ist die kanonische JSON-Form (sortierte Schluessel, kompakte Trenner, ASCII wie die Datenbankform des Servers) von
Antwort ohne `anker_id` und `anker_signatur`, Route (`<Methode> <Pfad>`), Geraete-ID und Zeitstempel der Anfrage: eine
Antwort gilt nur fuer genau diese Anfrage. Die `anker_id` (Anfang des SHA-256 ueber den oeffentlichen Schluessel, DER)
waehlt den Schluessel.

Fail closed: eine Datei ohne gueltigen oeffentlichen Schluessel (der Platzhalter im Repo, eine fehlende oder beschaedigte
Datei) macht jede Antwort ungueltig. Die Datei des Pakets ist die einzige Quelle; `SMARTHEAT_ANKER_DATEI` gilt nur zusammen
mit einer nicht leeren `SMARTHEAT_TESTKENNUNG` (Testlaeufe), sonst wird sie ignoriert und das im Log vermerkt."""
import base64
import binascii
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)

ANKER_DATEI = Path(__file__).with_name("anker.pem")
ENV_DATEI = "SMARTHEAT_ANKER_DATEI"
_TESTKENNUNG = "SMARTHEAT_TESTKENNUNG"
ANKER_FELDER = ("anker_id", "anker_signatur")
_ID_ZEICHEN = 16
_DATEI_MAX_BYTES = 16384
_BLOCK = re.compile(r"-----BEGIN PUBLIC KEY-----[A-Za-z0-9+/=\s]+?-----END PUBLIC KEY-----")


def _pfad() -> Path:
    override = os.environ.get(ENV_DATEI)
    if override is None:
        return ANKER_DATEI
    if not os.environ.get(_TESTKENNUNG, "").strip():
        logger.warning("%s ohne Testkennung ignoriert", ENV_DATEI)  # nie der Wert
        return ANKER_DATEI
    return Path(override)


def _anker_id(key: ec.EllipticCurvePublicKey) -> str:
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:_ID_ZEICHEN]


def schluessel() -> dict[str, ec.EllipticCurvePublicKey]:
    """Die oeffentlichen ECDSA-P-256-Schluessel der Datei nach `anker_id`; leer, wenn die Datei fehlt, zu gross ist oder
    keinen gueltigen Schluessel enthaelt (Platzhalter). Andere Schluesselarten und private Teile zaehlen nicht."""
    try:
        with _pfad().open("rb") as datei:
            roh = datei.read(_DATEI_MAX_BYTES + 1)
        if len(roh) > _DATEI_MAX_BYTES:
            return {}
        text = roh.decode("ascii")
    except (OSError, UnicodeDecodeError):
        return {}
    gefunden: dict[str, ec.EllipticCurvePublicKey] = {}
    for block in _BLOCK.findall(text):
        try:
            key = serialization.load_pem_public_key(block.encode())
        except ValueError:
            continue
        if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
            gefunden[_anker_id(key)] = key
    return gefunden


def _nachricht(antwort: dict, route: str, device_id: str, ts: int) -> bytes:
    inhalt = {k: v for k, v in antwort.items() if k not in ANKER_FELDER}
    return json.dumps({"antwort": inhalt, "route": route, "device_id": device_id, "ts": ts}, sort_keys=True,
                      separators=(",", ":")).encode()


def pruefen(antwort, *, route: str, device_id: str, ts: int) -> bool:
    """Wahr, wenn `antwort` (ein Objekt mit `anker_id` und `anker_signatur`) mit dem Schluessel der `anker_id` aus der
    Datei fuer genau diese Route, Geraete-ID und diesen Zeitstempel signiert ist; sonst falsch, nie eine Ausnahme."""
    if not isinstance(antwort, dict):
        return False
    kennung, signatur_roh = antwort.get("anker_id"), antwort.get("anker_signatur")
    if not isinstance(kennung, str) or not isinstance(signatur_roh, str):
        return False
    key = schluessel().get(kennung)
    if key is None:
        return False
    try:
        signatur = base64.b64decode(signatur_roh, validate=True)
        key.verify(signatur, _nachricht(antwort, route, device_id, ts), ec.ECDSA(hashes.SHA256()))
    except (binascii.Error, ValueError, InvalidSignature, TypeError):
        return False
    return True
