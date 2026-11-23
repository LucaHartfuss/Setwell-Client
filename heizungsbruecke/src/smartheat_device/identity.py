"""Identitaet des Geraets: Signierschluessel ECDSA P-256 (Geraete-ID und
signierte Bootstrap-Anfragen), eigener TLS-Schluessel fuer das MQTT-Zertifikat (CSR mit CN = Geraete-ID) und
Uebernahme-Code (16 Zeichen Base32, 80 Bit). Die Schluessel liegen unter <daten>/device/ (0600), der Code nur im Speicher:
jeder Start erzeugt einen neuen, eine alte Datei claim_code wird geloescht, ohne sie zu lesen. Den Verschluesselungsschluessel enc_key.pem erzeugt das Geraet nicht; eine
vorhandene Datei bleibt liegen und wird nicht gelesen. Geraete-ID = shg- + die ersten 16
Zeichen von Base32(SHA-256(DER-SPKI)) in Kleinbuchstaben (unveraendert). boot_id: neu je Prozessstart (Erkennung doppelt
laufender Geraete). Schluessel und Code erscheinen nie in einem Log (Regel 6)."""
import base64
import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from smartheat_core import dateien
from smartheat_device import nachrichten, wire

KEY_FILE = "key.pem"
TLS_KEY_FILE = "tls_key.pem"
CLAIM_CODE_FILE = "claim_code"  # nur noch zum Loeschen einer Datei aelterer Staende
CERTIFICATE_FILE = "certificate.pem"
ENDPOINT_FILE = "endpoint.json"
STATE_FILE = "bootstrap.json"
NEU_SUFFIX = ".neu"  # Dateien eines laufenden Zertifikatswechsels neben den aktuellen
#: Marker eines begonnenen Verwerfens der Identitaet: liegt vor dem ersten Loeschen und geht nach dem letzten.
MARKER_FILE = "neue_identitaet.json"
#: Alles, was die Identitaet und ihren Zugang ausmacht. Nie dabei: die Rastung des Sicherheitssatzes
#: (sicherheitssatz.json) und die Sicherung (backup.json), sie gehoeren zur Anlage, nicht zum Geraet.
_VERWORFEN = (KEY_FILE, TLS_KEY_FILE, CERTIFICATE_FILE, ENDPOINT_FILE, STATE_FILE,
              TLS_KEY_FILE + NEU_SUFFIX, CERTIFICATE_FILE + NEU_SUFFIX)
#: 10 Zufallsbytes = 80 Bit = genau 16 Base32-Zeichen ohne Auffuellung.
_CODE_BYTES = wire.CLAIM_CODE_CHARS * 5 // 8

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    device_id: str
    signing_key: ec.EllipticCurvePrivateKey = field(repr=False)
    tls_key: ec.EllipticCurvePrivateKey = field(repr=False)
    claim_code: str = field(repr=False)


def _der(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def device_id_for(public_key) -> str:
    digest = base64.b32encode(hashlib.sha256(_der(public_key)).digest()).decode()
    return wire.DEVICE_ID_PREFIX + digest[: wire.DEVICE_ID_HASH_CHARS].lower()


def public_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    return _KODIERUNGEN[wire.PUBLIC_KEY_ENCODING](_der(key.public_key()))


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except FileNotFoundError:
        key = ec.generate_private_key(ec.SECP256R1())
        dateien.schreiben_text(path, _pem(key), modus=dateien.PRIVAT)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError(f"{path.name} ist kein EC-Schluessel")
    return key


def new_claim_code(randbytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return base64.b32encode(randbytes(_CODE_BYTES)).decode()


def load_or_create(device_dir: Path, randbytes: Callable[[int], bytes] = secrets.token_bytes) -> Identity:
    """Schluessel aus den Dateien (oder neu), der Uebernahme-Code nie: er entsteht bei jedem Start neu im Speicher und
    steht in keiner Datei. Eine Datei aelterer Staende wird geloescht, ohne sie zu lesen."""
    signing = _key(device_dir / KEY_FILE)
    tls = _key(device_dir / TLS_KEY_FILE)
    try:
        (device_dir / CLAIM_CODE_FILE).unlink()
        logger.info("Alte Datei %s geloescht: der Uebernahme-Code liegt nur noch im Speicher", CLAIM_CODE_FILE)
    except FileNotFoundError:
        pass
    except OSError as error:
        logger.warning("Alte Datei %s nicht geloescht (%s)", CLAIM_CODE_FILE, type(error).__name__)
    return Identity(device_id_for(signing.public_key()), signing, tls, new_claim_code(randbytes))


def verwerfen(device_dir: Path) -> None:
    """Die Identitaet verwerfen (der Server hat das Geraet freigegeben): erst der Marker, dann Schluessel, Zertifikat,
    Endpunkt und Status. Die Rastung des Sicherheitssatzes und die Sicherung bleiben unberuehrt. Ein Fehler oder Absturz
    nach dem Marker laesst ihn liegen, fortsetzen() beendet das Loeschen beim naechsten Start."""
    dateien.schreiben_json(device_dir / MARKER_FILE, {}, modus=dateien.PRIVAT)
    fortsetzen(device_dir)


def fortsetzen(device_dir: Path) -> bool:
    """Ein begonnenes Verwerfen zu Ende fuehren; False, wenn keines begonnen war."""
    marker = device_dir / MARKER_FILE
    if not marker.exists():
        return False
    for name in _VERWORFEN:
        (device_dir / name).unlink(missing_ok=True)
    marker.unlink()
    logger.info("Identitaet verworfen: das Geraet registriert sich neu")
    return True


def _base64url_ohne_padding(daten: bytes) -> str:
    return base64.urlsafe_b64encode(daten).decode().rstrip("=")


def _base64(daten: bytes) -> str:
    return base64.b64encode(daten).decode()


#: Kodierungen des Vertrags (wire.SIGNATURE_ENCODING, wire.PUBLIC_KEY_ENCODING). Nennt der Vertrag eine andere,
#: scheitert das Geraet laut (KeyError) statt still falsch zu kodieren.
_KODIERUNGEN = {"base64url_unpadded": _base64url_ohne_padding, "base64_der_spki": _base64}
assert {wire.SIGNATURE_ENCODING, wire.PUBLIC_KEY_ENCODING} <= set(_KODIERUNGEN), "Kodierung des Vertrags unbekannt"


def sign(key: ec.EllipticCurvePrivateKey, method: str, path: str, timestamp: int, body: bytes) -> str:
    der = key.sign(wire.canonical_string(method, path, timestamp, body), ec.ECDSA(hashes.SHA256()))
    return _KODIERUNGEN[wire.SIGNATURE_ENCODING](der)


def nachweis(ident: Identity, jetzt: int) -> dict:
    """Besitznachweis fuer den Geraetecode der Einrichtung: Geraete-ID, Zeit (Unix-Sekunden) und die Signatur ueber
    Methode, Pfad und Koerper aus wire (NACHWEIS_*). Beides steht hier nicht zur Wahl: ein anderer Pfad oder Koerper
    wird nie signiert. Der Schluessel verlaesst die Funktion nicht, nur die Signatur."""
    signatur = sign(ident.signing_key, wire.NACHWEIS_METHOD, wire.NACHWEIS_PATH, jetzt, wire.NACHWEIS_BODY)
    return {"geraet_id": ident.device_id, "zeitstempel": jetzt, "signatur": signatur}


def csr_pem(ident: Identity) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ident.device_id)])
    csr = x509.CertificateSigningRequestBuilder().subject_name(name).sign(ident.tls_key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def tls_key_pem(ident: Identity) -> str:
    return _pem(ident.tls_key)


def mit_neuem_tls_schluessel(ident: Identity) -> Identity:
    """Dieselbe Identitaet mit frischem TLS-Schluessel, nur im Speicher: die Datei bleibt, bis der Wechsel bestaetigt ist."""
    return replace(ident, tls_key=ec.generate_private_key(ec.SECP256R1()))


def fingerprint(certificate_pem: str) -> str:
    """SHA-256 des DER-Zertifikats, Kleinbuchstaben-Hex (dieselbe Rechnung wie der Server). ValueError bei unlesbarem PEM."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def passt_zum_schluessel(ident: Identity, certificate_pem: str) -> bool:
    """Gehoert das Zertifikat aus der Bootstrap-Antwort zum eigenen TLS-Schluessel? (Schutz vor einer Verwechslung.)"""
    try:
        cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    except ValueError:
        return False
    return _der(cert.public_key()) == _der(ident.tls_key.public_key())


def register_body(ident: Identity, *, version: str, host: str, capabilities: dict) -> dict:
    return nachrichten.register(
        device_id=ident.device_id, public_key=public_key_b64(ident.signing_key),
        claim_code_hash=wire.claim_code_hash(ident.device_id, ident.claim_code), version=version, host=host,
        capabilities=capabilities, csr=csr_pem(ident))


def new_boot_id() -> str:
    return uuid.uuid4().hex
