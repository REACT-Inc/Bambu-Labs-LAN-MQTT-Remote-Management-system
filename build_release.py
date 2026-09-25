"""Build a manifest-bearing release without local credentials or generated data."""
import hashlib,json,re,sys,zipfile
from pathlib import Path
from update_package import runtime_path,inspect_package

def build(version,output):
    if not re.fullmatch('[A-Za-z0-9._-]{1,80}',version):raise ValueError('Invalid version')
    root=Path(__file__).resolve().parent
    files=[]
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p.is_symlink():continue
        name=p.relative_to(root).as_posix()
        if name=='agent_service_windows.py':continue
        if name in ('.gitignore','.github/workflows/release.yml','.github/workflows/tests.yml'):
            files.append((p,name));continue
        if any(part.startswith('.') or part in ('__pycache__','venv','node_modules') for part in p.relative_to(root).parts):continue
        if runtime_path(name) or (len(Path(name).parts)==1 and p.suffix=='.sh') or (name.startswith('tests/') and p.suffix=='.py'):files.append((p,name))
    contents={n:p.read_bytes() for p,n in files}
    contents['version.py']=('VERSION = '+repr(version.lstrip('v'))+'\n').encode()
    manifest={'format':1,'application':'3d-printer-management','version':version,'files':{n:hashlib.sha256(data).hexdigest() for n,data in contents.items() if runtime_path(n)}}
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for n,data in contents.items():z.writestr('printer-management/'+n,data)
        z.writestr('printer-management/update-manifest.json',json.dumps(manifest,indent=2)+'\n')
    return inspect_package(output)

if __name__=='__main__':print(build(sys.argv[1],sys.argv[2]))
