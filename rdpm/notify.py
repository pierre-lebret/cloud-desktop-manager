"""Signaux système discrets : clignotement de la fenêtre et bip."""

from __future__ import annotations

import ctypes
import sys


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint)]


FLASHW_ALL = 0x3
FLASHW_TIMERNOFG = 0xC


def flash_window(tk_root) -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(tk_root.winfo_id())
        info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 5, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except (OSError, AttributeError):
        pass


def beep(kind: str = "info") -> None:
    if sys.platform != "win32":
        return
    import winsound
    flag = {"error": winsound.MB_ICONHAND, "warning": winsound.MB_ICONEXCLAMATION}.get(
        kind, winsound.MB_ICONASTERISK)
    try:
        winsound.MessageBeep(flag)
    except RuntimeError:
        pass


def single_instance(name: str):
    """Renvoie un handle de mutex, ou None si une autre instance tourne déjà."""
    if sys.platform != "win32":
        return object()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle
