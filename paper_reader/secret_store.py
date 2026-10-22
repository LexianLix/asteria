"""Windows user-bound DPAPI. No silent plaintext fallback for API keys."""
from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _convert(value: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise ValueError("系统密钥保护当前仅支持 Windows；其他平台请使用环境变量配置 Key")
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    api.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    api.restype = wintypes.BOOL
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError("无法使用当前 Windows 用户的系统密钥保护数据")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        free(output.pbData)


def protect(value: str) -> str:
    return base64.b64encode(_convert(value.encode(), decrypt=False)).decode()


def reveal(value: str) -> str:
    return _convert(base64.b64decode(value, validate=True), decrypt=True).decode()
