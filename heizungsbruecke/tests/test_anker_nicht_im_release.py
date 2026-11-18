"""Der Override der Ankerdatei (`SMARTHEAT_ANKER_DATEI`, nur mit `SMARTHEAT_TESTKENNUNG`) ist ein Werkzeug der Tests: kein
Image, keine Add-on-Konfiguration und keine Compose-Datei des Releases darf ihn setzen. Sonst koennte ein Release
einen fremden Vertrauensanker annehmen. Erlaubt sind die Tests und das Dev-Overlay des Gateways."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NAMEN = (b"SMARTHEAT_ANKER_DATEI", b"SMARTHEAT_TESTKENNUNG")
ERLAUBT = frozenset({"gateway/compose/docker-compose.dev.yml"})
UEBERSPRINGEN = frozenset({".git", "tests", "__pycache__", "node_modules", ".venv"})
ENDUNGEN = (".yml", ".yaml", ".env", ".service", ".sh", ".json")


def konfig_dateien(wurzel: Path) -> list[Path]:
    """Dockerfiles, Add-on-Konfiguration, Compose-Dateien und Skripte des Releases (ohne Test-Ordner)."""
    gefunden = []
    for ordner, unterordner, namen in os.walk(wurzel):
        unterordner[:] = [u for u in unterordner if u not in UEBERSPRINGEN]
        for name in namen:
            if name.startswith("Dockerfile") or name.endswith(ENDUNGEN):
                gefunden.append(Path(ordner) / name)
    return sorted(gefunden)


def verstoesse(wurzel: Path) -> list[str]:
    treffer = []
    for datei in konfig_dateien(wurzel):
        rel = datei.relative_to(wurzel).as_posix()
        if rel in ERLAUBT or datei.is_symlink():
            continue
        text = datei.read_bytes()
        treffer += [f"{rel}: {name.decode()}" for name in NAMEN if name in text]
    return treffer


def test_the_release_files_do_not_set_the_anchor_override():
    gesehen = {p.relative_to(REPO).as_posix() for p in konfig_dateien(REPO)}
    # Der Wachtest muss die Dateien, um die es geht, tatsaechlich lesen.
    assert {"heizungsbruecke/Dockerfile", "gateway/Dockerfile", "heizungsbruecke/config.yaml",
            "gateway/compose/docker-compose.yml"} <= gesehen
    assert verstoesse(REPO) == []


def test_a_planted_override_is_found(tmp_path):
    (tmp_path / "gateway" / "compose").mkdir(parents=True)
    (tmp_path / "heizungsbruecke").mkdir()
    (tmp_path / "heizungsbruecke" / "Dockerfile").write_text("ENV SMARTHEAT_ANKER_DATEI=/x\n")
    (tmp_path / "heizungsbruecke" / "config.yaml").write_text("env:\n  SMARTHEAT_TESTKENNUNG: a\n")
    (tmp_path / "gateway" / "compose" / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "gateway" / "compose" / "docker-compose.dev.yml").write_text("SMARTHEAT_ANKER_DATEI: /x\n")
    assert verstoesse(tmp_path) == [
        "heizungsbruecke/Dockerfile: SMARTHEAT_ANKER_DATEI", "heizungsbruecke/config.yaml: SMARTHEAT_TESTKENNUNG"]
