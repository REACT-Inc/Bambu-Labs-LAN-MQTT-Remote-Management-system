"""Version-1 update package validation. Standard library only; also installed root-owned."""
import ast
import hashlib
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

MAX_ZIP=32*1024*1024
MAX_EXPANDED=128*1024*1024
REQUIRED={'main.py','core.py','dashboard.py','queueing.py','requirements.txt','static/index.html','static/app.js'}

PACKAGE_DIRS={'swapMod','failureDetection','Updater','discord_Intergration','laptopManagement_Intergration'}


def runtime_path(name):
    p=PurePosixPath(name)
    if len(p.parts)==1:
        return p.suffix in ('.py','.md') or name in ('requirements.txt','bambu_error_catalog.json','BAMBU_RESOURCE_LICENSE.txt')
    if p.parts[0] in PACKAGE_DIRS and p.suffix in ('.py','.md'):
        return all(part.replace('_','').isalnum() for part in p.parts[:-1])
    return p.parts[0]=='static' and p.suffix.lower() in ('.js','.css','.html','.svg','.png','.jpg','.ico','.woff','.woff2')


def _inspect_package(source,output=None):
    """No execution, extractall, paths from manifest outside the permitted runtime tree."""
    with zipfile.ZipFile(source) as z:
        infos=z.infolist()
        if len(infos)>1500:raise ValueError('Too many ZIP entries.')
        names=set();total=0
        for info in infos:
            name=info.filename;parts=PurePosixPath(name).parts
            if not name or '\\' in name or '\x00' in name or name.startswith('/') or any(x in ('..','.') for x in name.split('/')) or ':' in name:
                raise ValueError('Unsafe ZIP path.')
            if name.rstrip('/')!=str(PurePosixPath(name)):raise ValueError('Noncanonical ZIP path.')
            if not parts or parts[0]!='printer-management':raise ValueError('ZIP must contain the printer-management folder.')
            if name in names:raise ValueError('Duplicate ZIP entry.')
            names.add(name)
            mode=info.external_attr>>16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)):raise ValueError('Links and special files are not allowed.')
            if info.flag_bits&1:raise ValueError('Encrypted ZIP files are not supported.')
            total+=info.file_size
            if total>MAX_EXPANDED:raise ValueError('Expanded package exceeds 128 MiB.')
        try:manifest=json.loads(z.read('printer-management/update-manifest.json'))
        except (KeyError,ValueError):raise ValueError('This ZIP has no valid updater manifest. Use a new management release ZIP.')
        if not isinstance(manifest,dict) or manifest.get('format')!=1 or manifest.get('application')!='3d-printer-management':raise ValueError('Wrong application or update format.')
        version=manifest.get('version');files=manifest.get('files')
        if not isinstance(version,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}',version):raise ValueError('Invalid release version.')
        if not isinstance(files,dict) or not REQUIRED<=set(files) or len(files)>500:raise ValueError('Required application files are missing.')
        for name,digest in files.items():
            if not isinstance(name,str) or not runtime_path(name) or not re.fullmatch('[0-9a-f]{64}',str(digest)):
                raise ValueError('Invalid runtime file in manifest.')
            # Every manifest key must match a validated archive entry exactly.
            full='printer-management/'+name
            if full not in names or full.endswith('/'):raise ValueError('Manifest file is missing.')
            data=z.read(full)
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Package checksum mismatch.')
            if name.endswith('.py'):
                try:ast.parse(data,filename=name)
                except (SyntaxError,ValueError) as exc:raise ValueError('Invalid Python in package.') from exc
            if name=='requirements.txt':
                for line in data.decode().splitlines():
                    line=line.strip()
                    if not line or line.startswith('#'):continue
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~]+[A-Za-z0-9.*+-]+(?:,[<>=!~]+[A-Za-z0-9.*+-]+)*)?',line):
                        raise ValueError('Only package names and version constraints are allowed in requirements.txt.')
            if output is not None:
                target=output/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    return {'version':version,'files':len(files),'expanded_bytes':total}


def inspect_package(source,output=None):
    try:return _inspect_package(source,output)
    except (zipfile.BadZipFile,UnicodeError,RuntimeError) as exc:raise ValueError("Invalid or damaged ZIP package.") from exc
