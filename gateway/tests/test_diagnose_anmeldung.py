"""Abschnitt "Anmelden beim Hersteller" der LAN-Seite: der Link mit state und Challenge und der QR-Code derselben Adresse
erscheinen nur hinter der Sitzung, nur bei offener Anmeldung, mit no-store, und nie im Auszug fuer diagnostics, im
Ergebnis oder im Log."""
import html
import http.client
import json
import logging
from pathlib import Path

import pytest
import segno

from smartheat_gateway import lokale_texte
from smartheat_gateway.anmeldung import Zugang
from smartheat_gateway.diagnose import render_html, start_server

ZUGANG = Zugang(Path("/gibt/es/nicht/werk.json"))
COOKIE = f"shg_sitzung={ZUGANG.sitzungen.neu()}"
GEHEIMER_STATE = "geheimer-state-0123456789"
URL = ("https://iam.viessmann-climatesolutions.com/idp/v3/authorize?response_type=code&client_id=abc"
       f"&state={GEHEIMER_STATE}&code_challenge=xyz&code_challenge_method=S256")
SNAPSHOT = {
    "geraet": "shg-abcdefghijklmnop", "zustand": "regelt", "eingerichtet": True, "device_state": "uebernommen",
    "versionen": {"gateway": "0.6.0"}, "server_ok": True, "laufzeit": "regelt", "zigbee_stick": True, "host": {},
    "hinweise": [], "meldungen": [], "konfiguration_abgelehnt": None, "code": None,
}


@pytest.fixture
def server_mit_anmeldung():
    """Startet einen Server mit der gegebenen Quelle der offenen Anmeldung; liefert (port, abrufen)."""
    gestartet = []

    def starten(quelle, **kwargs):
        server = start_server(0, lambda: SNAPSHOT, zugang=ZUGANG, anmeldung=quelle, **kwargs)
        gestartet.append(server)
        return server.server_address[1]

    yield starten
    for server in gestartet:
        server.shutdown()
        server.server_close()


def _get(port: int, pfad: str, cookie: str | None = COOKIE, sprache: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        kopf = {"Host": "127.0.0.1"}
        if cookie:
            kopf["Cookie"] = cookie
        if sprache:
            kopf["Accept-Language"] = sprache
        connection.request("GET", pfad, headers=kopf)
        antwort = connection.getresponse()
        return antwort.status, antwort.read().decode(), antwort.headers
    finally:
        connection.close()


def test_the_section_shows_the_link_escaped_in_both_languages():
    for sprache in ("de", "en"):
        seite = render_html({**SNAPSHOT, "anmelde_url": URL}, sprache)
        t = lokale_texte.alle(sprache)
        assert t["abschnitt.anmelden"] in seite and t["text.anmelden"] in seite
        assert 'href="' + URL.replace("&", "&amp;") + '"' in seite
        assert "<img" not in seite  # der QR-Code ist ein Inline-SVG, kein Bild
        assert "noreferrer" in seite
        assert t["alt.anmelden_qr"] in seite


def _erwartetes_svg(url: str) -> str:
    return segno.make(url, error="l").svg_inline(scale=4, border=4, dark="#000", light="#fff")


def test_the_section_shows_a_qr_code_with_the_same_url_next_to_the_link():
    seite = render_html({**SNAPSHOT, "anmelde_url": URL}, "de")
    assert _erwartetes_svg(URL) in seite
    assert seite.index("<svg") > seite.index("<a href=")  # der Link bleibt daneben stehen
    assert 'aria-label="' + html.escape(lokale_texte.alle("de")["alt.anmelden_qr"]) + '"' in seite
    assert 'aria-label="' + html.escape(lokale_texte.alle("en")["alt.anmelden_qr"]) + '"' in render_html(
        {**SNAPSHOT, "anmelde_url": URL}, "en")


def test_the_qr_code_is_self_contained():
    svg = _erwartetes_svg(URL)
    seite = render_html({**SNAPSHOT, "anmelde_url": URL}, "de")
    inline = seite[seite.index("<svg"):seite.index("</svg>") + len("</svg>")]
    assert inline == svg
    for verboten in ("<script", "xlink:href", "http://", "https://", "<image", "<use", "<style", "onload"):
        assert verboten not in inline


def test_a_long_url_still_fits_into_one_qr_code():
    lang = URL + "&redirect_uri=" + "x" * 400
    assert _erwartetes_svg(lang) in render_html({**SNAPSHOT, "anmelde_url": lang}, "de")


def test_the_qr_code_is_only_served_with_a_session_and_an_open_login(server_mit_anmeldung):
    port = server_mit_anmeldung(lambda: URL)
    status, koerper, kopf = _get(port, "/")
    assert status == 200 and _erwartetes_svg(URL) in koerper and kopf["Cache-Control"] == "no-store"
    assert "<svg" not in _get(port, "/", cookie=None)[1]
    assert "<svg" not in _get(port, "/", cookie="shg_sitzung=falsch")[1]
    assert "<svg" not in _get(port, "/healthz")[1]
    offen = server_mit_anmeldung(lambda: None)
    assert "<svg" not in _get(offen, "/")[1]
    health = server_mit_anmeldung(lambda: URL, nur_health=True)
    assert "<svg" not in _get(health, "/", cookie=None)[1]


def test_a_hostile_url_is_escaped():
    seite = render_html({**SNAPSHOT, "anmelde_url": 'https://x.test/"><script>alert(1)</script>'}, "de")
    assert "<script>" not in seite


def test_without_an_open_login_there_is_no_section():
    for daten in (SNAPSHOT, {**SNAPSHOT, "anmelde_url": None}):
        seite = render_html(daten, "de")
        assert lokale_texte.alle("de")["abschnitt.anmelden"] not in seite and "href" not in seite


def test_the_link_is_only_served_with_a_session_and_never_cached(server_mit_anmeldung):
    port = server_mit_anmeldung(lambda: URL)
    status, koerper, kopf = _get(port, "/")
    assert status == 200 and GEHEIMER_STATE in koerper
    assert kopf["Cache-Control"] == "no-store"
    status, koerper, kopf = _get(port, "/", cookie=None)
    assert status == 303 and GEHEIMER_STATE not in koerper and kopf["Location"] == "/anmelden"
    status, koerper, _ = _get(port, "/", cookie="shg_sitzung=falsch")
    assert status == 303 and GEHEIMER_STATE not in koerper


def test_the_source_is_not_asked_without_a_session(server_mit_anmeldung):
    fragen = []
    port = server_mit_anmeldung(lambda: fragen.append(1) or URL)
    _get(port, "/", cookie=None)
    _get(port, "/anmelden", cookie=None)
    assert fragen == []


def test_no_open_login_means_no_section_on_the_served_page(server_mit_anmeldung):
    port = server_mit_anmeldung(lambda: None)
    status, koerper, _ = _get(port, "/", sprache="de")
    assert status == 200 and lokale_texte.alle("de")["abschnitt.anmelden"] not in koerper
    port = server_mit_anmeldung(None)
    assert lokale_texte.alle("de")["abschnitt.anmelden"] not in _get(port, "/", sprache="de")[1]


def test_healthz_and_the_health_server_never_carry_the_link(server_mit_anmeldung):
    port = server_mit_anmeldung(lambda: URL)
    status, koerper, _ = _get(port, "/healthz")
    assert status == 200 and GEHEIMER_STATE not in koerper
    health = server_mit_anmeldung(lambda: URL, nur_health=True)
    for pfad in ("/healthz", "/"):
        assert GEHEIMER_STATE not in _get(health, pfad, cookie=None)[1]


def test_a_failing_source_does_not_break_the_page(server_mit_anmeldung):
    def kaputt():
        raise OSError("Datei weg")

    port = server_mit_anmeldung(kaputt)
    status, koerper, _ = _get(port, "/", sprache="de")
    assert status == 200 and GEHEIMER_STATE not in koerper


def test_the_link_is_not_in_the_snapshot_and_not_in_the_log(server_mit_anmeldung, caplog):
    """Die Quelle fuettert nur die Antwort der Seite: der Zustand (Quelle von diagnostics und /healthz) bleibt frei."""
    caplog.set_level(logging.DEBUG)
    port = server_mit_anmeldung(lambda: URL)
    _get(port, "/")
    assert GEHEIMER_STATE not in caplog.text
    assert "anmelde_url" not in SNAPSHOT and "anmelde_url" not in json.loads(_get(port, "/healthz")[1])
