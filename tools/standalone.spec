"""Freeze the existing CLI and embed an already staged native release.

python -m PyInstaller --distpath PACKAGE --workpath BUILD tools/standalone.spec -- --package PACKAGE
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig

parser = argparse.ArgumentParser()
parser.add_argument('--package', required=True, type=Path)
package = parser.parse_args().package.resolve(strict=True)
root = Path(SPECPATH).parent

analysis = Analysis([str(root / 'tools/sim')], pathex=[str(root)],
                    excludes=['qemu', 'mujoco', 'numpy', 'aiohttp', 'mcap',
                              'crccheck', 'pytest'])

licenses = package / 'licenses' / 'standalone'
licenses.mkdir()
python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
if not python_license.is_file():
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')
dependencies = []
for name in ('PyInstaller', 'pyinstaller-hooks-contrib', 'PyYAML', 'altgraph',
             'packaging', 'pefile', 'pywin32-ctypes', 'setuptools'):
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        continue
    destination = licenses / name
    destination.mkdir()
    for entry in distribution.files or ():
        if any(part.lower().startswith(('license', 'copying', 'copyright')) for part in entry.parts):
            source = distribution.locate_file(entry)
            if source.is_file():
                target = destination / entry
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    (destination / 'METADATA').write_text(distribution.read_text('METADATA'), encoding='utf-8')
    dependencies.append({'name': name, 'version': distribution.version})

# Keep the licenses/version identities of system libraries selected by the
# freezer. The QEMU ELF is appended below so this does not pull GLib into Python.
system_packages = {}
if sys.platform == 'linux':
    for _, source, _ in analysis.binaries:
        if Path(source).is_relative_to(sys.prefix):
            continue
        listing = subprocess.check_output(['dpkg-query', '-S', str(Path(source).resolve())], text=True)
        owner = listing.split(': ', 1)[0]
        if owner in system_packages:
            continue
        copyright_file = Path('/usr/share/doc') / owner.split(':')[0] / 'copyright'
        shutil.copy2(copyright_file, licenses / (owner.replace(':', '_') + '-copyright'))
        system_packages[owner] = subprocess.check_output(
            ['dpkg-query', '-W', '-f=${Version}', owner], text=True)

# These exact files already have QEMU/MSYS2 licenses and source provenance.
# Append after Analysis to retain their bytes and dependency boundary unchanged.
analysis.binaries += [(f'bin/{path.name}', str(path), 'BINARY')
                      for path in sorted((package / 'bin').iterdir()) if path.is_file()]
executable = EXE(PYZ(analysis.pure), analysis.scripts, analysis.binaries,
                 analysis.datas, [], name='sim', console=True, strip=False, upx=False)
provenance_path = package / 'provenance.json'
provenance = json.loads(provenance_path.read_text(encoding='utf-8'))
binary = package / ('sim.exe' if sys.platform == 'win32' else 'sim')
with binary.open('rb') as stream:
    checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
provenance['standalone'] = {
    'file': binary.name,
    'sha256': checksum,
    'python': platform.python_version(),
    'dependencies': dependencies,
    'system_packages': system_packages,
    'scope': 'firmware terminal and reset-paused GDB listener',
}
provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
