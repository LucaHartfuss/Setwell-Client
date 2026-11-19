"""Anmeldung bei Viessmann per OAuth2 mit PKCE (Variante A). Der Server sieht nur den kurzlebigen Code;
Verifier, Access- und Refresh-Token liegen nur in der privaten Datei des Geraets. Erneuert wird unter flock und mit
Nachlesen: das Refresh-Token rotiert, ein zweiter Leser (Befehl des Geraetekerns bei der Erkennung, Laufzeit im Takt;
je ein eigener flock) darf nie mit dem alten erneuern. Ein abgelehntes Refresh-Token (invalid_grant) loescht die Tokens
und merkt `abgelaufen`; ein unerreichbares oder voruebergehend ueberlastetes IAM ist kein Abmelden."""
import base64
import fcntl
import hashlib
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeGuard
from urllib.parse import urlencode, urlsplit

import requests

from smartheat_core import dateien
from smartheat_core.zahlen import ist_endliche_zahl

logger = logging.getLogger(__name__)

IAM_BASE = "https://iam.viessmann-climatesolutions.com"
API_BASE = "https://api.viessmann-climatesolutions.com"
TOKEN_PATH = "/idp/v3/token"
AUTHORIZE_PATH = "/idp/v3/authorize"
CALLBACK_PATH = "/oauth/callback"
ERLAUBTE_SCOPES = ("IoT User offline_access",)  # fest im Geraet: ein kompromittierter Server kann keine Rechte ausweiten
CLIENT_ID_FORM = re.compile(r"[A-Za-z0-9_.-]{8,128}")
PENDING_SECONDS = 600
RENEW_BEFORE_SECONDS = 60
HTTP_TIMEOUT = 20
TRANSIENT_CLIENT_STATUS = (408, 429)  # 4xx, die keine Ablehnung der Anmeldung sind
LOGIN_FAILED_TEXT = "Die Anmeldung bei Viessmann ist fehlgeschlagen. Bitte noch einmal anmelden."


def _base(env_name: str, default: str) -> str:
    if os.environ.get("SHG_TEST_ENDPOINTS") == "1" and os.environ.get(env_name):
        return os.environ[env_name].rstrip("/")
    return default


def iam_base() -> str:
    return _base("SHG_VICARE_IAM_BASE", IAM_BASE)


def api_base() -> str:
    return _base("SHG_VICARE_API_BASE", API_BASE)


def portal_redirect_uri() -> str | None:
    """Die einzige `redirect_uri`, die das Geraet annimmt: das Portal aus dem signierten Bau (`install.sh` legt
    SHG_PORTAL_BASE_URL in gateway.env ab, Compose reicht sie durch) samt Pfad; der Server kann sie nicht setzen.
    Ohne gueltiges Portal (https, nur Host) gibt es keine; http nur fuer die Testumgebung."""
    base = os.environ.get("SHG_PORTAL_BASE_URL", "").rstrip("/")
    teile = urlsplit(base)
    schemata = ("https", "http") if os.environ.get("SHG_TEST_ENDPOINTS") == "1" else ("https",)
    if teile.scheme not in schemata or not teile.hostname or teile.path or teile.query or teile.fragment:
        return None
    return base + CALLBACK_PATH


class NotLoggedIn(Exception):
    """Keine (oder abgelaufene) Anmeldung: der Kunde muss sie im Portal erneuern."""


class TokenUnavailable(Exception):
    """Das IAM ist nicht erreichbar; die Anmeldung gilt weiter."""


class LoginRejected(Exception):
    def __init__(self, text: str = LOGIN_FAILED_TEXT) -> None:
        super().__init__(text)
        self.text = text


def _lebensdauer(value) -> float | None:
    """expires_in der Token-Antwort in Sekunden; fehlt es, gelten 3600. Alles Unbrauchbare (kein Zahlenwert, nicht endlich,
    auch eine JSON-Ganzzahl ausserhalb des float-Bereichs, und alles bis 0 s, Entscheidung 22) ist None."""
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if ist_endliche_zahl(seconds) and seconds > 0 else None


def new_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class TokenStore:
    def __init__(self, path: Path, *, wall=time.time, iam: str | None = None, post=requests.post) -> None:
        self._path = path
        self._lock_path = path.with_suffix(".lock")
        self._wall = wall
        self._iam = iam
        self._post = post

    # --- Datei ---

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict:
        data = dateien.lesen_json(self._path)
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        dateien.schreiben_json(self._path, data, modus=dateien.PRIVAT)

    def logged_in(self) -> bool:
        return bool(self._load().get("refresh_token"))

    def expired(self) -> bool:
        return bool(self._load().get("abgelaufen"))

    def forget(self) -> None:
        with self._locked():
            for path in (self._path, self._path.with_name(self._path.name + ".tmp")):
                path.unlink(missing_ok=True)

    # --- Anmeldung ---

    def begin(self, client_id: str, redirect_uri: str) -> str:
        verifier, challenge = new_pair()
        with self._locked():
            data = self._load()
            data["pending"] = {"verifier": verifier, "client_id": client_id, "redirect_uri": redirect_uri,
                               "created": self._wall()}
            self._save(data)
        return challenge

    def finish(self, code: str, redirect_uri: str) -> None:
        with self._locked():
            data = self._load()
            pending = data.get("pending")
            if (not isinstance(pending, dict) or pending.get("redirect_uri") != redirect_uri
                    or not ist_endliche_zahl(pending.get("created"))
                    or self._wall() - pending["created"] > PENDING_SECONDS):
                raise NotLoggedIn("Es wurde keine Anmeldung vorbereitet (oder sie ist abgelaufen).")
            body = self._token_request({
                "grant_type": "authorization_code", "client_id": pending["client_id"], "code": code,
                "redirect_uri": redirect_uri, "code_verifier": pending["verifier"]})
            if body is None:
                raise LoginRejected()
            self._save(self._with_tokens({"client_id": pending["client_id"]}, body))

    # --- Anmeldung im Geraet: Verifier und state entstehen hier und verlassen das Geraet nie ---

    def anmeldung_beginnen(self, params: dict) -> dict:
        """Prueft die Vorgaben des Servers (`client_id` nach der Form, `redirect_uri` genau Portal-Host plus Pfad, `scope`
        gegen die feste Liste), legt Verifier und state als offene Anmeldung an und gibt nur den Hash des state zurueck.
        Eine neue Anmeldung ersetzt die offene."""
        client_id, redirect_uri, scope = params.get("client_id"), params.get("redirect_uri"), params.get("scope")
        ziel = portal_redirect_uri()
        if not (isinstance(client_id, str) and CLIENT_ID_FORM.fullmatch(client_id)
                and ziel is not None and redirect_uri == ziel and scope in ERLAUBTE_SCOPES):
            logger.warning("Anmeldevorgaben des Servers abgelehnt")
            raise LoginRejected()
        verifier, _ = new_pair()
        state = secrets.token_urlsafe(32)
        with self._locked():
            data = self._load()
            data["pending"] = {"verifier": verifier, "state": state, "client_id": client_id,
                               "redirect_uri": redirect_uri, "scope": scope, "created": self._wall()}
            self._save(data)
        return {"state_hash": hashlib.sha256(state.encode()).hexdigest()}

    def anmeldung_abschliessen(self, code: str) -> None:
        """Tauscht den Code gegen Tokens; `client_id` und `redirect_uri` stammen aus der offenen Anmeldung, die in jedem
        Fall verbraucht ist (auch bei Ablehnung oder unerreichbarem IAM). Eine bestehende Anmeldung bleibt dabei erhalten."""
        with self._locked():
            data = self._load()
            pending = data.pop("pending", None)
            if pending is not None:
                self._save(data)
            if not self._offen(pending):
                raise NotLoggedIn("Es wurde keine Anmeldung vorbereitet (oder sie ist abgelaufen).")
            body = self._token_request({
                "grant_type": "authorization_code", "client_id": pending["client_id"], "code": code,
                "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"]})
            if body is None:
                raise LoginRejected()
            self._save(self._with_tokens({"client_id": pending["client_id"]}, body))

    def offene_anmeldung(self) -> str | None:
        """Die Adresse des Herstellers zur offenen Anmeldung (mit Challenge und state), nur innerhalb von PENDING_SECONDS.
        Enthaelt das Geheimnis des state: nur an die LAN-Seite hinter der Anmeldung geben, nie loggen oder melden."""
        pending = self._load().get("pending")
        if not self._offen(pending) or not isinstance(pending.get("state"), str) or not isinstance(pending.get("scope"), str):
            return None
        challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).rstrip(b"=").decode()
        query = urlencode({
            "response_type": "code", "client_id": pending["client_id"], "redirect_uri": pending["redirect_uri"],
            "scope": pending["scope"], "state": pending["state"],
            "code_challenge": challenge, "code_challenge_method": "S256"})
        return f"{iam_base()}{AUTHORIZE_PATH}?{query}"

    def _offen(self, pending: object) -> TypeGuard[dict]:
        return (isinstance(pending, dict) and all(isinstance(pending.get(k), str)
                                                  for k in ("verifier", "client_id", "redirect_uri"))
                and ist_endliche_zahl(pending.get("created")) and self._wall() - pending["created"] <= PENDING_SECONDS)

    # --- Zugriff ---

    def access_token(self, force_refresh: bool = False) -> str:
        data = self._load()
        if not data.get("refresh_token"):
            raise NotLoggedIn("Nicht bei Viessmann angemeldet.")
        if not force_refresh and self._fresh(data):
            return data["access_token"]
        with self._locked():
            data = self._load()  # Nachlesen: ein anderer Prozess kann schon erneuert haben
            if not data.get("refresh_token"):
                raise NotLoggedIn("Nicht bei Viessmann angemeldet.")
            if not force_refresh and self._fresh(data):
                return data["access_token"]
            body = self._token_request({"grant_type": "refresh_token", "client_id": data["client_id"],
                                        "refresh_token": data["refresh_token"]},
                                       revoked_only=True)
            if body is None:
                # Ein vorbereiteter PKCE-Zustand bleibt, die laufende Anmeldung im Portal gelingt noch
                kept = {"pending": data["pending"]} if isinstance(data.get("pending"), dict) else {}
                self._save({"client_id": data.get("client_id"), "abgelaufen": True, **kept})
                raise NotLoggedIn("Die Anmeldung bei Viessmann ist abgelaufen.")
            data = self._with_tokens(data, body)
            self._save(data)
            return data["access_token"]

    def _fresh(self, data: dict) -> bool:
        expires_at = data.get("expires_at")
        if not ist_endliche_zahl(expires_at):  # fehlt oder unbrauchbar (Datei ist JSON): wie abgelaufen, es wird erneuert
            return False
        return bool(data.get("access_token")) and expires_at - self._wall() > RENEW_BEFORE_SECONDS

    def _with_tokens(self, data: dict, body: dict) -> dict:
        # "pending" bleibt: eine Erneuerung waehrend einer neuen Anmeldung darf deren PKCE-Zustand nicht loeschen
        # (finish baut die Daten ohne "pending" neu auf).
        data = {k: v for k, v in data.items() if k != "abgelaufen"}
        lifetime = _lebensdauer(body.get("expires_in", 3600))
        if lifetime is None:  # _token_request liefert nur Antworten mit brauchbarer Lebensdauer
            raise TokenUnavailable()
        data.update({"access_token": body["access_token"],
                     "refresh_token": body.get("refresh_token") or data.get("refresh_token"),
                     "expires_at": self._wall() + lifetime})
        return data

    def _token_request(self, form: dict, *, revoked_only: bool = False) -> dict | None:
        """Antwort bei 200, None bei einer Ablehnung, TokenUnavailable sonst. Nie den Inhalt loggen.

        Beim Erneuern (revoked_only) gilt nur 400/401 mit error=invalid_grant als Ablehnung, denn sie loescht die
        Anmeldung; 403 (WAF), 404 (falsche Basis), invalid_client u. a. sind kein Widerruf. Beim Code-Tausch
        (finish) ist jede nicht voruebergehende 4xx eine Ablehnung, das zerstoert nichts."""
        try:
            response = self._post((self._iam or iam_base()) + TOKEN_PATH, data=form, timeout=HTTP_TIMEOUT)
        except requests.RequestException as error:
            logger.warning("Viessmann-IAM nicht erreichbar (%s)", type(error).__name__)
            raise TokenUnavailable() from None
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if status == 200:
            if (isinstance(body, dict) and isinstance(body.get("access_token"), str)
                    and _lebensdauer(body.get("expires_in", 3600)) is not None):
                return body
            logger.warning("Viessmann-IAM: unbrauchbare Antwort")
            raise TokenUnavailable()
        if revoked_only:
            if status in (400, 401) and isinstance(body, dict) and body.get("error") == "invalid_grant":
                logger.warning("Viessmann-IAM widerruft die Anmeldung (%s, invalid_grant)", status)
                return None
            logger.warning("Viessmann-IAM antwortet beim Erneuern mit %s, Anmeldung bleibt", status)
            raise TokenUnavailable()
        if 400 <= status < 500 and status not in TRANSIENT_CLIENT_STATUS:
            logger.warning("Viessmann-IAM lehnt die Anfrage ab (%s)", status)
            return None
        logger.warning("Viessmann-IAM antwortet voruebergehend mit %s", status)
        raise TokenUnavailable()
