from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
analysis = Analysis([str(root / 'packaging/desktop_entry.py')], pathex=[str(root)],
    binaries=[], datas=collect_data_files('paper_reader') + [(str(root / 'config/topics.json'), 'config'),
        (str(root / 'paper_reader/complex_worker.py'), 'paper_reader')],
    hiddenimports=['pystray._win32', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
                   'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
                   'webview.platforms.winforms', 'webview.platforms.edgechromium'],
    hookspath=[str(root / 'packaging/hooks')],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3', 'tkinter', 'pandas', 'matplotlib', 'scipy', 'torch', 'docling', 'IPython', 'pytest', 'playwright', 'reportlab'],
    noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='Asteria',
          console=False, debug=False, upx=False, icon=str(root / 'build/windows/paper-reader.ico'))
collect = COLLECT(exe, analysis.binaries, analysis.datas, name='Asteria', upx=False)
