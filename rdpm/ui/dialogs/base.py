"""Fenêtres modales et confirmations à trois niveaux (aucune / OK-Annuler / saisie du nom)."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from .. import theme as t
from ..widgets import danger_button, danger_outline_button, label, notice, primary_button, secondary_button

_STACK: list["Modal"] = []
CONFIRM_WORD = "CONFIRM"


def is_confirm_word(text: str) -> bool:
    """Confirmation des actions destructrices : « CONFIRM », sans tenir compte de la casse."""
    return text.strip().upper() == CONFIRM_WORD


def top_window(root):
    """Fenêtre au premier plan. `root` peut être une vue projet (ProjectScope) : on prend sa vraie fenêtre."""
    alive = [m for m in _STACK if m.winfo_exists()]
    return alive[-1] if alive else getattr(root, "tk_root", root)


class Modal(ctk.CTkToplevel):
    def __init__(self, master, title: str, width: int = 540, resizable: bool = False) -> None:
        parent = top_window(master)
        super().__init__(parent)
        self.parent_window = parent
        self.result = None
        self.title(title)
        self.configure(fg_color=t.BG)
        self.resizable(resizable, resizable)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _e: self.cancel())
        self._width = width
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=22, pady=(18, 8))
        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.pack(fill="x", padx=22, pady=(4, 18))
        _STACK.append(self)
        self.after(20, self.__activate)  # nom privé : une sous-classe ne peut pas l'écraser

    def __activate(self) -> None:
        if not self.winfo_exists():
            return
        self.update_idletasks()
        w = max(self._width, self.winfo_reqwidth())
        h = self.winfo_reqheight()
        px, py = self.parent_window.winfo_rootx(), self.parent_window.winfo_rooty()
        pw, ph = self.parent_window.winfo_width(), self.parent_window.winfo_height()
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + max(20, (ph - h) // 3))
        self._pos = (x, y)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            self.after(100, self.__activate)

    def refit(self) -> None:
        self.update_idletasks()
        size = f"{max(self._width, self.winfo_reqwidth())}x{self.winfo_reqheight()}"
        pos = getattr(self, "_pos", None)   # sans position, certains gestionnaires de fenêtres la remettent à 0,0
        self.geometry(f"{size}+{pos[0]}+{pos[1]}" if pos else size)

    def close(self, result=None) -> None:
        self.result = result
        try:
            self.grab_release()
        except tk.TclError:
            pass
        if self in _STACK:
            _STACK.remove(self)
        self.destroy()
        prev = top_window(None)
        if prev is not None and prev is not self and hasattr(prev, "grab_set"):
            try:
                prev.grab_set()
                prev.focus_force()
            except tk.TclError:
                pass

    def cancel(self) -> None:
        self.close(None)

    def show(self):
        self.master.wait_window(self)
        return self.result

    # --- aides de mise en page ---------------------------------------------------------------
    def heading(self, text: str, subtitle: str | None = None) -> None:
        label(self.body, text, 17, "bold").pack(fill="x")
        if subtitle:
            label(self.body, subtitle, 12, color=t.MUTED, wraplength=self._width - 50).pack(fill="x", pady=(2, 0))

    def text(self, text: str, color=t.TEXT, size: int = 13, pady=(10, 0), parent=None,
             wraplength: int | None = None) -> ctk.CTkLabel:
        lbl = label(parent or self.body, text, size, color=color, wraplength=wraplength or self._width - 50)
        lbl.pack(fill="x", pady=pady)
        return lbl

    def notice(self, text: str, kind: str = "info", pady=(12, 0), parent=None,
               wraplength: int | None = None) -> ctk.CTkFrame:
        box = notice(parent or self.body, text, kind, wraplength=wraplength or self._width - 80)
        box.pack(fill="x", pady=pady)
        return box

    def section(self, title: str, pady=(16, 6), parent=None) -> None:
        label(parent or self.body, title.upper(), 10, "bold", color=t.FAINT).pack(fill="x", pady=pady)

    def buttons(self, *specs: tuple[str, Callable, str]) -> list[ctk.CTkButton]:
        """specs : (libellé, commande, style) : primary / secondary / danger / danger_outline."""
        made = []
        for text_, cmd, style in reversed(specs):
            factory = {"primary": primary_button, "danger": danger_button,
                       "danger_outline": danger_outline_button}.get(style, secondary_button)
            btn = factory(self.footer, text_, cmd)
            btn.pack(side="right", padx=(8, 0))
            made.append(btn)
        return list(reversed(made))


class ConfirmDialog(Modal):
    def __init__(self, master, title: str, message: str, ok_text: str = "Confirmer", danger: bool = False,
                 details: list[str] | None = None, checkbox: str | None = None) -> None:
        super().__init__(master, title, width=500)
        self.heading(title)
        self.text(message)
        if details:
            for line in details:
                label(self.body, f"•  {line}", 12, color=t.MUTED, wraplength=440).pack(fill="x", padx=(6, 0),
                                                                                    pady=(4, 0))
        self.check_var = tk.BooleanVar(value=False)
        if checkbox:
            ctk.CTkCheckBox(self.body, text=checkbox, variable=self.check_var, font=t.font(12)).pack(
                anchor="w", pady=(14, 0))
        cancel, ok = self.buttons(("Annuler", self.cancel, "secondary"),
                                  (ok_text, lambda: self.close(True), "danger" if danger else "primary"))
        if danger:
            cancel.focus_set()  # Entrée ne doit jamais valider une action destructive
        else:
            self.bind("<Return>", lambda _e: self.close(True))
            ok.focus_set()


class TypedConfirmDialog(Modal):
    """Niveau 3 : le bouton rouge ne s'active qu'une fois « CONFIRM » saisi (casse indifférente)."""

    def __init__(self, master, title: str, message: str, expected: str, ok_text: str,
                 lost: list[str] = (), kept: list[str] = ()) -> None:
        super().__init__(master, title, width=540)
        self.expected = expected
        self.heading(title)
        self.notice(message, "danger")
        if lost:
            self.section("Sera perdu")
            for line in lost:
                label(self.body, f"✕  {line}", 12, color=t.tone("danger")[0], wraplength=480).pack(fill="x")
        if kept:
            self.section("Sera conservé")
            for line in kept:
                label(self.body, f"✓  {line}", 12, color=t.tone("success")[0], wraplength=480).pack(fill="x")
        self.section(f"Tape {CONFIRM_WORD} pour confirmer")
        self.var = tk.StringVar()
        entry = ctk.CTkEntry(self.body, textvariable=self.var, height=34, font=t.font(13),
                             placeholder_text=CONFIRM_WORD)
        entry.pack(fill="x")
        self.ok = self.buttons(("Annuler", self.cancel, "secondary"), (ok_text, self._confirm, "danger"))[1]
        self.ok.configure(state="disabled")
        self.var.trace_add("write", lambda *_: self.ok.configure(
            state="normal" if is_confirm_word(self.var.get()) else "disabled"))
        entry.bind("<Return>", lambda _e: self._confirm())
        self.after(80, entry.focus_set)

    def _confirm(self) -> None:
        if is_confirm_word(self.var.get()):
            self.close(True)


class TextInputDialog(Modal):
    def __init__(self, master, title: str, prompt: str, initial: str = "", ok_text: str = "Valider",
                 secret: bool = False, validate: Callable[[str], str | None] | None = None,
                 allow_unchanged: bool = True) -> None:
        super().__init__(master, title, width=480)
        self.validate = validate
        self.initial, self.allow_unchanged = initial.strip(), allow_unchanged
        self.heading(title)
        self.text(prompt, t.MUTED, 12)
        self.var = tk.StringVar(value=initial)
        self.entry = ctk.CTkEntry(self.body, textvariable=self.var, height=34, font=t.font(13),
                                  show="•" if secret else "")
        self.entry.pack(fill="x", pady=(10, 0))
        self.error = label(self.body, "", 12, color=t.tone("danger")[0])
        self.error.pack(fill="x", pady=(4, 0))
        self.ok = self.buttons(("Annuler", self.cancel, "secondary"), (ok_text, self._ok, "primary"))[1]
        self.entry.bind("<Return>", lambda _e: self._ok())
        self.var.trace_add("write", lambda *_: self._check())
        self._check(show=False)
        self.after(80, lambda: (self.entry.focus_set(), self.entry.select_range(0, "end")))

    def _problem(self, value: str) -> str | None:
        """Raison de refus ("" : rien à signaler, mais rien à valider non plus), None si valide."""
        if not value:
            return ""
        if not self.allow_unchanged and value == self.initial:
            return ""
        return self.validate(value) if self.validate else None

    def _check(self, show: bool = True) -> None:
        problem = self._problem(self.var.get().strip())
        self.ok.configure(state="normal" if problem is None else "disabled")
        self.error.configure(text=(problem or "") if show else "")

    def _ok(self) -> None:
        value = self.var.get().strip()
        if self._problem(value) is not None:
            return
        self.close(value)


class InfoDialog(Modal):
    def __init__(self, master, title: str, paragraphs: list[str], code: str | None = None,
                 width: int = 560) -> None:
        super().__init__(master, title, width=width)
        self.heading(title)
        for para in paragraphs:
            self.text(para, size=12)
        if code:
            box = ctk.CTkTextbox(self.body, height=min(200, 22 * (code.count("\n") + 2)), font=t.mono(12),
                                 fg_color=t.SURFACE_2, wrap="none")
            box.insert("1.0", code)
            box.configure(state="disabled")
            box.pack(fill="x", pady=(12, 0))
            self.buttons(("Copier", lambda: self._copy(code), "secondary"), ("OK", self.cancel, "primary"))
        else:
            self.buttons(("OK", self.cancel, "primary"))

    def _copy(self, code: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(code)


def confirm(master, title: str, message: str, ok_text: str = "Confirmer", danger: bool = False,
            details: list[str] | None = None, checkbox: str | None = None) -> tuple[bool, bool]:
    dlg = ConfirmDialog(master, title, message, ok_text, danger, details, checkbox)
    return bool(dlg.show()), dlg.check_var.get()


def confirm_typed(master, title: str, message: str, expected: str, ok_text: str,
                  lost: list[str] = (), kept: list[str] = ()) -> bool:
    return bool(TypedConfirmDialog(master, title, message, expected, ok_text, lost, kept).show())


def ask_text(master, title: str, prompt: str, initial: str = "", ok_text: str = "Valider",
             secret: bool = False, validate=None, allow_unchanged: bool = True) -> str | None:
    """Saisie d'un texte ; le bouton reste grisé tant que la valeur est vide, invalide ou (au choix) inchangée."""
    return TextInputDialog(master, title, prompt, initial, ok_text, secret, validate, allow_unchanged).show()


def info(master, title: str, paragraphs: list[str], code: str | None = None) -> None:
    InfoDialog(master, title, paragraphs, code).show()
