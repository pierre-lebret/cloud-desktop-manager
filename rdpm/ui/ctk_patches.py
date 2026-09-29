"""Correctifs customtkinter appliqués à l'import de rdpm.ui.

CTkBaseClass rejoue son dernier grid()/pack()/place() à chaque changement de mise à l'échelle
(fenêtre déplacée sur un écran d'un autre DPI), mais n'oublie pas ce grid() sur grid_remove() :
les widgets masqués réapparaissaient. grid_remove() l'oublie désormais, et un grid() sans
argument réaffiche le widget avec ses options d'origine (remises à l'échelle correctement).
"""

from __future__ import annotations

import tkinter as tk

from customtkinter.windows.widgets.core_widget_classes.ctk_base_class import CTkBaseClass

_grid = CTkBaseClass.grid


def _grid_remove(self) -> None:
    call = self._last_geometry_manager_call
    if call is not None and call["function"].__name__.startswith("grid"):
        self._removed_grid_kw = call["kwargs"]
    self._last_geometry_manager_call = None
    tk.Grid.grid_remove(self)


def _grid_restore(self, **kwargs):
    if not kwargs:
        kwargs = getattr(self, "_removed_grid_kw", None) or {}
    self._removed_grid_kw = None
    return _grid(self, **kwargs)


CTkBaseClass.grid_remove = _grid_remove
CTkBaseClass.grid = _grid_restore
