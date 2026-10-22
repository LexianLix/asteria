"""Build Asteria for Windows without packaging personal data or credentials."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def distributions():
    pending = ['fastapi', 'uvicorn', 'pdfplumber', 'python-multipart', 'psutil',
               'PyJWT', 'cryptography', 'pywebview', 'pystray', 'Pillow']
    found = {}
    while pending:
        name = pending.pop()
        key = canonicalize_name(name)
        if key in found:
            continue
        package = metadata.distribution(name)
        found[key] = package
        for entry in package.requires or []:
            requirement = Requirement(entry)
            if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
                pending.append(requirement.name)
    return found


def private_file(path):
    return (path.suffix.lower() in {'.sqlite', '.db', '.pdf', '.pem', '.key', '.pfx'}
            or path.name in {'desktop-settings.json', 'desktop-ready.json',
                             'chatgpt-auth.json', 'chatgpt-host.json', '.env'}
            or '.paper-reader-private' in path.parts
            or '__pycache__' in path.parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--iscc', type=Path, help='Path to Inno Setup ISCC.exe')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from paper_reader import __version__
    from paper_reader.lifecycle import tray_image

    work = root / 'build/windows'
    output = root / 'dist/windows'
    if (output / 'Asteria').exists():
        raise RuntimeError('Output already exists. Use a clean checkout or preserve and move the previous bundle before rebuilding.')
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    tray_image().save(work / 'paper-reader.ico', sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--log-level', 'WARN', '--noconfirm',
                    '--distpath', str(output), '--workpath', str(work / 'pyinstaller'),
                    str(root / 'packaging/paper-reader.spec')], cwd=root, check=True)
    app = output / 'Asteria'
    notices = app / 'third-party-notices'
    notices.mkdir()
    versions = {}
    for name, package in sorted(distributions().items()):
        versions[name] = package.version
        for entry in package.files or []:
            if any(part.lower() in {'licenses', 'license', 'license.txt', 'license.md',
                                    'copying', 'copying.txt', 'notice', 'notice.txt'} for part in entry.parts):
                source = Path(package.locate_file(entry))
                if source.is_file():
                    relative = Path(*(part if part not in {'.', '..'} else '_' for part in entry.parts))
                    target = notices / name / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    (notices / 'versions.json').write_text(json.dumps(versions, indent=2), encoding='utf-8')
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copyfile(python_license, notices / 'Python-LICENSE.txt')
    for filename in ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md']:
        shutil.copyfile(root / filename, app / filename)
    files = sorted(path for path in app.rglob('*') if path.is_file())
    for path in files:
        if private_file(path):
            raise RuntimeError('Refusing to package private file: ' + str(path.relative_to(app)))
    archive = output / f'Asteria-{__version__}-windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(output))
    artifacts = [archive]
    if args.iscc:
        subprocess.run([str(args.iscc.resolve(strict=True)), '/Q', '/DAppVersion=' + __version__,
                        str(root / 'packaging/windows-installer.iss')], check=True, cwd=root)
        artifacts.append(output / f'Asteria-{__version__}-Setup-x64.exe')
    sums = [(hashlib.sha256(path.read_bytes()).hexdigest(), path.name) for path in artifacts]
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{digest}  {name}\n' for digest, name in sums), encoding='utf-8')
    print(json.dumps({'version': __version__, 'artifacts': dict((name, digest) for digest, name in sums)}, indent=2))


if __name__ == '__main__':
    main()
