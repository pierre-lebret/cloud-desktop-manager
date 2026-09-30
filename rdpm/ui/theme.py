"""Palette (clair, sombre), typographie et tailles."""

from __future__ import annotations

import customtkinter as ctk

BG = ("#F3F4F7", "#0E1014")
SURFACE = ("#FFFFFF", "#171A21")
SURFACE_2 = ("#F0F2F5", "#1E222B")
SURFACE_3 = ("#E6E9EE", "#262B36")
BORDER = ("#E1E4EA", "#282D38")
TEXT = ("#14161B", "#E8EAEF")
MUTED = ("#5E6574", "#8C94A5")
FAINT = ("#8A91A0", "#626A7A")

ACCENT = ("#3B5BDB", "#5C7CFA")
ACCENT_HOVER = ("#3049B8", "#4C6EF5")
ACCENT_DISABLED = ("#AAB6E6", "#2E3A66")   # bouton principal grisé

TONES = {
    #          texte fort           fond doux
    "muted":   (("#5E6574", "#9AA2B2"), ("#ECEEF2", "#232833")),
    "info":    (("#1864AB", "#74C0FC"), ("#E7F1FC", "#162536")),
    "success": (("#2B8A3E", "#69DB7C"), ("#E9F8EC", "#15291C")),
    "warning": (("#B35C00", "#FFC078"), ("#FFF3E0", "#2E2213")),
    "danger":  (("#C92A2A", "#FF8787"), ("#FDECEC", "#321818")),
    "accent":  (("#5F3DC4", "#B197FC"), ("#F1ECFE", "#241D38")),
}

DANGER = ("#E03131", "#FA5252")
DANGER_HOVER = ("#C92A2A", "#F03E3E")
DANGER_DISABLED = ("#EDB3B3", "#5C2A2A")

PAD = 16
CARD_WIDTH = 400
RADIUS = 12

_FAMILY = "Segoe UI"
_MONO = "Cascadia Mono"


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=_FAMILY, size=size, weight=weight)


def mono(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(family=_MONO, size=size)


def tone(kind: str) -> tuple[tuple[str, str], tuple[str, str]]:
    return TONES.get({"error": "danger"}.get(kind, kind), TONES["muted"])


def pick(color: tuple[str, str]) -> str:
    return color[1] if ctk.get_appearance_mode() == "Dark" else color[0]
