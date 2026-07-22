from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from typing import Callable


class ClipboardError(RuntimeError):
    pass


class ClipboardBusyError(ClipboardError):
    pass


@dataclass
class ClipboardResult:
    success: bool
    method: str = ""
    error: str | None = None
    attempts: int = 0


def copy_text(
    text: str,
    retries: int = 6,
    delay_ms: int = 75,
    windows_backend: Callable[[str], None] | None = None,
    fallback: Callable[[str], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> ClipboardResult:
    if not text:
        return ClipboardResult(False, error="Nothing to copy", attempts=0)

    backend = windows_backend
    if backend is None and os.name == "nt":
        backend = _set_windows_clipboard_once

    last_error = ""
    if backend is not None:
        for attempt in range(1, max(1, retries) + 1):
            try:
                backend(text)
                return ClipboardResult(True, method="windows", attempts=attempt)
            except ClipboardBusyError as exc:
                last_error = str(exc)
                if attempt < max(1, retries):
                    sleeper(max(0, delay_ms) / 1000.0)
            except Exception as exc:
                last_error = str(exc)
                break

    fallback_backend = fallback or _set_tk_clipboard
    try:
        fallback_backend(text)
        return ClipboardResult(
            True,
            method="tkinter" if fallback is None else "fallback",
            attempts=max(1, retries) if backend is not None else 1,
        )
    except Exception as exc:
        fallback_error = str(exc)
        message = last_error
        if fallback_error:
            message = f"{message}; fallback: {fallback_error}" if message else fallback_error
        return ClipboardResult(
            False,
            method="",
            error=message or "Clipboard operation failed",
            attempts=max(1, retries) if backend is not None else 1,
        )


def set_clipboard(text: str, retries: int = 6, delay_ms: int = 75) -> None:
    result = copy_text(text, retries=retries, delay_ms=delay_ms)
    if not result.success:
        raise ClipboardError(result.error or "Clipboard operation failed")


def _set_windows_clipboard_once(text: str) -> None:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    cf_unicode_text = 13
    gmem_moveable = 0x0002
    encoded = (text + "\0").encode("utf-16-le")

    if not user32.OpenClipboard(None):
        error = ctypes.get_last_error()
        raise ClipboardBusyError(f"Windows clipboard is busy (error {error})")

    handle: int | None = None
    ownership_transferred = False
    try:
        if not user32.EmptyClipboard():
            error = ctypes.get_last_error()
            raise ClipboardError(f"Could not clear Windows clipboard (error {error})")

        raw_handle = kernel32.GlobalAlloc(gmem_moveable, len(encoded))
        if not raw_handle:
            error = ctypes.get_last_error()
            raise ClipboardError(f"Could not allocate clipboard memory (error {error})")
        handle = int(raw_handle)

        pointer = kernel32.GlobalLock(ctypes.c_void_p(handle))
        if not pointer:
            error = ctypes.get_last_error()
            raise ClipboardError(f"Could not lock clipboard memory (error {error})")
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(ctypes.c_void_p(handle))

        result = user32.SetClipboardData(cf_unicode_text, ctypes.c_void_p(handle))
        if not result:
            error = ctypes.get_last_error()
            raise ClipboardError(f"Could not set clipboard data (error {error})")
        ownership_transferred = True
    finally:
        user32.CloseClipboard()
        if handle is not None and not ownership_transferred:
            kernel32.GlobalFree(ctypes.c_void_p(handle))


def _set_tk_clipboard(text: str) -> None:
    import tkinter

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    finally:
        root.destroy()
