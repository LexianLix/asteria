"""Read-only WebView2 discovery; never install runtimes or change the registry."""
import ctypes
import os
from pathlib import Path
import platform


def sdk_probe(webview):
    architecture = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'x86'
    path = Path(webview.__file__).parent / 'lib/runtimes' / ('win-' + architecture) / 'native/WebView2Loader.dll'
    loader = ctypes.WinDLL(str(path))
    available = loader.GetAvailableCoreWebView2BrowserVersionString
    available.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    available.restype = ctypes.c_long
    free = ctypes.OleDLL('ole32').CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    def probe(folder):
        pointer = ctypes.c_void_p()
        # The native loader requires a Windows-normalized path, not a raw
        # forward-slash string. HRESULT failures must not become a success.
        result = available(str(Path(folder)) if folder is not None else None, ctypes.byref(pointer))
        try:
            return ctypes.wstring_at(pointer) if result >= 0 and pointer.value else None
        finally:
            if pointer.value: free(pointer)
    return probe


def supported(version):
    try:
        return int(version.split('.', 1)[0]) >= 86
    except (AttributeError, ValueError, TypeError):
        return False


def configure_windows_runtime(webview, *, roots=None, probe=None):
    probe = probe or sdk_probe(webview)
    explicit = webview.settings.get('WEBVIEW2_RUNTIME_PATH')
    if explicit:
        if not supported(probe(Path(explicit))):
            raise RuntimeError('指定的 Microsoft WebView2 Runtime 不可用，请修复后重试。')
        return str(Path(explicit))
    if roots is None:
        roots = [Path(os.environ[name]) / 'Microsoft/EdgeWebView/Application'
            for name in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA') if os.environ.get(name)]
    candidates = []
    for root in dict.fromkeys(map(Path, roots)):
        if not root.is_dir(): continue
        for folder in root.iterdir():
            parts = folder.name.split('.')
            if len(parts) != 4 or not all(p.isdigit() for p in parts): continue
            if folder.resolve() != folder.absolute() or not (folder / 'msedgewebview2.exe').is_file(): continue
            candidates.append((tuple(map(int, parts)), folder))
    for _, folder in sorted(candidates, reverse=True):
        if supported(probe(folder)):
            webview.settings['WEBVIEW2_RUNTIME_PATH'] = str(folder)
            return str(folder)
    if supported(probe(None)):
        return None  # Standard SDK/registry discovery remains available.
    raise RuntimeError('未检测到可用的 Microsoft WebView2 Runtime，请安装或修复后重试；不会回退旧版浏览器。')
