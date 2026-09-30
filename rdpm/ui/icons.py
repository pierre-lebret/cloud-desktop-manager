"""Icônes PNG du projet (rdpm/ui/assets, générées par tools/make_icons.py), sans Pillow.

Tk 8.6 lit le PNG nativement (tk.PhotoImage). Une PhotoImage n'étant pas remise à l'échelle par
customtkinter, on choisit la déclinaison selon l'échelle d'affichage (16 px, ou 32 px au-delà de 150 %).
"""

from __future__ import annotations

import tkinter as tk
import warnings
from pathlib import Path

import customtkinter as ctk

from ..constants import OS_LINUX, OS_WINDOWS

ASSETS = Path(__file__).resolve().parent / "assets"
OS_ICONS = {OS_WINDOWS: "os-windows", OS_LINUX: "os-linux"}
OS_NAMES = {OS_WINDOWS: "Windows", OS_LINUX: "Linux"}

# customtkinter préfère ses CTkImage (qui exigent Pillow) : une PhotoImage fonctionne, sans mise à l'échelle.
warnings.filterwarnings("ignore", message=".*Given image is not CTkImage.*")

_cache: dict[tuple[str, int], tk.PhotoImage] = {}


def _scaling(widget) -> float:
    try:
        return float(widget._get_widget_scaling())
    except (AttributeError, tk.TclError):
        return 1.0


def icon(widget, name: str, size: int = 16) -> tk.PhotoImage | None:
    """PhotoImage de l'icône `name` pour une taille logique `size` (16 ou 32), None si introuvable."""
    wanted = size * 2 if _scaling(widget) >= 1.5 else size
    for px in (wanted, size, 32, 16):
        key = (name, px)
        if key in _cache:
            return _cache[key]
        path = ASSETS / f"{name}-{px}.png"
        if path.exists():
            try:
                _cache[key] = tk.PhotoImage(master=widget, file=str(path))
            except tk.TclError:
                return None
            return _cache[key]
    return None


def icon_label(master, name: str, size: int = 16, **kw) -> ctk.CTkLabel:
    """Étiquette qui n'affiche qu'une icône (vide si le fichier manque)."""
    image = icon(master, name, size)
    return ctk.CTkLabel(master, text="", image=image, width=size, height=size, **kw)


def os_icon_name(os_name: str) -> str:
    return OS_ICONS.get(os_name, OS_ICONS[OS_WINDOWS])


def set_window_icon(window) -> None:
    """Icône de la fenêtre principale et des fenêtres filles (iconphoto par défaut)."""
    images = [img for img in (icon(window, "app", 64), icon(window, "app", 32), icon(window, "app", 16)) if img]
    if images:
        try:
            window.iconphoto(True, *images)
        except tk.TclError:
            pass
