"""Garde-fous sur l'interface, vérifiés sans ouvrir de fenêtre (lecture du code source et des fichiers)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIALOGS = sorted((ROOT / "rdpm" / "ui" / "dialogs").glob("*.py"))
ASSETS = ROOT / "rdpm" / "ui" / "assets"

# Méthodes de Tk / de Modal qu'une sous-classe ne doit pas redéfinir par accident : « IP fixe… » réservait une IP
# à l'ouverture parce que sa méthode _activate remplaçait celle que Modal planifie 20 ms après sa création.
FORBIDDEN = {"_activate", "_Modal__activate", "__activate", "state", "title", "geometry", "destroy", "show"}


def _modal_classes():
    for path in DIALOGS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name != "Modal":
                yield path.name, node


def test_dialogs_do_not_shadow_modal_or_tk_methods():
    offenders = []
    for filename, cls in _modal_classes():
        for item in cls.body:
            if isinstance(item, ast.FunctionDef) and item.name in FORBIDDEN:
                offenders.append(f"{filename}:{cls.name}.{item.name}")
    assert not offenders, f"méthodes qui écrasent Modal/Tk : {offenders}"


def test_modal_schedules_a_private_activation():
    source = (ROOT / "rdpm" / "ui" / "dialogs" / "base.py").read_text(encoding="utf-8")
    assert "self.after(20, self.__activate)" in source


def test_icons_are_png_files_of_the_project():
    for name in ("os-windows", "os-linux", "provider-hetzner", "app"):
        for size in (16, 32):
            data = (ASSETS / f"{name}-{size}.png").read_bytes()
            assert data.startswith(b"\x89PNG\r\n\x1a\n"), name
            assert int.from_bytes(data[16:20], "big") == size   # largeur dans l'en-tête IHDR
