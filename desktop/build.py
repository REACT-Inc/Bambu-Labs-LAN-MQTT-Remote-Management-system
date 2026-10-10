"""Build the desktop app's .exe with PyInstaller: one file, no console window, with the app's icon and version.

    pip install -r desktop/requirements-build.txt
    python desktop/build.py v1.7.8-beta.1

Run it on Windows (an .exe can only be built there); the GitHub Actions workflow .github/workflows/desktop.yml does it
for pull requests and releases. The result is dist/3d-printer-management-desktop.exe.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = '3d-printer-management-desktop'
# Other platforms' web view backends and GUI toolkits: never used by the Windows app, so left out of the .exe.
EXCLUDE = ('tkinter', 'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.cocoa', 'webview.platforms.cef',
           'webview.platforms.android', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'gi')
VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'REACT-Inc'),
      StringStruct('FileDescription', '3D Printer Management'),
      StringStruct('FileVersion', {text!r}),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '3D Printer Management'),
      StringStruct('ProductVersion', {text!r})])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def version_numbers(version):
    """'v1.7.8-beta.1' -> (1, 7, 8, 1): Windows wants four numbers in an .exe's version."""
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-(?:alpha|beta)\.(\d+))?', version)
    return tuple(int(part or 0) for part in match.groups()) if match else (0, 0, 0, 0)


def prepare(version, work):
    """The generated files: the version the app shows, the .exe's version details, and its icon."""
    sys.path.insert(0, str(HERE))
    import icon
    work.mkdir(parents=True, exist_ok=True)
    (work / '_version.py').write_text(f'VERSION = {version.lstrip("v")!r}\n', encoding='utf-8')
    (work / 'version_info.txt').write_text(
        VERSION_INFO.format(numbers=version_numbers(version), text=version.lstrip('v'), name=NAME), encoding='utf-8')
    icon.write_ico(work / 'icon.ico')


def command(work, dist):
    return [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed', '--name', NAME,
            '--icon', str(work / 'icon.ico'), '--version-file', str(work / 'version_info.txt'),
            '--add-data', f'{HERE / "connect.html"}{os.pathsep}.', '--paths', str(work), '--paths', str(HERE),
            '--distpath', str(dist), '--workpath', str(work / 'pyinstaller'), '--specpath', str(work),
            *[f'--exclude-module={module}' for module in EXCLUDE], str(HERE / 'desktop_app.py')]


def main(argv):
    version = argv[0] if argv else 'dev'
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,40}', version):
        raise SystemExit('Give a version such as v1.7.8-beta.1.')
    work, dist = HERE / 'build', HERE.parent / 'dist'
    prepare(version, work)
    subprocess.run(command(work, dist), check=True)
    print(dist / f'{NAME}.exe')


if __name__ == '__main__':
    main(sys.argv[1:])
