"""Widgets réutilisables : pastilles, boutons, bandeaux, toasts, info-bulles."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from . import theme as t


def logical(widget, px: float) -> float:
    """Pixels physiques (winfo_width, event.width) -> unités logiques CTk (avant mise à l'échelle DPI)."""
    try:
        return px / widget._get_widget_scaling()
    except AttributeError:
        return px


class Pill(ctk.CTkLabel):
    """Pastille d'état : point coloré + libellé sur fond doux."""

    def __init__(self, master, text: str = "", kind: str = "muted", **kw) -> None:
        strong, soft = t.tone(kind)
        super().__init__(master, text=f"●  {text}", fg_color=soft, text_color=strong, corner_radius=10,
                         font=t.font(12, "bold"), height=24, padx=10, **kw)
        self._state = (text, kind)

    def set(self, text: str, kind: str) -> None:
        if (text, kind) == self._state:
            return
        self._state = (text, kind)
        strong, soft = t.tone(kind)
        self.configure(text=f"●  {text}", fg_color=soft, text_color=strong)


def primary_button(master, text: str, command: Callable, width: int = 0, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
                         text_color="#FFFFFF", text_color_disabled=("#C9D3F5", "#8C9BD6"), font=t.font(13, "bold"),
                         height=34, corner_radius=8, width=width or 120, **kw)


def secondary_button(master, text: str, command: Callable, width: int = 0, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent", hover_color=t.SURFACE_3,
                         border_width=1, border_color=t.BORDER, text_color=t.TEXT, text_color_disabled=t.FAINT,
                         font=t.font(13),
                         height=34, corner_radius=8, width=width or 120, **kw)


def danger_button(master, text: str, command: Callable, width: int = 0, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color=t.DANGER, hover_color=t.DANGER_HOVER,
                         text_color="#FFFFFF", font=t.font(13, "bold"), height=34, corner_radius=8,
                         width=width or 120, **kw)


def danger_outline_button(master, text: str, command: Callable, width: int = 0, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent", hover_color=t.tone("danger")[1],
                         border_width=1, border_color=t.DANGER, text_color=t.tone("danger")[0], font=t.font(13),
                         height=34, corner_radius=8, width=width or 120, **kw)


def ghost_button(master, text: str, command: Callable, width: int = 0, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent", hover_color=t.SURFACE_3,
                         text_color=kw.pop("text_color", t.MUTED), font=kw.pop("font", t.font(12)),
                         height=kw.pop("height", 28), corner_radius=6, width=width or 40, **kw)


def label(master, text: str = "", size: int = 13, weight: str = "normal", color=t.TEXT, **kw) -> ctk.CTkLabel:
    kw.setdefault("anchor", "w")
    kw.setdefault("justify", "left")
    return ctk.CTkLabel(master, text=text, font=t.font(size, weight), text_color=color, **kw)


def caption(master, text: str, **kw) -> ctk.CTkLabel:
    return label(master, text.upper(), size=10, weight="bold", color=t.FAINT, **kw)


def card_frame(master, **kw) -> ctk.CTkFrame:
    return ctk.CTkFrame(master, fg_color=t.SURFACE, corner_radius=t.RADIUS, border_width=1,
                        border_color=t.BORDER, **kw)


def notice(master, text: str, kind: str = "info", wraplength: int = 520) -> ctk.CTkFrame:
    strong, soft = t.tone(kind)
    box = ctk.CTkFrame(master, fg_color=soft, corner_radius=8)
    ctk.CTkLabel(box, text=text, text_color=strong, font=t.font(12), wraplength=wraplength, justify="left",
                 anchor="w").pack(fill="x", padx=12, pady=8)
    return box


class Banner(ctk.CTkFrame):
    def __init__(self, master, kind: str, text: str, actions: list[tuple[str, Callable]],
                 on_dismiss: Callable | None) -> None:
        strong, soft = t.tone(kind)
        super().__init__(master, fg_color=soft, corner_radius=10)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(self, width=4, height=1, fg_color=strong, corner_radius=2).grid(
            row=0, column=0, sticky="ns", padx=(8, 0), pady=8)
        self.text = ctk.CTkLabel(self, text=text, text_color=strong, font=t.font(12, "bold"), anchor="w",
                                 justify="left", wraplength=900)
        self.text.grid(row=0, column=1, sticky="w", padx=12, pady=8)
        col = 2
        self._buttons = []
        for text_, cmd in actions:
            btn = ctk.CTkButton(self, text=text_, command=cmd, height=28, corner_radius=6, fg_color=strong,
                                hover_color=strong, text_color=t.SURFACE, font=t.font(12, "bold"), width=10)
            btn.grid(row=0, column=col, padx=(0, 8), pady=6)
            self._buttons.append(btn)
            col += 1
        if on_dismiss:
            self._buttons.append(ctk.CTkButton(self, text="✕", command=on_dismiss, width=28, height=28,
                                               fg_color="transparent", hover_color=t.SURFACE_3, text_color=strong,
                                               font=t.font(12)))
            self._buttons[-1].grid(row=0, column=col, padx=(0, 8))
        self._wrap = 900
        self.bind("<Configure>", self._on_resize, add="+")

    def _on_resize(self, event) -> None:
        # Largeur restante une fois les boutons d'action placés.
        buttons = sum(logical(self, w.winfo_reqwidth()) + 8 for w in self._buttons)
        wrap = max(200, int(logical(self, event.width) - buttons - 40))
        if abs(wrap - self._wrap) > 4:
            self._wrap = wrap
            self.text.configure(wraplength=wrap)


class ToastManager:
    """Notifications empilées en bas à droite ; les erreurs restent jusqu'à fermeture."""

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.toasts: list[ctk.CTkFrame] = []

    def show(self, message: str, kind: str = "info", actions: list[tuple[str, Callable]] = (),
             duration_ms: int | None = None) -> None:
        strong, soft = t.tone(kind)
        frame = ctk.CTkFrame(self.root, fg_color=t.SURFACE, border_color=strong, border_width=1,
                             corner_radius=10)
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(frame, width=4, height=1, fg_color=strong, corner_radius=2).grid(
            row=0, column=0, rowspan=2, sticky="ns", padx=(8, 0), pady=10)
        ctk.CTkLabel(frame, text=message, font=t.font(12), text_color=t.TEXT, wraplength=330, justify="left",
                     anchor="w").grid(row=0, column=1, sticky="w", padx=10, pady=(10, 6 if actions else 10))
        close = ctk.CTkButton(frame, text="✕", width=24, height=24, fg_color="transparent",
                              hover_color=t.SURFACE_3, text_color=t.MUTED, command=lambda: self.close(frame))
        close.grid(row=0, column=2, sticky="ne", padx=6, pady=6)
        if actions:
            row = ctk.CTkFrame(frame, fg_color="transparent", height=1)
            row.grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(0, 10))
            for text_, cmd in actions:
                ctk.CTkButton(row, text=text_, height=26, width=10, corner_radius=6, fg_color=soft,
                              hover_color=t.SURFACE_3, text_color=strong, font=t.font(12, "bold"),
                              command=lambda c=cmd: (self.close(frame), c())).pack(side="left", padx=4)
        self.toasts.append(frame)
        del self.toasts[:-5]
        self._layout()
        if duration_ms is None:
            duration_ms = 30000 if kind == "error" else 15000 if actions else 6000
        if duration_ms:
            self.root.after(duration_ms, lambda: self.close(frame))

    def close(self, frame: ctk.CTkFrame) -> None:
        if frame in self.toasts:
            self.toasts.remove(frame)
        try:
            frame.destroy()
        except tk.TclError:
            pass
        self._layout()

    def _layout(self) -> None:
        alive = []
        for f in self.toasts:
            if f.winfo_exists():
                alive.append(f)
        self.toasts = alive
        y = -16
        for frame in reversed(self.toasts):
            frame.update_idletasks()
            frame.place(relx=1.0, rely=1.0, x=-16, y=y, anchor="se")
            frame.lift()
            y -= frame.winfo_reqheight() + 8


class Tooltip:
    def __init__(self, widget, text: str) -> None:
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background=t.pick(t.SURFACE_3),
                 foreground=t.pick(t.TEXT), relief="flat", padx=8, pady=5, font=("Segoe UI", 9),
                 wraplength=320).pack()

    def _hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None
